# Architecture notes

Findings from a read-through of the host side, written up while building `python/testing/`.
It is a record of the places where the design does not hold together as well as the rest of
it does, with the evidence that made each one visible. Measurements were taken on the checked-in `.venv` (CPython 3.14) on
an Apple Silicon machine, and the commands that produced them are given so they can be
disagreed with.

Items are being worked through and headed **fixed** as they close; the finding and its
evidence stay, because the reasoning is the part worth keeping. Nothing is currently
marked `@unittest.expectedFailure` in the suite, but that remains the convention for a gap
nobody has decided to fix: prose in this file rots, a failing test makes the suite red for
something deliberate, and an expected failure does neither — it sits quietly, and the day
someone fixes it the runner reports an unexpected success and points here.

Reviewed again after the desktop dashboard was removed; the first section records what
that closed.

---

## Resolved: the desktop dashboard, and everything that hung off it

The first two entries here were about `plot.py` and `dashboard.py`. Both are gone — the
browser dashboard replaced them outright — so both findings went with them, and the two
`@unittest.expectedFailure` cases that recorded the first one went too. Kept as a stub
rather than deleted, because the measurements are the reason the decision was easy and
somebody will eventually ask why there is no matplotlib window.

**The board reset.** `t_ms` restarts at zero when the board reboots. `view.py` stitched its
timeline across that and the browser client cleared its buffers on it; `plot.py` did
neither. From a 5 s window fed 1000 samples and then 100 more starting again at zero:

```
xlim: (-4.505, 0.495)
points drawn: 1100   x range: 0.0 .. 4.995
```

An axis running from negative time, with every pre-reset sample drawn inside it — the old
capture stacked on the new one until the ring rolled over, a minute at the default window.

**Frame cost grew with the window.** `_refresh()` rebuilt its view every frame with
`list(deque)[first:]`, about twenty full copies, before anything was drawn. Data handling
only, matplotlib's drawing excluded:

```
window    30 s:    3.8 ms per _refresh
window   300 s:   63.3 ms per _refresh
window   600 s:  157.7 ms per _refresh
```

A 6 fps ceiling at `MAX_WINDOW_S` against an fps default of 20, with nothing warning about
it. The browser has no equivalent: the page holds its own buffers and the server only
reformats rows, which is the whole reason it is smoother.

Two things outlived them and are worth knowing. `tiles.py` was extracted so the
declarations were not inside the renderer, and that is what made the removal a
five-file change rather than a rewrite — `view.py` now imports its constants from there
directly. And `pipeline.py`'s `watch_source` went with `dashboard.py`: it existed only to
close a matplotlib window when the source died, carefully routed through the plot object
so that the seam needed no matplotlib. Both remaining consumers poll `source.error` in
their own loop. A test now asserts that importing either `pipeline` or `tiles` pulls in no
renderer at all, which is what that care was protecting.

## 1. Start-up failures are handled in two different ways — fixed

`main.py` was careful with most of them: no board, an unparseable `--listen`, a port
already taken, a malformed `--burst-on` each printed one line and returned 1.
`CsvLogger.open()` was called outside all of that, so a path that could not be written came
out as a traceback:

```
UNCAUGHT OSError: [Errno 30] Read-only file system: '/nope'
```

with the source already opened and no exit code worth acting on. Of the set, this was the
one most likely to happen unattended — a full disk, a mount that went away, a typo in a
config file read by cron.

The reason the gap was easy to leave open was the shape around it: the three failure paths
that *were* handled each repeated `source.stop()` and `log.close()` by hand before
returning 1, so wrapping a fourth acquisition meant writing a fourth copy of the cleanup.
That is what was fixed, rather than the instance. `main()` now acquires everything inside a
`contextlib.ExitStack`, and the CSV goes on it like the rest; the three hand-written
cleanups are one `stack.close()`, called at the top of the exit summary so the summary
still describes a capture that has stopped. Two orderings are load-bearing and are
commented as such: release is registration order reversed, and the dashboard child only
notices the capture has ended once the hub closes, so its wait is registered *before* the
hub's shutdown in order to run *after* it.

The `@unittest.expectedFailure` that recorded this is now a plain passing test, and with it
the suite has no expected failures left.

## 2. The autoscale rule lives in two languages — narrowed as far as it goes

