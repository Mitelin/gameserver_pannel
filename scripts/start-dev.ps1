param(
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$DevArgs
)

$RepoRoot = Split-Path -Parent $PSScriptRoot
$PythonExe = Join-Path $RepoRoot ".venv\Scripts\python.exe"
if (-not (Test-Path $PythonExe)) {
    $PythonExe = "python"
}

& $PythonExe (Join-Path $PSScriptRoot "dev.py") @DevArgs
exit $LASTEXITCODE