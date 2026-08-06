#!/usr/bin/env python3
"""Offline viewer for the CSVs main.py writes.

The live dashboard shows the last N seconds of a stream; this shows a whole file at
once. Same tiles, same palette, same 12-column grid, so a recording reads exactly like
the stream it came from -- what changes is the time axis, which is now a fixed span you
scrub and zoom rather than a window sliding under the newest sample.

Examples:
    python view.py                              # newest file in logs/
    python view.py logs/nicla_20260805_231541.csv
    python view.py logs/ --overview gz_dps      # pick the file, drive the strip off gyro

Drag on the overview strip at the bottom to select a range; every tile redraws to it.
Move the mouse across any tile to put a cursor at that instant -- the readouts beside
each title are the values at the cursor, not the last row of the file.
"""

import argparse
import csv
import datetime
import glob
import os
import sys

import matplotlib.pyplot as plt
import numpy as np
from matplotlib.gridspec import GridSpec
from matplotlib.ticker import MaxNLocator
from matplotlib.transforms import blended_transform_factory
from matplotlib.widgets import SpanSelector

from columns import BURST_COLUMN, HOST_TIME_COLUMN
from plot import (
    ACCENT,
    BSEC_ACCURACY_NOTES,
    BSEC_TILES,
    CAPTURE_SLOT,
    FONT,
    GRID,
    MUTED,
    PAGE_BG,
    PLACEMENT,
    TEXT,
    TILE_BG,
    TILE_EDGE,
    TILES,
)

# Points drawn per trace. Two per bucket of the min/max reduction below, so the envelope
# survives; a plain stride at this ratio (117k rows into a 4-inch tile) drops the peak of
# a 20 ms impact about 19 times out of 20.
MAX_POINTS = 1200

# Smallest range the viewer will zoom to. At 200 Hz that is still 10 samples across.
MIN_VIEW_S = 0.05

# A drag shorter than this is a click that happened to move a pixel; treat it as placing
# the cursor rather than as a request to zoom into 3 ms of data.
MIN_DRAG_S = 0.02

OVERVIEW_LINE = "#7a8b8b"
BURST_SHADE = "#2a2f12"


class LogFile(object):
    """One CSV, loaded into columns.

    `t` is seconds from the first row, derived from the board's `t_ms` rather than the
    host clock: it is the board's own sample timing, so it spaces samples the way they
    were actually taken instead of the way the host happened to see them arrive.
    """

    def __init__(self, path, t, columns, burst, started):
        self.path = path
        self.t = t
        self.columns = columns
        self.burst = burst
        self.started = started

    @property
    def rows(self):
        return len(self.t)

    @property
    def duration(self):
        return self.t[-1] - self.t[0] if self.rows > 1 else 0.0

    @property
    def mean_hz(self):
        return (self.rows - 1) / self.duration if self.duration > 0 else 0.0

    def wall_clock(self, seconds):
        """Wall-clock time at `seconds` into the file, or None if unknown.

        Interpolated from the first row's host timestamp plus board elapsed time rather
        than read per row: parsing 100k ISO strings costs more than the sub-second drift
        between the two clocks is worth.
        """
        if self.started is None:
            return None
        return self.started + datetime.timedelta(seconds=float(seconds))

    def index_at(self, seconds):
        """Row nearest a time, clamped to the file."""
        i = int(np.searchsorted(self.t, seconds))
        if i <= 0:
            return 0
        if i >= self.rows:
            return self.rows - 1
        return i if (self.t[i] - seconds) < (seconds - self.t[i - 1]) else i - 1

    def span(self, t0, t1):
        """Half-open row range covering [t0, t1], widened by one row on each side.

        The extra row on each end keeps a trace touching both edges of the tile instead
        of stopping short wherever the last in-range sample happens to fall.
        """
        lo = max(0, int(np.searchsorted(self.t, t0, "left")) - 1)
        hi = min(self.rows, int(np.searchsorted(self.t, t1, "right")) + 1)
        return lo, max(hi, lo + 1)


