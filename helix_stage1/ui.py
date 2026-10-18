from __future__ import annotations

import logging
import json
import os
import queue
import re
import tkinter as tk
from datetime import datetime
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from typing import Literal
from urllib.parse import quote, urlencode
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import customtkinter as ctk

from .automation import AutomationWorker, WorkerCommand, WorkerEvent
from .config import LOG_DIR
from .customer_catalog import CUSTOMERS
from .email_catalog import EMAIL_RECIPIENT_GROUPS
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

BatchStatus = Literal["OK", "ERROR", "OMITIDA"]

APPEARANCE_LABELS = {"Sistema": "system", "Claro": "light", "Oscuro": "dark"}
NO_EMAIL_GROUP = "Sin seleccionar"


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


def _creation_texts(incident: str, title: str) -> tuple[str, str]:
    summary = f"{incident}, {_one_line(title)}"
    email = (
        "Estimados:\n\n"
        f'Informo que estamos presentando un evento entre "{title}".\n'
        f'Que se atenderá con el número de incidencia "{incident}".\n\n'
        "Favor comentar si tienen evento en su tramo.\n\n"
        "Saludos."
    )
    return summary, email


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
        self.batch_checkpoint_path: Path | None = None
        self.worker_failure_message = ""
        self._row_ids: dict[str, str] = {}
        self._last_incident_text = ""
        self._updating_incident = False
        self._close_requested_at = 0.0
        self.close_tasks_var = tk.BooleanVar(value=False)
        self.created_summary = ""

        root.title("Incidencias | BMC Helix")
        root.geometry("820x670")
        root.minsize(760, 620)
        root.protocol("WM_DELETE_WINDOW", self._on_close)

        self.font_title = ctk.CTkFont(family=UI_FONT, size=19, weight="bold")
        self.font_subtitle = ctk.CTkFont(family=UI_FONT, size=12)
        self.font_section = ctk.CTkFont(family=UI_FONT, size=13, weight="bold")
        self.font_body = ctk.CTkFont(family=UI_FONT, size=13)
        self.font_status = ctk.CTkFont(family=UI_FONT, size=13, weight="bold")
        self.font_mono = ctk.CTkFont(family="Consolas", size=12)

        root.grid_columnconfigure(0, weight=1)
        root.grid_rowconfigure(0, weight=1)
        container = ctk.CTkFrame(root, fg_color="transparent")
        container.grid(row=0, column=0, sticky="nsew", padx=12, pady=10)
        self.container = container
        container.grid_columnconfigure(0, weight=1)
        container.grid_rowconfigure(4, weight=1)

        self._build_header(container)
        self._build_common_card(container)
        self._build_modes_tabs(container)
        self._build_status_card(container)
        self._build_output_tabs(container)
        self.modes_tabs.configure(command=self._on_mode_changed)
        self._on_mode_changed()

        self._style_treeview()
        self.worker.start()
        self.root.after(100, self._poll_events)
        self.root.after(200, self._warn_unfinished_batches)
        self.entry.focus_set()

    # ------------------------------------------------------------------ UI
    def _card(self, parent: ctk.CTkBaseClass) -> ctk.CTkFrame:
        return ctk.CTkFrame(parent, corner_radius=8)

    def _build_header(self, container: ctk.CTkFrame) -> None:
        header = ctk.CTkFrame(container, fg_color="transparent")
        header.grid(row=0, column=0, sticky="ew")
        header.grid_columnconfigure(0, weight=1)
        self.header_title = ctk.CTkLabel(
            header, text="Cierre de incidencias Helix", font=self.font_title
        )
        self.header_title.grid(row=0, column=0, sticky="w")
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
        self.appearance_menu.grid(row=0, column=2, rowspan=2, sticky="e")
        self.restore_button = ctk.CTkButton(
            header,
            text="Restaurar",
            command=self._restore_app,
            width=90,
            height=28,
            font=self.font_subtitle,
            fg_color=("gray75", "gray30"),
            hover_color=("gray65", "gray40"),
            text_color=("gray10", "gray90"),
        )
        self.restore_button.grid(row=0, column=1, rowspan=2, sticky="e", padx=(8, 10))

    def _build_common_card(self, container: ctk.CTkFrame) -> None:
        card = self._card(container)
        self.common_card = card
        card.grid(row=1, column=0, sticky="ew", pady=(8, 6))
        card.grid_columnconfigure((0, 1), weight=1, uniform="common")
        ctk.CTkLabel(card, text="1. Asignación del resolutor", font=self.font_section).grid(
            row=0, column=0, columnspan=2, sticky="w", padx=12, pady=(8, 2)
        )
        ctk.CTkLabel(card, text="Grupo Resolutor", font=self.font_body).grid(
            row=1, column=0, sticky="w", padx=(12, 6)
        )
        ctk.CTkLabel(card, text="Usuario Resolutor", font=self.font_body).grid(
            row=1, column=1, sticky="w", padx=(6, 12)
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
            height=30,
        )
        self.resolver_group_combo.set("")
        self.resolver_group_combo.grid(row=2, column=0, sticky="ew", padx=(12, 6), pady=(2, 8))
        self.resolver_user_var = tk.StringVar()
        self.resolver_user_combo = ctk.CTkComboBox(
            card,
            variable=self.resolver_user_var,
            values=[""],
            state="disabled",
            font=self.font_body,
            dropdown_font=self.font_body,
            height=30,
        )
        self.resolver_user_combo.set("")
        self.resolver_user_combo.grid(row=2, column=1, sticky="ew", padx=(6, 12), pady=(2, 8))

    def _build_modes_tabs(self, container: ctk.CTkFrame) -> None:
        self.modes_tabs = ctk.CTkTabview(container, corner_radius=8, height=210)
        self.modes_tabs.grid(row=2, column=0, sticky="ew")
        manual_tab = self.modes_tabs.add("Cierre manual")
        excel_tab = self.modes_tabs.add("Cierre masivo (Excel)")
        create_tab = self.modes_tabs.add("Crear ticket")
        create_tab.grid_columnconfigure(0, weight=1)
        create_tab.grid_rowconfigure(0, weight=1)
        create_form = ctk.CTkScrollableFrame(create_tab, fg_color="transparent", corner_radius=0)
        create_form.grid(row=0, column=0, sticky="nsew")
        create_form.grid_columnconfigure(0, weight=1)
        create_form.grid_columnconfigure(1, weight=0)
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

        ctk.CTkLabel(create_form, text="Cliente", font=self.font_body).grid(
            row=0, column=0, sticky="w", padx=8
        )
        self.customer_var = tk.StringVar()
        self.customer_combo = ctk.CTkComboBox(
            create_form,
            variable=self.customer_var,
            values=list(CUSTOMERS),
            state="readonly" if CUSTOMERS else "disabled",
            font=self.font_body,
            dropdown_font=self.font_body,
            height=34,
        )
        self.customer_combo.set("")
        self.customer_combo.grid(row=1, column=0, columnspan=2, sticky="ew", padx=8, pady=(2, 5))
        ctk.CTkLabel(create_form, text="Título de la incidencia", font=self.font_body).grid(
            row=2, column=0, sticky="w", padx=8
        )
        self.new_incident_title_var = tk.StringVar()
        self.new_incident_title_entry = ctk.CTkEntry(
            create_form,
            textvariable=self.new_incident_title_var,
            font=self.font_body,
            height=34,
        )
        self.new_incident_title_entry.grid(row=3, column=0, columnspan=2, sticky="ew", padx=8, pady=(2, 5))
        self.create_button = ctk.CTkButton(
            create_form,
            text="Crear ticket",
            command=self._start_create_ticket,
            state="disabled",
            width=150,
            height=32,
            font=self.font_status,
        )
        self.create_button.grid(row=4, column=0, columnspan=2, sticky="w", padx=8, pady=(0, 4))
        self.create_result_status_var = tk.StringVar(value="")
        ctk.CTkLabel(
            create_form,
            textvariable=self.create_result_status_var,
            font=self.font_subtitle,
            text_color=("gray35", "gray65"),
            anchor="w",
        ).grid(row=5, column=0, columnspan=2, sticky="ew", padx=8, pady=(0, 4))

        ctk.CTkLabel(create_form, text="Asunto", font=self.font_section).grid(
            row=6, column=0, sticky="w", padx=8
        )
        self.copy_summary_button = ctk.CTkButton(
            create_form,
            text="Copiar",
            command=lambda: self._copy_creation_text("summary"),
            state="disabled",
            width=88,
            height=28,
            font=self.font_body,
        )
        self.copy_summary_button.grid(row=6, column=1, sticky="e", padx=8)
        self.created_summary_text = ctk.CTkTextbox(
            create_form, height=46, wrap="word", font=self.font_body, state="disabled"
        )
        self.created_summary_text.grid(
            row=7, column=0, columnspan=2, sticky="ew", padx=8, pady=(3, 6)
        )

        ctk.CTkLabel(create_form, text="Cuerpo del correo", font=self.font_section).grid(
            row=8, column=0, sticky="w", padx=8
        )
        self.copy_email_button = ctk.CTkButton(
            create_form,
            text="Copiar",
            command=lambda: self._copy_creation_text("email"),
            state="disabled",
            width=88,
            height=28,
            font=self.font_body,
        )
        self.copy_email_button.grid(row=8, column=1, sticky="e", padx=8)
        self.created_email_text = ctk.CTkTextbox(
            create_form, height=160, wrap="word", font=self.font_body
        )
        self.created_email_text.grid(row=9, column=0, sticky="nsew", padx=(8, 6), pady=(3, 6))

        recipients = ctk.CTkFrame(create_form, fg_color="transparent", width=310)
        recipients.grid(row=9, column=1, sticky="nsew", padx=(6, 8), pady=(3, 6))
        recipients.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(recipients, text="TO · Grupo", font=self.font_body, height=18).grid(
            row=0, column=0, sticky="w"
        )
        self.email_to_var = tk.StringVar(value=NO_EMAIL_GROUP)
        self.email_to_combo = ctk.CTkComboBox(
            recipients,
            variable=self.email_to_var,
            values=[NO_EMAIL_GROUP, *EMAIL_RECIPIENT_GROUPS],
            state="readonly",
            command=lambda _value: self._on_email_group_changed(),
            font=self.font_body,
            dropdown_font=self.font_body,
            width=300,
            height=28,
        )
        self.email_to_combo.set(NO_EMAIL_GROUP)
        self.email_to_combo.grid(row=1, column=0, sticky="ew", pady=(1, 2))
        self.email_to_count_var = tk.StringVar(value="Sin seleccionar")
        ctk.CTkLabel(
            recipients, textvariable=self.email_to_count_var, font=self.font_subtitle,
            text_color=("gray35", "gray65"), height=16,
        ).grid(row=2, column=0, sticky="w")

        ctk.CTkLabel(recipients, text="CC · Grupo", font=self.font_body, height=18).grid(
            row=3, column=0, sticky="w", pady=(2, 0)
        )
        self.email_cc_var = tk.StringVar(value=NO_EMAIL_GROUP)
        self.email_cc_combo = ctk.CTkComboBox(
            recipients,
            variable=self.email_cc_var,
            values=[NO_EMAIL_GROUP, *EMAIL_RECIPIENT_GROUPS],
            state="readonly",
            command=lambda _value: self._on_email_group_changed(),
            font=self.font_body,
            dropdown_font=self.font_body,
            width=300,
            height=28,
        )
        self.email_cc_combo.set(NO_EMAIL_GROUP)
        self.email_cc_combo.grid(row=4, column=0, sticky="ew", pady=(1, 2))
        self.email_cc_count_var = tk.StringVar(value="Sin seleccionar")
        ctk.CTkLabel(
            recipients, textvariable=self.email_cc_count_var, font=self.font_subtitle,
            text_color=("gray35", "gray65"), height=16,
        ).grid(row=5, column=0, sticky="w")
        self.open_email_button = ctk.CTkButton(
            recipients,
            text="Crear correo",
            command=self._open_creation_email,
            state="disabled",
            width=132,
            height=28,
            font=self.font_body,
        )
        self.open_email_button.grid(row=6, column=0, sticky="e", pady=(3, 0))

    def _build_status_card(self, container: ctk.CTkFrame) -> None:
        card = self._card(container)
        card.grid(row=3, column=0, sticky="ew", pady=6)
        card.grid_columnconfigure(0, weight=1)
        self.status_var = tk.StringVar(value="Iniciando la interfaz…")
        ctk.CTkLabel(
            card,
            textvariable=self.status_var,
            font=self.font_status,
            wraplength=720,
            justify="left",
            anchor="w",
        ).grid(row=0, column=0, sticky="ew", padx=12, pady=7)

    def _build_output_tabs(self, container: ctk.CTkFrame) -> None:
        self.output_tabs = ctk.CTkTabview(container, corner_radius=8)
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
            height=4,
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
        self.results_table.tag_configure("OMITIDA", foreground="#d5a64a" if dark else "#946200")
        self.results_table.tag_configure("ERROR", foreground="#ff6b6b" if dark else "#c62828")
        self.results_table.tag_configure("PENDIENTE", foreground="#9aa0a6" if dark else "#5f6368")
        self.results_table.tag_configure("PROCESANDO", foreground="#4aa3ff" if dark else "#1565c0")

    def _on_appearance_changed(self, label: str) -> None:
        ctk.set_appearance_mode(APPEARANCE_LABELS.get(label, "system"))
        self.root.after(50, self._style_treeview)

    def _on_mode_changed(self) -> None:
        if self.modes_tabs.get() == "Crear ticket":
            self.common_card.grid_remove()
            self.output_tabs.grid_remove()
            self.container.grid_rowconfigure(2, weight=1)
            self.container.grid_rowconfigure(4, weight=0)
            self.modes_tabs.configure(height=455)
            self.modes_tabs.grid_configure(sticky="nsew")
            self.header_title.configure(text="Crear incidencia Helix")
        else:
            self.common_card.grid()
            self.output_tabs.grid()
            self.container.grid_rowconfigure(2, weight=0)
            self.container.grid_rowconfigure(4, weight=1)
            self.modes_tabs.configure(height=210)
            self.modes_tabs.grid_configure(sticky="ew")
            self.header_title.configure(text="Cierre de incidencias Helix")

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

    # ------------------------------------------------------------- creation
    def _start_create_ticket(self) -> None:
        if self.busy or self.closing:
            return
        customer = self.customer_var.get().strip()
        if customer not in CUSTOMERS:
            self.status_var.set("Selecciona un Cliente de la lista antes de crear el ticket.")
            return
        title = self.new_incident_title_var.get().strip()
        if not title:
            self.status_var.set("Escribe el título de la nueva incidencia.")
            self.new_incident_title_entry.focus_set()
            return
        if len(title) > 100:
            self.status_var.set("El título no puede superar 100 caracteres.")
            self.new_incident_title_entry.focus_set()
            return
        self.email_to_var.set(NO_EMAIL_GROUP)
        self.email_to_combo.set(NO_EMAIL_GROUP)
        self.email_cc_var.set(NO_EMAIL_GROUP)
        self.email_cc_combo.set(NO_EMAIL_GROUP)
        self._on_email_group_changed()
        self.create_result_status_var.set("Preparando una nueva incidencia…")
        self.busy = True
        self._set_busy_buttons()
        self.status_var.set("Abriendo Nueva incidencia y seleccionando Cliente en Remedy…")
        self._append_log(f"Abriendo Nueva incidencia para Cliente {customer}.")
        self.commands.put(WorkerCommand(action="create", customer=customer, title=title))

    def _show_creation_output(self, incident: str, title: str) -> str:
        summary, email = _creation_texts(incident, title)
        self.created_summary = summary
        self.created_summary_text.configure(state="normal")
        self.created_summary_text.delete("1.0", "end")
        self.created_summary_text.insert("1.0", summary)
        self.created_summary_text.configure(state="disabled")
        self.created_email_text.delete("1.0", "end")
        self.created_email_text.insert("1.0", email)
        self.create_result_status_var.set("Proceso finalizado. Guardar enviado.")
        self.copy_summary_button.configure(state="normal")
        self.copy_email_button.configure(state="normal")
        self._on_email_group_changed()
        return summary

    def _copy_creation_text(self, kind: Literal["summary", "email"]) -> None:
        value = (
            self.created_summary
            if kind == "summary"
            else self.created_email_text.get("1.0", "end-1c")
        )
        if not value.strip():
            return
        self.root.clipboard_clear()
        self.root.clipboard_append(value)
        self.root.update_idletasks()
        self.status_var.set("Incidencia copiada." if kind == "summary" else "Correo copiado.")

    def _open_creation_email(self) -> None:
        body = self.created_email_text.get("1.0", "end-1c")
        if not self.created_summary or not body.strip():
            return
        to_group = self.email_to_var.get()
        cc_group = self.email_cc_var.get()
        if to_group not in ("", NO_EMAIL_GROUP) and to_group not in EMAIL_RECIPIENT_GROUPS:
            self.status_var.set("El grupo TO seleccionado no existe.")
            return
        if cc_group not in ("", NO_EMAIL_GROUP) and cc_group not in EMAIL_RECIPIENT_GROUPS:
            self.status_var.set("El grupo CC seleccionado no existe.")
            return
        subject = self.created_summary
        to_addresses = EMAIL_RECIPIENT_GROUPS.get(to_group, ())
        to_keys = {address.casefold() for address in to_addresses}
        cc_addresses = tuple(
            address for address in EMAIL_RECIPIENT_GROUPS.get(cc_group, ())
            if address.casefold() not in to_keys
        )
        params = {"subject": subject, "body": body}
        if cc_addresses:
            params["cc"] = ",".join(cc_addresses)
        mailto = "mailto:" + quote(",".join(to_addresses), safe="@,") + "?" + urlencode(
            params, quote_via=quote
        )
        try:
            os.startfile(mailto)
        except OSError as error:
            messagebox.showerror(
                "Abrir correo",
                f"No se pudo abrir la aplicación de correo predeterminada: {error}",
            )

    def _on_email_group_changed(self) -> None:
        to_group = self.email_to_var.get()
        cc_group = self.email_cc_var.get()
        self.email_to_count_var.set(
            f"{len(EMAIL_RECIPIENT_GROUPS[to_group])} correos"
            if to_group in EMAIL_RECIPIENT_GROUPS else "Sin seleccionar"
        )
        self.email_cc_count_var.set(
            f"{len(EMAIL_RECIPIENT_GROUPS[cc_group])} correos"
            if cc_group in EMAIL_RECIPIENT_GROUPS else "Sin seleccionar"
        )
        self.open_email_button.configure(
            state="normal" if self.created_summary else "disabled"
        )

    def _restore_app(self) -> None:
        if self.busy or self.closing:
            return
        if not messagebox.askyesno(
            "Restaurar aplicación",
            "Se limpiarán los datos y resultados visibles de esta sesión. "
            "Los archivos de bitácora y la sesión de Chrome se conservarán. ¿Continuar?",
        ):
            return

        self.resolver_group_var.set("")
        self.resolver_group_combo.set("")
        self.resolver_user_var.set("")
        self.resolver_user_combo.configure(values=[""], state="disabled")
        self.resolver_user_combo.set("")
        self.incident_var.set("")
        self._last_incident_text = ""
        self.resolution_text.delete("1.0", "end")
        self.close_tasks_var.set(False)
        self.loaded_records = []
        self.resolution_by_incident.clear()
        self.batch_running = False
        self.batch_index = 0
        self.batch_results = []
        self.excel_status_var.set("Sin Excel cargado.")
        self.batch_progress.set(0)
        self.batch_progress_var.set("Sin proceso activo.")
        self._clear_results_table()

        self.customer_var.set("")
        self.customer_combo.set("")
        self.new_incident_title_var.set("")
        self.created_summary = ""
        self.created_summary_text.configure(state="normal")
        self.created_summary_text.delete("1.0", "end")
        self.created_summary_text.configure(state="disabled")
        self.created_email_text.delete("1.0", "end")
        self.create_result_status_var.set("")
        self.email_to_var.set(NO_EMAIL_GROUP)
        self.email_to_combo.set(NO_EMAIL_GROUP)
        self.email_cc_var.set(NO_EMAIL_GROUP)
        self.email_cc_combo.set(NO_EMAIL_GROUP)
        self._on_email_group_changed()
        self.copy_summary_button.configure(state="disabled")
        self.copy_email_button.configure(state="disabled")

        self.log_text.configure(state="normal")
        self.log_text.delete("1.0", "end")
        self.log_text.configure(state="disabled")
        self.output_tabs.set("Resultados")
        self.modes_tabs.set("Cierre manual")
        self._on_mode_changed()
        self.status_var.set("Lista para una nueva operación.")
        self._set_idle_buttons()
        self.entry.focus_set()

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
        self.batch_checkpoint_path = LOG_DIR / (
            f"cierre_lote_{datetime.now().strftime('%Y%m%d_%H%M%S_%f')}.json"
        )
        try:
            self._save_batch_checkpoint("running")
        except OSError as error:
            self.batch_running = False
            self.batch_checkpoint_path = None
            self.status_var.set("No se pudo iniciar el lote: falta el registro de recuperación.")
            messagebox.showerror("Cierre masivo", f"No se pudo registrar el lote: {error}")
            return
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
        self.restore_button.configure(state=state)
        self.search_button.configure(state=state)
        self.create_button.configure(state=state)
        self.new_incident_title_entry.configure(state=state)
        self.customer_combo.configure(
            state="readonly" if state == "normal" and CUSTOMERS else "disabled"
        )
        self.load_excel_button.configure(state=state)
        self.close_tasks_check.configure(state=state)
        self.close_tasks_check_manual.configure(state=state)
        self.process_excel_button.configure(
            state="normal" if state == "normal" and self.loaded_records else "disabled"
        )

    def _set_busy_buttons(self) -> None:
        self.restore_button.configure(state="disabled")
        self.search_button.configure(state="disabled")
        self.create_button.configure(state="disabled")
        self.new_incident_title_entry.configure(state="disabled")
        self.customer_combo.configure(state="disabled")
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
            elif status == "OMITIDA":
                self._append_log(f"{incident}: omitida, ya estaba cerrada.")
                messagebox.showinfo("Incidencia ya cerrada", f"{incident} ya estaba cerrada; no se modificó.")
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
        try:
            self._save_batch_checkpoint("running")
        except OSError as error:
            self.batch_running = False
            self.status_var.set("Lote detenido: no se pudo actualizar el registro de recuperación.")
            self._append_log(f"Registro de recuperación no disponible: {error}")
            try:
                report_path = self._write_batch_report()
                self._append_log(f"Reporte parcial generado: {report_path}")
            except OSError as report_error:
                self._append_log(f"No se pudo guardar el reporte parcial: {report_error}")
            self._finish_current_operation()
            messagebox.showerror("Cierre masivo detenido", f"No se pudo registrar el avance: {error}")
            return

        if status == "ERROR":
            self.batch_running = False
            report_path = self._write_batch_report()
            try:
                self._save_batch_checkpoint("stopped")
            except OSError as error:
                self._append_log(f"No se pudo cerrar el registro de recuperación: {error}")
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

        if status == "OMITIDA":
            self._append_log(f"{finished_incident}: omitida, ya estaba cerrada.")
        else:
            self._append_log(f"{finished_incident}: cierre OK.")
        self.busy = False
        self._submit_next_batch_incident(
            self.resolver_group_var.get(), self.resolver_user_var.get()
        )

    def _finish_batch(self) -> None:
        self.batch_running = False
        report_path = self._write_batch_report()
        try:
            self._save_batch_checkpoint("finished")
        except OSError as error:
            self._append_log(f"No se pudo marcar el registro de recuperación como finalizado: {error}")
        ok_count = sum(1 for _i, status, _m in self.batch_results if status == "OK")
        skipped_count = sum(1 for _i, status, _m in self.batch_results if status == "OMITIDA")
        error_count = len(self.batch_results) - ok_count - skipped_count
        self.status_var.set(
            f"Excel finalizado: {ok_count} OK, {skipped_count} omitidas, "
            f"{error_count} con error. Revisa el reporte."
        )
        self.batch_progress.set(1)
        self.batch_progress_var.set(
            f"Finalizado: {ok_count} OK, {skipped_count} omitidas, {error_count} errores"
        )
        self._append_log(
            f"Cierre masivo finalizado: {ok_count} OK, {skipped_count} omitidas, "
            f"{error_count} errores."
        )
        self._append_log(f"Reporte generado: {report_path}")
        self._finish_current_operation()
        messagebox.showinfo(
            "Trabajos finalizados",
            f"Cierre masivo finalizado.\nOK: {ok_count}\n"
            f"Omitidas: {skipped_count}\nErrores: {error_count}",
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
            skipped_count = sum(1 for _i, status, _m in self.batch_results if status == "OMITIDA")
            error_count = len(self.batch_results) - ok_count - skipped_count
            report.write("Resumen:\n")
            report.write(f"Total: {len(self.batch_results)}\n")
            report.write(f"OK: {ok_count}\n")
            report.write(f"Omitidas: {skipped_count}\n")
            report.write(f"Errores: {error_count}\n\n")
            report.write("Detalle:\n")
            report.write("numero de incidencia      Estado    Detalle  |  Resolución\n")
            for incident, status, message in self.batch_results:
                detail = self._shorten_detail(message)
                resolution = _one_line(self.resolution_by_incident.get(incident, ""))
                report.write(f"{incident:<25}{status:<10}{detail}  |  {resolution}\n")
        return report_path

    def _save_batch_checkpoint(self, state: str) -> None:
        path = getattr(self, "batch_checkpoint_path", None)
        if path is None:
            return
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        current = (
            self.loaded_records[self.batch_index].incident
            if self.batch_index < len(self.loaded_records) else None
        )
        data = {
            "state": state,
            "current_incident": current,
            "pending_incidents": [record.incident for record in self.loaded_records[self.batch_index:]],
            "results": [
                {"incident": incident, "status": status, "detail": message}
                for incident, status, message in self.batch_results
            ],
        }
        temporary = path.with_suffix(".tmp")
        with temporary.open("w", encoding="utf-8") as output:
            json.dump(data, output, ensure_ascii=False, indent=2)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)

    def _warn_unfinished_batches(self) -> None:
        if self.closing:
            return
        unfinished = []
        for path in LOG_DIR.glob("cierre_lote_*.json"):
            try:
                with path.open(encoding="utf-8") as source:
                    data = json.load(source)
                if data.get("state") == "running":
                    unfinished.append((path, data.get("current_incident")))
            except (OSError, ValueError, AttributeError):
                self._append_log(f"No se pudo leer el registro de recuperación: {path.name}")
        if unfinished:
            path, incident = max(unfinished, key=lambda entry: entry[0].name)
            self.status_var.set("Hay un cierre masivo interrumpido; revisa Remedy antes de reintentar.")
            self._append_log(f"Lote interrumpido: {path}; incidencia a verificar: {incident or 'ninguna'}.")
            messagebox.showwarning(
                "Cierre masivo interrumpido",
                f"Revisa {incident or 'la ultima incidencia'} en Remedy antes de reintentar.\n"
                f"Registro de recuperacion: {path}",
            )

    def _handle_worker_stopped(self) -> None:
        message = self.worker_failure_message or "El trabajador de automatización se detuvo."
        incident = None
        report_path = None
        if self.batch_running:
            incident = self.loaded_records[self.batch_index].incident
            self.batch_results.append((incident, "ERROR", "Proceso interrumpido; verificar estado en Remedy."))
            self._set_result_row(
                incident, "ERROR", self.resolution_by_incident.get(incident, ""),
                "Estado incierto; verificar en Remedy.",
            )
            self.batch_running = False
            self.batch_progress_var.set(f"Interrumpido: {self.batch_index + 1}/{len(self.loaded_records)}")
            try:
                self._save_batch_checkpoint("interrupted")
                report_path = self._write_batch_report()
            except OSError as error:
                self._append_log(f"No se pudo guardar el reporte parcial: {error}")
        elif self.busy:
            incident = self.incident_var.get().strip() or None
        self.busy = True
        self._set_busy_buttons()
        self.status_var.set("Automatización detenida; revisa Remedy antes de reiniciar la app.")
        self._append_log(f"{message} Incidencia a verificar: {incident or 'sin identificar'}.")
        if report_path:
            self._append_log(f"Reporte parcial generado: {report_path}")
        messagebox.showerror(
            "Automatización detenida",
            f"{message}\n\nIncidencia a verificar: {incident or 'sin identificar'}.\n"
            "No se reintentará automáticamente. Revisa Remedy antes de reiniciar la app.",
        )

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
            self.worker_failure_message = event.message
            self.status_var.set(event.message)
            self._append_log(f"Advertencia: {event.message}")
        elif event.kind == "create_save_sent":
            if not event.incident or not event.title:
                self.status_var.set("Guardar pulsado, pero faltan datos del resumen.")
                self._append_log("Guardar pulsado sin ID o título en el resultado del worker.")
                self._finish_current_operation()
                return
            summary = self._show_creation_output(event.incident, event.title)
            self.new_incident_title_var.set("")
            self.status_var.set(f"Guardar enviado: {event.incident}")
            self._append_log(f"Guardar enviado: {summary}")
            try:
                LOG_DIR.mkdir(parents=True, exist_ok=True)
                with (LOG_DIR / "Incidencias creadas.txt").open("a", encoding="utf-8") as report:
                    report.write(f"{summary}\n")
            except OSError as error:
                self._append_log(f"No se pudo actualizar la bitácora local: {error}")
                messagebox.showwarning(
                    "Bitácora", f"Guardar enviado, pero no se pudo guardar la bitácora: {error}"
                )
            messagebox.showinfo(
                "Proceso finalizado",
                f"Se pulsó Guardar para {event.incident}.\n"
                "El resumen y el correo están listos para copiar en Crear ticket.",
            )
        elif event.kind == "create_home_ready":
            self._finish_current_operation()
        elif event.kind == "create_unverified":
            self.status_var.set("No se pudo determinar si Guardar fue enviado.")
            self.create_result_status_var.set("Resultado de Guardar incierto; revisa Remedy.")
            self._append_log(event.message)
            self._finish_current_operation()
            messagebox.showwarning("Creación no confirmada", event.message)
        elif event.kind == "create_home_warning":
            self.status_var.set("Guardar enviado; revisa la pantalla de Remedy antes de continuar.")
            self._append_log(event.message)
            self._finish_current_operation()
        elif event.kind == "create_error":
            self.status_var.set("No se pudo preparar Nueva incidencia.")
            self.create_result_status_var.set("No se completó el nuevo ticket. Resultado anterior conservado.")
            self._append_log(f"Error al preparar Nueva incidencia: {event.message}")
            self._finish_current_operation()
            messagebox.showerror("Crear ticket", event.message)
        elif event.kind == "done":
            self.status_var.set(f"Incidencia verificada: {event.incident}")
            self._handle_incident_finished(event.incident, "OK", event.message)
        elif event.kind == "searched":
            self.status_var.set(f"{event.incident}: Guardar enviado. Revisa la confirmación en Remedy.")
            self._handle_incident_finished(event.incident, "OK", event.message)
        elif event.kind == "skipped_closed":
            self.status_var.set(f"{event.incident}: ya cerrada, omitida sin cambios.")
            self._handle_incident_finished(event.incident, "OMITIDA", event.message)
        elif event.kind == "error":
            self.status_var.set(f"No se pudo completar: {event.incident}.")
            self._handle_incident_finished(event.incident, "ERROR", event.message)
        elif event.kind == "closed":
            if self.closing:
                self.root.destroy()
            else:
                self._handle_worker_stopped()

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
