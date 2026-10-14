"""Minimal stand-ins for the Domino runtime, for running pieces locally.

Pieces import `domino.base_piece.BasePiece`, which only exists inside the
Domino worker image. Without a substitute the only way to exercise the workflow
is to build the image and submit a run, which is a slow loop for a change that
breaks in the first piece.

This package provides just enough of the contract that pieces rely on:
`results_path`, `display_result` and the `piece_function` entry point. It is a
test harness, not a reimplementation, and nothing in the production path imports
it when the real Domino runtime is present.
"""

import sys
import types

from .base_piece import BasePiece

__all__ = ["BasePiece", "install_domino_stub"]


def install_domino_stub() -> None:
    """Make `from domino.base_piece import BasePiece` resolve to the harness.

    Registering the stub in sys.modules rather than editing each piece keeps the
    pieces importing the real thing, so nothing about the production import path
    depends on this package existing.
    """
    if "domino.base_piece" in sys.modules:
        return

    domino = sys.modules.get("domino") or types.ModuleType("domino")
    base_piece = types.ModuleType("domino.base_piece")
    base_piece.BasePiece = BasePiece
    domino.base_piece = base_piece
    sys.modules["domino"] = domino
    sys.modules["domino.base_piece"] = base_piece
