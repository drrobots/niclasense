# Pulling the logs into one place

Windows-only, and separate from `packaging/`: that installs a capture machine, this sets up
the one machine that collects from them. Three scripts, no Python, nothing new on the
capture side beyond a read-only share.

The design and the reasoning behind it are in `HISTORICAL-VIEWER.md`. The short version is
that a viewer reading files needs no inbound port on any capture machine, gets its cross-board
time alignment from `host_iso` for free, and lets AD do the authentication.

## Which script runs where

| Script | Machine | When |
|---|---|---|
| `share-logs.ps1` | every capture | once, elevated |
| `pull-task.ps1` | the viewer machine | once, elevated |
| `pull-logs.ps1` | the viewer machine | by the task, every few minutes |

## Setting it up

**On each capture machine**, publish the logs read-only. `VIEWERBOX$` is the viewer
machine's account — the trailing `$` is not a typo, it is how a machine account is named,
and it is what lets the pull run without a password stored anywhere:

```powershell
.\share-logs.ps1 -ReadAccount "CONTOSO\VIEWERBOX$"
```

It prints the line to add to `sources.txt`. Once there is more than one thing reading,
grant a group instead and put the machine accounts in that.

**On the viewer machine**, list the captures and register the task:

```powershell
Copy-Item sources.example.txt sources.txt   # then edit it
.\pull-task.ps1 -ArchiveRoot D:\NiclaArchive
```

Run `pull-logs.ps1` by hand first to see it work — it reports each source on its own line and
writes the same to `pull-logs.log` in the archive root.

## What lands where

```
D:\NiclaArchive\
    bench\      nicla_20260819_085534.csv ...
    rig-a\      ...
    rig-b\      ...
    pull-logs.log
```

One directory per source, named from `sources.txt`. **That layout is the board list.**
Whatever reads the archive enumerates directories rather than being handed a separate list,
so there is nothing to keep in step.

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

**A machine is skipped as `unreachable`.** Expected, and not a failure — captures reboot.
The next run collects what it missed, because nothing deletes and robocopy skips what it
already has. If it is *always* unreachable, check File and Printer Sharing is allowed
through the capture's firewall for the domain or private profile; `share-logs.ps1
-OpenFirewall` will enable it, and it is off by default because on a managed fleet that is
usually policy's job.

**A source says `FAILED` with a robocopy exit code.** The share answered and then could not
be read — usually the account in `-ReadAccount` is not the one the task runs as. The task
runs as SYSTEM, so it arrives as `DOMAIN\VIEWERBOX$`.

**Nothing appears to happen.** robocopy's exit code is a bitmask where success is not zero:
0 is "nothing needed copying", 1 is "files copied", and only 8 and above are failures.
`pull-logs.ps1` accounts for that, but anything you write around it needs to as well.

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
