# Build dist\UC3.2_SEED\UC3.2_SEED.exe for colleagues (no Python required).
# Run from anywhere:
#   powershell -ExecutionPolicy Bypass -File scripts\build_windows_exe.ps1

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
Set-Location $Root

Write-Host "Repo: $Root" -ForegroundColor Cyan
Write-Host "Installing PyInstaller (if needed)..." -ForegroundColor Gray
python -m pip install --upgrade pip pyinstaller | Out-Host

$Spec = Join-Path $Root "packaging\uc32_seed.spec"
$Dist = Join-Path $Root "dist\UC3.2_SEED"
$Work = Join-Path $Root "build\pyinstaller"

if (Test-Path $Dist) {
    Remove-Item -Recurse -Force $Dist
}
if (Test-Path $Work) {
    Remove-Item -Recurse -Force $Work
}

Write-Host "Building (this can take several minutes)..." -ForegroundColor Cyan
python -m PyInstaller --noconfirm --clean --distpath (Join-Path $Root "dist") --workpath $Work $Spec
if ($LASTEXITCODE -ne 0) {
    throw "PyInstaller failed with exit code $LASTEXITCODE"
}

$Readme = @"
UC3.2 SEED – lokalna Windows aplikacia
======================================

1. Rozbalte cely priecinok UC3.2_SEED (nie len .exe).
2. Dvojklik na UC3.2_SEED.exe
3. Otvorí sa http://127.0.0.1:8088/ vo webovom prehliadaci.
4. Okno konzoly nechajte otvorene, kym pracujete.
5. Zastavenie: zatvorte konzolu alebo Ctrl+C.

Poznamky:
- Netreba instalovat Python.
- Data sa NEukladaju na OneData — iba na PC.
- Kazdy beh vytvori vlastny priecinok: .local_runs\YYYYMMDD_HHMMSS_xxxxxx\
  (vstupy + vysledky + dashboard.html).
- Potrebuje internet (Open-Meteo / SHMU / OKTE podla scenara).
"@
Set-Content -Path (Join-Path $Dist "SPUSTIT.txt") -Value $Readme -Encoding UTF8

Write-Host ""
Write-Host "Hotovo: $Dist\UC3.2_SEED.exe" -ForegroundColor Green
Write-Host "Poslite kolegom cely priecinok dist\UC3.2_SEED (alebo ho zippujte)." -ForegroundColor Green
