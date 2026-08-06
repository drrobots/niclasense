#!/usr/bin/env python3
"""Log the Nicla Sense ME sensor stream to CSV and plot it live.

The capture and the plot are separable. --listen makes the logger serve its samples on a
TCP socket; --attach plots a logger that is already running. So a long capture can run
headless for hours and you can open a plot on it, close the plot, and open another,
without the logging ever noticing -- see hub.py for the protocol.

Examples:
    python main.py                                   # auto-detect port, log + plot
    python main.py --csv runs/walk.csv --window 60
    python main.py --no-plot --duration 15           # headless capture
    python main.py --no-plot --listen                # headless logger, viewers welcome
    python main.py --attach                          # plot that logger, then walk away
    python main.py --list-ports
"""

import argparse
import datetime
import os
import queue
import sys
import time

from decimator import AdaptiveDecimator
from hub import DEFAULT_ENDPOINT, SampleHub, parse_endpoint
from logger import CsvLogger
from sources import (
    SerialControl,
    SerialSource,
    SourceError,
    create_source,
    list_serial_ports,
)


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description="Stream, log, and plot Nicla Sense ME sensor data.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--port", default=None, help="Serial port (default: auto-detect).")
    parser.add_argument(
        "--baud", type=int, default=1000000,
        help="Serial baud rate to try first (the sketch boots at 1000000).",
    )
    parser.add_argument(
        "--no-autobaud", action="store_true",
        help="Fail rather than trying other baud rates when --baud yields nothing.",
    )
    parser.add_argument(
        "--rate", type=int, default=0,
        help="Ask the board to stream at this many Hz on connect (0 = leave it alone).",
    )
    parser.add_argument(
        "--csv", default=None,
        help="CSV to append to (default: logs/nicla_<timestamp>.csv). Use 'none' to skip logging.",
    )
    parser.add_argument(
        "--log-rate", type=float, default=0.0,
        help="Write the CSV at this many Hz instead of every sample "
             "(0 = log everything). Bursts still record at the full stream rate.",
    )
    parser.add_argument(
        "--burst-on", action="append", default=None, metavar="COL:THRESH",
        help="Trigger a full-rate burst when COL leaves its baseline by more than "
             "THRESH. Repeatable. Default: the accelerometer and gyroscope axes.",
    )
    parser.add_argument(
        "--burst-hold", type=float, default=1.0,
        help="Seconds of full-rate logging after the last trigger.",
    )
    parser.add_argument(
        "--burst-pre", type=float, default=0.25,
        help="Seconds of full-rate history kept from before each trigger.",
    )
    parser.add_argument(
        "--burst-tau", type=float, default=0.5,
        help="Baseline time constant, seconds. Longer = slower to accept a new resting "
             "value, so sustained motion keeps triggering for longer.",
    )
    parser.add_argument("--window", type=float, default=30.0, help="Plot window, seconds.")
    parser.add_argument("--fps", type=float, default=20.0, help="Plot refresh rate.")
    parser.add_argument("--no-plot", action="store_true", help="Log only, no plot window.")
    parser.add_argument(
        "--duration", type=float, default=0.0,
        help="Stop after this many seconds (0 = until interrupted). Headless mode only.",
    )
    parser.add_argument(
        "--listen", nargs="?", const=DEFAULT_ENDPOINT, default=None, metavar="HOST:PORT",
        help="Serve the live stream to attached plots on this address (default %s). "
             "With --no-plot this is a headless logger you can plot on demand."
             % DEFAULT_ENDPOINT,
    )
    parser.add_argument(
        "--attach", nargs="?", const=DEFAULT_ENDPOINT, default=None, metavar="HOST:PORT",
        help="Plot a logger that is already running with --listen (default %s) instead "
             "of opening the serial port. This process does not log." % DEFAULT_ENDPOINT,
    )
    parser.add_argument("--list-ports", action="store_true", help="List serial ports and exit.")
    args = parser.parse_args(argv)

    if args.attach:
        # Everything about the capture belongs to the logger that owns the board. Quietly
        # ignoring these would be worse: --csv in particular reads like it would write a
        # second copy of the log, and it never could.
        owned = [
            name for name in ("port", "rate", "csv", "log_rate", "burst_on", "listen")
            if getattr(args, name) not in (None, 0, 0.0)
        ]
        if args.no_plot:
            owned.append("no_plot")
        if owned:
            parser.error(
                "--attach only plots; %s belong%s to the logger you are attaching to."
                % (", ".join("--" + n.replace("_", "-") for n in sorted(owned)),
                   "" if len(owned) > 1 else "s")
            )
    return args


