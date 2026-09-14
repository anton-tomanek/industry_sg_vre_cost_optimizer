"""Optional OneData (fsspec) I/O layer for boundary pieces.

Design goals (do NOT break the existing local/Domino workflow):

* When no OneData secret is configured AND the path is a plain local path,
  every helper behaves exactly like the current ``pathlib`` / ``pandas`` code.
* When OneData secrets are configured AND the path carries a protocol
  (e.g. ``onedata:///space/file.csv``), the same helper transparently routes
  through ``fsspec`` + the ``onedatarestfsspec`` backend.
* ``fsspec`` is imported lazily, so a local run without the OneData backend
  installed still works as long as only local paths are used.

Secrets are passed to a piece by Domino as ``secrets_data`` and locally as
``None`` (the orchestrator never sets them), which keeps the local path active.
"""
from __future__ import annotations

import json
import os
import shutil
from pathlib import Path, PurePosixPath
from typing import Any

import pandas as pd

# Protocols we treat as "remote" (handled by fsspec). A plain Windows path
# like ``C:\...`` is NOT a protocol here: ``url_to_fs`` would misread the
# drive letter, so we explicitly only route known remote schemes.
_REMOTE_PROTOCOLS = ("onedata://",)

# Input fields that may point at OneData output locations (created by the piece run).
_STAGE_SKIP_FIELDS = frozenset({"output_dir"})

from .onedata_defaults import (
    DEFAULT_INPUT_DIR,
    DEFAULT_ONEDATA_TOKEN,
    DEFAULT_ONEZONE_HOST,
    DEFAULT_OUTPUT_DIR,
)

DEFAULT_TOKEN_FILE = "/run/secrets/onedata_token"


def normalize_remote_path(path: str | os.PathLike[str]) -> str:
    """Normalize Domino/UI variants like ``onedata:/space/file`` → ``onedata:///space/file``."""
    text = str(path).strip()
    if text.startswith("onedata:/") and not text.startswith("onedata:///"):
        return "onedata:///" + text[len("onedata:/") :].lstrip("/")
    return text


def has_protocol(path: str | os.PathLike[str]) -> bool:
    """True only for paths that should be handled by fsspec (e.g. onedata://)."""
    return normalize_remote_path(path).startswith(_REMOTE_PROTOCOLS)


_backend_ready = False
_onedata_configured = False


def onedata_configured() -> bool:
    return _onedata_configured


def _require_onedata_for(path: str | os.PathLike[str]) -> None:
    """Raise a clear error when a remote path is used without credentials."""
    if has_protocol(path) and not _onedata_configured:
        raise ValueError(
            "OneData path requires workflow secrets: onedata_onezone_host, "
            "onedata_token (and onedata_output_dir for outputs). "
            f"Got path: {normalize_remote_path(path)}"
        )


def _ensure_backend() -> None:
    """Import the vendored onedata fsspec backend so the ``onedata`` protocol is
    registered with fsspec.

    The backend (``onedatarestfsspec``) is vendored next to this module under
    ``common/onedatarestfsspec`` because it is git-only (not on PyPI) and the
    Domino base image has no git to build it. Importing it runs
    ``register_implementation('onedata', ...)`` at module load. Idempotent.
    """
    global _backend_ready
    if _backend_ready:
        return
    try:
        from . import onedatarestfsspec  # noqa: F401  (vendored, self-registers)
    except Exception:
        import sys
        here = os.path.dirname(os.path.abspath(__file__))
        if here not in sys.path:
            sys.path.insert(0, here)
        import onedatarestfsspec  # noqa: F401
    _backend_ready = True


def _resolve_token() -> str | None:
    token = os.environ.get("ONEDATA_TOKEN")
    if token:
        return str(token).strip() or None
    token_file = os.environ.get("ONEDATA_TOKEN_FILE", DEFAULT_TOKEN_FILE)
    try:
        text = Path(token_file).read_text(encoding="utf-8").strip()
        return text or None
    except OSError:
        return None


