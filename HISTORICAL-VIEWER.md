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

## Where it runs

A copy per person, on their own machine, against the share. Not one instance served to the
network.

That decision is what removes the authentication question rather than answering it. A
served viewer would need something in front of it — a reverse proxy doing Windows
Integrated Auth was the only sound option, since there is no authentication in `viewer.py`
and putting some there would cost the project its one-dependency rule for what IIS does as
a checkbox. Running a copy each means nothing is ever served beyond loopback, and who may
read the logs is decided by the share's permissions, which AD already enforces and somebody
already has to get right for the sensors to write there at all.

The costs, stated rather than discovered: the viewer has to be installed on each machine
that wants it, reading many small files over SMB is slower than off local disk, and two
people scanning the same archive each pay to scan it once because the episode cache is per
process. At a row a minute none of those are felt; if `log_rate` is ever raised, the first
two will be.

`viewer.cmd` is the double-click. It reads `viewer.conf` beside it, so nothing site-specific
is ever edited into a script, and it uses `pushd` rather than `cd` because cmd cannot hold a
UNC path as a working directory — it warns and silently leaves you in `C:\Windows`, where
neither the script nor its config is. That is what lets the launcher live on the share next
to the logs. The console window it opens is the viewer: closing it stops it, which is the
whole on/off switch and the reason it is not a silent background task.

## Getting the files there

**Each capture pushes its own logs to the share.** Into a folder named for itself, on a
schedule, knowing about no other machine. Readers point `viewer.cmd` at that share. There is
nothing in between.

Captures write locally and copy afterwards rather than writing across the network directly,
which is the point of having a copy step at all: a capture writing to a share stops recording
when the network hiccups, and this way only the copy fails. Nothing is lost, because the next
run sends what the last one could not.

Each sensor lands in its own subdirectory, which is a requirement rather than a convention: a
capture is named only for the second it started, so two boards writing into one folder would
eventually choose the same filename. Those subdirectory names are also the board list, so the
layout does both jobs and there is no register of sensors anywhere.

Push rather than pull, and the reasoning changed once when the topology did. Pulling was
right while the destination was a directory on a machine that already existed. Once readers
became desktops, the collector existed *only* to shuttle files -- an extra always-on machine
and a single point of failure for all syncing. Pushing removes it, removes the read-only
share on every capture, and removes `sources.txt`. It also fits the installer, which already
registers a service and a logon task on every capture, so a fleet is one identical command
rather than a machine somebody has to set up by hand.

What it costs is that every capture can write to the archive, where a collector would have
been the only account that could. Scope it per subdirectory with NTFS permissions if that
matters. The pull scripts are kept in `archive/` for the same reason -- the trade is real and
the choice is reversible -- but nothing uses them.

The tolerated lag is minutes, which is what makes a copy step possible at all. It also costs
nothing: `main.py` sets `flush_every = max(1, int(log_rate))`, so at 1/60 Hz every row is
flushed as it is written.

## Security

Better than the live route on every axis, mostly by not doing things.

Nothing inbound is opened on any capture machine, and nothing is served to the network at
all: every viewer binds loopback on the machine of the person using it. Authorisation is the
share's ACLs, which AD enforces and which have to be right anyway for the sensors to write
there. No proxy, no certificate, no auth code.

Two items carry over from the dashboard and are worth fixing wherever this lands: the status
payload discloses the capture's absolute CSV path, and no route sets
`X-Content-Type-Options`, `X-Frame-Options` or a CSP.

What does **not** improve: a share full of sensor logs is still readable by whoever the share
lets in. That is now an AD permissions question rather than a Python one, which is the point.

## Testing this from a Mac

Development is happening on a Mac, and the bulk of this is testable there — more so than the
live route would have been, which is worth saying because it is a consequence of the design
rather than luck. Pulling to a central directory puts the network on the far side of a
directory boundary, so *the viewer never opens a socket to anything*. It reads a local
directory, and a local directory is a local directory on either platform.

Fixtures are the part that could have been awkward and isn't. Feeding synthetic samples
through the real `AdaptiveDecimator` and `CsvLogger` with the settings out of
`packaging/nicla.conf` produces a genuine fleet-shaped file — 29 columns, the `burst` flag,
both densities — with no board attached and no Windows. Because the motion is synthetic, the
episodes land at known times, which is exactly the ground truth the burst index needs to be
tested against.

A five-minute run of that generator is a useful thing to have looked at before writing any
drawing code: two episodes totalling one second of movement produced 890 rows, against 5 rows
for the five quiet minutes around them. Rendering that naively is what the two render modes
exist to avoid.

What cannot be tested here is the delivery step and the auth tier — `robocopy`, the scheduled
task, share semantics, and IIS or AD if it comes to that. That is the same position
`packaging/` is already in, and the same answer applies: check what can be checked statically
in `test_packaging.py`, and let the Windows workflow and one real machine cover the rest. None
of it is our code.

## Open questions

- Whether the archive's own retention is a policy, a script, or nothing for now.
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
