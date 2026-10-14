"""A BasePiece with the same surface the pieces use, backed by the local disk."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any


class BasePiece:
    """Local substitute for domino.base_piece.BasePiece.

    Domino injects `results_path` per task and collects whatever the piece
    writes there. Here it is an ordinary directory chosen by the caller, so the
    outputs of a local run can be inspected exactly as the platform would
    present them.
    """

    def __init__(self, results_path: str | Path | None = None, **_: Any) -> None:
        self.results_path = str(results_path) if results_path else "."
        self.display_result: dict[str, Any] | None = None
        self.deploy_mode = "local"
        self.task_id = "local"
        self.dag_id = "local"
        self.logger = logging.getLogger(type(self).__name__)
        if not self.logger.handlers:
            handler = logging.StreamHandler()
            handler.setFormatter(logging.Formatter("[%(name)s] %(message)s"))
            self.logger.addHandler(handler)
            self.logger.setLevel(logging.INFO)

    def generate_paths(self) -> None:
        Path(self.results_path).mkdir(parents=True, exist_ok=True)

    def piece_function(self, input_data, secrets_data=None):  # pragma: no cover
        raise NotImplementedError

    def run(self, input_data, secrets_data=None):
        self.generate_paths()
        return self.piece_function(input_data, secrets_data)