def effective_secrets(secrets_data: Any, *, use_defaults: bool = False) -> dict[str, str | None]:
    """Merge Domino secrets, env vars, and (optionally) production defaults."""
    host = _get(secrets_data, "onedata_onezone_host") or os.environ.get("ONEDATA_ONEZONE_HOST")
    token = _get(secrets_data, "onedata_token")
    if not token and use_defaults:
        token = _resolve_token() or DEFAULT_ONEDATA_TOKEN
    if use_defaults and not host:
        host = DEFAULT_ONEZONE_HOST
    output = _get(secrets_data, "onedata_output_dir")
    if isinstance(output, str) and not output.strip():
        output = ""
    else:
        output = (str(output).strip() if output else None) or os.environ.get("ONEDATA_OUTPUT_BASE")
        if use_defaults and not output:
            output = DEFAULT_OUTPUT_DIR
    return {
        "onedata_onezone_host": host,
        "onedata_token": token,
        "onedata_output_dir": output,
    }


def configure_onedata(secrets_data: Any, *, force: bool = False) -> bool:
    """Register OneData credentials and the backend if both are present.

    Accepts a pydantic model, a dict, or ``None``. Returns ``True`` when the
    OneData backend was configured, ``False`` otherwise (local-only run).
    Falls back to environment variables (used by the onedata pytest jobs).
    """
    global _onedata_configured
    merged = effective_secrets(secrets_data, use_defaults=force)
    host = merged.get("onedata_onezone_host")
    token = merged.get("onedata_token")
    if not host or not token:
        _onedata_configured = False
        return False

    # The vendored backend reads credentials from these env vars
    # (see onedatarestfsspec.config.get_onedata_config_from_env), so set them
    # before any onedata:// filesystem is created.
    os.environ["ONEDATA_ONEZONE_HOST"] = str(host)
    os.environ["ONEDATA_TOKEN"] = str(token)

    _ensure_backend()
    _onedata_configured = True
    return True


def _get(obj: Any, name: str) -> Any:
    if obj is None:
        return None
    if isinstance(obj, dict):
        return obj.get(name)
    return getattr(obj, name, None)


def _fs(path: str):
    import fsspec

    _ensure_backend()
    filesystem, fs_path = fsspec.core.url_to_fs(path)
    return filesystem, fs_path


# --- existence / listing -------------------------------------------------

def exists(path: str | os.PathLike[str]) -> bool:
    path = normalize_remote_path(path)
    if has_protocol(path):
        _require_onedata_for(path)
        fs, p = _fs(str(path))
        return fs.exists(p)
    return Path(path).exists()


def isfile(path: str | os.PathLike[str]) -> bool:
    path = normalize_remote_path(path)
    if has_protocol(path):
        _require_onedata_for(path)
        fs, p = _fs(str(path))
        return fs.isfile(p)
    return Path(path).is_file()


def isdir(path: str | os.PathLike[str]) -> bool:
    path = normalize_remote_path(path)
    if has_protocol(path):
        _require_onedata_for(path)
        fs, p = _fs(str(path))
        return fs.isdir(p)
    return Path(path).is_dir()


def glob(directory: str | os.PathLike[str], pattern: str) -> list[str]:
    """List entries matching ``pattern`` inside ``directory``.

    Returns full paths (URLs for remote). Sorted for deterministic order,
    mirroring the existing ``sorted(Path(dir).glob(pattern))`` usage.
    """
    if has_protocol(directory):
        import fsspec

        proto, _ = fsspec.core.split_protocol(str(directory))
        fs, p = _fs(str(directory))
        matches = fs.glob(f"{p.rstrip('/')}/{pattern}")
        return sorted(f"{proto}:///{m.lstrip('/')}" for m in matches)
    return sorted(str(p) for p in Path(directory).glob(pattern))


def makedirs(path: str | os.PathLike[str], exist_ok: bool = True) -> None:
    if has_protocol(path):
        fs, p = _fs(str(path))
        fs.makedirs(p, exist_ok=exist_ok)
        return
    Path(path).mkdir(parents=True, exist_ok=exist_ok)


