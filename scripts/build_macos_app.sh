#!/usr/bin/env bash
# Build dist/UC3.2_SEED.app for colleagues (no Python required at runtime).
# MUST run on macOS (Intel or Apple Silicon).
#
#   chmod +x scripts/build_macos_app.sh
#   ./scripts/build_macos_app.sh
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

if [[ "$(uname -s)" != "Darwin" ]]; then
  echo "Tento build treba spustit na Macu (teraz: $(uname -s))." >&2
  exit 1
fi

echo "Repo: $ROOT"
echo "Instalujem PyInstaller (ak treba)..."
python3 -m pip install --upgrade pip pyinstaller
# Runtime deps used by the local workflow (same set as Domino piece image).
python3 -m pip install -r dependencies/requirements_0.txt

SPEC="$ROOT/packaging/uc32_seed_macos.spec"
DIST="$ROOT/dist"
WORK="$ROOT/build/pyinstaller_macos"
APP="$DIST/UC3.2_SEED.app"

rm -rf "$APP" "$DIST/UC3.2_SEED" "$WORK"

echo "Building (niekolko minut)..."
python3 -m PyInstaller --noconfirm --clean --distpath "$DIST" --workpath "$WORK" "$SPEC"

README="$DIST/SPUSTIT_MAC.txt"
cat > "$README" <<'EOF'
UC3.2 SEED – lokalna macOS aplikacia
====================================

1. Skopirujte / rozbalte UC3.2_SEED.app (cely balicek).
2. Prvy krat: pravym klikom → Open (Gatekeeper).
3. Dvojklik na UC3.2_SEED.app
4. Otvorí sa http://127.0.0.1:8088/ v prehliadaci.
5. Konzolove okno nechajte otvorene, kym pracujete.
6. Zastavenie: zatvorte okno / Ctrl+C.

Poznamky:
- Netreba instalovat Python.
- Data sa NEukladaju na OneData — iba na Mac.
- Kazdy beh: .local_runs/YYYYMMDD_HHMMSS_xxxxxx/ vedla .app
  (ak Finder blokuje zapis vedla .app, vysledky su v
   ~/Library/Application Support/UC3.2_SEED/.local_runs — pozri konzolu).
- Potrebuje internet (Open-Meteo / SHMU / OKTE).
EOF

echo ""
echo "Hotovo: $APP"
echo "Poslite kolegom UC3.2_SEED.app (alebo zip)."
ls -la "$DIST" | sed -n '1,20p'
