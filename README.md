# Nicla Sense ME — sensor streaming, logging, and live plotting

An Arduino sketch that streams every Nicla Sense ME sensor over USB serial as compact CSV,
plus a Python program that appends the stream to a CSV file and plots it live.

Verified working end to end on this machine: exactly 200.000 Hz, zero dropped samples over a
15 s run, with the sample grid holding a 5 ms period (min = max = 5 ms).

```
nicla_stream/nicla_stream.ino   firmware
nicla_bench/nicla_bench.ino     throughput benchmark firmware
python/                         logger, live dashboard, log viewer
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

# Look at what you just captured
../.venv/bin/python view.py
```

| Flag | Default | Meaning |
|---|---|---|
| `--port` | auto | Serial device |
| `--baud` | `1000000` | Baud to try first; the sketch boots at 1000000 |
| `--no-autobaud` | off | Fail instead of trying other rates when `--baud` yields nothing |
| `--rate` | `0` | Ask the board to stream at N Hz on connect (0 = leave it alone) |
| `--csv` | `logs/nicla_<ts>.csv` | Output file; `none` disables logging |
| `--window` | `30` | Plot window in seconds |
| `--fps` | `20` | Plot refresh rate |
| `--no-plot` | off | Log without opening a window |
| `--duration` | `0` | Stop after N seconds (headless only; 0 = until Ctrl-C) |
| `--listen` | off | Serve the live stream to attachable plots, default `127.0.0.1:8765` |
| `--attach` | off | Plot a logger already running with `--listen` instead of the board |

```
# 5 Hz baseline, full 200 Hz for a second either side of any real motion
../.venv/bin/python main.py --log-rate 5

# Trigger on pressure instead, 3 s tail
../.venv/bin/python main.py --log-rate 1 --burst-on press_hPa:0.5 --burst-hold 3
```

Re-running against an existing CSV **appends** to it without repeating the header row.
Note that `--log-rate` adds a `burst` column, so a decimated log and a full-rate one are
not append-compatible.

## Adaptive logging

The board always streams at 200 Hz; `--log-rate` thins the *file*, not the wire. That
direction is the whole point. Asking the board for a low rate and raising it when
something happens cannot work — the samples that prove something happened are exactly the
ones that were never sent. Decimating on the host keeps full-rate history in hand, so a
trigger can retroactively keep the samples *leading up to* it (`--burst-pre`).

A trigger fires when a watched column leaves its own exponential baseline by more than its
threshold. A baseline rather than a sample-to-sample difference because at 200 Hz that
difference is mostly sensor noise; a baseline with a ~0.5 s time constant tracks the
resting value and ignores it. The baseline keeps updating during a burst, so a board that
moves to a new resting attitude stops triggering once it settles there rather than
latching on forever — raise `--burst-tau` if you want sustained motion to keep triggering.

Timing comes from the board's `t_ms`, not host arrival time: the CMSIS-DAP bridge buffers,
so host timing bunches samples the board spaced evenly. The steady grid is phase-locked
and restarts after each burst, so the end of a burst never dumps a backlog of overdue
rows. A `r` command resetting the board's time origin resets the decimator with it.

The extra `burst` column marks which rows came from a trigger (`1`) and which sit on the
steady grid (`0`) — necessary because rows are no longer evenly spaced. The plot still
receives every sample regardless; decimation is about the size of the file on disk.

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
  after about six minutes of continuous uptime during testing.
  The run-in clock is *board uptime*, and it restarts from zero on every reset — including
  a reflash. So a board that gets reset every few minutes never leaves `bsec_acc` 0, and the
  three air-quality tiles hold their placeholders no matter how long you watch. The
  dashboard labels those tiles with the calibration state so this is visible rather than
  looking like a frozen plot; `gas_ohm` is raw and keeps moving throughout, which is the
  quickest way to confirm the sensor itself is alive. Note that opening the serial port
  used to reset the board on its own — see the DTR/RTS handling in `_open_at()`.
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
- **Capture tile** — log Hz, measured Hz, and the plot window (the one editable field)

The capture tile occupies the slot the web dashboard gives its RGB LED picker. Since this
tool's job is logging rather than driving the board, it reports the log rate (lit while a
burst is recording, and reading `all` when not decimating), the measured sample rate, the
CSV being written, rows on disk, burst count, buffered samples, and any dropped or malformed
samples — that last line turns orange when either is non-zero. The **window s** field
changes how many seconds of history every tile scrolls, live — enter a value between
2 and 600 and hit enter; the ring buffers backing the traces grow to fit if you ask for
more history than they currently hold.