def ensure_parent_dir(path: str | os.PathLike[str]) -> None:
    if has_protocol(path):
        import fsspec

        fs, p = _fs(str(path))
        parent = str(PurePosixPath(p).parent)
        if parent not in ("", ".", "/"):
            fs.makedirs(parent, exist_ok=True)
        return
    Path(path).parent.mkdir(parents=True, exist_ok=True)


def _prepare_remote_write(path: str | os.PathLike[str]) -> None:
    """Ensure the parent dir exists and remove any existing file at ``path``.

    The OneData REST backend refuses to create a file that already exists
    (HTTP 400 / POSIX ``eexist``), so writes are not idempotent unless we delete
    the target first. This makes re-running the workflow safe (overwrite).
    """
    ensure_parent_dir(path)
    try:
        fs, p = _fs(str(path))
        if fs.exists(p):
            fs.rm(p)
    except Exception:
        pass


def move(src: str | os.PathLike[str], dst: str | os.PathLike[str]) -> None:
    """Move/rename a file. Both paths must be on the same filesystem family."""
    if has_protocol(src) or has_protocol(dst):
        import fsspec

        fs, src_p = _fs(str(src))
        _, dst_p = fsspec.core.url_to_fs(str(dst))
        _prepare_remote_write(dst)
        fs.mv(src_p, dst_p)
        return
    Path(src).rename(dst)


# --- text / bytes --------------------------------------------------------

def read_text(path: str | os.PathLike[str], encoding: str = "utf-8") -> str:
    if has_protocol(path):
        import fsspec

        with fsspec.open(str(path), "r", encoding=encoding) as f:
            return f.read()
    return Path(path).read_text(encoding=encoding)


def write_text(path: str | os.PathLike[str], text: str, encoding: str = "utf-8") -> None:
    if has_protocol(path):
        import fsspec

        _prepare_remote_write(path)
        with fsspec.open(str(path), "w", encoding=encoding) as f:
            f.write(text)
        return
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(text, encoding=encoding)


# --- pandas IO -----------------------------------------------------------
# pandas natively routes URLs through fsspec once the onedata backend is
# configured, so these thin wrappers work for both local and remote paths.

def read_csv(path: str | os.PathLike[str], **kwargs) -> pd.DataFrame:
    return pd.read_csv(str(path), **kwargs)


def read_parquet(path: str | os.PathLike[str], **kwargs) -> pd.DataFrame:
    return pd.read_parquet(str(path), **kwargs)


def to_csv(df: pd.DataFrame, path: str | os.PathLike[str], **kwargs) -> None:
    if not has_protocol(path):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
    else:
        _prepare_remote_write(path)
    df.to_csv(str(path), **kwargs)


def to_parquet(df: pd.DataFrame, path: str | os.PathLike[str], **kwargs) -> None:
    if not has_protocol(path):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
    else:
        _prepare_remote_write(path)
    df.to_parquet(str(path), **kwargs)


# --- bytes ---------------------------------------------------------------

def read_bytes(path: str | os.PathLike[str]) -> bytes:
    if has_protocol(path):
        import fsspec

        with fsspec.open(str(path), "rb") as f:
            return f.read()
    return Path(path).read_bytes()


def write_bytes(path: str | os.PathLike[str], data: bytes) -> None:
    if has_protocol(path):
        import fsspec

        _prepare_remote_write(path)
        with fsspec.open(str(path), "wb") as f:
            f.write(data)
        return
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_bytes(data)


# --- json ----------------------------------------------------------------

def read_json(path: str | os.PathLike[str]) -> Any:
    return json.loads(read_text(path))


def write_json(path: str | os.PathLike[str], obj: Any, *, indent: int = 2,
               ensure_ascii: bool = False) -> None:
    write_text(path, json.dumps(obj, indent=indent, ensure_ascii=ensure_ascii))


# --- joblib (model pickles) ---------------------------------------------

def joblib_dump(obj: Any, path: str | os.PathLike[str], **kwargs) -> None:
    import joblib

    if has_protocol(path):
        import fsspec

        _prepare_remote_write(path)
        with fsspec.open(str(path), "wb") as f:
            joblib.dump(obj, f, **kwargs)
        return
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    joblib.dump(obj, str(path), **kwargs)


