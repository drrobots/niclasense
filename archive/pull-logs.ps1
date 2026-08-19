<#
.SYNOPSIS
    Copies capture logs from every machine in sources.txt into one archive directory.

.DESCRIPTION
    Runs on the viewer machine, on a schedule, and pulls rather than being pushed to. One
    job to maintain instead of one per capture, the capture machines stay read-only with no
    write access to the archive and no scheduled task of their own, and adding a board is a
    line in a text file on one machine.

    This is an archive, not a mirror. /MIR and /PURGE are deliberately absent: the archive
    is meant to outlive what any single capture keeps, and retention.py will delete files
    from a capture machine on its own schedule. A mirror would faithfully reproduce those
    deletions, which is the one thing an archive must not do.

    The destination layout is the board list. Each source lands in its own subdirectory
    named after it, so whatever reads the archive later enumerates directories rather than
    being told separately what boards exist -- there is no second list to keep in step.

.PARAMETER ArchiveRoot
    Directory to pull into, normally the UNC path of the share the viewers read. One
    subdirectory per source is created beneath it, and those names are the board list.

.PARAMETER Sources
    Machine list. Defaults to sources.txt beside this script.

.PARAMETER LogPath
    Where to append this run's summary. Defaults to pull-logs.log beside the archive.

.EXAMPLE
    .\pull-logs.ps1 -ArchiveRoot D:\NiclaArchive
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)] [string] $ArchiveRoot,
    [string] $Sources,
    [string] $LogPath
)

$ErrorActionPreference = "Stop"

if (-not $Sources) { $Sources = Join-Path $PSScriptRoot "sources.txt" }
if (-not $LogPath) { $LogPath = Join-Path $ArchiveRoot "pull-logs.log" }

function Write-Line([string] $Text) {
    $stamped = "{0}  {1}" -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss"), $Text
    # Write-Host, not Write-Output. Write-Output puts the line on the success stream, and
    # this is called from inside Copy-Source, whose success stream *is* its return value --
    # so every progress line would be handed back to the caller along with the result, and
    # the switch reading that result would be matching against log text. It happened to
    # count correctly and print nothing, which is the worst way for it to be wrong.
    Write-Host $stamped
    try {
        Add-Content -Path $LogPath -Value $stamped -Encoding UTF8
    } catch {
        # A log that cannot be written is not a reason to skip the copy. The scheduled task
        # keeps its own transcript, so the run is still recoverable.
    }
}

# -- the machine list --------------------------------------------------------------------
#
# name <whitespace> UNC path, one per line, # for comments. Deliberately not INI or JSON:
# it is edited by hand on one machine, it has two fields, and a format with no parser is
# one fewer thing to get wrong at three in the morning.
function Read-Sources([string] $Path) {
    if (-not (Test-Path -LiteralPath $Path)) {
        throw "no source list at $Path -- copy sources.example.txt to sources.txt and edit it"
    }
    $entries = @()
    $lineNo = 0
    foreach ($raw in Get-Content -LiteralPath $Path) {
        $lineNo++
        $line = $raw.Trim()
        if ($line -eq "" -or $line.StartsWith("#")) { continue }
        $parts = $line -split "\s+", 2
        if ($parts.Count -ne 2) {
            throw "$Path line ${lineNo}: expected '<name> <\\host\share>', got '$line'"
        }
        $name = $parts[0]
        # The name becomes a directory, so anything that is not a plain name is refused here
        # rather than producing a path somewhere surprising.
        if ($name -notmatch '^[A-Za-z0-9._-]+$') {
            throw "$Path line ${lineNo}: '$name' is not usable as a directory name"
        }
        $entries += [pscustomobject]@{ Name = $name; Share = $parts[1].Trim() }
    }
    return $entries
}

