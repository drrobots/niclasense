"""Burst episodes: what happened, and when.

With the fleet decimating to a row a minute and promoting to full rate on movement, the
burst rows *are* the events. Everything else is the slow background the events sit on. So
the first useful question about an archive is not "draw me a day" but "list what happened",
and that is answerable by scanning one column.

An episode is a maximal run of rows with the flag set. The decimator counts episodes itself
but does not write that count down, so it is recovered here from the flag alone -- which
also means this works on any decimated capture, including ones made before anybody thought
about a viewer.

Recovered, not reproduced. Two bursts can be written back to back: the hold expires and a
new trigger fires before the steady grid has emitted anything, leaving no gap between them
in the file. Those read as one episode here and as two in the decimator's own counter, and
nothing in the flag can tell them apart. Measured against a real capture the counts came out
16 against 17, and against a deliberately over-triggered one 13 against 21 -- the gap widens
exactly as bursting approaches continuous, which is a misconfigured threshold rather than
anything a fleet at 0.15 g will see.

That is the right trade for a list somebody reads: a board that shook for four seconds while
the trigger re-armed twice is one thing that happened, not three. If an exact count is ever
wanted, the fix belongs in the logger -- an episode number instead of a 0/1 flag -- and not
in cleverer grouping here.

A full-rate capture has no flag and therefore no episodes. That is correct rather than a
gap: nothing was promoted because nothing was being thinned.
"""

import math

from columns import BURST_COLUMN, HOST_TIME_COLUMN

# Two burst rows further apart than this start a new episode. Bursts are written at the
# board's full rate -- 5 ms apart at 200 Hz -- so ten sample periods is far above the spacing
# inside an episode and far below the quiet between two of them.
#
# Measured against real captures rather than picked. What matters is not a particular number
# but that the answer stops moving: between 0.02 and 0.05 the recovered count is identical,
# which is the signature of a threshold separating real gaps instead of sampling jitter.
# Above 0.1 it starts merging distinct events, and it merges them silently.
MAX_GAP_S = 0.05

# What "how big was it" means, given the deployment triggers on movement. Named here rather
# than assumed inline: widen `burst_cols` in nicla.conf and these stop being the whole story,
# and this is the line that should be read when that happens.
ACCEL = ("ax_g", "ay_g", "az_g")
GYRO = ("gx_dps", "gy_dps", "gz_dps")


def _board_seconds(row):
    """The board's own clock for this row, in seconds, or None if it is not readable.

    Gaps are measured on t_ms and not on host_iso, which is the same rule decimator.py
    states and for the same reason: the CMSIS-DAP bridge delivers in bunches, so host
    arrival time clusters samples the board spaced evenly. Using host_iso here splits single
    bursts at every pause between batches -- on a real capture it turned 17 episodes into
    over a thousand.

    host_iso is still what an episode reports, because that is the clock that lines boards
    up against each other. It is the wrong one to measure spacing with and the only one
    worth showing.
    """
    try:
        return int(row["t_ms"]) / 1000.0
    except (KeyError, TypeError, ValueError):
        return None


def _magnitude(row, names):
    """Vector magnitude over three columns, or None if the row cannot supply them."""
    total = 0.0
    for name in names:
        try:
            value = float(row[name])
        except (KeyError, TypeError, ValueError):
            return None
        total += value * value
    return math.sqrt(total)


class Episode(object):
    """One burst: when it started, how long it lasted, and how hard it was."""

    def __init__(self, board, start, end, rows, peak_g, peak_dps):
        self.board = board
        self.start = start
        self.end = end
        self.rows = rows
        self.peak_g = peak_g
        self.peak_dps = peak_dps

    @property
    def duration_s(self):
        return (self.end - self.start).total_seconds()

    def as_dict(self):
        """JSON-safe. Times as ISO strings, which is how they arrived."""
        return {
            "board": self.board,
            "start": self.start.isoformat(timespec="milliseconds"),
            "end": self.end.isoformat(timespec="milliseconds"),
            "duration_s": round(self.duration_s, 3),
            "rows": self.rows,
            "peak_g": None if self.peak_g is None else round(self.peak_g, 4),
            "peak_dps": None if self.peak_dps is None else round(self.peak_dps, 2),
        }

    def __repr__(self):
        return "<Episode %s %s %.2fs>" % (self.board, self.start, self.duration_s)


def episodes(rows, max_gap_s=MAX_GAP_S):
    """Group a row stream into episodes. Rows must arrive in time order.

    Takes an iterable and holds only the episode being built, so a whole capture never has
    to be resident -- which matters less at a row a minute than it would at 200 Hz, but the
    burst regions are 200 Hz and there is no reason to make the caller think about it.
    """
    found = []
    board = None
    start = None
    last = None
    last_board_s = None
    count = 0
    peak_g = None
    peak_dps = None

    def flush():
        if count:
            found.append(Episode(board, start, last, count, peak_g, peak_dps))

    for row in rows:
        when = row.get(HOST_TIME_COLUMN)
        if when is None:
            continue
        if not row.get(BURST_COLUMN):
            flush()
            board, start, last, count = None, None, None, 0
            last_board_s, peak_g, peak_dps = None, None, None
            continue

        board_s = _board_seconds(row)
        if board_s is None or last_board_s is None:
            # No board clock to compare with -- fall back to arrival time, which is worse
            # but is all there is. A capture without t_ms is not one of ours anyway.
            gap = None if last is None else (when - last).total_seconds()
        else:
            # A negative gap is the board having reset mid-capture, which ends the episode
            # as surely as a long pause does.
            gap = board_s - last_board_s
            if gap < 0:
                gap = max_gap_s + 1.0

        if count and (gap is None or gap > max_gap_s):
            flush()
            board, start, last, count = None, None, None, 0
            last_board_s, peak_g, peak_dps = None, None, None

        if not count:
            board = row.get("board")
            start = when
        last = when
        last_board_s = _board_seconds(row)
        count += 1

        magnitude = _magnitude(row, ACCEL)
        if magnitude is not None and (peak_g is None or magnitude > peak_g):
            peak_g = magnitude
        magnitude = _magnitude(row, GYRO)
        if magnitude is not None and (peak_dps is None or magnitude > peak_dps):
            peak_dps = magnitude

    flush()
    return found


class EventIndex(object):
    """Episodes across the archive, with the scanning done once per file.

    A capture that has not changed cannot have grown new episodes, so its result is kept
    against its size and mtime. Only the file currently being written changes, which means
    a query over a year of archive scans one file and reads the rest out of a dict.
    """

    def __init__(self, store):
        self.store = store
        self._cache = {}
        self.scans = 0          # files actually read, for tests and for a status line
        self.hits = 0

    def _for_capture(self, capture):
        key = capture.path
        stamp = (capture.size, capture.end)
        cached = self._cache.get(key)
        if cached is not None and cached[0] == stamp:
            self.hits += 1
            return cached[1]
        found = episodes(self.store.rows(capture))
        self.scans += 1
        self._cache[key] = (stamp, found)
        return found

    def between(self, start=None, end=None, board=None):
        """Every episode overlapping [start, end], oldest first.

        The window filters episodes as well as files: a capture is chosen because it might
        hold something in range, and most of what it holds will not be.
        """
        out = []
        for capture in self.store.captures(board=board, start=start, end=end):
            for episode in self._for_capture(capture):
                if start is not None and episode.end < start:
                    continue
                if end is not None and episode.start > end:
                    continue
                out.append(episode)
        out.sort(key=lambda one: (one.start, one.board))
        return out
