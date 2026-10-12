from __future__ import annotations

import logging
import queue
import re
import tkinter as tk
from datetime import datetime
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from typing import Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import customtkinter as ctk

from .automation import AutomationWorker, WorkerCommand, WorkerEvent
from .config import LOG_DIR
from .excel_loader import (
    INCIDENT_PATTERN,
    IncidentRecord,
    LoadResult,
    load_incident_file,
)
from .resolver_catalog import RESOLVER_USERS

PARTIAL_INCIDENT_PATTERN = re.compile(r"(?:I(?:N(?:C\d{0,12})?)?)?", re.IGNORECASE)
REPORT_HEADER = "numero de incidencia      Estado"
REPORT_TITLE_FORMAT = "%m/%d/%Y   %H:%M hrs"
SHOW_STEP_LOGS = True  # False = el registro solo muestra inicio/fin de cada incidencia
UI_FONT = "Segoe UI"

BatchStatus = Literal["OK", "ERROR"]

APPEARANCE_LABELS = {"Sistema": "system", "Claro": "light", "Oscuro": "dark"}


def _create_logger() -> logging.Logger:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("helix_stage1")
    logger.setLevel(logging.INFO)
    if not logger.handlers:
        handler = logging.FileHandler(LOG_DIR / "automation.log", encoding="utf-8")
        handler.setFormatter(
            logging.Formatter("%(asctime)s %(levelname)s %(threadName)s %(message)s")
        )
        logger.addHandler(handler)
    return logger


def _report_now() -> datetime:
    try:
        return datetime.now(ZoneInfo("America/Santiago"))
    except ZoneInfoNotFoundError:
        return datetime.now()


def _one_line(text: str, limit: int | None = None) -> str:
    flat = " ".join(text.split())
    if limit and len(flat) > limit:
        return flat[: limit - 1] + "…"
    return flat


