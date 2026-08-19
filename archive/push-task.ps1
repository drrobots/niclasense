<#
.SYNOPSIS
    Registers (or removes) the scheduled task that pushes this machine's logs to the share.

.DESCRIPTION
    Runs once per capture machine, elevated. The installer does this for you when given
    /ARCHIVE=; this script is what it calls, and what to run by hand on a machine that was
    installed before there was a share to push to.

    As SYSTEM, so it reaches the share as this machine's account -- DOMAIN\THISBOX$ -- and no
    credential is stored anywhere. That account, or a group holding it, is what needs write
    access to the share. It is the same trick the capture service uses to stay LocalSystem.

    On a startup trigger rather than a logon one: a capture machine may have nobody signed in
    for weeks, and the archive has to keep filling regardless. That is the difference between
    this and the dashboard's logon task, which exists to put a window in front of a person.

.PARAMETER ArchiveRoot
    The share to push into. Passed straight to push-logs.ps1.

.PARAMETER Name
    This sensor's folder on the share. Defaults to the machine name.

.PARAMETER EveryMinutes
    How often to push. Minutes of lag is the design tolerance, so the default is unhurried.

.PARAMETER Uninstall
    Remove the task instead of creating it.

.EXAMPLE
    .\push-task.ps1 -ArchiveRoot \\fileserver\NiclaLogs
    .\push-task.ps1 -ArchiveRoot \\fileserver\NiclaLogs -Name bench -EveryMinutes 15
    .\push-task.ps1 -Uninstall
#>
[CmdletBinding()]
param(
    [string] $ArchiveRoot,
    [string] $Name,
    [int] $EveryMinutes = 5,
    [switch] $Uninstall
)

$ErrorActionPreference = "Stop"
$TaskPath = "\NiclaSense\"
$TaskName = "ArchivePush"

if ($Uninstall) {
    Get-ScheduledTask -TaskPath $TaskPath -TaskName $TaskName -ErrorAction SilentlyContinue |
        Unregister-ScheduledTask -Confirm:$false
    exit 0
}

if (-not $ArchiveRoot) { throw "-ArchiveRoot is required unless -Uninstall is given" }
if ($EveryMinutes -lt 1) { throw "-EveryMinutes must be at least 1" }

$script = Join-Path $PSScriptRoot "push-logs.ps1"
if (-not (Test-Path -LiteralPath $script)) {
    throw "push-logs.ps1 is not beside this script ($script)"
}

$arguments = '-NoProfile -ExecutionPolicy Bypass -File "{0}" -ArchiveRoot "{1}"' `
    -f $script, $ArchiveRoot
if ($Name) { $arguments += ' -Name "{0}"' -f $Name }

$action = New-ScheduledTaskAction -Execute "powershell.exe" -Argument $arguments `
    -WorkingDirectory $PSScriptRoot

# A startup trigger carrying an indefinite repetition. The Once trigger below is built only
# to borrow its Repetition block -- the scheduler has no direct way to say "at startup, then
# every N minutes forever", and this is the documented way round it.
$trigger = New-ScheduledTaskTrigger -AtStartup
$repeating = New-ScheduledTaskTrigger -Once -At (Get-Date) `
    -RepetitionInterval (New-TimeSpan -Minutes $EveryMinutes) `
    -RepetitionDuration ([TimeSpan]::MaxValue)
$trigger.Repetition = $repeating.Repetition

$principal = New-ScheduledTaskPrincipal -UserId "SYSTEM" -LogonType ServiceAccount `
    -RunLevel Highest

# IgnoreNew rather than the default: a push still working through an unresponsive share must
# not have a second copy of itself started on top of it every few minutes. The time limit is
# the other half -- a run that has not finished in an hour is stuck, and letting the scheduler
# end it beats letting them accumulate.
$settings = New-ScheduledTaskSettingsSet `
    -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit (New-TimeSpan -Hours 1) `
    -StartWhenAvailable `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries

Register-ScheduledTask -TaskPath $TaskPath -TaskName $TaskName `
    -Action $action -Trigger $trigger -Principal $principal -Settings $settings `
    -Description "Pushes this machine's Nicla capture logs to the archive share." `
    -Force | Out-Null

$folder = if ($Name) { $Name } else { [Environment]::MachineName.ToLower() }
Write-Output ("registered {0}{1}: every {2} minute(s) -> {3}\{4}" -f
              $TaskPath, $TaskName, $EveryMinutes, $ArchiveRoot, $folder)
Write-Output "grant this machine's account write access to the share (see README.md)"
