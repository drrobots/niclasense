#!/usr/bin/env python3
"""Log the Nicla Sense ME sensor stream to CSV and plot it live.

Examples:
    python main.py                                   # auto-detect port, log + plot
    python main.py --csv runs/walk.csv --window 60
    python main.py --no-plot --duration 15           # headless capture
    python main.py --source ble                      # requires STREAM_BLE 1 firmware
    python main.py --list-ports
"""

import argparse
import datetime
import os
import queue
import sys
import time

from decimator import AdaptiveDecimator
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
    parser.add_argument(
        "--source", choices=("serial", "ble"), default="serial",
        help="Transport to read from.",
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
    parser.add_argument("--ble-name", default="NiclaStream", help="BLE local name to scan for.")
    parser.add_argument("--ble-address", default=None, help="BLE address, skips scanning.")
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
    parser.add_argument("--list-ports", action="store_true", help="List serial ports and exit.")
    return parser.parse_args(argv)


def default_csv_path():
    stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    return os.path.join("logs", "nicla_%s.csv" % stamp)


def make_drain(source, log, plot, decimator=None):
    """Return a function that moves everything queued into the logger and the plot.

    The plot always sees every sample; only the CSV is thinned. Decimation is about the
    size of the file on disk, not about how much the host is willing to look at.
    """

    def drain():
        moved = 0
        # Bounded so a backlog can never starve the caller (the animation timer).
        while moved < 5000:
            try:
                sample = source.queue.get_nowait()
            except queue.Empty:
                break
            if log is not None:
                if decimator is None:
                    log.write(sample)
                else:
                    for kept, is_burst in decimator.feed(sample):
                        log.write(kept, is_burst)
            if plot is not None:
                plot.add(sample)
            moved += 1
        return moved

    return drain


def run_headless(source, drain, duration):
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
                sys.stderr.write(
                    "\r%6.1f s   %7d rows   %5.1f Hz"
                    % (elapsed, total, total / elapsed if elapsed else 0.0)
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

    plot = None
    if not args.no_plot:
        from plot import LivePlot

        def status():
            return {
                "source": source.describe(),
                "csv": csv_path if log is not None else None,
                "rows": log.rows_written if log is not None else 0,
                "dropped": source.dropped,
                "malformed": getattr(source, "malformed", 0),
                "log_rate": decimator.rate if decimator is not None else 0,
                "bursts": decimator.bursts if decimator is not None else 0,
                "bursting": decimator.bursting if decimator is not None else False,
            }

        plot = LivePlot(
            window=args.window,
            fps=args.fps,
            title=source.describe(),
            status=status,
        )

    drain = make_drain(source, log, plot, decimator)

    try:
        if plot is not None:
            print("close the plot window to stop")
            plot.run(drain=drain)
            drain()
        else:
            run_headless(source, drain, args.duration)
    finally:
        source.stop()
        rows = log.rows_written if log is not None else 0
        if log is not None:
            log.close()
        print("wrote %d rows%s" % (rows, " to %s" % csv_path if log is not None else ""))
        if decimator is not None and log is not None:
            print(decimator.summary())
        if getattr(source, "malformed", 0):
            print("skipped %d malformed lines" % source.malformed)
        if source.dropped:
            print("dropped %d samples (consumer fell behind)" % source.dropped)
        if source.error is not None:
            print("source error: %s" % source.error, file=sys.stderr)
    # Outside the finally: a return there would swallow an in-flight exception.
    if source.error is not None:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
