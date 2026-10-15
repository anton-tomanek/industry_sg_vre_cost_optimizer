#!/usr/bin/env bash
# Spustí vstupnú webovú stránku a otvorí ju v prehliadači.
# Prvý beh vytvorí .venv a nainštaluje balíky z dependencies/requirements_local.txt.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
export UC32_LOCAL_MODE=1
export ONEDATA_DISABLE=1
export PYTHONUTF8=1
# .venv má byť izolované od iných inštalácií Pythonu.
unset PYTHONPATH PYTHONHOME
if [[ ! -x .venv/bin/python ]]; then
  if ! command -v python3 >/dev/null 2>&1; then
    echo "python3 nie je v PATH" >&2
    exit 1
  fi
  python3 -m venv .venv
  .venv/bin/python -m pip install --upgrade pip
  .venv/bin/python -m pip install -r dependencies/requirements_local.txt
fi
# Python z python.org nemá certifikáty → HTTPS by zlyhal na CERTIFICATE_VERIFY_FAILED.
if [[ -z "${SSL_CERT_FILE:-}" ]]; then
  SSL_CERT_FILE="$(.venv/bin/python -m certifi 2>/dev/null)" && export SSL_CERT_FILE
fi
if [[ ! -f examples/demo_site/load_and_prices.csv ]]; then
  .venv/bin/python scripts/make_demo_inputs.py
fi
exec .venv/bin/python scripts/start_local_web.py "$@"
