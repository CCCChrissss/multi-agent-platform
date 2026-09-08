[CmdletBinding()]
param(
    [Parameter(Position = 0)]
    [ValidateSet('doctor', 'check', 'db-check', 'db-init', 'ollama', 'services', 'workers', 'ui', 'trigger', 'stop', 'status')]
    [string]$Action = 'doctor',
    [ValidateSet('stt_check_notify', 'stt_exclusion_notify')]
    [string]$Workflow = 'stt_check_notify',
    [switch]$Preview
)

$ErrorActionPreference = 'Stop'
$repoPath = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..')).Path
$pythonPath = Join-Path $repoPath '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $pythonPath)) {
    if ($Action -notin @('doctor', 'check', 'status', 'stop')) {
        throw 'Missing .venv. From repository root, run: uv python install 3.11; uv sync --locked'
    }
    # A generic `python` command can be an uv trampoline which automatically
    # creates and synchronizes the project environment. Read-only actions must
    # not mutate a fresh checkout, so resolve an explicit interpreter instead.
    $uvCommand = Get-Command uv -ErrorAction SilentlyContinue
    if (-not $uvCommand) {
        throw 'Missing .venv and uv. Install uv and Python 3.11, then run uv sync --locked.'
    }
    $pythonPath = [string](& $uvCommand.Source python find 3.11 2>$null | Select-Object -First 1)
    $pythonPath = $pythonPath.Trim()
    if ([string]::IsNullOrWhiteSpace($pythonPath) -or -not (Test-Path -LiteralPath $pythonPath -PathType Leaf)) {
        throw 'Python 3.11 not found. Run: uv python install 3.11; uv sync --locked'
    }
}
$env:PYTHONUTF8 = '1'
Push-Location -LiteralPath $repoPath
try {
    if ($Action -eq 'doctor') {
        & $pythonPath -B -m scripts.doctor
    } elseif ($Action -eq 'check') {
        foreach ($module in @('scripts.static_compat_check', 'services.stt.temp_audio_smoke_test', 'scripts.portability_smoke_test')) {
            & $pythonPath -B -m $module
            if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
        }
    } elseif ($Action -in @('db-check', 'db-init')) {
        $databaseAction = if ($Action -eq 'db-init') { 'init' } else { 'check' }
        & $pythonPath -B -m scripts.database_setup $databaseAction
    } else {
        $runnerArgs = @('-B', '-m', 'scripts.dev_runner', $Action, '--workflow', $Workflow)
        if ($Preview) { $runnerArgs += '--preview' }
        & $pythonPath @runnerArgs
    }
    exit $LASTEXITCODE
} finally {
    Pop-Location
}
