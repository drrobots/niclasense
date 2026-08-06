"""The offline viewer's arithmetic: decimation, reset stitching, and tolerant loading.

view.py is mostly a matplotlib figure, and none of that is tested here. What is tested is
the handful of functions that decide *what* gets drawn, because each of them exists to
avoid a specific way of lying about a recording:

    envelope()    a plain stride deletes short impacts, which is what you opened the log for
    _timeline()   a board reset folds the rest of the file back on top of the beginning
    load()        one torn row at the end of a Ctrl-C'd capture should not cost the file

The backend is forced to Agg before view.py is imported, so this runs with no display.
"""

import os
import shutil
import tempfile
import unittest

import matplotlib

matplotlib.use("Agg")

import numpy as np  # noqa: E402

import support  # noqa: E402,F401 -- puts python/ on the path
import view  # noqa: E402
from columns import CSV_COLUMNS  # noqa: E402
from logger import CsvLogger  # noqa: E402


class Envelope(unittest.TestCase):
    def test_short_traces_are_passed_through_untouched(self):
        t = np.arange(100.0)
        y = np.sin(t)
        out_t, out_y = view.envelope(t, y, max_points=1200)
        self.assertIs(out_t, t)
        self.assertIs(out_y, y)

    def test_a_brief_spike_survives_decimation(self):
        """The whole reason this is not a stride.

        A 20 ms impact in a two-minute recording is a handful of samples out of tens of
        thousands; taking every nth row drops it about nineteen times in twenty, and the
        tile then shows a calm trace over the exact event being looked for.
        """
        t = np.arange(0.0, 60.0, 0.005)          # 12,000 samples at 200 Hz
        y = np.zeros_like(t)
        y[7001:7005] = 9.0                       # a 20 ms impact, between stride points

        _out_t, out_y = view.envelope(t, y, max_points=1200)
        self.assertAlmostEqual(float(np.max(out_y)), 9.0)

        strided = y[::len(y) // 1200]
        self.assertLess(float(np.max(strided)), 9.0,
                        "the stride happened to catch it; pick a different index")

    def test_both_ends_of_the_range_are_kept(self):
        t = np.arange(0.0, 10.0, 0.001)
        y = np.linspace(-3.0, 5.0, len(t))
        _out_t, out_y = view.envelope(t, y, max_points=800)
        self.assertAlmostEqual(float(np.min(out_y)), -3.0, places=2)
        self.assertAlmostEqual(float(np.max(out_y)), 5.0, places=2)

    def test_output_time_is_non_decreasing(self):
        t = np.arange(0.0, 10.0, 0.001)
        y = np.sin(t * 30.0)
        out_t, _out_y = view.envelope(t, y, max_points=600)
        self.assertTrue(np.all(np.diff(out_t) >= 0))

    def test_the_point_budget_is_respected(self):
        t = np.arange(0.0, 100.0, 0.005)
        out_t, _out_y = view.envelope(t, np.sin(t), max_points=900)
        self.assertLessEqual(len(out_t), 900)


class Timeline(unittest.TestCase):
    def test_a_clean_file_starts_at_zero(self):
        t = view._timeline(np.arange(0.0, 1000.0, 5.0))
        self.assertAlmostEqual(float(t[0]), 0.0)
        self.assertTrue(np.all(np.diff(t) > 0))

    def test_a_reset_is_stitched_rather_than_folded_back(self):
        """t_ms restarts at zero when the board reboots. Left alone, the second half of
        the file would be drawn on top of the first."""
        t_ms = np.concatenate([
            np.arange(0.0, 1000.0, 5.0),
            np.arange(0.0, 1000.0, 5.0),
        ])
        t = view._timeline(t_ms)
        self.assertTrue(np.all(np.diff(t) > 0), "the timeline still goes backwards")
        self.assertGreater(float(t[-1]), 1.9)

    def test_several_resets_are_all_stitched(self):
        t_ms = np.concatenate([np.arange(0.0, 500.0, 5.0)] * 4)
        t = view._timeline(t_ms)
        self.assertTrue(np.all(np.diff(t) > 0))

    def test_the_seam_is_one_sample_wide(self):
        """The log does not record how long the board took to come back, so the gap is
        deliberately a seam rather than a measurement."""
        t_ms = np.concatenate([np.arange(0.0, 100.0, 5.0), np.arange(0.0, 100.0, 5.0)])
        t = view._timeline(t_ms)
        gaps = np.diff(t)
        self.assertAlmostEqual(float(np.max(gaps)), 0.005, places=6)


class BurstRuns(unittest.TestCase):
    def test_no_bursts_is_no_runs(self):
        t = np.arange(10.0)
        self.assertEqual(view.burst_runs(t, np.zeros(10)), [])

    def test_a_run_in_the_middle(self):
        t = np.arange(10.0)
        burst = np.array([0, 0, 1, 1, 1, 0, 0, 0, 0, 0], dtype=float)
        runs = view.burst_runs(t, burst)
        self.assertEqual(len(runs), 1)
        start, width = runs[0]
        self.assertAlmostEqual(start, 2.0)
        self.assertAlmostEqual(width, 3.0)

    def test_runs_touching_either_end_are_not_lost(self):
        t = np.arange(6.0)
        burst = np.array([1, 1, 0, 0, 1, 1], dtype=float)
        self.assertEqual(len(view.burst_runs(t, burst)), 2)

    def test_two_separate_runs(self):
        t = np.arange(12.0)
        burst = np.array([0, 1, 1, 0, 0, 0, 1, 1, 1, 0, 0, 0], dtype=float)
        self.assertEqual(len(view.burst_runs(t, burst)), 2)


class Loading(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.mkdtemp(prefix="nicla-test-")
        self.addCleanup(shutil.rmtree, self.directory, True)
        self.path = os.path.join(self.directory, "capture.csv")

    def write_log(self, samples, mark_bursts=False, bursts=None):
        with CsvLogger(self.path, mark_bursts=mark_bursts) as log:
            for i, one in enumerate(samples):
                log.write(one, (bursts or {}).get(i, 0))
        return self.path

    def test_a_capture_reads_back_with_its_columns_and_timeline(self):
        samples = support.ramp(200, hz=200.0, ax_g=0.5, temp_C=21.0)
        log = view.load(self.write_log(samples))
        self.assertEqual(log.rows, 200)
        self.assertNotIn("t_ms", log.columns)         # consumed into .t
        self.assertNotIn("host_iso", log.columns)     # parsed into .started
        self.assertAlmostEqual(float(log.t[0]), 0.0)
        self.assertAlmostEqual(log.duration, 0.995, places=3)
        self.assertAlmostEqual(log.mean_hz, 200.0, places=0)
        self.assertTrue(np.allclose(log.columns["ax_g"], 0.5))
        self.assertIsNotNone(log.started)

    def test_a_torn_final_row_costs_the_row_not_the_file(self):
        """How a capture killed with Ctrl-C mid-write ends."""
        self.write_log(support.ramp(50))
        with open(self.path, "a") as handle:
            handle.write("2026-08-06T12:00:00.000,1,2,3")
        log = view.load(self.path)
        self.assertEqual(log.rows, 50)

    def test_a_short_row_in_the_middle_is_skipped(self):
        path = self.write_log(support.ramp(50))
        with open(path) as handle:
            lines = handle.readlines()
        lines.insert(20, "2026-08-06T12:00:00.000,1,2,3\n")
        with open(path, "w") as handle:
            handle.writelines(lines)
        self.assertEqual(view.load(path).rows, 50)

    def test_the_burst_column_is_picked_up_when_present(self):
        samples = support.ramp(30)
        self.write_log(samples, mark_bursts=True, bursts={10: 1, 11: 1, 12: 1})
        log = view.load(self.path)
        self.assertIsNotNone(log.burst)
        self.assertNotIn("burst", log.columns)
        self.assertEqual(len(view.burst_runs(log.t, log.burst)), 1)

    def test_a_file_with_no_burst_column_loads_the_same_way(self):
        self.write_log(support.ramp(30))
        self.assertIsNone(view.load(self.path).burst)

    def test_a_file_that_is_not_a_log_is_refused_by_name(self):
        path = os.path.join(self.directory, "other.csv")
        with open(path, "w") as handle:
            handle.write("a,b,c\n1,2,3\n")
        self.assertRaises(ValueError, view.load, path)

    def test_an_empty_file_is_refused(self):
        path = os.path.join(self.directory, "empty.csv")
        with open(path, "w") as handle:
            handle.write(",".join(CSV_COLUMNS) + "\n")
        self.assertRaises(ValueError, view.load, path)

    def test_a_reset_mid_capture_loads(self):
        samples = support.ramp(100) + support.ramp(100)
        log = view.load(self.write_log(samples))
        self.assertEqual(log.rows, 200)
        self.assertTrue(np.all(np.diff(log.t) > 0))


class Navigation(unittest.TestCase):
    def make(self, count=1000, hz=200.0):
        t = np.arange(count) / hz
        return view.LogFile("x.csv", t, {"ax_g": np.zeros(count)}, None, None)

    def test_index_at_finds_the_nearest_row(self):
        log = self.make()
        self.assertEqual(log.index_at(0.0), 0)
        self.assertEqual(log.index_at(1.0), 200)
        self.assertEqual(log.index_at(1.0 + 0.002), 200)
        self.assertEqual(log.index_at(1.0 + 0.004), 201)

    def test_index_at_clamps_to_the_file(self):
        log = self.make()
        self.assertEqual(log.index_at(-100.0), 0)
        self.assertEqual(log.index_at(1e9), log.rows - 1)

    def test_span_widens_by_one_row_each_side(self):
        """So a trace touches both edges of the tile instead of stopping short."""
        log = self.make()
        lo, hi = log.span(1.0, 2.0)
        self.assertLess(log.t[lo], 1.0)
        self.assertGreater(log.t[hi - 1], 2.0)

    def test_span_is_never_empty(self):
        log = self.make()
        lo, hi = log.span(1.0, 1.0)
        self.assertGreater(hi, lo)


if __name__ == "__main__":
    unittest.main()
