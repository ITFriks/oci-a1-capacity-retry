<#
.SYNOPSIS
    OPTIONAL: start 'oci-a1-retry run' at logon with Windows Task Scheduler.

.DESCRIPTION
    Registers a task for the current user only (no administrator rights, no stored
    password). At logon it opens a minimized console window running
    'oci-a1-retry run'; restore it from the taskbar to watch progress, press Ctrl+C
    to stop. After a successful Apply the program exits, and at later logons it
    exits immediately with "Nothing to do".
    Nothing is changed until you confirm (or pass -Force).

.PARAMETER ExePath
    Full path to oci-a1-retry.exe. Default: auto-detected.

.PARAMETER ConfigPath
    Optional config.toml, passed as --config.

.PARAMETER Uninstall
    Remove the scheduled task.

.PARAMETER Force
    Do not ask for confirmation.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File scripts\install-windows-task.ps1

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File scripts\install-windows-task.ps1 -Uninstall
#>
[CmdletBinding()]
param(
    [string]$ExePath,
    [string]$ConfigPath,
    [switch]$Uninstall,
    [switch]$Force
)

$ErrorActionPreference = 'Stop'
$TaskName = 'oci-a1-capacity-retry'
$SkipConfirmation = $Force.IsPresent

function Confirm-Step([string]$Question) {
    if ($SkipConfirmation) { return $true }
    $answer = Read-Host "$Question [y/N]"
    return $answer -match '^[yY]'
}

if ($Uninstall) {
    if (-not (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue)) {
        Write-Host "No scheduled task named '$TaskName'."
        exit 0
    }
    if (Confirm-Step "Remove scheduled task '$TaskName'?") {
        Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
        Write-Host 'Removed. A program instance that is already running keeps running.'
    }
    exit 0
}

if (-not $ExePath) {
    $found = Get-Command oci-a1-retry -ErrorAction SilentlyContinue
    $candidates = @(
        $(if ($found) { $found.Source }),
        (Join-Path $env:LOCALAPPDATA 'oci-a1-capacity-retry\venv\Scripts\oci-a1-retry.exe'),
        (Join-Path $HOME '.local\bin\oci-a1-retry.exe')
    )
    $ExePath = $candidates | Where-Object { $_ -and (Test-Path $_) } | Select-Object -First 1
}
if (-not $ExePath -or -not (Test-Path $ExePath)) {
    Write-Host 'oci-a1-retry.exe not found. Run scripts\install.ps1 first or pass -ExePath.' -ForegroundColor Red
    exit 1
}
if ($ConfigPath -and -not (Test-Path $ConfigPath)) {
    Write-Host "Config file not found: $ConfigPath" -ForegroundColor Red
    exit 1
}

# 'start /min' opens the program in its own minimized console window; Task Scheduler
# only launches it, so no task time limit can kill the long-running retry loop.
$programArgs = 'run'
if ($ConfigPath) { $programArgs = "run --config `"$ConfigPath`"" }
$cmdArgs = "/c start `"oci-a1-retry`" /min `"$ExePath`" $programArgs"
$user = "$env:USERDOMAIN\$env:USERNAME"

Write-Host 'This will register a Task Scheduler task:'
Write-Host "  Name:    $TaskName"
Write-Host "  Trigger: at logon of $user"
Write-Host "  Runs:    cmd.exe $cmdArgs"
Write-Host '  Rights:  your normal user rights (not elevated), only while you are logged on'
if (-not (Confirm-Step 'Register it?')) {
    Write-Host 'Nothing changed.'
    exit 0
}

$action = New-ScheduledTaskAction -Execute 'cmd.exe' -Argument $cmdArgs
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $user
$principal = New-ScheduledTaskPrincipal -UserId $user -LogonType Interactive -RunLevel Limited
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -StartWhenAvailable -ExecutionTimeLimit (New-TimeSpan -Minutes 5)
try {
    Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger `
        -Principal $principal -Settings $settings -Force `
        -Description 'Re-submits an OCI Resource Manager Apply job until A1 capacity is available.' | Out-Null
}
catch {
    Write-Host "Registering the task failed: $($_.Exception.Message)" -ForegroundColor Red
    Write-Host 'Alternative without Task Scheduler: press Win+R, open shell:startup and create'
    Write-Host 'a shortcut there with this target:'
    Write-Host "  cmd.exe $cmdArgs"
    exit 1
}
Write-Host "Registered. It starts at your next logon. Start it now with:"
Write-Host "  Start-ScheduledTask -TaskName $TaskName"
Write-Host 'Remove it with:'
Write-Host '  powershell -ExecutionPolicy Bypass -File scripts\install-windows-task.ps1 -Uninstall'
