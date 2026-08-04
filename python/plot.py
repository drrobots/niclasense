"""Live dashboard for the Nicla stream.

Laid out like Arduino's own NiclaSenseME web dashboard: a dark tile grid, one widget
per sensor, current value called out next to each title, and a scrolling trace
underneath. Where that dashboard puts an RGB LED picker we put the capture readout --
output file, measured rate, rows on disk -- since this tool's job is logging.

Data is kept in fixed-length deques sized to the requested time window.
"""

from collections import deque

import matplotlib.pyplot as plt
from matplotlib.animation import FuncAnimation
from matplotlib.gridspec import GridSpec
from matplotlib.ticker import MaxNLocator
from matplotlib.widgets import TextBox

from columns import COLUMNS

# Palette lifted from the Arduino dashboard: near-black page, slightly lifted tiles,
# and the Arduino Pro accent yellow-green for anything that reads as "live".
PAGE_BG = "#000000"
TILE_BG = "#111111"
TILE_EDGE = "#1f1f1f"
TEXT = "#DAE3E3"
MUTED = "#888888"
ACCENT = "#d8f41d"
GRID = "#242424"

# Per-axis colours for the three-component sensors, so x/y/z read the same everywhere.
XYZ = ("#d8f41d", "#50BFE6", "#FF6EFF")

FONT = "DejaVu Sans Mono"

# A tile is one widget. `series` are (column, label, colour); `value` is the column whose
# latest reading is printed large next to the title (None for multi-component tiles,
# which print all of their components small instead).
#
# `min_span` is the smallest y-range a tile will autoscale to. Without it, a board
# sitting still gets scaled to its own quantization steps, which renders sensor noise as
# dramatic staircases and reads as real signal. Each value is roughly the sensor's noise
# floor, so a resting board looks flat and genuine motion still fills the tile.
TILES = (
    {
        "name": "orientation",
        "title": "ORIENTATION",
        "series": (
            ("heading_deg", "hdg", XYZ[0]),
            ("pitch_deg", "pit", XYZ[1]),
            ("roll_deg", "rol", XYZ[2]),
        ),
        "unit": "deg",
        "fmt": "%6.1f",
        "min_span": 30.0,
        "quaternion": True,
    },
    {
        "name": "accelerometer",
        "title": "ACCELEROMETER",
        "series": (("ax_g", "x", XYZ[0]), ("ay_g", "y", XYZ[1]), ("az_g", "z", XYZ[2])),
        "unit": "g",
        "fmt": "%6.2f",
        "min_span": 0.5,
    },
    {
        "name": "gyroscope",
        "title": "GYROSCOPE",
        "series": (
            ("gx_dps", "x", XYZ[0]),
            ("gy_dps", "y", XYZ[1]),
            ("gz_dps", "z", XYZ[2]),
        ),
        "unit": "deg/s",
        "fmt": "%7.1f",
        "min_span": 5.0,
    },
    {
        "name": "magnetometer",
        "title": "MAGNETOMETER",
        "series": (("mx_uT", "x", XYZ[0]), ("my_uT", "y", XYZ[1]), ("mz_uT", "z", XYZ[2])),
        "unit": "uT",
        "fmt": "%7.1f",
        "min_span": 20.0,
    },
    {
        "name": "gas",
        "title": "GAS RESISTANCE",
        "series": (("gas_ohm", "gas", "#FF9933"),),
        "value": "gas_ohm",
        "unit": "ohm",
        "fmt": "%.0f",
        "min_span": 2000.0,
    },
    {
        "name": "temperature",
        "title": "TEMPERATURE",
        "series": (("temp_C", "temp", "#FF6037"),),
        "value": "temp_C",
        "unit": "degC",
        "fmt": "%.2f",
        "min_span": 2.0,
    },
    {
        "name": "humidity",
        "title": "HUMIDITY",
        "series": (("hum_pct", "hum", "#50BFE6"),),
        "value": "hum_pct",
        "unit": "%RH",
        "fmt": "%.1f",
        "min_span": 2.0,
    },
    {
        "name": "pressure",
        "title": "PRESSURE",
        "series": (("press_hPa", "press", "#AAF0D1"),),
        "value": "press_hPa",
        "unit": "hPa",
        "fmt": "%.2f",
        "min_span": 2.0,
    },
    {
        "name": "iaq",
        "title": "AIR QUALITY",
        "series": (("iaq", "iaq", "#CCFF00"),),
        "value": "iaq",
        "unit": "IAQ",
        "fmt": "%.0f",
        "min_span": 50.0,
    },
    {
        "name": "co2",
        "title": "CO2 EQUIVALENT",
        "series": (("co2_eq_ppm", "co2", "#66FF66"),),
        "value": "co2_eq_ppm",
        "unit": "ppm",
        "fmt": "%.0f",
        "min_span": 100.0,
    },
    {
        "name": "bvoc",
        "title": "BVOC EQUIVALENT",
        "series": (("bvoc_eq_ppm", "bvoc", "#FF355E"),),
        "value": "bvoc_eq_ppm",
        "unit": "ppm",
        "fmt": "%.2f",
        "min_span": 1.0,
    },
)

