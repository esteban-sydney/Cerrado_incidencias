"""Clientes seleccionables para la creación de incidencias."""

from __future__ import annotations

import json
from pathlib import Path

from .config import PROJECT_ROOT


CATALOG_PATH = PROJECT_ROOT / "customer_catalog.local.json"


def _load_customers(path: Path) -> tuple[str, ...]:
    if not path.exists():
        return ()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        raise RuntimeError(f"El catálogo local de clientes no es JSON válido: {path}") from error
    if not isinstance(raw, list) or any(not isinstance(item, str) for item in raw):
        raise RuntimeError("El catálogo local de clientes debe ser una lista de textos.")
    customers = tuple(item.strip() for item in raw)
    if any(not item for item in customers) or len(set(customers)) != len(customers):
        raise RuntimeError("El catálogo de clientes no debe contener vacíos ni duplicados.")
    return customers


CUSTOMERS = _load_customers(CATALOG_PATH)
