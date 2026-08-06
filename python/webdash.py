#!/usr/bin/env python3
"""Browser dashboard for a capture that is already running.

The same job dashboard.py does, in a browser instead of a window. main.py owns the board
and the CSV and serves every sample on a TCP socket. This attaches to that socket and
re-serves it over HTTP, and the page it hands out draws the tiles.

Unlike the matplotlib dashboard, the window length is not a setting of this process: every
tab keeps its own buffers and picks its own window, so two people can watch the same
capture over different spans. Which is why there is no --window or --fps here.

Nothing goes back to the board. This attaches, reads, and draws -- there is no control
channel, by design, exactly as with dashboard.py.

Examples:
    python main.py                           # in one terminal: the capture
    python webdash.py                        # in another: this, then open the URL
    python webdash.py --open                 # ... and open a browser at it
    python webdash.py 8790 --http-port 9100  # a capture elsewhere, served elsewhere
"""

import argparse
import sys
import time
import webbrowser

from hub import DEFAULT_ENDPOINT, parse_endpoint
from pipeline import attached_status, make_drain
from sources import SourceError, StreamSource
from webhub import DEFAULT_HTTP_PORT, WebHub, build_spec

# How often the capture's status is republished to open tabs. The capture itself publishes
# roughly once a second (main.py's run_headless), so anything faster only resends what we
# already have.
STATUS_INTERVAL = 1.0


def serve(source, drain, hub, status):
    """Move samples from the source to the browsers until the capture goes away.

    dashboard.py hands this job to matplotlib -- plt.show() owns the thread and the
    animation timer calls the drain. There is no such loop here: the HTTP server has its
    own threads and this one is free, so it does what run_headless does, which is poll the
    source and drain on a short sleep.
    """
    last_status = 0.0
    try:
        while True:
            if source.error is not None:
                # Reported through the exit summary rather than raised. A capture that
                # stops is how this program normally ends -- it is a viewer, and the
                # process holding the board is entitled to finish first -- so it should
                # not go out on a traceback.
                sys.stderr.write("\n%s\n" % source.error)
                break
            if not source.running:
                sys.stderr.write("\nsource stopped\n")
                break
            drain()
            now = time.time()
            if now - last_status >= STATUS_INTERVAL:
                hub.push_status(status())
                last_status = now
            time.sleep(0.02)
    except KeyboardInterrupt:
        pass
    finally:
        drain()


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Serve a browser dashboard for a Nicla capture running elsewhere.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "endpoint", nargs="?", default=DEFAULT_ENDPOINT,
        help="Capture to attach to, as HOST:PORT, a bare port, or a bare host.",
    )
    parser.add_argument(
        "--http-port", type=int, default=DEFAULT_HTTP_PORT,
        help="Port to serve the dashboard on.",
    )
    parser.add_argument(
        "--open", action="store_true", help="Open a browser at the dashboard.",
    )
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

    # stream_hz comes out of the board's banner, which the logger forwards, so the page
    # sizes its ring buffers for the rate actually being served rather than a guess.
    spec = build_spec(sample_hz=source.stream_hz or 200.0, source=source.describe())
    # Bound to 127.0.0.1 and not configurable: this serves an unauthenticated live feed of
    # someone's sensor data, and a --bind flag is an invitation to put that on a network.
    hub = WebHub(port=args.http_port, spec=spec)
    try:
        hub.start()
    except OSError as exc:
        print("error: %s" % exc, file=sys.stderr)
        source.stop()
        return 1

    # The capture tile is filled from the status the capture publishes over the same
    # connection, so the page reports the same rows, bursts and CSV path the capture would
    # report about itself.
    drain = make_drain(source, [hub.broadcast])
    status = attached_status(source)

    print("serving %s -- Ctrl-C to stop; the capture keeps running" % hub.url())
    print("every tab gets its own buffers, window and theme")
    if args.open:
        webbrowser.open(hub.url())

    try:
        serve(source, drain, hub, status)
    finally:
        # Tabs are told before the socket goes, so an open page says the capture ended
        # rather than sitting on a frozen dashboard wondering.
        hub.push_event("ended", {"reason": str(source.error) if source.error else "stopped"})
        time.sleep(0.1)
        hub.stop()
        source.stop()
        if hub.served:
            print("served %d browser connection(s)" % hub.served)
        if hub.client_drops:
            print("dropped %d rows to browsers that fell behind" % hub.client_drops)
        # This viewer's own losses, not the capture's -- those belong to the process that
        # owns the CSV and are reported there.
        if source.dropped:
            print("dropped %d samples on the way here (this process fell behind)"
                  % source.dropped)
        if source.malformed:
            print("skipped %d malformed lines" % source.malformed)
        if source.error is not None:
            print("detached: %s" % source.error, file=sys.stderr)
    return 1 if source.error is not None else 0


if __name__ == "__main__":
    sys.exit(main())
