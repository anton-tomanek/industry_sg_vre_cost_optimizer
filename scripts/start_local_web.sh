#!/usr/bin/env bash
# Spustí vstupnú webovú stránku a otvorí ju v prehliadači.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
if ! command -v python3 >/dev/null 2>&1; then
  echo "python3 nie je v PATH" >&2
  exit 1
fi
if [[ ! -f examples/demo_site/load_and_prices.csv ]]; then
  python3 scripts/make_demo_inputs.py
fi
exec python3 scripts/start_local_web.py "$@"
