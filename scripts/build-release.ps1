param(
    [string]$OutputDirectory = "dist/releases"
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

function Invoke-Checked {
    param([string[]]$Command)
    & $Command[0] $Command[1..($Command.Length - 1)]
    if ($LASTEXITCODE -ne 0) {
        throw "Command failed with exit code ${LASTEXITCODE}: $($Command -join ' ')"
    }
}

Invoke-Checked @("uv", "sync", "--locked", "--extra", "dev")
Invoke-Checked @("uv", "run", "python", "scripts/check-public-release.py", "--root", $root)
Invoke-Checked @("uv", "run", "ruff", "check", "coderus", "tests", "scripts/check-public-release.py")
Invoke-Checked @("uv", "run", "pytest", "-q")
Invoke-Checked @(
    "uv", "run", "python", "-m", "coderus.release_manifest",
    "--root", $root, "--output", $OutputDirectory
)
