"""AdaptiveDecimator: the piece with the most behaviour and the least visibility.

It runs inside a capture, writes to a file nobody watches while it is being written, and
the only evidence it worked is a row count in the exit summary. Every property below is
one that would be plausible-looking and wrong in a log: a steady rate that is really the
input rate, a burst that starts one sample after the interesting one, a board reset that
stops the file growing for the rest of the session.

Samples are fed directly rather than through a source, which is the point -- this is
arithmetic over t_ms, so it can be tested at whatever rate the assertion needs with no
threads, no sleeps, and no clock.
"""

import unittest

import support
from columns import COLUMNS
from decimator import AdaptiveDecimator, parse_trigger, parse_triggers

AX = COLUMNS.index("ax_g")


def run(decimator, samples):
    """Feed a whole list, returning the flat (sample, is_burst) output."""
    out = []
    for one in samples:
        out.extend(decimator.feed(one))
    return out


def seqs(written):
    return [row[support.SEQ] for row, _burst in written]


class SteadyRate(unittest.TestCase):
    """With nothing moving, the file should come out at the rate that was asked for."""

    def test_thins_to_the_requested_rate(self):
        decimator = AdaptiveDecimator(rate=5.0)
        written = run(decimator, support.ramp(2000, hz=200.0))  # ten seconds

        self.assertEqual(decimator.bursts, 0)
        self.assertEqual(decimator.burst_rows, 0)
        # The input spans 0 .. 9.995 s, so the 5 Hz grid lands on 0.0 .. 9.8: fifty rows,
        # the first of them the capture's very first sample.
        self.assertEqual(len(written), 50)
        self.assertEqual(decimator.seen, 2000)

    def test_kept_rows_stay_locked_to_the_grid(self):
        """Spacing must not wander, and must not accumulate error over a long file.

        The accumulator advances by whole periods rather than being reset to `now`, which
        is what keeps the last row of an overnight capture on the same grid as the first.
        The tolerance below is one input sample: t_ms is an integer millisecond count and
        the grid rarely falls exactly on one, so each row is kept at the first sample at
        or after its slot. Anything looser than that would be drift.
        """
        decimator = AdaptiveDecimator(rate=10.0)
        written = run(decimator, support.ramp(20000, hz=200.0))  # a hundred seconds
        times = [row[support.T_MS] for row, _burst in written]
        gaps = [b - a for a, b in zip(times, times[1:])]
        self.assertTrue(gaps)
        for gap in gaps:
            self.assertLessEqual(abs(gap - 100), 5, "gap of %d ms" % gap)
        # End to end, not just gap by gap: a bias of half a sample per row would satisfy
        # the check above and still be seconds out by the end of the file.
        self.assertLessEqual(abs((times[-1] - times[0]) - 100 * len(gaps)), 5)

    def test_first_sample_is_always_written(self):
        """A file that starts a fifth of a second late looks like a slow start-up."""
        decimator = AdaptiveDecimator(rate=5.0)
        written = decimator.feed(support.sample(seq=0, t_ms=0))
        self.assertEqual(len(written), 1)
        self.assertEqual(written[0][1], 0)

    def test_a_gap_does_not_leave_the_grid_in_the_past(self):
        """After a stall the grid catches up in one step, not one row per missed slot.

        A source that goes quiet for a while -- a USB hiccup, a board being reflashed --
        leaves the phase accumulator behind. If it advanced by a single period per sample
        it would spend the next stretch emitting every sample it saw to work off the
        backlog, which puts a burst-shaped lump in the file with no trigger behind it.
        """
        decimator = AdaptiveDecimator(rate=5.0)
        run(decimator, support.ramp(10, hz=200.0))
        before = decimator.steady_rows

        # Thirty seconds of nothing, then a normal stream again.
        resumed = support.ramp(200, hz=200.0, start_seq=10, start_ms=30000)
        written = run(decimator, resumed)

        self.assertEqual(decimator.bursts, 0)
        # One second of stream at 5 Hz, plus the row that closes the gap itself.
        self.assertLessEqual(len(written), 7)
        self.assertGreater(decimator.steady_rows, before)


