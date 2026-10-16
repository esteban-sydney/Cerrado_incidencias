import logging
import json
import queue
import unittest
from unittest.mock import MagicMock, call, mock_open, patch

from helix_stage1 import automation, email_catalog, selectors
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

    def test_each_creation_returns_home_after_save(self) -> None:
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
            self.assertEqual(actions, ["home"])
            self.assertFalse(self.worker._create_form_opened)
            second_events = self._run_create()

        self.assertEqual(actions, ["home", "home"])
        self.assertTrue(any(event.kind == "create_save_sent" for event in first_events))
        self.assertTrue(any(event.kind == "create_save_sent" for event in second_events))
        self.assertTrue(any(event.kind == "create_home_ready" for event in first_events))
        self.assertTrue(any(event.kind == "create_home_ready" for event in second_events))
        self.assertFalse(self.worker._create_form_opened)

    def test_failed_return_home_keeps_save_result_and_warns(self) -> None:
        self._prepare_worker()
        self.worker._return_home_before_new_incident = MagicMock(
            side_effect=automation.AutomationError("Inicio no disponible")
        )

        events = self._run_create()

        self.assertTrue(any(event.kind == "create_save_sent" for event in events))
        self.assertTrue(any(event.kind == "create_home_warning" for event in events))
        self.assertFalse(any(event.kind == "create_unverified" for event in events))
        self.assertTrue(self.worker._create_form_opened)

    def test_open_email_uses_default_mail_app_without_sending(self) -> None:
        app = ui.HelixApp.__new__(ui.HelixApp)
        app.created_summary = f"{INCIDENT_ID}, {TITLE}"
        app.created_email_text = MagicMock()
        app.created_email_text.get.return_value = "Estimados:\n\nTexto de prueba."
        app.email_to_var = MagicMock()
        app.email_to_var.get.return_value = ui.NO_EMAIL_GROUP
        app.email_cc_var = MagicMock()
        app.email_cc_var.get.return_value = ui.NO_EMAIL_GROUP

        with patch.object(ui.os, "startfile") as open_mail:
            app._open_creation_email()

        mailto = open_mail.call_args.args[0]
        self.assertTrue(mailto.startswith("mailto:?"))
        self.assertIn(f"subject={INCIDENT_ID}%2C%20", mailto)
        self.assertIn("body=Estimados%3A", mailto)
        self.assertIn("Texto%20de%20prueba", mailto)
        self.assertNotIn("+", mailto)

    def test_open_email_includes_every_to_and_cc_address(self) -> None:
        app = ui.HelixApp.__new__(ui.HelixApp)
        app.created_summary = f"{INCIDENT_ID}, {TITLE}"
        app.created_email_text = MagicMock()
        app.created_email_text.get.return_value = "Correo de prueba"
        app.email_to_var = MagicMock()
        app.email_to_var.get.return_value = "Tigo · Cortes"
        app.email_cc_var = MagicMock()
        app.email_cc_var.get.return_value = "Claro · Back office"

        with patch.dict(ui.EMAIL_RECIPIENT_GROUPS, {
            "Tigo · Cortes": ("uno@example.com", "dos@example.com"),
            "Claro · Back office": ("claro1@example.com", "claro2@example.com"),
        }), patch.object(ui.os, "startfile") as open_mail:
            app._open_creation_email()

        mailto = open_mail.call_args.args[0]
        self.assertTrue(mailto.startswith("mailto:uno@example.com,dos@example.com?"))
        self.assertIn("cc=claro1%40example.com%2Cclaro2%40example.com", mailto)

    def test_entel_email_uses_only_selected_group(self) -> None:
        app = ui.HelixApp.__new__(ui.HelixApp)
        app.created_summary = f"{INCIDENT_ID}, {TITLE}"
        app.created_email_text = MagicMock()
        app.created_email_text.get.return_value = "Correo de prueba"
        app.email_to_var = MagicMock()
        app.email_to_var.get.return_value = "Entel · Nueva incidencia"
        app.email_cc_var = MagicMock()
        app.email_cc_var.get.return_value = ui.NO_EMAIL_GROUP

        with patch.dict(ui.EMAIL_RECIPIENT_GROUPS, {
            "Entel · Nueva incidencia": ("nueva@example.com",),
            "Entel · Corte troncal": ("troncal@example.com",),
            "Entel · Escalamiento 2da línea TX Core": ("core@example.com",),
        }, clear=True), patch.object(ui.os, "startfile") as open_mail:
            app._open_creation_email()

        mailto = open_mail.call_args.args[0]
        self.assertTrue(mailto.startswith("mailto:nueva@example.com?"))
        self.assertNotIn("troncal@example.com", mailto)
        self.assertNotIn("core@example.com", mailto)

    def test_open_email_removes_cc_address_already_in_to(self) -> None:
        app = ui.HelixApp.__new__(ui.HelixApp)
        app.created_summary = f"{INCIDENT_ID}, {TITLE}"
        app.created_email_text = MagicMock()
        app.created_email_text.get.return_value = "Texto"
        app.email_to_var = MagicMock()
        app.email_to_var.get.return_value = "Tigo · Cortes"
        app.email_cc_var = MagicMock()
        app.email_cc_var.get.return_value = "Claro · Back office"

        with patch.dict(ui.EMAIL_RECIPIENT_GROUPS, {
            "Tigo · Cortes": ("uno@example.com",),
            "Claro · Back office": ("UNO@example.com", "dos@example.com"),
        }), patch.object(ui.os, "startfile") as open_mail:
            app._open_creation_email()

        mailto = open_mail.call_args.args[0]
        self.assertTrue(mailto.startswith("mailto:uno@example.com?"))
        self.assertIn("cc=dos%40example.com", mailto)
        self.assertNotIn("UNO%40example.com", mailto)

    def test_group_selection_counts_only_its_contacts(self) -> None:
        app = ui.HelixApp.__new__(ui.HelixApp)
        app.created_summary = f"{INCIDENT_ID}, {TITLE}"
        app.email_to_var = MagicMock()
        app.email_to_var.get.return_value = "GTD · Servicios bancarios"
        app.email_cc_var = MagicMock()
        app.email_cc_var.get.return_value = ui.NO_EMAIL_GROUP
        app.email_to_count_var = MagicMock()
        app.email_cc_count_var = MagicMock()
        app.open_email_button = MagicMock()

        with patch.dict(ui.EMAIL_RECIPIENT_GROUPS, {"GTD · Servicios bancarios": ("uno@example.com", "dos@example.com")}):
            app._on_email_group_changed()
        app.email_to_count_var.set.assert_called_with("2 correos")
        app.open_email_button.configure.assert_called_with(state="normal")

    def test_open_email_without_recipient_groups_keeps_subject_and_body(self) -> None:
        app = ui.HelixApp.__new__(ui.HelixApp)
        app.created_summary = f"{INCIDENT_ID}, {TITLE}"
        app.created_email_text = MagicMock()
        app.created_email_text.get.return_value = "Correo de prueba"
        app.email_to_var = MagicMock()
        app.email_to_var.get.return_value = ui.NO_EMAIL_GROUP
        app.email_cc_var = MagicMock()
        app.email_cc_var.get.return_value = ui.NO_EMAIL_GROUP

        with patch.object(ui.os, "startfile") as open_mail:
            app._open_creation_email()

        mailto = open_mail.call_args.args[0]
        self.assertTrue(mailto.startswith("mailto:?subject="))
        self.assertIn("body=Correo%20de%20prueba", mailto)
        self.assertNotIn("cc=", mailto)

    def test_open_email_with_only_cc_company(self) -> None:
        app = ui.HelixApp.__new__(ui.HelixApp)
        app.created_summary = f"{INCIDENT_ID}, {TITLE}"
        app.created_email_text = MagicMock()
        app.created_email_text.get.return_value = "Correo de prueba"
        app.email_to_var = MagicMock()
        app.email_to_var.get.return_value = ui.NO_EMAIL_GROUP
        app.email_cc_var = MagicMock()
        app.email_cc_var.get.return_value = "Tigo · Cortes"

        with patch.dict(ui.EMAIL_RECIPIENT_GROUPS, {"Tigo · Cortes": ("uno@example.com",)}), patch.object(
            ui.os, "startfile"
        ) as open_mail:
            app._open_creation_email()

        mailto = open_mail.call_args.args[0]
        self.assertTrue(mailto.startswith("mailto:?"))
        self.assertIn("cc=uno%40example.com", mailto)

    def test_all_requested_company_groups_are_available(self) -> None:
        to_groups, _cc_groups = email_catalog._load_email_groups(email_catalog.EXAMPLE_PATH)
        self.assertEqual(list(to_groups), [
            "Entel", "Tigo", "GTD", "GTD Telsur", "Claro", "Adexus", "Gigared",
            "Mundo Pacífico / Holdconet", "Cirion", "Internexa", "Silica", "Ufinet",
        ])
        self.assertEqual(list(to_groups["GTD"]), ["Servicios bancarios"])
        self.assertEqual(list(to_groups["GTD Telsur"]), ["General"])
        self.assertIn("GTD Telsur", email_catalog._recipient_groups(to_groups, _cc_groups))
        self.assertNotIn("Telsur · Supervisión de red", email_catalog._recipient_groups(to_groups, _cc_groups))
        self.assertEqual(list(to_groups["Mundo Pacífico / Holdconet"]), ["NOC y coordinación"])

    def test_email_catalog_supports_empty_groups_and_validates_addresses(self) -> None:
        path = MagicMock()
        path.read_text.return_value = json.dumps({
            "to": {"Tigo": {"Cortes": ["uno@example.com", "dos@example.com"]}, "Claro": []},
            "cc": {"Equipo Entel": ["entel@example.com"]},
            "pending_verification": {"Tigo": ["pending@example.com"]},
        })
        to_groups, cc_groups = email_catalog._load_email_groups(path)
        self.assertEqual(to_groups["Tigo"]["Cortes"], ("uno@example.com", "dos@example.com"))
        self.assertEqual(to_groups["Claro"]["General"], ())
        self.assertEqual(cc_groups["Equipo Entel"], ("entel@example.com",))
        self.assertNotIn("pending@example.com", to_groups["Tigo"]["Cortes"])

        path.read_text.return_value = json.dumps({
            "to": {"Tigo": ["dirección inválida"]}, "cc": {"Equipo Entel": []},
        })
        with self.assertRaises(RuntimeError):
            email_catalog._load_email_groups(path)

    def test_email_catalog_deduplicates_only_within_each_group(self) -> None:
        path = MagicMock()
        path.read_text.return_value = json.dumps({
            "to": {"Entel": {
                "Nueva incidencia": ["uno@example.com", "UNO@example.com", "dos@example.com"],
                "Corte troncal": ["UNO@example.com", "tres@example.com"],
            }},
            "cc": {"Equipo Entel": ["uno@example.com", "UNO@example.com"]},
        })

        to_groups, cc_groups = email_catalog._load_email_groups(path)

        self.assertEqual(to_groups["Entel"]["Nueva incidencia"], ("uno@example.com", "dos@example.com"))
        self.assertEqual(to_groups["Entel"]["Corte troncal"], ("UNO@example.com", "tres@example.com"))
        self.assertEqual(cc_groups["Equipo Entel"], ("uno@example.com",))

    def test_recipient_groups_keep_entel_lists_separate(self) -> None:
        contacts = email_catalog._recipient_groups(
            {"Entel": {"Primero": ("a@example.com",), "Segundo": ("A@example.com", "b@example.com")},
             "Tigo": {"NOC": ("tigo@example.com",)}},
            {"Equipo Entel": ("B@example.com", "c@example.com")},
        )
        self.assertEqual(contacts["Entel · Primero"], ("a@example.com",))
        self.assertEqual(contacts["Entel · Segundo"], ("A@example.com", "b@example.com"))
        self.assertEqual(contacts["Entel · Equipo Entel (CC)"], ("B@example.com", "c@example.com"))
        self.assertEqual(contacts["Tigo · NOC"], ("tigo@example.com",))

    def test_entel_groups_are_distinct_in_the_example_catalog(self) -> None:
        to_groups, cc_groups = email_catalog._load_email_groups(email_catalog.EXAMPLE_PATH)
        groups = email_catalog._recipient_groups(to_groups, cc_groups)
        self.assertIn("Entel · Nueva incidencia", groups)
        self.assertIn("Entel · Corte troncal", groups)
        self.assertIn("Entel · Escalamiento 2da línea TX Core", groups)
        self.assertNotIn("Entel · Escalamiento 2da línea TX Core (CC)", groups)

    def test_restore_clears_visible_state_without_touching_files_or_browser(self) -> None:
        app = ui.HelixApp.__new__(ui.HelixApp)
        app.busy = False
        app.closing = False
        app.loaded_records = [object()]
        app.resolution_by_incident = {INCIDENT_ID: "resolución"}
        app.batch_running = True
        app.batch_index = 1
        app.batch_results = [(INCIDENT_ID, "OK", "guardada")]
        app._last_incident_text = INCIDENT_ID
        app.created_summary = f"{INCIDENT_ID}, {TITLE}"
        for name in (
            "resolver_group_var", "resolver_group_combo", "resolver_user_var",
            "resolver_user_combo", "incident_var", "resolution_text", "close_tasks_var",
            "excel_status_var", "batch_progress", "batch_progress_var", "customer_var",
            "customer_combo", "new_incident_title_var", "created_summary_text",
            "created_email_text", "create_result_status_var", "copy_summary_button",
            "copy_email_button", "open_email_button", "log_text", "output_tabs",
            "modes_tabs", "status_var", "entry", "email_to_var", "email_to_combo",
            "email_cc_var", "email_cc_combo", "email_to_count_var", "email_cc_count_var",
        ):
            setattr(app, name, MagicMock())
        app._clear_results_table = MagicMock()
        app._on_mode_changed = MagicMock()
        app._on_email_group_changed = MagicMock()
        app._set_idle_buttons = MagicMock()

        with patch.object(ui.messagebox, "askyesno", return_value=True):
            app._restore_app()

        self.assertEqual(app.loaded_records, [])
        self.assertEqual(app.resolution_by_incident, {})
        self.assertEqual(app.batch_results, [])
        self.assertEqual(app.created_summary, "")
        app.created_summary_text.delete.assert_called_once_with("1.0", "end")
        app.created_email_text.delete.assert_called_once_with("1.0", "end")
        app.log_text.delete.assert_called_once_with("1.0", "end")
        app.modes_tabs.set.assert_called_once_with("Cierre manual")
        app._on_email_group_changed.assert_called_once_with()

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
