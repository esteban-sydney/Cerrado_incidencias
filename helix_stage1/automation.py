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
    BROWSER_CHANNEL,
    BROWSER_SCALE_FACTOR,
    BROWSER_PROFILE_DIR,
    CATEGORIZATION_TAB_SETTLE_DELAY_MS,
    COMPANY_KEYSTROKE_DELAY_MS,
    COMPANY_SETTLE_DELAY_MS,
    CONSOLE_SETTLE_DELAY_MS,
    CUSTOMER_KEYSTROKE_DELAY_MS,
    CUSTOMER_SUGGESTION_DELAY_MS,
    DEFAULT_TIMEOUT_MS,
    FIELD_SETTLE_DELAY_MS,
    FAILURE_CONDITION_MENU_SETTLE_DELAY_MS,
    FAILURE_CONDITION_SETTLE_DELAY_MS,
    HELIX_URL,
    MENU_CLICK_TIMEOUT_MS,
    MENU_SETTLE_DELAY_MS,
    NAVIGATION_TIMEOUT_MS,
    NETWORKS_TAB_SETTLE_DELAY_MS,
    NEW_INCIDENT_TEXT_KEYSTROKE_DELAY_MS,
    NEW_INCIDENT_TEXT_SETTLE_DELAY_MS,
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
    TEMPLATE_KEYSTROKE_DELAY_MS,
    TEMPLATE_SUGGESTION_DELAY_MS,
)


@dataclass(frozen=True)
class WorkerCommand:
    action: str
    incident: str | None = None
    customer: str | None = None
    title: str | None = None
    resolver_group: str | None = None
    resolver_user: str | None = None
    resolution: str | None = None
    close_tasks: bool = False


