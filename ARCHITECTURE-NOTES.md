# Architecture notes

Findings from a read-through of the host side, written up while building `python/testing/`.
Nothing here is a plan and nothing here has been fixed; it is a record of the places where
the design does not hold together as well as the rest of it does, with the evidence that
made each one visible. Measurements were taken on the checked-in `.venv` (CPython 3.14) on
an Apple Silicon machine, and the commands that produced them are given so they can be
disagreed with.

The three items marked **recorded** have an `@unittest.expectedFailure` case in the suite.
That is deliberate: a gap that is described in prose rots, and a gap that is left as a
failing test makes the suite red for something nobody has decided to fix yet. An expected
failure does neither — it sits quietly, and the day someone fixes it the runner reports an
unexpected success and points at this file.

---

## 1. `plot.py` is the only viewer that does not handle a board reset

**recorded** — `testing/test_plot.py`, class `BoardReset`

`t_ms` restarts at zero whenever the board reboots. `view.py` stitches its timeline across
that in `_timeline()`, and the browser client clears its ring buffers in `resetBuffers()`.
`plot.py` does neither: `add()` appends the new samples after the old ones and `_refresh()`
carries on.

What that looks like, from a 5 s window fed 1000 samples and then 100 more starting again
at zero:

```
xlim: (-4.505, 0.495)
points drawn: 1100   x range: 0.0 .. 4.995
```

So the axis runs from negative time, and every pre-reset sample still in the ring is drawn
inside it — the old capture stacked on top of the new one, with most of the window empty.
It clears when the ring rolls over, which at the default 30 s window is a minute of a
dashboard that looks broken rather than blank.

This is the one worth fixing first, because the other two viewers already show what the
answer looks like and `testing/replay.py` reproduces it on demand: the replay loops, and
each wrap sends `t_ms` backwards precisely because that is what a reset looks like.

## 2. Frame cost is linear in the window length

`_refresh()` rebuilds its view of the data every frame with `list(self._series[column])[first:]`
— a full copy of each deque, about twenty of them, before anything is drawn. The deques are
sized `window * sample_hz * 2`, so the copying grows with the window while the point budget
that follows it does not.

Data handling only, with matplotlib's drawing excluded entirely:

```
window    30 s:    3.8 ms per _refresh
window   300 s:   63.3 ms per _refresh
window   600 s:  157.7 ms per _refresh
```

At `MAX_WINDOW_S` that is a 6 fps ceiling before a single artist is touched, against a
`--fps` default of 20. Nothing warns about it, and the window box in the capture tile
accepts 600 happily.

The fix is not more striding — the stride already caps what is drawn. It is that the
window slice is recomputed from scratch rather than maintained, and that `MAX_POINTS`
worth of output is being selected by copying `window * 200 * 2` inputs to get there.

## 3. Start-up failures are handled in two different ways

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

## 4. The autoscale rule lives in three languages

Pulling the declarations out into `tiles.py` worked, and the suite now checks that all
three renderers can trust them. But the rule that *consumes* those declarations — min/max
over the undecimated window, widen about the midpoint to `min_span`, then pad 12% — is
implemented three times: `plot.py:_refresh`, `view.py:_draw_traces`, and `app.js`. They are
held in step by a comment in each saying the order matters.

There is no clean shared home for it: one of the three is JavaScript, so the honest options
are to accept the duplication and test it, or to move the rule to the server and have the
browser ask for limits it could compute itself. The suite now covers two of the three
implementations, which narrows the window rather than closing it. Worth stating plainly so
the next person does not assume `tiles.py` already solved this.

## 5. Attached-viewer counts only decay when data flows

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

## 6. IPv6 parses but cannot be bound

`parse_endpoint("[::1]:8765")` correctly returns `("[::1]", 8765)` — `rpartition` gets the
bracketed form right for free. `SampleHub.start()` then constructs an `AF_INET` socket, so
the address cannot actually be served. Nothing in the project is IPv6-aware; the parsing
just happens to look as though it is.

The same `rpartition` is why `parse_endpoint("host:80:80")` yields a host called `host:80`
rather than an error. That one is defensible — the failure lands at connect time with the
bad host named — but it is the same coin.

## 7. `--rate` is silently ignored for a non-serial source

The `isinstance(source, SerialSource)` guard is correct: there is no board to ask. But a
replay run with `--rate 50` gets no acknowledgement in either direction, and the flag reads
as though it did something. One line on stderr would settle it.

## 8. Two memory ceilings worth knowing before they are hit

`view.py` loads a whole file into memory, and drops to a pure-Python reader for any file
containing one bad row — which is every file from a capture ended with Ctrl-C. A multi-hour
200 Hz log is 100 MB+ of CSV, and the tolerant path is substantially slower than
`np.loadtxt`. `testing/replay.py` has the same shape, and says so in its docstring.

Neither is wrong for the sizes this project actually produces. Both would need rethinking
before an overnight capture could be browsed comfortably.

## 9. `config.py` depends on a private argparse API

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
