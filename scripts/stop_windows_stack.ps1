[CmdletBinding(SupportsShouldProcess = $true)]
param()

# Compatibility entry point: only this checkout's managed groups are targeted.
$devScript = Join-Path $PSScriptRoot 'dev.ps1'
if ($PSCmdlet.ShouldProcess('managed groups recorded in this checkout .run', 'Request stop')) {
    & $devScript stop
} else {
    & $devScript stop -Preview
}