def joblib_load(path: str | os.PathLike[str]) -> Any:
    import joblib

    if has_protocol(path):
        import fsspec

        with fsspec.open(str(path), "rb") as f:
            return joblib.load(f)
    return joblib.load(str(path))


# --- copy / remove / listdir --------------------------------------------

def copy(src: str | os.PathLike[str], dst: str | os.PathLike[str]) -> None:
    if has_protocol(src) or has_protocol(dst):
        write_bytes(dst, read_bytes(src))
        return
    Path(dst).parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(str(src), str(dst))


def remove(path: str | os.PathLike[str], missing_ok: bool = True) -> None:
    if has_protocol(path):
        fs, p = _fs(str(path))
        if fs.exists(p):
            fs.rm(p)
        elif not missing_ok:
            raise FileNotFoundError(path)
        return
    pp = Path(path)
    if pp.exists():
        pp.unlink()
    elif not missing_ok:
        raise FileNotFoundError(path)


def listdir(path: str | os.PathLike[str]) -> list[str]:
    """Return full child paths (URLs for remote), sorted."""
    if has_protocol(path):
        import fsspec

        proto, _ = fsspec.core.split_protocol(str(path))
        fs, p = _fs(str(path))
        return sorted(f"{proto}:///{m.lstrip('/')}" for m in fs.ls(p, detail=False))
    return sorted(str(x) for x in Path(path).iterdir())


# --- whole-workflow OneData staging -------------------------------------
# These two helpers let EVERY piece run fully through OneData WITHOUT touching
# its internal I/O: inputs are downloaded to a local temp before the piece runs
# (so the unchanged piece logic works on local files), and the piece's outputs
# are uploaded from results_path to OneData afterwards
# (default ``onedata:///SCDI/UC3.2_COST_OPTIMIZER/outputs``).

def _remote_name(path: str) -> str:
    fs, p = _fs(str(path))
    return PurePosixPath(p).name


def _output_base(secrets_data: Any) -> str | None:
    raw = _get(secrets_data, "onedata_output_dir")
    if isinstance(raw, str) and not raw.strip():
        return None
    val = (str(raw).strip() if raw else None) or os.environ.get("ONEDATA_OUTPUT_BASE") or DEFAULT_OUTPUT_DIR
    return str(val).strip() or None


def resolve_run_id(input_data: Any, secrets_data: Any, *, generate: bool = False) -> str | None:
    """Per-workflow run folder under ``onedata_output_dir`` (e.g. ``.../outputs/<run_id>/``)."""
    rid = _get(input_data, "run_id")
    if rid and str(rid).strip():
        return str(rid).strip()
    rid = _get(secrets_data, "onedata_run_id") or os.environ.get("ONEDATA_RUN_ID")
    if rid and str(rid).strip():
        return str(rid).strip()
    if generate:
        import uuid
        return uuid.uuid4().hex[:12]
    return None


def _mirror_base(secrets_data: Any, run_id: str | None) -> str | None:
    base = _output_base(secrets_data)
    if not base:
        return None
    if run_id:
        return f"{base.rstrip('/')}/{run_id}"
    return base


class StageHandle:
    """Holds temp dirs created while staging inputs; call cleanup() when done."""

    def __init__(self) -> None:
        self.tmpdirs: list[str] = []
        self.active: bool = False
        self.local_to_remote: dict[str, str] = {}

    def cleanup(self) -> None:
        for d in self.tmpdirs:
            shutil.rmtree(d, ignore_errors=True)
        self.tmpdirs.clear()
        self.local_to_remote.clear()


def _collect_remote_paths(val: Any, *, skip_keys: set[str] | None = None) -> list[str]:
    skip = skip_keys or _STAGE_SKIP_FIELDS
    found: list[str] = []
    if isinstance(val, str):
        text = normalize_remote_path(val)
        if has_protocol(text):
            found.append(text)
    elif isinstance(val, dict):
        for key, item in val.items():
            if key in skip:
                continue
            found.extend(_collect_remote_paths(item, skip_keys=skip))
    elif isinstance(val, list):
        for item in val:
            found.extend(_collect_remote_paths(item, skip_keys=skip))
    return found


