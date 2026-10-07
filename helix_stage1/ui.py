from __future__ import annotations

import logging
import queue
import re
import tkinter as tk
from tkinter import ttk

from .automation import AutomationWorker, WorkerCommand, WorkerEvent
from .config import LOG_DIR
from .resolver_catalog import RESOLVER_USERS


INCIDENT_PATTERN = re.compile(r"INC\d{12}", re.IGNORECASE)
PARTIAL_INCIDENT_PATTERN = re.compile(r"(?:I(?:N(?:C\d{0,12})?)?)?", re.IGNORECASE)


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


class HelixApp:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.commands: queue.Queue[WorkerCommand] = queue.Queue()
        self.events: queue.Queue[WorkerEvent] = queue.Queue()
        self.worker = AutomationWorker(self.commands, self.events, _create_logger())
        self.closing = False
        self.busy = True

        root.title("Consulta de incidencias | BMC Helix")
        root.geometry("820x560")
        root.minsize(680, 440)
        root.protocol("WM_DELETE_WINDOW", self._on_close)

        container = ttk.Frame(root, padding=18)
        container.pack(fill="both", expand=True)
        container.columnconfigure(0, weight=1, uniform="assignment")
        container.columnconfigure(1, weight=1, uniform="assignment")
        container.rowconfigure(7, weight=1)

        ttk.Label(container, text="Número de incidencia").grid(
            row=0, column=0, sticky="w", pady=(0, 6)
        )
        self.incident_var = tk.StringVar()
        validator = root.register(self._validate_partial_incident)
        self.entry = ttk.Entry(
            container,
            textvariable=self.incident_var,
            validate="key",
            validatecommand=(validator, "%P"),
            width=32,
        )
        self.entry.grid(row=1, column=0, sticky="ew")
        self.entry.bind("<Return>", self._on_enter)

        self.search_button = ttk.Button(
            container,
            text="Buscar incidencia",
            command=self._submit,
            state="disabled",
        )
        self.search_button.grid(row=1, column=1, padx=(10, 0))

        ttk.Label(container, text="Grupo Resolutor").grid(
            row=2, column=0, sticky="w", pady=(14, 5), padx=(0, 8)
        )
        ttk.Label(container, text="Usuario Resolutor").grid(
            row=2, column=1, sticky="w", pady=(14, 5), padx=(8, 0)
        )
        self.resolver_group_var = tk.StringVar()
        self.resolver_group_combo = ttk.Combobox(
            container,
            textvariable=self.resolver_group_var,
            values=tuple(RESOLVER_USERS),
            state="readonly",
        )
        self.resolver_group_combo.grid(row=3, column=0, sticky="ew", padx=(0, 8))
        self.resolver_group_combo.bind("<<ComboboxSelected>>", self._on_group_selected)

        self.resolver_user_var = tk.StringVar()
        self.resolver_user_combo = ttk.Combobox(
            container,
            textvariable=self.resolver_user_var,
            state="disabled",
        )
        self.resolver_user_combo.grid(row=3, column=1, sticky="ew", padx=(8, 0))

        ttk.Label(container, text="Resolución").grid(
            row=4, column=0, columnspan=2, sticky="w", pady=(14, 5)
        )
        self.resolution_text = tk.Text(container, wrap="word", height=4)
        self.resolution_text.grid(row=5, column=0, columnspan=2, sticky="ew")

        self.status_var = tk.StringVar(value="Iniciando la interfaz…")
        ttk.Label(container, textvariable=self.status_var).grid(
            row=6, column=0, columnspan=2, sticky="w", pady=(12, 8)
        )

        log_frame = ttk.Frame(container)
        log_frame.grid(row=7, column=0, columnspan=2, sticky="nsew")
        log_frame.rowconfigure(0, weight=1)
        log_frame.columnconfigure(0, weight=1)
        self.log_text = tk.Text(log_frame, wrap="word", state="disabled", height=12)
        scrollbar = ttk.Scrollbar(log_frame, orient="vertical", command=self.log_text.yview)
        self.log_text.configure(yscrollcommand=scrollbar.set)
        self.log_text.grid(row=0, column=0, sticky="nsew")
        scrollbar.grid(row=0, column=1, sticky="ns")

        self.worker.start()
        self.root.after(100, self._poll_events)
        self.entry.focus_set()

    @staticmethod
    def _validate_partial_incident(candidate: str) -> bool:
        return PARTIAL_INCIDENT_PATTERN.fullmatch(candidate) is not None

    def _on_enter(self, _event: tk.Event[tk.Misc]) -> str:
        self._submit()
        return "break"

    def _on_group_selected(self, _event: tk.Event[tk.Misc]) -> None:
        group = self.resolver_group_var.get()
        users = RESOLVER_USERS.get(group, ())
        self.resolver_user_var.set("")
        self.resolver_user_combo.configure(values=users)
        self.resolver_user_combo.configure(state="readonly" if users else "disabled")

    def _submit(self) -> None:
        if self.busy or self.closing:
            return
        incident = self.incident_var.get().strip().upper()
        if INCIDENT_PATTERN.fullmatch(incident) is None:
            self.status_var.set("Formato inválido: usa INC seguido de 12 dígitos.")
            self._append_log("Formato inválido. Ejemplo: INC123456789012")
            self.entry.focus_set()
            return

        resolver_group = self.resolver_group_var.get()
        resolver_user = self.resolver_user_var.get()
        if resolver_group not in RESOLVER_USERS:
            self.status_var.set("Selecciona un Grupo Resolutor.")
            self._append_log("Falta seleccionar el Grupo Resolutor.")
            self.resolver_group_combo.focus_set()
            return
        if resolver_user not in RESOLVER_USERS[resolver_group]:
            self.status_var.set("Selecciona un usuario válido para el grupo elegido.")
            self._append_log("Falta seleccionar un Usuario Resolutor válido para el grupo.")
            self.resolver_user_combo.focus_set()
            return
        resolution = self.resolution_text.get("1.0", "end-1c").strip()
        if not resolution:
            self.status_var.set("Escribe el texto de Resolución.")
            self._append_log("Falta ingresar el texto de Resolución.")
            self.resolution_text.focus_set()
            return

        self.busy = True
        self.search_button.configure(state="disabled")
        self.status_var.set(f"Navegando y buscando {incident}…")
        self._append_log(
            f"Solicitud: {incident} | Grupo: {resolver_group} | Usuario: {resolver_user} "
            "| Resolución: ingresada"
        )
        self.commands.put(
            WorkerCommand(
                action="search",
                incident=incident,
                resolver_group=resolver_group,
                resolver_user=resolver_user,
                resolution=resolution,
            )
        )

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
        self._append_log(event.message)
        if event.kind == "ready":
            self.status_var.set("Lista. Edge se abrirá al enviar una incidencia.")
            self.busy = False
            self.search_button.configure(state="normal")
        elif event.kind == "warning":
            self.status_var.set(event.message)
            self.busy = False
            self.search_button.configure(state="normal")
        elif event.kind == "done":
            self.status_var.set(f"Incidencia verificada: {event.incident}")
            self.busy = False
            self.search_button.configure(state="normal")
        elif event.kind == "searched":
            self.status_var.set(
                f"{event.incident}: Guardar enviado. Revisa la confirmación en Remedy."
            )
            self.busy = False
            self.search_button.configure(state="normal")
        elif event.kind == "error":
            self.status_var.set("No se pudo completar la búsqueda. Revisa el log.")
            self.busy = False
            self.search_button.configure(state="normal")
        elif event.kind == "closed":
            self.root.destroy()

    def _append_log(self, message: str) -> None:
        self.log_text.configure(state="normal")
        self.log_text.insert("end", message + "\n")
        self.log_text.see("end")
        self.log_text.configure(state="disabled")

    def _on_close(self) -> None:
        if self.closing:
            return
        self.closing = True
        self.busy = True
        self.search_button.configure(state="disabled")
        self.status_var.set("Cerrando Edge…")
        self.commands.put(WorkerCommand(action="stop"))
        self.root.after(100, self._poll_close)

    def _poll_close(self) -> None:
        while True:
            try:
                event = self.events.get_nowait()
            except queue.Empty:
                break
            self._handle_event(event)
            if event.kind == "closed":
                return
        self.root.after(100, self._poll_close)


def run_app() -> None:
    root = tk.Tk()
    HelixApp(root)
    root.mainloop()
