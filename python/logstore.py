"""The archive, read.

One directory per board beneath a root, each holding the CSVs `archive/pull-logs.ps1`
copied off that board's capture machine. That layout is the board list -- there is no
separate register of what exists, and adding a board to the pull is the only place a name
is ever written down.

Two properties of the files make an index cheap enough to build on demand. A capture is
named for the moment it started, so the name bounds its beginning without opening it; and
the copy preserves the source's last-write time, so the mtime bounds its end. Between them
a time window can be narrowed to a handful of candidate files by stat alone.

Times are naive local wall clock throughout, because that is what is in the files:
`logger.py` writes `datetime.now().isoformat()` and `main.py` names captures from
`datetime.now()`. That is right for a single site and wrong across timezones, which is a
trade this makes deliberately rather than by accident -- an archive spanning zones would
need the sketch and the logger changed first, not this.
"""

import csv
import datetime
import os
import re

from columns import BURST_COLUMN, CSV_COLUMNS, HOST_TIME_COLUMN

# nicla_20260819_085534.csv -- main.py's default_csv_path(), which is what the service uses
# because nicla.conf deliberately leaves csv unset.
CAPTURE_NAME = re.compile(r"^nicla_(\d{8}_\d{6})\.csv$")
NAME_STAMP = "%Y%m%d_%H%M%S"


def parse_start(filename):
    """The moment a capture began, from its name, or None if it is not one of ours."""
    match = CAPTURE_NAME.match(filename)
    if not match:
        return None
    try:
        return datetime.datetime.strptime(match.group(1), NAME_STAMP)
    except ValueError:
        # A name shaped like a capture but holding an impossible date. Not ours either.
        return None


def parse_time(text):
    """One host_iso value. None rather than an exception: a torn final row is normal."""
    try:
        return datetime.datetime.fromisoformat(text)
    except (TypeError, ValueError):
        return None


class Capture(object):
    """One CSV in the archive, placed in time without being opened."""

    def __init__(self, board, path, start, end, size):
        self.board = board
        self.path = path
        self.start = start
        self.end = end
        self.size = size

    @property
    def name(self):
        return os.path.basename(self.path)

    def overlaps(self, start=None, end=None):
        """Whether this file could hold rows in [start, end]. Bounds are inclusive.

        The end is the file's mtime, which for the capture currently being written is
        whenever the pull last copied it -- so it trails reality by a sync interval. It is
        used only to *exclude* files, and a bound that is too early would exclude a file
        that does hold wanted rows, so the live file is never excluded on its end.
        """
        if start is not None and self.end is not None and self.end < start:
            return False
        if end is not None and self.start is not None and self.start > end:
            return False
        return True

    def __repr__(self):
        return "<Capture %s/%s>" % (self.board, self.name)


class LogStore(object):
    """The archive root: which boards exist, which files they hold, and their rows."""

    def __init__(self, root):
        self.root = root

    # -- what is there ---------------------------------------------------------

    def boards(self):
        """Board names, sorted. The directories are the list."""
        try:
            entries = os.listdir(self.root)
        except OSError:
            return []
        return sorted(
            name for name in entries
            if os.path.isdir(os.path.join(self.root, name)) and not name.startswith(".")
        )

    def captures(self, board=None, start=None, end=None):
        """Captures overlapping [start, end], oldest first.

        Files that do not carry a capture name are skipped rather than scanned. They are
        reported by `strays()` instead of being silently invisible -- pull-logs.ps1 writes
        its own log into the archive root, and something unexpected inside a board's
        directory is worth being able to see.
        """
        found = []
        for name in ([board] if board else self.boards()):
            directory = os.path.join(self.root, name)
            try:
                entries = os.listdir(directory)
            except OSError:
                continue
            for filename in entries:
                began = parse_start(filename)
                if began is None:
                    continue
                path = os.path.join(directory, filename)
                try:
                    stat = os.stat(path)
                except OSError:
                    continue
                capture = Capture(
                    board=name,
                    path=path,
                    start=began,
                    end=datetime.datetime.fromtimestamp(stat.st_mtime),
                    size=stat.st_size,
                )
                if capture.overlaps(start, end):
                    found.append(capture)
        found.sort(key=lambda one: (one.start, one.board))
        return found

    def strays(self, board=None):
        """Files inside a board directory that are not captures. Usually nothing."""
        out = []
        for name in ([board] if board else self.boards()):
            directory = os.path.join(self.root, name)
            try:
                entries = os.listdir(directory)
            except OSError:
                continue
            for filename in sorted(entries):
                if parse_start(filename) is None and not filename.startswith("."):
                    out.append(os.path.join(name, filename))
        return out

    # -- rows ------------------------------------------------------------------

    def rows(self, capture, start=None, end=None):
        """Yield rows from one capture as dicts, within [start, end] if given.

        Two file shapes have to be read by the same code. A decimated capture carries a
        trailing `burst` column and a full-rate one does not, because at full rate the flag
        would be a constant -- so the header is what decides, not an assumption about how
        the fleet is configured.

        A torn last row is expected rather than exceptional: the file being written is
        copied mid-append, and any capture ended with Ctrl-C leaves one too. It is dropped
        without comment. A row that is malformed in the *middle* of a file is dropped the
        same way, which loses a sample and keeps the other several thousand.
        """
        with open(capture.path, "r", newline="") as handle:
            reader = csv.reader(handle)
            try:
                header = next(reader)
            except StopIteration:
                return
            if not header or header[0] != HOST_TIME_COLUMN:
                return
            width = len(header)
            has_burst = header[-1] == BURST_COLUMN
            for fields in reader:
                if len(fields) != width:
                    continue
                when = parse_time(fields[0])
                if when is None:
                    continue
                if start is not None and when < start:
                    continue
                if end is not None and when > end:
                    continue
                row = dict(zip(header, fields))
                row[HOST_TIME_COLUMN] = when
                row[BURST_COLUMN] = int(row[BURST_COLUMN]) if has_burst else 0
                row["board"] = capture.board
                yield row

    def has_burst_column(self, capture):
        """Whether this capture was written by a decimating logger."""
        try:
            with open(capture.path, "r", newline="") as handle:
                header = next(csv.reader(handle), [])
        except (OSError, StopIteration):
            return False
        return bool(header) and header[-1] == BURST_COLUMN


def column_count(header):
    """How many columns a capture header declares, for callers checking shape."""
    return len(CSV_COLUMNS) + (1 if header and header[-1] == BURST_COLUMN else 0)