Tiles are declared in `TILES` in `plot.py` and positioned by `PLACEMENT`, a 12-column grid;
moving a widget is a one-line change.

Each tile has a minimum y-span (`min_span`). This matters: without it, matplotlib autoscales
a motionless board down to its own quantization steps, and sensor noise renders as dramatic
staircases that read as real signal. The floors are set near each sensor's noise level, so a
resting board looks flat while real motion still fills the tile. Traces are strided down to
~900 points per tile, which is more resolution than a tile can show and keeps redraws cheap.

Ingest runs at the full 200 Hz regardless of the redraw rate, so plotting never throttles
logging — the reader thread fills a queue that the animation callback drains on the main
thread (macOS requires matplotlib to own the main thread). That is also why closing the
window ends an all-in-one run: matplotlib owns the loop that drives the CSV. Run the
capture with `--listen` when you want the two to have separate lifetimes.

[dash]: https://github.com/arduino/ArduinoAI/tree/main/NiclaSenseME-dashboard

## Headless logger, plot on demand

Run all-in-one and the plot owns the process: matplotlib holds the main thread, the
animation callback is what drains the queue into the CSV, and closing the window ends the
capture. That is fine for a ten-minute recording and wrong for an overnight one, where you
want to glance at the stream and walk away without taking the log down with you.

`--listen` splits them. The logger keeps the serial port, the decimator and the CSV, and
publishes every sample on a TCP socket; `--attach` is the same dashboard reading that
socket instead of the board. Attach and detach as often as you like — the capture never
sees it.

```bash
# Terminal 1: the capture. Runs until Ctrl-C.
../.venv/bin/python main.py --no-plot --listen --log-rate 5

# Terminal 2: look at it, close the window, come back tomorrow
../.venv/bin/python main.py --attach
```

Points worth knowing:

- **Attached plots see every sample, not the decimated file.** `--log-rate` thins what
  lands on disk; the socket carries the full 200 Hz. So the dashboard's measured rate reads
  200 while the capture tile reads a 5 Hz log rate, and both are correct.
- **The socket speaks the board's own format** — a `#seq,t_ms,...` schema line, a `#`
  banner, then one CSV row per sample, exactly as `nicla_stream.ino` prints them. So
  `nc 127.0.0.1 8765` is a usable client, and the attaching end parses the logger with the
  same code it uses to parse the board (`StreamSource` and `SerialSource` are
  interchangeable; `plot.py` cannot tell which it has).
- **A slow viewer loses its own samples, never the capture's.** Each viewer gets a bounded
  backlog and its own writer thread; when it fills, the oldest row is dropped. A suspended
  or wedged viewer cannot back up the serial buffer and skew log timing, which is the
  failure this design exists to prevent. Viewer-side losses are reported separately from
  the capture's for the same reason.
- **The capture tile shows the logger's numbers** — its CSV path, row count, burst count
  and live burst state — pushed over the same connection once a second.
- `--attach` refuses `--csv`, `--log-rate`, `--port` and friends rather than ignoring them:
  those belong to the process that owns the board.
- It binds loopback by default. `--listen 0.0.0.0:8765` opens it to the network, which is
  unauthenticated — only worth doing on a network you trust.

## Viewing a log

`view.py` opens a finished CSV in the same tiles, the same palette, and the same 12-column
grid as the live dashboard, so a recording reads like the stream it came from. What changes
is the time axis: a fixed span you scrub and zoom instead of a window sliding under the
newest sample.

```bash
# Newest file under logs/
../.venv/bin/python view.py

# A specific file, or the newest in a directory
../.venv/bin/python view.py ../logs/nicla_20260805_231541.csv
../.venv/bin/python view.py runs/

# Drive the scrub strip off the gyro instead of accelerometer magnitude
../.venv/bin/python view.py --overview gz_dps
```

- **Overview strip** (bottom) — the whole file at a glance, accelerometer magnitude by
  default. Drag to select a range; every tile redraws to it. Burst regions are shaded,
  since they are where the logger decided something was happening and therefore the
  obvious places to zoom into.
- **Cursor** — move the mouse across any tile. The values beside each title are the row
  under the cursor, not the last row of the file, and the header shows both the offset
  into the file and the wall-clock time there.
- **Keys** — arrows pan, `+`/`-` zoom, `r` resets to the whole file.
- **File tile** — duration, mean rate, row count, burst count, capture start, and the
  range currently in view, in the slot the live dashboard uses for capture status.

