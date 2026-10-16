"""Grupos locales de destinatarios para borradores de correo."""

from __future__ import annotations

import json
import re
from pathlib import Path

from .config import PROJECT_ROOT


LOCAL_PATH = PROJECT_ROOT / "email_recipients.local.json"
EXAMPLE_PATH = PROJECT_ROOT / "email_recipients.example.json"
EMAIL_PATTERN = re.compile(r"[^@\s,;]+@[^@\s,;]+\.[^@\s,;]+")


def _validated_addresses(name: str, addresses: object) -> tuple[str, ...]:
    if not isinstance(addresses, list) or any(not isinstance(addr, str) for addr in addresses):
        raise RuntimeError(f"El grupo '{name}' debe contener una lista de correos.")
    cleaned = tuple(addr.strip() for addr in addresses)
    if any(EMAIL_PATTERN.fullmatch(addr) is None for addr in cleaned):
        raise RuntimeError(f"El grupo '{name}' contiene un correo inválido.")
    unique: dict[str, str] = {}
    for address in cleaned:
        unique.setdefault(address.casefold(), address)
    return tuple(unique.values())


def _load_email_groups(
    path: Path,
) -> tuple[dict[str, dict[str, tuple[str, ...]]], dict[str, tuple[str, ...]]]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise RuntimeError(f"No se pudo leer el catálogo de correos: {path}") from error
    if not isinstance(raw, dict) or not {"to", "cc"}.issubset(raw) or set(raw) - {
        "to", "cc", "pending_verification"
    }:
        raise RuntimeError("El catálogo de correos debe contener 'to' y 'cc'.")

    to_section = raw["to"]
    cc_section = raw["cc"]
    if not isinstance(to_section, dict) or not to_section:
        raise RuntimeError("La sección 'to' debe contener operadores.")
    if not isinstance(cc_section, dict) or not cc_section:
        raise RuntimeError("La sección 'cc' debe contener grupos.")

    to_groups: dict[str, dict[str, tuple[str, ...]]] = {}
    for operator, groups in to_section.items():
        if not isinstance(operator, str) or not operator.strip():
            raise RuntimeError("La sección 'to' tiene un operador sin nombre.")
        # Acepta el catálogo plano anterior como un único grupo General.
        if isinstance(groups, list):
            groups = {"General": groups}
        if not isinstance(groups, dict) or not groups:
            raise RuntimeError(f"El operador '{operator}' debe contener grupos.")
        validated: dict[str, tuple[str, ...]] = {}
        for group_name, addresses in groups.items():
            if not isinstance(group_name, str) or not group_name.strip():
                raise RuntimeError(f"El operador '{operator}' tiene un grupo sin nombre.")
            validated[group_name.strip()] = _validated_addresses(group_name, addresses)
        if len(validated) != len(groups):
            raise RuntimeError(f"El operador '{operator}' contiene grupos duplicados.")
        to_groups[operator.strip()] = validated
    if len(to_groups) != len(to_section):
        raise RuntimeError("La sección 'to' contiene operadores duplicados.")

    cc_groups: dict[str, tuple[str, ...]] = {}
    for name, addresses in cc_section.items():
        if not isinstance(name, str) or not name.strip():
            raise RuntimeError("La sección 'cc' tiene un grupo sin nombre.")
        cc_groups[name.strip()] = _validated_addresses(name, addresses)
    if len(cc_groups) != len(cc_section):
        raise RuntimeError("La sección 'cc' contiene grupos duplicados.")
    return to_groups, cc_groups


def _recipient_groups(
    to_groups: dict[str, dict[str, tuple[str, ...]]],
    cc_groups: dict[str, tuple[str, ...]],
) -> dict[str, tuple[str, ...]]:
    contacts: dict[str, tuple[str, ...]] = {}
    for operator, groups in to_groups.items():
        for group_name, addresses in groups.items():
            label = operator if group_name == "General" else f"{operator} · {group_name}"
            contacts[label] = addresses
    for group_name, addresses in cc_groups.items():
        if addresses:
            contacts[f"Entel · {group_name} (CC)"] = addresses
    return contacts


EMAIL_RECIPIENT_GROUPS = _recipient_groups(
    *_load_email_groups(LOCAL_PATH if LOCAL_PATH.exists() else EXAMPLE_PATH)
)
