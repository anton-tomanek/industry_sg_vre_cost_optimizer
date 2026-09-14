"""Reusable OneData boundary helpers for Domino pieces."""

from __future__ import annotations

import inspect
from functools import wraps
from typing import Any, Callable, TypeVar

T = TypeVar("T")


def import_onedata_io():
    try:
        from common import onedata_io as od
    except ModuleNotFoundError:
        try:
            from pieces.common import onedata_io as od
        except ModuleNotFoundError:
            od = None
    return od


def run_id_for_piece(od: Any, input_data: Any, secrets_data: Any, *, entry: bool = False) -> str | None:
    if od is None:
        return None
    if entry:
        return od.resolve_run_id(input_data, secrets_data, generate=True)
    return od.resolve_run_id(input_data, secrets_data, generate=False)


def finish_or_return(
    od: Any,
    output: Any,
    results_path: str,
    secrets_data: Any,
    piece_name: str,
    stage: Any,
    run_id: str | None,
) -> Any:
    if od is not None and output is not None:
        return od.finish_piece(
            output, results_path, secrets_data, piece_name, stage, run_id=run_id
        )
    if stage is not None:
        stage.cleanup()
    return output


def _call_piece(fn, self, input_data, secrets_data, run_id):
    params = inspect.signature(fn).parameters
    kwargs: dict[str, Any] = {}
    if "secrets_data" in params:
        kwargs["secrets_data"] = secrets_data
    if "run_id" in params:
        kwargs["run_id"] = run_id
    return fn(self, input_data, **kwargs)


def onedata_piece(
    piece_name: str,
    *,
    entry: bool = False,
) -> Callable[[Callable[..., T]], Callable[..., T]]:
    """Decorator: stage OneData inputs, mirror outputs to per-run folder."""

    def decorator(fn: Callable[..., T]) -> Callable[..., T]:
        @wraps(fn)
        def wrapper(self, input_data, secrets_data=None, **_extra) -> T:
            od = import_onedata_io()
            stage = None
            piece_out = None
            run_id = None
            if od is not None:
                input_data, stage = od.stage_inputs(input_data, secrets_data)
                run_id = run_id_for_piece(od, input_data, secrets_data, entry=entry)
            try:
                piece_out = _call_piece(fn, self, input_data, secrets_data, run_id)
            except Exception:
                if od is not None and piece_out is None:
                    od.cleanup_on_error(
                        self.results_path,
                        secrets_data,
                        piece_name,
                        stage,
                        run_id=run_id,
                    )
                raise
            finally:
                if od is not None and piece_out is None and stage is not None:
                    stage.cleanup()
            return finish_or_return(
                od, piece_out, self.results_path, secrets_data, piece_name, stage, run_id
            )

        return wrapper

    return decorator
