# Nicla Sense ME — sensor streaming, logging, and live plotting

An Arduino sketch that streams every Nicla Sense ME sensor over USB serial as compact CSV,
plus a Python program that appends the stream to a CSV file and plots it live.

Verified working end to end on this machine: exactly 200.000 Hz, zero dropped samples over a
15 s run, with the sample grid holding a 5 ms period (min = max = 5 ms).

```
nicla_stream/nicla_stream.ino   firmware
nicla_bench/nicla_bench.ino     throughput benchmark firmware
python/                         capture, browser dashboard, log viewer
python/bench/                   benchmark harness
.venv/                          project virtualenv
```

## Quick start

Flash the board:

```bash
arduino-cli compile --fqbn arduino:mbed_nicla:nicla_sense nicla_stream && arduino-cli upload -p /dev/cu.usbmodemEE7B25F12 --fqbn arduino:mbed_nicla:nicla_sense nicla_stream
```

Run the capture, and a dashboard with it:

```bash
cd python && ../.venv/bin/python main.py --plot
```

The port is auto-detected by USB VID/PID, the CSV lands in `python/logs/nicla_<timestamp>.csv`,
and a browser opens on the dashboard at `http://127.0.0.1:8988/`. Ctrl-C in the terminal
stops the capture; closing the tab only detaches the dashboard, which is a separate process.