def _download_remote(val: str, stage: StageHandle, field: str) -> str:
    import tempfile

    if isdir(val):
        tmp = tempfile.mkdtemp(prefix="od_in_")
        stage.tmpdirs.append(tmp)
        local_dir = os.path.join(tmp, _remote_name(val) or "dir")
        os.makedirs(local_dir, exist_ok=True)
        for entry in listdir(val):
            if isfile(entry):
                write_bytes(os.path.join(local_dir, _remote_name(entry)), read_bytes(entry))
        local = os.path.abspath(local_dir)
        stage.local_to_remote[local.replace("\\", "/")] = val.rstrip("/")
        return local
    if isfile(val):
        tmp = tempfile.mkdtemp(prefix="od_in_")
        stage.tmpdirs.append(tmp)
        local_f = os.path.join(tmp, _remote_name(val))
        write_bytes(local_f, read_bytes(val))
        local = os.path.abspath(local_f)
        stage.local_to_remote[local.replace("\\", "/")] = val
        return local
    raise FileNotFoundError(
        f"OneData input missing for '{field}': {val}. "
        "Upstream piece may not have mirrored output; check DAG edges."
    )


def _stage_tree(val: Any, stage: StageHandle, *, key: str | None = None) -> Any:
    if isinstance(val, str):
        if key in _STAGE_SKIP_FIELDS:
            return val
        text = normalize_remote_path(val)
        if not has_protocol(text):
            return val
        return _download_remote(text, stage, key or "path")
    if isinstance(val, dict):
        return {k: _stage_tree(v, stage, key=k) for k, v in val.items()}
    if isinstance(val, list):
        return [_stage_tree(item, stage, key=key) for item in val]
    return val


def _restore_tree(val: Any, mapping: dict[str, str]) -> Any:
    if isinstance(val, str):
        norm = os.path.abspath(val).replace("\\", "/") if not has_protocol(val) else val
        if has_protocol(val):
            return val
        for local, remote in mapping.items():
            local_n = local.replace("\\", "/").rstrip("/")
            if norm == local_n:
                return remote
            prefix = local_n + "/"
            if norm.startswith(prefix):
                rel = norm[len(prefix) :]
                return f"{remote.rstrip('/')}/{rel}"
        return val
    if isinstance(val, dict):
        return {k: _restore_tree(v, mapping) for k, v in val.items()}
    if isinstance(val, list):
        return [_restore_tree(item, mapping) for item in val]
    return val


def restore_staged_paths(output: Any, stage: StageHandle | None) -> Any:
    """Map staged local temp paths in outputs back to the original OneData URLs."""
    if output is None or stage is None or not stage.local_to_remote:
        return output
    dumped = None
    try:
        dumped = output.model_dump()
    except Exception:
        return output
    restored = _restore_tree(dumped, stage.local_to_remote)
    if restored == dumped:
        return output
    try:
        return type(output).model_validate(restored)
    except Exception:
        try:
            return output.model_copy(update=restored)
        except Exception:
            return output


