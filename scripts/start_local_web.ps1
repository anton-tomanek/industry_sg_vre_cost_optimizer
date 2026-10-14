<#
.SYNOPSIS
  Spustí vstupnú webovú stránku a otvorí ju v predvolenom prehliadači.

  powershell -ExecutionPolicy Bypass -File scripts\start_local_web.ps1
#>
param(
    [int]$Port = 8088
)
$ErrorActionPreference = "Stop"
$Root = Resolve-Path (Join-Path $PSScriptRoot "..")
Set-Location $Root

function Test-Python {
    param([string]$Exe, [string[]]$Prefix = @())
    try {
        $null = & $Exe @Prefix -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)" 2>$null
        return ($LASTEXITCODE -eq 0)
    } catch {
        return $false
    }
}

$exe = $null
$prefix = @()
if (Test-Python "py" @("-3")) {
    $exe = "py"
    $prefix = @("-3")
} elseif (Test-Python "python") {
    $exe = "python"
} elseif (Test-Python "python3") {
    $exe = "python3"
}

if (-not $exe) {
    throw "Python 3.10+ nie je v PATH. U vás funguje: python scripts\start_local_web.py"
}

if (-not (Test-Path (Join-Path $Root "examples\demo_site\load_and_prices.csv"))) {
    & $exe @prefix scripts\make_demo_inputs.py
}

& $exe @prefix scripts\start_local_web.py --port $Port
