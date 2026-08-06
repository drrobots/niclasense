#!/usr/bin/env python3
"""Run main.py against a logged CSV instead of the board.

    python testing/replay.py logs/nicla_20260803_223115.csv --plot --duration 20
    python testing/replay.py runs/walk.csv --log-rate 5 --csv /tmp/out.csv

The first argument is the log; everything after it is passed to main.py untouched, so any
capture that works against the board can be run against a recording of one. There is no
board involved and no serial port opened.

This exists because there is no test suite and the usual way to verify a change is to
plug the board in -- which is fine until the change is in the host pipeline and the board
is somewhere else. A recording exercises everything downstream of the source: the
decimator and its bursts, the CSV writer, the socket hub, attached dashboards, the
browser server, and the shutdown paths of all of them. What it cannot exercise is
SerialSource itself -- auto-detect, autobaud, the rate handshake -- since that is exactly
the piece being stood in for.

Timing comes from the file's own t_ms, not from a fixed sleep, so a 200 Hz recording
replays at 200 Hz and a decimated one replays with its gaps intact. Rows are held in
memory, so a very large log costs a pause and a few hundred MB at start-up.

The replay loops. A short recording therefore serves indefinitely, and each wrap sends
t_ms backwards -- which every viewer already has to handle, because that is what a board
reset looks like. Watching the tiles clear on the wrap is the cheapest test of that path
there is.
"""

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import main  # noqa: E402
from columns import COLUMNS, CSV_COLUMNS, PARSERS  # noqa: E402
from sources import _ThreadedSource  # noqa: E402


class ReplaySource(_ThreadedSource):
    """A source that reads a finished CSV, pretending to be the board that wrote it.

    Interchangeable with SerialSource in the only ways main.py cares about: it fills
    `queue` with sample tuples from a thread, carries the same counters, and describes
    itself. The describe() string keeps the board's banner shape -- StreamSource parses it
    at connect and viewers show it in their header -- but names the file, so a dashboard
    attached to a replay never looks like one attached to hardware.
    """

    def __init__(self, path):
        _ThreadedSource.__init__(self)
        self.path = path
        # The banner values a viewer trusts. The rate is the board's, not a measurement:
        # what actually comes out depends on the recording's own spacing.
        self.stream_hz = 200
        self.reported_baud = 1000000
        self.rows = self._load(path)

    @staticmethod
    def _load(path):
        """Every parsable row of a log, as the board's 27 columns.

        The logger prepends host_iso and may append burst, so the board's columns are the
        slice between them. Unparsable rows are skipped rather than fatal: a log torn off
        mid-write by a Ctrl-C ends in a partial line, and refusing to replay a capture
        because of its last row would be a poor trade.
        """
        rows = []
        with open(path) as handle:
            header = handle.readline().strip().split(",")
            offset = 1 if header[:1] == [CSV_COLUMNS[0]] else 0
            for line in handle:
                fields = line.strip().split(",")[offset:offset + len(COLUMNS)]
                if len(fields) != len(COLUMNS):
                    continue
                try:
                    rows.append(tuple(p(f) for p, f in zip(PARSERS, fields)))
                except ValueError:
                    continue
        if not rows:
            raise SystemExit("no usable sample rows in %s" % path)
        return rows

    def open(self):
        print("replaying %d rows from %s" % (len(self.rows), self.path))
        return self

    def describe(self):
        return "Replay(%s): nicla-stream v3 rate_hz=%d baud=%d columns=%d" % (
            os.path.basename(self.path), self.stream_hz, self.reported_baud, len(COLUMNS),
        )

    def _run(self):
        """Emit rows on the schedule the board emitted them.

        Each row is due at its own t_ms offset from the first, measured against a
        wall-clock start rather than accumulated sleeps -- otherwise every sleep's
        overshoot adds up and a long replay drifts slower and slower behind the rate it
        claims to be running at.
        """
        t_index = COLUMNS.index("t_ms")
        while not self._stop.is_set():
            start = time.time()
            base = self.rows[0][t_index]
            for row in self.rows:
                if self._stop.is_set():
                    return
                delay = start + (row[t_index] - base) / 1000.0 - time.time()
                if delay > 0:
                    time.sleep(delay)
                self._emit(row)


def main_(argv):
    if not argv or argv[0].startswith("-"):
        print(__doc__.strip().splitlines()[0], file=sys.stderr)
        print("usage: replay.py LOG.csv [main.py args...]", file=sys.stderr)
        return 2
    path = argv[0]
    if not os.path.exists(path):
        print("error: no such log: %s" % path, file=sys.stderr)
        return 1
    # The one line that makes this work: main.py builds its source through this function,
    # so replacing it is the whole of the substitution. Nothing in main.py knows.
    main.create_source = lambda args: ReplaySource(path)
    return main.main(argv[1:])


if __name__ == "__main__":
    sys.exit(main_(sys.argv[1:]))
