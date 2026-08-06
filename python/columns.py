"""Schema shared by the Arduino sketch, the transport, and the CSV writer.

This mirrors the header emitted by nicla_stream/nicla_stream.ino. If you change the
column list there, change it here too -- SerialSource validates the board's header
against COLUMNS at connect time and will tell you when they drift apart.
"""

# Column order exactly as the sketch emits it.
COLUMNS = (
    "seq",
    "t_ms",
    "ax_g", "ay_g", "az_g",
    "gx_dps", "gy_dps", "gz_dps",
    "mx_uT", "my_uT", "mz_uT",
    "qx", "qy", "qz", "qw",
    "heading_deg", "pitch_deg", "roll_deg",
    "temp_C", "hum_pct", "press_hPa", "gas_ohm",
    "iaq", "iaq_s", "co2_eq_ppm", "bvoc_eq_ppm", "bsec_acc",
)

# Wall-clock column the logger prepends; the board's own t_ms restarts every reset and
# so cannot line up samples across sessions.
HOST_TIME_COLUMN = "host_iso"

CSV_COLUMNS = (HOST_TIME_COLUMN,) + COLUMNS

# Optional trailing column written when the logger is decimating: 1 for rows kept because
# a trigger fired, 0 for rows on the steady grid. Only present when --log-rate is used,
# so a plain full-rate capture keeps the 28-column schema the README documents.
BURST_COLUMN = "burst"

# Columns the sketch emits with zero decimals. Kept as Python ints so the CSV reads
# "500" rather than "500.0"; everything else is a float.
INTEGER_COLUMNS = frozenset(
    ("seq", "t_ms", "gas_ohm", "iaq", "iaq_s", "co2_eq_ppm", "bsec_acc")
)

# Per-column parser, positionally aligned with COLUMNS.
PARSERS = tuple(int if c in INTEGER_COLUMNS else float for c in COLUMNS)

# USB VID/PID for the Nicla Sense ME, used to auto-detect the port.
NICLA_USB_VID = 0x2341
NICLA_USB_PIDS = (0x0060, 0x8060, 0x0360)


def index_of(name):
    """Position of a column in a sample tuple."""
    return COLUMNS.index(name)