def stage_inputs(input_data: Any, secrets_data: Any):
    """Download any ``onedata://`` input fields to a local temp dir.

    Recurses into nested dicts/lists (payloads, ModelSpec bundles). Returns
    ``(input_data, StageHandle)``. No-op when there are no remote paths.
    """
    stage = StageHandle()
    try:
        values = input_data.model_dump()
    except Exception:
        return input_data, stage

    remote_paths = _collect_remote_paths(values)
    if not remote_paths:
        return input_data, stage
    if not configure_onedata(secrets_data, force=True):
        eff = effective_secrets(secrets_data, use_defaults=True)
        raise ValueError(
            "OneData input paths require onedata_token. "
            f"Host/output use defaults ({eff.get('onedata_onezone_host')}, "
            f"{eff.get('onedata_output_dir')}). "
            "Set env ONEDATA_TOKEN or mount token at ONEDATA_TOKEN_FILE "
            f"(default {DEFAULT_TOKEN_FILE}). "
            f"Paths: {', '.join(remote_paths[:8])}"
        )

    try:
        staged = _stage_tree(values, stage)
    except Exception as exc:
        host = (effective_secrets(secrets_data, use_defaults=True) or {}).get(
            "onedata_onezone_host", DEFAULT_ONEZONE_HOST
        )
        raise RuntimeError(
            f"Failed to download OneData input ({exc}). "
            f"Host {host} must be reachable from the piece container."
        ) from exc

    if staged != values:
        stage.active = True
        try:
            input_data = type(input_data).model_validate(staged)
        except Exception:
            try:
                input_data = input_data.model_copy(update=staged)
            except Exception:
                for key, item in staged.items():
                    try:
                        setattr(input_data, key, item)
                    except Exception:
                        object.__setattr__(input_data, key, item)
    return input_data, stage


def stage_registry(input_data: Any, field: str, secrets_data: Any):
    """Round-trip an ``onedata://`` directory field used for read+write (e.g. a
    per-department model registry).

    Downloads any existing files to a local temp dir, repoints ``field`` at it and
    returns ``(input_data, local_dir, onedata_target)``. After the piece runs, call
    ``upload_registry(local_dir, onedata_target)`` to persist new/updated files.
    Returns ``(input_data, None, None)`` for local paths or when not configured.
    """
    import tempfile

    if not configure_onedata(secrets_data, force=True):
        return input_data, None, None
    val = str(_get(input_data, field) or "")
    if not has_protocol(val):
        return input_data, None, None
    local_dir = tempfile.mkdtemp(prefix="od_reg_")
    try:
        if isdir(val):
            for entry in listdir(val):
                if isfile(entry):
                    write_bytes(os.path.join(local_dir, _remote_name(entry)), read_bytes(entry))
    except Exception:
        pass
    try:
        input_data = input_data.model_copy(update={field: local_dir})
    except Exception:
        try:
            setattr(input_data, field, local_dir)
        except Exception:
            object.__setattr__(input_data, field, local_dir)
    return input_data, local_dir, val


def upload_registry(local_dir: str | None, onedata_target: str | None) -> None:
    """Upload all files in a local registry dir back to its ``onedata://`` target."""
    if not local_dir or not onedata_target:
        return
    for fn in os.listdir(local_dir):
        fp = os.path.join(local_dir, fn)
        if os.path.isfile(fp):
            write_bytes(onedata_target.rstrip("/") + "/" + fn, Path(fp).read_bytes())


def fetch_sibling(orig_remote: str | os.PathLike[str],
                  local_path: str | os.PathLike[str],
                  sibling_name: str) -> None:
    """If ``orig_remote`` is an ``onedata://`` file, download a sibling file (e.g.
    a model's ``shift_profile.json``) next to the already-staged ``local_path``.

    No-op for local paths or when the sibling does not exist remotely.
    """
    orig = str(orig_remote)
    if not has_protocol(orig):
        return
    remote_sibling = orig.rsplit("/", 1)[0] + "/" + sibling_name
    try:
        if exists(remote_sibling):
            dst = os.path.join(os.path.dirname(str(local_path)), sibling_name)
            write_bytes(dst, read_bytes(remote_sibling))
    except Exception:
        pass


def mirror_results(results_path: str | os.PathLike[str], secrets_data: Any,
                   piece_name: str, *, run_id: str | None = None) -> str | None:
    """Upload every file under ``results_path`` to ``<base>/<run_id>/<piece_name>/``.

    ``base`` comes from the ``onedata_output_dir`` secret, ``ONEDATA_OUTPUT_BASE``,
    or ``DEFAULT_OUTPUT_DIR``. An empty secret keeps results on local Domino storage.
    When ``run_id`` is set, outputs are isolated per workflow run. Returns the OneData target dir.
    """
    base = _mirror_base(secrets_data, run_id)
    if not base:
        return None
    if not _onedata_configured and not configure_onedata(secrets_data, force=True):
        return None
    rp = Path(str(results_path))
    if not rp.exists():
        return None
    target = f"{base.rstrip('/')}/{piece_name}"
    for f in rp.rglob("*"):
        if f.is_file():
            rel = f.relative_to(rp).as_posix()
            write_bytes(f"{target}/{rel}", f.read_bytes())
    return target


