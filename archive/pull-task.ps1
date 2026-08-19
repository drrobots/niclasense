<#
.SYNOPSIS
    Registers (or removes) the scheduled task that pulls capture logs into the archive.

.DESCRIPTION
    Runs on one always-on collector machine -- not on a capture, and not on the desktops
    people read from. One task, pulling from every capture in sources.txt onto the share.

    Three roles, and it is worth being clear which machine is which. A capture records to its
    own disk and offers it read-only. This collector copies from all of them to the share.
    Anyone reading points viewer.cmd at that share. Nothing but the collector needs to see
    more than one machine.

    Why SYSTEM rather than a service account with a stored password. A task running as
    SYSTEM reaches the network as the machine account -- DOMAIN\THISBOX$ -- so it is that
    account, or a group holding it, that needs read on each capture's share and **write** on
    the archive share. There is no password anywhere on disk or in the task definition. It is
    the same trick the capture service uses to stay LocalSystem, and nothing here has to be
    rotated.

    The repetition rides on a startup trigger rather than a logon one. Nobody needs to be
    signed in for the archive to keep filling, which is the difference between this and the
    dashboard's logon task -- that one exists to put a window in front of a person, and this
    one exists whether or not anyone is looking. A collector that is off simply misses
    copies; the next run collects them, because nothing here deletes and robocopy skips what
    it already has.

.PARAMETER ArchiveRoot
    Directory to pull into. Passed straight to pull-logs.ps1.

.PARAMETER EveryMinutes
    How often to pull. Minutes of lag is the design tolerance, so the default is unhurried.

.PARAMETER Uninstall
    Remove the task instead of creating it.

.EXAMPLE
    .\pull-task.ps1 -ArchiveRoot \\fileserver\NiclaLogs
    .\pull-task.ps1 -ArchiveRoot \\fileserver\NiclaLogs -EveryMinutes 15
    .\pull-task.ps1 -ArchiveRoot \\fileserver\NiclaLogs -Uninstall
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)] [string] $ArchiveRoot,
    [int] $EveryMinutes = 5,
    [switch] $Uninstall
)

$ErrorActionPreference = "Stop"
$TaskPath = "\NiclaSense\"
$TaskName = "ArchivePull"

if ($Uninstall) {
    Get-ScheduledTask -TaskPath $TaskPath -TaskName $TaskName -ErrorAction SilentlyContinue |
        Unregister-ScheduledTask -Confirm:$false
    exit 0
}

if ($EveryMinutes -lt 1) { throw "-EveryMinutes must be at least 1" }

$script = Join-Path $PSScriptRoot "pull-logs.ps1"
if (-not (Test-Path -LiteralPath $script)) {
    throw "pull-logs.ps1 is not beside this script ($script)"
}

$arguments = '-NoProfile -ExecutionPolicy Bypass -File "{0}" -ArchiveRoot "{1}"' `
    -f $script, $ArchiveRoot

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

# IgnoreNew rather than the default: a pull that is still working through an unresponsive
# share must not have a second copy of itself started on top of it every few minutes. The
# time limit is the other half of that -- a run that has somehow not finished in an hour is
# stuck, and letting the scheduler end it is better than letting it accumulate.
$settings = New-ScheduledTaskSettingsSet `
    -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit (New-TimeSpan -Hours 1) `
    -StartWhenAvailable `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries

Register-ScheduledTask -TaskPath $TaskPath -TaskName $TaskName `
    -Action $action -Trigger $trigger -Principal $principal -Settings $settings `
    -Description "Pulls Nicla capture logs from every machine in sources.txt into the archive." `
    -Force | Out-Null

Write-Output ("registered {0}{1}: every {2} minute(s) -> {3}" -f $TaskPath, $TaskName, $EveryMinutes, $ArchiveRoot)
Write-Output "grant this machine's account read on each capture share and write on the archive"
Write-Output "  (see README.md)"
