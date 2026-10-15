import logging
import queue
import unittest
from unittest.mock import MagicMock, call, mock_open, patch

from helix_stage1 import automation, selectors
from helix_stage1 import ui
from helix_stage1.ui import _creation_texts


INCIDENT_ID = "INC000009556476"
TITLE = "Corte FO via Entel"


class CreateFlowTests(unittest.TestCase):
    def setUp(self) -> None:
        self.events = queue.Queue()
        logger = logging.getLogger("test.create_flow")
        logger.setLevel(logging.CRITICAL)
        self.worker = automation.AutomationWorker(
            queue.Queue(), self.events, logger
        )

    def _prepare_worker(self) -> MagicMock:
        page = MagicMock()
        self.worker.console_page = page
        self.worker._ensure_browser = MagicMock()
        self.worker._navigate_to_console = MagicMock()
        self.worker._settle = MagicMock()
        self.worker._check_unexpected_ui = MagicMock()
        self.worker._wait_for_single_editable_match = MagicMock()
        self.worker._wait_for_single_visible_match = MagicMock()
        self.worker._select_new_incident_template = MagicMock()
        self.worker._select_new_incident_failure_condition = MagicMock()
        self.worker._set_new_incident_title_fields = MagicMock()
        self.worker._set_new_incident_categorization_company = MagicMock()
        self.worker._read_new_incident_id = MagicMock(return_value=INCIDENT_ID)
        self.worker._scroll_remedy_down = MagicMock()
        customer_field = self.worker._wait_for_single_editable_match.return_value
        customer_field.input_value.return_value = "APELLIDO EJEMPLO"
        save_button = MagicMock()
        save_button.is_disabled.return_value = False
        self.worker._wait_for_visible_locator_matches = MagicMock(return_value=[save_button])
        self.save_button = save_button
        return page

    def _run_create(self) -> list[automation.WorkerEvent]:
        with patch.object(automation, "HELIX_URL", "https://example.invalid"), patch.object(
            selectors, "OPEN_NEW_INCIDENT", MagicMock()
        ) as menu_spec, patch.object(
            selectors, "NEW_INCIDENT_SAVE_BUTTON", MagicMock()
        ):
            menu_spec.build.return_value.count.return_value = 1
            self.worker._open_new_incident("APELLIDO EJEMPLO", TITLE)
        events = []
        while not self.events.empty():
            events.append(self.events.get_nowait())
        return events

    def test_save_click_reports_id_and_title_without_searching(self) -> None:
        self._prepare_worker()
        actions = []
        self.worker._scroll_remedy_down.side_effect = lambda *_args, **_kwargs: actions.append(
            "scroll"
        )
        self.save_button.click.side_effect = lambda *_args, **_kwargs: actions.append("save")

        events = self._run_create()

        self.assertEqual(actions, ["scroll", "save", "save"])
        self.assertEqual(self.save_button.click.call_count, 2)
        self.assertTrue(self.save_button.click.call_args_list[0].kwargs["trial"])
        self.assertNotIn("trial", self.save_button.click.call_args_list[1].kwargs)
        sent = [event for event in events if event.kind == "create_save_sent"]
        self.assertEqual([(event.incident, event.title) for event in sent], [(INCIDENT_ID, TITLE)])

    def test_uncertain_click_never_retries_save_or_reports_sent(self) -> None:
        self._prepare_worker()
        self.save_button.click.side_effect = [
            None,
            automation.PlaywrightTimeoutError("No se pudo confirmar el clic"),
        ]

        events = self._run_create()

        self.assertEqual(self.save_button.click.call_count, 2)
        self.assertFalse(any(event.kind == "create_save_sent" for event in events))
        self.assertEqual(
            [event.incident for event in events if event.kind == "create_unverified"],
            [INCIDENT_ID],
        )

    def test_blocked_save_button_does_not_send_click_or_completion(self) -> None:
        self._prepare_worker()
        self.save_button.click.side_effect = automation.PlaywrightTimeoutError(
            "Guardar no está listo para recibir el clic"
        )

        events = self._run_create()

        self.save_button.click.assert_called_once()
        self.assertTrue(self.save_button.click.call_args.kwargs["trial"])
        self.assertFalse(any(event.kind == "create_save_sent" for event in events))
        self.assertTrue(any(event.kind == "create_error" for event in events))

    def test_second_creation_returns_home_before_opening_new_form(self) -> None:
        self._prepare_worker()
        actions = []
        home_button = MagicMock()
        home_button.is_disabled.return_value = False
        home_button.click.side_effect = lambda **_kwargs: actions.append("home")
        self.worker._wait_for_single_visible_match.side_effect = (
            lambda _page, _locator, description, *_args: (
                home_button if description == "botón Inicio" else MagicMock()
            )
        )
        with patch.object(selectors, "HOME_BUTTON", MagicMock()):
            first_events = self._run_create()
            self.assertFalse(actions)
            second_events = self._run_create()

        self.assertEqual(actions, ["home"])
        self.assertTrue(any(event.kind == "create_save_sent" for event in first_events))
        self.assertTrue(any(event.kind == "create_save_sent" for event in second_events))
        self.assertTrue(self.worker._create_form_opened)

    def test_creation_txt_contains_only_ids_and_titles(self) -> None:
        app = ui.HelixApp.__new__(ui.HelixApp)
        app._show_creation_output = MagicMock(side_effect=lambda incident, title: f"{incident}, {title}")
        app.new_incident_title_var = MagicMock()
        app.status_var = MagicMock()
        app._append_log = MagicMock()
        app._finish_current_operation = MagicMock()
        log_dir = MagicMock()
        log_path = log_dir.__truediv__.return_value
        log_path.open = mock_open()
        with patch.object(ui, "LOG_DIR", log_dir), patch.object(ui.messagebox, "showinfo"), patch.object(
            ui.messagebox, "showwarning"
        ) as warning:
            for incident, title in ((INCIDENT_ID, TITLE), ("INC000009556477", "Segundo corte")):
                app._handle_event(automation.WorkerEvent("create_save_sent", "", incident, title))
            warning.assert_not_called()

        log_dir.__truediv__.assert_called_with("Incidencias creadas.txt")
        self.assertEqual(
            log_path.open.return_value.__enter__.return_value.write.call_args_list,
            [call(f"{INCIDENT_ID}, {TITLE}\n"), call("INC000009556477, Segundo corte\n")],
        )

    def test_incident_id_is_validated_before_save(self) -> None:
        page = MagicMock()
        field = MagicMock()
        field.input_value.return_value = INCIDENT_ID
        self.worker._wait_for_visible_locator_matches = MagicMock(return_value=[field])
        self.assertEqual(self.worker._read_new_incident_id(page), INCIDENT_ID)

        field.input_value.return_value = "INC123"
        with self.assertRaises(automation.VerificationError):
            self.worker._read_new_incident_id(page)

    def test_copyable_summary_and_email_keep_the_exact_title(self) -> None:
        title = "Corte FO vía Entel, OSN 8800, entre COLINA-WDM-CR04 y CHICUREO-WDM-CR04"

        summary, email = _creation_texts(INCIDENT_ID, title)

        self.assertEqual(summary, f"{INCIDENT_ID}, {title}")
        self.assertIn(f'evento entre "{title}"', email)
        self.assertIn(f'número de incidencia "{INCIDENT_ID}"', email)


if __name__ == "__main__":
    unittest.main()
