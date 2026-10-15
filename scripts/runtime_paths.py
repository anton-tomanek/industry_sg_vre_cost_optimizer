"""Resolve repo vs frozen (.exe) paths for the local UC3.2 SEED app."""

from __future__ import annotations

import sys
from pathlib import Path


def force_utf8_stdio() -> None:
    """Windows consoles often use cp1250; piece messages may contain arrows etc."""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if not callable(reconfigure):
            continue
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


def is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"))


def bundle_root() -> Path:
    """Read-only assets (pieces, static HTML, demo inputs)."""
    if is_frozen():
        return Path(sys._MEIPASS)  # type: ignore[attr-defined]
    return Path(__file__).resolve().parents[1]


def app_root() -> Path:
    """Writable root: folder next to the .exe, or the git checkout."""
    if is_frozen():
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parents[1]


def ensure_import_paths() -> None:
    force_utf8_stdio()
    root = bundle_root()
    for path in (root, root / "pieces", root / "scripts", root / "webapp"):
        text = str(path)
        if text not in sys.path:
            sys.path.insert(0, text)
