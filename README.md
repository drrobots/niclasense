# Nicla Sense ME — sensor streaming, logging, and live plotting

An Arduino sketch that streams every Nicla Sense ME sensor over USB serial as compact CSV,
plus a Python program that appends the stream to a CSV file and plots it live.

Verified working end to end on this machine: exactly 200.000 Hz, zero dropped samples over a
15 s run, with the sample grid holding a 5 ms period (min = max = 5 ms).

```
nicla_stream/nicla_stream.ino   firmware
nicla_bench/nicla_bench.ino     throughput benchmark firmware
python/                         logger + live dashboard
python/bench/                   benchmark harness
.venv/                          project virtualenv
```

## Quick start

Flash the board:

```bash
arduino-cli compile --fqbn arduino:mbed_nicla:nicla_sense nicla_stream && arduino-cli upload -p /dev/cu.usbmodemEE7B25F12 --fqbn arduino:mbed_nicla:nicla_sense nicla_stream
```

Run the logger and live dashboard:

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
| `seq`, `t_ms` | count, ms since board reset | 200 Hz |
| `ax_g`, `ay_g`, `az_g` | g (±4 g range) | 200 Hz |
| `gx_dps`, `gy_dps`, `gz_dps` | deg/s (±2000 dps range) | 200 Hz |
| `mx_uT`, `my_uT`, `mz_uT` | µT | 50 Hz |
| `qx`, `qy`, `qz`, `qw` | unit quaternion | 200 Hz |
| `heading_deg`, `pitch_deg`, `roll_deg` | degrees | 200 Hz |
| `temp_C`, `hum_pct`, `press_hPa`, `gas_ohm` | °C, %RH, hPa, ohm | ~1 Hz |
| `iaq`, `iaq_s`, `co2_eq_ppm`, `bvoc_eq_ppm`, `bsec_acc` | index, ppm, ppm, 0–3 | ~1 Hz |

Rows are emitted on a fixed 200 Hz grid. The magnetometer genuinely updates at 50 Hz and the
environmental and air-quality sensors at about 1 Hz, so **those columns repeat their most
recent value between updates** — they are held, not resampled. Rows are ~165 bytes, about
33 kB/s, which is 33% of the 1 Mbaud link.

### Two things that will look like bugs but are not

- **`bsec_acc` starts at 0 and IAQ sits at 25 / CO₂-eq at 500.** Bosch's BSEC fusion reports
  its own calibration state in `bsec_acc` (0 = unstable, 3 = fully calibrated) and emits
  placeholder values until it has run in. This takes minutes on first power-up and longer
  from cold. Treat air-quality readings as meaningful only once `bsec_acc` ≥ 1. It reached 1
  within a couple of minutes during testing.
- **`temp_C` reads a few degrees above room temperature.** The BME688 sits on a powered PCB
  and self-heats. If you need ambient air temperature, calibrate the offset, or use BSEC's
  internally compensated value.

## Live dashboard

A dark tile grid modelled on Arduino's own [NiclaSenseME web dashboard][dash]: one widget
per sensor, current value beside the title, scrolling trace underneath. All tiles share the
same time window, so a bump shows up in the same horizontal place everywhere.

- **Row 1** — orientation (heading/pitch/roll, with the raw quaternion under the tile),
  accelerometer, gyroscope
- **Row 2** — magnetometer, **capture**, gas resistance
- **Row 3** — temperature, humidity, pressure, IAQ, CO₂-eq, bVOC-eq

The capture tile occupies the slot the web dashboard gives its RGB LED picker. Since this
tool's job is logging rather than driving the board, it reports the measured sample rate,
the CSV being written, rows on disk, buffered samples, and any dropped or malformed
samples — that last line turns orange when either is non-zero.

Tiles are declared in `TILES` in `plot.py` and positioned by `PLACEMENT`, a 12-column grid;
moving a widget is a one-line change.

Each tile has a minimum y-span (`min_span`). This matters: without it, matplotlib autoscales
a motionless board down to its own quantization steps, and sensor noise renders as dramatic
staircases that read as real signal. The floors are set near each sensor's noise level, so a
resting board looks flat while real motion still fills the tile. Traces are strided down to
~900 points per tile, which is more resolution than a tile can show and keeps redraws cheap.

Ingest runs at the full 200 Hz regardless of the redraw rate, so plotting never throttles
logging — the reader thread fills a queue that the animation callback drains on the main
thread (macOS requires matplotlib to own the main thread).

[dash]: https://github.com/arduino/ArduinoAI/tree/main/NiclaSenseME-dashboard

## BLE mode

USB is the primary path. For untethered use, set `#define STREAM_BLE 1` at the top of
`nicla_stream.ino`, reflash, and run with `--source ble`.

The BLE path sends a packed 108-byte binary frame (`uint32 seq`, `uint32 t_ms`, 25 ×
`float32`) as a single notification, at 10 Hz rather than 200. Binary because BLE is
packet-based anyway, and 108 bytes fits inside the ATT MTU macOS negotiates. Python unpacks
it into the identical column set, so the CSV is interchangeable between transports.

