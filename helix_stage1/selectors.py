"""Selectores Helix centralizados; completar con datos observados en codegen."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from playwright.sync_api import Locator, Page


SelectorKind = Literal["role", "label", "text", "placeholder", "test_id", "css"]


@dataclass(frozen=True)
class LocatorSpec:
    """Describe un locator estable sin acoplar el flujo a un selector concreto."""

    kind: SelectorKind
    value: str
    name: str | None = None
    exact: bool = True

    def build(self, page: Page, incident: str = "") -> Locator:
        value = self.value.replace("{incident}", incident)
        name = self.name.replace("{incident}", incident) if self.name else None

        if self.kind == "role":
            return page.get_by_role(value, name=name, exact=self.exact)
        if self.kind == "label":
            return page.get_by_label(value, exact=self.exact)
        if self.kind == "text":
            return page.get_by_text(value, exact=self.exact)
        if self.kind == "placeholder":
            return page.get_by_placeholder(value, exact=self.exact)
        if self.kind == "test_id":
            return page.get_by_test_id(value)
        if self.kind == "css":
            return page.locator(value)
        raise ValueError(f"Tipo de selector no soportado: {self.kind}")


OPEN_INCIDENT_SEARCH: LocatorSpec | None = LocatorSpec(
    "css", '[ardbn="z2NI_SearchIncident"][artype="NavBarItem"]'
)
OPEN_NEW_INCIDENT: LocatorSpec | None = LocatorSpec(
    "css", '[ardbn="z2NI_NewIncident"][artype="NavBarItem"]'
)
NEW_INCIDENT_CUSTOMER_FIELD: LocatorSpec = LocatorSpec(
    "css", '[ardbn="Customer"][artype="Char"] textarea'
)
NEW_INCIDENT_TEMPLATE_FIELD: LocatorSpec = LocatorSpec(
    "css", '[ardbn="z1D_Template_Name"][artype="Char"] textarea'
)
NEW_INCIDENT_FAILURE_CONDITION_WIDGET: LocatorSpec = LocatorSpec(
    "css", '[ardbn="Condicion_de_falla"][artype="EnumSel"]'
)
NEW_INCIDENT_FAILURE_CONDITION_OPTION: LocatorSpec = LocatorSpec(
    "css", "table.MenuTable tr.MenuTableRow td.MenuEntryName"
)
NEW_INCIDENT_CATEGORIZATION_TAB: LocatorSpec = LocatorSpec(
    "css", "a.btn.f1:visible"
)
NEW_INCIDENT_CATEGORIZATION_COMPANY_FIELD: LocatorSpec = LocatorSpec(
    "css", '[ardbn="Company"][arid="1000000001"][artype="Char"] textarea'
)
NEW_INCIDENT_SUMMARY_FIELD: LocatorSpec = LocatorSpec(
    "css", '[arid="1000000000"][artype="Char"] textarea'
)
NEW_INCIDENT_NOTES_FIELD: LocatorSpec = LocatorSpec(
    "css", '[arid="1000000151"][artype="Char"] textarea'
)
NEW_INCIDENT_NUMBER_FIELD: LocatorSpec = LocatorSpec(
    "css", '[arid="1000000161"][artype="Char"] textarea[readonly]'
)
NEW_INCIDENT_SAVE_BUTTON: LocatorSpec = LocatorSpec(
    "css", 'a[artype="Control"][arid="301614800"]:has(div.f1:text-is("Guardar"))'
)
SEARCH_FIELD: LocatorSpec | None = LocatorSpec(
    "css",
    (
        'textarea[maxlen="15"]:not([readonly]), '
        'input[maxlength="15"]:not([readonly]), '
        'textarea[aria-label="ID de la incidencia"]:not([readonly]), '
        'input[aria-label="ID de la incidencia"]:not([readonly])'
    ),
)
NETWORKS_CHILE_TAB: LocatorSpec | None = LocatorSpec("css", "a.btn.f1:visible")
RESOLVER_GROUP_FIELD: LocatorSpec | None = LocatorSpec("label", "Grupo Resolutor")
RESOLVER_USER_FIELD: LocatorSpec | None = LocatorSpec("label", "Usuario Resolutor")
INCIDENT_RESOLUTION_FIELD: LocatorSpec | None = LocatorSpec("label", "Resolución")
SAVE_INCIDENT_BUTTON: LocatorSpec | None = LocatorSpec(
    "css", 'a[artype="Control"][arid="301614800"]:has(div.f1:text-is("Guardar"))'
)
HOME_BUTTON: LocatorSpec | None = LocatorSpec(
    "css", 'a[artype="Control"][title="Inicio"]:has(img[alt="Inicio"])'
)
INCIDENT_STATUS_WIDGET: LocatorSpec | None = LocatorSpec(
    "css", '[ardbn="Status"][artype="EnumSel"]'
)
CLOSED_STATUS_OPTION: LocatorSpec | None = LocatorSpec(
    "css", "table.MenuTable tr.MenuTableRow td.MenuEntryName"
)
STATUS_REASON_WIDGET: LocatorSpec | None = LocatorSpec(
    "css", 'textarea[armenu="SYS:RSN:StatusReason-Q-HPD-HelpDesk"]'
)
STATUS_REASON_TOGGLE_SELECTOR = (
    "xpath=following-sibling::a[contains(concat(' ', normalize-space(@class), ' '), ' menu ')]"
)
STATUS_REASON_OPTION: LocatorSpec | None = LocatorSpec(
    "css", "table.MenuTable tr.MenuTableRow td.MenuEntryName"
)
TASKS_TAB: LocatorSpec | None = LocatorSpec("css", "a.btn.f1:visible")
TASK_VIEW_BUTTON: LocatorSpec | None = LocatorSpec(
    "css", 'a[ardbn="z3Btn_TaskView"][arid="303518600"][artype="Control"]'
)
TASK_STATUS_WIDGET: LocatorSpec | None = LocatorSpec(
    "css", '[ardbn="Status"][artype="EnumSel"]'
)
TASK_SAVE_BUTTON: LocatorSpec | None = LocatorSpec(
    "css", 'a[ardbn="z3Btn_Save"][arid="300000300"][artype="Control"]'
)
TASK_INCIDENT_BREADCRUMB: LocatorSpec | None = LocatorSpec(
    "css", 'a.btn:has-text("Incidencia (Buscar)")'
)


def missing_selector_names() -> list[str]:
    return [
        name
        for name, spec in (
            ("OPEN_INCIDENT_SEARCH", OPEN_INCIDENT_SEARCH),
            ("SEARCH_FIELD", SEARCH_FIELD),
            ("NETWORKS_CHILE_TAB", NETWORKS_CHILE_TAB),
            ("RESOLVER_GROUP_FIELD", RESOLVER_GROUP_FIELD),
            ("RESOLVER_USER_FIELD", RESOLVER_USER_FIELD),
            ("INCIDENT_RESOLUTION_FIELD", INCIDENT_RESOLUTION_FIELD),
            ("SAVE_INCIDENT_BUTTON", SAVE_INCIDENT_BUTTON),
            ("HOME_BUTTON", HOME_BUTTON),
            ("INCIDENT_STATUS_WIDGET", INCIDENT_STATUS_WIDGET),
            ("CLOSED_STATUS_OPTION", CLOSED_STATUS_OPTION),
            ("STATUS_REASON_WIDGET", STATUS_REASON_WIDGET),
            ("STATUS_REASON_OPTION", STATUS_REASON_OPTION),
            ("TASKS_TAB", TASKS_TAB),
            ("TASK_VIEW_BUTTON", TASK_VIEW_BUTTON),
            ("TASK_STATUS_WIDGET", TASK_STATUS_WIDGET),
            ("TASK_SAVE_BUTTON", TASK_SAVE_BUTTON),
            ("TASK_INCIDENT_BREADCRUMB", TASK_INCIDENT_BREADCRUMB),
        )
        if spec is None
    ]