def load(path):
    """Read a logger CSV into a LogFile."""
    with open(path, "r") as handle:
        header_line = handle.readline()
        first_row = handle.readline()
    if not header_line or not first_row:
        raise ValueError("%s has no data rows" % path)

    header = [name.strip() for name in header_line.strip().split(",")]
    if "t_ms" not in header:
        raise ValueError("%s is not a Nicla log (no t_ms column)" % path)

    # Every column except the host timestamp is numeric; that one is parsed once, below.
    numeric = [(i, name) for i, name in enumerate(header) if name != HOST_TIME_COLUMN]
    values = _read_numeric(path, [i for i, _name in numeric], len(header))
    columns = {name: values[:, k] for k, (_i, name) in enumerate(numeric)}

    started = None
    if HOST_TIME_COLUMN in header:
        stamp = first_row.split(",")[header.index(HOST_TIME_COLUMN)].strip()
        try:
            started = datetime.datetime.fromisoformat(stamp)
        except ValueError:
            started = None

    burst = columns.pop(BURST_COLUMN, None)
    t = _timeline(columns.pop("t_ms"))
    return LogFile(path, t, columns, burst, started)


def _read_numeric(path, usecols, ncols):
    """Numeric columns as a 2-D array, tolerating a torn last line."""
    try:
        return np.loadtxt(
            path, delimiter=",", skiprows=1, usecols=usecols, ndmin=2, dtype=np.float64
        )
    except ValueError:
        # A capture killed mid-write leaves a partial final row, and an unlucky USB glitch
        # can leave a short one anywhere. One bad row should not cost you the file.
        return _read_numeric_tolerant(path, usecols, ncols)


def _read_numeric_tolerant(path, usecols, ncols):
    rows = []
    skipped = 0
    with open(path, "r", newline="") as handle:
        reader = csv.reader(handle)
        next(reader, None)
        for row in reader:
            if len(row) != ncols:
                skipped += 1
                continue
            try:
                rows.append([float(row[i]) for i in usecols])
            except ValueError:
                skipped += 1
    if skipped:
        print("skipped %d malformed row(s)" % skipped, file=sys.stderr)
    if not rows:
        raise ValueError("%s has no parseable rows" % path)
    return np.array(rows, dtype=np.float64)


def _timeline(t_ms):
    """Board milliseconds to seconds from the start, stitched across resets.

    `t_ms` restarts at zero whenever the board reboots, which would otherwise fold the
    rest of the file back on top of the beginning. Each backwards step is bridged by one
    median sample interval -- the gap is however long the board took to come back, which
    the log does not record, so this is deliberately a seam and not a measurement.
    """
    t = t_ms / 1000.0
    steps = np.diff(t)
    resets = np.flatnonzero(steps < 0)
    if resets.size:
        dt = float(np.median(steps[steps > 0])) if np.any(steps > 0) else 0.005
        shift = np.zeros_like(t)
        for i in resets:
            shift[i + 1:] += t[i] + dt - t[i + 1]
        t = t + shift
        print(
            "note: %d board reset(s) in this file; the timeline is stitched across them"
            % resets.size,
            file=sys.stderr,
        )
    return t - t[0]


def envelope(t, y, max_points=MAX_POINTS):
    """Downsample to a drawable trace that keeps every peak.

    Buckets the range and emits each bucket's min and max in time order. The result is
    the signal's envelope rather than its waveform -- at these ratios a tile cannot show
    a waveform anyway, and the alternative (a plain stride) silently deletes the spikes
    that are the whole reason to look at a log.
    """
    n = len(t)
    if n <= max_points:
        return t, y
    buckets = max_points // 2
    edges = np.linspace(0, n, buckets + 1).astype(np.intp)
    # Empty buckets would make reduceat read past their start; n > max_points guarantees
    # at least two rows each, but clip anyway so a pathological range cannot raise.
    starts = np.minimum(edges[:-1], n - 1)
    lows = np.minimum.reduceat(y, starts)
    highs = np.maximum.reduceat(y, starts)
    mids = (t[starts] + t[np.minimum(edges[1:], n - 1)]) / 2.0
    out_t = np.empty(buckets * 2)
    out_y = np.empty(buckets * 2)
    out_t[0::2] = t[starts]
    out_t[1::2] = mids
    out_y[0::2] = lows
    out_y[1::2] = highs
    return out_t, out_y


def burst_runs(t, burst):
    """Contiguous burst regions as (start, width) pairs for broken_barh."""
    flags = burst > 0.5
    if not np.any(flags):
        return []
    edges = np.diff(flags.astype(np.int8))
    starts = list(np.flatnonzero(edges == 1) + 1)
    ends = list(np.flatnonzero(edges == -1) + 1)
    if flags[0]:
        starts.insert(0, 0)
    if flags[-1]:
        ends.append(len(flags) - 1)
    return [(t[s], max(t[e] - t[s], 1e-3)) for s, e in zip(starts, ends)]