class Bursts(unittest.TestCase):
    def quiet_then_shake(self, quiet=400, shake=100, level=1.0):
        """400 still samples, then `shake` samples with the accelerometer displaced."""
        samples = support.ramp(quiet, hz=200.0)
        samples += support.ramp(
            shake, hz=200.0, start_seq=quiet, start_ms=quiet * 5, ax_g=level
        )
        return samples

    def test_a_trigger_records_at_full_rate(self):
        decimator = AdaptiveDecimator(rate=5.0, hold=1.0, pre_roll=0.25)
        written = run(decimator, self.quiet_then_shake())

        self.assertEqual(decimator.bursts, 1)
        self.assertTrue(decimator.burst_rows > 100)
        moving = [row for row, burst in written if burst]
        self.assertTrue(moving, "the shake produced no burst rows at all")

    def test_the_burst_keeps_samples_from_before_the_trigger(self):
        """The whole reason decimation happens on the host rather than the board.

        The samples that prove something started are the ones just before the threshold
        was crossed, and they only exist to be kept because the full-rate stream was
        already in hand.
        """
        decimator = AdaptiveDecimator(rate=1.0, hold=1.0, pre_roll=0.25)
        quiet = 400
        written = run(decimator, self.quiet_then_shake(quiet=quiet))

        pre_trigger = [
            row for row, burst in written if burst and row[support.SEQ] < quiet
        ]
        self.assertTrue(pre_trigger, "no pre-trigger samples were kept")
        # 0.25 s of pre-roll at 200 Hz, and none of it from before that.
        earliest = min(row[support.SEQ] for row in pre_trigger)
        self.assertGreaterEqual(earliest, quiet - 52)
        self.assertLess(earliest, quiet)

    def test_no_sample_is_written_twice(self):
        """The pre-roll flush must not re-emit rows the steady grid already kept.

        These two paths both write from the same history and only the seq guard keeps
        them apart; a duplicated row would give the log two samples with one timestamp,
        which view.py would draw as a vertical line.
        """
        decimator = AdaptiveDecimator(rate=20.0, hold=0.5, pre_roll=0.25)
        written = run(decimator, self.quiet_then_shake(quiet=200, shake=300))
        ordered = seqs(written)
        self.assertEqual(ordered, sorted(set(ordered)))

    def test_the_burst_ends_after_the_hold(self):
        decimator = AdaptiveDecimator(rate=5.0, hold=0.5, pre_roll=0.1)
        samples = self.quiet_then_shake(quiet=200, shake=40)
        samples += support.ramp(600, hz=200.0, start_seq=240, start_ms=240 * 5)
        run(decimator, samples)
        self.assertFalse(decimator.bursting)

    def test_a_sustained_move_settles_into_a_new_resting_value(self):
        """The baseline keeps tracking during a burst, so a tilt is an event, not a state.

        Without that, moving the board to a new attitude and leaving it there would latch
        the log at full rate for as long as it stayed put -- which is the failure mode
        that makes adaptive logging useless overnight.
        """
        decimator = AdaptiveDecimator(rate=5.0, hold=0.5, tau=0.5)
        samples = support.ramp(200, hz=200.0)
        # Ten seconds held at the new attitude, well past a 0.5 s time constant.
        samples += support.ramp(2000, hz=200.0, start_seq=200, start_ms=1000, ax_g=1.0)
        run(decimator, samples)
        self.assertFalse(decimator.bursting)
        self.assertEqual(decimator.bursts, 1)

    def test_counts_add_up(self):
        decimator = AdaptiveDecimator(rate=5.0)
        written = run(decimator, self.quiet_then_shake())
        self.assertEqual(
            len(written), decimator.steady_rows + decimator.burst_rows
        )
        self.assertEqual(
            sum(1 for _row, burst in written if burst), decimator.burst_rows
        )
        self.assertIn("1 burst", decimator.summary())


class BoardReset(unittest.TestCase):
    """`r` on the link, or a power blip, restarts t_ms and seq at zero."""

    def test_the_grid_restarts_instead_of_sitting_in_the_future(self):
        """Without the reset check the phase accumulator would be minutes ahead.

        Nothing would then be written for the rest of the session, and the symptom is a
        CSV that simply stops growing while the capture reports healthy row counts for
        every other stage.
        """
        decimator = AdaptiveDecimator(rate=5.0)
        run(decimator, support.ramp(2000, hz=200.0))
        before = decimator.steady_rows

        # The board comes back at t_ms 0, seq 0.
        written = run(decimator, support.ramp(400, hz=200.0))
        self.assertTrue(written, "nothing was written after the reset")
        self.assertGreater(decimator.steady_rows, before)
        self.assertEqual(written[0][1], 0)

    def test_the_reset_does_not_count_as_a_trigger(self):
        decimator = AdaptiveDecimator(rate=5.0)
        run(decimator, support.ramp(400, hz=200.0, ax_g=0.9))
        bursts = decimator.bursts
        run(decimator, support.ramp(400, hz=200.0, ax_g=0.9))
        # The baseline is rebuilt from the first sample after the reset, so the value it
        # was already sitting at is not a departure from anything.
        self.assertEqual(decimator.bursts, bursts)


class TriggerParsing(unittest.TestCase):
    def test_parses_a_column_and_threshold(self):
        self.assertEqual(parse_trigger("ax_g:0.15"), (AX, "ax_g", 0.15))

    def test_tolerates_spacing(self):
        self.assertEqual(parse_trigger("  ax_g : 0.15 "), (AX, "ax_g", 0.15))

    def test_rejects_bad_specs(self):
        for spec in ("ax_g", "nope:1.0", "ax_g:banana", "ax_g:0", "ax_g:-1"):
            self.assertRaises(ValueError, parse_trigger, spec)

    def test_empty_specs_fall_back_to_the_motion_defaults(self):
        for specs in (None, [], ()):
            names = [name for _i, name, _t in parse_triggers(specs)]
            self.assertEqual(names, ["ax_g", "ay_g", "az_g", "gx_dps", "gy_dps", "gz_dps"])

    def test_rate_must_be_positive(self):
        self.assertRaises(ValueError, AdaptiveDecimator, 0)
        self.assertRaises(ValueError, AdaptiveDecimator, -1)


if __name__ == "__main__":
    unittest.main()
