"""Delete old captures so a capture that never stops does not fill the disk.

A capture run from a terminal ends when somebody ends it, and the CSVs it leaves are that
person's problem. A capture installed as a Windows service does not end: it starts at boot,
restarts when the board is unplugged and replugged, and writes a new timestamped file every
time it starts. Nobody is watching it, so something has to decide what to throw away.

Both limits are off by default, which keeps a hand-run capture behaving exactly as it did.
The Windows installer turns them on, because that is the deployment where nothing else
will.

Two limits rather than one, and they answer different questions. The age limit is the
policy -- keep a year -- and it is what somebody would say out loud if asked. The size limit
is the safety net, for the board left somewhere that vibrates: the steady rate is a row a
minute, about 100 MB a year, but a burst logs at the full 200 Hz and a day of solid bursting
is 3.3 GB on its own. A year of resting data plus a day of bursting is what the ceiling is
sized for, and if a month of bursting happens instead, the ceiling is what stops it.

Deletion is oldest-first by modification time, not by the timestamp in the filename. They
usually agree, but a file appended to by a later run -- which `--csv` makes easy -- has an
old name and recent contents, and it is the contents that decide whether it is still wanted.
"""

import glob
import os

# What a log directory is allowed to contain that this will consider deleting. Narrow on
# purpose: the sweep runs unattended against a directory a config file named, and the cost
# of a wrong path is somebody's data rather than a stale log.
PATTERN = "*.csv"

SECONDS_PER_DAY = 86400.0


class SweepResult(object):
    """What one sweep did, in the form the caller reports it in."""

    def __init__(self):
        self.removed = []
        self.freed = 0
        self.remaining = 0
        self.failed = []

    def summary(self):
        if not self.removed and not self.failed:
            return "retention: nothing to remove, %s in place" % _mb(self.remaining)
        parts = ["retention: removed %d file(s), freed %s, %s in place"
                 % (len(self.removed), _mb(self.freed), _mb(self.remaining))]
        if self.failed:
            parts.append("%d could not be removed" % len(self.failed))
        return "; ".join(parts)


def _mb(count):
    if count >= 1024 ** 3:
        return "%.1f GB" % (count / float(1024 ** 3))
    return "%.0f MB" % (count / float(1024 ** 2))


def sweep(directory, max_age_days=0.0, max_bytes=0, keep=(), now=None):
    """Apply the limits to `directory`, returning a SweepResult.

    `keep` is the paths that must survive whatever the limits say -- in practice the file
    the capture is writing to at this moment. It still counts towards the total, because a
    ceiling that ignored the file actually growing would be exceeded by exactly the amount
    that matters, but it is never a candidate for deletion. Deleting the open file would on
    Windows fail outright and on POSIX succeed silently, leaving the capture writing to an
    unlinked inode nobody can read; neither is a way to enforce a disk quota.

    Either limit at zero is off. Both off makes this a no-op that still reports the size in
    place, which is what a caller wants at start-up whether or not the limits are set.

    A file that cannot be removed is recorded and stepped over rather than raised. This runs
    inside a capture that is otherwise working, and on Windows another process holding a
    handle to an old CSV -- a spreadsheet, an editor, an antivirus scanner mid-read -- is an
    ordinary thing that must not take the capture down with it.
    """
    if now is None:
        import time
        now = time.time()

    protected = set(os.path.abspath(path) for path in keep)
    result = SweepResult()

    entries = []
    for path in glob.glob(os.path.join(directory, PATTERN)):
        try:
            stat = os.stat(path)
        except OSError:
            # Vanished between the glob and the stat. Nothing to do about a file that is
            # already gone, and it is not an error that the caller can act on.
            continue
        entries.append((stat.st_mtime, stat.st_size, os.path.abspath(path)))
    entries.sort()

    total = sum(size for _mtime, size, _path in entries)

    def drop(entry):
        mtime, size, path = entry
        try:
            os.remove(path)
        except OSError as exc:
            result.failed.append((path, exc))
            return 0
        result.removed.append(path)
        result.freed += size
        return size

    survivors = []
    if max_age_days > 0:
        cutoff = now - max_age_days * SECONDS_PER_DAY
        for entry in entries:
            mtime, _size, path = entry
            if mtime < cutoff and path not in protected:
                total -= drop(entry)
            else:
                survivors.append(entry)
    else:
        survivors = list(entries)

    # Size runs second and on what the age limit left, so a file removed for being old is
    # not also counted as having been removed for space. Oldest first again: `survivors` is
    # still in mtime order.
    if max_bytes > 0:
        for entry in survivors:
            if total <= max_bytes:
                break
            if entry[2] in protected:
                continue
            total -= drop(entry)

    result.remaining = total
    return result


def make_sweeper(directory, max_age_days=0.0, max_bytes=0, keep=()):
    """A no-argument callable that sweeps, for a caller that just wants to tick it.

    main.py calls this on a timer from its drain loop, where the loop should not have to
    know what the limits were or where the logs live.
    """
    def run():
        return sweep(directory, max_age_days, max_bytes, keep)
    return run
