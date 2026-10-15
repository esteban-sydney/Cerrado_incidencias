from __future__ import annotations

import os
from pathlib import Path

try:
    from . import local_settings as _local_settings
except ImportError:
    _local_settings = None


def _local_value(name: str, default: object) -> object:
    if _local_settings is None:
        return default
    return getattr(_local_settings, name, default)


PROJECT_ROOT = Path(__file__).resolve().parents[1]
HELIX_URL = (
    os.environ.get("HELIX_URL", "").strip()
    or str(_local_value("HELIX_URL", "")).strip()
)
BROWSER_CHANNEL = (
    os.environ.get("BROWSER_CHANNEL", "").strip()
    or str(_local_value("BROWSER_CHANNEL", "chrome")).strip()
    or "chrome"
)
# 1.0 = tamaño normal. Si necesitas ver más contenido prueba 0.9 (zoom a nivel de navegador,
# nunca CSS zoom: Remedy calcula su layout en JavaScript y se rompe con html{zoom}).
BROWSER_SCALE_FACTOR = float(
    os.environ.get("BROWSER_SCALE_FACTOR", "")
    or _local_value("BROWSER_SCALE_FACTOR", 1.0)
)
BROWSER_PROFILE_DIR = PROJECT_ROOT / "chrome_profile"
LOG_DIR = PROJECT_ROOT / "logs"

DEFAULT_TIMEOUT_MS = 60_000
NAVIGATION_TIMEOUT_MS = 90_000
SPA_READY_TIMEOUT_MS = 90_000
RESOLVER_FIELD_TIMEOUT_MS = 90_000
MENU_CLICK_TIMEOUT_MS = 30_000
CONSOLE_SETTLE_DELAY_MS = 2_000
MENU_SETTLE_DELAY_MS = 3_000
FIELD_SETTLE_DELAY_MS = 1_500
CUSTOMER_KEYSTROKE_DELAY_MS = 350
CUSTOMER_SUGGESTION_DELAY_MS = 2_000
TEMPLATE_KEYSTROKE_DELAY_MS = 350
TEMPLATE_SUGGESTION_DELAY_MS = 2_000
FAILURE_CONDITION_MENU_SETTLE_DELAY_MS = 1_500
FAILURE_CONDITION_SETTLE_DELAY_MS = 1_000
CATEGORIZATION_TAB_SETTLE_DELAY_MS = 1_500
COMPANY_KEYSTROKE_DELAY_MS = 350
COMPANY_SETTLE_DELAY_MS = 2_000
NEW_INCIDENT_TEXT_KEYSTROKE_DELAY_MS = 60
NEW_INCIDENT_TEXT_SETTLE_DELAY_MS = 500
RESULT_SETTLE_DELAY_MS = 4_000
NETWORKS_TAB_SETTLE_DELAY_MS = 3_500
RESOLVER_GROUP_SETTLE_DELAY_MS = 2_500
RESOLVER_USER_SETTLE_DELAY_MS = 0
STATUS_MENU_SETTLE_DELAY_MS = 1_500
STATUS_SETTLE_DELAY_MS = 2_500
STATUS_REASON_MENU_SETTLE_DELAY_MS = 1_500
STATUS_REASON_SETTLE_DELAY_MS = 2_000
SAVE_TO_HOME_DELAY_MS = 3_000
SEARCH_ATTEMPTS = 2
