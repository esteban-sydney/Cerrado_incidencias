import logging
import queue
import unittest
from pathlib import Path
from unittest.mock import MagicMock, mock_open, patch

from helix_stage1 import automation, selectors, ui
from helix_stage1.excel_loader import IncidentRecord


INCIDENT = "INC000000000001"
NEXT_INCIDENT = "INC000000000002"


class ClosedIncidentTests(unittest.TestCase):
    def setUp(self) -> None:
        self.events: queue.Queue[automation.WorkerEvent] = queue.Queue()
        logger = logging.getLogger("test.closed_incident")
        logger.setLevel(logging.CRITICAL)
        self.worker = automation.AutomationWorker(
            queue.Queue(), self.events, logger
        )

    def _prepare_closed_search(self) -> MagicMock:
        page = MagicMock()
        self.worker.console_page = page
        for method in (
            "_validate_selectors", "_ensure_browser", "_navigate_to_console", "_settle",
            "_ensure_session", "_check_unexpected_ui", "_close_pending_tasks",
            "_return_home_after_save",
        ):
            setattr(self.worker, method, MagicMock())
        search_field = MagicMock()
        search_field.input_value.return_value = INCIDENT
        self.worker._wait_for_single_editable_match = MagicMock(return_value=search_field)
        self.worker._read_opened_incident_status = MagicMock(return_value="Cerrado")
        return page

    def _run_search(self) -> list[automation.WorkerEvent]:
        with patch.object(automation, "HELIX_URL", "https://example.invalid"), patch.object(
            selectors, "OPEN_INCIDENT_SEARCH", MagicMock()
        ) as menu_spec, patch.object(selectors, "SEARCH_FIELD", MagicMock()):
            menu_spec.build.return_value.count.return_value = 1
            self.worker._process_incident(INCIDENT, "Grupo", "Usuario", "Resolución", True)
        events = []
        while not self.events.empty():
            events.append(self.events.get_nowait())
        return events

    def test_closed_incident_returns_home_without_modifying_it(self) -> None:
        page = self._prepare_closed_search()

        events = self._run_search()

        self.worker._read_opened_incident_status.assert_called_once_with(page, INCIDENT)
        self.worker._close_pending_tasks.assert_not_called()
        self.worker._return_home_after_save.assert_called_once_with(page, INCIDENT)
        self.assertEqual([event.kind for event in events if event.kind != "log"], ["skipped_closed"])

    def test_failed_return_home_stops_instead_of_skipping(self) -> None:
        self._prepare_closed_search()
        self.worker._return_home_after_save.side_effect = automation.AutomationError(
            "Inicio no disponible"
        )

        events = self._run_search()

        self.assertNotIn("skipped_closed", [event.kind for event in events])
        self.assertEqual([event.kind for event in events if event.kind != "log"], ["error"])

    def test_open_incident_continues_to_tasks(self) -> None:
        self._prepare_closed_search()
        self.worker._read_opened_incident_status.return_value = "Asignado"
        self.worker._close_pending_tasks.side_effect = automation.AutomationError(
            "Prueba detenida después de validar tareas"
        )

        events = self._run_search()

        self.worker._close_pending_tasks.assert_called_once()
        self.worker._return_home_after_save.assert_not_called()
        self.assertNotIn("skipped_closed", [event.kind for event in events])

    def test_status_read_verifies_opened_id_before_status(self) -> None:
        page = MagicMock()
        id_field = MagicMock()
        id_field.input_value.return_value = NEXT_INCIDENT
        self.worker._wait_for_visible_locator_matches = MagicMock(return_value=[id_field])
        self.worker._incident_status_widget = MagicMock()

        with self.assertRaises(automation.VerificationError):
            self.worker._read_opened_incident_status(page, INCIDENT)

        self.worker._incident_status_widget.assert_not_called()

    def test_status_read_accepts_closed_readonly_field(self) -> None:
        page = MagicMock()
        id_field = MagicMock()
        id_field.input_value.return_value = INCIDENT
        self.worker._wait_for_visible_locator_matches = MagicMock(return_value=[id_field])
        status_field = MagicMock()
        status_field.count.return_value = 1
        status_field.input_value.return_value = "Cerrado"
        status_widget = MagicMock()
        status_widget.locator.return_value = status_field
        self.worker._incident_status_widget = MagicMock(return_value=status_widget)

        self.assertEqual(self.worker._read_opened_incident_status(page, INCIDENT), "Cerrado")

    def test_skipped_excel_row_continues_with_next_incident(self) -> None:
        app = ui.HelixApp.__new__(ui.HelixApp)
        app.batch_running = True
        app.batch_index = 0
        app.loaded_records = [
            IncidentRecord(INCIDENT, "Primera resolución"),
            IncidentRecord(NEXT_INCIDENT, "Segunda resolución"),
        ]
        app.batch_results = []
        app.resolution_by_incident = {INCIDENT: "Primera resolución"}
        app.status_var = MagicMock()
        app.batch_progress = MagicMock()
        app.resolver_group_var = MagicMock()
        app.resolver_user_var = MagicMock()
        app._set_result_row = MagicMock()
        app._append_log = MagicMock()
        app._submit_next_batch_incident = MagicMock()

        app._handle_event(automation.WorkerEvent("skipped_closed", "Ya cerrada.", INCIDENT))

        self.assertEqual(app.batch_results, [(INCIDENT, "OMITIDA", "Ya cerrada.")])
        self.assertEqual(app.batch_index, 1)
        app._submit_next_batch_incident.assert_called_once()

    def test_batch_report_counts_skipped_separately(self) -> None:
        app = ui.HelixApp.__new__(ui.HelixApp)
        app.batch_results = [
            (INCIDENT, "OMITIDA", "Incidencia ya cerrada; no se modificó."),
            (NEXT_INCIDENT, "OK", "Guardada."),
        ]
        app.resolution_by_incident = {
            INCIDENT: "Primera resolución",
            NEXT_INCIDENT: "Segunda resolución",
        }
        app.resolver_group_var = MagicMock()
        app.resolver_user_var = MagicMock()

        file_mock = mock_open()
        with patch.object(Path, "mkdir"), patch.object(Path, "open", file_mock):
            app._write_batch_report()
        report = "".join(call.args[0] for call in file_mock().write.call_args_list)

        self.assertIn("OK: 1", report)
        self.assertIn("Omitidas: 1", report)
        self.assertIn("Errores: 0", report)

    def test_batch_summary_shows_skipped_without_error(self) -> None:
        app = ui.HelixApp.__new__(ui.HelixApp)
        app.batch_running = True
        app.batch_results = [
            (INCIDENT, "OMITIDA", "Ya cerrada."),
            (NEXT_INCIDENT, "OK", "Guardada."),
        ]
        app.status_var = MagicMock()
        app.batch_progress = MagicMock()
        app.batch_progress_var = MagicMock()
        app._append_log = MagicMock()
        app._write_batch_report = MagicMock(return_value=Path("reporte.txt"))
        app._finish_current_operation = MagicMock()

        with patch.object(ui.messagebox, "showinfo") as showinfo:
            app._finish_batch()

        self.assertFalse(app.batch_running)
        self.assertIn("1 omitidas", app.status_var.set.call_args.args[0])
        self.assertIn("Errores: 0", showinfo.call_args.args[1])


if __name__ == "__main__":
    unittest.main()
