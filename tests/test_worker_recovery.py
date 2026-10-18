import json
import unittest
from pathlib import Path
from unittest.mock import MagicMock, mock_open, patch

from helix_stage1 import automation, ui
from helix_stage1.excel_loader import IncidentRecord


INCIDENT = "INC000000000001"
NEXT_INCIDENT = "INC000000000002"


class WorkerRecoveryTests(unittest.TestCase):
    def test_checkpoint_tracks_current_incident_without_resolutions(self) -> None:
        app = ui.HelixApp.__new__(ui.HelixApp)
        app.batch_checkpoint_path = Path("cierre_lote_test.json")
        app.loaded_records = [
            IncidentRecord(INCIDENT, "Resolucion privada"),
            IncidentRecord(NEXT_INCIDENT, "Otra resolucion privada"),
        ]
        app.batch_index = 0
        app.batch_results = []
        output = mock_open()
        with patch.object(Path, "mkdir"), patch.object(Path, "open", output), patch.object(
            ui.os, "fsync"
        ), patch.object(ui.os, "replace"):
            app._save_batch_checkpoint("running")
            data = json.loads("".join(call.args[0] for call in output().write.call_args_list))
            self.assertEqual(data["current_incident"], INCIDENT)
            self.assertEqual(data["pending_incidents"], [INCIDENT, NEXT_INCIDENT])
            self.assertNotIn("Resolucion privada", json.dumps(data))

            output.reset_mock()
            app.batch_results.append((INCIDENT, "OK", "Guardada"))
            app.batch_index = 1
            app._save_batch_checkpoint("running")
            data = json.loads("".join(call.args[0] for call in output().write.call_args_list))
            self.assertEqual(data["current_incident"], NEXT_INCIDENT)
            self.assertEqual(data["results"][0]["status"], "OK")

    def test_unexpected_worker_stop_keeps_window_and_writes_partial_report(self) -> None:
        app = ui.HelixApp.__new__(ui.HelixApp)
        app.closing = False
        app.busy = True
        app.worker_failure_message = "Fallo del navegador"
        app.batch_running = True
        app.batch_index = 0
        app.loaded_records = [IncidentRecord(INCIDENT, "Resolucion")]
        app.batch_results = []
        app.resolution_by_incident = {INCIDENT: "Resolucion"}
        app.root = MagicMock()
        app.status_var = MagicMock()
        app.batch_progress_var = MagicMock()
        app._set_result_row = MagicMock()
        app._set_busy_buttons = MagicMock()
        app._append_log = MagicMock()
        app._save_batch_checkpoint = MagicMock()
        app._write_batch_report = MagicMock(return_value=Path("reporte.txt"))

        with patch.object(ui.messagebox, "showerror") as showerror:
            app._handle_event(automation.WorkerEvent("closed", "Navegador cerrado."))

        app.root.destroy.assert_not_called()
        self.assertFalse(app.batch_running)
        self.assertEqual(app.batch_results[0][0], INCIDENT)
        self.assertEqual(app.batch_results[0][1], "ERROR")
        self.assertIn("verificar", app.batch_results[0][2])
        app._save_batch_checkpoint.assert_called_once_with("interrupted")
        app._write_batch_report.assert_called_once()
        showerror.assert_called_once()

    def test_startup_warns_about_unfinished_batch_without_retrying(self) -> None:
        app = ui.HelixApp.__new__(ui.HelixApp)
        app.closing = False
        app.status_var = MagicMock()
        app._append_log = MagicMock()
        path = Path("cierre_lote_test.json")
        source = mock_open(read_data=json.dumps({"state": "running", "current_incident": INCIDENT}))
        with patch.object(Path, "glob", return_value=[path]), patch.object(
            Path, "open", source
        ), patch.object(ui.messagebox, "showwarning") as warning:
            app._warn_unfinished_batches()

        self.assertIn(INCIDENT, warning.call_args.args[1])
        self.assertIn("interrumpido", app.status_var.set.call_args.args[0])


if __name__ == "__main__":
    unittest.main()