def default_csv_path():
    stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    return os.path.join("logs", "nicla_%s.csv" % stamp)


def make_log_sink(log, decimator=None):
    """Return a sink that writes samples to the CSV, thinned if we are decimating."""

    if decimator is None:
        return log.write

    def sink(sample):
        for kept, is_burst in decimator.feed(sample):
            log.write(kept, is_burst)

    return sink


def make_drain(source, sinks):
    """Return a function that moves everything queued to each sink in turn.

    Sinks are plain callables taking one sample, which is what keeps the logger and the
    plot independent of each other: the logger's sink is the CSV, the plot's is its ring
    buffers, the hub's is every attached viewer, and no sink knows the others exist.

    Only the CSV sink is ever thinned. Decimation is about the size of the file on disk,
    not about how much the host is willing to look at, so plots -- local or attached --
    always see every sample.
    """

    sinks = tuple(s for s in sinks if s is not None)

    def drain():
        moved = 0
        # Bounded so a backlog can never starve the caller (the animation timer).
        while moved < 5000:
            try:
                sample = source.queue.get_nowait()
            except queue.Empty:
                break
            for sink in sinks:
                sink(sample)
            moved += 1
        return moved

    return drain


def capture_status(source, log, decimator, csv_path, hub=None):
    """The facts the capture tile shows, as a dict.

    Named rather than inlined because the headless logger publishes exactly this dict to
    attached viewers, so a remote plot and a local one report the same capture the same
    way.
    """
    status = {
        "source": source.describe(),
        "csv": csv_path if log is not None else None,
        "rows": log.rows_written if log is not None else 0,
        "dropped": source.dropped,
        "malformed": getattr(source, "malformed", 0),
        "log_rate": decimator.rate if decimator is not None else 0,
        "bursts": decimator.bursts if decimator is not None else 0,
        "bursting": decimator.bursting if decimator is not None else False,
    }
    if hub is not None:
        status["viewers"] = hub.clients
    return status


def attached_status(source):
    """The logger's published status, plus what only this process can know.

    Loss is counted at both ends and means different things -- the logger's rows never
    reached the CSV, ours never reached this window -- but the tile answers one question,
    "is what I am looking at complete", and the honest answer there is the sum. The two
    are kept as separate keys as well, and the exit summary reports this viewer's own
    count on its own, so which end is struggling stays recoverable.
    """

    def status():
        published = dict(source.status)
        capture_dropped = published.get("dropped", 0)
        published["source"] = source.describe()
        published["capture_dropped"] = capture_dropped
        published["link_dropped"] = source.dropped
        published["dropped"] = capture_dropped + source.dropped
        published["malformed"] = published.get("malformed", 0) + source.malformed
        return published

    return status


def watch_source(source, drain, plot):
    """Wrap drain so a source that dies closes the plot window instead of freezing it.

    run_headless() has always checked source.error on every pass; under matplotlib the
    animation callback is the only place left to check it from.
    """
    import matplotlib.pyplot as plt

    def guarded():
        moved = drain()
        if source.error is not None or not source.running:
            reason = source.error if source.error is not None else "source stopped"
            sys.stderr.write("\n%s\n" % reason)
            plt.close(plot.figure)
        return moved

    return guarded


def run_headless(source, drain, duration, hub=None, status=None):
    start = time.time()
    last_report = start
    total = 0
    try:
        while True:
            if source.error is not None:
                raise source.error
            if not source.running:
                sys.stderr.write("\nsource stopped\n")
                break
            total += drain()
            now = time.time()
            elapsed = now - start
            if now - last_report >= 1.0:
                if hub is not None and status is not None:
                    hub.push_status(status())
                sys.stderr.write(
                    "\r%6.1f s   %7d rows   %5.1f Hz%s"
                    % (
                        elapsed,
                        total,
                        total / elapsed if elapsed else 0.0,
                        "   %d viewer(s)" % hub.clients if hub is not None else "",
                    )
                )
                sys.stderr.flush()
                last_report = now
            if duration and elapsed >= duration:
                break
            time.sleep(0.02)
    except KeyboardInterrupt:
        pass
    finally:
        total += drain()
        sys.stderr.write("\n")


