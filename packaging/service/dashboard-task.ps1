<#
.SYNOPSIS
    Registers (or removes) the logon task that starts the dashboard in the user's session.

.DESCRIPTION
    Why a logon task and not a second service. The dashboard is the thing a person looks
    at: it needs a session with a browser in it, and a service runs in session 0 where
    there is neither. The split is the one the project already makes -- the capture is the
    durable process and viewers come and go over its socket -- so the dashboard being
    per-user and restartable is that design working rather than a compromise.

    Why it is invisible. pythonw.exe has no console to show, and -Hidden stops the task
    scheduler from flashing one of its own. supervise.py points the streams at a file,
    because under pythonw sys.stderr is None and webdash.py's first status line would
    otherwise raise AttributeError on it.

    A Users-group principal with a plain logon trigger means "any member of Users, at their
    own logon, as themselves". Two people logged on at once is therefore two dashboards,
    and the second cannot bind port 8988: it logs the clash and gives up, leaving the
    first one working. That is the honest behaviour for a single-port loopback server.

    Built with the ScheduledTasks cmdlets rather than schtasks /XML because the XML route
    wants a UTF-16 file and quietly misbehaves given anything else -- an encoding bug in
    an installer is a miserable thing to diagnose from the other end of a support email.
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)] [string] $AppDir,
    [switch] $Uninstall,
    [int] $HttpPort = 8988,
    [string] $Endpoint = "127.0.0.1:8765",
    [string] $HttpHost = "127.0.0.1",
    [string[]] $AllowHost = @()
)

$ErrorActionPreference = "Stop"
$TaskPath = "\NiclaSense\"
$TaskName = "Dashboard"

if ($Uninstall) {
    Get-ScheduledTask -TaskPath $TaskPath -TaskName $TaskName -ErrorAction SilentlyContinue |
        Unregister-ScheduledTask -Confirm:$false
    exit 0
}

$pythonw   = Join-Path $AppDir "python\pythonw.exe"
$supervise = Join-Path $AppDir "service\supervise.py"

# The log goes under the user's own LocalAppData, not ProgramData: the task runs
# unprivileged, one per logged-on user, and a shared file two sessions append to is a
# permissions problem and a muddled log at once. %LOCALAPPDATA% is left unexpanded on
# purpose -- the task scheduler expands it in the running user's context, which is the
# whole point of it.
# supervise.py passes anything it does not recognise straight through to webdash.py, so the
# bind address needs no support there -- only a way through this task's own argument list.
#
# $HttpHost defaults to loopback, which is what a machine gets unless the installer was told
# otherwise. Moving it off loopback puts an unauthenticated feed on the network, and needs a
# firewall rule before it is reachable at all; see packaging/README.md.
$arguments = '"{0}" dashboard --log "%LOCALAPPDATA%\NiclaSense\dashboard.log" {1} --http-port {2} --http-host {3}' `
    -f $supervise, $Endpoint, $HttpPort, $HttpHost
foreach ($name in $AllowHost) {
    $arguments += ' --allow-host {0}' -f $name
}

$action = New-ScheduledTaskAction -Execute $pythonw -Argument $arguments `
    -WorkingDirectory (Join-Path $AppDir "app")

# Half a minute after logon. The capture is a delayed-start service, so at a cold boot it
# may still be waiting on the board; the dashboard retries regardless, and the delay keeps
# the ordinary case from opening its log with a run of attach failures.
$trigger = New-ScheduledTaskTrigger -AtLogOn
$trigger.Delay = "PT30S"

# S-1-5-32-545 is BUILTIN\Users, named by SID because the name is localised.
$principal = New-ScheduledTaskPrincipal -GroupId "S-1-5-32-545" -RunLevel Limited

# No execution time limit: it is meant to run for as long as the user is logged on. No
# restart-on-failure either -- supervise.py owns the retrying, and a second restarter above
# it would only race with the first.
$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -ExecutionTimeLimit ([TimeSpan]::Zero) `
    -MultipleInstances IgnoreNew `
    -Hidden

Register-ScheduledTask -TaskPath $TaskPath -TaskName $TaskName -Force `
    -Action $action -Trigger $trigger -Principal $principal -Settings $settings |
    Out-Null

Write-Host "registered $TaskPath$TaskName"

# Start it now as well as at the next logon, so installing and then looking at the
# dashboard does not require signing out first. The task runs as whoever is logged on; when
# that is nobody -- an unattended or remote install -- there is no session to start it in
# and the next logon is when it begins. Not an error either way.
try {
    Start-ScheduledTask -TaskPath $TaskPath -TaskName $TaskName
} catch {
    Write-Host "not started now (no interactive session); it will start at the next logon"
}
