"""The live dashboard's data handling, with the figure rendered to a memory canvas.

Nothing here checks what the window looks like. What it checks is the arithmetic behind
the picture -- ring buffers, the measured rate, and the autoscale rule -- because that rule
is not local to this file. web/app.js ports it deliberately and in order (min/max over the
undecimated window, widen to min_span, then pad 12%), and view.py applies the same floor to
a finished file. Three implementations of one rule is worth a test that states it once.

The last case in this module is a known gap rather than a passing check; see
BoardReset.
"""

import unittest

import matplotlib

matplotlib.use("Agg")

import support  # noqa: E402,F401 -- puts python/ on the path
import tiles  # noqa: E402
from plot import LivePlot  # noqa: E402


def tile_named(plot, name):
    for entry in plot._tiles:
        if entry["tile"]["name"] == name:
            return entry
    raise AssertionError("no tile called %r" % name)


class Buffers(unittest.TestCase):
    def setUp(self):
        self.plot = LivePlot(window=5.0, sample_hz=200.0)
        self.addCleanup(self.close)

    def close(self):
        import matplotlib.pyplot as plt

        plt.close(self.plot.figure)

    def test_a_sample_lands_in_every_buffer_it_belongs_in(self):
        self.plot.add(support.sample(seq=1, t_ms=500, ax_g=0.25, qw=0.5, bsec_acc=3))
        self.assertEqual(list(self.plot._t), [0.5])
        self.assertEqual(list(self.plot._series["ax_g"]), [0.25])
        self.assertEqual(list(self.plot._series["qw"]), [0.5])
        # Never plotted, but the three BSEC tiles are meaningless without it.
        self.assertEqual(list(self.plot._series["bsec_acc"]), [3])

    def test_every_plotted_column_has_a_buffer(self):
        for tile in tiles.TILES:
            for column, _label, _colour in tile["series"]:
                self.assertIn(column, self.plot._series, column)

    def test_the_buffers_are_bounded(self):
        for one in support.ramp(20000, hz=200.0):
            self.plot.add(one)
        capacity = self.plot._t.maxlen
        self.assertEqual(len(self.plot._t), capacity)
        for column, buffer in self.plot._series.items():
            self.assertEqual(len(buffer), capacity, column)

    def test_the_buffers_hold_more_than_the_window_needs(self):
        """Headroom of 2x, so a faster-than-nominal stream still fills the window."""
        self.assertGreaterEqual(self.plot._t.maxlen, 5.0 * 200.0)

    def test_widening_the_window_grows_the_buffers_and_keeps_the_history(self):
        for one in support.ramp(400, hz=200.0, ax_g=0.5):
            self.plot.add(one)
        before = list(self.plot._t)
        self.plot._resize_capacity(60.0)
        self.assertGreater(self.plot._t.maxlen, 60.0 * 200.0)
        self.assertEqual(list(self.plot._t), before)
        self.assertEqual(len(self.plot._series["ax_g"]), len(before))

    def test_narrowing_the_window_leaves_the_buffers_alone(self):
        """Holding more history than the display uses is harmless; reallocating is not."""
        capacity = self.plot._t.maxlen
        self.plot._resize_capacity(2.0)
        self.assertEqual(self.plot._t.maxlen, capacity)

    def test_measured_hz_is_over_the_visible_window(self):
        self.assertAlmostEqual(self.plot._measured_hz([0.0, 0.005, 0.010]), 200.0)
        self.assertEqual(self.plot._measured_hz([]), 0.0)
        self.assertEqual(self.plot._measured_hz([1.0]), 0.0)
        self.assertEqual(self.plot._measured_hz([1.0, 1.0]), 0.0)


