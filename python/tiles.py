"""What the dashboards draw: the palette, the widgets, and where they sit.

Declaration only -- no renderer, no browser, no rendering of any kind. One program reads
this and draws it: the browser dashboard, into a grid of uPlot charts. Adding a tile here
adds it there.

Being declaration only is what lets webhub.py serve this whole module to the browser as
JSON, with nothing but the standard library behind it. There were two other consumers once,
both matplotlib -- a live dashboard and view.py's offline viewer -- and pulling these
constants out of the first is what let both be deleted later without taking the layout with
them. Keep it that way: a renderer imported here would end up inside the capture's web
server, and a test enforces its absence.
"""

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
#
# `alt_unit` is optional: a second unit the viewer can switch a tile into, as
# {"unit", "mul", "add", "min_span"} applied to every series in the tile as
# `value * mul + add`. It is a display choice and nothing else -- the wire, the CSV and the
# column names stay in the unit the board reports, so a log means the same thing whoever
# was watching it. The toggle is per tab, like the window length and the theme, which is
# the same argument: two people can watch one capture and disagree about how to read it.
# `mul` must be positive, since the client converts the extremes of a window rather than
# every sample in it and relies on the order surviving.
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
        "name": "temperature",
        "title": "TEMPERATURE",
        "series": (("temp_C", "temp", "#FF6037"),),
        "value": "temp_C",
        "unit": "degC",
        "fmt": "%.2f",
        "min_span": 2.0,
        # Display only, and per tab: the wire and the CSV stay in Celsius, because the
        # column is named temp_C on both sides of a schema rule that says the two must
        # match. A file whose units depended on what a browser was showing when it was
        # written would be unreadable a month later.
        #
        # min_span is restated rather than scaled from the one above, because it is a
        # judgement about what counts as a flat line and not an arithmetic consequence:
        # 2 degC of noise floor is 3.6 degF of it.
        "alt_unit": {"unit": "degF", "mul": 1.8, "add": 32.0, "min_span": 3.6},
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
#
# Four rows of ten tiles, as 3 / 1 / 3 / 3. Every row fills its twelve columns and no tile
# is narrower than a third of the page, which is the point of the arrangement: the six
# environment tiles used to share one row at two columns each, and a two-column tile is
# about 170 px on a laptop -- a trace with three tick labels and nowhere to put a number.
#
# The magnetometer has row 1 to itself. It shared it with the gas resistance until that tile
# was dropped, and full width suits it: three components on one axis, which is the trace
# that most rewards the extra pixels. gas_ohm is still recorded -- the column is part of a
# schema the sketch and columns.py both declare -- it is simply not drawn.
#
# There used to be a hole here too, a four-column slot in row 1 for the capture tile that
# every other tile was packed around. The capture state moved to the page header, and this
# grid is twelve equal cells of sensor as a result.
PLACEMENT = {
    "orientation": (0, 0, 4),
    "accelerometer": (0, 4, 4),
    "gyroscope": (0, 8, 4),
    "magnetometer": (1, 0, 12),
    "temperature": (2, 0, 4),
    "humidity": (2, 4, 4),
    "pressure": (2, 8, 4),
    "iaq": (3, 0, 4),
    "co2": (3, 4, 4),
    "bvoc": (3, 8, 4),
}

# Tiles fed by BSEC, whose outputs are only real once the gas sensor has run in. Until
# then BSEC reports accuracy 0 and emits fixed placeholders (IAQ 25, CO2 500 ppm, bVOC
# 0.49 ppm), which look exactly like a live-but-flat trace -- so each of these tiles
# states the calibration state next to its unit rather than letting a constant pass for a
# reading. Run-in takes minutes of *uninterrupted* uptime and restarts on every board
# reset, so a stuck tile usually means something rebooted the board.
BSEC_TILES = frozenset(("iaq", "co2", "bvoc"))

# BSEC accuracy word -> what to show beside the unit. 3 is fully calibrated and says
# nothing, keeping the tile clean in the normal case.
BSEC_ACCURACY_NOTES = {
    0: ("warming up", "#FF9F1C"),
    1: ("calibrating", "#CCCC33"),
    2: ("calibrated", MUTED),
    3: (None, MUTED),
}

# Drawing every sample of a 200 Hz stream is invisible detail at tile size and costs real
# frame time, so traces are strided down to about this many points.
MAX_POINTS = 900

# Below this the trace has too few points to read; above it the ring buffer needed to
# back it (see _resize_capacity) starts costing real memory for little benefit.
MIN_WINDOW_S = 2.0
MAX_WINDOW_S = 600.0

# The palette above is the dark one, and it is the canonical one -- it is what the two
# matplotlib programs draw and what `series` colours in TILES actually contain. The
# browser dashboard also offers a light theme, and four of those colours are chosen for
# contrast against near-black and are close to invisible on white. Rather than fork TILES
# into two colour sets, the light theme looks each stroke up here and falls through to the
# original when there is no entry: only the offenders are restated, and the desktop
# rendering is untouched.
LIGHT_OVERRIDES = {
    "#d8f41d": "#6b7d00",   # accent yellow-green -- the worst of them on white
    "#CCFF00": "#6f8a00",
    "#AAF0D1": "#2f9c6e",
    "#66FF66": "#1f9e3f",
    "#FF6EFF": "#c026c0",
    "#50BFE6": "#0d7ea8",
}

# Page and tile colours for the light theme. Same roles as the constants above; kept as a
# dict because nothing in Python reads them -- they are served to the browser, which sets
# them as CSS custom properties.
LIGHT_PALETTE = {
    "page_bg": "#f4f5f4",
    "tile_bg": "#ffffff",
    "tile_edge": "#dcdedc",
    "text": "#1b2020",
    "muted": "#6c7272",
    "accent": "#6b7d00",
    "grid": "#e6e8e6",
}

DARK_PALETTE = {
    "page_bg": PAGE_BG,
    "tile_bg": TILE_BG,
    "tile_edge": TILE_EDGE,
    "text": TEXT,
    "muted": MUTED,
    "accent": ACCENT,
    "grid": GRID,
}
