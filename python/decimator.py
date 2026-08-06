"""Rate-limit the CSV log, but keep full 200 Hz detail across interesting events.

The board streams at 200 Hz and the host throws most of it away. That direction matters:
the alternative -- asking the board for a low rate with `s<N>` and raising it when
something happens -- cannot work, because the samples that prove something happened are
exactly the ones that were never sent. Decimating on the host means the full-rate history
is always in hand, so a trigger can retroactively keep the samples *leading up to* it.

Triggering compares each watched column against its own exponential baseline. A baseline
rather than the previous sample because a per-sample difference at 200 Hz is mostly sensor
noise; a baseline with a ~0.5 s time constant tracks the resting value and ignores it.
The baseline keeps updating during a burst, so a board that moves to a new resting
attitude stops triggering once it settles there, rather than latching on forever.
"""

from columns import COLUMNS, index_of

SEQ = index_of("seq")
T_MS = index_of("t_ms")

# Sensible defaults: motion channels only. The environmental columns update at ~1 Hz and
# are held between updates, so they step rather than move and make poor triggers.
DEFAULT_TRIGGERS = (
    ("ax_g", 0.15),
    ("ay_g", 0.15),
    ("az_g", 0.15),
    ("gx_dps", 25.0),
    ("gy_dps", 25.0),
    ("gz_dps", 25.0),
)


def parse_trigger(spec):
    """Parse a `column:threshold` string into a (index, name, threshold) tuple."""
    if ":" not in spec:
        raise ValueError(
            "trigger %r must be column:threshold, e.g. ax_g:0.15" % spec
        )
    name, _, raw = spec.partition(":")
    name = name.strip()
    if name not in COLUMNS:
        raise ValueError("unknown column %r in trigger %r" % (name, spec))
    try:
        threshold = float(raw)
    except ValueError:
        raise ValueError("threshold in trigger %r is not a number" % spec)
    if threshold <= 0:
        raise ValueError("threshold in trigger %r must be positive" % spec)
    return (COLUMNS.index(name), name, threshold)


def parse_triggers(specs):
    if not specs:
        return tuple(
            (COLUMNS.index(n), n, t) for n, t in DEFAULT_TRIGGERS
        )
    return tuple(parse_trigger(s) for s in specs)


