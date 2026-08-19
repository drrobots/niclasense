"""Bucketing rows into something a plot can draw."""

import datetime
import unittest

import support  # noqa: F401  (puts python/ on the path)

from series import DEFAULT_WIDTH, MAX_WIDTH, as_payload, bucket_rows, epoch

T0 = datetime.datetime(2026, 8, 19, 9, 0, 0)
COLS = ("temp_C", "ax_g")


def at(seconds):
    return T0 + datetime.timedelta(seconds=seconds)


def row(seconds, temp=26.7, ax=0.82, burst=0):
    return {"host_iso": at(seconds), "temp_C": temp, "ax_g": ax, "burst": burst}


class Sparse(unittest.TestCase):
    """Fewer rows than buckets, which is the steady 1/min case."""

    def test_every_row_survives_as_its_own_bucket(self):
        rows = [row(i * 60, temp=20 + i) for i in range(10)]
        buckets, seen = bucket_rows(rows, COLS, start=at(0), end=at(600), width=900)
        self.assertEqual(seen, 10)
        self.assertEqual(len(buckets), 10)

    def test_the_band_collapses_to_a_line(self):
        """One row in a bucket means min == max, so the client needs no separate case for a
        window that happened to be sparse."""
        rows = [row(i * 60, temp=20 + i) for i in range(5)]
        buckets, _seen = bucket_rows(rows, COLS, start=at(0), end=at(300), width=900)
        payload = as_payload(buckets, COLS, 5)
        self.assertFalse(payload["downsampled"])
        self.assertEqual(payload["columns"]["temp_C"]["min"],
                         payload["columns"]["temp_C"]["max"])

    def test_empty_buckets_are_dropped_rather_than_sent_as_gaps(self):
        """A row a minute over an hour fills one bucket in thirteen. Sending the rest as
        nulls draws the line as dust."""
        rows = [row(i * 60) for i in range(60)]
        buckets, _seen = bucket_rows(rows, COLS, start=at(0), end=at(3600), width=900)
        self.assertEqual(len(buckets), 60)
        payload = as_payload(buckets, COLS, 60)
        self.assertEqual(len(payload["t"]), 60)
        self.assertNotIn(None, payload["columns"]["temp_C"]["min"])


class Dense(unittest.TestCase):
    """More rows than buckets, which is a burst."""

    def test_buckets_are_capped_at_the_width(self):
        rows = [row(i * 0.005) for i in range(4000)]
        buckets, seen = bucket_rows(rows, COLS, start=at(0), end=at(20), width=100)
        self.assertEqual(seen, 4000)
        self.assertLessEqual(len(buckets), 100)

    def test_the_envelope_keeps_the_extremes(self):
        """The whole reason for min/max over every-Nth. A spike that lasted one sample is
        the content of a burst, and taking every Nth row throws it away precisely because it
        was brief."""
        rows = [row(i * 0.005, ax=0.82) for i in range(1000)]
        rows[503]["ax_g"] = 9.9
        rows[504]["ax_g"] = -4.4
        buckets, _seen = bucket_rows(rows, COLS, start=at(0), end=at(5), width=50)
        payload = as_payload(buckets, COLS, 1000)
        self.assertAlmostEqual(max(payload["columns"]["ax_g"]["max"]), 9.9, places=6)
        self.assertAlmostEqual(min(payload["columns"]["ax_g"]["min"]), -4.4, places=6)

    def test_downsampling_is_declared(self):
        rows = [row(i * 0.005) for i in range(1000)]
        buckets, _seen = bucket_rows(rows, COLS, start=at(0), end=at(5), width=50)
        self.assertTrue(as_payload(buckets, COLS, 1000)["downsampled"])

    def test_a_burst_anywhere_in_a_bucket_marks_it(self):
        """The client draws the rail off this, so losing a short burst to a wide bucket
        would hide exactly the thing worth seeing."""
        rows = [row(i * 0.005) for i in range(1000)]
        rows[700]["burst"] = 1
        buckets, _seen = bucket_rows(rows, COLS, start=at(0), end=at(5), width=20)
        payload = as_payload(buckets, COLS, 1000)
        self.assertEqual(sum(payload["burst"]), 1)


class Grid(unittest.TestCase):
    def test_the_window_fixes_the_grid_so_two_boards_line_up(self):
        """Bucketed from their own rows, two boards asked for separately would land on
        different grids and could not be compared."""
        early = [row(i) for i in range(0, 10)]
        late = [row(i) for i in range(50, 60)]
        a, _ = bucket_rows(early, COLS, start=at(0), end=at(100), width=100)
        b, _ = bucket_rows(late, COLS, start=at(0), end=at(100), width=100)
        self.assertAlmostEqual(a[0].t, epoch(at(0)), places=3)
        self.assertAlmostEqual(b[0].t, epoch(at(50)), places=3)

    def test_without_a_window_the_rows_supply_the_grid(self):
        buckets, seen = bucket_rows([row(0), row(10)], COLS, width=10)
        self.assertEqual(seen, 2)
        self.assertEqual(len(buckets), 2)

    def test_a_single_row_does_not_divide_by_zero(self):
        buckets, seen = bucket_rows([row(5)], COLS, width=100)
        self.assertEqual((len(buckets), seen), (1, 1))

    def test_a_window_of_no_width_does_not_divide_by_zero(self):
        buckets, _seen = bucket_rows([row(0), row(0)], COLS,
                                     start=at(0), end=at(0), width=100)
        self.assertEqual(len(buckets), 1)

    def test_a_row_on_the_far_edge_lands_inside(self):
        """(moment - lo) / step is exactly `width` for the last instant, which would index
        one past the end."""
        buckets, _seen = bucket_rows([row(0), row(10)], COLS,
                                     start=at(0), end=at(10), width=10)
        self.assertEqual(len(buckets), 2)

    def test_nothing_in_gives_nothing_back(self):
        self.assertEqual(bucket_rows([], COLS, width=100), ([], 0))

    def test_width_is_clamped(self):
        rows = [row(i * 0.005) for i in range(500)]
        buckets, _seen = bucket_rows(rows, COLS, start=at(0), end=at(2.5),
                                     width=MAX_WIDTH * 10)
        self.assertLessEqual(len(buckets), MAX_WIDTH)
        buckets, _seen = bucket_rows(rows, COLS, start=at(0), end=at(2.5), width=0)
        self.assertEqual(len(buckets), 1)


class Values(unittest.TestCase):
    def test_a_column_nothing_reports_is_left_out(self):
        buckets, _seen = bucket_rows([row(0)], ("temp_C", "nope"), width=10)
        payload = as_payload(buckets, ("temp_C", "nope"), 1)
        self.assertIn("temp_C", payload["columns"])
        self.assertNotIn("nope", payload["columns"])

    def test_unparseable_values_do_not_sink_the_bucket(self):
        one = row(0)
        one["temp_C"] = "not a number"
        buckets, seen = bucket_rows([one, row(1)], COLS, width=10)
        self.assertEqual(seen, 2)
        payload = as_payload(buckets, COLS, 2)
        self.assertIn("ax_g", payload["columns"])

    def test_rows_without_a_timestamp_are_skipped(self):
        buckets, seen = bucket_rows([{"temp_C": 1.0}, row(0)], COLS, width=10)
        self.assertEqual(seen, 1)
        self.assertEqual(len(buckets), 1)

    def test_the_default_width_is_a_plot_width(self):
        self.assertGreater(DEFAULT_WIDTH, 100)
        self.assertLessEqual(DEFAULT_WIDTH, MAX_WIDTH)


if __name__ == "__main__":
    unittest.main()