`main.py` itself is always headless and always serves the stream on `127.0.0.1:8765`, so
without `--plot` you get a capture you can attach to whenever you like — with `webdash.py`
or `nc`. See [Capture and dashboard as separate programs](#capture-and-dashboard-as-separate-programs).

**The Arduino IDE's Serial Monitor holds the port exclusively.** Close it before running, or
you get `Resource busy`.

## Usage

```bash
# Capture, fixed duration, chosen file
../.venv/bin/python main.py --duration 60 --csv runs/walk.csv

# Dashboard on a different port, explicit serial device
../.venv/bin/python main.py --plot --http-port 9100 --port /dev/cu.usbmodemEE7B25F12

# Serve only, no logging
../.venv/bin/python main.py --csv none

# What ports exist?
../.venv/bin/python main.py --list-ports
```

| Flag | Default | Meaning |
|---|---|---|
| `--config` | none | INI file setting any of the flags below |
| `--port` | auto | Serial device |
| `--baud` | `1000000` | Baud to try first; the sketch boots at 1000000 |
| `--no-autobaud` | off | Fail instead of trying other rates when `--baud` yields nothing |
| `--rate` | `0` | Ask the board to stream at N Hz on connect (0 = leave it alone) |
| `--csv` | `logs/nicla_<ts>.csv` | Output file; `none` disables logging |
| `--retain-days` | `0` | Delete logs in that directory older than N days (0 = keep everything) |
| `--retain-gb` | `0` | Also delete the oldest while the directory is over N GB (0 = no ceiling) |
| `--plot` | off | Also start `webdash.py` against this capture's socket, and open a browser at it |
| `--http-port` | `8988` | Port for the dashboard `--plot` starts. Ignored without `--plot` |
| `--duration` | `0` | Stop after N seconds (0 = until Ctrl-C) |
| `--listen` | `127.0.0.1:8765` | Address the live stream is served on. Serving is not optional; where is |

```
# 5 Hz baseline, full 200 Hz for a second either side of any real motion
../.venv/bin/python main.py --log-rate 5

# Trigger on pressure instead, 3 s tail
../.venv/bin/python main.py --log-rate 1 --burst-on press_hPa:0.5 --burst-hold 3
```

With no board to hand, `testing/replay.py` puts a logged CSV where the serial port would
be and passes everything after the filename to `main.py`:

```bash
../.venv/bin/python testing/replay.py logs/nicla_20260803_223115.csv --plot --duration 20
```

Timing comes from the file's own `t_ms`, so a 200 Hz recording replays at 200 Hz, and it
loops — each wrap sends `t_ms` backwards, which is what a board reset looks like to a
viewer, so watching the tiles clear is a free test of that path. Everything downstream of
the source is the real thing: decimator, bursts, CSV writer, socket, dashboards. The one
piece it cannot exercise is `SerialSource` itself, which is the piece being replaced.

Re-running against an existing CSV **appends** to it without repeating the header row.
Note that `--log-rate` adds a `burst` column, so a decimated log and a full-rate one are
not append-compatible.

## Configuration file

Every flag above is also a config key, so a setup that takes a paragraph of shell can live
in a file next to the logs it produces. `python/example.conf` lists all of them with their
defaults; copy it and delete what you are not changing.

```bash
../.venv/bin/python main.py --config overnight.conf

# The file describes the setup; flags vary one run of it
../.venv/bin/python main.py --config overnight.conf --log-rate 1 --duration 300
```

```ini
# overnight.conf -- decimated, attach a plot when I feel like it
csv = runs/overnight.csv
listen = 127.0.0.1:8765

[burst]
log_rate = 5
burst_hold = 3
burst_on =
    ax_g:0.15
    press_hPa:0.5
```

**Precedence is defaults < file < command line.** Key names are the flag names with the
leading dashes dropped; `log-rate` and `log_rate` both work. Section headers are for the
reader only — any key is accepted under any heading, and keys before the first heading are
fine — because a correctly spelled key silently ignored for sitting under the wrong
heading is the worst way for a config file to fail. Misspelled keys *are* an error, with a
suggestion, since a typo would otherwise look identical to a setting that had no effect.

Two edges inherited from argparse:

- **`--burst-on` on the command line adds to the file's triggers** rather than replacing
  them, because it is a repeatable flag. Leave `burst_on` out of the file when you want
  a run's triggers to start from a clean slate.
- **On/off keys can be turned on by a file but not back off from the command line**:
  there is no `--no-plot` to counter a `plot = true`. Comment the key out instead.

`--config` is read in its own pass before the rest of the command line, and the loader is
driven by the parser itself (`config.py` takes it as an argument), so a flag added to
`main.py` becomes settable from a file with no second list to keep in step.

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

## Retention: deleting what nobody will delete

A row is about 190 bytes, so an undecimated capture writes **3.2 GB a day** and a new file
every time the process starts. That is nobody's problem when a capture is a thing you start
and stop; it is the whole problem when the capture is a Windows service that starts at boot
and restarts whenever the board is replugged.

`--retain-days` and `--retain-gb` bound the CSV's directory. Both are **off by default** —
a capture you started yourself owns its own files — and the installer turns them both on.
They answer different questions and that is why there are two. The age limit is the policy,
the thing somebody would say out loud: keep a year. The size limit is the safety net for the
board left somewhere that vibrates, where the steady rate stops being what fills the disk:

| | rows | bytes |
|---|---|---|
| 1/min for 365 days | 525,600 | ~100 MB |
| 200 Hz for 1 day, all bursts | 17,280,000 | ~3.3 GB |

Hence the installed defaults: a row a minute, 365 days, and a 4 GB ceiling sized for a year
of resting data plus a day of solid bursting. If a month of bursting happens instead, the
ceiling is what stops it rather than the disk filling.

Deletion is oldest-first by modification time rather than by the timestamp in the filename.
They usually agree, but `--csv` makes it easy to append to a file whose name is old and
whose contents are recent, and it is the contents that decide. The age limit runs first and
the size limit works on what it left, so a file removed for being old is not also counted as
having freed space. The file being written is never deleted — on Windows that would fail and
on POSIX it would succeed silently, leaving the capture writing to an unlinked inode — but it
does count towards the ceiling, since it is the one file guaranteed to be growing. A file
that cannot be removed, which on Windows means anything another process has open, is
reported and stepped over; a locked CSV is not a reason to take down a working capture.

The sweep runs once at start-up and hourly thereafter. Start-up matters more than it looks:
a service crash-looping against an absent board would otherwise never reach its first hour.

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

## Dashboard

`webdash.py` attaches to a running capture and serves the dashboard as a web page. It is
the only live viewer; `main.py --plot` starts it for you and opens a browser at it.

```bash
# Terminal 1: the capture, as above
../.venv/bin/python main.py

# Terminal 2: serve it, and open a browser at it
../.venv/bin/python webdash.py --open
```

A dark tile grid modelled on Arduino's own [NiclaSenseME web dashboard][dash]: one widget
per sensor, current value beside the title, scrolling trace underneath. All tiles share the
same time window, so a bump shows up in the same horizontal place everywhere.

- **Row 1** — accelerometer, gyroscope
- **Row 2** — orientation (heading/pitch/roll, with the raw quaternion under the tile),
  magnetometer
- **Row 3** — temperature, humidity, pressure
- **Row 4** — IAQ, CO₂-eq, bVOC-eq
- **Header strip** — the capture's own state

Four rows of ten tiles, as 2 / 2 / 3 / 3 — the four three-trace tiles paired across the top
two rows, then the environment tiles — every row filling its twelve columns and nothing
narrower than a third of the page. The six environment tiles used to share one row at two
columns each, which is about 170 px on a laptop — a trace with three tick labels and nowhere
to put a number.

**There is no gas resistance tile.** `gas_ohm` is still captured and still in the CSV — it is
part of a schema the sketch and `columns.py` both declare, and it is what BSEC computes IAQ
from — it is simply not drawn, because the derived tiles beside it are the ones worth
watching. Adding it back is one entry in `TILES` and one in `PLACEMENT`.

**Three traces need height, not width.** Orientation, accelerometer, gyroscope and
magnetometer each draw three components against one y-axis, and what separates them is
vertical space; width only buys time resolution. All tiles had the same 92 px chart, which
gave each of three traces about thirty pixels and drew a resting board as three lines on top
of one another. The four multi-component tiles now get a taller chart — `.tile.multi` in
`dash.css`, which the client marks by counting a tile's series, so a new three-trace tile
gets it without anything being told about it.

None of this is fixed: the `tiles` dialog resizes, reorders and hides per tab, and `reset to
default` comes back here.

The capture state is in the page header rather than in a tile. It was a tile once, in a
slot the Arduino dashboard gives its RGB LED picker, and it was the odd one out there:
every other cell draws a sensor over the window you chose, and this one draws nothing and
answers "is this working" instead. It also cost the grid its most awkward constraint — a
four-column hole in the middle of row 2 that every other tile had to be packed around, and
that anything rearranging them had to know about. In the header it is visible at every
width, next to the source line it describes, and the grid is twelve equal cells of sensor.

It reports the measured sample rate, the log rate (lit while a burst is recording, and
reading `all` when not decimating), the CSV being written, rows on disk, burst count,
buffered samples, and any dropped or malformed samples — the whole strip turns orange when
either of those is non-zero.

Tiles are declared in `TILES` in `tiles.py` and positioned by `PLACEMENT`, a 12-column grid;
moving a widget is a one-line change, and it moves in the offline viewer too. Each tile has
a minimum y-span (`min_span`). This matters: without it a motionless board autoscales down
to its own quantization steps, and sensor noise renders as dramatic staircases that read as
real signal. The floors are set near each sensor's noise level, so a resting board looks
flat while real motion still fills the tile. Traces are strided down to ~900 points per
tile, which is more resolution than a tile can show.

It draws in the browser rather than shipping pictures to it. The server parses the stream,
batches it into [Server-Sent Events](https://developer.mozilla.org/docs/Web/API/Server-sent_events)
and forgets about it; the page keeps its own buffers and draws the traces with
[uPlot](https://github.com/leeoniya/uPlot), vendored into `python/web/` (51 KB, MIT). There
is no build step and nothing to install — `webhub.py` is stdlib, and the page loads no
third-party URL, so it works with no network beyond loopback.

Consequences of drawing client-side, all of which are the reason for it:

- **Every tab is independent.** Its own ring buffers, its own window length, its own theme,
  its own units. Two people can watch one capture over different spans, and neither sees the
  other's cursor. This is why `main.py` has no `--window` or `--fps` to pass: neither is a
  property of the capture, or even of the dashboard process.
- **Which tiles, how wide, in what order** — the `tiles` button opens a dialog with a row
  per tile: a checkbox, a width in grid columns, and up/down. Per tab and persisted, and
  `reset to default` restores `tiles.py`'s layout exactly. The declared layout in
  `PLACEMENT` is absolute `(row, column, span)`, so it cannot survive a tile being hidden or
  widened — a hole does not close and a wide tile overlaps its neighbour. So the grid is
  all-or-nothing: touch anything and the whole page switches to auto-flow, every tile
  keeping a width and the browser packing them in order. That is not a new mechanism to
  trust — it is what the page already does at every width below 1180px. Hidden tiles are
  skipped by the draw loop, which makes hiding one a small speed-up rather than a cost.
- **Units are a display choice, and only that.** A tile may declare an `alt_unit` in
  `tiles.py` — temperature declares Fahrenheit — and the header grows a toggle that switches
  every such tile between the two, persisted per tab. The whole visible window converts, not
  just the samples after the click, because the conversion happens on the way to the canvas
  and the buffers hold what the board sent. Nothing converted ever reaches the wire or the
  CSV: the column is called `temp_C` on both sides of a schema rule that says the two must
  match, and a log whose units depended on what a browser was showing when it was written
  would be unreadable a month later. The autoscale floor is declared separately for the
  alternative unit, since 2 °C of noise floor is 3.6 °F of it and that is a judgement about
  what counts as flat rather than a length to be converted.
- **The server does no rendering.** It sits near 3% CPU with tabs attached, because all it
  does is reformat rows.
- **Light and dark themes**, from `prefers-color-scheme` with a toggle that overrides it and
  persists. Colours live in `web/dash.css` as custom properties; the charts read them back
  through `getComputedStyle` on every draw, so switching is a redraw with nothing
  reconfigured. Four trace colours are chosen against near-black and are illegible on
  white, so `tiles.LIGHT_OVERRIDES` substitutes darker equivalents for those and passes the
  rest through.
- **It reflows.** Two tiles abreast on a tablet, one on a phone.

Deliberately absent: any control over the board. Window length is a client-side choice
about how much of the buffer to draw, and nothing here can change the rate — see
*The dashboard does not change the board's rate*.

Like `--listen`, it binds loopback and there is no flag to change that. It is an
unauthenticated live feed of whatever the board can hear.

One thing worth knowing when it looks broken:

- **A tab opens on the last 30 seconds, not on an empty window**, because the server keeps
  a short backlog for arrivals. Pick a 300 s window and the first five minutes fill in from
  the live stream rather than appearing at once.

There was a matplotlib dashboard — `dashboard.py` and `plot.py` — doing this job in a
desktop window. It is gone, and the browser one is the default because it is smoother: the
drawing is vectors in a browser rather than a Python process pushing artists around, and
none of the awkwardness of a GUI event loop in the same interpreter as a capture came with
it. The tile declarations survived it, because they were never in it.

[dash]: https://github.com/arduino/ArduinoAI/tree/main/NiclaSenseME-dashboard

## Capture and dashboard as separate programs

There was once an all-in-one mode, and it had the coupling you would expect: matplotlib
held the main thread, so the animation callback was what drained the queue into the CSV,
and closing the window ended the capture. Fine for a ten-minute recording, wrong for an
overnight one, where you want to glance at the stream and walk away without taking the log
down with you.

So `main.py` is always headless and always serves. It keeps the serial port, the decimator
and the CSV, and publishes every sample on a TCP socket; `webdash.py` reads that socket
instead of the board. Attach and detach as often as you like — the capture never sees it.

```bash
# Terminal 1: the capture. Runs until Ctrl-C.
../.venv/bin/python main.py --log-rate 5

# Terminal 2: look at it, close the tab, come back tomorrow
../.venv/bin/python webdash.py --open

# ...or a capture on another port, or another machine
../.venv/bin/python webdash.py 8790
../.venv/bin/python webdash.py bench.local:8765
```

Points worth knowing:

- **Attached viewers see every sample, not the decimated file.** `--log-rate` thins what
  lands on disk; the socket carries the full 200 Hz. So the dashboard's measured rate reads
  200 while the header's log rate reads 5 Hz, and both are correct.
- **The socket speaks the board's own format** — a `#seq,t_ms,...` schema line, a `#`
  banner, then one CSV row per sample, exactly as `nicla_stream.ino` prints them. So
  `nc 127.0.0.1 8765` is a usable client, and the attaching end parses the logger with the
  same code it uses to parse the board (`StreamSource` and `SerialSource` are
  interchangeable; nothing downstream can tell which it has).
- **A slow viewer loses its own samples, never the capture's.** Each viewer gets a bounded
  backlog and its own writer thread; when it fills, the oldest row is dropped. A suspended
  or wedged viewer cannot back up the serial buffer and skew log timing, which is the
  failure this design exists to prevent. Viewer-side losses are reported separately from
  the capture's for the same reason.
- **The header strip shows the logger's numbers** — its CSV path, row count, burst count
  and live burst state — pushed over the same connection once a second.
- **The viewer is its own program, not a mode.** Attaching shares none of the capture's
  settings — no port, no baud, no CSV, no log rate — because those belong to whoever holds
  the board. A dashboard mode on `main.py` would be a flag that quietly invalidates half
  the others, so there isn't one.
- **`--plot` is a shortcut, not an exception to that.** It starts `webdash.py` as a child
  process pointed at this capture's own socket, which is why closing the tab detaches a
  viewer rather than stopping the capture, and why `--http-port` is the only thing passed
  through to it. A dashboard that fails to start is a warning, not a dead capture.
- It binds loopback. `--listen 0.0.0.0:8765` opens it to the network, which is
  unauthenticated — only worth doing on a network you trust.

## Viewing a log

There is no offline viewer any more. `view.py` drew a finished CSV into a matplotlib figure
in the same tiles as the live dashboard, and it was removed when the project was packaged
for Windows: it was the only thing that needed matplotlib and numpy, which between them are
some fifty megabytes of the bundled interpreter, for a program that runs on the developer's
machine rather than on the box doing the capturing. The trade was deliberate and it is a
real loss — a finished capture is now a CSV like any other, for pandas or a spreadsheet or
whatever else reads one.

What went with it is worth knowing, because it is what an offline viewer has to do again if
one comes back. Traces were min/max decimated rather than strided, so that a 20 ms impact in
a ten-minute file still drew as a spike; a plain stride at that ratio deletes it almost every
time. The loader was tolerant of two things a live reader never sees: a torn last row, from a
capture killed mid-write, and a board reset mid-capture, which restarts `t_ms` at zero and
would otherwise fold the rest of the file onto its beginning. The browser client still
handles the reset — see `app.js` — because a capture can outlive a reboot of the board.

The shape a replacement should take is a file-backed source rather than a second renderer:
`sources.py` already defines the interface, `testing/replay.py` already implements most of
it, and feeding a finished CSV through `webdash.py` would put a log in the browser client
that exists rather than in a drawing layer that would have to be written twice again.

## Running it on Windows, unattended

`packaging/` builds an installer that puts the capture on a Windows machine as a service
that starts at boot and never stops, plus a dashboard that appears in the logged-on user's
browser at <http://127.0.0.1:8988/>. Neither shows a window. The target machine needs no
Python and no internet — the installer carries an embeddable interpreter with pyserial
unpacked into it, which is most of why the offline viewer and its matplotlib dependency
were deleted.

Every push builds one. To take the current build rather than making your own:

```bash
gh run download --name NiclaSense-Setup
```

To build it yourself — needs Windows, Inno Setup 6, and a Python on `PATH` to resolve the
wheel:

```powershell
.\packaging\build.ps1 -AppVersion 1.0.0
```

**`packaging/README.md` is the detail** — the installed layout, why the capture is a service
and the dashboard is a logon task, what `supervise.py` is for, and the known limits. The
short version of `supervise.py`: `main.py` is entitled to exit, and at boot it usually does,
because a service starts before USB enumeration finishes.

The installed configuration is where the two halves of this README meet. It logs one row a
minute and bursts to the full 200 Hz on movement (see **Adaptive logging**), and keeps a year
of that or 4 GB, whichever comes first (see **Retention**). Both are off by default
everywhere else.

What is verified and what is not, plainly: the installer builds on every push and the staged
tree is made to import the app under its bundled interpreter before the compile is attempted,
so a packaging mistake fails in CI. But nothing has yet *run* the installed thing — service
registration, the logon task, and the board being found over a COM port are unverified until
somebody installs it on a real Windows machine with a board attached.

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

## Tests

```bash
cd python && ../.venv/bin/python testing/run.py
```

Stdlib `unittest`, nothing to install, about forty seconds — most of which is real time
spent replaying recordings at the rate they were recorded at. Name modules to run a subset:

```bash
cd python && ../.venv/bin/python testing/run.py decimator hub
```

Output is buffered and shown only for tests that fail, because `test_capture.py` runs
`main.py` for real and a capture is chatty.

What the suite is for, module by module:

| Module | What it pins down |
|---|---|
| `test_schema.py` | The two-sided schema rule, statically. Parses the sketch's header literal, `COLUMN_COUNT` and `DECIMALS`, and checks all three against `columns.py` — including that a column the board prints with zero decimals is parsed as an `int` here |
| `test_wire.py` | The line protocol as a closed loop: a sample formatted and reparsed is the same sample, types included. Plus framing across split reads, malformed counting, banners, and endpoint parsing |
| `test_decimator.py` | Steady rate, grid lock over a long file, retroactive pre-roll, no row written twice, a sustained tilt settling instead of latching, and a board reset restarting the grid |
| `test_pipeline.py` | Sinks are independent and decimation reaches the file only; the drain's bound; status merging at both ends of an attached viewer |
| `test_retention.py` | What the sweep refuses to delete — the active file, anything inside the limits, anything that is not a CSV, everything when the limits are off — plus age running before size, and a locked file being stepped over rather than raised |
| `test_logger.py` | Header written once however often a file is appended to; integer columns reaching disk without a decimal point; the `burst` column only when decimating |
| `test_hub.py` | The TCP hub over a real socket: schema and banner before data, fan-out to several viewers, status as a comment, drop-oldest backpressure, and that a second capture on a taken port is refused and says so — which on Windows it was not, until CI ran there |
| `test_webhub.py` | `/spec` really does carry everything `tiles.py` declares; the routes, including that traversal gets nowhere; and that SSE rows arrive batched rather than one event per sample |
| `test_tiles.py` | The declarations the renderer trusts and does not validate: every series names a real column, every tile is placed, nothing overlaps, every tile has a `min_span` — and that importing them reaches nothing outside the standard library |
| `test_config.py` | Precedence, unknown-key suggestions, and the two argparse edges documented above. Uses `main.build_parser()`, so a new flag is covered the day it is added |
| `test_capture.py` | A whole capture end to end through `replay.py`, with a viewer attached over TCP, checking the CSV and the socket against the recording that went in |
| `test_packaging.py` | The three parts of the Windows build that can be checked away from Windows: `supervise.py`'s restart loop, the installed `nicla.conf` parsed with `main.py`'s own parser, and what `build.ps1` stages |

One case is marked `@unittest.expectedFailure`, recording a known gap rather than
describing it: `test_capture.py` on an unwritable `--csv` path coming out as a traceback
rather than as the one-line error every other start-up failure gets. Fix it and the suite
reports an unexpected success. (There were three; the other two recorded the desktop
dashboard's missing board-reset handling, and went with it.)

`SerialSource` is the one part not covered — auto-detect, the auto-baud sweep, the `s<N>`
handshake, the byte-paced command writer. That is the board, and standing in for the board
is what `replay.py` does. Those stay verified by plugging it in.

`ARCHITECTURE-NOTES.md` records the places the host side does not hold together as well as
the rest of it does, with the measurements behind each one.

### Continuous integration

`.github/workflows/windows-installer.yml` runs on every push to `master` and on pull
requests. It is the only place the Windows half of this project executes at all — it is
written on a Mac — so it is as much a test as a build.

| Job | What it runs |
|---|---|
| `test` ×3 | The suite on Windows/3.9 (the floor the code keeps to), Windows/3.12 (what the installer bundles), and macOS/3.12 (where it is written) |
| `installer` | `build.ps1`, which fetches the runtime, stages the tree, imports the app under the bundled interpreter, and compiles the setup |

The installer lands as a run artifact, so the current build is always a download away:

```bash
gh run download --name NiclaSense-Setup
```

3.9 is only on Windows because the macOS runners are arm64 and `setup-python` has no 3.9
build for them.

**It earned its keep on the first run.** `SO_REUSEADDR` does not mean the same thing on both
platforms. On Unix it means "rebind a port still in `TIME_WAIT` from a previous process",
which a capture restarted immediately after being stopped needs, and it does not let two
live sockets hold one port. On Windows it means nearly the opposite: a second socket may
bind an address another is *actively listening on*, and which of them a connection reaches
is unspecified. So a second capture on Windows would not have exited with "another logger is
probably already running" — it would have started silently and taken an arbitrary share of
the viewers, and the same for a second dashboard. Three tests said so the first time they
ran on Windows, and nothing on a Mac could have. `hub.REUSE_ADDR` now sets the flag only
where it means what this project wants; the Windows default is already the wanted behaviour.

The same run failed a test that was measuring the runner rather than the code — SSE batching
fed at a paced 5 ms and read for a fixed 1.6 s, where a loaded runner delivered 51 rows of
200. A longer timeout would not have fixed it: once the gap between samples exceeds
`FLUSH_INTERVAL`, every sample honestly does get its own event and the assertion fails from
the other side. Timing-dependent tests in `test_webhub.py` now read until they have what they
are waiting for.

Builds warn that they cannot verify their downloads until `packaging/hashes.txt` exists; run
`build.ps1 -Record` once from a build you trust, commit it, and CI switches to `-Verify` on
its own.

## Layout

| File | Role |
|---|---|
| `nicla_stream/nicla_stream.ino` | Firmware: reads the BHI260AP, emits CSV |
| `nicla_bench/nicla_bench.ino` | Benchmark firmware: free-runs each candidate encoding |
| `python/bench/runbench.py` | Drives the benchmark, reports Hz and link use per encoding |
| `python/bench/capture.py` | Raw capture: bytes/line, achieved rate, dropped samples |
| `python/columns.py` | Single source of truth for the schema |
| `python/config.py` | Reads `--config`, typed from main.py's own parser |
| `python/example.conf` | Every switch as a config key, with defaults |
| `python/sources.py` | `SerialSource` and `StreamSource`, both threaded into a queue |
| `python/logger.py` | CSV appender, header written only for new files |
| `python/decimator.py` | Rate limiting and burst-on-change for the CSV |
| `python/retention.py` | Deletes old logs by age and total size, for captures nobody is watching |
| `python/hub.py` | Serves the live stream to attached viewers over TCP |
| `python/webhub.py` | Serves the same stream to browsers, over SSE, plus the page |
| `python/pipeline.py` | The seam: a source's queue on one side, sample sinks on the other |
| `python/tiles.py` | The tiles, palette and grid placement — declaration only, no drawing |
| `python/main.py` | The capture: port, decimator, CSV, socket. Headless, always |
| `python/webdash.py` | The dashboard: attaches to a capture and serves it to browsers |
| `python/web/` | The browser client: page, stylesheet, drawing code, vendored uPlot |
| `python/testing/replay.py` | Runs a capture against a logged CSV, for when there is no board |
| `python/testing/run.py` | Test runner: all modules, or the ones named on the command line |
| `python/testing/support.py` | Test helpers: typed sample builders, a free port, `wait_for` |
| `python/testing/test_*.py` | One module per thing under test; see **Tests** |
| `packaging/build.ps1` | Fetches the runtime, stages the tree, compiles the installer |
| `packaging/nicla.iss` | The installer itself: layout, service registration, uninstall |
| `packaging/nicla.conf` | The installed capture's settings — decimation and retention on |
| `packaging/service/supervise.py` | Restarts the capture, and gives pythonw's null streams somewhere to go |
| `packaging/service/nicla-capture.xml` | WinSW's service definition |
| `packaging/service/dashboard-task.ps1` | Registers the logon task that starts the dashboard |
| `.github/workflows/windows-installer.yml` | Runs the suite on three interpreters, then builds the installer |

## Environment

- Board: Arduino Nicla Sense ME, FQBN `arduino:mbed_nicla:nicla_sense`
- Core `arduino:mbed_nicla` 4.6.0, library `Arduino_BHY2` 1.0.8
- The code stays 3.9-compatible, whatever the checked-in `.venv` happens to be — hence
  `class X(object)`, `%` formatting, no walrus and no `match`. CI runs the suite on 3.9,
  so the convention is now enforced rather than merely stated
- `pyserial` 3.5 — the only dependency; everything else is the standard library
- The installer bundles CPython 3.12 (embeddable) rather than the developer's interpreter

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
