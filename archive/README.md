# Pulling the logs into one place

Windows-only. `packaging/` installs a capture machine; this is how a capture's logs reach the
share everyone reads from.

**Two roles, not three.** A *capture* records to its own disk and pushes its own logs to its
own folder on the share. *Readers* point `viewer.cmd` at that share. There is no machine in
between, no list of sensors anywhere, and no capture is a file server for any other machine.
Adding a sensor is installing a sensor.

Captures write locally and copy afterwards rather than writing across the network directly.
That is the point of the arrangement: a capture writing to a share stops recording when the
network hiccups, and this way it is only the copy that fails. Nothing is lost, because the
next run sends what the last one could not.

| Script | Machine | When |
|---|---|---|
| `push-task.ps1` | every capture | once, elevated — or by the installer |
| `push-logs.ps1` | every capture | by the task, every few minutes |

`pull-logs.ps1`, `pull-task.ps1` and `share-logs.ps1` are the other arrangement — a read-only
share on each capture and one collector machine copying from all of them. Nothing uses them
now. They are kept because the choice is reversible and the trade is real: pulling means one
account writes to the archive instead of every capture. See *The other way round* below.

## Setting it up

**By installer, which is the point of pushing.** One command, identical on every machine:

```
setup.exe /VERYSILENT /ARCHIVE=\\fileserver\NiclaLogs
```

The folder on the share is the machine's own name. Pass `/SENSORNAME=bench` to call it
something else. An install naming no archive pushes nothing and behaves exactly as installs
did before there was a share.

**By hand**, on a machine installed before there was an archive:

```powershell
.\push-task.ps1 -ArchiveRoot \\fileserver\NiclaLogs
```

**On the share**, grant every capture's machine account write access — a group holding them
is the manageable form. The trailing `$` in `CONTOSO\BENCH01$` is not a typo; it is how a
machine account is named, and using it is what lets the push run with no password stored
anywhere. Readers need only read.

If it matters that a capture can only write to its own folder, scope that with NTFS
permissions per subdirectory. Share-level write is the simpler grant and lets any capture
touch another's folder.

## What lands where

```
\\fileserver\NiclaLogs\
    bench\      nicla_20260819_085534.csv ...
    rig-a\      ...
    rig-b\      ...
    pull-logs.log
```

One directory per source, named from `sources.txt`. **That layout is the board list.**
Whatever reads the archive enumerates directories rather than being handed a separate list,
so there is nothing to keep in step — and it is also what stops two sensors colliding, since
a capture is named only for the second it started.

This is the path readers put in `viewer.conf`.

## What it will not do

**It never deletes.** No `/MIR`, no `/PURGE`, and a test asserts their absence. The archive
is meant to outlive what a capture machine keeps — `retention.py` deletes from the captures
on its own schedule, and a mirror would faithfully reproduce those deletions, which is the
one behaviour that would defeat the whole point.

Nothing prunes the archive either. Its storage and retention are handled outside this
directory.

**It does not write to the captures.** Read at the share level, which combines with NTFS by
taking the more restrictive of the two, so the puller cannot alter a capture's logs whatever
the file system would otherwise permit.

## When it does not work

**`archive unreachable`.** The share is not mounted on that capture. Expected occasionally
and not a failure — the next run sends what this one could not. If it never clears, the
machine cannot see the share at all.

**`cannot create ... this machine's account probably cannot write to the share`.** The grant
is missing. The task runs as SYSTEM, so the account to grant is `DOMAIN\THATMACHINE$`, not a
user.

**`nothing to push`.** The capture service has not written anything yet, or is not installed.

**The run fails before it starts, naming the archive.** The share is not mounted on the
collector, or the collector's machine account cannot write to it. The task runs as SYSTEM, so
the account to grant is `DOMAIN\COLLECTOR$`.

**A source says `FAILED` with a robocopy exit code.** The share answered and then could not
be read — usually the account in `-ReadAccount` is not the one the task runs as. The task
runs as SYSTEM, so it arrives as `DOMAIN\VIEWERBOX$`.

**Nothing appears to happen.** robocopy's exit code is a bitmask where success is not zero:
0 is "nothing needed copying", 1 is "files copied", and only 8 and above are failures.
`pull-logs.ps1` accounts for that, but anything you write around it needs to as well.

## The other way round

`share-logs.ps1`, `pull-task.ps1` and `pull-logs.ps1` implement pulling instead: each capture
offers a read-only share, one always-on collector copies from all of them onto the archive,
and `sources.txt` on that collector lists the machines.

It is kept for one reason. Pushing means every capture can write to the archive; pulling
means exactly one account can, and every capture is read-only to the rest of the network. If
that trade ever matters more than the collector machine costs, the scripts are here and
`archive/README.md` history has the setup.

Do not run both. They would copy the same files by two routes, which works and is confusing.

## Testing

`python/testing/test_archive.py`, and more of it runs than you would expect from a Mac.

Static checks cover the invariants: nothing here may delete, retries are bounded so an
offline share cannot wedge a run, the share is granted Read and nothing else, no password is
ever stored, and no application port is opened anywhere.

The rest is actually executed, wherever PowerShell exists — `pwsh` on macOS is enough. With
every source unresolvable robocopy is never invoked, so the source parser, the directory
layout, the unreachable path, the log and the exit code all run. That is most of this script.

It earns its place: running it is what found `Write-Line` emitting on the success stream,
where every progress line became part of `Copy-Source`'s return value. It printed nothing to
the console and still counted correctly, which is the worst way for something to be wrong,
and no amount of reading the file would have shown it.

What still needs Windows is robocopy itself, share semantics and the scheduled task —
exactly as `packaging/` does. Run it on one machine before the fleet.
