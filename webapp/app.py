"""Local browser UI: collect inputs, run the DAG, open the dashboard.

    python3 scripts/start_local_web.py

The page is a form. Submit starts the same workflow as ``scripts/run_local.py``
in a background thread. The browser polls ``/api/status`` and, when
DashboardPiece has written its HTML, is sent there.
"""

from __future__ import annotations

import json
import sys
import threading
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote

ROOT = Path(__file__).resolve().parents[1]
WEBAPP = Path(__file__).resolve().parent
SCRIPTS = ROOT / "scripts"
STATIC = WEBAPP / "static"
RUNS = ROOT / ".local_web"
DEFAULT_PORT = 8088

for path in (ROOT, WEBAPP, SCRIPTS):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from scenario_from_form import build_scenario  # noqa: E402
from make_demo_inputs import build_load  # noqa: E402
import yaml  # noqa: E402

_lock = threading.Lock()
_state: dict[str, Any] = {
    "phase": "idle",
    "order": [],
    "pieces": [],
    "current": None,
    "ok": None,
    "dashboard": None,
    "error": None,
    "log": [],
}


def _snapshot() -> dict[str, Any]:
    with _lock:
        return {
            "phase": _state["phase"],
            "order": list(_state["order"]),
            "pieces": list(_state["pieces"]),
            "current": _state["current"],
            "ok": _state["ok"],
            "dashboard": _state["dashboard"],
            "error": _state["error"],
            "log": list(_state["log"]),
        }


def _log(line: str) -> None:
    text = line if line.endswith("\n") else line + "\n"
    with _lock:
        _state["log"].append(text)
        _state["log"] = _state["log"][-200:]


def _event(event: dict[str, Any]) -> None:
    kind = event.get("type")
    with _lock:
        if kind == "start":
            _state["order"] = list(event.get("pieces") or [])
            _state["pieces"] = []
            _state["current"] = None
            _state["log"].append(f"Spúšťam {len(_state['order'])} krokov.\n")
        elif kind == "piece_start":
            _state["current"] = event.get("piece")
        elif kind == "piece_end":
            row = {k: event.get(k) for k in ("piece", "status", "seconds", "message", "error")}
            _state["pieces"].append(row)
            _state["current"] = None
            extra = f" ({row['seconds']}s)" if row.get("seconds") is not None else ""
            detail = row.get("error") or row.get("message") or ""
            _state["log"].append(
                f"{str(row.get('status') or '').upper():<8} {row.get('piece')}{extra}"
                + (f" — {detail}" if detail else "")
                + "\n"
            )
            _state["log"] = _state["log"][-200:]
        elif kind == "done":
            _state["ok"] = bool(event.get("ok"))
            _state["dashboard"] = "/dashboard.html" if event.get("dashboard") else None


def _suffix(filename: str, fallback: str) -> str:
    suffix = Path(filename or "").suffix.lower()
    if suffix in {".csv", ".xlsx", ".xlsm", ".xls"}:
        return suffix
    return fallback


def _write_inputs(
    fields: dict[str, Any], files: dict[str, tuple[str, bytes]]
) -> tuple[Path, Path, str]:
    RUNS.mkdir(parents=True, exist_ok=True)
    run_dir = RUNS / "inputs"
    run_dir.mkdir(parents=True, exist_ok=True)
    scenario_path = run_dir / "scenario.yaml"
    prices_path = ""

    use_demo = str(fields.get("use_demo") or "no") != "no"
    uploaded = files.get("load_csv")
    if not use_demo and uploaded and uploaded[1]:
        load_path = run_dir / f"load_upload{_suffix(uploaded[0], '.csv')}"
        load_path.write_bytes(uploaded[1])
    elif use_demo:
        load_path = run_dir / "load_and_prices.csv"
        demo = ROOT / "examples" / "demo_site" / "load_and_prices.csv"
        if demo.is_file():
            load_path.write_bytes(demo.read_bytes())
        else:
            from datetime import datetime

            build_load(datetime(2024, 1, 1), 365, 15, 20260906).to_csv(load_path, index=False)
    else:
        raise ValueError("Nahrajte súbor odberu, alebo zvoľte demo profil.")

    prices = files.get("prices_csv")
    if prices and prices[1]:
        dest = run_dir / f"prices_upload{_suffix(prices[0], '.csv')}"
        dest.write_bytes(prices[1])
        prices_path = str(dest)

    scenario = build_scenario(fields)
    scenario_path.write_text(
        yaml.safe_dump(scenario, allow_unicode=True, sort_keys=False), encoding="utf-8"
    )
    return load_path, scenario_path, prices_path


