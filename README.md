# Nicla Sense ME — sensor streaming, logging, and live plotting

An Arduino sketch that streams every Nicla Sense ME sensor over USB serial as compact CSV,
plus a Python program that appends the stream to a CSV file and plots it live.

Verified working end to end on this machine: exactly 50.00 Hz, zero dropped samples over a
10 s run.

```
nicla_stream/nicla_stream.ino   firmware
python/                         logger + live plot
.venv/                          project virtualenv
```

## Quick start

Flash the board:

```bash
arduino-cli compile --fqbn arduino:mbed_nicla:nicla_sense nicla_stream && arduino-cli upload -p /dev/cu.usbmodemEE7B25F12 --fqbn arduino:mbed_nicla:nicla_sense nicla_stream
```

Run the logger and live plot:

```bash
cd python && ../.venv/bin/python main.py
```

The port is auto-detected by USB VID/PID, the CSV lands in `python/logs/nicla_<timestamp>.csv`,
and the plot window scrolls the last 30 seconds. Close the window to stop.

**The Arduino IDE's Serial Monitor holds the port exclusively.** Close it before running, or
you get `Resource busy`.

## Usage

```bash
# Headless capture, fixed duration, chosen file
../.venv/bin/python main.py --no-plot --duration 60 --csv runs/walk.csv

# Longer plot window, explicit port
../.venv/bin/python main.py --window 60 --port /dev/cu.usbmodemEE7B25F12

# Plot only, no logging
../.venv/bin/python main.py --csv none

# What ports exist?
../.venv/bin/python main.py --list-ports
```

| Flag | Default | Meaning |
|---|---|---|
| `--source` | `serial` | `serial` or `ble` |
| `--port` | auto | Serial device |
| `--csv` | `logs/nicla_<ts>.csv` | Output file; `none` disables logging |
| `--window` | `30` | Plot window in seconds |
| `--fps` | `20` | Plot refresh rate |
| `--no-plot` | off | Log without opening a window |
| `--duration` | `0` | Stop after N seconds (headless only; 0 = until Ctrl-C) |
| `--ble-name` | `NiclaStream` | BLE local name to scan for |

Re-running against an existing CSV **appends** to it without repeating the header row.

## CSV schema

28 columns: `host_iso` (host wall clock, added by the logger) followed by the 27 the board
emits.

| Column | Unit | Rate |
|---|---|---|
| `seq`, `t_ms` | count, ms since board reset | 50 Hz |
| `ax_g`, `ay_g`, `az_g` | g (±4 g range) | 100 Hz |
| `gx_dps`, `gy_dps`, `gz_dps` | deg/s (±2000 dps range) | 100 Hz |
| `mx_uT`, `my_uT`, `mz_uT` | µT | 100 Hz |
| `qx`, `qy`, `qz`, `qw` | unit quaternion | 100 Hz |
| `heading_deg`, `pitch_deg`, `roll_deg` | degrees | 100 Hz |
| `temp_C`, `hum_pct`, `press_hPa`, `gas_ohm` | °C, %RH, hPa, ohm | ~1 Hz |
| `iaq`, `iaq_s`, `co2_eq_ppm`, `bvoc_eq_ppm`, `bsec_acc` | index, ppm, ppm, 0–3 | ~1 Hz |

Rows are emitted on a fixed 50 Hz grid. The environmental and air-quality sensors genuinely
update at about 1 Hz, so **those columns repeat their most recent value between updates** —
they are held, not resampled. Rows are ~168 bytes, about 8.4 kB/s.

### Two things that will look like bugs but are not

- **`bsec_acc` starts at 0 and IAQ sits at 25 / CO₂-eq at 500.** Bosch's BSEC fusion reports
  its own calibration state in `bsec_acc` (0 = unstable, 3 = fully calibrated) and emits
  placeholder values until it has run in. This takes minutes on first power-up and longer
  from cold. Treat air-quality readings as meaningful only once `bsec_acc` ≥ 1. It reached 1
  within a couple of minutes during testing.
- **`temp_C` reads a few degrees above room temperature.** The BME688 sits on a powered PCB
  and self-heats. If you need ambient air temperature, calibrate the offset, or use BSEC's
  internally compensated value.

## Live plot

Four stacked panels sharing a time axis, each with a secondary axis so quantities of very
different magnitude stay readable together:

