"""The archive index: what is there, and where in time it sits."""

import datetime
import os
import shutil
import tempfile
import unittest

import support

from columns import CSV_COLUMNS
from logstore import Capture, LogStore, parse_start, parse_time

T0 = datetime.datetime(2026, 8, 19, 9, 0, 0)


class Names(unittest.TestCase):
    def test_a_capture_name_gives_its_start(self):
        self.assertEqual(parse_start("nicla_20260819_085534.csv"),
                         datetime.datetime(2026, 8, 19, 8, 55, 34))

    def test_anything_else_is_not_ours(self):
        for name in ("pull-logs.log", "notes.txt", "nicla_2026.csv",
                     "nicla_20260819_085534.csv.bak", "README.md", ""):
            self.assertIsNone(parse_start(name), name)

    def test_a_name_shaped_right_but_impossible_is_refused(self):
        """Shape is not enough -- strptime is what decides."""
        self.assertIsNone(parse_start("nicla_20261345_996699.csv"))

    def test_a_torn_timestamp_reads_as_nothing_rather_than_raising(self):
        self.assertIsNone(parse_time("2026-08-19T08:55:3"))
        self.assertIsNone(parse_time(""))
        self.assertIsNone(parse_time(None))


class ArchiveFixture(unittest.TestCase):
    """One archive with two boards, laid out the way pull-logs.ps1 leaves it."""

    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="nicla-store-")
        self.addCleanup(shutil.rmtree, self.root, True)
        self.bench = os.path.join(self.root, "bench")
        self.riga = os.path.join(self.root, "rig-a")
        os.makedirs(self.bench)
        os.makedirs(self.riga)
        # The puller writes its own log into the root, beside the board directories.
        with open(os.path.join(self.root, "pull-logs.log"), "w") as handle:
            handle.write("2026-08-19 09:00:00  pull start\n")

    def capture(self, directory, started, **kwargs):
        path = support.write_capture(directory, started, **kwargs)
        support.restamp(path, started)
        return path


class Discovery(ArchiveFixture):
    def test_the_directories_are_the_board_list(self):
        self.assertEqual(LogStore(self.root).boards(), ["bench", "rig-a"])

    def test_files_beside_the_boards_are_not_boards(self):
        """pull-logs.log lives in the root and is not a sensor."""
        self.assertNotIn("pull-logs.log", LogStore(self.root).boards())

    def test_a_missing_archive_is_empty_rather_than_an_error(self):
        """The task may not have run yet, and a viewer that raises on that is worse than
        one that says there is nothing here."""
        self.assertEqual(LogStore(os.path.join(self.root, "absent")).boards(), [])
        self.assertEqual(LogStore(os.path.join(self.root, "absent")).captures(), [])

    def test_captures_are_found_and_ordered(self):
        self.capture(self.bench, T0, minutes=1)
        self.capture(self.bench, T0 + datetime.timedelta(hours=2), minutes=1)
        self.capture(self.riga, T0 + datetime.timedelta(hours=1), minutes=1)
        found = LogStore(self.root).captures()
        self.assertEqual([one.board for one in found], ["bench", "rig-a", "bench"])
        self.assertEqual([one.start for one in found], sorted(one.start for one in found))

    def test_one_board_can_be_asked_for(self):
        self.capture(self.bench, T0, minutes=1)
        self.capture(self.riga, T0, minutes=1)
        found = LogStore(self.root).captures(board="bench")
        self.assertEqual([one.board for one in found], ["bench"])

    def test_something_that_is_not_a_capture_is_skipped_but_visible(self):
        """Skipped so it is never parsed as data; visible so it is not a silent hole."""
        self.capture(self.bench, T0, minutes=1)
        stray = os.path.join(self.bench, "notes.txt")
        with open(stray, "w") as handle:
            handle.write("ignore me\n")
        store = LogStore(self.root)
        self.assertEqual([one.name for one in store.captures(board="bench")],
                         ["nicla_20260819_090000.csv"])
        self.assertIn(os.path.join("bench", "notes.txt"), store.strays())


class Windows(unittest.TestCase):
    """Overlap is decided from the name and the mtime, without opening anything."""

    def make(self, start, end):
        return Capture("bench", "/x/nicla.csv", start, end, 0)

    def test_a_file_entirely_before_the_window_is_excluded(self):
        one = self.make(T0, T0 + datetime.timedelta(minutes=5))
        self.assertFalse(one.overlaps(start=T0 + datetime.timedelta(hours=1)))

    def test_a_file_entirely_after_the_window_is_excluded(self):
        one = self.make(T0 + datetime.timedelta(hours=1), T0 + datetime.timedelta(hours=2))
        self.assertFalse(one.overlaps(end=T0 + datetime.timedelta(minutes=5)))

    def test_an_overlapping_file_is_kept(self):
        one = self.make(T0, T0 + datetime.timedelta(hours=2))
        self.assertTrue(one.overlaps(start=T0 + datetime.timedelta(minutes=30),
                                     end=T0 + datetime.timedelta(minutes=40)))

    def test_an_open_window_keeps_everything(self):
        self.assertTrue(self.make(T0, T0).overlaps())


