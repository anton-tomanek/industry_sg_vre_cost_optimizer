#!/usr/bin/env bash
# Dvojklik na Macu — používa už nainštalovaný Python 3.
cd "$(dirname "$0")"
export UC32_LOCAL_MODE=1
export ONEDATA_DISABLE=1
export PYTHONUTF8=1

if ! command -v python3 >/dev/null 2>&1; then
  echo "python3 nie je v PATH."
  read -r -p "Enter pre zatvorenie..."
  exit 1
fi

echo "UC3.2 SEED — http://127.0.0.1:8088/"
echo "Okno nechajte otvorené. Zastavenie: Ctrl+C"
echo
python3 scripts/start_local_web.py "$@"
status=$?
if [[ $status -ne 0 ]]; then
  echo
  echo "Spustenie zlyhalo (exit $status)."
  read -r -p "Enter pre zatvorenie..."
fi
exit $status
