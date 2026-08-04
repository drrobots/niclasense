"""Live scrolling plot of the Nicla stream.

Four stacked panels, each with an optional secondary axis so that quantities of very
different magnitude (pressure vs temperature, gas resistance vs IAQ) stay readable
together. Data is kept in fixed-length deques sized to the requested time window.
"""

from collections import deque

import matplotlib.pyplot as plt
from matplotlib.animation import FuncAnimation

from columns import COLUMNS

# Each panel: title, series on the left axis, left axis label, series on the right axis,
# right axis label. Series are (column, legend label, line style).
#
# `*_min_span` is the smallest y-range an axis is allowed to autoscale to. Without it, a
# board sitting still gets scaled to its own quantization steps, which renders sensor
# noise as dramatic staircases and reads as real signal. Each value is set to roughly the
# sensor's noise floor, so a resting board looks flat and genuine motion still fills the
# panel.
PANELS = (
    {
        "title": "Motion",
        "left": (("ax_g", "acc x", "-"), ("ay_g", "acc y", "-"), ("az_g", "acc z", "-")),
        "left_label": "g",
        "left_min_span": 0.5,
        "right": (
            ("gx_dps", "gyr x", "--"),
            ("gy_dps", "gyr y", "--"),
            ("gz_dps", "gyr z", "--"),
        ),
        "right_label": "deg/s",
        "right_min_span": 5.0,
    },
    {
        "title": "Magnetometer & orientation",
        "left": (("mx_uT", "mag x", "-"), ("my_uT", "mag y", "-"), ("mz_uT", "mag z", "-")),
        "left_label": "uT",
        "left_min_span": 20.0,
        "right": (
            ("heading_deg", "heading", "--"),
            ("pitch_deg", "pitch", "--"),
            ("roll_deg", "roll", "--"),
        ),
        "right_label": "deg",
        "right_min_span": 30.0,
    },
    {
        "title": "Environment",
        "left": (("temp_C", "temp", "-"), ("hum_pct", "humidity", "-")),
        "left_label": "degC / %RH",
        "left_min_span": 5.0,
        "right": (("press_hPa", "pressure", "--"),),
        "right_label": "hPa",
        "right_min_span": 2.0,
    },
    {
        "title": "Air quality",
        "left": (
            ("iaq", "IAQ", "-"),
            ("co2_eq_ppm", "CO2-eq", "-"),
            ("bvoc_eq_ppm", "bVOC-eq", "-"),
        ),
        "left_label": "IAQ / ppm",
        "left_min_span": 100.0,
        "right": (("gas_ohm", "gas R", "--"),),
        "right_label": "ohm",
        "right_min_span": 2000.0,
    },
)


class LivePlot(object):
    """Scrolling matplotlib view over the most recent `window` seconds."""

    def __init__(self, window=30.0, sample_hz=200.0, fps=20.0, title=""):
        self.window = float(window)
        self.fps = fps
        # Headroom of 2x so a faster-than-nominal stream still fills the window.
        capacity = max(100, int(self.window * sample_hz * 2))
        self._t = deque(maxlen=capacity)
        self._series = {}
        self._lines = {}
        self._drain = None

        for panel in PANELS:
            for column, _label, _style in panel["left"] + panel["right"]:
                self._series[column] = deque(maxlen=capacity)

        self.figure, axes = plt.subplots(
            len(PANELS), 1, figsize=(11, 9), sharex=True
        )
        self.figure.canvas.manager.set_window_title("Nicla Sense ME" + (" - " + title if title else ""))
        self._axes = []

        for axis, panel in zip(axes, PANELS):
            axis.set_ylabel(panel["left_label"])
            axis.set_title(panel["title"], fontsize=10, loc="left")
            axis.grid(True, alpha=0.3)
            # Show absolute values; the default offset notation turns 998.7 hPa into a
            # near-useless "+9.987e2" tucked in the corner.
            axis.ticklabel_format(axis="y", useOffset=False, style="plain")
            handles = []
            for column, label, style in panel["left"]:
                (line,) = axis.plot([], [], style, linewidth=1.0, label=label)
                self._lines[column] = line
                handles.append(line)

            twin = None
            if panel["right"]:
                twin = axis.twinx()
                twin.set_ylabel(panel["right_label"])
                twin.ticklabel_format(axis="y", useOffset=False, style="plain")
                for column, label, style in panel["right"]:
                    (line,) = twin.plot([], [], style, linewidth=1.0, label=label)
                    self._lines[column] = line
                    handles.append(line)

            axis.legend(
                handles,
                [h.get_label() for h in handles],
                loc="upper left",
                fontsize=7,
                ncol=3,
                framealpha=0.6,
            )
            self._axes.append((axis, twin, panel))

        axes[-1].set_xlabel("seconds since board reset")
        self.figure.tight_layout()

        self._t_index = COLUMNS.index("t_ms")
        self._column_index = {c: COLUMNS.index(c) for c in self._series}

    def add(self, sample):
        """Append one sample tuple to the ring buffers."""
        self._t.append(sample[self._t_index] / 1000.0)
        for column, buffer in self._series.items():
            buffer.append(sample[self._column_index[column]])

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
        artists = []
        for axis, twin, panel in self._axes:
            groups = (
                (axis, panel["left"], panel["left_min_span"]),
                (twin, panel["right"], panel["right_min_span"]),
            )
            for target, group, min_span in groups:
                if target is None:
                    continue
                low, high = None, None
                for column, _label, _style in group:
                    values = list(self._series[column])[first:]
                    self._lines[column].set_data(window_times, values)
                    artists.append(self._lines[column])
                    if values:
                        vmin, vmax = min(values), max(values)
                        low = vmin if low is None else min(low, vmin)
                        high = vmax if high is None else max(high, vmax)
                if low is None:
                    continue
                # Widen about the midpoint to the floor, so a quiet sensor reads as
                # quiet instead of being blown up to its own noise.
                if high - low < min_span:
                    middle = (high + low) / 2.0
                    low, high = middle - min_span / 2.0, middle + min_span / 2.0
                span = high - low
                # Extra room at the top keeps the legend off the traces.
                target.set_ylim(low - span * 0.08, high + span * 0.28)
            axis.set_xlim(t_start, max(t_end, t_start + 1e-3))
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
