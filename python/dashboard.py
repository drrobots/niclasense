#!/usr/bin/env python3
"""Live dashboard for a capture that is already running.

main.py owns the board and the CSV; run it with --listen and it serves every sample on a
TCP socket. This connects to that socket and plots it, in the same tiles the all-in-one
run uses. Close the window whenever you like -- the capture does not notice, and you can
open another.

It is a separate program rather than a flag on main.py because it shares none of the
capture's settings: no port, no baud, no CSV, no log rate. Those belong to the process
holding the board.

Examples:
    python main.py --no-plot --listen        # in one terminal: the capture
    python dashboard.py                      # in another: this
    python dashboard.py 8790                 # a capture on a different port
    python dashboard.py bench.local:8765     # a capture on another machine
"""

import argparse
import sys

from hub import DEFAULT_ENDPOINT, parse_endpoint
from pipeline import attached_status, make_drain, watch_source
from plot import LivePlot
from sources import SourceError, StreamSource


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Plot a Nicla capture that is running elsewhere with --listen.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "endpoint", nargs="?", default=DEFAULT_ENDPOINT,
        help="Capture to attach to, as HOST:PORT, a bare port, or a bare host.",
    )
    parser.add_argument("--window", type=float, default=30.0, help="Plot window, seconds.")
    parser.add_argument("--fps", type=float, default=20.0, help="Plot refresh rate.")
    args = parser.parse_args(argv)

    try:
        host, port = parse_endpoint(args.endpoint)
    except ValueError:
        print("error: %r is not a HOST:PORT" % args.endpoint, file=sys.stderr)
        return 1

    source = StreamSource(host=host, port=port)
    try:
        source.open()
    except SourceError as exc:
        print("error: %s" % exc, file=sys.stderr)
        return 1

    print("attached to %s" % source.describe())
    source.start()

    # The capture tile is filled from the status the capture publishes over the same
    # connection, so this window reports the same rows, bursts and CSV path the capture
    # would report about itself.
    plot = LivePlot(
        window=args.window, fps=args.fps,
        title=source.describe(), status=attached_status(source),
    )
    drain = make_drain(source, [plot.add])

    print("close the window to detach; the capture keeps running")
    try:
        plot.run(drain=watch_source(source, drain, plot))
    finally:
        source.stop()
        # This viewer's own losses, not the capture's -- those belong to the process that
        # owns the CSV and are reported there.
        if source.dropped:
            print("dropped %d samples on the way here (this plot fell behind)"
                  % source.dropped)
        if source.malformed:
            print("skipped %d malformed lines" % source.malformed)
        if source.error is not None:
            print("detached: %s" % source.error, file=sys.stderr)
    return 1 if source.error is not None else 0


if __name__ == "__main__":
    sys.exit(main())