# Where each tile sits in the 12-column grid: (row, first column, column span).
PLACEMENT = {
    "orientation": (0, 0, 4),
    "accelerometer": (0, 4, 4),
    "gyroscope": (0, 8, 4),
    "magnetometer": (1, 0, 5),
    # (1, 5, 4) is the capture tile, in the slot the web dashboard gives the LED picker.
    "gas": (1, 9, 3),
    "temperature": (2, 0, 2),
    "humidity": (2, 2, 2),
    "pressure": (2, 4, 2),
    "iaq": (2, 6, 2),
    "co2": (2, 8, 2),
    "bvoc": (2, 10, 2),
}

CAPTURE_SLOT = (1, 5, 4)

# Drawing every sample of a 200 Hz stream is invisible detail at tile size and costs real
# frame time, so traces are strided down to about this many points.
MAX_POINTS = 900

GRID_BOTTOM_PLAIN = 0.03

# The sketch computes its period as the integer 1000/hz, so anything above this is not a
# meaningfully different rate and the firmware itself was never asked to go faster.
MAX_TARGET_HZ = 200

# Below this the trace has too few points to read; above it the ring buffer needed to
# back it (see _resize_capacity) starts costing real memory for little benefit.
MIN_WINDOW_S = 2.0
MAX_WINDOW_S = 600.0

BUTTON_FACE = "#191919"
BUTTON_HOVER = "#2c2c2c"


