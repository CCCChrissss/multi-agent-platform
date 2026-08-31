[CmdletBinding(SupportsShouldProcess = $true)]
param()

$ErrorActionPreference = 'Stop'

# Load read-only process/network cmdlets before ShouldProcess propagates the
# caller's -WhatIf preference into module auto-import and prints unrelated
# alias-creation previews on Windows PowerShell 5.1.
$requestedWhatIf = $WhatIfPreference
$WhatIfPreference = $false
try {
    Import-Module CimCmdlets -ErrorAction Stop
    Import-Module NetTCPIP -ErrorAction Stop
} finally {
    $WhatIfPreference = $requestedWhatIf
}

$repoRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..')).Path
$requiredFiles = @('Procfile', 'Procfile.workers', 'pyproject.toml')
foreach ($requiredFile in $requiredFiles) {
    $requiredPath = Join-Path $repoRoot $requiredFile
    if (-not (Test-Path -LiteralPath $requiredPath -PathType Leaf)) {
        throw "Refusing to continue: repository marker not found: $requiredPath"
    }
}

$all = @(Get-CimInstance Win32_Process)
$allowedNames = @(
    'honcho.exe',
    'python.exe',
    'uv.exe',
    'uvicorn.exe',
    'litellm.exe',
    'cmd.exe',
    'ollama.exe',
    'ollama app.exe'
)
$modulePattern = (
    'honcho\.exe.*(?:Procfile|Procfile\.workers)' +
    '|litellm(?:\.exe)?.*gateway[\\/]config\.yaml' +
    '|services\.stt\.server:app' +
    '|services\.notified\.server:app' +
    '|python(?:\.exe)?"?\s+-m\s+agents\.server' +
    '|python(?:\.exe)?"?\s+-m\s+workflows\.event_driven_pipeline'
)

$seeds = @(
    $all | Where-Object {
        $_.Name -in $allowedNames -and
        $_.CommandLine -match $modulePattern
    }
)

# Procfile owns Ollama on the verified Windows path. Only include the process
# that is actually listening on 11434, and only when it identifies as Ollama.
$ollamaListener = Get-NetTCPConnection -State Listen -LocalPort 11434 -ErrorAction SilentlyContinue |
    Select-Object -First 1
if ($ollamaListener) {
    $ollamaProcess = $all |
        Where-Object ProcessId -eq $ollamaListener.OwningProcess |
        Select-Object -First 1
    if (
        $ollamaProcess -and
        $ollamaProcess.Name -match '^ollama(?: app)?\.exe$' -and
        $ollamaProcess.CommandLine -match '\bserve\b'
    ) {
        $seeds += $ollamaProcess
    }
}

$targetIds = [System.Collections.Generic.HashSet[int]]::new()
foreach ($seed in $seeds) {
    [void]$targetIds.Add([int]$seed.ProcessId)
}

# Include child processes such as uv/Python wrappers and Agent Runtime's MCP
# stdio servers.
$queue = @($targetIds)
while ($queue.Count -gt 0) {
    $parentId = [int]$queue[0]
    if ($queue.Count -eq 1) {
        $queue = @()
    } else {
        $queue = @($queue[1..($queue.Count - 1)])
    }

    foreach ($child in @($all | Where-Object ParentProcessId -eq $parentId)) {
        if ($targetIds.Add([int]$child.ProcessId)) {
            $queue += [int]$child.ProcessId
        }
    }
}

# Honcho on Windows can exit while uv/cmd/Python grandchildren remain. Walk
# upward only through known wrapper processes that still belong to this repo.
foreach ($seed in $seeds) {
    $parentId = [int]$seed.ParentProcessId
    while ($parentId -gt 0) {
        $parent = $all |
            Where-Object ProcessId -eq $parentId |
            Select-Object -First 1
        if (-not $parent -or $parent.Name -notin $allowedNames) {
            break
        }

        $belongsToRepo = (
            $parent.CommandLine -like "*$repoRoot*" -or
            $parent.CommandLine -match $modulePattern -or
            $parent.CommandLine -match 'multiprocessing\.spawn'
        )
        if (-not $belongsToRepo) {
            break
        }

        [void]$targetIds.Add([int]$parent.ProcessId)
        $parentId = [int]$parent.ParentProcessId
    }
}

$targets = @(
    $all |
        Where-Object { $targetIds.Contains([int]$_.ProcessId) } |
        Sort-Object ProcessId
)

if ($targets.Count -eq 0) {
    Write-Host 'No matching multi-agent-platform processes were found.'
} else {
    Write-Host 'The following multi-agent-platform processes will be stopped:'
    $targets |
        Select-Object ProcessId, ParentProcessId, Name, CommandLine |
        Format-Table -AutoSize

    if ($PSCmdlet.ShouldProcess(
        "$($targets.Count) multi-agent-platform process(es)",
        'Stop process tree'
    )) {
        $remainingIds = @($targetIds)
        for ($pass = 0; $pass -lt 6 -and $remainingIds.Count -gt 0; $pass++) {
            $snapshot = @(
                Get-CimInstance Win32_Process |
                    Where-Object { $remainingIds -contains [int]$_.ProcessId }
            )
            $parentIds = @($snapshot.ParentProcessId)
            $leaves = @(
                $snapshot |
                    Where-Object { $parentIds -notcontains [int]$_.ProcessId } |
                    Select-Object -ExpandProperty ProcessId
            )
            if ($leaves.Count -eq 0) {
                $leaves = @($snapshot | Select-Object -ExpandProperty ProcessId)
            }

            foreach ($processId in $leaves) {
                Stop-Process -Id $processId -Force -ErrorAction SilentlyContinue
            }
            Start-Sleep -Milliseconds 400
            $remainingIds = @(
                $remainingIds |
                    Where-Object {
                        Get-Process -Id $_ -ErrorAction SilentlyContinue
                    }
            )
        }

        foreach ($processId in $remainingIds) {
            Stop-Process -Id $processId -Force -ErrorAction SilentlyContinue
        }
        Start-Sleep -Seconds 1
    }
}

$rows = foreach ($port in @(5432, 11434, 4000, 8001, 8002, 8003)) {
    $listeners = @(
        Get-NetTCPConnection -State Listen -LocalPort $port -ErrorAction SilentlyContinue
    )
    [pscustomobject]@{
        Port = $port
        Listening = $listeners.Count -gt 0
        PIDs = (($listeners.OwningProcess | Sort-Object -Unique) -join ',')
    }
}

Write-Host 'Listener verification:'
$rows | Format-Table -AutoSize
Write-Host 'Expected after cleanup: 5432=True; 11434/4000/8001/8002/8003=False.'