class LogView(object):
    """The window: header, tile grid, file tile, and an overview strip to scrub with."""

    def __init__(self, log, overview_column=None):
        self.log = log
        self.view = (log.t[0], max(log.t[-1], log.t[0] + MIN_VIEW_S))
        self.cursor = log.t[-1]

        self.figure = plt.figure(figsize=(15, 9), facecolor=PAGE_BG)
        self.figure.canvas.manager.set_window_title(
            "Nicla log - " + os.path.basename(log.path)
        )

        grid = GridSpec(
            5, 12,
            figure=self.figure,
            height_ratios=(0.30, 1.0, 1.0, 0.78, 0.46),
            left=0.035, right=0.988, top=0.965, bottom=0.075,
            wspace=0.55, hspace=0.42,
        )

        self._header = self._make_header(grid)
        self._tiles = [self._make_tile(grid, tile) for tile in TILES]
        self._info = self._make_info(grid)
        self._overview = self._make_overview(grid, overview_column)

        self.figure.canvas.mpl_connect("motion_notify_event", self._on_motion)
        self.figure.canvas.mpl_connect("key_press_event", self._on_key)

        self._draw_traces()
        self._draw_cursor()

    # -- construction ----------------------------------------------------------

    def _blank_axes(self, axes):
        axes.set_facecolor(TILE_BG)
        axes.set_xticks([])
        axes.set_yticks([])
        for spine in axes.spines.values():
            spine.set_color(TILE_EDGE)
        return axes

    def _make_header(self, grid):
        axes = self._blank_axes(self.figure.add_subplot(grid[0, :]))
        subtitle = axes.text(
            0.008, 0.5, os.path.basename(self.log.path),
            transform=axes.transAxes, va="center", ha="left",
            fontfamily=FONT, fontsize=11.5, color=MUTED,
        )
        clock = axes.text(
            0.992, 0.5, "",
            transform=axes.transAxes, va="center", ha="right",
            fontfamily=FONT, fontsize=13, color=TEXT,
        )
        return {"subtitle": subtitle, "clock": clock}

    def _make_tile(self, grid, tile):
        row, column, span = PLACEMENT[tile["name"]]
        axes = self.figure.add_subplot(grid[row + 1, column:column + span])
        axes.set_facecolor(TILE_BG)
        axes.grid(True, color=GRID, linewidth=0.6)
        axes.set_axisbelow(True)
        for spine in axes.spines.values():
            spine.set_color(TILE_EDGE)
        axes.tick_params(axis="y", colors=MUTED, labelsize=8.5, length=0, pad=2)
        # The visible range is stated once under the overview strip, so per-tile time
        # ticks would only repeat it eleven times.
        axes.set_xticks([])
        axes.yaxis.set_major_locator(MaxNLocator(nbins=3, prune="both"))
        axes.ticklabel_format(axis="y", useOffset=False, style="plain")

        present = [s for s in tile["series"] if s[0] in self.log.columns]
        axes.set_title(
            tile["title"], loc="left", pad=6,
            fontfamily=FONT, fontsize=10, color=TEXT if present else MUTED,
        )
        readout = axes.text(
            1.0, 1.015, "" if present else "not in this log",
            transform=axes.transAxes, va="bottom", ha="right",
            fontfamily=FONT, color="#FFFFFF" if present else MUTED,
            fontsize=12.5 if tile.get("value") else 11.0,
        )
        unit = axes.text(
            0.0, -0.02, tile["unit"],
            transform=axes.transAxes, va="top", ha="left",
            fontfamily=FONT, fontsize=8.5, color=MUTED,
        )
        accuracy = None
        if tile["name"] in BSEC_TILES and "bsec_acc" in self.log.columns:
            accuracy = axes.text(
                1.0, -0.02, "",
                transform=axes.transAxes, va="top", ha="right",
                fontfamily=FONT, fontsize=8.5, color=MUTED,
            )

        lines = {}
        for name, _label, colour in present:
            (line,) = axes.plot([], [], "-", linewidth=1.1, color=colour)
            lines[name] = line

        quaternion = None
        if tile.get("quaternion") and all(
            q in self.log.columns for q in ("qx", "qy", "qz", "qw")
        ):
            quaternion = axes.text(
                0.12, -0.02, "",
                transform=axes.transAxes, va="top", ha="left",
                fontfamily=FONT, fontsize=8.5, color=MUTED,
            )

        # x in data coords, y spanning the tile: the cursor is a time, and it should reach
        # the full height of whatever the tile is autoscaled to.
        cursor = axes.axvline(
            self.log.t[-1], color="#FFFFFF", linewidth=0.8, alpha=0.45, zorder=5,
        )

        return {
            "axes": axes, "tile": tile, "series": present, "lines": lines,
            "readout": readout, "unit": unit, "accuracy": accuracy,
            "quaternion": quaternion, "cursor": cursor,
        }

    def _make_info(self, grid):
        row, column, span = CAPTURE_SLOT
        axes = self._blank_axes(
            self.figure.add_subplot(grid[row + 1, column:column + span])
        )
        axes.set_title(
            "FILE", loc="left", pad=6,
            fontfamily=FONT, fontsize=10, color=TEXT,
        )
        axes.text(
            0.0, 0.86, "duration",
            transform=axes.transAxes, va="center", ha="left",
            fontfamily=FONT, fontsize=9, color=MUTED,
        )
        axes.text(
            0.62, 0.86, "mean Hz",
            transform=axes.transAxes, va="center", ha="left",
            fontfamily=FONT, fontsize=9, color=MUTED,
        )
        axes.text(
            0.0, 0.6, _duration(self.log.duration),
            transform=axes.transAxes, va="center", ha="left",
            fontfamily=FONT, fontsize=24, color=TEXT,
        )
        axes.text(
            0.62, 0.6, "%.0f" % self.log.mean_hz,
            transform=axes.transAxes, va="center", ha="left",
            fontfamily=FONT, fontsize=24, color=ACCENT,
        )
        started = self.log.started
        detail = axes.text(
            0.0, 0.31, "\n".join((
                "file    %s" % _shorten(self.log.path, 34),
                "rows    %s%s" % (
                    _thousands(self.log.rows),
                    "   bursts %s" % _thousands(len(self._burst_runs()))
                    if self.log.burst is not None else "",
                ),
                "start   %s" % (
                    started.strftime("%Y-%m-%d %H:%M:%S") if started else "unknown"
                ),
            )),
            transform=axes.transAxes, va="center", ha="left",
            fontfamily=FONT, fontsize=9.5, color=TEXT, linespacing=1.7,
        )
        selection = axes.text(
            0.0, 0.045, "",
            transform=axes.transAxes, va="center", ha="left",
            fontfamily=FONT, fontsize=9.5, color=MUTED,
        )
        return {"axes": axes, "detail": detail, "selection": selection}

    def _burst_runs(self):
        if self.log.burst is None:
            return []
        if not hasattr(self, "_runs"):
            self._runs = burst_runs(self.log.t, self.log.burst)
        return self._runs

    def _make_overview(self, grid, overview_column):
        axes = self.figure.add_subplot(grid[4, :])
        axes.set_facecolor(TILE_BG)
        for spine in axes.spines.values():
            spine.set_color(TILE_EDGE)
        axes.set_yticks([])
        axes.tick_params(axis="x", colors=MUTED, labelsize=8.5, length=0, pad=3)
        axes.margins(x=0)

        name, values = self._overview_signal(overview_column)
        t, y = envelope(self.log.t, values, MAX_POINTS * 2)
        axes.plot(t, y, "-", linewidth=0.8, color=OVERVIEW_LINE)
        axes.set_xlim(self.log.t[0], self.log.t[-1])
        low, high = float(np.min(values)), float(np.max(values))
        pad = (high - low) * 0.08 or 1.0
        axes.set_ylim(low - pad, high + pad)

        # Bursts are where the logger decided something was happening, which makes them
        # the obvious places to zoom into -- so they are marked on the thing you scrub.
        runs = self._burst_runs()
        if runs:
            blended = blended_transform_factory(axes.transData, axes.transAxes)
            axes.broken_barh(
                runs, (0, 1), transform=blended, facecolor=BURST_SHADE, zorder=0,
            )

        axes.set_title(
            "OVERVIEW   %s%s" % (name, "   burst regions shaded" if runs else ""),
            loc="left", pad=5, fontfamily=FONT, fontsize=9, color=MUTED,
        )
        axes.text(
            0.0, -0.30, "seconds into the file",
            transform=axes.transAxes, va="top", ha="left",
            fontfamily=FONT, fontsize=8.5, color=MUTED,
        )
        axes.text(
            1.0, -0.30, "drag to select   arrows pan   +/- zoom   r reset",
            transform=axes.transAxes, va="top", ha="right",
            fontfamily=FONT, fontsize=8.5, color=MUTED,
        )
        # Held on the instance: matplotlib widgets stop responding once garbage collected.
        self._selector = SpanSelector(
            axes, self._on_select, "horizontal",
            useblit=True, interactive=True, drag_from_anywhere=True,
            # Light: viewing the whole file washes the entire strip, and at 0.18 the
            # accent turns the overview trace olive and hard to read.
            props={"facecolor": ACCENT, "alpha": 0.08},
            handle_props={"color": ACCENT},
        )
        self._selector.extents = self.view
        return {"axes": axes}

    def _overview_signal(self, overview_column):
        """What the scrub strip plots: motion by default, since that is what you hunt for."""
        if overview_column:
            if overview_column not in self.log.columns:
                raise ValueError(
                    "%s is not in this log (have: %s)"
                    % (overview_column, ", ".join(sorted(self.log.columns)))
                )
            return overview_column, self.log.columns[overview_column]
        accel = ("ax_g", "ay_g", "az_g")
        if all(a in self.log.columns for a in accel):
            magnitude = np.sqrt(sum(self.log.columns[a] ** 2 for a in accel))
            return "|accel| g", magnitude
        name = next(iter(sorted(self.log.columns)))
        return name, self.log.columns[name]

    # -- drawing ---------------------------------------------------------------

    def _draw_traces(self):
        """Redraw every tile for the current view range. Called on range changes only."""
        t0, t1 = self.view
        lo, hi = self.log.span(t0, t1)
        times = self.log.t[lo:hi]

        for entry in self._tiles:
            axes, tile = entry["axes"], entry["tile"]
            low = high = None
            for name, _label, _colour in entry["series"]:
                values = self.log.columns[name][lo:hi]
                drawn_t, drawn_y = envelope(times, values)
                entry["lines"][name].set_data(drawn_t, drawn_y)
                vmin, vmax = float(np.min(values)), float(np.max(values))
                low = vmin if low is None else min(low, vmin)
                high = vmax if high is None else max(high, vmax)
                # Single-value tiles print the range under the trace: the readout above
                # is one instant, and on a zoomed-out view the spread is the real story.
                # Not on the BSEC tiles -- their calibration note has the other end of
                # that line, and on a two-column tile the two would collide.
                if tile.get("value") and entry["accuracy"] is None:
                    entry["unit"].set_text(
                        "%s   %s .. %s"
                        % (
                            tile["unit"],
                            (tile["fmt"] % vmin).strip(),
                            (tile["fmt"] % vmax).strip(),
                        )
                    )

            if low is not None:
                # Same floor as the live dashboard: without it a quiet stretch autoscales
                # to its own quantization steps and sensor noise reads as real signal.
                if high - low < tile["min_span"]:
                    middle = (high + low) / 2.0
                    low = middle - tile["min_span"] / 2.0
                    high = middle + tile["min_span"] / 2.0
                span = high - low
                axes.set_ylim(low - span * 0.12, high + span * 0.12)
            axes.set_xlim(t0, t1)

        self._selector.extents = self.view
        self._info["selection"].set_text(
            "view    %s .. %s   (%s)"
            % (_clock(t0), _clock(t1), _duration(t1 - t0))
        )
        self.figure.canvas.draw_idle()

    def _draw_cursor(self):
        """Update the cursor line and every readout. Called on every mouse move."""
        i = self.log.index_at(self.cursor)
        t = float(self.log.t[i])

        for entry in self._tiles:
            entry["cursor"].set_xdata([t, t])
            if not entry["series"]:
                continue
            tile = entry["tile"]
            latest = [
                (label, float(self.log.columns[name][i]))
                for name, label, _colour in entry["series"]
            ]
            if tile.get("value"):
                entry["readout"].set_text(tile["fmt"] % latest[0][1])
            else:
                entry["readout"].set_text(
                    "  ".join("%s %s" % (l, tile["fmt"] % v) for l, v in latest)
                )
            if entry["accuracy"] is not None:
                note, colour = BSEC_ACCURACY_NOTES.get(
                    int(self.log.columns["bsec_acc"][i]), (None, MUTED)
                )
                entry["accuracy"].set_text(note or "")
                entry["accuracy"].set_color(colour)
            if entry["quaternion"] is not None:
                entry["quaternion"].set_text(
                    "quat  " + "  ".join(
                        "%s %6.3f" % (q, self.log.columns[q][i])
                        for q in ("qx", "qy", "qz", "qw")
                    )
                )

        wall = self.log.wall_clock(t)
        self._header["clock"].set_text(
            "t + %s%s" % (_clock(t), "   %s" % wall.strftime("%H:%M:%S.%f")[:-3] if wall else "")
        )
        self.figure.canvas.draw_idle()

    # -- interaction -----------------------------------------------------------

    def set_view(self, t0, t1):
        first, last = float(self.log.t[0]), float(self.log.t[-1])
        span = max(min(t1 - t0, last - first), MIN_VIEW_S)
        t0 = min(max(t0, first), max(last - span, first))
        self.view = (t0, t0 + span)
        self._draw_traces()
        self._draw_cursor()

    def _on_select(self, t0, t1):
        if t1 - t0 < MIN_DRAG_S:
            # A click, not a drag: leave the range alone and put the cursor there.
            self._selector.extents = self.view
            self.cursor = t0
            self._draw_cursor()
            return
        self.set_view(t0, t1)

    def _on_motion(self, event):
        if event.xdata is None or event.inaxes is None:
            return
        if event.inaxes is self._overview["axes"]:
            return
        if not any(event.inaxes is entry["axes"] for entry in self._tiles):
            return
        self.cursor = event.xdata
        self._draw_cursor()

    def _on_key(self, event):
        t0, t1 = self.view
        span = t1 - t0
        if event.key in ("r", "home"):
            self.set_view(float(self.log.t[0]), float(self.log.t[-1]))
        elif event.key == "left":
            self.set_view(t0 - span * 0.25, t1 - span * 0.25)
        elif event.key == "right":
            self.set_view(t0 + span * 0.25, t1 + span * 0.25)
        elif event.key in ("+", "=", "up"):
            middle = (t0 + t1) / 2.0
            self.set_view(middle - span * 0.35, middle + span * 0.35)
        elif event.key in ("-", "down"):
            middle = (t0 + t1) / 2.0
            self.set_view(middle - span, middle + span)

    def show(self):
        plt.show()


