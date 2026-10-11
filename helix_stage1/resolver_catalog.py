"""Catálogo local de grupos y usuarios resolutores."""

from __future__ import annotations

import json
from pathlib import Path

from .config import PROJECT_ROOT


CATALOG_PATH = PROJECT_ROOT / "resolver_catalog.local.json"


def _load_resolver_users(path: Path) -> dict[str, tuple[str, ...]]:
    if not path.exists():
        return {}
    try:
        raw_catalog = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        raise RuntimeError(f"El catálogo local de resolutores no es JSON válido: {path}") from error

    if not isinstance(raw_catalog, dict):
        raise RuntimeError("El catálogo local de resolutores debe ser un objeto JSON.")

    catalog: dict[str, tuple[str, ...]] = {}
    for group, users in raw_catalog.items():
        if not isinstance(group, str) or not isinstance(users, list):
            raise RuntimeError("Cada grupo del catálogo debe tener una lista de usuarios.")
        clean_users = tuple(str(user).strip() for user in users if str(user).strip())
        if clean_users:
            catalog[group.strip()] = clean_users
    return catalog


RESOLVER_USERS = _load_resolver_users(CATALOG_PATH)