class AdaptiveDecimator(object):
    """Thin every sample stream down to `rate` Hz, except while a trigger is hot.

    feed(sample) returns a list of (sample, is_burst) pairs to write -- usually zero or
    one, but more when a trigger fires and the pre-roll is flushed.

    All timing comes from the board's `t_ms`, not host arrival time. The CMSIS-DAP bridge
    buffers, so host timing bunches samples that the board spaced evenly.
    """

    def __init__(self, rate, triggers=None, hold=1.0, pre_roll=0.25, tau=0.5,
                 burst_rate=200.0):
        if rate <= 0:
            raise ValueError("rate must be positive")
        self.rate = float(rate)
        self.period = 1.0 / self.rate
        self.triggers = parse_triggers(triggers)
        self.hold = float(hold)
        self.pre_roll = float(pre_roll)
        self.tau = float(tau)

        # Pre-roll depth in samples, sized from the incoming rate with a little slack.
        self._pre_max = max(1, int(round(self.pre_roll * burst_rate)) + 2)

        self.bursts = 0            # number of distinct burst episodes
        self.burst_rows = 0        # rows written because of a burst
        self.steady_rows = 0       # rows written on the steady grid
        self.seen = 0              # samples fed in

        self._reset_state()

    def _reset_state(self):
        self._baseline = None
        self._pre = []
        self._burst_until = None
        self._in_burst = False
        self._next_due = None
        self._last_t = None
        self._last_seq = None
        self._last_written_seq = None

    # -- internals ---------------------------------------------------------------

    def _update_baseline(self, sample, dt):
        """Return True if any watched column has left its baseline, then advance it."""
        if self._baseline is None:
            self._baseline = {i: float(sample[i]) for i, _n, _t in self.triggers}
            return False

        hot = False
        # dt/(tau+dt) is the discrete-time EMA coefficient for time constant tau. Using
        # the real dt rather than a fixed alpha keeps the time constant honest when the
        # stream rate changes or samples are dropped.
        alpha = dt / (self.tau + dt) if self.tau > 0 else 1.0
        if alpha > 1.0:
            alpha = 1.0
        for i, _name, threshold in self.triggers:
            value = float(sample[i])
            base = self._baseline[i]
            if abs(value - base) > threshold:
                hot = True
            self._baseline[i] = base + alpha * (value - base)
        return hot

    def _flush_pre_roll(self, now):
        """Emit buffered samples inside the pre-roll window that were not already kept."""
        cutoff = now - self.pre_roll
        out = []
        for buffered in self._pre:
            if buffered[T_MS] / 1000.0 < cutoff:
                continue
            if (self._last_written_seq is not None
                    and buffered[SEQ] <= self._last_written_seq):
                continue
            out.append(buffered)
        return out

    def _emit(self, sample, is_burst, out):
        out.append((sample, 1 if is_burst else 0))
        self._last_written_seq = sample[SEQ]
        if is_burst:
            self.burst_rows += 1
        else:
            self.steady_rows += 1

    # -- public ------------------------------------------------------------------

    def feed(self, sample):
        self.seen += 1
        now = sample[T_MS] / 1000.0

        # `r` on the serial link, or a board reboot, restarts the time origin and the
        # sequence counter together. Without noticing, the phase accumulator would sit years
        # in the future and nothing would ever be written again.
        #
        # Both counters have to move back, not just the clock. A row torn by a host-side
        # buffer overrun can lose a digit out of t_ms and still parse (see _consistent() in
        # sources.py, which rejects most of them); treating one of those as a reset throws
        # away the pre-roll and rephases the steady grid for a sample that never happened.
        seq = sample[SEQ]
        if (self._last_t is not None and now < self._last_t
                and (self._last_seq is None or seq < self._last_seq)):
            self._reset_state()

        dt = self.period if self._last_t is None else max(now - self._last_t, 1e-6)
        self._last_t = now
        self._last_seq = seq

        hot = self._update_baseline(sample, dt)
        if hot:
            if not self._in_burst:
                self.bursts += 1
            self._burst_until = now + self.hold

        out = []
        if self._burst_until is not None and now < self._burst_until:
            if not self._in_burst:
                for buffered in self._flush_pre_roll(now):
                    self._emit(buffered, True, out)
                self._in_burst = True
            self._emit(sample, True, out)
            # Restart the steady grid from the end of the burst rather than letting the
            # accumulator run through it, which would otherwise dump a backlog of
            # "overdue" samples the moment the burst ends.
            self._next_due = now + self.period
        else:
            self._in_burst = False
            if self._next_due is None:
                self._emit(sample, False, out)
                self._next_due = now + self.period
            elif now >= self._next_due:
                self._emit(sample, False, out)
                # Advance by whole periods so the grid stays phase-locked, but never
                # leave it in the past after a gap.
                missed = int((now - self._next_due) / self.period) + 1
                self._next_due += missed * self.period

        self._pre.append(sample)
        if len(self._pre) > self._pre_max:
            del self._pre[:-self._pre_max]

        return out

    @property
    def bursting(self):
        """True while a trigger is still holding the log at full rate."""
        return self._in_burst

    def summary(self):
        written = self.steady_rows + self.burst_rows
        kept = (100.0 * written / self.seen) if self.seen else 0.0
        return (
            "decimated to %g Hz: %d of %d samples kept (%.1f%%), "
            "%d rows in %d burst%s"
            % (self.rate, written, self.seen, kept, self.burst_rows,
               self.bursts, "" if self.bursts == 1 else "s")
        )