1. **Motion** — accelerometer (g, left) and gyroscope (deg/s, right)
2. **Magnetometer & orientation** — µT (left) and heading/pitch/roll (right)
3. **Environment** — temperature and humidity (left), pressure (right)
4. **Air quality** — IAQ, CO₂-eq, bVOC-eq (left), gas resistance (right)

Each axis has a minimum y-span (`*_min_span` in `plot.py`). This matters: without it,
matplotlib autoscales a motionless board down to its own quantization steps, and sensor
noise renders as dramatic staircases that read as real signal. The floors are set near each
sensor's noise level, so a resting board looks flat while real motion still fills the panel.

Ingest runs at the full 50 Hz regardless of the redraw rate, so plotting never throttles
logging — the reader thread fills a queue that the animation callback drains on the main
thread (macOS requires matplotlib to own the main thread).

## BLE mode

USB is the primary path. For untethered use, set `#define STREAM_BLE 1` at the top of
`nicla_stream.ino`, reflash, and run with `--source ble`.

The BLE path sends a packed 108-byte binary frame (`uint32 seq`, `uint32 t_ms`, 25 ×
`float32`) as a single notification, at 10 Hz rather than 50. Binary because BLE is
packet-based anyway, and 108 bytes fits inside the ATT MTU macOS negotiates. Python unpacks
it into the identical column set, so the CSV is interchangeable between transports.

This works because `BHY2.begin(NICLA_STANDALONE)` skips the library's own BLE handler,
leaving `ArduinoBLE` free for the sketch's custom service.

**BLE mode is written but untested** — it was not exercised against real hardware, since the
board was flashed in USB mode throughout.

## Serial commands

Single characters, sent to the board while streaming:

- `h` — reprint the header block (banner + column names)
- `r` — reset the sequence counter and time origin

Lines starting with `#` are metadata; everything else is data. `SerialSource` validates the
board's header against `columns.py` at connect time and reports a clear error if the
firmware and the Python schema have drifted apart.

## Layout

| File | Role |
|---|---|
| `nicla_stream/nicla_stream.ino` | Firmware: reads the BHI260AP, emits CSV |
| `python/columns.py` | Single source of truth for the schema, shared by both transports |
| `python/sources.py` | `SerialSource` and `BleSource`, both threaded into a queue |
| `python/logger.py` | CSV appender, header written only for new files |
| `python/plot.py` | Live scrolling matplotlib figure |
| `python/main.py` | CLI entry point |

## Environment

- Board: Arduino Nicla Sense ME, FQBN `arduino:mbed_nicla:nicla_sense`
- Core `arduino:mbed_nicla` 4.6.0, library `Arduino_BHY2` 1.0.8
- Python 3.9 in `.venv` — the code stays 3.9-compatible
- `pyserial` 3.5, `matplotlib` 3.9.4, `bleak` 1.1.1

Recreate the environment with:

```bash
python3 -m venv .venv && .venv/bin/python -m pip install -r python/requirements.txt
```

### Sensor scale factors

`SensorXYZ` in `Arduino_BHY2` returns **raw int16 counts**, not physical units — the sketch
applies the scaling itself, derived from the ranges it requests via `setRange()`. Verified
against known physical references on a resting board:

| Check | Measured | Expected |
|---|---|---|
| `\|accel\|` | 0.9952 g | ~1.0 g (gravity) |
| `\|mag\|` | 34.0 µT | 25–60 µT (Earth's field) |
| `\|quat\|` | 0.99996 | 1.0 (unit quaternion) |
| pressure | 998.8 hPa | ~1013 hPa at sea level |

If you change `ACCEL_RANGE_G` or `GYRO_RANGE_DPS`, the scale constants follow automatically
(`range / 32768`). The magnetometer's `1/16` µT per LSB is fixed by the BMM150.

Air quality uses `SENSOR_ID_BSEC` (115), not `SENSOR_ID_BSEC_LEGACY` (171). Its 18-byte FIFO
frame routes through the library's `SensorLongDataPacket` path, so every BSEC field is
populated without patching `SENSOR_DATA_FIXED_LENGTH`. Note that the comment block in the
library's `SensorBSEC.h` has the two format names swapped relative to the enum — the parser
code in `DataParser.cpp` is the authority.
