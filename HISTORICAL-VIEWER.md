# A remote viewer for the logs

A design note, not a built thing. It records why the remote viewer reads CSV files rather
than attaching to the live stream, and what the burst-mode deployment means for what it has
to draw. Nothing here is implemented; the branch exists so the decision can be weighed
against the live fan-in option in `dashboard-lan-bind` without either contaminating the
other.

## The problem

Two people want to see every board at once, from one machine on an internal LAN. That is
not what the dashboard is for: `webdash.py` attaches to exactly one capture, and its exit
condition is that capture going away. The obvious reading — teach it N sources — turns out
to be the expensive one.

## Why files rather than the live stream

The live route needs every capture machine to run `--listen 0.0.0.0:8765`, which puts an
unauthenticated socket on the network *in the process that owns the board and the CSV*.
That is a worse place for it than the viewer: a dashboard falling over costs a page, and
`main.py` falling over costs the recording. It also needs stream identity invented on the
wire, a lifecycle inversion in `webdash.py` (one board going quiet must stop being fatal),
and a host timebase, because `t_ms` is each board's own clock and two boards drift apart
with nothing on screen admitting it.

Reading the files makes four of those disappear rather than solving them:

- **Identity** is the directory the file came from. Nothing to invent.
- **The timebase** is already there. `host_iso` is column one, host wall clock at
  millisecond precision, stamped by `logger.py` on every row. The problem only existed
  because the *wire* format has no host time; the file does.
- **Exposure** does not happen. Captures keep `listen = 127.0.0.1:8765` exactly as shipped,
  and nothing inbound is opened on any capture machine.
- **Authentication** stops being ours to design. Files on a Windows share get Kerberos and
  group ACLs from AD directly, and the awkward constraint that `EventSource` cannot set
  request headers — which rules out most token schemes for `/stream` — is irrelevant when
  there is no stream.

The local loopback dashboards stay exactly as they are. This is additive: it touches
`main.py`, `webdash.py`, `webhub.py`, `decimator.py` and `retention.py` not at all.

## What the deployment writes

Burst mode at a steady 1/min, which is `packaging/nicla.conf` unchanged. Two consequences
that between them determine the entire viewer:

**The file holds two densities differing by a factor of twelve thousand.** A one-second
burst is 200 rows; the minute around it is one row. Any drawing code that treats the file as
a time series of roughly even spacing will be wrong exactly where the interesting data is.

**Rows are not evenly spaced, by design.** The steady grid restarts from the end of a burst
rather than firing off the samples it owes, so even the quiet spacing is not a reliable
60 seconds near an event. Plot against `host_iso`; never against an index times a nominal
interval.

The `burst` column makes this tractable rather than something to infer. `CsvLogger` sets
`mark_bursts` whenever the decimator is running, so every row carries a 0/1 flag and the
files on the fleet are one column wider than an undecimated capture. A reader has to handle
both shapes — the flag is deliberately absent at full rate, where it would be a constant.

## The viewer

Three routes. It is closer to a report generator than to a dashboard, and much of what makes
`webhub.py` complicated — the client registry, per-client queues, `broadcast`, backlog
priming, keepalives, flush batching — exists to hold a permanent stream open and has no
counterpart here.

    GET /            the page
    GET /spec        tiles.py and columns.py as JSON, exactly as webhub serves it
    GET /range       rows for a time window and a set of boards
    GET /events      the burst index

**The file index needs no database.** Captures are named `nicla_<timestamp>.csv`, so the
name bounds each file's start and the mtime bounds its end. That is enough to choose
candidate files for a window without opening any of them. Within a file `host_iso` is
monotonic, and at one row a minute a whole day is 1,440 rows, so scanning is fine; only
burst regions are dense enough to want seeking.

**The burst index is the front door.** With this configuration bursts *are* the events —
the moments something happened — and they are cheap to find by scanning one flag. A list of
(start, duration, board, peak) is more useful as a landing view than a 24-hour trace, and
the trend plot becomes the context you drop into from an event rather than the thing you
arrive at.