class Autoscale(unittest.TestCase):
    """The rule web/app.js ports, in the order it has to be applied in."""

    def setUp(self):
        self.plot = LivePlot(window=5.0, sample_hz=200.0)
        self.addCleanup(self.close)

    def close(self):
        import matplotlib.pyplot as plt

        plt.close(self.plot.figure)

    def feed(self, **overrides):
        for one in support.ramp(400, hz=200.0, **overrides):
            self.plot.add(one)
        self.plot._refresh(0)

    def test_a_resting_sensor_is_widened_to_its_min_span(self):
        """Without the floor, a still board autoscales to its own quantization steps and
        sensor noise is drawn as dramatic staircases."""
        self.feed(ax_g=0.0)
        entry = tile_named(self.plot, "accelerometer")
        low, high = entry["axes"].get_ylim()
        self.assertGreaterEqual(high - low, entry["tile"]["min_span"])

    def test_the_floor_is_centred_on_the_data_not_on_zero(self):
        """A tile parked well away from zero should still be centred on where it is."""
        self.feed(ax_g=9.0, ay_g=9.0, az_g=9.0)
        low, high = tile_named(self.plot, "accelerometer")["axes"].get_ylim()
        self.assertAlmostEqual((low + high) / 2.0, 9.0, places=6)

    def test_real_motion_still_fills_the_tile(self):
        """A range well past the floor is padded by 12% and no more."""
        span = 4.0
        for i, one in enumerate(support.ramp(400, hz=200.0)):
            value = -span / 2.0 if i % 2 else span / 2.0
            self.plot.add(one[:2] + (value,) + one[3:])
        self.plot._refresh(0)
        low, high = tile_named(self.plot, "accelerometer")["axes"].get_ylim()
        self.assertAlmostEqual(high - low, span * 1.24, places=6)
        self.assertAlmostEqual((low + high) / 2.0, 0.0, places=6)

    def test_a_tile_scales_across_all_of_its_series_together(self):
        """x, y and z share an axis, so they stay comparable by eye."""
        self.feed(ax_g=0.0, ay_g=3.0, az_g=-3.0)
        low, high = tile_named(self.plot, "accelerometer")["axes"].get_ylim()
        self.assertLessEqual(low, -3.0)
        self.assertGreaterEqual(high, 3.0)

    def test_the_time_axis_is_the_window_ending_at_the_newest_sample(self):
        self.feed()
        entry = tile_named(self.plot, "accelerometer")
        low, high = entry["axes"].get_xlim()
        self.assertAlmostEqual(high, 1.995, places=3)
        self.assertAlmostEqual(high - low, 5.0, places=6)

    def test_drawing_is_strided_towards_the_point_budget(self):
        """Towards, not to: the stride is an integer, so the drawn count lands somewhere
        between MAX_POINTS and twice it rather than on the budget exactly. Worth pinning
        down as the real bound -- the point is that frame time stops growing with the
        window, and it does."""
        plot = LivePlot(window=120.0, sample_hz=200.0)
        self.addCleanup(lambda: __import__("matplotlib.pyplot",
                                           fromlist=["close"]).close(plot.figure))
        for one in support.ramp(24000, hz=200.0):
            plot.add(one)
        plot._refresh(0)
        drawn = plot._lines["ax_g"].get_xdata()
        self.assertGreaterEqual(len(drawn), tiles.MAX_POINTS)
        self.assertLessEqual(len(drawn), tiles.MAX_POINTS * 2)

    def test_refreshing_with_nothing_buffered_is_not_an_error(self):
        self.assertEqual(self.plot._refresh(0), [])


class WindowBox(unittest.TestCase):
    def setUp(self):
        self.plot = LivePlot(window=30.0, sample_hz=200.0)
        self.addCleanup(self.close)

    def close(self):
        import matplotlib.pyplot as plt

        plt.close(self.plot.figure)

    def test_a_typed_window_is_clamped_to_the_bounds(self):
        self.plot._window_submitted("100000")
        self.assertEqual(self.plot.window, tiles.MAX_WINDOW_S)
        self.plot._window_submitted("0.001")
        self.assertEqual(self.plot.window, tiles.MIN_WINDOW_S)

    def test_a_typed_window_is_applied(self):
        self.plot._window_submitted("45")
        self.assertEqual(self.plot.window, 45.0)

    def test_nonsense_is_reported_and_the_window_is_left_alone(self):
        self.plot._window_submitted("banana")
        self.assertEqual(self.plot.window, 30.0)
        self.assertTrue(self.plot._capture["message"].get_text())

    def test_an_empty_box_is_ignored(self):
        self.plot._window_submitted("   ")
        self.assertEqual(self.plot.window, 30.0)


class BoardReset(unittest.TestCase):
    """A known gap, recorded rather than described.

    t_ms restarts at zero whenever the board reboots. view.py stitches its timeline across
    that (_timeline) and the browser client clears its ring buffers on it (resetBuffers in
    app.js), but plot.py does neither: it appends the new samples after the old ones and
    keeps drawing. The window then ends at the newest sample -- a fraction of a second
    after the reboot -- so the x-axis runs from negative time, and every pre-reset sample
    still in the ring is drawn inside it. The dashboard shows the old capture stacked on
    top of the new one until the buffers roll over, which at a 30 s window is a minute of
    a display that looks broken rather than empty.

    The assertion below is what the other two viewers already do. It is marked as an
    expected failure so the suite records the gap without going red for it; if someone
    fixes plot.py, unittest reports an unexpected success and this can become a plain test.
    """

    def setUp(self):
        self.plot = LivePlot(window=5.0, sample_hz=200.0)
        self.addCleanup(self.close)

    def close(self):
        import matplotlib.pyplot as plt

        plt.close(self.plot.figure)

    @unittest.expectedFailure
    def test_a_reset_does_not_leave_the_dashboard_drawing_negative_time(self):
        for one in support.ramp(1000, hz=200.0, ax_g=0.5):   # five seconds
            self.plot.add(one)
        for one in support.ramp(100, hz=200.0, ax_g=0.5):    # the board comes back
            self.plot.add(one)
        self.plot._refresh(0)

        low, _high = tile_named(self.plot, "accelerometer")["axes"].get_xlim()
        self.assertGreaterEqual(low, 0.0)

    @unittest.expectedFailure
    def test_a_reset_does_not_draw_the_old_capture_over_the_new_one(self):
        for one in support.ramp(1000, hz=200.0, ax_g=0.5):
            self.plot.add(one)
        for one in support.ramp(100, hz=200.0, ax_g=0.5):
            self.plot.add(one)
        self.plot._refresh(0)

        drawn = list(self.plot._lines["ax_g"].get_xdata())
        self.assertEqual(drawn, sorted(drawn), "the trace runs backwards in time")


if __name__ == "__main__":
    unittest.main()