def _rel_under_results(val: str, results_path: str | os.PathLike) -> str | None:
    """Return a posix relative path when ``val`` points inside ``results_path``."""
    if not val:
        return None
    rp = Path(str(results_path)).resolve()
    rp_s = str(rp).replace("\\", "/").rstrip("/")
    val_s = str(val).replace("\\", "/")
    if val_s == rp_s:
        return ""
    if val_s.startswith(rp_s + "/"):
        return val_s[len(rp_s) + 1 :]
    try:
        p = Path(val).resolve()
        if p.is_relative_to(rp):
            return p.relative_to(rp).as_posix()
    except (ValueError, OSError, AttributeError):
        pass
    return None


def _rewrite_tree(val: Any, results_path: str | os.PathLike, base: str) -> Any:
    if isinstance(val, str):
        rel = _rel_under_results(val, results_path)
        if rel is not None:
            return f"{base}/{rel}" if rel else base
        return val
    if isinstance(val, dict):
        return {k: _rewrite_tree(v, results_path, base) for k, v in val.items()}
    if isinstance(val, list):
        return [_rewrite_tree(item, results_path, base) for item in val]
    return val


def rewrite_output_paths(output: Any, results_path: str | os.PathLike,
                         onedata_target: str) -> Any:
    """Rewrite OutputModel path fields from local ``results_path`` to OneData URLs.

    Recurses into nested dicts/lists (``model_spec``, ``artifacts``). Domino
    passes those strings downstream; after mirror-out they must be
    ``onedata:///...`` so the next piece can stage them in.
    """
    if output is None:
        return output
    base = onedata_target.rstrip("/")
    try:
        dumped = output.model_dump()
    except Exception:
        dumped = None
    if dumped is not None:
        rewritten = _rewrite_tree(dumped, results_path, base)
        if rewritten == dumped:
            return output
        try:
            return type(output).model_validate(rewritten)
        except Exception:
            try:
                return output.model_copy(update=rewritten)
            except Exception:
                return output

    updates: dict[str, Any] = {}
    fields = getattr(output, "model_fields", None) or getattr(output, "__fields__", {})
    for name in fields:
        val = getattr(output, name, None)
        new_val = _rewrite_tree(val, results_path, base)
        if new_val != val:
            updates[name] = new_val
    if not updates:
        return output
    if hasattr(output, "model_copy"):
        return output.model_copy(update=updates)
    for k, v in updates.items():
        setattr(output, k, v)
    return output


def finish_piece(output: Any, results_path: str | os.PathLike, secrets_data: Any,
                 piece_name: str, stage: Any = None,
                 *, registry_local: str | None = None,
                 registry_target: str | None = None,
                 run_id: str | None = None) -> Any:
    """Mirror results to OneData, restore pass-through URLs, then clean staging."""
    if registry_local and registry_target:
        upload_registry(registry_local, registry_target)
    target = mirror_results(results_path, secrets_data, piece_name, run_id=run_id)
    if target and output is not None:
        output = rewrite_output_paths(output, results_path, target)
    output = restore_staged_paths(output, stage)
    if stage is not None:
        stage.cleanup()
    return output


def cleanup_on_error(results_path: str | os.PathLike, secrets_data: Any,
                     piece_name: str, stage: Any = None,
                     *, registry_local: str | None = None,
                     registry_target: str | None = None,
                     run_id: str | None = None) -> None:
    """Mirror partial results and release staging after a failed piece run."""
    if registry_local and registry_target:
        try:
            upload_registry(registry_local, registry_target)
        except Exception:
            pass
    try:
        mirror_results(results_path, secrets_data, piece_name, run_id=run_id)
    except Exception:
        pass
    if stage is not None:
        stage.cleanup()
