from __future__ import annotations

import logging
import queue
import re
import threading
from dataclasses import dataclass
from time import monotonic
from urllib.parse import urlsplit

from playwright.sync_api import (
    BrowserContext,
    Dialog,
    Error as PlaywrightError,
    Locator,
    Page,
    TimeoutError as PlaywrightTimeoutError,
    sync_playwright,
)

from . import selectors
from .config import (
    BROWSER_PROFILE_DIR,
    CONSOLE_SETTLE_DELAY_MS,
    DEFAULT_TIMEOUT_MS,
    FIELD_SETTLE_DELAY_MS,
    HELIX_URL,
    MENU_CLICK_TIMEOUT_MS,
    MENU_SETTLE_DELAY_MS,
    NAVIGATION_TIMEOUT_MS,
    NETWORKS_TAB_SETTLE_DELAY_MS,
    RESULT_SETTLE_DELAY_MS,
    RESOLVER_FIELD_TIMEOUT_MS,
    RESOLVER_GROUP_SETTLE_DELAY_MS,
    RESOLVER_USER_SETTLE_DELAY_MS,
    SAVE_TO_HOME_DELAY_MS,
    SPA_READY_TIMEOUT_MS,
    SEARCH_ATTEMPTS,
    STATUS_MENU_SETTLE_DELAY_MS,
    STATUS_REASON_MENU_SETTLE_DELAY_MS,
    STATUS_REASON_SETTLE_DELAY_MS,
    STATUS_SETTLE_DELAY_MS,
)


@dataclass(frozen=True)
class WorkerCommand:
    action: str
    incident: str | None = None
    resolver_group: str | None = None
    resolver_user: str | None = None
    resolution: str | None = None


@dataclass(frozen=True)
class WorkerEvent:
    kind: str
    message: str
    incident: str | None = None


class AutomationError(RuntimeError):
    """Error que puede mostrarse directamente en la interfaz."""


class SessionExpiredError(AutomationError):
    pass


class SelectorConfigurationError(AutomationError):
    pass


class VerificationError(AutomationError):
    pass


class UnexpectedModalError(AutomationError):
    pass


