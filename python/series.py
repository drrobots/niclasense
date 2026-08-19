"""Rows to something a plot can draw.

The whole problem is that one capture holds two densities. At rest the fleet writes a row a
minute; while something is moving it writes two hundred a second. Over an afternoon that is
a few hundred steady points with occasional walls of thousands, and the naive answers are
both wrong: send everything and the browser parses megabytes to draw a line eight hundred
pixels wide, send every Nth row and the bursts either vanish or swamp the rest.

So: bucket the window into as many buckets as there are pixels, and carry the minimum and
maximum of each. An envelope keeps the extremes, which for burst data is the entire content
-- a spike that lasted 5 ms is still a spike in the bucket it landed in. Taking every Nth
row would have thrown it away precisely because it was brief.

Empty buckets are dropped rather than sent as gaps. A row a minute across an hour fills one
bucket in thirteen, and a series with twelve nulls between every point draws as dust rather
than as a line. Dropping them means a sparse window comes back as its own raw points, which
is what it should have been all along, and the same code path produced it.
"""

import math

from columns import BURST_COLUMN, HOST_TIME_COLUMN
from logstore import PLOT_TIME

# Buckets in a full-width plot. The client sends its own; this is what it gets for not
# saying. Above a few thousand the envelope stops being a saving and starts being a way to
# send a large amount of data slowly.
DEFAULT_WIDTH = 900
MAX_WIDTH = 4000


def epoch(when):
    """Seconds since the epoch, reading the naive stamp as local time.

    Which is what it is: `logger.py` writes `datetime.now()` with no zone, and the browser
    reads a zoneless ISO string as local too, so the two agree as long as they are on the
    same clock. A archive spanning timezones would need the logger changed first -- see the
    note in logstore.py.
    """
    return when.timestamp()


class Bucket(object):
    __slots__ = ("t", "lo", "hi", "burst", "rows")

    def __init__(self, t):
        self.t = t
        self.lo = {}
        self.hi = {}
        self.burst = 0
        self.rows = 0

    def add(self, when, values, burst):
        self.rows += 1
        if burst:
            self.burst = 1
        for name, value in values.items():
            current = self.lo.get(name)
            if current is None or value < current:
                self.lo[name] = value
            current = self.hi.get(name)
            if current is None or value > current:
                self.hi[name] = value


def _value(row, name):
    try:
        return float(row[name])
    except (KeyError, TypeError, ValueError):
        return None


def bucket_rows(rows, columns, start=None, end=None, width=DEFAULT_WIDTH):
    """Reduce rows to at most `width` buckets, keeping each one's extremes.

    `start` and `end` fix the bucket grid. Without them the grid is taken from the rows
    themselves, which means two boards asked for separately would be bucketed on different
    grids -- fine for drawing one series, wrong for comparing two, so the caller that cares
    passes the window it asked for.

    Returns (buckets, seen) where `seen` is how many rows went in, which is the number the
    client needs to know whether it is looking at everything or at an envelope of it.
    """
    width = max(1, min(int(width), MAX_WIDTH))
    rows = iter(rows)

    first = None
    kept = []
    seen = 0

    lo_t = epoch(start) if start is not None else None
    hi_t = epoch(end) if end is not None else None

    for row in rows:
        # The board-anchored clock when there is one. Inside a burst host_iso has no
        # resolution to speak of -- see the note on LogStore.rows.
        when = row.get(PLOT_TIME) or row.get(HOST_TIME_COLUMN)
        if when is None:
            continue
        seen += 1
        moment = epoch(when)
        if first is None:
            first = moment
        values = {}
        for name in columns:
            value = _value(row, name)
            if value is not None and not math.isnan(value):
                values[name] = value
        kept.append((moment, values, row.get(BURST_COLUMN, 0)))

    if not kept:
        return [], 0

    if lo_t is None:
        lo_t = kept[0][0]
    if hi_t is None:
        hi_t = kept[-1][0]
    span = hi_t - lo_t

    # A window with no width -- one instant, or one row -- would divide by zero deciding
    # which bucket anything belongs in. Everything goes in the first one.
    step = (span / float(width)) if span > 0 else None

    buckets = []
    index_of = {}
    for moment, values, burst in kept:
        if step is None:
            index = 0
        else:
            index = int((moment - lo_t) / step)
            if index >= width:
                index = width - 1     # the row sitting exactly on the far edge
            if index < 0:
                index = 0
        bucket = index_of.get(index)
        if bucket is None:
            bucket = Bucket(moment)
            index_of[index] = bucket
            buckets.append((index, bucket))
        bucket.add(moment, values, burst)

    buckets.sort(key=lambda pair: pair[0])
    return [bucket for _index, bucket in buckets], seen


def as_payload(buckets, columns, seen):
    """The buckets as parallel arrays, which is the shape a plotting library wants.

    One array of times, and per column an array of minima and an array of maxima. When a
    bucket held a single row those two are equal and the band it describes collapses to the
    line it should be, so the client needs no separate case for a sparse window.
    """
    times = [round(bucket.t, 3) for bucket in buckets]
    out = {}
    for name in columns:
        lo = []
        hi = []
        for bucket in buckets:
            lo.append(bucket.lo.get(name))
            hi.append(bucket.hi.get(name))
        if any(value is not None for value in lo):
            out[name] = {"min": lo, "max": hi}
    return {
        "t": times,
        "columns": out,
        "burst": [bucket.burst for bucket in buckets],
        "buckets": len(buckets),
        "rows": seen,
        # True when more than one row landed in some bucket, which is the client's cue that
        # it is looking at an envelope rather than at every sample.
        "downsampled": any(bucket.rows > 1 for bucket in buckets),
    }
