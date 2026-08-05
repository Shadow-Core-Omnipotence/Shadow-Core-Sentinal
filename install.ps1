<#
.SYNOPSIS
    Register Shadow-Core Sentinel to start automatically at logon.

.DESCRIPTION
    Creates a Scheduled Task that runs Sentinel when the current user logs on.

    WHY THIS FILE EXISTS
    --------------------
    mcp_config.json states that Sentinel "starts at logon via the 'Shadow Core
    MCP Servers' scheduled task", and SENTINEL.md describes it as an always-on
    service started at logon. No script, task definition or documented procedure
    for that existed anywhere in the repository — the deployment step the
    product's own documentation depends on lived only in undocumented machine
    state, so the build produced an artifact nothing knew how to install.

    NOT ELEVATED, DELIBERATELY
    --------------------------
    The task runs at the user's normal integrity level. A previous incarnation
    ran with highest privileges, which meant a non-elevated `taskkill` returned
    "Access denied" and every rebuild needed Task Manager. Sentinel does not
    need elevation: it reads and hashes files the user can already read, and
    binds a loopback port. Combined with `POST /admin/shutdown`, restarting it
    needs no administrator rights at all.

.PARAMETER TaskName
    Scheduled task name. Default "Shadow-Core Sentinel".

.PARAMETER Exe
    Path to a built shadow-core-sentinel.exe. If omitted, the script uses
    dist\shadow-core-sentinel.exe when present, otherwise runs main.py with the
    interpreter given by -Python.

.PARAMETER Python
    Interpreter to use when running from source. Default: .venv311\Scripts\pythonw.exe
    if present, else pythonw.exe from PATH.

.PARAMETER AuditDir
    Where audit data is written. Default: <repo>\audit_logs

.PARAMETER McpPort
    MCP SSE port. Default 7702.

.PARAMETER DashboardPort
    Dashboard port. Default 7654.

.PARAMETER Force
    Replace an existing task with the same name without prompting.

.EXAMPLE
    .\install.ps1
    Install using the built exe if present, otherwise source.

.EXAMPLE
    .\install.ps1 -Force -McpPort 7702
    Reinstall, replacing any existing task.
#>
[CmdletBinding()]
param(
    [string] $TaskName = "Shadow-Core Sentinel",
    [string] $Exe,
    [string] $Python,
    [string] $AuditDir,
    [int]    $McpPort = 7702,
    [int]    $DashboardPort = 7654,
    [switch] $Force
)

$ErrorActionPreference = "Stop"
$repo = Split-Path -Parent $MyInvocation.MyCommand.Path

function Write-Step { param([string] $m) Write-Host "  $m" }

Write-Host ""
Write-Host "Shadow-Core Sentinel - install" -ForegroundColor Cyan
Write-Host "==============================="
Write-Host "  repo : $repo"

# ── Decide what gets launched ────────────────────────────────────────────────
# A frozen exe is preferred when one has been built; source is the fallback so
# the task can be installed from a fresh clone without running PyInstaller.
if (-not $AuditDir) { $AuditDir = Join-Path $repo "audit_logs" }

if (-not $Exe) {
    $candidate = Join-Path $repo "dist\shadow-core-sentinel.exe"
    if (Test-Path $candidate) { $Exe = $candidate }
}

