# Architecture notes

Findings from a read-through of the host side, written up while building `python/testing/`.
Nothing here is a plan and nothing here has been fixed; it is a record of the places where
the design does not hold together as well as the rest of it does, with the evidence that
made each one visible. Measurements were taken on the checked-in `.venv` (CPython 3.14) on
an Apple Silicon machine, and the commands that produced them are given so they can be
disagreed with.

The item marked **recorded** has an `@unittest.expectedFailure` case in the suite.
That is deliberate: a gap that is described in prose rots, and a gap that is left as a
failing test makes the suite red for something nobody has decided to fix yet. An expected
failure does neither — it sits quietly, and the day someone fixes it the runner reports an
unexpected success and points at this file.

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
five-file change rather than a rewrite — `view.py` imported its constants from there
directly afterwards, and when `view.py` in turn was deleted the layout stayed where it was.
And `pipeline.py`'s `watch_source` went with `dashboard.py`: it existed only to
close a matplotlib window when the source died, carefully routed through the plot object
so that the seam needed no matplotlib. Both remaining consumers poll `source.error` in
their own loop. A test now asserts that importing either `pipeline` or `tiles` pulls in no
renderer at all, which is what that care was protecting.

## 1. Start-up failures are handled in two different ways

`main.py` is careful with most of them: no board, an unparseable `--listen`, a port
already taken, a malformed `--burst-on` each print one line and return 1. `CsvLogger.open()`
is called outside all of that, so a path that cannot be written comes out as a traceback:

```
UNCAUGHT OSError: [Errno 30] Read-only file system: '/nope'
```

with the source already opened and no exit code worth acting on. Of the set, this is the
one most likely to happen unattended — a full disk, a mount that went away, a typo in a
config file read by cron.

**recorded** — `testing/test_capture.py`, `test_a_csv_that_cannot_be_opened_is_an_error_not_a_traceback`

Related, and the reason the gap is easy to leave open: the three failure paths that *are*
handled each repeat `source.stop()` and `log.close()` by hand before returning 1. A fourth
would mean a fourth copy. An `ExitStack` around the resources would remove the class of
mistake rather than this instance of it.

## 2. Resolved: the autoscale rule lived in two languages

The rule that *consumes* the tile declarations — min/max over the undecimated window, widen
about the midpoint to `min_span`, then pad 12% — used to be implemented three times, then
twice: `view.py:_draw_traces` and `app.js`, held in step by nothing stronger than a comment
in each saying the order matters. The finding recorded that one of the two was JavaScript,
so the honest options were to accept the duplication and test it, or to move the rule to the
server and have the browser ask for limits it could compute itself.

Neither is what happened. `view.py` was deleted when the project was packaged for Windows —
it was the only consumer of matplotlib and numpy, which are most of the weight of a bundled
interpreter, and it ran on the developer's machine rather than on the box doing the
capturing. The rule now exists once, in `app.js`, which is where the only renderer is.

Kept rather than deleted because the duplication is the sort that comes back: a second
renderer is the obvious way to add offline viewing, and the note is the argument for making
that a file-backed *source* feeding the client that exists instead. The number is kept for
the same reason the section is.

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

## 6. A memory ceiling worth knowing before it is hit

`testing/replay.py` loads a whole file into memory, and says so in its docstring. A
multi-hour 200 Hz log is 100 MB+ of CSV, so a replay of one costs that much resident.

This was two findings until `view.py` went; that one loaded a whole file too, and dropped to
a slow pure-Python reader for any file containing one bad row — which is every file from a
capture ended with Ctrl-C. What remains is not wrong for the sizes replay is actually
pointed at, which are recordings made for tests rather than overnight captures.

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
