"""Verify piece models import the way Domino organize does (pieces/ on sys.path)."""

from __future__ import annotations

import importlib
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PIECES = ROOT / "pieces"
sys.path.insert(0, str(PIECES))

for models_path in sorted(PIECES.glob("*/models.py")):
    name = models_path.parent.name
    if name == "common":
        continue
    mod = importlib.import_module(f"{name}.models")
    assert hasattr(mod, "InputModel"), name
    assert hasattr(mod, "OutputModel"), name
    print("OK", name, "+SecretsModel" if hasattr(mod, "SecretsModel") else "")

print("All piece models import successfully (Domino organize path).")