# -- one source --------------------------------------------------------------------------
function Copy-Source($Entry, [string] $Root) {
    $target = Join-Path $Root $Entry.Name
    if (-not (Test-Path -LiteralPath $target)) {
        New-Item -ItemType Directory -Path $target -Force | Out-Null
    }

    # Asked before robocopy is started, so an expected absence -- a capture box rebooting,
    # or off for the weekend -- is reported in a second rather than after robocopy has spent
    # its retries discovering the same thing.
    if (-not (Test-Path -LiteralPath $Entry.Share)) {
        Write-Line ("unreachable  {0}  {1}" -f $Entry.Name, $Entry.Share)
        return "unreachable"
    }

    # /R:1 /W:5 rather than the defaults, which are a million retries thirty seconds apart.
    # A share that goes away mid-copy would otherwise hold this job open for weeks, and the
    # next scheduled run would find it still going.
    #
    # No /MIR and no /PURGE: see the note at the top. Nothing here may delete.
    $roboArgs = @(
        $Entry.Share, $target,
        "/E",            # subdirectories, including empty ones
        "/COPY:DAT",     # data, attributes, timestamps
        "/R:1", "/W:5",
        "/NP",           # no per-file percentage; this runs unattended
        "/NDL",          # no directory list
        "/NJH", "/NJS"   # no job header or summary: this script writes its own
    )
    $output = & robocopy.exe @roboArgs 2>&1
    $code = $LASTEXITCODE

    # robocopy's exit code is a bitmask, and success is not zero. 0 means nothing needed
    # copying, 1 means files were copied, 2 and 4 are informational; only 8 and above are
    # failures. Treating non-zero as an error -- the reflex everywhere else -- would report
    # every successful copy as a failure.
    if ($code -ge 8) {
        Write-Line ("FAILED       {0}  robocopy exit {1}" -f $Entry.Name, $code)
        foreach ($line in $output) {
            $text = "$line".Trim()
            if ($text) { Write-Line ("             {0}" -f $text) }
        }
        return "failed"
    }

    if ($code -eq 0) {
        Write-Line ("up to date   {0}" -f $Entry.Name)
    } else {
        Write-Line ("copied       {0}  (robocopy {1})" -f $Entry.Name, $code)
    }
    return "ok"
}

# -- run -----------------------------------------------------------------------------------

# The destination is checked as deliberately as the sources are, because it is now normally a
# share too. An unreachable one would otherwise surface as whatever New-Item throws, from a
# scheduled task nobody is watching, and the difference between "the share is down" and "the
# account cannot write there" is most of the diagnosis.
if (-not (Test-Path -LiteralPath $ArchiveRoot)) {
    try {
        New-Item -ItemType Directory -Path $ArchiveRoot -Force -ErrorAction Stop | Out-Null
    } catch {
        Write-Line ("FAILED       cannot reach or create {0}" -f $ArchiveRoot)
        Write-Line ("             {0}" -f $_.Exception.Message)
        Write-Line "             the share is unmounted, or this account cannot write to it"
        exit 2
    }
}

$entries = Read-Sources $Sources
if ($entries.Count -eq 0) {
    Write-Line "nothing to do: the source list is empty"
    exit 0
}

Write-Line ("pull start   {0} source(s) -> {1}" -f $entries.Count, $ArchiveRoot)

$failed = 0
$unreachable = 0
foreach ($entry in $entries) {
    switch (Copy-Source $entry $ArchiveRoot) {
        "failed"      { $failed++ }
        "unreachable" { $unreachable++ }
    }
}

Write-Line ("pull done    {0} ok, {1} unreachable, {2} failed" -f
            ($entries.Count - $failed - $unreachable), $unreachable, $failed)

# A machine being unreachable is a normal state and not a failure: captures reboot, and the
# next run collects what it missed because nothing here deletes and robocopy skips what it
# already has. A share that answered and then could not be read is a real fault, and is the
# only thing that makes this run non-zero.
if ($failed -gt 0) { exit 1 }
exit 0