def _run_job(fields: dict[str, Any], files: dict[str, tuple[str, bytes]]) -> None:
    try:
        load_path, scenario_path, prices_path = _write_inputs(fields, files)
        _log(f"Odber: {load_path}\n")
        _log(f"Scenár: {scenario_path}\n")
        if prices_path:
            _log(f"Cenník: {prices_path}\n")
        else:
            _log("Cenník: nebol nahratý — ak odber ceny nemá, stiahne sa OKTE DAM.\n")

        from run_local import run_workflow
        import shutil

        out = ROOT / ".local_run"
        if out.exists():
            shutil.rmtree(out)
        summary = run_workflow(
            load_csv=load_path,
            scenario_yaml=scenario_path,
            out=out,
            stop_on_error=True,
            prices_csv=prices_path,
            on_event=_event,
        )
        with _lock:
            _state["phase"] = "done"
            _state["ok"] = summary["ok"]
            _state["dashboard"] = "/dashboard.html" if summary.get("dashboard") else None
            if not summary["ok"]:
                _state["error"] = "Zlyhalo: " + ", ".join(summary.get("failed") or [])
    except Exception as exc:  # noqa: BLE001
        _log(traceback.format_exc())
        with _lock:
            _state["phase"] = "done"
            _state["ok"] = False
            _state["error"] = str(exc)


def _parse_multipart(header: str, body: bytes) -> tuple[dict[str, str], dict[str, tuple[str, bytes]]]:
    boundary = ""
    for part in header.split(";"):
        part = part.strip()
        if part.lower().startswith("boundary="):
            boundary = part.split("=", 1)[1].strip().strip('"')
    if not boundary:
        raise ValueError("multipart without boundary")
    marker = b"--" + boundary.encode()
    fields: dict[str, str] = {}
    files: dict[str, tuple[str, bytes]] = {}
    for chunk in body.split(marker):
        chunk = chunk.strip(b"\r\n")
        if not chunk or chunk == b"--":
            continue
        head, _, data = chunk.partition(b"\r\n\r\n")
        if data.endswith(b"\r\n"):
            data = data[:-2]
        headers = head.decode("utf-8", errors="replace")
        name = filename = ""
        for line in headers.split("\r\n"):
            if line.lower().startswith("content-disposition:"):
                for item in line.split(";"):
                    item = item.strip()
                    if item.startswith("name="):
                        name = item.split("=", 1)[1].strip('"')
                    elif item.startswith("filename="):
                        filename = item.split("=", 1)[1].strip('"')
        if not name:
            continue
        if filename:
            files[name] = (filename, data)
        else:
            fields[name] = data.decode("utf-8", errors="replace")
    return fields, files


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt: str, *args: Any) -> None:
        return

    def _send(self, code: int, body: bytes, content_type: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, code: int, payload: dict[str, Any]) -> None:
        self._send(code, json.dumps(payload, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")

    def do_GET(self) -> None:  # noqa: N802
        path = unquote(self.path.split("?", 1)[0])
        if path in ("/", "/index.html"):
            self._send(200, (STATIC / "index.html").read_bytes(), "text/html; charset=utf-8")
            return
        if path == "/api/status":
            self._json(200, _snapshot())
            return
        if path == "/dashboard.html":
            dash = ROOT / ".local_run" / "DashboardPiece" / "dashboard.html"
            if not dash.is_file():
                self._send(404, "Dashboard ešte nie je hotový.".encode("utf-8"), "text/plain; charset=utf-8")
                return
            html = dash.read_bytes()
            banner = (
                '<div style="position:sticky;top:0;z-index:20;background:#163a56;color:#fff;'
                'padding:8px 16px;font:13px/1.4 sans-serif;display:flex;justify-content:space-between;gap:12px;">'
                '<span>UC3.2. SEED</span>'
                '<a href="/" style="color:#9ad4ef" data-i18n="nav.new">Nový výpočet</a></div>'
            ).encode("utf-8")
            if b"<body>" in html:
                html = html.replace(b"<body>", b"<body>" + banner, 1)
            self._send(200, html, "text/html; charset=utf-8")
            return
        self._send(404, b"Not found", "text/plain")

    def do_POST(self) -> None:  # noqa: N802
        path = unquote(self.path.split("?", 1)[0])
        if path != "/api/run":
            self._send(404, b"Not found", "text/plain")
            return
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else b""
        ctype = self.headers.get("Content-Type") or ""
        try:
            if "multipart/form-data" in ctype:
                fields, files = _parse_multipart(ctype, body)
            else:
                parsed = parse_qs(body.decode("utf-8"), keep_blank_values=True)
                fields = {k: v[-1] for k, v in parsed.items()}
                files = {}
        except Exception as exc:  # noqa: BLE001
            self._json(400, {"error": f"Neplatný formulár: {exc}"})
            return

        with _lock:
            if _state["phase"] == "running":
                self._json(409, {"error": "Výpočet už beží."})
                return
            _state.update(
                {
                    "phase": "running",
                    "order": [],
                    "pieces": [],
                    "current": None,
                    "ok": None,
                    "dashboard": None,
                    "error": None,
                    "log": [],
                }
            )

        thread = threading.Thread(target=_run_job, args=(fields, files), daemon=True)
        thread.start()
        self._json(202, {"ok": True, "phase": "running"})


def serve(port: int = DEFAULT_PORT) -> None:
    httpd = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    print(f"Otvorte http://127.0.0.1:{port}/")
    httpd.serve_forever()
