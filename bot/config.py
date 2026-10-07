"""Config loading: YAML -> attribute-access dicts, local override wins."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parent.parent


class Cfg(dict):
    """dict with attribute access, recursively."""

    def __getattr__(self, key: str) -> Any:
        try:
            value = self[key]
        except KeyError as exc:
            raise AttributeError(key) from exc
        return Cfg(value) if isinstance(value, dict) and not isinstance(value, Cfg) else value


def _merge(base: dict, override: dict) -> dict:
    out = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _merge(out[key], value)
        else:
            out[key] = value
    return out


def load_config(path: str | None = None) -> Cfg:
    data = yaml.safe_load((ROOT / "config.yaml").read_text())
    local = Path(path) if path else ROOT / "config.local.yaml"
    if local.exists():
        data = _merge(data, yaml.safe_load(local.read_text()) or {})
    elif path:
        raise FileNotFoundError(path)
    if data.get("mode") != "paper":
        raise SystemExit("Only mode: paper is supported. Live trading is not wired in this build.")
    return Cfg(data)
