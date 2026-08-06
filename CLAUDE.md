# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

Firmware for an Arduino Nicla Sense ME that streams all 27 sensor columns as CSV over a
1 Mbaud UART, plus a Python host side that logs, plots live, serves the stream over TCP,
and views finished logs. There is no test suite and no linter; verification is running the
programs against the board — or, with no board to hand, `testing/replay.py`, which swaps a
logged CSV in for `SerialSource` and leaves the rest of the pipeline genuinely running.

`README.md` is unusually complete — it documents the measured throughput ceilings, the
sensor scale factors, the BSEC calibration behaviour, and the reasoning behind the
adaptive-logging and split-process designs. Read the relevant section before changing
anything in those areas; most of the surprising code has a paragraph explaining why.

## Commands

Run everything through the project virtualenv, from `python/`:

```bash
cd python && ../.venv/bin/python main.py
```

```bash
arduino-cli compile --fqbn arduino:mbed_nicla:nicla_sense nicla_stream && arduino-cli upload -p /dev/cu.usbmodemEE7B25F12 --fqbn arduino:mbed_nicla:nicla_sense nicla_stream
```

| Task | Command (from `python/`) |
|---|---|
| Capture (always headless, always serving) | `../.venv/bin/python main.py --duration 15` |
| Capture plus a dashboard on it | `../.venv/bin/python main.py --plot` |
| Attach a live dashboard | `../.venv/bin/python dashboard.py` |
| Attach a browser dashboard | `../.venv/bin/python webdash.py --open` |
| View a finished CSV | `../.venv/bin/python view.py [path-or-dir]` |
| Run a capture with no board | `../.venv/bin/python testing/replay.py logs/<file>.csv --plot` |
| Which ports exist | `../.venv/bin/python main.py --list-ports` |
| Confirm firmware holds its rate | `../.venv/bin/python bench/capture.py` |
| Encoding throughput benchmark | flash `nicla_bench`, then `../.venv/bin/python bench/runbench.py 1000000 5` |

The Arduino IDE's Serial Monitor holds the port exclusively; close it or connects fail with
`Resource busy`.

## Architecture

The host side is a source → queue → sinks pipeline. `pipeline.py` is the seam and imports
no entry point, which is what keeps `main.py` and `dashboard.py` independent of each other.

- `main.py` — the capture, and never a viewer. It always serves the stream (`--listen` only
  moves the address), and `--plot` starts `dashboard.py` as a *child process* attached over
  that socket rather than plotting in-process. Don't reintroduce an in-process plot: it puts
  a GUI event loop on the thread that has to keep draining the serial queue, and makes the
  CSV's lifetime the window's.
- `sources.py` — `SerialSource` (the board) and `StreamSource` (an attached capture) are
  interchangeable subclasses of `_ThreadedSource`; both push parsed sample tuples onto
  `source.queue` from a reader thread. `plot.py` cannot tell which it has. `SerialControl`
  sends `s`/`b`/`r`/`h` commands byte-by-byte with banner verification — the board silently
  drops bursts (no RX ring buffer), so this is not incidental caution.
- `pipeline.py` — `make_drain(source, sinks)` moves queued samples to plain callables. Only
  the CSV sink is ever decimated; plots and attached viewers always see all 200 Hz.
- `decimator.py` — `AdaptiveDecimator` thins the *file*, keeping a full-rate ring so a
  trigger can retroactively keep pre-trigger samples. Timing is from the board's `t_ms`.
- `logger.py` / `hub.py` — CSV appender (header only for new files) and the TCP fan-out.
  The hub re-emits the board's own wire format, so `nc` is a valid client and the attaching
  end reuses the board parser.
- `tiles.py` — the widget declarations. `TILES`, `PLACEMENT` on a 12-column grid, the
  palette, `BSEC_*`, `MAX_POINTS`, the window bounds. No matplotlib and no rendering, so
  the browser server can import it and serve it as JSON. Three programs draw it three
  ways; adding a tile here adds it to all three. Keep `min_span` on new tiles or a resting
  board autoscales its own quantization noise into dramatic-looking staircases.
- `plot.py` — the live tile grid, drawn by `dashboard.py` only; `main.py` does not import
  it. Re-exports the `tiles.py` constants because `view.py` has always imported them here.
- `webhub.py` / `webdash.py` / `web/` — the browser dashboard, attach-only like
  `dashboard.py`. Stdlib `ThreadingHTTPServer` + Server-Sent Events on `127.0.0.1`, and a
  client that draws with vendored uPlot. The server renders nothing; the page holds its own
  buffers, so every tab has its own window length and theme. `/spec` serves `tiles.py`, so
  the layout is never restated in JavaScript. `app.js` ports the autoscale from
  `plot.py:_refresh` and the order matters — min/max over the *undecimated* window, widen
  to `min_span`, then pad 12%; get it wrong and tiles breathe differently from the desktop
  version. Rows go out in ~20 Hz batches, not per sample.
- `view.py` — offline viewer. Min/max envelope decimation (not striding, which deletes
  short impacts), plus tolerant loading of torn final rows and mid-capture board resets.
- `columns.py` — the schema, and the only place column order lives on the host.
- `config.py` — `--config` INI loader for `main.py`. Types are derived from the parser
  passed in, not restated, so new flags are configurable for free; precedence is
  defaults < file < command line, via `parser.set_defaults()`.

Firmware: `nicla_stream.ino` formats a whole line into a `LineBuffer` and issues one
`write()`, because the core's `Serial` is an mbed `UnbufferedSerial` that busy-waits per
byte. Commands accumulate digits until a non-digit terminator, which is itself acted on.

## Conventions

- **Schema changes are two-sided.** The column list in `nicla_stream.ino` and `COLUMNS` in
  `columns.py` must match; `SerialSource` validates the board's header at connect and will
  report the drift, but it will not fix it.
- **Python stays 3.9-compatible** even though the checked-in `.venv` is newer — hence
  `class X(object)`, `%` formatting, no walrus/match.
- Prose comments explain *why*, at paragraph length where the reason is non-obvious. Match
  that register rather than adding restating one-liners.
- `logs/`, `runs/` and all `*.csv` are gitignored run artifacts; never commit captures.
- Board/rate facts worth not re-deriving: the BHI260AP caps motion sensors near 200 Hz,
  the magnetometer at 50 Hz, BSEC at ~1 Hz — slow columns are *held*, not resampled. The
  board is deliberately pinned at 200 Hz so the decimator always has full-rate history;
  the dashboard intentionally exposes no rate or baud control.