if ($Exe) {
    if (-not (Test-Path $Exe)) { throw "Executable not found: $Exe" }
    $action_exe = $Exe
    $action_args = "--mcp-port $McpPort --dashboard-port $DashboardPort"
    Write-Step "mode : frozen exe"
    Write-Step "exe  : $Exe"
}
else {
    if (-not $Python) {
        # pythonw.exe, not python.exe: a console window appearing at every
        # logon for a background service is not acceptable, and Sentinel
        # redirects its own stdout/stderr to a log file regardless.
        $venvw = Join-Path $repo ".venv311\Scripts\pythonw.exe"
        if (Test-Path $venvw) {
            $Python = $venvw
        }
        else {
            $found = Get-Command pythonw.exe -ErrorAction SilentlyContinue
            if (-not $found) {
                throw "No interpreter found. Build the exe, or pass -Python <path to pythonw.exe>."
            }
            $Python = $found.Source
        }
    }
    if (-not (Test-Path $Python)) { throw "Interpreter not found: $Python" }

    $mainPy = Join-Path $repo "main.py"
    if (-not (Test-Path $mainPy)) { throw "main.py not found in $repo" }

    $action_exe = $Python
    $action_args = "`"$mainPy`" --mcp-port $McpPort --dashboard-port $DashboardPort"
    Write-Step "mode : source"
    Write-Step "py   : $Python"
}

Write-Step "audit: $AuditDir"
Write-Step "ports: mcp $McpPort / dashboard $DashboardPort"

# ── Refuse to install over a port that is already serving ────────────────────
# Two Sentinels on one port means the second fails to bind and exits, and the
# task then looks installed while nothing new is running.
$busy = Get-NetTCPConnection -LocalPort $McpPort -State Listen -ErrorAction SilentlyContinue
if ($busy) {
    Write-Host ""
    Write-Host "  WARNING: port $McpPort is already listening." -ForegroundColor Yellow
    Write-Host "  If that is a running Sentinel, stop it first:" -ForegroundColor Yellow
    Write-Host "      Invoke-RestMethod -Method POST http://127.0.0.1:$McpPort/admin/shutdown"
    Write-Host ""
}

# ── Existing task ────────────────────────────────────────────────────────────
$existing = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if ($existing) {
    if (-not $Force) {
        $reply = Read-Host "  Task '$TaskName' already exists. Replace it? [y/N]"
        if ($reply -notmatch '^(y|yes)$') {
            Write-Host "  Aborted; nothing changed." -ForegroundColor Yellow
            return
        }
    }
    Write-Step "removing existing task"
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
}

# ── Register ─────────────────────────────────────────────────────────────────
# WorkingDirectory matters: AUDIT_DIR and WATCH_DIR default to relative paths,
# so a task started elsewhere would write its audit trail somewhere unexpected.
$action = New-ScheduledTaskAction -Execute $action_exe `
                                  -Argument $action_args `
                                  -WorkingDirectory $repo

$trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME

# Limited, not Highest — see the header. RunOnlyIfNetworkAvailable is off
# because Sentinel is loopback-only and must come up without a network.
$principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" `
                                        -LogonType Interactive `
                                        -RunLevel Limited

$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -RestartCount 3 `
    -RestartInterval (New-TimeSpan -Minutes 1) `
    -ExecutionTimeLimit (New-TimeSpan -Seconds 0)   # 0 = never killed for running long

Write-Step "registering task '$TaskName'"
Register-ScheduledTask -TaskName $TaskName `
                       -Action $action `
                       -Trigger $trigger `
                       -Principal $principal `
                       -Settings $settings `
                       -Description "Shadow-Core Sentinel - filesystem audit trail served over MCP (SSE on 127.0.0.1:$McpPort). Boots watching nothing until a session calls watch_project." | Out-Null

Write-Host ""
Write-Host "  Installed." -ForegroundColor Green
Write-Host ""
Write-Host "  Start it now      : Start-ScheduledTask -TaskName '$TaskName'"
Write-Host "  Check it is up    : curl http://127.0.0.1:$McpPort/health"
Write-Host "  Dashboard         : http://127.0.0.1:$DashboardPort"
Write-Host "  Stop it           : Invoke-RestMethod -Method POST http://127.0.0.1:$McpPort/admin/shutdown"
Write-Host "  Remove            : .\uninstall.ps1"
Write-Host ""
Write-Host "  Sentinel boots watching NOTHING. A session must call watch_project."
Write-Host ""