class AutomationWorker(threading.Thread):
    """Único hilo propietario de Playwright, Edge y sus páginas."""

    def __init__(
        self,
        commands: queue.Queue[WorkerCommand],
        events: queue.Queue[WorkerEvent],
        logger: logging.Logger,
    ) -> None:
        super().__init__(name="helix-playwright", daemon=True)
        self.commands = commands
        self.events = events
        self.logger = logger
        self.playwright = None
        self.context: BrowserContext | None = None
        self.console_page: Page | None = None
        self.latest_page: Page | None = None
        self._dialog_messages: list[str] = []

    def run(self) -> None:
        self._emit("ready", "UI lista. Edge se abrirá cuando envíes una incidencia.")
        try:
            while True:
                command = self.commands.get()
                if command.action == "stop":
                    break
                if command.action == "search" and command.incident:
                    self._process_incident(
                        command.incident,
                        command.resolver_group,
                        command.resolver_user,
                        command.resolution,
                    )
        except Exception as error:
            self.logger.exception("Fallo no recuperable del trabajador de Playwright")
            self._emit("warning", f"El trabajador de automatización se detuvo: {error}")
        finally:
            self._shutdown()
            self._emit("closed", "Navegador cerrado.")

    def _start_browser(self) -> None:
        BROWSER_PROFILE_DIR.mkdir(parents=True, exist_ok=True)
        self.playwright = sync_playwright().start()
        self.context = self.playwright.chromium.launch_persistent_context(
            user_data_dir=str(BROWSER_PROFILE_DIR),
            channel="msedge",
            headless=False,
            chromium_sandbox=True,
            viewport=None,
            args=["--start-maximized"],
            timeout=NAVIGATION_TIMEOUT_MS,
        )
        self.context.set_default_timeout(DEFAULT_TIMEOUT_MS)
        self.context.set_default_navigation_timeout(NAVIGATION_TIMEOUT_MS)
        self.context.on("page", self._handle_new_page)

        pages = self.context.pages
        self.console_page = pages[0] if pages else None
        if self.console_page is None:
            self.console_page = self.context.new_page()
        self._install_page_handlers(self.console_page)
        for extra_page in pages[1:]:
            if not extra_page.is_closed():
                extra_page.close()
        self.latest_page = self.console_page

    def _handle_new_page(self, page: Page) -> None:
        if self.console_page is None:
            self.console_page = page
            self._install_page_handlers(page)
            return
        if page is self.console_page:
            self._install_page_handlers(page)
            return
        self.logger.info("Cierro una pestaña emergente para mantener una sola ventana Edge: %s", page.url)
        try:
            page.close()
        except PlaywrightError:
            pass

    def _install_page_handlers(self, page: Page) -> None:
        page.on("dialog", self._handle_dialog)

    def _handle_dialog(self, dialog: Dialog) -> None:
        self._dialog_messages.append(dialog.message)
        try:
            dialog.dismiss()
        except PlaywrightError:
            pass

    def _emit(self, kind: str, message: str, incident: str | None = None) -> None:
        self.events.put(WorkerEvent(kind=kind, message=message, incident=incident))

    def _ensure_session(self, page: Page) -> None:
        path = urlsplit(page.url).path.casefold()
        if any(token in path for token in ("/login", "login.jsp", "/sso/", "authentication")):
            raise SessionExpiredError(
                "Helix redirigió a autenticación. No se automatiza el login: inicia sesión "
                "manualmente en Edge y vuelve a buscar, o consulta la alternativa CDP del README."
            )

    def _check_unexpected_ui(self, page: Page) -> None:
        self._ensure_session(page)
        if self._dialog_messages:
            message = self._dialog_messages.pop(0)
            raise UnexpectedModalError(
                f"Helix mostró un cuadro de diálogo inesperado; se descartó para evitar bloquear "
                f"el navegador. Mensaje: {message}"
            )

        dialogs = page.get_by_role("dialog")
        for index in range(dialogs.count()):
            dialog = dialogs.nth(index)
            if dialog.is_visible():
                summary = self._summarize_visible_dialog(dialog)
                raise UnexpectedModalError(
                    "Helix muestra un diálogo/modal inesperado. "
                    "No se realizó ninguna modificación. "
                    f"Detalle: {summary}"
                )

    def _summarize_visible_dialog(self, dialog: Locator) -> str:
        try:
            details = dialog.evaluate(
                "element => ({"
                "text: (element.innerText || '').trim(),"
                "ariaLabel: element.getAttribute('aria-label') || '',"
                "title: element.getAttribute('title') || '',"
                "role: element.getAttribute('role') || ''"
                "})"
            )
        except PlaywrightError:
            return "No se pudo leer el contenido del modal."

        parts = [
            str(details.get("text") or "").strip(),
            str(details.get("ariaLabel") or "").strip(),
            str(details.get("title") or "").strip(),
            str(details.get("role") or "").strip(),
        ]
        summary = " | ".join(part for part in parts if part)
        if not summary:
            return "Modal visible sin texto detectable."
        return summary[:500]

    def _settle(self, page: Page, delay_ms: int, step: str, incident: str) -> None:
        if delay_ms <= 0:
            return
        self._emit("log", f"Esperando {delay_ms / 1000:g} s para estabilizar {step}…", incident)
        page.wait_for_timeout(delay_ms)
        self._check_unexpected_ui(page)

    def _navigate_to_console(self) -> None:
        if self.console_page is None:
            raise AutomationError("La página de Helix no está disponible.")

        current_url = urlsplit(self.console_page.url)
        target_url = urlsplit(HELIX_URL)
        if (
            current_url.scheme == target_url.scheme
            and current_url.netloc == target_url.netloc
            and current_url.path == target_url.path
        ):
            self.latest_page = self.console_page
            self._check_unexpected_ui(self.console_page)
            return

        last_error: Exception | None = None
        for attempt in range(1, SEARCH_ATTEMPTS + 1):
            try:
                self.console_page.goto(HELIX_URL, wait_until="domcontentloaded")
                self.latest_page = self.console_page
                self._check_unexpected_ui(self.console_page)
                return
            except SessionExpiredError:
                raise
            except Exception as error:
                last_error = error
                self.logger.warning(
                    "Navegación a consola falló (intento %s/%s): %s",
                    attempt,
                    SEARCH_ATTEMPTS,
                    error,
                )
        raise AutomationError(
            f"No se pudo abrir la consola de Helix tras {SEARCH_ATTEMPTS} intentos: {last_error}"
        )

    def _validate_selectors(self) -> None:
        missing = selectors.missing_selector_names()
        if missing:
            raise SelectorConfigurationError(
                "Faltan selectores reales en helix_stage1/selectors.py: "
                + ", ".join(missing)
                + ". Obténlos con la guía del README; no se usarán selectores inventados."
            )

    def _process_incident(
        self,
        incident: str,
        resolver_group: str | None,
        resolver_user: str | None,
        resolution: str | None,
    ) -> None:
        self._emit("log", f"Abriendo Buscar incidencia para {incident}…", incident)
        result = "ERROR"
        try:
            if not resolver_group or not resolver_user:
                raise AutomationError("Selecciona un Grupo Resolutor y un Usuario Resolutor.")
            if not resolution or not resolution.strip():
                raise AutomationError("Ingresa el texto de Resolución en Tkinter.")
            self._validate_selectors()
            if self.context is None:
                self._start_browser()
            self._navigate_to_console()
            console_page = self.console_page
            if console_page is None:
                raise AutomationError("La página de Helix no está disponible.")
            self._settle(console_page, CONSOLE_SETTLE_DELAY_MS, "la consola", incident)

            menu_spec = selectors.OPEN_INCIDENT_SEARCH
            search_spec = selectors.SEARCH_FIELD
            assert menu_spec is not None and search_spec is not None

            menu_item = menu_spec.build(console_page)
            self._emit("log", "Esperando que Remedy termine de cargar el menú…", incident)
            menu_item.wait_for(state="visible", timeout=SPA_READY_TIMEOUT_MS)
            if menu_item.count() != 1:
                raise SelectorConfigurationError(
                    "El locator del menú debe identificar una única opción 'Buscar incidencia'; "
                    f"encontré {menu_item.count()} elementos."
                )
            self._emit("log", "Seleccionando 'Buscar incidencia'…", incident)
            menu_item.click(timeout=MENU_CLICK_TIMEOUT_MS)
            self.logger.info("Clic en Buscar incidencia completado para %s", incident)
            self._emit("log", "Menú seleccionado; esperando el formulario de búsqueda…", incident)
            self._settle(console_page, MENU_SETTLE_DELAY_MS, "la navegación del menú", incident)

            search_page = console_page
            self.latest_page = search_page
            self._ensure_session(search_page)
            search_field = search_spec.build(search_page)
            try:
                self._emit("log", "Esperando el campo editable 'ID de la incidencia'…", incident)
                search_field = self._wait_for_single_editable_match(
                    search_page,
                    search_field,
                    "el campo editable 'ID de la incidencia'",
                    SPA_READY_TIMEOUT_MS,
                    incident,
                )
            except PlaywrightTimeoutError as error:
                self._check_unexpected_ui(search_page)
                raise AutomationError(
                    "Se pulsó 'Buscar incidencia', pero no apareció un campo editable para ingresar "
                    "el ID. Remedy puede haber recargado la incidencia anterior en lugar del formulario "
                    "de búsqueda; revisa el log y confirma que el botón Inicio dejó la consola lista."
                ) from error

            self._check_unexpected_ui(search_page)
            search_field.fill(incident)
            search_field.focus()
            self._settle(search_page, FIELD_SETTLE_DELAY_MS, "el campo de incidencia", incident)
            entered_number = search_field.input_value(timeout=DEFAULT_TIMEOUT_MS)
            if entered_number.strip().upper() != incident.upper():
                raise VerificationError(
                    f"El campo quedó con '{entered_number.strip()}', no con '{incident}'."
                )

            self._emit("log", "Ejecutando búsqueda con Enter…", incident)
            search_field.press("Enter")
            self._settle(search_page, RESULT_SETTLE_DELAY_MS, "los resultados de búsqueda", incident)

            tab_spec = selectors.NETWORKS_CHILE_TAB
            group_spec = selectors.RESOLVER_GROUP_FIELD
            user_spec = selectors.RESOLVER_USER_FIELD
            assert tab_spec is not None and group_spec is not None and user_spec is not None

            self._emit("log", "Esperando la pestaña 'Redes Chile'…", incident)
            networks_tab = tab_spec.build(search_page).filter(
                has_text=re.compile(r"^\s*Redes\s*Chile\s*$", re.IGNORECASE)
            )
            networks_tab.first.wait_for(state="visible", timeout=SPA_READY_TIMEOUT_MS)
            visible_tabs = [
                networks_tab.nth(index)
                for index in range(networks_tab.count())
                if networks_tab.nth(index).is_visible()
            ]
            if len(visible_tabs) != 1:
                raise SelectorConfigurationError(
                    "Se esperaba una única pestaña visible 'Redes Chile'; "
                    f"se encontraron {len(visible_tabs)}."
                )
            self._emit("log", "Abriendo pestaña 'Redes Chile'…", incident)
            visible_tabs[0].click(timeout=MENU_CLICK_TIMEOUT_MS)
            self._settle(
                search_page,
                NETWORKS_TAB_SETTLE_DELAY_MS,
                "la pestaña Redes Chile",
                incident,
            )

            group_field = group_spec.build(search_page)
            self._emit("log", "Esperando el campo Grupo Resolutor…", incident)
            group_field.wait_for(state="visible", timeout=RESOLVER_FIELD_TIMEOUT_MS)
            self._fill_and_verify(
                search_page,
                group_field,
                resolver_group,
                "Grupo Resolutor",
                RESOLVER_GROUP_SETTLE_DELAY_MS,
                incident,
            )

            user_field = user_spec.build(search_page)
            self._emit("log", "Esperando el campo Usuario Resolutor…", incident)
            user_field.wait_for(state="visible", timeout=RESOLVER_FIELD_TIMEOUT_MS)
            self._fill_and_verify(
                search_page,
                user_field,
                resolver_user,
                "Usuario Resolutor",
                RESOLVER_USER_SETTLE_DELAY_MS,
                incident,
            )

            status_widget_spec = selectors.INCIDENT_STATUS_WIDGET
            closed_option_spec = selectors.CLOSED_STATUS_OPTION
            assert status_widget_spec is not None and closed_option_spec is not None
            status_widget = status_widget_spec.build(search_page)
            self._emit("log", "Esperando que el widget Estado esté en el DOM…", incident)
            status_widget.wait_for(state="attached", timeout=RESOLVER_FIELD_TIMEOUT_MS)
            if status_widget.count() != 1:
                raise SelectorConfigurationError(
                    "El atributo de Remedy para Estado debe identificar un único EnumSel; "
                    f"encontré {status_widget.count()}."
                )
            status_selection = status_widget.locator("div.selection")
            self._emit("log", "Esperando el control visible del campo Estado…", incident)
            status_selection.wait_for(state="visible", timeout=RESOLVER_FIELD_TIMEOUT_MS)
            if status_selection.count() != 1:
                raise SelectorConfigurationError(
                    "El widget Estado debe contener un único bloque `div.selection`; "
                    f"encontré {status_selection.count()}."
                )
            status_field = status_selection.locator("input")
            status_field.wait_for(state="visible", timeout=RESOLVER_FIELD_TIMEOUT_MS)
            if status_field.count() != 1:
                raise SelectorConfigurationError(
                    "El widget Estado debe contener un único input para verificar el valor; "
                    f"encontré {status_field.count()}."
                )
            status_toggle = status_selection.locator("a.selectionbtn")
            status_toggle.wait_for(state="visible", timeout=RESOLVER_FIELD_TIMEOUT_MS)
            if status_toggle.count() != 1:
                raise SelectorConfigurationError(
                    "El bloque del campo Estado debe contener un único enlace `a.selectionbtn`; "
                    f"encontré {status_toggle.count()}."
                )

            status_before = (status_field.input_value(timeout=DEFAULT_TIMEOUT_MS) or "").strip()
            status_title_before = (status_field.get_attribute("title") or "").strip()
            self.logger.info(
                "Estado antes del dropdown para %s: valor=%r titulo=%r",
                incident,
                status_before,
                status_title_before,
            )
            self._emit(
                "log",
                f"Estado actual: '{status_before or status_title_before}'. "
                "Abriendo su enlace desplegable asociado.",
                incident,
            )
            status_toggle.click(timeout=MENU_CLICK_TIMEOUT_MS)
            self._emit(
                "log",
                "Click enviado a Estado > div.selection > a.selectionbtn; esperando 'Cerrado'…",
                incident,
            )
            self._settle(
                search_page,
                STATUS_MENU_SETTLE_DELAY_MS,
                "las opciones del selector Estado",
                incident,
            )

            closed_option = closed_option_spec.build(search_page).filter(
                has_text=re.compile(r"^\s*Cerrado\s*$", re.IGNORECASE)
            )
            self._emit("log", "Esperando la opción 'Cerrado'…", incident)
            closed_option_visible = self._wait_for_single_visible_match(
                search_page,
                closed_option,
                "la opción exacta 'Cerrado'",
                RESOLVER_FIELD_TIMEOUT_MS,
                incident,
            )
            self._emit("log", "Seleccionando Estado 'Cerrado'…", incident)
            closed_option_visible.click(timeout=MENU_CLICK_TIMEOUT_MS)
            self._settle(search_page, STATUS_SETTLE_DELAY_MS, "el estado Cerrado", incident)
            status_value = (status_field.input_value(timeout=DEFAULT_TIMEOUT_MS) or "").strip()
            status_title = (status_field.get_attribute("title") or "").strip()
            if not {status_value.casefold(), status_title.casefold()} & {"cerrado", "closed"}:
                raise VerificationError(
                    "Estado no quedó en 'Cerrado'. "
                    f"Valor detectado: '{status_value or status_title}'."
                )

            reason_widget_spec = selectors.STATUS_REASON_WIDGET
            reason_option_spec = selectors.STATUS_REASON_OPTION
            assert reason_widget_spec is not None and reason_option_spec is not None
            reason_widget = reason_widget_spec.build(search_page)
            self._emit("log", "Esperando el control Motivo del estado…", incident)
            reason_widget.wait_for(state="visible", timeout=RESOLVER_FIELD_TIMEOUT_MS)
            if reason_widget.count() != 1:
                raise SelectorConfigurationError(
                    "El textarea con el armenu de Motivo del estado debe ser único; "
                    f"encontré {reason_widget.count()}."
                )
            reason_field = reason_widget
            reason_toggle = reason_widget.locator(selectors.STATUS_REASON_TOGGLE_SELECTOR)
            reason_toggle.wait_for(state="visible", timeout=RESOLVER_FIELD_TIMEOUT_MS)
            if reason_toggle.count() != 1:
                raise SelectorConfigurationError(
                    "El textarea de Motivo del estado debe tener un único enlace desplegable "
                    "`a.btn.btn3d.menu` hermano; "
                    f"encontré {reason_toggle.count()}."
                )

            self._emit("log", "Abriendo la lista de Motivo del estado…", incident)
            reason_toggle.click(timeout=MENU_CLICK_TIMEOUT_MS)
            self._settle(
                search_page,
                STATUS_REASON_MENU_SETTLE_DELAY_MS,
                "las opciones de Motivo del estado",
                incident,
            )
            reason_option = reason_option_spec.build(search_page).filter(
                has_text=re.compile(
                    r"^\s*Resolución autom\. notificada\s*$",
                    re.IGNORECASE,
                )
            )
            self._emit("log", "Esperando 'Resolución autom. notificada'…", incident)
            reason_option_visible = self._wait_for_single_visible_match(
                search_page,
                reason_option,
                "la opción exacta 'Resolución autom. notificada'",
                RESOLVER_FIELD_TIMEOUT_MS,
                incident,
            )
            self._emit("log", "Seleccionando 'Resolución autom. notificada'…", incident)
            reason_option_visible.click(timeout=MENU_CLICK_TIMEOUT_MS)
            self._settle(
                search_page,
                STATUS_REASON_SETTLE_DELAY_MS,
                "el Motivo del estado",
                incident,
            )
            reason_value = (reason_field.input_value(timeout=DEFAULT_TIMEOUT_MS) or "").strip()
            reason_title = (reason_field.get_attribute("title") or "").strip()
            expected_reason = "Resolución autom. notificada"
            if expected_reason.casefold() not in {
                reason_value.casefold(),
                reason_title.casefold(),
            }:
                raise VerificationError(
                    "Motivo del estado no quedó en 'Resolución autom. notificada'. "
                    f"Valor detectado: '{reason_value or reason_title}'."
                )

            resolution_spec = selectors.INCIDENT_RESOLUTION_FIELD
            assert resolution_spec is not None
            resolution_field = resolution_spec.build(search_page)
            self._emit("log", "Esperando el campo Resolución…", incident)
            resolution_field.wait_for(state="visible", timeout=RESOLVER_FIELD_TIMEOUT_MS)
            self._fill_and_verify(
                search_page,
                resolution_field,
                resolution,
                "Resolución",
                0,
                incident,
            )

            save_spec = selectors.SAVE_INCIDENT_BUTTON
            assert save_spec is not None
            save_button = save_spec.build(search_page)
            self._emit("log", "Esperando el botón Guardar…", incident)
            save_button.wait_for(state="visible", timeout=RESOLVER_FIELD_TIMEOUT_MS)
            if save_button.count() != 1:
                raise SelectorConfigurationError(
                    f"Se esperaba un único control Guardar; encontré {save_button.count()}."
                )
            if save_button.is_disabled():
                raise AutomationError("El botón Guardar está deshabilitado; no se guardó.")

            self._emit("log", "Guardando incidencia con los campos verificados…", incident)
            save_button.click(timeout=MENU_CLICK_TIMEOUT_MS)
            self._settle(
                search_page,
                RESULT_SETTLE_DELAY_MS,
                "la respuesta de guardado de Remedy",
                incident,
            )
            save_confirmed = search_page.is_closed()
            if not search_page.is_closed():
                save_success = search_page.get_by_text(
                    re.compile(
                        r"guardad[oa]|saved successfully|registro guardado|se guardó",
                        re.IGNORECASE,
                    )
                )
                try:
                    save_success.first.wait_for(state="visible", timeout=5_000)
                    save_confirmed = True
                except PlaywrightTimeoutError:
                    self.logger.warning(
                        "Se envió Guardar para %s, pero no se detectó confirmación visible; "
                        "no se reintentará automáticamente.",
                        incident,
                    )

            if not search_page.is_closed():
                self._settle(
                    search_page,
                    SAVE_TO_HOME_DELAY_MS,
                    "el guardado antes de volver a Inicio",
                    incident,
                )
                self._return_home_after_save(search_page, incident)
            result = (
                "INCIDENCIA GUARDADA Y CONFIRMADA"
                if save_confirmed
                else "CLICK GUARDAR ENVIADO; CONFIRMACIÓN NO DETECTADA"
            )
            message = (
                f"{incident} buscada. En 'Redes Chile' se rellenaron Grupo Resolutor "
                f"'{resolver_group}', Usuario Resolutor '{resolver_user}', Estado 'Cerrado', "
                "Motivo del estado 'Resolución autom. notificada' y Resolución. "
                + (
                    "Remedy confirmó el guardado."
                    if save_confirmed
                    else "Se pulsó Guardar, pero no se detectó confirmación; verifica Remedy "
                    "antes de reintentar."
                )
            )
            self.logger.info("incidencia=%s resultado=%s", incident, result)
            self._emit("searched", message, incident)
        except Exception as error:
            self.logger.exception("Fallo procesando incidencia=%s resultado=%s", incident, result)
            self._emit("error", str(error), incident)

    def _return_home_after_save(self, page: Page, incident: str) -> None:
        home_spec = selectors.HOME_BUTTON
        menu_spec = selectors.OPEN_INCIDENT_SEARCH
        assert home_spec is not None and menu_spec is not None

        self._emit("log", "Volviendo a Inicio para dejar Remedy listo…", incident)
        home_button = home_spec.build(page)
        home_button.wait_for(state="visible", timeout=RESOLVER_FIELD_TIMEOUT_MS)
        if home_button.count() != 1:
            raise SelectorConfigurationError(
                f"Se esperaba un único botón Inicio; encontré {home_button.count()}."
            )
        if home_button.is_disabled():
            raise AutomationError("El botón Inicio está deshabilitado; no se pudo volver al inicio.")

        home_button.click(timeout=MENU_CLICK_TIMEOUT_MS)
        self._settle(page, CONSOLE_SETTLE_DELAY_MS, "la pantalla de inicio", incident)
        self._emit("log", "Recargando la consola para iniciar desde cero…", incident)
        page.goto(HELIX_URL, wait_until="domcontentloaded")
        self._settle(page, CONSOLE_SETTLE_DELAY_MS, "la consola recargada", incident)

        menu_item = menu_spec.build(page)
        menu_item.wait_for(state="visible", timeout=SPA_READY_TIMEOUT_MS)
        self.latest_page = page
        self.console_page = page
        self._emit("log", "Remedy quedó en Inicio y listo para una nueva búsqueda.", incident)

    def _fill_and_verify(
        self,
        page: Page,
        field: Locator,
        expected_value: str,
        field_name: str,
        settle_delay_ms: int,
        incident: str,
    ) -> None:
        self._emit("log", f"Ingresando {field_name}: {expected_value}…", incident)
        field.fill(expected_value, timeout=SPA_READY_TIMEOUT_MS)
        field.focus()
        self._settle(page, settle_delay_ms, field_name, incident)
        actual_value = field.input_value(timeout=DEFAULT_TIMEOUT_MS).strip()
        if actual_value.casefold() != expected_value.strip().casefold():
            raise VerificationError(
                f"{field_name} quedó como '{actual_value}', se esperaba '{expected_value}'."
            )

    def _wait_for_single_visible_match(
        self,
        page: Page,
        locator: Locator,
        description: str,
        timeout_ms: int,
        incident: str,
    ) -> Locator:
        deadline = monotonic() + timeout_ms / 1000
        while True:
            visible_matches = [
                (locator.nth(index), locator.nth(index).bounding_box())
                for index in range(locator.count())
                if locator.nth(index).is_visible()
            ]
            visible_matches = [
                (match, box) for match, box in visible_matches if box is not None
            ]
            visual_rows: list[list[tuple[Locator, dict[str, float]]]] = []
            for match, box in visible_matches:
                matching_row = next(
                    (
                        row
                        for row in visual_rows
                        if abs(
                            (row[0][1]["y"] + row[0][1]["height"] / 2)
                            - (box["y"] + box["height"] / 2)
                        ) <= max(2.0, min(row[0][1]["height"], box["height"]) * 0.2)
                    ),
                    None,
                )
                if matching_row is None:
                    visual_rows.append([(match, box)])
                else:
                    matching_row.append((match, box))

            if len(visual_rows) == 1:
                row_matches = visual_rows[0]
                most_specific = max(
                    row_matches,
                    key=lambda item: item[0].evaluate(
                        "element => {let depth = 0; "
                        "for (let node = element; node; node = node.parentElement) depth++; "
                        "return depth;}"
                    ),
                )
                return most_specific[0]
            if len(visual_rows) > 1:
                match_details = [
                    match.evaluate(
                        "element => {"
                        "const ancestors = [];"
                        "let current = element;"
                        "for (let depth = 0; current && depth < 4; depth++, current = current.parentElement) {"
                        "ancestors.push({tag: current.tagName, id: current.id || '', "
                        "className: typeof current.className === 'string' ? current.className : '', "
                        "role: current.getAttribute('role') || '', "
                        "ardbn: current.getAttribute('ardbn') || ''});"
                        "}"
                        "return {tag: element.tagName, id: element.id || '', "
                        "className: typeof element.className === 'string' ? element.className : '', "
                        "role: element.getAttribute('role') || '', "
                        "insideStatusWidget: Boolean(element.closest('[ardbn=\\\"Status\\\"]'))," 
                        "ancestors};"
                        "}"
                    )
                    for row in visual_rows
                    for match, _box in row
                ]
                self.logger.warning(
                    "%s tiene varias coincidencias visibles para incidencia %s: %s",
                    description,
                    incident,
                    match_details,
                )
                raise SelectorConfigurationError(
                    f"{description} aparece en {len(visual_rows)} filas visuales distintas; "
                    "no seleccionaré entre opciones separadas. "
                    f"Detalles DOM (revisa logs/automation.log): {match_details}"
                )

            remaining_ms = int((deadline - monotonic()) * 1000)
            if remaining_ms <= 0:
                raise PlaywrightTimeoutError(
                    f"Timeout esperando {description} visible tras {timeout_ms} ms."
                )
            page.wait_for_timeout(min(250, remaining_ms))
            self._check_unexpected_ui(page)

    def _wait_for_single_editable_match(
        self,
        page: Page,
        locator: Locator,
        description: str,
        timeout_ms: int,
        incident: str,
    ) -> Locator:
        deadline = monotonic() + timeout_ms / 1000
        while True:
            editable_matches: list[Locator] = []
            for index in range(locator.count()):
                candidate = locator.nth(index)
                if not candidate.is_visible():
                    continue
                if candidate.evaluate(
                    "element => !element.disabled && !element.readOnly && "
                    "element.getAttribute('aria-disabled') !== 'true'"
                ):
                    editable_matches.append(candidate)

            if len(editable_matches) == 1:
                return editable_matches[0]
            if len(editable_matches) > 1:
                raise SelectorConfigurationError(
                    f"{description} aparece como editable en {len(editable_matches)} controles; "
                    "no seleccionaré entre campos ambiguos."
                )

            remaining_ms = int((deadline - monotonic()) * 1000)
            if remaining_ms <= 0:
                readonly_count = locator.evaluate_all(
                    "elements => elements.filter(element => element.readOnly).length"
                )
                total_count = locator.count()
                self.logger.warning(
                    "%s no quedó editable para incidencia %s. total=%s readonly=%s",
                    description,
                    incident,
                    total_count,
                    readonly_count,
                )
                raise PlaywrightTimeoutError(
                    f"Timeout esperando {description} editable tras {timeout_ms} ms. "
                    f"Coincidencias: {total_count}; readonly: {readonly_count}."
                )
            page.wait_for_timeout(min(250, remaining_ms))
            self._check_unexpected_ui(page)

    def _shutdown(self) -> None:
        if self.context is not None:
            try:
                self.context.close()
            except PlaywrightError:
                pass
            self.context = None
        if self.playwright is not None:
            try:
                self.playwright.stop()
            except PlaywrightError:
                pass
            self.playwright = None