def main(argv=None):
    args = parse_args(argv)

    if args.list_ports:
        ports = list_serial_ports()
        if not ports:
            print("No serial ports found.")
        for device, description in ports:
            print("%-40s %s" % (device, description))
        return 0

    source = create_source(args)
    try:
        source.open()
    except SourceError as exc:
        print("error: %s" % exc, file=sys.stderr)
        return 1

    decimator = None
    if args.log_rate:
        try:
            decimator = AdaptiveDecimator(
                rate=args.log_rate,
                triggers=args.burst_on,
                hold=args.burst_hold,
                pre_roll=args.burst_pre,
                tau=args.burst_tau,
                burst_rate=args.rate or 200.0,
            )
        except ValueError as exc:
            print("error: %s" % exc, file=sys.stderr)
            return 1

    # Attached mode plots someone else's capture: that process owns the port, the CSV and
    # the decimator, and this one is a window onto it.
    csv_path = "none" if args.attach else (
        args.csv if args.csv is not None else default_csv_path()
    )
    log = None
    if csv_path.lower() != "none":
        # Flush about once a second either way; the default of every 200 rows is 40 s
        # apart at 5 Hz, which makes a tail -f look stalled.
        flush_every = max(1, int(args.log_rate)) if decimator else 200
        log = CsvLogger(
            csv_path, flush_every=flush_every, mark_bursts=decimator is not None
        ).open()
        print("logging to %s" % csv_path)
        if decimator is not None:
            print(
                "steady rate %g Hz, bursting to full rate for %.2gs after a trigger on %s"
                % (
                    decimator.rate,
                    decimator.hold,
                    ", ".join(
                        "%s>%g" % (name, thr) for _i, name, thr in decimator.triggers
                    ),
                )
            )
    print("reading from %s" % source.describe())

    source.start()

    # Routed through SerialControl rather than the source so the link-budget clamp that
    # protects the GUI buttons protects the flag too.
    if args.rate and isinstance(source, SerialSource):
        print(SerialControl(source).set_rate(args.rate))
        if decimator is not None and args.rate < decimator.rate:
            print(
                "warning: the board is streaming at %d Hz, so bursts cannot exceed that"
                % args.rate,
                file=sys.stderr,
            )

    hub = None
    if args.listen:
        host, port = parse_endpoint(args.listen)
        # describe() rather than a captured string: a viewer attaching after a rate change
        # should be told what the board is doing now.
        hub = SampleHub(host=host, port=port, banner=source.describe)
        try:
            hub.start()
        except OSError as exc:
            print("error: %s" % exc, file=sys.stderr)
            source.stop()
            if log is not None:
                log.close()
            return 1
        print("serving samples on %s -- attach with: python main.py --attach %s"
              % (hub.describe(), hub.describe()))

    if args.attach:
        status = attached_status(source)
    else:
        def status():
            return capture_status(source, log, decimator, csv_path, hub)

    plot = None
    if not args.no_plot:
        from plot import LivePlot

        plot = LivePlot(
            window=args.window,
            fps=args.fps,
            title=source.describe(),
            status=status,
        )

    drain = make_drain(source, [
        make_log_sink(log, decimator) if log is not None else None,
        plot.add if plot is not None else None,
        hub.broadcast if hub is not None else None,
    ])

    try:
        if plot is not None:
            print("close the plot window to stop")
            # A dead source has to end plt.show() itself: matplotlib owns the main thread
            # from here, and without this an unplugged board or a logger that exited
            # leaves a window quietly frozen on its last frame.
            plot.run(drain=watch_source(source, drain, plot))
            drain()
        else:
            run_headless(source, drain, args.duration, hub, status)
    finally:
        source.stop()
        if hub is not None:
            hub.stop()
        rows = log.rows_written if log is not None else 0
        if log is not None:
            log.close()
            print("wrote %d rows to %s" % (rows, csv_path))
        if decimator is not None and log is not None:
            print(decimator.summary())
        if getattr(source, "malformed", 0):
            print("skipped %d malformed lines" % source.malformed)
        if source.dropped:
            print("dropped %d samples (consumer fell behind)" % source.dropped)
        if hub is not None and hub.client_drops:
            print("dropped %d samples to viewers that fell behind" % hub.client_drops)
        if source.error is not None:
            print("source error: %s" % source.error, file=sys.stderr)
    # Outside the finally: a return there would swallow an in-flight exception.
    if source.error is not None:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