class PlotTime(ArchiveFixture):
    """Rows carry two clocks, and the one to draw with is not the one in the file.

    The CMSIS-DAP bridge delivers in clumps, so inside a burst five or six consecutive
    samples share a host_iso millisecond and the stamp then jumps ~25 ms. Drawn on host_iso
    a 200 Hz waveform came out as 44 stair-steps where there were 250 samples -- measured on
    a real board capture, which is the only reason it was noticed.
    """

    def rows_of(self, path):
        store = LogStore(self.root)
        capture = store.captures(board="bench")[0]
        return list(store.rows(capture))

    def test_rows_carry_both_clocks(self):
        path = self.capture(self.bench, T0, minutes=1)
        first = self.rows_of(path)[0]
        self.assertIn("host_iso", first)
        self.assertIn("t", first)
        self.assertIsInstance(first["t"], datetime.datetime)

    def test_burst_samples_come_out_evenly_spaced(self):
        """Whatever the arrival stamps did, the board wrote these 5 ms apart."""
        path = self.capture(self.bench, T0, minutes=2, moves=((30.0, 0.5),))
        rows = [row for row in self.rows_of(path) if row["burst"]]
        self.assertGreater(len(rows), 50)
        gaps = set()
        for before, after in zip(rows, rows[1:]):
            gaps.add(round((after["t"] - before["t"]).total_seconds(), 4))
        self.assertEqual(gaps, {0.005}, "burst samples are not on the board's grid")

    def test_the_anchor_is_retaken_between_dense_runs(self):
        """So the board clock only ever positions samples inside one run and cannot drift
        away from wall clock across a whole file."""
        path = self.capture(self.bench, T0, minutes=4, moves=((30.0, 0.4), (180.0, 0.4)))
        rows = self.rows_of(path)
        for row in rows:
            drift = abs((row["t"] - row["host_iso"]).total_seconds())
            self.assertLess(drift, 2.0, "plot time drifted away from the wall clock")

    def test_plot_time_never_runs_backwards(self):
        """An anchor retaken to an earlier wall clock would scramble the series."""
        path = self.capture(self.bench, T0, minutes=4, moves=((30.0, 0.4), (180.0, 0.4)))
        times = [row["t"] for row in self.rows_of(path)]
        self.assertEqual(times, sorted(times))


class Rows(ArchiveFixture):
    def test_rows_arrive_typed_and_tagged(self):
        path = self.capture(self.bench, T0, minutes=1)
        store = LogStore(self.root)
        capture = store.captures(board="bench")[0]
        rows = list(store.rows(capture))
        self.assertTrue(rows)
        first = rows[0]
        self.assertIsInstance(first["host_iso"], datetime.datetime)
        self.assertIn(first["burst"], (0, 1))
        self.assertEqual(first["board"], "bench")
        self.assertEqual(os.path.basename(path), capture.name)

    def test_a_window_filters_rows(self):
        self.capture(self.bench, T0, minutes=5, moves=((120.0, 0.6),))
        store = LogStore(self.root)
        capture = store.captures(board="bench")[0]
        window = list(store.rows(capture,
                                 start=T0 + datetime.timedelta(seconds=118),
                                 end=T0 + datetime.timedelta(seconds=122)))
        self.assertTrue(window)
        for row in window:
            self.assertGreaterEqual(row["host_iso"], T0 + datetime.timedelta(seconds=118))
            self.assertLessEqual(row["host_iso"], T0 + datetime.timedelta(seconds=122))

    def test_a_torn_final_row_is_dropped_not_raised(self):
        """The file being written is copied mid-append, and any capture ended with Ctrl-C
        leaves one. This is the normal case, not a corrupt archive."""
        path = self.capture(self.bench, T0, minutes=1)
        with open(path, "a") as handle:
            handle.write("2026-08-19T09:01:00.000,123,456,0.1")   # cut off mid-row
        store = LogStore(self.root)
        capture = store.captures(board="bench")[0]
        rows = list(store.rows(capture))
        self.assertTrue(rows)
        self.assertTrue(all(isinstance(row["host_iso"], datetime.datetime) for row in rows))

    def test_a_full_rate_capture_has_no_burst_column_and_still_reads(self):
        """28 columns rather than 29. The header decides, not an assumption about how the
        fleet happens to be configured."""
        path = os.path.join(self.riga, "nicla_20260819_090000.csv")
        with open(path, "w", newline="") as handle:
            handle.write(",".join(CSV_COLUMNS) + "\n")
            handle.write("2026-08-19T09:00:00.000," + ",".join(["0"] * (len(CSV_COLUMNS) - 1)) + "\n")
        store = LogStore(self.root)
        capture = store.captures(board="rig-a")[0]
        self.assertFalse(store.has_burst_column(capture))
        rows = list(store.rows(capture))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["burst"], 0)

    def test_a_file_that_is_not_a_capture_at_all_yields_nothing(self):
        path = os.path.join(self.bench, "nicla_20260819_090000.csv")
        with open(path, "w") as handle:
            handle.write("this,is,not,a,capture\n1,2,3,4,5\n")
        store = LogStore(self.root)
        capture = store.captures(board="bench")[0]
        self.assertEqual(list(store.rows(capture)), [])


if __name__ == "__main__":
    unittest.main()
