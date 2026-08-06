"""What the dashboards draw: the palette, the widgets, and where they sit.

Declaration only -- no matplotlib, no browser, no rendering of any kind. Three programs
read this and draw it three different ways: plot.py into a matplotlib figure, view.py into
the same figure over a finished file, and the browser dashboard into a grid of uPlot
charts. Adding a tile here makes it appear in all three.

It lives apart from plot.py because two of those three consumers do not want matplotlib's
plotting layer imported to get at a colour: view.py used to pull thirteen constants out of
plot.py, and webhub.py serves this whole module to the browser as JSON.
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
