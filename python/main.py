#!/usr/bin/env python3
"""Log the Nicla Sense ME sensor stream to CSV and serve it over TCP.

This is the capture: it owns the serial port, the decimator and the CSV, and it is always
headless. Every sample goes out on a TCP socket as well as to the file (see hub.py for the
protocol), and drawing is somebody else's process -- dashboard.py for a window, webdash.py
for a browser, `nc` for the raw rows.

There is no in-process plot, deliberately. A window that shares this interpreter shares
its GIL and its lifetime with the logging, which is the wrong coupling for something meant
to run for hours: the capture is the durable thing and viewers come and go over the
socket, here or on another machine. --plot is only a convenience for the common case of
wanting one immediately -- it starts dashboard.py as a *child* pointed at our own socket,
so it attaches like any other viewer and closing it leaves the capture running.

Every switch is also settable from an INI file (--config); see config.py and
example.conf. The command line still wins, so a file describes a standing setup and
flags vary one run of it.

Examples:
    python main.py                                   # log and serve; attach when you like
    python main.py --plot                            # ...and open a dashboard on it now
    python main.py --csv runs/walk.csv --duration 15
    python main.py --listen 0.0.0.0:8790             # serve somewhere else
    python main.py --config overnight.conf           # a setup that lives in a file
    python main.py --config overnight.conf --log-rate 1   # ...with one thing changed
    python main.py --list-ports
"""

import argparse
import datetime
import os
import subprocess
import sys
import time

import config
from decimator import AdaptiveDecimator
from hub import DEFAULT_ENDPOINT, SampleHub, parse_endpoint
from logger import CsvLogger
from pipeline import capture_status, make_drain, make_log_sink
from sources import (
    SerialControl,
    SerialSource,
    SourceError,
    create_source,
    list_serial_ports,
)


def build_parser():
    parser = argparse.ArgumentParser(
        description="Log Nicla Sense ME sensor data to CSV and serve it to viewers.",
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
    parser.add_argument(
        "--plot", action="store_true",
        help="Also start dashboard.py, attached to this capture over the socket. "
             "Closing its window detaches it; the capture keeps going.",
    )
    parser.add_argument(
        "--window", type=float, default=30.0,
        help="Plot window, seconds. Passed to the dashboard --plot starts.",
    )
    parser.add_argument(
        "--fps", type=float, default=20.0,
        help="Plot refresh rate. Passed to the dashboard --plot starts.",
    )
    parser.add_argument(
        "--duration", type=float, default=0.0,
        help="Stop after this many seconds (0 = until interrupted).",
    )
    parser.add_argument(
        "--listen", default=DEFAULT_ENDPOINT, metavar="HOST:PORT",
        help="Address to serve the live stream on. Serving is not optional -- it is how "
             "anything else sees this capture -- but where is.",
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


def launch_viewer(endpoint, window, fps):
    """Start dashboard.py against our own socket. Returns the process, or None.

    A child rather than an import: matplotlib in this interpreter would put a GUI event
    loop on the thread that has to keep draining the serial queue, which is the coupling
    the split exists to remove. Over the socket it is an ordinary viewer that happens to
    have been started for you, and it can be closed, reopened, or replaced by webdash.py
    without the capture being involved.

    A dashboard that will not start is reported and shrugged off. The samples are already
    reaching the CSV and the socket by the time we get here, and killing a good capture
    because its convenience window failed would be the wrong trade.
    """
    script = os.path.join(os.path.dirname(os.path.abspath(__file__)), "dashboard.py")
    argv = [sys.executable, script, endpoint, "--window", "%g" % window, "--fps", "%g" % fps]
    try:
        return subprocess.Popen(argv)
    except OSError as exc:
        print("warning: could not start dashboard.py (%s); the capture continues" % exc,
              file=sys.stderr)
        return None


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

    try:
        host, port = parse_endpoint(args.listen)
    except ValueError:
        print("error: --listen %r is not a HOST:PORT" % args.listen, file=sys.stderr)
        source.stop()
        if log is not None:
            log.close()
        return 1
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

    drain = make_drain(source, [
        make_log_sink(log, decimator) if log is not None else None,
        hub.broadcast,
    ])

    viewer = launch_viewer(hub.describe(), args.window, args.fps) if args.plot else None

    try:
        run_headless(source, drain, args.duration, hub, status)
    finally:
        source.stop()
        hub.stop()
        if viewer is not None:
            # The hub is already shut, so the dashboard's own source has died and it is
            # closing its window of its own accord. Give it that moment before insisting,
            # so the usual case exits without a signal.
            try:
                viewer.wait(timeout=2.0)
            except subprocess.TimeoutExpired:
                viewer.terminate()
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
        if hub.client_drops:
            print("dropped %d samples to viewers that fell behind" % hub.client_drops)
        if source.error is not None:
            print("source error: %s" % source.error, file=sys.stderr)
    # Outside the finally: a return there would swallow an in-flight exception.
    if source.error is not None:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
