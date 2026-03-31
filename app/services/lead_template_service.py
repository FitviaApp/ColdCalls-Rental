"""
Template helpers for per-lead variable rendering.
"""
from __future__ import annotations

import json
import re
from typing import Any

_TEMPLATE_PATTERN = re.compile(r"\{\{\s*([a-zA-Z0-9_]+)\s*\}\}")


def parse_lead_variables_json(raw_value: str | None) -> dict[str, str]:
    if not raw_value:
        return {}
    try:
        data = json.loads(raw_value)
    except Exception:
        return {}
    if not isinstance(data, dict):
        return {}
    variables: dict[str, str] = {}
    for key, value in data.items():
        normalized_key = str(key or "").strip().lower()
        if not normalized_key:
            continue
        variables[normalized_key] = str(value or "").strip()
    return variables


def render_lead_template(text: str | None, variables: dict[str, Any] | None) -> str:
    source = str(text or "")
    values = {str(k).strip().lower(): str(v or "").strip() for k, v in (variables or {}).items()}

    def _replace(match: re.Match[str]) -> str:
        key = match.group(1).strip().lower()
        return values.get(key, "")

    return _TEMPLATE_PATTERN.sub(_replace, source)