class LivePlot(object):
    """Scrolling dashboard over the most recent `window` seconds.

    `status` is an optional callable returning a dict of capture facts to show in the
    capture tile and header. Recognised keys: source, csv, rows, dropped, malformed.

    `control` is an optional object exposing rate/max_rate and set_rate(); supplying one
    adds an editable target-rate field to the capture tile. Baud switching stays a
    CLI-only affair (see --baud/--no-autobaud in main.py); the GUI no longer exposes it.
    """

    def __init__(self, window=30.0, sample_hz=200.0, fps=20.0, title="", status=None,
                 control=None):
        self.window = float(window)
        self.fps = fps
        self.title = title
        self._status = status
        self._control = control
        self._sample_hz = sample_hz
        # Headroom of 2x so a faster-than-nominal stream still fills the window.
        capacity = max(100, int(self.window * sample_hz * 2))
        self._t = deque(maxlen=capacity)
        self._series = {}
        self._lines = {}
        self._drain = None
        self._samples = 0

        for tile in TILES:
            for column, _label, _colour in tile["series"]:
                self._series[column] = deque(maxlen=capacity)
        for column in ("qx", "qy", "qz", "qw"):
            self._series[column] = deque(maxlen=capacity)

        self.figure = plt.figure(figsize=(15, 9), facecolor=PAGE_BG)
        self.figure.canvas.manager.set_window_title(
            "Nicla Sense ME" + (" - " + title if title else "")
        )

        grid = GridSpec(
            4, 12,
            figure=self.figure,
            height_ratios=(0.34, 1.0, 1.0, 0.78),
            left=0.035, right=0.988, top=0.965,
            bottom=GRID_BOTTOM_PLAIN,
            wspace=0.55, hspace=0.42,
        )

        self._header = self._make_header(grid)
        self._tiles = [self._make_tile(grid, tile) for tile in TILES]
        # Held on the instance: matplotlib widgets stop responding once garbage collected.
        self._capture = self._make_capture(grid)

        self._t_index = COLUMNS.index("t_ms")
        self._column_index = {c: COLUMNS.index(c) for c in self._series}

    # -- construction ----------------------------------------------------------

    def _blank_axes(self, axes):
        """Strip an axes down to a bare tile background."""
        axes.set_facecolor(TILE_BG)
        axes.set_xticks([])
        axes.set_yticks([])
        for spine in axes.spines.values():
            spine.set_color(TILE_EDGE)
        return axes

    def _make_header(self, grid):
        axes = self._blank_axes(self.figure.add_subplot(grid[0, :]))
        axes.set_xlim(0, 1)
        axes.set_ylim(0, 1)
        subtitle = axes.text(
            0.008, 0.5, self.title,
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
        # The window length is stated once in the header, so per-tile time ticks would
        # only repeat it at six different font sizes.
        axes.set_xticks([])
        axes.yaxis.set_major_locator(MaxNLocator(nbins=3, prune="both"))
        # Show absolute values; the default offset notation turns 998.7 hPa into a
        # near-useless "+9.987e2" tucked in the corner.
        axes.ticklabel_format(axis="y", useOffset=False, style="plain")

        axes.set_title(
            tile["title"], loc="left", pad=6,
            fontfamily=FONT, fontsize=10, color=TEXT,
        )
        readout = axes.text(
            1.0, 1.015, "",
            transform=axes.transAxes, va="bottom", ha="right",
            fontfamily=FONT, color="#FFFFFF",
            # Three components need room to sit beside the title without colliding.
            fontsize=12.5 if tile.get("value") else 11.0,
        )
        unit = axes.text(
            0.0, -0.02, tile["unit"],
            transform=axes.transAxes, va="top", ha="left",
            fontfamily=FONT, fontsize=8.5, color=MUTED,
        )

        for column_name, label, colour in tile["series"]:
            (line,) = axes.plot([], [], "-", linewidth=1.1, color=colour, label=label)
            self._lines[column_name] = line

        quaternion = None
        if tile.get("quaternion"):
            # Shares the unit line rather than hanging below the tile, where it would
            # crowd the title of whatever sits in the next row.
            quaternion = axes.text(
                0.12, -0.02, "",
                transform=axes.transAxes, va="top", ha="left",
                fontfamily=FONT, fontsize=8.5, color=MUTED,
            )

        return {"axes": axes, "tile": tile, "readout": readout, "quaternion": quaternion}

    def _make_capture(self, grid):
        row, column, span = CAPTURE_SLOT
        axes = self._blank_axes(self.figure.add_subplot(grid[row + 1, column:column + span]))
        axes.set_title(
            "CAPTURE", loc="left", pad=6,
            fontfamily=FONT, fontsize=10, color=TEXT,
        )
        axes.text(
            0.0, 0.86, "target Hz",
            transform=axes.transAxes, va="center", ha="left",
            fontfamily=FONT, fontsize=9, color=MUTED,
        )
        axes.text(
            0.62, 0.86, "actual Hz",
            transform=axes.transAxes, va="center", ha="left",
            fontfamily=FONT, fontsize=9, color=MUTED,
        )
        actual = axes.text(
            0.62, 0.6, "--",
            transform=axes.transAxes, va="center", ha="left",
            fontfamily=FONT, fontsize=24, color=ACCENT,
        )
        message = axes.text(
            0.0, 0.42, "",
            transform=axes.transAxes, va="center", ha="left",
            fontfamily=FONT, fontsize=9, color=MUTED,
        )
        # file/rows sit as a plain two-line block; the window row below them mixes text
        # with an inline entry field, so it's built from separate pieces that line up on
        # the same baseline instead of being part of that block.
        detail_top = axes.text(
            0.09, 0.309, "",
            transform=axes.transAxes, va="center", ha="left",
            fontfamily=FONT, fontsize=9.5, color=TEXT, linespacing=1.7,
        )
        axes.text(
            0.09, 0.176, "window",
            transform=axes.transAxes, va="center", ha="left",
            fontfamily=FONT, fontsize=9.5, color=TEXT,
        )
        window_suffix = axes.text(
            0.37, 0.176, "",
            transform=axes.transAxes, va="center", ha="left",
            fontfamily=FONT, fontsize=9.5, color=TEXT,
        )
        detail_drops = axes.text(
            0.09, 0.087, "",
            transform=axes.transAxes, va="center", ha="left",
            fontfamily=FONT, fontsize=9.5, color=TEXT,
        )
        capture = {
            "axes": axes, "actual": actual, "message": message,
            "detail_top": detail_top, "window_suffix": window_suffix,
            "detail_drops": detail_drops,
        }
        capture["target_box"] = (
            self._make_editable_box(
                axes, 0.02, 0.64, 0.26, 0.16,
                str(int(self._control.rate)) if self._control.rate else "",
                self._target_submitted,
            )
            if self._control is not None else None
        )
        capture["window_box"] = self._make_editable_box(
            axes, 0.22, 0.176, 0.13, 0.09, str(int(self.window)), self._window_submitted
        )
        return capture

    def _make_editable_box(self, capture_axes, x_fraction, y_center_fraction,
                            width_fraction, height_fraction, initial, on_submit):
        """An editable text field, sized and placed to sit inside the capture tile.

        A TextBox needs its own axes, which can't nest inside a data axes -- so its
        rectangle is derived from the capture tile's own figure-fraction position rather
        than laid out independently.
        """
        pos = capture_axes.get_position()
        box_axes = self.figure.add_axes((
            pos.x0 + pos.width * x_fraction,
            pos.y0 + pos.height * (y_center_fraction - height_fraction / 2),
            pos.width * width_fraction,
            pos.height * height_fraction,
        ))
        for spine in box_axes.spines.values():
            spine.set_color(TILE_EDGE)
        # TextBox sets its own axes facecolor from `color` on construction, overriding
        # anything set beforehand -- so the dark theme has to be passed in, not applied
        # after the fact the way it works for a plain Axes.
        box = TextBox(box_axes, "", initial=initial, color=BUTTON_FACE, hovercolor=BUTTON_HOVER)
        box.text_disp.set_fontfamily(FONT)
        # Scales with the box so the small inline window field doesn't overflow its tile.
        box.text_disp.set_fontsize(11 if height_fraction >= 0.16 else 9.5)
        box.text_disp.set_color(TEXT)
        # The cursor is hardcoded black by TextBox, invisible on a dark box.
        box.cursor.set_color(TEXT)
        box.on_submit(on_submit)
        return box

    def _target_submitted(self, text):
        control = self._control
        text = text.strip()
        if not text:
            return
        try:
            hz = int(float(text))
        except ValueError:
            self._announce_capture("enter a number", failed=True)
            return
        hz = max(1, min(hz, MAX_TARGET_HZ))
        self._capture["target_box"].set_val(str(hz))
        result = control.set_rate(hz)
        # The actual-Hz readout already shows a successful change; only failures are
        # worth a message of their own.
        if "failed" in result or "needs" in result:
            self._announce_capture(result, failed=True)
        else:
            self._announce_capture("")

    def _window_submitted(self, text):
        text = text.strip()
        if not text:
            return
        try:
            seconds = float(text)
        except ValueError:
            self._announce_capture("enter a number", failed=True)
            return
        seconds = max(MIN_WINDOW_S, min(seconds, MAX_WINDOW_S))
        self._capture["window_box"].set_val(str(int(seconds) if seconds == int(seconds) else seconds))
        self._resize_capacity(seconds)
        self.window = seconds
        self._announce_capture("")

    def _resize_capacity(self, new_window):
        """Grow the ring buffers if the new window needs more history than they hold.

        Shrinking never needs to touch the buffers -- they just end up holding more
        history than the display window uses, which is harmless.
        """
        needed = max(100, int(new_window * self._sample_hz * 2))
        if needed <= self._t.maxlen:
            return
        self._t = deque(self._t, maxlen=needed)
        for column, buffer in self._series.items():
            self._series[column] = deque(buffer, maxlen=needed)

    def _announce_capture(self, text, failed=False):
        message = self._capture["message"]
        message.set_text(text)
        message.set_color("#FF6037" if failed else MUTED)
        self.figure.canvas.draw_idle()
        self.figure.canvas.flush_events()

    # -- data ------------------------------------------------------------------

    def add(self, sample):
        """Append one sample tuple to the ring buffers."""
        self._t.append(sample[self._t_index] / 1000.0)
        for column, buffer in self._series.items():
            buffer.append(sample[self._column_index[column]])
        self._samples += 1

    def _measured_hz(self, times):
        """Sample rate over the visible window, which is what the stream is doing now."""
        if len(times) < 2:
            return 0.0
        span = times[-1] - times[0]
        return (len(times) - 1) / span if span > 0 else 0.0

    def _update_capture(self, times):
        status = self._status() if self._status is not None else {}
        hz = self._measured_hz(times)
        self._capture["actual"].set_text("%.0f" % hz if hz else "--")

        # The description carries the board's banner, so it restates rate and baud after
        # every change -- and this is measured independently of the buttons.
        if status.get("source"):
            self._header["subtitle"].set_text(status["source"])

        csv_path = status.get("csv") or "not logging"
        rows = status.get("rows", 0)
        top_lines = [
            "file   %s" % _shorten(csv_path, 34),
            "rows   %s" % _thousands(rows),
        ]
        self._capture["detail_top"].set_text("\n".join(top_lines))
        self._capture["window_suffix"].set_text(
            "s      buffered %s" % _thousands(len(self._t))
        )

        losses = []
        if status.get("dropped"):
            losses.append("dropped %s" % _thousands(status["dropped"]))
        if status.get("malformed"):
            losses.append("malformed %s" % _thousands(status["malformed"]))
        self._capture["detail_drops"].set_text(
            "   ".join(losses) if losses else "no drops"
        )

        colour = "#FF6037" if losses else TEXT
        self._capture["detail_top"].set_color(colour)
        self._capture["window_suffix"].set_color(colour)
        self._capture["detail_drops"].set_color(colour)

        if times:
            self._header["clock"].set_text("t + %.1f s" % times[-1])

    def _refresh(self, _frame):
        if self._drain is not None:
            self._drain()
        if not self._t:
            return []

        times = list(self._t)
        t_end = times[-1]
        t_start = t_end - self.window

        # Index of the first sample inside the window; the deque is time-ordered, so a
        # linear scan from the front is enough and avoids a bisect import.
        first = 0
        for i, t in enumerate(times):
            if t >= t_start:
                first = i
                break

        window_times = times[first:]
        stride = max(1, len(window_times) // MAX_POINTS)
        drawn_times = window_times[::stride]

        artists = []
        for entry in self._tiles:
            axes, tile = entry["axes"], entry["tile"]
            low, high = None, None
            latest = []
            for column, label, _colour in tile["series"]:
                values = list(self._series[column])[first:]
                line = self._lines[column]
                line.set_data(drawn_times, values[::stride])
                artists.append(line)
                if not values:
                    continue
                latest.append((label, values[-1]))
                vmin, vmax = min(values), max(values)
                low = vmin if low is None else min(low, vmin)
                high = vmax if high is None else max(high, vmax)

            if low is not None:
                # Widen about the midpoint to the floor, so a quiet sensor reads as
                # quiet instead of being blown up to its own noise.
                if high - low < tile["min_span"]:
                    middle = (high + low) / 2.0
                    low = middle - tile["min_span"] / 2.0
                    high = middle + tile["min_span"] / 2.0
                span = high - low
                axes.set_ylim(low - span * 0.12, high + span * 0.12)
            axes.set_xlim(t_start, max(t_end, t_start + 1e-3))

            if latest:
                if tile.get("value"):
                    text = tile["fmt"] % latest[0][1]
                else:
                    text = "  ".join(
                        "%s %s" % (label, tile["fmt"] % value) for label, value in latest
                    )
                entry["readout"].set_text(text)
                artists.append(entry["readout"])

            if entry["quaternion"] is not None:
                entry["quaternion"].set_text(
                    "quat  " + "  ".join(
                        "%s %6.3f" % (name, self._series[name][-1])
                        for name in ("qx", "qy", "qz", "qw")
                    )
                )
                artists.append(entry["quaternion"])

        self._update_capture(window_times)
        return artists

    def run(self, drain=None):
        """Show the window and animate until it is closed.

        `drain` is called once per frame on the main thread; use it to move samples from
        the source queue into the logger and into add().
        """
        self._drain = drain
        # Held on the instance so the animation is not garbage collected.
        self._animation = FuncAnimation(
            self.figure,
            self._refresh,
            interval=int(1000.0 / self.fps),
            blit=False,
            cache_frame_data=False,
        )
        plt.show()


def _thousands(value):
    return "{:,}".format(int(value))


def _shorten(text, width):
    """Keep the tail of an over-long path; the file name matters more than its parents."""
    return text if len(text) <= width else "..." + text[-(width - 3):]
