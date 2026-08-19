#!/usr/bin/env python3
"""The dashboard. Attaches to a capture that is already running and draws it in a browser.

main.py owns the board and the CSV and serves every sample on a TCP socket. This attaches
to that socket and re-serves it over HTTP, and the page it hands out draws the tiles. It
is the only live viewer in the project -- there was a matplotlib one, and this replaced it
outright: server-rendered vectors in a browser are smoother than a Python process pushing
artists around, and everything awkward about a GUI event loop sharing an interpreter with
a capture went with it.

The window length is not a setting of this process: every tab keeps its own buffers and
picks its own window and theme, so two people can watch the same capture over different
spans. Which is why there is no --window or --fps here, and why main.py has none to pass.

Nothing goes back to the board. This attaches, reads, and draws -- there is no control
channel, by design.

Examples:
    python main.py --plot                    # the capture starts this for you
    python main.py                           # in one terminal: the capture
    python webdash.py                        # in another: this, then open the URL
    python webdash.py --open                 # ... and open a browser at it
    python webdash.py 8790 --http-port 9100  # a capture elsewhere, served elsewhere
    python webdash.py --http-host 0.0.0.0    # ... and let other machines open it
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

    The HTTP server has its own threads and this one is free, so it does what
    main.py's run_headless does: poll the source and drain on a short sleep. The
    matplotlib dashboard this replaced could not -- plt.show() owned the thread, so the
    drain had to be called from an animation timer and a dead source had to be noticed
    from inside a draw callback.
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
        "--http-host", default="127.0.0.1", metavar="ADDR",
        help="Address to serve the dashboard on. The default keeps it on this machine; "
             "give a LAN address (or 0.0.0.0) to let other machines open it.",
    )
    parser.add_argument(
        "--allow-host", action="append", default=[], metavar="NAME",
        help="A host name the dashboard will answer to, beyond localhost and bare "
             "addresses. Needed only if you reach it by name. Repeatable.",
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
    # Bound to 127.0.0.1 by default, and that default is the security model: this serves an
    # unauthenticated live feed of someone's sensor data, and nothing here asks who is
    # asking. --http-host exists because reaching the dashboard from another machine is a
    # real need that the alternatives answer badly -- a second Python install on every
    # viewing machine, or an SSH tunnel per viewer -- but it is a deliberate act, not a
    # default, and moving it off loopback puts the feed in front of everyone who can route
    # to this port.
    #
    # What comes with it is the Host allowlist in webhub.host_allowed(). Binding a LAN
    # address would otherwise expose the dashboard to any page the person at this machine
    # happens to visit, via DNS rebinding, which is a much wider audience than "the
    # network" and not one anybody means to invite.
    hub = WebHub(host=args.http_host, port=args.http_port, spec=spec,
                 allowed_hosts=args.allow_host)
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
    if hub.public:
        # Worth a line on stdout every time. A dashboard that is quietly reachable from
        # the rest of the network is exactly the thing someone should be reminded of.
        print("reachable from the network on %s -- there is no password on it" % args.http_host)
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