Pulling the declarations out into `tiles.py` worked, and the suite checks that both
renderers can trust them. But the rule that *consumes* those declarations — min/max over
the undecimated window, widen about the midpoint to `min_span`, then pad 12% — was
implemented twice: `view.py:_draw_traces` and `app.js`, held in step by nothing but a
comment in each saying the order matters.

One of the two is JavaScript, so the loop itself cannot be shared, and the alternative —
computing limits on the server and shipping them — has the browser asking for numbers it
already has the data to work out, and going stale between batches. So the duplication
stays, but the *numbers* no longer can drift. `tiles.autoscale()` is the rule in Python and
`view.py` calls it; `AUTOSCALE_PAD` sits beside `min_span` in `tiles.py` and `webhub.py`
serves it, so `app.js` reads the pad out of `/spec` instead of restating `0.12`.
`testing/test_tiles.py` pins the arithmetic — including that widening happens before
padding — asserts the pad reaches `/spec`, and greps the client for a re-introduced
literal.

What is left duplicated is the shape of the loop, in two languages, which is the honest
floor for this. It is worth knowing that `tiles.py` does not solve it by itself: a change
to the *order* still has to be made twice.

## 3. Attached-viewer counts only decay when data flows

A client's writer thread finds out its socket is gone by writing to it. With the stream
idle, nothing writes, so `hub.clients` keeps counting a viewer that left — and it takes two
writes to notice, not one, since the first is absorbed before the reset comes back:

```
attached: 1
after detach, no traffic: 1
after 1 broadcast(s): 1
after 2 broadcast(s): 0
```

In a live capture this is bounded by `push_status`, which goes out once a second even when
the board has gone quiet, so the staleness is never more than about a second and nobody
will see it. It is a wart rather than a bug. It is written down because the count is
reported in the progress line and in the capture tile as though it were current, and
because `webhub.py` has the same shape for the same reason.

## 4. IPv6 parses but cannot be bound

`parse_endpoint("[::1]:8765")` correctly returns `("[::1]", 8765)` — `rpartition` gets the
bracketed form right for free. `SampleHub.start()` then constructs an `AF_INET` socket, so
the address cannot actually be served. Nothing in the project is IPv6-aware; the parsing
just happens to look as though it is.

The same `rpartition` is why `parse_endpoint("host:80:80")` yields a host called `host:80`
rather than an error. That one is defensible — the failure lands at connect time with the
bad host named — but it is the same coin.

## 5. `--rate` is silently ignored for a non-serial source

The `isinstance(source, SerialSource)` guard is correct: there is no board to ask. But a
replay run with `--rate 50` gets no acknowledgement in either direction, and the flag reads
as though it did something. One line on stderr would settle it.

## 6. Two memory ceilings worth knowing before they are hit

`view.py` loads a whole file into memory, and drops to a pure-Python reader for any file
containing one bad row — which is every file from a capture ended with Ctrl-C. A multi-hour
200 Hz log is 100 MB+ of CSV, and the tolerant path is substantially slower than
`np.loadtxt`. `testing/replay.py` has the same shape, and says so in its docstring.

Neither is wrong for the sizes this project actually produces. Both would need rethinking
before an overnight capture could be browsed comfortably.

## 7. `config.py` depends on a private argparse API

`parser._actions` is the only way to ask argparse what it accepts, and the module argues
for using it convincingly: the alternative is a second list of option names maintained by
hand, which is the exact drift the module exists to prevent. It is still private API.

This is now covered — `testing/test_config.py` builds its parser with `main.build_parser()`
rather than a fixture, so a Python release that changes `_actions` fails there rather than
in someone's overnight config file. Worth leaving as it is, with the exposure written down.

---

## Not a shortfall, but worth recording

`hub.format_sample()` re-emits rows that are *value*-equal to the board's, not
byte-identical: `str()` round-trips the parse, so `0.50` from the board leaves the hub as
`0.5`. Every consumer parses rather than compares text, so this is harmless, and it is
tested as an exact round-trip through the type system in `testing/test_wire.py`. But
"the hub re-emits the board's own wire format" is a statement about the structure, not
about the bytes, and a CSV written by a viewer will not diff clean against the capture's.

## What the suite does not reach

`SerialSource` — auto-detect, the auto-baud sweep, the `s<N>` rate handshake, and the
byte-paced command writer. That is the board, and standing in for the board is what
`replay.py` does. Those stay verified by plugging it in.
