#!/usr/bin/env python3
"""Log the Nicla Sense ME sensor stream to CSV and plot it live.

This is the capture: it owns the serial port, the decimator and the CSV. It can plot what
it is capturing, but it does not have to -- --listen serves the live stream on a TCP
socket, and dashboard.py plots that from a separate process. So a capture can run headless
for hours while plots come and go over it, without the logging ever noticing. See hub.py
for the protocol.

Every switch is also settable from an INI file (--config); see config.py and
example.conf. The command line still wins, so a file describes a standing setup and
flags vary one run of it.

Examples:
    python main.py                                   # auto-detect port, log + plot
    python main.py --csv runs/walk.csv --window 60
    python main.py --no-plot --duration 15           # headless capture
    python main.py --no-plot --listen                # headless, plot it with dashboard.py
    python main.py --config overnight.conf           # a setup that lives in a file
    python main.py --config overnight.conf --window 60   # ...with one thing changed
    python main.py --list-ports
"""

import argparse
import datetime
import os
import sys
import time

import config
from decimator import AdaptiveDecimator
from hub import DEFAULT_ENDPOINT, SampleHub, parse_endpoint
from logger import CsvLogger
from pipeline import capture_status, make_drain, make_log_sink, watch_source
from sources import (
    SerialControl,
    SerialSource,
    SourceError,
    create_source,
    list_serial_ports,
)


def build_parser():
    parser = argparse.ArgumentParser(
        description="Stream, log, and plot Nicla Sense ME sensor data.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "--config", default=None, metavar="FILE",
        help="INI file setting any of the options below. Anything also given on the "
             "command line wins, except --burst-on, which adds to the file's triggers.",
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
        help="Serve the live stream on this address (default %s) for dashboard.py to "
             "plot. With --no-plot this is a headless capture you can plot on demand."
             % DEFAULT_ENDPOINT,
    )
    parser.add_argument("--list-ports", action="store_true", help="List serial ports and exit.")
    return parser


def parse_args(argv=None):
    """Command line over config file over defaults.

    Two passes: --config is read on its own first, because the file's values become the
    parser's defaults, and a default has to exist before the arguments that override it
    are parsed. Loading is driven by the parser itself, so a flag added above needs no
    corresponding change in config.py.
    """
    parser = build_parser()

    finder = argparse.ArgumentParser(add_help=False)
    finder.add_argument("--config", default=None)
    preview, _rest = finder.parse_known_args(argv)

    if preview.config is not None:
        try:
            parser.set_defaults(**config.load(preview.config, parser))
        except config.ConfigError as exc:
            # Through parser.error so a bad file fails the way a bad flag does: usage,
            # message, exit 2 -- rather than a traceback.
            parser.error(str(exc))

    return parser.parse_args(argv)


def default_csv_path():
    stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    return os.path.join("logs", "nicla_%s.csv" % stamp)


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

    if args.config is not None:
        print("config from %s" % args.config)

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

    csv_path = args.csv if args.csv is not None else default_csv_path()
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
        print("serving samples on %s -- plot it with: python dashboard.py %s"
              % (hub.describe(), hub.describe()))

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