class HelixApp:
    def __init__(self, root: ctk.CTk) -> None:
        self.root = root
        self.commands: queue.Queue[WorkerCommand] = queue.Queue()
        self.events: queue.Queue[WorkerEvent] = queue.Queue()
        self.worker = AutomationWorker(self.commands, self.events, _create_logger())
        self.closing = False
        self.busy = True
        self.loaded_records: list[IncidentRecord] = []
        self.resolution_by_incident: dict[str, str] = {}
        self.batch_running = False
        self.batch_index = 0
        self.batch_results: list[tuple[str, BatchStatus, str]] = []
        self._row_ids: dict[str, str] = {}
        self._last_incident_text = ""
        self._updating_incident = False
        self._close_requested_at = 0.0
        self.close_tasks_var = tk.BooleanVar(value=False)

        root.title("Cierre de incidencias | BMC Helix")
        root.geometry("980x860")
        root.minsize(900, 760)
        root.protocol("WM_DELETE_WINDOW", self._on_close)

        self.font_title = ctk.CTkFont(family=UI_FONT, size=22, weight="bold")
        self.font_subtitle = ctk.CTkFont(family=UI_FONT, size=12)
        self.font_section = ctk.CTkFont(family=UI_FONT, size=13, weight="bold")
        self.font_body = ctk.CTkFont(family=UI_FONT, size=13)
        self.font_status = ctk.CTkFont(family=UI_FONT, size=13, weight="bold")
        self.font_mono = ctk.CTkFont(family="Consolas", size=12)

        root.grid_columnconfigure(0, weight=1)
        root.grid_rowconfigure(0, weight=1)
        container = ctk.CTkFrame(root, fg_color="transparent")
        container.grid(row=0, column=0, sticky="nsew", padx=18, pady=14)
        container.grid_columnconfigure(0, weight=1)
        container.grid_rowconfigure(4, weight=1)

        self._build_header(container)
        self._build_common_card(container)
        self._build_modes_tabs(container)
        self._build_status_card(container)
        self._build_output_tabs(container)

        self._style_treeview()
        self.worker.start()
        self.root.after(100, self._poll_events)
        self.entry.focus_set()

    # ------------------------------------------------------------------ UI
    def _card(self, parent: ctk.CTkBaseClass) -> ctk.CTkFrame:
        return ctk.CTkFrame(parent, corner_radius=12)

    def _build_header(self, container: ctk.CTkFrame) -> None:
        header = ctk.CTkFrame(container, fg_color="transparent")
        header.grid(row=0, column=0, sticky="ew")
        header.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(header, text="Cierre de incidencias Helix", font=self.font_title).grid(
            row=0, column=0, sticky="w"
        )
        ctk.CTkLabel(
            header,
            text="Automatización BMC Helix ITSM · Redes Chile",
            font=self.font_subtitle,
            text_color=("gray35", "gray65"),
        ).grid(row=1, column=0, sticky="w")
        self.appearance_menu = ctk.CTkSegmentedButton(
            header,
            values=list(APPEARANCE_LABELS),
            command=self._on_appearance_changed,
            font=self.font_subtitle,
        )
        self.appearance_menu.set("Sistema")
        self.appearance_menu.grid(row=0, column=1, rowspan=2, sticky="e")

    def _build_common_card(self, container: ctk.CTkFrame) -> None:
        card = self._card(container)
        card.grid(row=1, column=0, sticky="ew", pady=(14, 10))
        card.grid_columnconfigure((0, 1), weight=1, uniform="common")
        ctk.CTkLabel(card, text="1. Asignación del resolutor", font=self.font_section).grid(
            row=0, column=0, columnspan=2, sticky="w", padx=16, pady=(12, 6)
        )
        ctk.CTkLabel(card, text="Grupo Resolutor", font=self.font_body).grid(
            row=1, column=0, sticky="w", padx=(16, 8)
        )
        ctk.CTkLabel(card, text="Usuario Resolutor", font=self.font_body).grid(
            row=1, column=1, sticky="w", padx=(8, 16)
        )
        self.resolver_group_var = tk.StringVar()
        self.resolver_group_combo = ctk.CTkComboBox(
            card,
            variable=self.resolver_group_var,
            values=list(RESOLVER_USERS),
            state="readonly",
            command=self._on_group_selected,
            font=self.font_body,
            dropdown_font=self.font_body,
            height=34,
        )
        self.resolver_group_combo.set("")
        self.resolver_group_combo.grid(row=2, column=0, sticky="ew", padx=(16, 8), pady=(2, 14))
        self.resolver_user_var = tk.StringVar()
        self.resolver_user_combo = ctk.CTkComboBox(
            card,
            variable=self.resolver_user_var,
            values=[""],
            state="disabled",
            font=self.font_body,
            dropdown_font=self.font_body,
            height=34,
        )
        self.resolver_user_combo.set("")
        self.resolver_user_combo.grid(row=2, column=1, sticky="ew", padx=(8, 16), pady=(2, 14))

    def _build_modes_tabs(self, container: ctk.CTkFrame) -> None:
        self.modes_tabs = ctk.CTkTabview(container, corner_radius=12, height=240)
        self.modes_tabs.grid(row=2, column=0, sticky="ew")
        manual_tab = self.modes_tabs.add("Cierre manual")
        excel_tab = self.modes_tabs.add("Cierre masivo (Excel)")
        for tab in (manual_tab, excel_tab):
            tab.grid_columnconfigure(0, weight=1)

        # --- Manual
        manual_tab.grid_columnconfigure(1, weight=2)
        ctk.CTkLabel(manual_tab, text="Número de incidencia", font=self.font_body).grid(
            row=0, column=0, sticky="w", padx=8
        )
        ctk.CTkLabel(manual_tab, text="Resolución", font=self.font_body).grid(
            row=0, column=1, sticky="w", padx=8
        )
        self.incident_var = tk.StringVar()
        self.incident_var.trace_add("write", self._on_incident_text_changed)
        self.entry = ctk.CTkEntry(
            manual_tab,
            textvariable=self.incident_var,
            placeholder_text="INC000000000000",
            font=self.font_mono,
            height=34,
        )
        self.entry.grid(row=1, column=0, sticky="new", padx=8, pady=(2, 8))
        self.entry.bind("<Return>", self._on_enter)
        self.resolution_text = ctk.CTkTextbox(
            manual_tab, height=84, wrap="word", font=self.font_body, border_width=1
        )
        self.resolution_text.grid(row=1, column=1, rowspan=2, sticky="nsew", padx=8, pady=(2, 8))
        self.search_button = ctk.CTkButton(
            manual_tab,
            text="Cerrar incidencia",
            command=self._submit,
            state="disabled",
            height=36,
            font=self.font_status,
        )
        self.search_button.grid(row=2, column=0, sticky="ew", padx=8, pady=(0, 8))
        self.close_tasks_check_manual = ctk.CTkCheckBox(
            manual_tab,
            text="Cerrar tareas asociadas antes del cierre",
            variable=self.close_tasks_var,
            font=self.font_body,
        )
        self.close_tasks_check_manual.grid(
            row=3, column=0, columnspan=2, sticky="w", padx=8, pady=(0, 4)
        )

        # --- Excel
        ctk.CTkLabel(
            excel_tab,
            text=(
                "El Excel debe tener las columnas «Incidencias» y «Resolución». "
                "Cada incidencia se cierra con su propia resolución."
            ),
            font=self.font_body,
            wraplength=820,
            justify="left",
        ).grid(row=0, column=0, columnspan=2, sticky="w", padx=8, pady=(0, 8))
        excel_tab.grid_columnconfigure((0, 1), weight=1, uniform="excel")
        self.load_excel_button = ctk.CTkButton(
            excel_tab,
            text="Cargar Excel",
            command=self._load_excel,
            state="disabled",
            height=36,
            font=self.font_status,
            fg_color=("gray70", "gray30"),
            hover_color=("gray60", "gray40"),
            text_color=("gray10", "gray90"),
        )
        self.load_excel_button.grid(row=1, column=0, sticky="ew", padx=(8, 4), pady=(0, 8))
        self.process_excel_button = ctk.CTkButton(
            excel_tab,
            text="Iniciar cierre masivo",
            command=self._start_excel_batch,
            state="disabled",
            height=36,
            font=self.font_status,
        )
        self.process_excel_button.grid(row=1, column=1, sticky="ew", padx=(4, 8), pady=(0, 8))
        self.close_tasks_check = ctk.CTkCheckBox(
            excel_tab,
            text="Cerrar tareas asociadas antes del cierre",
            variable=self.close_tasks_var,
            font=self.font_body,
        )
        self.close_tasks_check.grid(
            row=2, column=0, columnspan=2, sticky="w", padx=8, pady=(0, 4)
        )
        self.excel_status_var = tk.StringVar(value="Sin Excel cargado.")
        ctk.CTkLabel(excel_tab, textvariable=self.excel_status_var, font=self.font_body).grid(
            row=3, column=0, columnspan=2, sticky="w", padx=8
        )
        self.batch_progress = ctk.CTkProgressBar(excel_tab, height=10)
        self.batch_progress.set(0)
        self.batch_progress.grid(row=4, column=0, columnspan=2, sticky="ew", padx=8, pady=(6, 2))
        self.batch_progress_var = tk.StringVar(value="Sin proceso activo.")
        ctk.CTkLabel(
            excel_tab,
            textvariable=self.batch_progress_var,
            font=self.font_subtitle,
            text_color=("gray35", "gray65"),
        ).grid(row=5, column=0, columnspan=2, sticky="w", padx=8)

    def _build_status_card(self, container: ctk.CTkFrame) -> None:
        card = self._card(container)
        card.grid(row=3, column=0, sticky="ew", pady=10)
        card.grid_columnconfigure(0, weight=1)
        self.status_var = tk.StringVar(value="Iniciando la interfaz…")
        ctk.CTkLabel(
            card,
            textvariable=self.status_var,
            font=self.font_status,
            wraplength=880,
            justify="left",
            anchor="w",
        ).grid(row=0, column=0, sticky="ew", padx=16, pady=10)

    def _build_output_tabs(self, container: ctk.CTkFrame) -> None:
        self.output_tabs = ctk.CTkTabview(container, corner_radius=12)
        self.output_tabs.grid(row=4, column=0, sticky="nsew")
        results_tab = self.output_tabs.add("Resultados")
        log_tab = self.output_tabs.add("Registro")

        results_tab.grid_columnconfigure(0, weight=1)
        results_tab.grid_rowconfigure(0, weight=1)
        self.results_table = ttk.Treeview(
            results_tab,
            columns=("index", "incident", "status", "resolution", "detail"),
            show="headings",
            style="Helix.Treeview",
            height=6,
        )
        for column, title, width, anchor, stretch in (
            ("index", "#", 44, "center", False),
            ("incident", "Incidencia", 150, "w", False),
            ("status", "Estado", 100, "center", False),
            ("resolution", "Resolución", 280, "w", True),
            ("detail", "Detalle", 280, "w", True),
        ):
            self.results_table.heading(column, text=title, anchor="w" if anchor == "w" else "center")
            self.results_table.column(column, width=width, anchor=anchor, stretch=stretch)
        tree_scroll = ctk.CTkScrollbar(results_tab, command=self.results_table.yview)
        self.results_table.configure(yscrollcommand=tree_scroll.set)
        self.results_table.grid(row=0, column=0, sticky="nsew", padx=(4, 0), pady=4)
        tree_scroll.grid(row=0, column=1, sticky="ns", pady=4)

        log_tab.grid_columnconfigure(0, weight=1)
        log_tab.grid_rowconfigure(0, weight=1)
        self.log_text = ctk.CTkTextbox(log_tab, wrap="word", font=self.font_mono, state="disabled")
        self.log_text.grid(row=0, column=0, sticky="nsew", padx=4, pady=4)

    def _style_treeview(self) -> None:
        dark = ctk.get_appearance_mode() == "Dark"
        bg = "#2b2b2b" if dark else "#ffffff"
        fg = "#e6e6e6" if dark else "#1a1a1a"
        head_bg = "#3a3a3a" if dark else "#e9edf2"
        selected = "#1f6aa5"
        try:
            scale = float(ctk.ScalingTracker.get_window_scaling(self.root))
        except Exception:
            scale = 1.0
        style = ttk.Style(self.root)
        style.theme_use("clam")
        style.configure(
            "Helix.Treeview",
            background=bg,
            fieldbackground=bg,
            foreground=fg,
            borderwidth=0,
            rowheight=int(28 * scale),
            font=(UI_FONT, 10),
        )
        style.map(
            "Helix.Treeview",
            background=[("selected", selected)],
            foreground=[("selected", "#ffffff")],
        )
        style.configure(
            "Helix.Treeview.Heading",
            background=head_bg,
            foreground=fg,
            relief="flat",
            borderwidth=0,
            font=(UI_FONT, 10, "bold"),
            padding=(6, 6),
        )
        style.map("Helix.Treeview.Heading", background=[("active", head_bg)])
        self.results_table.tag_configure("OK", foreground="#4caf50" if dark else "#1b7f3a")
        self.results_table.tag_configure("ERROR", foreground="#ff6b6b" if dark else "#c62828")
        self.results_table.tag_configure("PENDIENTE", foreground="#9aa0a6" if dark else "#5f6368")
        self.results_table.tag_configure("PROCESANDO", foreground="#4aa3ff" if dark else "#1565c0")

    def _on_appearance_changed(self, label: str) -> None:
        ctk.set_appearance_mode(APPEARANCE_LABELS.get(label, "system"))
        self.root.after(50, self._style_treeview)

    # ------------------------------------------------------------ handlers
    def _on_incident_text_changed(self, *_args: object) -> None:
        if self._updating_incident:
            return
        value = self.incident_var.get()
        cleaned = value.strip().upper()
        if PARTIAL_INCIDENT_PATTERN.fullmatch(cleaned) is None:
            cleaned = self._last_incident_text
        self._last_incident_text = cleaned
        if cleaned != value:
            self._updating_incident = True
            try:
                self.incident_var.set(cleaned)
            finally:
                self._updating_incident = False

    def _on_enter(self, _event: tk.Event) -> str:  # type: ignore[type-arg]
        self._submit()
        return "break"

    def _on_group_selected(self, group: str) -> None:
        users = list(RESOLVER_USERS.get(group, ()))
        self.resolver_user_var.set("")
        self.resolver_user_combo.configure(values=users or [""])
        self.resolver_user_combo.set("")
        self.resolver_user_combo.configure(state="readonly" if users else "disabled")

    def _validate_resolver(self) -> tuple[str, str] | None:
        group = self.resolver_group_var.get()
        user = self.resolver_user_var.get()
        if group not in RESOLVER_USERS:
            self.status_var.set("Selecciona un Grupo Resolutor.")
            self._append_log("Falta seleccionar el Grupo Resolutor.")
            self.resolver_group_combo.focus_set()
            return None
        if user not in RESOLVER_USERS[group]:
            self.status_var.set("Selecciona un usuario válido para el grupo elegido.")
            self._append_log("Falta seleccionar un Usuario Resolutor válido para el grupo.")
            self.resolver_user_combo.focus_set()
            return None
        return group, user

    # --------------------------------------------------------------- Excel
    def _load_excel(self) -> None:
        if self.busy or self.closing:
            return
        path = filedialog.askopenfilename(
            title="Seleccionar archivo Excel",
            filetypes=(
                ("Planillas", "*.xlsx *.xlsm *.ods"),
                ("Archivos Excel", "*.xlsx *.xlsm"),
                ("OpenDocument", "*.ods"),
                ("Todos los archivos", "*.*"),
            ),
        )
        if not path:
            return
        try:
            result = load_incident_file(Path(path))
        except Exception as error:
            self.loaded_records = []
            self.excel_status_var.set("No se pudo cargar el Excel.")
            self.batch_progress.set(0)
            self.batch_progress_var.set("Sin proceso activo.")
            self._set_idle_buttons()
            self._append_log(f"Error cargando Excel: {error}")
            messagebox.showerror("Error cargando Excel", str(error))
            return
        self._apply_load_result(Path(path), result)

    def _apply_load_result(self, path: Path, result: LoadResult) -> None:
        self.loaded_records = result.records
        report_path = self._write_load_report(path, result.report_rows)
        skipped = len(result.report_rows) - len(result.records)
        self.excel_status_var.set(
            f"{len(result.records)} incidencias listas"
            + (f" · {skipped} omitidas (ver registro)" if skipped else "")
        )
        self.batch_progress.set(0)
        self.batch_progress_var.set(f"Pendientes: {len(result.records)}")
        self._clear_results_table()
        for record in result.records:
            self._set_result_row(record.incident, "PENDIENTE", record.resolution, "")
        self.output_tabs.set("Resultados")
        self._set_idle_buttons()
        self._append_log(f"Excel cargado: {len(result.records)} incidencias con resolución propia.")
        self._append_log(f"Resumen TXT generado: {report_path}")
        if result.duplicates:
            self._append_log(f"Duplicadas omitidas: {', '.join(result.duplicates[:5])}")
        if result.invalid_values:
            preview = ", ".join(result.invalid_values[:5])
            suffix = "…" if len(result.invalid_values) > 5 else ""
            self._append_log(f"Valores inválidos omitidos: {preview}{suffix}")
        if result.missing_resolution:
            preview = ", ".join(result.missing_resolution[:5])
            suffix = "…" if len(result.missing_resolution) > 5 else ""
            self._append_log(f"Sin resolución (omitidas): {preview}{suffix}")

    def _write_load_report(self, excel_path: Path, rows: list[tuple[str, str, str]]) -> Path:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        safe_name = re.sub(r"[^A-Za-z0-9_-]", "_", excel_path.stem)[:40] or "excel"
        report_path = LOG_DIR / f"resumen_carga_{timestamp}_{safe_name}.txt"
        with report_path.open("w", encoding="utf-8") as report:
            report.write(REPORT_HEADER + "    Motivo\n")
            for incident, status, reason in rows:
                report.write(f"{incident:<25}{status:<8}{reason}\n")
        return report_path

    # -------------------------------------------------------------- manual
    def _submit(self) -> None:
        if self.busy or self.closing:
            return
        incident = self.incident_var.get().strip().upper()
        if INCIDENT_PATTERN.fullmatch(incident) is None:
            self.status_var.set("Formato inválido: usa INC seguido de 12 dígitos.")
            self._append_log("Formato inválido. Ejemplo: INC123456789012")
            self.entry.focus_set()
            return
        resolver = self._validate_resolver()
        if resolver is None:
            return
        resolution = self.resolution_text.get("1.0", "end-1c").strip()
        if not resolution:
            self.status_var.set("Escribe el texto de Resolución.")
            self._append_log("Falta ingresar el texto de Resolución.")
            self.resolution_text.focus_set()
            return
        self._clear_results_table()
        self._append_log(f"{incident}: iniciado.")
        self._queue_incident(incident, resolver[0], resolver[1], resolution)

    # --------------------------------------------------------------- batch
    def _start_excel_batch(self) -> None:
        if self.busy or self.closing:
            return
        if not self.loaded_records:
            self.status_var.set("Carga un Excel con incidencias y resoluciones válidas.")
            self._append_log("No hay incidencias cargadas para procesar.")
            return
        resolver = self._validate_resolver()
        if resolver is None:
            return
        group, user = resolver
        if not messagebox.askyesno(
            "Confirmar cierre masivo",
            "Se ejecutará el cierre masivo con estos datos:\n\n"
            f"Incidencias: {len(self.loaded_records)}\n"
            f"Grupo: {group}\n"
            f"Usuario: {user}\n"
            "Resolución: la indicada en el Excel para cada incidencia.\n\n"
            "¿Continuar?",
        ):
            self.status_var.set("Cierre masivo cancelado por el usuario.")
            self._append_log("Cierre masivo cancelado.")
            return
        self.batch_running = True
        self.batch_index = 0
        self.batch_results = []
        self._clear_results_table()
        for record in self.loaded_records:
            self._set_result_row(record.incident, "PENDIENTE", record.resolution, "")
        self.batch_progress.set(0)
        self.batch_progress_var.set(f"Procesando 0/{len(self.loaded_records)}")
        self._append_log(f"Cierre masivo iniciado: {len(self.loaded_records)} incidencias.")
        self._submit_next_batch_incident(group, user)

    def _submit_next_batch_incident(self, resolver_group: str, resolver_user: str) -> None:
        if self.batch_index >= len(self.loaded_records):
            self._finish_batch()
            return
        record = self.loaded_records[self.batch_index]
        self.batch_progress_var.set(f"Procesando {self.batch_index + 1}/{len(self.loaded_records)}")
        self._append_log(f"{record.incident}: iniciado.")
        self._set_result_row(record.incident, "PROCESANDO", record.resolution, "")
        self._queue_incident(record.incident, resolver_group, resolver_user, record.resolution)

    def _queue_incident(
        self,
        incident: str,
        resolver_group: str,
        resolver_user: str,
        resolution: str,
    ) -> None:
        self.busy = True
        self.resolution_by_incident[incident] = resolution
        self._set_busy_buttons()
        self.status_var.set(
            f"Automatización en proceso: {incident}. No manipular Remedy hasta finalizar."
        )
        self.commands.put(
            WorkerCommand(
                action="search",
                incident=incident,
                resolver_group=resolver_group,
                resolver_user=resolver_user,
                resolution=resolution,
                close_tasks=self.close_tasks_var.get(),
            )
        )

    # ------------------------------------------------------ buttons / table
    def _set_idle_buttons(self) -> None:
        state = "disabled" if self.closing else "normal"
        self.search_button.configure(state=state)
        self.load_excel_button.configure(state=state)
        self.close_tasks_check.configure(state=state)
        self.close_tasks_check_manual.configure(state=state)
        self.process_excel_button.configure(
            state="normal" if state == "normal" and self.loaded_records else "disabled"
        )

    def _set_busy_buttons(self) -> None:
        self.search_button.configure(state="disabled")
        self.load_excel_button.configure(state="disabled")
        self.close_tasks_check.configure(state="disabled")
        self.close_tasks_check_manual.configure(state="disabled")
        self.process_excel_button.configure(state="disabled")

    def _finish_current_operation(self) -> None:
        self.busy = False
        self._set_idle_buttons()

    def _clear_results_table(self) -> None:
        for item in self.results_table.get_children():
            self.results_table.delete(item)
        self._row_ids.clear()

    def _set_result_row(self, incident: str, status: str, resolution: str, detail: str) -> None:
        """Inserta la fila de la incidencia o la actualiza si ya existe."""
        iid = self._row_ids.get(incident)
        if iid is None:
            number = len(self._row_ids) + 1
            values = (number, incident, status, _one_line(resolution, 90), detail)
            self._row_ids[incident] = self.results_table.insert(
                "", "end", values=values, tags=(status,)
            )
            return
        number = self.results_table.set(iid, "index")
        self.results_table.item(
            iid,
            values=(number, incident, status, _one_line(resolution, 90), detail),
            tags=(status,),
        )
        self.results_table.see(iid)

    @staticmethod
    def _shorten_detail(message: str) -> str:
        normalized = " ".join(message.split())
        replacements = (
            (
                "Se pulsó Guardar, pero no se detectó confirmación; verifica Remedy antes de reintentar.",
                "Guardada; confirmación visible no detectada.",
            ),
            ("Remedy confirmó el guardado.", "Guardada."),
        )
        for source, target in replacements:
            if source in normalized:
                return target
        if "Helix redirigió a autenticación" in normalized:
            return "Sesión expirada o redirigida a login."
        if "campo editable" in normalized and "ID" in normalized:
            return "No se abrió el campo de búsqueda de incidencia."
        if "Redes Chile" in normalized:
            return "No se encontró o abrió la pestaña Redes Chile."
        if "Guardar está deshabilitado" in normalized:
            return "No se pudo guardar: botón Guardar deshabilitado."
        if "diálogo/modal inesperado" in normalized:
            return normalized[:180]
        return normalized[:180] or "Sin detalle."

    def _handle_incident_finished(
        self, incident: str | None, status: BatchStatus, message: str
    ) -> None:
        if not self.batch_running:
            self._finish_current_operation()
            if incident:
                self._clear_results_table()
                self._set_result_row(
                    incident,
                    status,
                    self.resolution_by_incident.get(incident, ""),
                    self._shorten_detail(message),
                )
            if status == "OK":
                self._append_log(f"{incident}: cierre OK.")
                messagebox.showinfo("Trabajo finalizado", f"Cierre manual finalizado para {incident}.")
            else:
                self._append_log(f"{incident or 'Incidencia'}: ERROR - {self._shorten_detail(message)}")
                messagebox.showerror(
                    "Error en cierre manual",
                    f"No se pudo completar {incident or 'la incidencia'}.\n\n{message}",
                )
            return

        finished_incident = incident or self.loaded_records[self.batch_index].incident
        self.batch_results.append((finished_incident, status, message))
        self._set_result_row(
            finished_incident,
            status,
            self.resolution_by_incident.get(finished_incident, ""),
            self._shorten_detail(message),
        )
        self.batch_index += 1
        self.batch_progress.set(self.batch_index / max(1, len(self.loaded_records)))

        if status == "ERROR":
            self.batch_running = False
            report_path = self._write_batch_report()
            self.status_var.set(
                f"Excel detenido en {finished_incident}. Revisa el error antes de continuar."
            )
            self.batch_progress_var.set(f"Detenido: {self.batch_index}/{len(self.loaded_records)}")
            self._append_log(f"{finished_incident}: ERROR - {self._shorten_detail(message)}")
            self._append_log("Cierre masivo detenido por seguridad.")
            self._append_log(f"Reporte parcial generado: {report_path}")
            self._finish_current_operation()
            messagebox.showerror(
                "Cierre masivo detenido",
                f"No se pudo completar {finished_incident}.\n\n{message}",
            )
            return

        self._append_log(f"{finished_incident}: cierre OK.")
        self.busy = False
        self._submit_next_batch_incident(
            self.resolver_group_var.get(), self.resolver_user_var.get()
        )

    def _finish_batch(self) -> None:
        self.batch_running = False
        report_path = self._write_batch_report()
        ok_count = sum(1 for _i, status, _m in self.batch_results if status == "OK")
        error_count = len(self.batch_results) - ok_count
        self.status_var.set(
            f"Excel finalizado: {ok_count} OK, {error_count} con error. Revisa el reporte."
        )
        self.batch_progress.set(1)
        self.batch_progress_var.set(f"Finalizado: {ok_count} OK, {error_count} errores")
        self._append_log(f"Cierre masivo finalizado: {ok_count} OK, {error_count} errores.")
        self._append_log(f"Reporte generado: {report_path}")
        self._finish_current_operation()
        messagebox.showinfo(
            "Trabajos finalizados",
            f"Cierre masivo finalizado.\nOK: {ok_count}\nErrores: {error_count}",
        )

    def _write_batch_report(self) -> Path:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        now = _report_now()
        timestamp = now.strftime("%Y%m%d_%H%M%S")
        report_path = LOG_DIR / f"resultado_cierre_excel_{timestamp}.txt"
        with report_path.open("w", encoding="utf-8") as report:
            report.write(f"Cierres automatizados {now.strftime(REPORT_TITLE_FORMAT)}\n\n")
            report.write(f"Grupo Resolutor: {self.resolver_group_var.get()}\n")
            report.write(f"Usuario Resolutor: {self.resolver_user_var.get()}\n")
            report.write("Resolución: individual por incidencia (columna 'Resolución' del Excel)\n\n")
            ok_count = sum(1 for _i, status, _m in self.batch_results if status == "OK")
            error_count = len(self.batch_results) - ok_count
            report.write("Resumen:\n")
            report.write(f"Total: {len(self.batch_results)}\n")
            report.write(f"OK: {ok_count}\n")
            report.write(f"Errores: {error_count}\n\n")
            report.write("Detalle:\n")
            report.write("numero de incidencia      Estado    Detalle  |  Resolución\n")
            for incident, status, message in self.batch_results:
                detail = self._shorten_detail(message)
                resolution = _one_line(self.resolution_by_incident.get(incident, ""))
                report.write(f"{incident:<25}{status:<10}{detail}  |  {resolution}\n")
        return report_path

    # -------------------------------------------------------------- events
    def _poll_events(self) -> None:
        while True:
            try:
                event = self.events.get_nowait()
            except queue.Empty:
                break
            self._handle_event(event)
        if not self.closing:
            self.root.after(100, self._poll_events)

    def _handle_event(self, event: WorkerEvent) -> None:
        if event.kind == "log":
            if SHOW_STEP_LOGS:
                prefix = f"{event.incident}: " if event.incident else ""
                self._append_log(f"   · {prefix}{event.message}")
            return
        if event.kind == "ready":
            self.status_var.set("Lista. El navegador se abrirá al enviar una incidencia.")
            self._append_log("Aplicación lista.")
            self._finish_current_operation()
        elif event.kind == "warning":
            self.status_var.set(event.message)
            self._append_log(f"Advertencia: {event.message}")
            self._finish_current_operation()
        elif event.kind == "done":
            self.status_var.set(f"Incidencia verificada: {event.incident}")
            self._handle_incident_finished(event.incident, "OK", event.message)
        elif event.kind == "searched":
            self.status_var.set(f"{event.incident}: Guardar enviado. Revisa la confirmación en Remedy.")
            self._handle_incident_finished(event.incident, "OK", event.message)
        elif event.kind == "error":
            self.status_var.set(f"No se pudo completar: {event.incident}.")
            self._handle_incident_finished(event.incident, "ERROR", event.message)
        elif event.kind == "closed":
            self.root.destroy()

    def _append_log(self, message: str) -> None:
        stamp = datetime.now().strftime("%H:%M:%S")
        self.log_text.configure(state="normal")
        self.log_text.insert("end", f"[{stamp}] {message}\n")
        self.log_text.see("end")
        self.log_text.configure(state="disabled")

    # --------------------------------------------------------------- close
    def _on_close(self) -> None:
        if self.closing:
            return
        self.closing = True
        self.busy = True
        self.batch_running = False
        self._set_busy_buttons()
        self.status_var.set("Cerrando el navegador…")
        self.commands.put(WorkerCommand(action="stop"))
        self._close_ticks = 0
        self.root.after(100, self._poll_close)

    def _poll_close(self) -> None:
        while True:
            try:
                event = self.events.get_nowait()
            except queue.Empty:
                break
            if event.kind == "closed":
                self.root.destroy()
                return
        self._close_ticks += 1
        # Si el hilo ya murió (o tarda demasiado) no dejamos la ventana colgada.
        if not self.worker.is_alive() or self._close_ticks > 300:
            self.root.destroy()
            return
        self.root.after(100, self._poll_close)


def run_app() -> None:
    ctk.set_appearance_mode("system")
    ctk.set_default_color_theme("blue")
    root = ctk.CTk()
    HelixApp(root)
    root.mainloop()
