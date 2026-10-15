#!/usr/bin/env bash
# Dvojklik na Macu. Prvý beh vytvorí .venv a nainštaluje balíky, ďalšie behy ho len použijú.
cd "$(dirname "$0")"
export UC32_LOCAL_MODE=1
export ONEDATA_DISABLE=1
export PYTHONUTF8=1
# .venv má byť izolované od iných inštalácií Pythonu.
unset PYTHONPATH PYTHONHOME

pause_exit() {
  echo
  read -r -p "Enter pre zatvorenie..."
  exit "$1"
}

if [[ ! -x .venv/bin/python ]]; then
  if ! command -v python3 >/dev/null 2>&1; then
    echo "python3 nie je v PATH."
    pause_exit 1
  fi
  echo "Prvé spustenie: vytváram .venv a inštalujem balíky (niekoľko minút)..."
  python3 -m venv .venv || pause_exit 1
  .venv/bin/python -m pip install --upgrade pip || pause_exit 1
  .venv/bin/python -m pip install -r dependencies/requirements_local.txt || pause_exit 1
fi

# Python z python.org nemá certifikáty → HTTPS (katalóg FVE, Open-Meteo) by zlyhal na CERTIFICATE_VERIFY_FAILED.
if [[ -z "${SSL_CERT_FILE:-}" ]]; then
  SSL_CERT_FILE="$(.venv/bin/python -m certifi 2>/dev/null)" && export SSL_CERT_FILE
fi

echo "UC3.2 SEED — http://127.0.0.1:8088/"
echo "Okno nechajte otvorené. Zastavenie: Ctrl+C"
echo
.venv/bin/python scripts/start_local_web.py "$@"
status=$?
if [[ $status -ne 0 ]]; then
  echo
  echo "Spustenie zlyhalo (exit $status)."
  pause_exit $status
fi
exit $status
