<#
.SYNOPSIS
    Remove the Shadow-Core Sentinel logon task.

.DESCRIPTION
    Stops a running Sentinel (gracefully, via its own shutdown endpoint) and
    unregisters the scheduled task created by install.ps1.

    Audit data is NOT touched. The trail is the reason the service exists, and
    an uninstall script is not the place to decide it is expendable — the path
    is printed instead so the choice stays yours.

.PARAMETER TaskName
    Scheduled task name. Default "Shadow-Core Sentinel".

.PARAMETER McpPort
    MCP SSE port, used to request a graceful shutdown. Default 7702.

.PARAMETER Force
    Do not prompt for confirmation.
#>
[CmdletBinding()]
param(
    [string] $TaskName = "Shadow-Core Sentinel",
    [int]    $McpPort = 7702,
    [switch] $Force
)

$ErrorActionPreference = "Stop"
$repo = Split-Path -Parent $MyInvocation.MyCommand.Path

Write-Host ""
Write-Host "Shadow-Core Sentinel - uninstall" -ForegroundColor Cyan
Write-Host "================================="

$task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if (-not $task) {
    Write-Host "  No task named '$TaskName'; nothing to remove." -ForegroundColor Yellow
}
else {
    if (-not $Force) {
        $reply = Read-Host "  Remove scheduled task '$TaskName'? [y/N]"
        if ($reply -notmatch '^(y|yes)$') {
            Write-Host "  Aborted; nothing changed." -ForegroundColor Yellow
            return
        }
    }

    # Ask it to stop itself first. This is the endpoint that exists precisely so
    # stopping Sentinel does not need elevation.
    try {
        Invoke-RestMethod -Method POST -TimeoutSec 5 `
            -Uri "http://127.0.0.1:$McpPort/admin/shutdown" | Out-Null
        Write-Host "  requested graceful shutdown"
        Start-Sleep -Seconds 2
    }
    catch {
        Write-Host "  (not running, or already stopped)"
    }

    try { Stop-ScheduledTask -TaskName $TaskName -ErrorAction Stop } catch {}
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
    Write-Host "  task removed." -ForegroundColor Green
}

$auditDir = Join-Path $repo "audit_logs"
if (Test-Path $auditDir) {
    Write-Host ""
    Write-Host "  Audit data was left in place:"
    Write-Host "      $auditDir"
    Write-Host "  Delete it yourself if you want it gone."
}
Write-Host ""