def _thousands(value):
    return "{:,}".format(int(value))


def _shorten(text, width):
    """Keep the tail of an over-long path; the file name matters more than its parents."""
    return text if len(text) <= width else "..." + text[-(width - 3):]


def _duration(seconds):
    if seconds < 60:
        return "%.1fs" % seconds
    minutes, rest = divmod(seconds, 60)
    if minutes < 60:
        return "%dm%02ds" % (minutes, rest)
    hours, minutes = divmod(minutes, 60)
    return "%dh%02dm" % (hours, minutes)


def _clock(seconds):
    """Position within the file, at a resolution that survives zooming right in."""
    minutes, rest = divmod(abs(seconds), 60)
    return "%d:%06.3f" % (minutes, rest) if minutes else "%.3fs" % rest


def newest_log(where):
    """Newest CSV in a directory, searching the usual places when given none."""
    candidates = [where] if where else _default_dirs()
    for directory in candidates:
        files = sorted(glob.glob(os.path.join(directory, "*.csv")), key=os.path.getmtime)
        if files:
            return files[-1]
    raise ValueError("no CSVs found in %s" % ", ".join(candidates))


def _default_dirs():
    here = os.path.dirname(os.path.abspath(__file__))
    return [
        os.path.join(os.getcwd(), "logs"),
        os.path.join(here, "logs"),
        os.path.join(os.path.dirname(here), "logs"),
    ]


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Browse a logged Nicla capture in the dashboard's own layout.",
    )
    parser.add_argument(
        "path", nargs="?", default=None,
        help="CSV to open, or a directory to take the newest CSV from "
             "(default: newest under logs/).",
    )
    parser.add_argument(
        "--overview", default=None, metavar="COL",
        help="Column to draw in the scrub strip (default: accelerometer magnitude).",
    )
    args = parser.parse_args(argv)

    path = args.path
    try:
        if path is None or os.path.isdir(path):
            path = newest_log(path)
        log = load(path)
        view = LogView(log, overview_column=args.overview)
    except (OSError, ValueError) as exc:
        print("error: %s" % exc, file=sys.stderr)
        return 1

    print(
        "%s: %s rows, %s, mean %.0f Hz"
        % (path, _thousands(log.rows), _duration(log.duration), log.mean_hz)
    )
    view.show()
    return 0


if __name__ == "__main__":
    sys.exit(main())
