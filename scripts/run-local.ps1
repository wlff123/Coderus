$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
$Python = Join-Path $Root ".venv\Scripts\python.exe"

if (-not (Test-Path -LiteralPath $Python)) {
    throw "Virtual environment not found. Run: python -m venv .venv"
}

& $Python -m coderus serve --config (Join-Path $Root "config.yaml") --secrets (Join-Path $Root "secrets.env")