@dataclass(frozen=True)
class WorkerEvent:
    kind: str
    message: str
    incident: str | None = None
    title: str | None = None


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
    """Único hilo propietario de Playwright, el navegador y sus páginas."""

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
        self._create_form_opened = False

    def run(self) -> None:
        self._emit("ready", "UI lista. El navegador se abrirá cuando envíes una incidencia.")
        try:
            while True:
                command = self.commands.get()
                if command.action == "stop":
                    break
                if command.action == "create":
                    self._open_new_incident(command.customer, command.title)
                    continue
                if command.action == "search" and not command.incident:
                    self._emit("error", "Comando de búsqueda sin número de incidencia.")
                    continue
                if command.action == "search" and command.incident:
                    self._process_incident(
                        command.incident,
                        command.resolver_group,
                        command.resolver_user,
                        command.resolution,
                        command.close_tasks,
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
        launch_args = ["--start-maximized"]
        if BROWSER_SCALE_FACTOR != 1.0:
            launch_args.append(f"--force-device-scale-factor={BROWSER_SCALE_FACTOR}")
        # IMPORTANTE: en Playwright Python `viewport=None` equivale a NO indicarlo y Playwright
        # emula 1280x720. Para usar todo el tamaño real de la ventana hay que usar no_viewport=True.
        self.context = self.playwright.chromium.launch_persistent_context(
            user_data_dir=str(BROWSER_PROFILE_DIR),
            channel=BROWSER_CHANNEL,
            headless=False,
            chromium_sandbox=True,
            no_viewport=True,
            args=launch_args,
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
        self._maximize_page_window(self.console_page)
        for extra_page in pages[1:]:
            if not extra_page.is_closed():
                extra_page.close()
        self.latest_page = self.console_page
        self._create_form_opened = False

    def _ensure_browser(self) -> None:
        """Abre el navegador o lo reabre si el usuario lo cerró manualmente."""
        if self.context is not None:
            page = self.console_page
            if page is None or page.is_closed():
                self.logger.warning("El navegador/pestaña fue cerrado; se reabrirá.")
                self._shutdown()
        if self.context is None:
            self._start_browser()

    def _maximize_page_window(self, page: Page) -> None:
        if self.context is None:
            return
        try:
            session = self.context.new_cdp_session(page)
            window = session.send("Browser.getWindowForTarget")
            session.send(
                "Browser.setWindowBounds",
                {
                    "windowId": window["windowId"],
                    "bounds": {"windowState": "maximized"},
                },
            )
        except PlaywrightError as error:
            self.logger.warning("No se pudo maximizar el navegador automáticamente: %s", error)

    def _refresh_layout(self, page: Page) -> None:
        """Pide a Remedy recalcular su layout con el tamaño real de la ventana."""
        try:
            page.evaluate("() => window.dispatchEvent(new Event('resize'))")
        except PlaywrightError as error:
            self.logger.warning("No se pudo refrescar el layout de Helix: %s", error)

    def _handle_new_page(self, page: Page) -> None:
        if self.console_page is None:
            self.console_page = page
            self._install_page_handlers(page)
            return
        if page is self.console_page:
            self._install_page_handlers(page)
            return
        self.logger.info("Cierro una pestaña emergente para mantener una sola ventana del navegador: %s", page.url)
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

    def _emit(
        self,
        kind: str,
        message: str,
        incident: str | None = None,
        title: str | None = None,
    ) -> None:
        self.events.put(WorkerEvent(kind=kind, message=message, incident=incident, title=title))

    def _ensure_session(self, page: Page) -> None:
        path = urlsplit(page.url).path.casefold()
        if any(token in path for token in ("/login", "login.jsp", "/sso/", "authentication")):
            raise SessionExpiredError(
                "Helix redirigió a autenticación. No se automatiza el login: inicia sesión "
                "manualmente en el navegador y vuelve a buscar, o consulta la alternativa CDP del README."
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

    def _center_locator(self, page: Page, locator: Locator, description: str, incident: str) -> None:
        try:
            locator.first.evaluate(
                "element => element.scrollIntoView({block: 'center', inline: 'center'})"
            )
            page.wait_for_timeout(250)
        except PlaywrightError as error:
            self.logger.warning(
                "No se pudo centrar %s para incidencia %s: %s",
                description,
                incident,
                error,
            )

    def _scroll_remedy_down(
        self,
        page: Page,
        incident: str,
        section: str,
        steps: int = 2,
        delta_y: int = 450,
    ) -> None:
        try:
            viewport = page.evaluate(
                "() => ({width: window.innerWidth || 1200, height: window.innerHeight || 800})"
            )
            x = min(850, max(200, int(viewport["width"]) - 120))
            y = min(560, max(220, int(viewport["height"]) - 180))
            self._emit("log", f"Desplazando Remedy hacia {section}…", incident)
            page.mouse.move(x, y)
            for _ in range(steps):
                page.mouse.wheel(0, delta_y)
                page.wait_for_timeout(200)
            self._check_unexpected_ui(page)
        except PlaywrightError as error:
            self.logger.warning(
                "No se pudo desplazar Remedy hacia %s para incidencia %s: %s",
                section,
                incident,
                error,
            )

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
            self._refresh_layout(self.console_page)
            self._check_unexpected_ui(self.console_page)
            return

        last_error: Exception | None = None
        for attempt in range(1, SEARCH_ATTEMPTS + 1):
            try:
                self.console_page.goto(HELIX_URL, wait_until="domcontentloaded")
                self.latest_page = self.console_page
                self._refresh_layout(self.console_page)
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

    def _open_new_incident(self, customer: str | None, title: str | None) -> None:
        incident_id: str | None = None
        save_attempted = False
        try:
            if not customer or not customer.strip():
                raise AutomationError("Selecciona un Cliente antes de crear el ticket.")
            if not title or not title.strip():
                raise AutomationError("Escribe un título para la nueva incidencia.")
            title = title.strip()
            if len(title) > 100:
                raise AutomationError("El título no puede superar 100 caracteres (límite de Resumen).")
            if not HELIX_URL:
                raise AutomationError(
                    "Configura HELIX_URL en helix_stage1/local_settings.py antes de ejecutar."
                )
            menu_spec = selectors.OPEN_NEW_INCIDENT
            if menu_spec is None:
                raise SelectorConfigurationError("Falta el selector de Nueva incidencia.")

            self._ensure_browser()
            self._navigate_to_console()
            page = self.console_page
            if page is None:
                raise AutomationError("La página de Helix no está disponible.")
            if self._create_form_opened:
                self._return_home_before_new_incident(page)
            self._settle(page, CONSOLE_SETTLE_DELAY_MS, "la consola", "")

            menu_item = menu_spec.build(page)
            menu_item.wait_for(state="visible", timeout=SPA_READY_TIMEOUT_MS)
            if menu_item.count() != 1:
                raise SelectorConfigurationError(
                    "Se esperaba una única opción 'Nueva incidencia'; "
                    f"se encontraron {menu_item.count()}."
                )
            self._emit("log", "Seleccionando 'Nueva incidencia'…")
            self._create_form_opened = True
            menu_item.click(timeout=MENU_CLICK_TIMEOUT_MS)
            self._settle(page, MENU_SETTLE_DELAY_MS, "el formulario de Nueva incidencia", "")
            customer_field = self._wait_for_single_editable_match(
                page,
                selectors.NEW_INCIDENT_CUSTOMER_FIELD.build(page),
                "Cliente de Nueva incidencia",
                SPA_READY_TIMEOUT_MS,
                "",
            )
            self._emit("log", f"Ingresando Cliente: {customer}.")
            customer_field.click(timeout=DEFAULT_TIMEOUT_MS)
            customer_field.press("ControlOrMeta+A", timeout=DEFAULT_TIMEOUT_MS)
            customer_field.press("Backspace", timeout=DEFAULT_TIMEOUT_MS)
            for character in customer.strip():
                customer_field.press(
                    "Space" if character == " " else character,
                    timeout=DEFAULT_TIMEOUT_MS,
                )
                page.wait_for_timeout(CUSTOMER_KEYSTROKE_DELAY_MS)
            typed_customer = customer_field.input_value(timeout=DEFAULT_TIMEOUT_MS).strip()
            if typed_customer.casefold() != customer.strip().casefold():
                raise VerificationError(
                    f"Cliente quedó escrito como '{typed_customer}'; "
                    f"se esperaba '{customer.strip()}'."
                )
            page.wait_for_timeout(CUSTOMER_SUGGESTION_DELAY_MS)
            self._check_unexpected_ui(page)
            suggestions = page.get_by_text(
                re.compile(r"^\s*" + re.escape(customer.strip()) + r"\s*\(", re.I)
            )
            try:
                suggestion = self._wait_for_single_visible_match(
                    page, suggestions, "sugerencia de Cliente", 1_000, ""
                )
            except PlaywrightTimeoutError:
                self._emit("log", "Remedy aún no muestra el Cliente; reintentando la última letra.")
                customer_field.press("End", timeout=DEFAULT_TIMEOUT_MS)
                customer_field.press("Backspace", timeout=DEFAULT_TIMEOUT_MS)
                customer_field.press(customer.strip()[-1], timeout=DEFAULT_TIMEOUT_MS)
                retry_customer = customer_field.input_value(timeout=DEFAULT_TIMEOUT_MS).strip()
                if retry_customer.casefold() != customer.strip().casefold():
                    raise VerificationError(
                        "El texto del campo Cliente cambió durante el reintento. "
                        "No se seleccionó ningún usuario."
                    )
                page.wait_for_timeout(CUSTOMER_SUGGESTION_DELAY_MS)
                try:
                    suggestion = self._wait_for_single_visible_match(
                        page, suggestions, "sugerencia de Cliente", 8_000, ""
                    )
                except PlaywrightTimeoutError as error:
                    raise AutomationError(
                        "Remedy recibió el apellido en Cliente, pero no mostró la sugerencia. "
                        "No se seleccionó ningún usuario; revisa el formulario en Chrome."
                    ) from error
            suggestion.click(timeout=DEFAULT_TIMEOUT_MS)
            actual_customer = customer_field.input_value(timeout=DEFAULT_TIMEOUT_MS).strip()
            if customer.strip().casefold() not in actual_customer.casefold():
                raise VerificationError(
                    "Se seleccionó la sugerencia, pero el campo Cliente no conserva "
                    "el apellido elegido. Revisa Remedy antes de continuar."
                )
            self._select_new_incident_template(page)
            self._select_new_incident_failure_condition(page)
            self._set_new_incident_title_fields(page, title)
            self._set_new_incident_categorization_company(page)
            incident_id = self._read_new_incident_id(page)
            self._scroll_remedy_down(page, incident_id, "el botón Guardar", steps=3)
            save_button = selectors.NEW_INCIDENT_SAVE_BUTTON.build(page)
            visible_buttons = self._wait_for_visible_locator_matches(
                page, save_button, "Guardar de Nueva incidencia", SPA_READY_TIMEOUT_MS, incident_id
            )
            if len(visible_buttons) != 1:
                raise SelectorConfigurationError(
                    "Se esperaba un único botón Guardar visible en Nueva incidencia; "
                    f"se encontraron {len(visible_buttons)}."
                )
            visible_buttons[0].scroll_into_view_if_needed(timeout=MENU_CLICK_TIMEOUT_MS)
            if visible_buttons[0].is_disabled():
                raise AutomationError("Guardar está deshabilitado; no se envió el guardado.")
            visible_buttons[0].click(trial=True, timeout=MENU_CLICK_TIMEOUT_MS)

            self._emit("log", f"Pulsando Guardar para {incident_id}.", incident_id)
            save_attempted = True
            visible_buttons[0].click(timeout=MENU_CLICK_TIMEOUT_MS)
            self.latest_page = page
            self.logger.info("Guardar pulsado para incidencia=%s; creación no verificada", incident_id)
            self._emit("create_save_sent", "Guardar pulsado; creación no verificada.", incident_id, title)
            try:
                self._settle(page, SAVE_TO_HOME_DELAY_MS, "el guardado antes de volver a Inicio", incident_id)
                self._return_home_before_new_incident(page)
                menu_item = menu_spec.build(page)
                menu_item.wait_for(state="visible", timeout=SPA_READY_TIMEOUT_MS)
                self._emit("log", "Remedy quedó en Inicio y listo para otra operación.", incident_id)
                self._emit("create_home_ready", "Remedy quedó en Inicio.", incident_id)
            except Exception as error:
                self._create_form_opened = True
                self.logger.warning("No se pudo volver a Inicio tras crear %s: %s", incident_id, error)
                self._emit(
                    "create_home_warning",
                    f"Guardar fue enviado para {incident_id}, pero Remedy no volvió a Inicio: {error}",
                    incident_id,
                )
        except Exception as error:
            self.logger.exception("Fallo creando incidencia; guardado intentado=%s", save_attempted)
            if save_attempted:
                self._emit(
                    "create_unverified",
                    "Se intentó pulsar Guardar, pero el resultado del clic es incierto. "
                    f"ID reservado: {incident_id or 'no disponible'}. "
                    f"Detalle: {error}. Busca ese ID en Remedy antes de volver a intentar.",
                    incident_id,
                    title,
                )
            else:
                self._emit("create_error", str(error))

    def _return_home_before_new_incident(self, page: Page) -> None:
        home_spec = selectors.HOME_BUTTON
        if home_spec is None:
            raise SelectorConfigurationError("Falta el selector del botón Inicio.")
        self._emit("log", "Volviendo a Inicio antes de crear otra incidencia…")
        home_button = self._wait_for_single_visible_match(
            page, home_spec.build(page), "botón Inicio", SPA_READY_TIMEOUT_MS, ""
        )
        if home_button.is_disabled():
            raise AutomationError("El botón Inicio está deshabilitado.")
        home_button.click(timeout=MENU_CLICK_TIMEOUT_MS)
        self._settle(page, CONSOLE_SETTLE_DELAY_MS, "Inicio", "")
        self._create_form_opened = False

    def _read_new_incident_id(self, page: Page) -> str:
        fields = self._wait_for_visible_locator_matches(
            page,
            selectors.NEW_INCIDENT_NUMBER_FIELD.build(page),
            "ID de la nueva incidencia",
            SPA_READY_TIMEOUT_MS,
            "",
        )
        if len(fields) != 1:
            raise SelectorConfigurationError(
                f"Se esperaba un único ID de incidencia visible; se encontraron {len(fields)}."
            )
        incident_id = fields[0].input_value(timeout=DEFAULT_TIMEOUT_MS).strip().upper()
        if re.fullmatch(r"INC\d{12}", incident_id) is None:
            raise VerificationError(
                f"El ID asignado por Remedy no tiene el formato esperado: '{incident_id}'."
            )
        self._emit("log", f"ID capturado antes de Guardar: {incident_id}.", incident_id)
        return incident_id

    def _select_new_incident_template(self, page: Page) -> str:
        prefix = "TX-"
        template_field = self._wait_for_single_editable_match(
            page,
            selectors.NEW_INCIDENT_TEMPLATE_FIELD.build(page),
            "Plantilla de Nueva incidencia",
            SPA_READY_TIMEOUT_MS,
            "",
        )
        self._emit("log", "Buscando Plantilla TX-.")
        template_field.click(timeout=DEFAULT_TIMEOUT_MS)
        template_field.press("ControlOrMeta+A", timeout=DEFAULT_TIMEOUT_MS)
        template_field.press("Backspace", timeout=DEFAULT_TIMEOUT_MS)
        for character in prefix:
            template_field.press(character, timeout=DEFAULT_TIMEOUT_MS)
            page.wait_for_timeout(TEMPLATE_KEYSTROKE_DELAY_MS)
        if template_field.input_value(timeout=DEFAULT_TIMEOUT_MS).strip().upper() != prefix:
            raise VerificationError("No se pudo escribir TX- completo en Plantilla.")

        page.wait_for_timeout(TEMPLATE_SUGGESTION_DELAY_MS)
        self._check_unexpected_ui(page)
        suggestions = page.get_by_text(re.compile(r"^\s*TX-", re.I))
        try:
            suggestion = self._wait_for_single_visible_match(
                page, suggestions, "sugerencia de Plantilla TX-", 1_000, ""
            )
        except PlaywrightTimeoutError:
            self._emit("log", "Remedy aún no muestra la Plantilla; reintentando la última tecla.")
            template_field.press("End", timeout=DEFAULT_TIMEOUT_MS)
            template_field.press("Backspace", timeout=DEFAULT_TIMEOUT_MS)
            template_field.press("-", timeout=DEFAULT_TIMEOUT_MS)
            if template_field.input_value(timeout=DEFAULT_TIMEOUT_MS).strip().upper() != prefix:
                raise VerificationError(
                    "El texto de Plantilla cambió durante el reintento. "
                    "No se seleccionó ninguna plantilla."
                )
            page.wait_for_timeout(TEMPLATE_SUGGESTION_DELAY_MS)
            try:
                suggestion = self._wait_for_single_visible_match(
                    page, suggestions, "sugerencia de Plantilla TX-", 8_000, ""
                )
            except PlaywrightTimeoutError as error:
                raise AutomationError(
                    "Remedy recibió TX- en Plantilla, pero no mostró una sugerencia. "
                    "No se seleccionó ninguna plantilla; revisa el formulario en Chrome."
                ) from error
        suggestion.click(timeout=DEFAULT_TIMEOUT_MS)
        template_name = template_field.input_value(timeout=DEFAULT_TIMEOUT_MS).strip()
        if not template_name.upper().startswith(prefix) or template_name.upper() == prefix:
            raise VerificationError(
                "Se hizo clic en la sugerencia, pero Plantilla no muestra un nombre TX- completo. "
                "Revisa Remedy antes de continuar."
            )
        return template_name

    def _select_new_incident_failure_condition(self, page: Page) -> None:
        widgets = self._wait_for_visible_locator_matches(
            page,
            selectors.NEW_INCIDENT_FAILURE_CONDITION_WIDGET.build(page),
            "Condición de falla de Nueva incidencia",
            SPA_READY_TIMEOUT_MS,
            "",
        )
        if len(widgets) != 1:
            raise SelectorConfigurationError(
                "Se esperaba una única Condición de falla visible; "
                f"se encontraron {len(widgets)}."
            )
        widget = widgets[0]
        selection = widget.locator("div.selection")
        field = selection.locator("input")
        toggle = selection.locator("a.selectionbtn")
        field.wait_for(state="visible", timeout=SPA_READY_TIMEOUT_MS)
        toggle.wait_for(state="visible", timeout=SPA_READY_TIMEOUT_MS)
        if selection.count() != 1 or field.count() != 1 or toggle.count() != 1:
            raise SelectorConfigurationError(
                "Condición de falla no tiene un control desplegable único."
            )
        expected = {"sin pérdida", "sin perdida"}
        current = (field.input_value(timeout=DEFAULT_TIMEOUT_MS) or "").strip()
        if current.casefold() in expected:
            self._emit("log", "Condición de falla ya está en Sin Pérdida.")
            return

        self._emit("log", "Seleccionando Condición de falla: Sin Pérdida.")
        toggle.click(timeout=MENU_CLICK_TIMEOUT_MS)
        self._settle(
            page,
            FAILURE_CONDITION_MENU_SETTLE_DELAY_MS,
            "las opciones de Condición de falla",
            "",
        )
        option = selectors.NEW_INCIDENT_FAILURE_CONDITION_OPTION.build(page).filter(
            has_text=re.compile(r"^\s*Sin P[eé]rdida\s*$", re.I)
        )
        visible_option = self._wait_for_single_visible_match(
            page, option, "la opción Sin Pérdida", DEFAULT_TIMEOUT_MS, ""
        )
        visible_option.click(timeout=MENU_CLICK_TIMEOUT_MS)
        self._settle(
            page,
            FAILURE_CONDITION_SETTLE_DELAY_MS,
            "la Condición de falla",
            "",
        )
        actual = (field.input_value(timeout=DEFAULT_TIMEOUT_MS) or "").strip()
        if actual.casefold() not in expected:
            raise VerificationError(
                f"Condición de falla quedó como '{actual}', no como 'Sin Pérdida'."
            )

    def _set_new_incident_categorization_company(self, page: Page) -> None:
        tab = selectors.NEW_INCIDENT_CATEGORIZATION_TAB.build(page).filter(
            has_text=re.compile(r"^\s*Categorización\s*$", re.I)
        )
        tabs = self._wait_for_visible_locator_matches(
            page, tab, "la pestaña Categorización", SPA_READY_TIMEOUT_MS, ""
        )
        if len(tabs) != 1:
            raise SelectorConfigurationError(
                "Se esperaba una única pestaña Categorización visible; "
                f"se encontraron {len(tabs)}."
            )
        self._emit("log", "Abriendo pestaña Categorización.")
        tabs[0].click(timeout=MENU_CLICK_TIMEOUT_MS)
        self._settle(page, CATEGORIZATION_TAB_SETTLE_DELAY_MS, "la pestaña Categorización", "")

        company_field = self._wait_for_single_editable_match(
            page,
            selectors.NEW_INCIDENT_CATEGORIZATION_COMPANY_FIELD.build(page),
            "Empresa de Categorización",
            SPA_READY_TIMEOUT_MS,
            "",
        )
        expected = "REDES CHILE"
        self._emit("log", f"Actualizando Empresa de Categorización a {expected}.")
        company_field.click(timeout=DEFAULT_TIMEOUT_MS)
        company_field.press("ControlOrMeta+A", timeout=DEFAULT_TIMEOUT_MS)
        company_field.press("Backspace", timeout=DEFAULT_TIMEOUT_MS)
        for character in expected:
            company_field.press(
                "Space" if character == " " else character,
                timeout=DEFAULT_TIMEOUT_MS,
            )
            page.wait_for_timeout(COMPANY_KEYSTROKE_DELAY_MS)
        typed_company = company_field.input_value(timeout=DEFAULT_TIMEOUT_MS).strip()
        if typed_company.casefold() != expected.casefold():
            raise VerificationError(
                f"Empresa de Categorización quedó escrita como '{typed_company}', "
                f"se esperaba '{expected}'."
            )
        company_field.press("Enter", timeout=DEFAULT_TIMEOUT_MS)
        self._settle(page, COMPANY_SETTLE_DELAY_MS, "la Empresa de Categorización", "")
        actual = company_field.input_value(timeout=DEFAULT_TIMEOUT_MS).strip()
        if actual.casefold() != expected.casefold():
            raise VerificationError(
                f"Tras Enter, Empresa de Categorización quedó como '{actual}', "
                f"no como '{expected}'."
            )

    def _set_new_incident_title_fields(self, page: Page, title: str) -> None:
        for field_name, field_spec in (
            ("Notas", selectors.NEW_INCIDENT_NOTES_FIELD),
            ("Resumen", selectors.NEW_INCIDENT_SUMMARY_FIELD),
        ):
            field = self._wait_for_single_editable_match(
                page, field_spec.build(page), field_name, SPA_READY_TIMEOUT_MS, ""
            )
            self._emit("log", f"Escribiendo título en {field_name}.")
            if field_name == "Notas":
                field.click(timeout=DEFAULT_TIMEOUT_MS)
                field.press("ControlOrMeta+A", timeout=DEFAULT_TIMEOUT_MS)
                field.press("Backspace", timeout=DEFAULT_TIMEOUT_MS)
                field.press_sequentially(
                    title,
                    delay=NEW_INCIDENT_TEXT_KEYSTROKE_DELAY_MS,
                    timeout=DEFAULT_TIMEOUT_MS,
                )
            else:
                field.fill(title, timeout=DEFAULT_TIMEOUT_MS)
            actual = field.input_value(timeout=DEFAULT_TIMEOUT_MS)
            if actual != title:
                raise VerificationError(
                    f"{field_name} no recibió el título completo. "
                    "La automatización se detuvo antes del siguiente campo."
                )
            field.press("Tab", timeout=DEFAULT_TIMEOUT_MS)
            self._settle(page, NEW_INCIDENT_TEXT_SETTLE_DELAY_MS, field_name, "")
            actual_after_blur = field.input_value(timeout=DEFAULT_TIMEOUT_MS)
            if actual_after_blur != title:
                raise VerificationError(
                    f"Remedy cambió el valor de {field_name} al salir del campo. "
                    "Revisa el formulario antes de guardar."
                )

        for field_name, field_spec in (
            ("Notas", selectors.NEW_INCIDENT_NOTES_FIELD),
            ("Resumen", selectors.NEW_INCIDENT_SUMMARY_FIELD),
        ):
            field = self._wait_for_single_editable_match(
                page, field_spec.build(page), field_name, SPA_READY_TIMEOUT_MS, ""
            )
            if field.input_value(timeout=DEFAULT_TIMEOUT_MS) != title:
                raise VerificationError(
                    f"{field_name} cambió después de completar ambos campos. "
                    "Revisa el formulario antes de guardar."
                )

    def _process_incident(
        self,
        incident: str,
        resolver_group: str | None,
        resolver_user: str | None,
        resolution: str | None,
        close_tasks: bool,
    ) -> None:
        self._emit("log", f"Abriendo Buscar incidencia para {incident}…", incident)
        result = "ERROR"
        try:
            if not resolver_group or not resolver_user:
                raise AutomationError("Selecciona un Grupo Resolutor y un Usuario Resolutor.")
            if not resolution or not resolution.strip():
                raise AutomationError("Ingresa el texto de Resolución en Tkinter.")
            if not HELIX_URL:
                raise AutomationError(
                    "Configura HELIX_URL en helix_stage1/local_settings.py antes de ejecutar."
                )
            self._validate_selectors()
            self._ensure_browser()
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

            if close_tasks:
                self._close_pending_tasks(search_page, incident)

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
            self._center_locator(search_page, group_field, "Grupo Resolutor", incident)
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
            self._center_locator(search_page, user_field, "Usuario Resolutor", incident)
            self._fill_and_verify(
                search_page,
                user_field,
                resolver_user,
                "Usuario Resolutor",
                RESOLVER_USER_SETTLE_DELAY_MS,
                incident,
            )

            self._scroll_remedy_down(search_page, incident, "la sección Estado", steps=2)
            status_widget_spec = selectors.INCIDENT_STATUS_WIDGET
            closed_option_spec = selectors.CLOSED_STATUS_OPTION
            assert status_widget_spec is not None and closed_option_spec is not None
            status_widget = status_widget_spec.build(search_page)
            self._emit("log", "Esperando que el widget Estado esté en el DOM…", incident)
            status_widget = self._incident_status_widget(search_page, status_widget, incident)
            self._center_locator(search_page, status_widget, "Estado", incident)
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
            self._scroll_remedy_down(search_page, incident, "la sección Resolución", steps=1)
            resolution_field.wait_for(state="visible", timeout=RESOLVER_FIELD_TIMEOUT_MS)
            self._center_locator(search_page, resolution_field, "Resolución", incident)
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

    def _incident_status_widget(self, page: Page, locator: Locator, incident: str) -> Locator:
        visible_status_widgets = self._wait_for_visible_locator_matches(
            page,
            locator,
            "el Estado visible de la incidencia",
            RESOLVER_FIELD_TIMEOUT_MS,
            incident,
        )
        if len(visible_status_widgets) == 1:
            return visible_status_widgets[0]

        candidates: list[tuple[int, Locator, str]] = []
        for widget in visible_status_widgets:
            details = widget.evaluate(
                "element => ({"
                "arwindowid: element.getAttribute('arwindowid') || '',"
                "id: element.id || '',"
                "text: (element.innerText || '').trim()"
                "})"
            )
            raw_window = str(details.get("arwindowid") or "")
            try:
                window_id = int(raw_window)
            except ValueError:
                window_id = 9999
            candidates.append((window_id, widget, str(details)))

        candidates.sort(key=lambda item: item[0])
        self.logger.info(
            "Se encontraron %s widgets Estado visibles para %s; se usará arwindowid menor. Detalle=%s",
            len(visible_status_widgets),
            incident,
            [detail for _window_id, _widget, detail in candidates],
        )
        return candidates[0][1]

    def _close_pending_tasks(self, page: Page, incident: str) -> None:
        tasks_tab_spec = selectors.TASKS_TAB
        task_view_spec = selectors.TASK_VIEW_BUTTON
        assert tasks_tab_spec is not None and task_view_spec is not None

        self._emit("log", "Validando tareas asociadas antes de Redes Chile…", incident)
        self._open_tasks_tab(page, incident)

        closed_count = 0
        for _attempt in range(10):
            pending_task = self._first_pending_task(page, incident)
            if pending_task is None:
                if closed_count:
                    self._emit(
                        "log",
                        f"Tareas pendientes cerradas: {closed_count}. Continuando con Redes Chile.",
                        incident,
                    )
                return

            task_id = str(pending_task["task_id"])
            state = str(pending_task["state"])
            row = pending_task["row"]
            self._emit(
                "log",
                f"Tarea {task_id} en estado '{state}'. Abriendo para cerrar…",
                incident,
            )
            if not hasattr(row, "click"):
                raise AutomationError("No se pudo identificar la fila de tarea pendiente.")
            row.click(timeout=MENU_CLICK_TIMEOUT_MS)
            self._settle(page, FIELD_SETTLE_DELAY_MS, f"la selección de tarea {task_id}", incident)

            view_button = task_view_spec.build(page)
            view_button.wait_for(state="visible", timeout=RESOLVER_FIELD_TIMEOUT_MS)
            if view_button.count() != 1:
                raise SelectorConfigurationError(
                    f"Se esperaba un único botón Ver de tareas; encontré {view_button.count()}."
                )
            if view_button.is_disabled():
                raise AutomationError("El botón Ver de tareas está deshabilitado.")

            view_button.click(timeout=MENU_CLICK_TIMEOUT_MS)
            self._settle(page, MENU_SETTLE_DELAY_MS, f"la pantalla de tarea {task_id}", incident)
            self._close_current_task(page, incident, task_id)
            closed_count += 1
            self._return_to_incident_from_task(page, incident, task_id)
            self._settle(page, NETWORKS_TAB_SETTLE_DELAY_MS, "la incidencia tras cerrar tarea", incident)
            self._open_tasks_tab(page, incident)
        raise AutomationError(
            "Se intentaron cerrar demasiadas tareas en una misma incidencia; proceso detenido por seguridad."
        )

    def _open_tasks_tab(self, page: Page, incident: str) -> None:
        tasks_tab_spec = selectors.TASKS_TAB
        assert tasks_tab_spec is not None

        tasks_tab = tasks_tab_spec.build(page).filter(
            has_text=re.compile(r"^\s*Tareas\s*$", re.IGNORECASE)
        )
        tasks_tab.first.wait_for(state="visible", timeout=SPA_READY_TIMEOUT_MS)
        visible_tabs = [
            tasks_tab.nth(index)
            for index in range(tasks_tab.count())
            if tasks_tab.nth(index).is_visible()
        ]
        if len(visible_tabs) != 1:
            raise SelectorConfigurationError(
                "Se esperaba una única pestaña visible 'Tareas'; "
                f"se encontraron {len(visible_tabs)}."
            )
        visible_tabs[0].click(timeout=MENU_CLICK_TIMEOUT_MS)
        self._settle(page, NETWORKS_TAB_SETTLE_DELAY_MS, "la pestaña Tareas", incident)

    def _first_pending_task(self, page: Page, incident: str) -> dict[str, object] | None:
        task_rows = page.locator("tr").filter(has_text=re.compile(r"TAS\d+", re.IGNORECASE))
        rows: list[tuple[Locator, str, str, str]] = []
        for index in range(task_rows.count()):
            row = task_rows.nth(index)
            if not row.is_visible():
                continue
            text = " ".join((row.inner_text(timeout=DEFAULT_TIMEOUT_MS) or "").split())
            task_match = re.search(r"TAS\d+", text, re.IGNORECASE)
            if task_match is None:
                continue
            task_id = task_match.group(0).upper()
            state = self._extract_task_state(text)
            rows.append((row, task_id, state, text))

        if not rows:
            self._emit("log", "No se encontraron tareas asociadas.", incident)
            return None

        pending = [
            (row, task_id, state, text)
            for row, task_id, state, text in rows
            if state.casefold() not in {"cerrado", "closed"}
        ]
        if not pending:
            self._emit("log", f"Tareas asociadas ya cerradas: {len(rows)}.", incident)
            return None

        row, task_id, state, text = pending[0]
        self.logger.info(
            "Tarea pendiente para incidencia %s: task_id=%s estado=%r fila=%r",
            incident,
            task_id,
            state,
            text,
        )
        return {"row": row, "task_id": task_id, "state": state}

    @staticmethod
    def _extract_task_state(row_text: str) -> str:
        known_states = (
            "Cerrado",
            "Asignado",
            "Pendiente",
            "Trabajo en curso",
            "En curso",
            "Esperando",
            "Omitido",
        )
        normalized = " ".join(row_text.split())
        for state in known_states:
            if re.search(rf"(?<!\w){re.escape(state)}(?!\w)", normalized, re.IGNORECASE):
                return state
        return "desconocido"

    def _close_current_task(self, page: Page, incident: str, task_id: str) -> None:
        task_status_spec = selectors.TASK_STATUS_WIDGET
        closed_option_spec = selectors.CLOSED_STATUS_OPTION
        task_save_spec = selectors.TASK_SAVE_BUTTON
        assert task_status_spec is not None and closed_option_spec is not None and task_save_spec is not None

        status_widget = task_status_spec.build(page)
        self._emit("log", f"Esperando Estado de tarea {task_id}…", incident)
        visible_status_widgets = self._wait_for_visible_locator_matches(
            page,
            status_widget,
            "el Estado visible de la tarea",
            RESOLVER_FIELD_TIMEOUT_MS,
            incident,
        )
        if len(visible_status_widgets) != 1:
            raise SelectorConfigurationError(
                "La pantalla de tarea debe tener un único Estado visible; "
                f"encontré {len(visible_status_widgets)}."
            )
        status_widget = visible_status_widgets[0]
        self._center_locator(page, status_widget, f"Estado de tarea {task_id}", incident)
        status_selection = status_widget.locator("div.selection")
        status_field = status_selection.locator("input")
        status_toggle = status_selection.locator("a.selectionbtn")
        status_field.wait_for(state="visible", timeout=RESOLVER_FIELD_TIMEOUT_MS)
        status_toggle.wait_for(state="visible", timeout=RESOLVER_FIELD_TIMEOUT_MS)

        status_before = (status_field.input_value(timeout=DEFAULT_TIMEOUT_MS) or "").strip()
        if status_before.casefold() in {"cerrado", "closed"}:
            self._emit("log", f"Tarea {task_id} ya está Cerrado; no se modifica.", incident)
            return

        self._emit("log", f"Cambiando tarea {task_id} de '{status_before}' a Cerrado…", incident)
        status_toggle.click(timeout=MENU_CLICK_TIMEOUT_MS)
        self._settle(page, STATUS_MENU_SETTLE_DELAY_MS, f"las opciones Estado de tarea {task_id}", incident)
        closed_option = closed_option_spec.build(page).filter(
            has_text=re.compile(r"^\s*Cerrado\s*$", re.IGNORECASE)
        )
        closed_option_visible = self._wait_for_single_visible_match(
            page,
            closed_option,
            "la opción exacta 'Cerrado' de la tarea",
            RESOLVER_FIELD_TIMEOUT_MS,
            incident,
        )
        closed_option_visible.click(timeout=MENU_CLICK_TIMEOUT_MS)
        self._settle(page, STATUS_SETTLE_DELAY_MS, f"Estado Cerrado de tarea {task_id}", incident)
        status_value = (status_field.input_value(timeout=DEFAULT_TIMEOUT_MS) or "").strip()
        status_title = (status_field.get_attribute("title") or "").strip()
        if not {status_value.casefold(), status_title.casefold()} & {"cerrado", "closed"}:
            raise VerificationError(
                f"Estado de tarea {task_id} no quedó en 'Cerrado'. "
                f"Valor detectado: '{status_value or status_title}'."
            )

        save_button = task_save_spec.build(page)
        self._emit("log", f"Guardando tarea {task_id}…", incident)
        save_button.wait_for(state="visible", timeout=RESOLVER_FIELD_TIMEOUT_MS)
        if save_button.count() != 1:
            raise SelectorConfigurationError(
                f"Se esperaba un único botón Guardar de tarea; encontré {save_button.count()}."
            )
        if save_button.is_disabled():
            raise AutomationError(f"El botón Guardar de tarea {task_id} está deshabilitado.")
        save_button.click(timeout=MENU_CLICK_TIMEOUT_MS)
        self._settle(page, RESULT_SETTLE_DELAY_MS, f"el guardado de tarea {task_id}", incident)
        self._emit("log", f"Tarea {task_id} guardada como Cerrado.", incident)

    def _wait_for_visible_locator_matches(
        self,
        page: Page,
        locator: Locator,
        description: str,
        timeout_ms: int,
        incident: str,
    ) -> list[Locator]:
        deadline = monotonic() + timeout_ms / 1000
        while True:
            visible_matches = [
                locator.nth(index)
                for index in range(locator.count())
                if locator.nth(index).is_visible()
            ]
            if visible_matches:
                return visible_matches

            remaining_ms = int((deadline - monotonic()) * 1000)
            if remaining_ms <= 0:
                raise PlaywrightTimeoutError(
                    f"Timeout esperando {description} visible tras {timeout_ms} ms."
                )
            page.wait_for_timeout(min(250, remaining_ms))
            self._check_unexpected_ui(page)

    def _return_to_incident_from_task(self, page: Page, incident: str, task_id: str) -> None:
        breadcrumb_spec = selectors.TASK_INCIDENT_BREADCRUMB
        assert breadcrumb_spec is not None

        breadcrumb = breadcrumb_spec.build(page)
        self._emit("log", f"Volviendo desde tarea {task_id} a Incidencia (Buscar)…", incident)
        breadcrumb.wait_for(state="visible", timeout=RESOLVER_FIELD_TIMEOUT_MS)
        visible_links = [
            breadcrumb.nth(index)
            for index in range(breadcrumb.count())
            if breadcrumb.nth(index).is_visible()
        ]
        if len(visible_links) != 1:
            raise SelectorConfigurationError(
                "Se esperaba un único breadcrumb 'Incidencia (Buscar)'; "
                f"se encontraron {len(visible_links)}."
            )
        visible_links[0].click(timeout=MENU_CLICK_TIMEOUT_MS)
        self._settle(page, RESULT_SETTLE_DELAY_MS, "la vuelta a Incidencia (Buscar)", incident)

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
        self._refresh_layout(page)
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
        if " ".join(actual_value.split()).casefold() != " ".join(expected_value.split()).casefold():
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