Traces are min/max decimated rather than strided: each drawn bucket keeps its extremes, so
a 20 ms impact in a 10-minute file still shows up as a spike. A plain stride at that ratio
deletes it almost every time. Single-value tiles print the min and max over the visible
range under the trace, because the cursor readout is one instant and the spread is usually
the story when zoomed out.

Two things the loader handles quietly: a torn last row (from a capture killed mid-write)
is skipped with a note rather than failing the file, and a board reset mid-capture — which
restarts `t_ms` at zero and would otherwise fold the rest of the file onto the beginning —
is stitched into one monotonic timeline, also with a note. The seam is one sample interval
wide; the log does not record how long the board was away.

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

Sent to the board while streaming:

- `h` — reprint the header block (banner + column names)
- `r` — reset the sequence counter and time origin
- `s<N>` — stream at N Hz, clamped to `[1, 200]`
- `b<N>` — set the UART baud, clamped to `[9600, 1000000]`

Lines starting with `#` are metadata; everything else is data. `SerialSource` validates the
board's header against `columns.py` at connect time and reports a clear error if the
firmware and the Python schema have drifted apart.

The digits of `s`/`b` accumulate until a **non-digit terminator** arrives, and the
terminator is itself acted on — so `s100h` sets the rate *and* makes the board reprint its
banner. The banner carries `rate_hz=` and `baud=`, which is how the host confirms a command
landed without needing a dedicated ack.

Three things make this less simple than it looks, all of them handled in `sources.py`:

- **Commands get silently dropped.** The core's `Serial` is an mbed `UnbufferedSerial` with
  no RX ring buffer, so incoming bytes sit in the UARTE's few-byte FIFO until `loop()`
  reads them — and `loop()` spends milliseconds at a time busy-waiting inside `printCsv()`.
  A command written as one burst lands mid-line and is partly eaten: measured **7/12**
  delivery for `s100h` at 200 Hz, against **12/12** with a 2 ms inter-byte gap. Commands are
  therefore written one byte at a time, and verified against the banner with retries.
- **`pyserial.readline()` can hang forever here.** Its timeout resets on every byte
  received, so it only returns when a `0x0A` turns up. A baud mismatch streams dense
  *structured* garbage — probing a 115200 stream at 1 Mbaud gives ~12 kB/s at 0% printable —
  that can contain no newline at all. `_read_lines()` reads by `in_waiting` against a real
  wall-clock deadline instead.
- **Lower the rate before lowering the baud.** An oversubscribed link does not drop samples;
  it stalls `loop()`, starves `BHY2.update()`, and wedges the board.

## The dashboard does not change the board's rate

Neither rate nor baud is settable from the plot window. Baud was always CLI-only
(`--baud`, `--no-autobaud`), and the rate buttons went with the arrival of `--log-rate`:
the board is now deliberately pinned at its 200 Hz ceiling so the decimator always has
full-rate history to draw a burst from, and a control that lowered the stream rate would
silently cap what a burst could capture. Log rate is a capture setting, not a board
setting.

`s<N>` still works over the wire, and `--rate` still sends it on connect — useful for a
host that cannot carry 1 Mbaud. Combining it with `--log-rate` prints a warning, since
bursts cannot exceed whatever the board is streaming.

## Layout

| File | Role |
|---|---|
| `nicla_stream/nicla_stream.ino` | Firmware: reads the BHI260AP, emits CSV |
| `nicla_bench/nicla_bench.ino` | Benchmark firmware: free-runs each candidate encoding |
| `python/bench/runbench.py` | Drives the benchmark, reports Hz and link use per encoding |
| `python/bench/capture.py` | Raw capture: bytes/line, achieved rate, dropped samples |
| `python/columns.py` | Single source of truth for the schema |
| `python/sources.py` | `SerialSource` and `StreamSource`, both threaded into a queue |
| `python/logger.py` | CSV appender, header written only for new files |
| `python/decimator.py` | Rate limiting and burst-on-change for the CSV |
| `python/hub.py` | Serves the live stream to attached plots (`--listen`) |
| `python/plot.py` | Live tiled dashboard |
| `python/view.py` | Offline viewer for logged CSVs |
| `python/main.py` | CLI entry point; wires a source to its sinks |

## Environment

- Board: Arduino Nicla Sense ME, FQBN `arduino:mbed_nicla:nicla_sense`
- Core `arduino:mbed_nicla` 4.6.0, library `Arduino_BHY2` 1.0.8
- Python 3.9 in `.venv` — the code stays 3.9-compatible
- `pyserial` 3.5, `matplotlib` 3.9.4

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
