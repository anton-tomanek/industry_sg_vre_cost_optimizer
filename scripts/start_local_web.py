#!/usr/bin/env python3
"""Start the local input page and open it in the default browser.

    python3 scripts/start_local_web.py
    python3 scripts/start_local_web.py --port 8088 --no-browser
"""

from __future__ import annotations

import argparse
import socket
import sys
import threading
import time
import webbrowser
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _free(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.3)
        return sock.connect_ex(("127.0.0.1", port)) != 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8088)
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args()

    sys.path.insert(0, str(ROOT / "webapp"))
    sys.path.insert(0, str(ROOT / "scripts"))
    from app import serve  # noqa: E402

    if not _free(args.port):
        print(f"Port {args.port} je obsadený. Zastavte predchádzajúci beh, alebo použite --port.")
        url = f"http://127.0.0.1:{args.port}/"
        print(f"Skúšam otvoriť existujúcu stránku: {url}")
        if not args.no_browser:
            webbrowser.open(url)
        return 1

    url = f"http://127.0.0.1:{args.port}/"
    if not args.no_browser:
        threading.Thread(target=lambda: (time.sleep(0.6), webbrowser.open(url)), daemon=True).start()
    print(url)
    serve(args.port)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
