"""Tiny JSON settings store in the user's app-data folder (no .env needed)."""

from __future__ import annotations

import json
import os
import sys
import threading
from pathlib import Path

_lock = threading.Lock()


def app_dir() -> Path:
    if sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    elif os.name == "nt":
        base = Path(os.environ.get("APPDATA", Path.home()))
    else:
        base = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    d = base / "Tracklister"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _path() -> Path:
    return app_dir() / "settings.json"


def load() -> dict:
    with _lock:
        try:
            return json.loads(_path().read_text("utf-8"))
        except (OSError, ValueError):
            return {}


def save(data: dict) -> None:
    with _lock:
        p = _path()
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2), "utf-8")
        if os.name != "nt":
            os.chmod(tmp, 0o600)
        tmp.replace(p)
