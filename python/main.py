#!/usr/bin/env python3
"""Log the Nicla Sense ME sensor stream to CSV and serve it over TCP.

This is the capture: it owns the serial port, the decimator and the CSV, and it is always
headless. Every sample goes out on a TCP socket as well as to the file (see hub.py for the
protocol), and drawing is somebody else's process -- webdash.py for a browser dashboard,
`nc` for the raw rows.

There is no in-process plot, deliberately. A viewer that shares this interpreter shares
its GIL and its lifetime with the logging, which is the wrong coupling for something meant
to run for hours: the capture is the durable thing and viewers come and go over the
socket, here or on another machine. --plot is only a convenience for the common case of
wanting one immediately -- it starts webdash.py as a *child* pointed at our own socket and
opens a browser at it, so it attaches like any other viewer and closing the tab leaves the
capture running.

Every switch is also settable from an INI file (--config); see config.py and
example.conf. The command line still wins, so a file describes a standing setup and
flags vary one run of it.

Examples:
    python main.py                                   # log and serve; attach when you like
    python main.py --plot                            # ...and open a browser dashboard now
    python main.py --csv runs/walk.csv --duration 15
    python main.py --listen 0.0.0.0:8790             # serve somewhere else
    python main.py --config overnight.conf           # a setup that lives in a file
    python main.py --config overnight.conf --log-rate 1   # ...with one thing changed
    python main.py --list-ports
"""

import argparse
import contextlib
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
from webhub import DEFAULT_HTTP_PORT


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
        help="Also start webdash.py against this capture's socket and open a browser at "
             "it. Closing the tab detaches it; the capture keeps going.",
    )
    parser.add_argument(
        "--http-port", type=int, default=DEFAULT_HTTP_PORT,
        help="Port for the dashboard --plot starts. Ignored without --plot.",
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


def launch_viewer(endpoint, http_port):
    """Start webdash.py against our own socket. Returns the process, or None.

    A child rather than an import: the dashboard's HTTP server and its clients would
    otherwise share this interpreter with the thread that has to keep draining the serial
    queue, which is the coupling the split exists to remove. Over the socket it is an
    ordinary viewer that happens to have been started for you, and it can be closed,
    reopened, or joined by a second one without the capture being involved.

    Note what is *not* passed: window length and refresh rate. Those are per-tab in the
    browser dashboard rather than per-process, which is the point of it -- two tabs can
    watch this capture over different spans -- so there is nothing for the capture to say
    about them.

    A dashboard that will not start is reported and shrugged off. The samples are already
    reaching the CSV and the socket by the time we get here, and killing a good capture
    because its convenience viewer failed would be the wrong trade.
    """
    script = os.path.join(os.path.dirname(os.path.abspath(__file__)), "webdash.py")
    argv = [sys.executable, script, endpoint, "--http-port", "%d" % http_port, "--open"]
    try:
        return subprocess.Popen(argv)
    except OSError as exc:
        print("warning: could not start webdash.py (%s); the capture continues" % exc,
              file=sys.stderr)
        return None


def shutdown_viewer(viewer):
    """Wait out the dashboard child, if there is one. Takes a list, empty or of one.

    A list because this is registered on the cleanup stack before the process exists:
    release runs in reverse registration order, and the dashboard only notices the
    capture has ended when the hub it is attached to closes, so the hub's shutdown has
    to have been registered later than this one.

    The wait itself is the courtesy. The hub is already shut by the time we get here, so
    the dashboard's own source has died and it is shutting down of its own accord --
    telling its open tabs the capture ended on the way out. Give it that moment before
    insisting, so the usual case exits without a signal and the page says what happened.
    """
    for process in viewer:
        try:
            process.wait(timeout=2.0)
        except subprocess.TimeoutExpired:
            process.terminate()
    del viewer[:]


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

    # Everything acquired past this point is released by the stack, on every way out of
    # the block: the start-up failures below, Ctrl-C, and the ordinary end of a capture
    # alike. It used to be that each `return 1` carried its own hand-written copy of the
    # cleanup, and the acquisition that had no copy -- the CSV -- was for that reason
    # never wrapped at all, so an unwritable path came out as a traceback with the board
    # already open. Registration order is release order reversed; where that matters it
    # is said so below.
    with contextlib.ExitStack() as stack:
        stack.callback(source.stop)

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
            )
            try:
                stack.enter_context(log)
            except (OSError, IOError) as exc:
                # A full disk, a mount that went away, a typo in a config file read by
                # cron: the start-up failure most likely to happen with nobody watching,
                # so it gets the same one line and exit code as the rest of them.
                print("error: %s" % exc, file=sys.stderr)
                return 1
            print("logging to %s" % csv_path)
            if decimator is not None:
                print(
                    "steady rate %g Hz, bursting to full rate for %.2gs after a trigger "
                    "on %s"
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

        # Routed through SerialControl rather than the source so the link-budget clamp
        # that protects the GUI buttons protects the flag too.
        if args.rate and isinstance(source, SerialSource):
            print(SerialControl(source).set_rate(args.rate))
            if decimator is not None and args.rate < decimator.rate:
                print(
                    "warning: the board is streaming at %d Hz, so bursts cannot exceed "
                    "that" % args.rate,
                    file=sys.stderr,
                )

        try:
            host, port = parse_endpoint(args.listen)
        except ValueError:
            print("error: --listen %r is not a HOST:PORT" % args.listen, file=sys.stderr)
            return 1

        # Registered before the viewer exists, and before the hub, because release runs
        # in reverse: the dashboard only shuts down of its own accord once the hub it is
        # attached to has gone, so hub.stop() has to happen first.
        viewer = []
        stack.callback(shutdown_viewer, viewer)

        # describe() rather than a captured string: a viewer attaching after a rate change
        # should be told what the board is doing now.
        hub = SampleHub(host=host, port=port, banner=source.describe)
        try:
            hub.start()
        except OSError as exc:
            print("error: %s" % exc, file=sys.stderr)
            return 1
        stack.callback(hub.stop)
        print("serving samples on %s -- watch it with: python webdash.py %s"
              % (hub.describe(), hub.describe()))

        def status():
            return capture_status(source, log, decimator, csv_path, hub)

        drain = make_drain(source, [
            make_log_sink(log, decimator) if log is not None else None,
            hub.broadcast,
        ])

        if args.plot:
            process = launch_viewer(hub.describe(), args.http_port)
            if process is not None:
                viewer.append(process)

        try:
            run_headless(source, drain, args.duration, hub, status)
        finally:
            rows = log.rows_written if log is not None else 0
            # Closed here rather than left to the end of the block, so the summary
            # describes a capture that has actually stopped. close() pops what it runs,
            # so leaving the block afterwards releases nothing a second time.
            stack.close()
            if log is not None:
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
