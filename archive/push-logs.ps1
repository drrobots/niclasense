<#
.SYNOPSIS
    Copies this machine's capture logs to its own folder on the archive share.

.DESCRIPTION
    Runs on a capture machine, on a schedule. Each machine pushes only its own logs, into a
    subdirectory named after itself, and knows nothing about any other sensor.

    That is the whole argument for pushing rather than being pulled from. There is no
    collector machine to keep running, no list of sensors to maintain anywhere, and no
    read-only share on this machine for something else to reach into -- a capture stays a
    capture rather than also being a file server. Adding a sensor is installing a sensor.

    What it costs is that every capture needs write access to the share, where a collector
    would have been the only one. Scope that per folder if it matters; see README.md.

    Still not a mirror. /MIR and /PURGE are absent for the reason they are absent from the
    pull: retention.py deletes old captures from this machine on its own schedule, and the
    archive is meant to outlive them. A mirror would faithfully reproduce those deletions,
    which is the one thing it must not do.

    The capture being written right now is copied too, and re-copied every run as it grows.
    At a row a minute that is a few hundred kilobytes; robocopy skips what has not changed.

.PARAMETER ArchiveRoot
    The share to push into. One subdirectory per machine is created beneath it.

.PARAMETER Name
    This sensor's folder on the share, and therefore the name it is known by everywhere.
    Defaults to the machine name, which is what makes this self-contained.

.PARAMETER LogDir
    Where the capture writes. Defaults to the location the installed service uses.

.EXAMPLE
    .\push-logs.ps1 -ArchiveRoot \\fileserver\NiclaLogs
    .\push-logs.ps1 -ArchiveRoot \\fileserver\NiclaLogs -Name bench
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)] [string] $ArchiveRoot,
    [string] $Name,
    [string] $LogDir,
    [string] $LogPath
)

$ErrorActionPreference = "Stop"

# [Environment]::MachineName rather than $env:COMPUTERNAME: the environment variable is
# Windows-only, and reaching .ToLower() through a null is how this failed the first time
# it was run anywhere else. The name matters too much to leave to a variable that might
# not be there.
if (-not $Name) { $Name = [Environment]::MachineName.ToLower() }
if (-not $LogDir) { $LogDir = Join-Path $env:ProgramData "NiclaSense\logs" }
if (-not $LogPath) { $LogPath = Join-Path $env:ProgramData "NiclaSense\push-logs.log" }

# The name becomes a directory on the share, so anything that is not a plain name is refused
# here rather than producing a path somewhere surprising.
if ($Name -notmatch '^[A-Za-z0-9._-]+$') {
    Write-Error "'$Name' is not usable as a directory name"
    exit 2
}

function Write-Line([string] $Text) {
    $stamped = "{0}  {1}" -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss"), $Text
    # Write-Host, not Write-Output: this is progress, not a return value, and on the pull
    # side emitting it to the success stream made it part of a function's result.
    Write-Host $stamped
    try {
        Add-Content -Path $LogPath -Value $stamped -Encoding UTF8
    } catch {
        # A log that cannot be written is not a reason to skip the copy.
    }
}

if (-not (Test-Path -LiteralPath $LogDir)) {
    Write-Line ("nothing to push: {0} does not exist" -f $LogDir)
    Write-Line "is the capture service installed, and has it run yet?"
    exit 0
}

# Checked before robocopy, so a share that is simply not there is reported in a second rather
# than after robocopy has spent its retries discovering the same thing. Being unable to reach
# it is a normal state on a machine that is sometimes off the network, and not an alarm: the
# next run copies what this one could not, because nothing here deletes.
if (-not (Test-Path -LiteralPath $ArchiveRoot)) {
    Write-Line ("archive unreachable: {0}" -f $ArchiveRoot)
    exit 1
}

$target = Join-Path $ArchiveRoot $Name
if (-not (Test-Path -LiteralPath $target)) {
    try {
        New-Item -ItemType Directory -Path $target -Force -ErrorAction Stop | Out-Null
    } catch {
        Write-Line ("cannot create {0}" -f $target)
        Write-Line ("  {0}" -f $_.Exception.Message)
        Write-Line "  this machine's account probably cannot write to the share"
        exit 2
    }
}

# /R:1 /W:5 rather than the defaults, which are a million retries thirty seconds apart. A
# share that goes away mid-copy would otherwise hold this run open for weeks, and the next
# scheduled one would find it still going.
$roboArgs = @(
    $LogDir, $target,
    "*.csv",         # only captures; nothing else in that directory belongs on the share
    "/COPY:DAT",
    "/R:1", "/W:5",
    "/NP", "/NDL", "/NJH", "/NJS"
)
$output = & robocopy.exe @roboArgs 2>&1
$code = $LASTEXITCODE

# robocopy's exit code is a bitmask and success is not zero: 0 is "nothing needed copying",
# 1 is "files copied", 2 and 4 are informational, and only 8 and above are failures.
if ($code -ge 8) {
    Write-Line ("FAILED  robocopy exit {0} pushing to {1}" -f $code, $target)
    foreach ($line in $output) {
        $text = "$line".Trim()
        if ($text) { Write-Line ("        {0}" -f $text) }
    }
    exit 1
}

if ($code -eq 0) {
    Write-Line ("up to date   {0} -> {1}" -f $Name, $ArchiveRoot)
} else {
    Write-Line ("pushed       {0} -> {1}  (robocopy {2})" -f $Name, $ArchiveRoot, $code)
}
exit 0