**Two render modes, keyed off the flag rather than inferred from density.** At wide zoom,
draw the 1/min trend as the trace and put bursts on a rail beneath it as marks — drawn as
trace data they are vertical smears that bury the trend they sit on. Zoomed into a burst,
draw the full-rate rows, which is where 200 Hz is exactly what is wanted.

**Downsample by min/max envelope per pixel column, never every Nth row.** Every-Nth either
drops bursts entirely or oversamples the steady grid, and the extremes are the content.

## What it reuses

More than it replaces, and this is the argument for keeping it in the project rather than
writing something separate. `tiles.py` has no renderer in it and is already served as JSON,
which is exactly the property that lets a second viewer inherit the layout, the palette,
`min_span` and the unit toggles for free. `columns.py` is the schema. uPlot is vendored and
needs no build step.

**Reuse the autoscale rule in `app.js` rather than writing a second one.** Min/max over the
range, widen to `min_span`, pad 12%. `ARCHITECTURE-NOTES.md` records that having this in two
languages was a real problem and that `app.js` is the only implementation left; a third would
be the same mistake with a new coat.

## Getting the files there

Captures keep writing locally. A scheduled `robocopy` on the viewer machine pulls from
read-only shares on each capture, against the same hard-coded list of machines the viewer
draws from.

Pull rather than push: one job to maintain instead of one per capture, and the capture
machines stay read-only with no write access to a shared archive and no scheduled task of
their own. Pointing `--csv` at a UNC path directly would look tidier and is a trap — a
network hiccup would break recording, and `retention.py`'s rule about never deleting the
file being written gets harder to reason about across a network.

The tolerated lag is minutes, which is what makes a sync step possible at all. It also
happens to cost nothing: `main.py` sets `flush_every = max(1, int(log_rate))`, so at 1/60 Hz
every row is flushed as it is written.

**Mirror or archive** is the one real decision here. `robocopy /MIR` propagates deletions, so
the central copy inherits the capture's 365-day and 4 GB horizon and stays bounded. A plain
copy accumulates and becomes the real history, outliving what any single machine keeps. The
archive is more useful for this purpose and is affordable — eight boards at rest is roughly
800 MB a year — but size for bursts rather than for rest: `nicla.conf` puts a solid day of
bursting at ~3.3 GB and caps each machine at 4 GB, so the honest worst case is about 4 GB per
board.

## Security

Better than the live route on every axis, mostly by not doing things.

Nothing inbound is opened on any capture machine. The viewer serves ordinary
request/response HTTP, so it proxies behind IIS with Windows Integrated Auth cleanly and
authorisation becomes an AD group rather than code. Short-lived requests also retire the
unbounded-connection question that a permanent event stream raises.

Two items carry over from the dashboard and are worth fixing wherever this lands: the status
payload discloses the capture's absolute CSV path, and no route sets
`X-Content-Type-Options`, `X-Frame-Options` or a CSP.

What does **not** improve: a share full of sensor logs is still readable by whoever the share
lets in. That is now an AD permissions question rather than a Python one, which is the point.

## Open questions

- **Mirror or archive**, above. Everything else is decided.
- Whether ranges are relative (`last 24 hours`) or absolute. Relative is the common case;
  absolute is what makes a view shareable between two people. Both are cheap.
- Whether the burst trigger stays motion-shaped. It defaults to the accelerometer and
  gyroscope axes, so a thermal or air-quality event with no movement is recorded at 1/min
  like everything else. That is the current intent, and `burst_cols` widens it if it stops
  being.

## What this deliberately does not do

It is not live and does not try to be. The loopback dashboard on each capture machine is the
real-time view and is unchanged; this is the historical one, and it updates when the page is
reloaded rather than on a timer. A view that refreshes itself would need the ring buffers,
the rolling window and the incremental merge that the live dashboard has, to show data that
is minutes old anyway.

Staleness must be visible on the page — a `data as of HH:MM` stamp. A view that only updates
on reload goes stale silently, and stale flat traces look exactly like a quiet sensor. The
dashboard already takes this position by labelling the BSEC tiles with their calibration
state rather than letting placeholder values pass as readings.