This works because `BHY2.begin(NICLA_STANDALONE)` skips the library's own BLE handler,
leaving `ArduinoBLE` free for the sketch's custom service.

**BLE mode is written but untested** — it was not exercised against real hardware, since the
board was flashed in USB mode throughout.

## Throughput: why 200 Hz, and why 1 Mbaud

`Serial` on this board is a **real UART**, not USB CDC — the `NICLA` variant leaves
`SERIAL_CDC` commented out, and the USB port is a CMSIS-DAP probe whose virtual COM port
bridges to the nRF52832's UARTE. Baud therefore matters, and 115200 could not carry 200 Hz:
a 165-byte line at 200 Hz needs 33 kB/s against 115200's 11.5 kB/s. The link runs at
**1 Mbaud, the nRF52832's hardware maximum**, confirmed working through the bridge.

`nicla_bench/` measures the ceilings. Free-running each encoding at 1 Mbaud, with rates
taken from the board's own clock:

| Encoding | Bytes/sample | Max Hz, core `Serial` | Max Hz, + EasyDMA |
|---|---|---|---|
| CSV, all 27 columns (what we ship) | 162 | **238** | 588 |
| CSV, ragged (env columns only when fresh) | 119 | 310 | 732 |
| CSV, motion only (18 columns) | 119 | 303 | 732 |
| Binary, 42-byte frame | 42 | 1288 | 2333 (98% of wire) |

Two ceilings sit below the wire, and both are software:

- **The core's `Serial` is an mbed `UnbufferedSerial`** — no TX buffer, no DMA, and every
  byte busy-waits. It costs ~15.4 µs/byte against the wire's 10.0, capping the port near
  65 kB/s at *any* baud. `printCsv()` therefore formats into RAM and issues one `write()`
  rather than printing field by field.
- **`Print::print(float, decimals)` costs ~1464 µs** for 27 columns, which alone caps CSV
  near 680 Hz. The binary encoder does the same job in ~20 µs — a 73× CPU saving that
  matters more than its 4× byte saving.

**None of that is the binding constraint.** The BHI260AP is: accel, gyro, quaternion and
orientation top out near **200 Hz** (requesting 400, 800 or 1600 all still yield ~197), the
magnetometer at **50 Hz**, and the BME688/BSEC fusion at **~1 Hz**. So 200 Hz is the useful
ceiling for this sensor set, and plain CSV reaches it with ~19% margin to spare. Going
ragged or binary would buy throughput the sensors cannot supply.

Worth knowing if you revisit this:

- A **ragged CSV is cheap to add** — the environmental columns are already the tail of the
  column order, so a ragged row is just a truncated one. It buys ~30%, and costs a
  variable-width parser.
- **Dropping the slow sensors buys almost nothing on its own** (303 Hz vs 310 Hz ragged).
  They only appear once per second, so they are already ~0.5% of the bytes.
- The **EasyDMA path drives `NRF_UARTE0` behind mbed's driver** and needs the TX interrupts
  masked first, or mbed's ISR consumes `EVENTS_ENDTX` and the poll deadlocks. EasyDMA on the
  nRF52832 also caps a single transfer at **255 bytes** (`TXD.MAXCNT` is 8 bits).
- **Passthrough sensor IDs are untested.** `ACC_PASS`/`GYRO_PASS` might exceed 197 Hz, but
  subscribing to them hung the board, so the ~200 Hz ceiling above is established only for
  the corrected/fused virtual sensors this project streams.
- Measure rates from the board's `t_ms`, not host arrival times. The CMSIS-DAP bridge
  buffers, so a saturated stream drains faster than the wire early in a capture and naive
  host timing reports over 100% of link capacity.

```bash
# Flash the benchmark, then drive it
arduino-cli compile --fqbn arduino:mbed_nicla:nicla_sense nicla_bench && arduino-cli upload -p /dev/cu.usbmodemEE7B25F12 --fqbn arduino:mbed_nicla:nicla_sense nicla_bench
cd python && ../.venv/bin/python bench/runbench.py 1000000 5

# Confirm a firmware change holds its rate (works against nicla_stream)
cd python && ../.venv/bin/python bench/capture.py
```

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
| `nicla_bench/nicla_bench.ino` | Benchmark firmware: free-runs each candidate encoding |
| `python/bench/runbench.py` | Drives the benchmark, reports Hz and link use per encoding |
| `python/bench/capture.py` | Raw capture: bytes/line, achieved rate, dropped samples |
| `python/columns.py` | Single source of truth for the schema, shared by both transports |
| `python/sources.py` | `SerialSource` and `BleSource`, both threaded into a queue |
| `python/logger.py` | CSV appender, header written only for new files |
| `python/plot.py` | Live tiled dashboard |
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
