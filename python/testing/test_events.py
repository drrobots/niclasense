"""Burst episodes, recovered from the flag alone."""

import datetime
import os
import shutil
import tempfile
import unittest

import support

from columns import CSV_COLUMNS
from events import Episode, EventIndex, episodes
from logstore import LogStore

T0 = datetime.datetime(2026, 8, 19, 9, 0, 0)


def row(when, burst, ax=0.8243, gx=0.0, board="bench", t_ms=None):
    """One row as logstore hands it over.

    t_ms tracks host_iso unless a test deliberately pulls them apart, which is the case
    worth having: they are two different clocks and only one of them is evenly spaced.
    """
    if t_ms is None:
        t_ms = int(round((when - T0).total_seconds() * 1000))
    return {
        "host_iso": when, "burst": burst, "board": board, "t_ms": t_ms,
        "ax_g": ax, "ay_g": 0.0, "az_g": 0.0,
        "gx_dps": gx, "gy_dps": 0.0, "gz_dps": 0.0,
    }


def at(seconds):
    return T0 + datetime.timedelta(seconds=seconds)


class Grouping(unittest.TestCase):
    """The rules, on rows built by hand so the expected answer is not in doubt."""

    def test_a_run_of_flagged_rows_is_one_episode(self):
        rows = [row(at(0), 0), row(at(1), 1), row(at(1.005), 1), row(at(1.01), 1),
                row(at(60), 0)]
        found = episodes(rows)
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0].rows, 3)
        self.assertEqual(found[0].start, at(1))
        self.assertEqual(found[0].end, at(1.01))

    def test_steady_rows_separate_episodes(self):
        rows = [row(at(1), 1), row(at(60), 0), row(at(120), 1)]
        self.assertEqual(len(episodes(rows)), 2)

    def test_a_long_gap_splits_an_episode_even_with_no_steady_row_between(self):
        """Bursts are written 5 ms apart, so a gap of minutes is two events that happened to
        leave no steady row between them -- not one event lasting minutes."""
        rows = [row(at(1), 1), row(at(1.005), 1), row(at(300), 1), row(at(300.005), 1)]
        found = episodes(rows)
        self.assertEqual(len(found), 2)
        self.assertEqual([one.rows for one in found], [2, 2])

    def test_an_episode_running_to_the_end_of_the_file_is_still_reported(self):
        """Nothing closes it, and a burst in progress when the copy was taken is exactly the
        event somebody is looking for."""
        found = episodes([row(at(0), 0), row(at(1), 1), row(at(1.005), 1)])
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0].rows, 2)

    def test_no_flags_means_no_episodes(self):
        self.assertEqual(episodes([row(at(0), 0), row(at(60), 0)]), [])

    def test_peaks_come_from_the_episode_not_its_surroundings(self):
        rows = [row(at(0), 0, ax=9.0, gx=900.0),        # quiet row, must not count
                row(at(1), 1, ax=1.45, gx=42.0),
                row(at(1.005), 1, ax=2.10, gx=11.0)]
        found = episodes(rows)[0]
        self.assertAlmostEqual(found.peak_g, 2.10, places=4)
        self.assertAlmostEqual(found.peak_dps, 42.0, places=2)

    def test_a_row_missing_its_columns_does_not_sink_the_episode(self):
        rows = [row(at(1), 1), {"host_iso": at(1.005), "burst": 1, "board": "bench"}]
        found = episodes(rows)
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0].rows, 2)

    def test_the_dict_form_is_json_safe(self):
        found = episodes([row(at(1), 1), row(at(1.5), 1)])[0].as_dict()
        self.assertIsInstance(found["start"], str)
        self.assertIsInstance(found["duration_s"], float)
        self.assertEqual(found["board"], "bench")


class WhichClock(unittest.TestCase):
    """Gaps are measured on the board's t_ms, never on arrival time.

    This is the bug a real capture found and a synthetic one could not. The CMSIS-DAP
    bridge delivers in bunches, so host_iso clusters -- twenty rows sharing an instant, then
    a jump. Measured that way a single burst looks like dozens of tiny ones: on a real
    capture it turned 17 episodes into 1090.
    """

    def test_bunched_arrival_does_not_split_a_burst(self):
        # Evenly spaced on the board at 200 Hz, delivered in two clumps 300 ms apart.
        rows = []
        for i in range(40):
            arrival = at(0.0 if i < 20 else 0.3)
            rows.append(row(arrival, 1, t_ms=i * 5))
        found = episodes(rows)
        self.assertEqual(len(found), 1, "arrival-time bunching split one burst")
        self.assertEqual(found[0].rows, 40)

    def test_a_real_gap_on_the_board_clock_still_splits(self):
        rows = [row(at(0), 1, t_ms=0), row(at(0), 1, t_ms=5),
                row(at(0), 1, t_ms=5000), row(at(0), 1, t_ms=5005)]
        self.assertEqual(len(episodes(rows)), 2)

    def test_a_board_reset_ends_the_episode(self):
        """t_ms restarts from zero on every reset, so it can run backwards inside one file."""
        rows = [row(at(0), 1, t_ms=90000), row(at(0.005), 1, t_ms=90005),
                row(at(0.01), 1, t_ms=0), row(at(0.015), 1, t_ms=5)]
        self.assertEqual(len(episodes(rows)), 2)

    def test_rows_without_a_board_clock_still_group(self):
        """Falls back to arrival time. Worse, and better than refusing to answer."""
        rows = [{"host_iso": at(0), "burst": 1, "board": "bench"},
                {"host_iso": at(0.01), "burst": 1, "board": "bench"}]
        self.assertEqual(len(episodes(rows)), 1)


class KnownLimit(unittest.TestCase):
    def test_back_to_back_bursts_read_as_one(self):
        """The decimator's hold can expire and re-trigger with nothing written between, and
        the flag cannot tell that from one continuous burst. Recorded rather than fixed: the
        fix belongs in the logger, as an episode number instead of a 0/1 flag."""
        rows = [row(at(0), 1, t_ms=0), row(at(0.005), 1, t_ms=5),
                row(at(0.01), 1, t_ms=10), row(at(0.015), 1, t_ms=15)]
        self.assertEqual(len(episodes(rows)), 1)


class AgainstRealCaptures(unittest.TestCase):
    """The same rules against files the real decimator and logger produced."""

    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="nicla-events-")
        self.addCleanup(shutil.rmtree, self.root, True)
        self.bench = os.path.join(self.root, "bench")
        os.makedirs(self.bench)
        self.store = LogStore(self.root)

    def capture(self, started, **kwargs):
        path = support.write_capture(self.bench, started, **kwargs)
        support.restamp(path, started)
        return path

    def test_two_shakes_are_found_as_two_episodes(self):
        self.capture(T0, minutes=5, moves=((120.0, 0.6), (240.0, 0.4)))
        found = EventIndex(self.store).between()
        self.assertEqual(len(found), 2, [one.as_dict() for one in found])

    def test_an_episode_brackets_the_movement_that_caused_it(self):
        """It starts slightly early and ends late on purpose: the decimator keeps a quarter
        second of pre-roll and holds for a second after the trigger stops."""
        self.capture(T0, minutes=5, moves=((120.0, 0.6),))
        found = EventIndex(self.store).between()[0]
        self.assertLessEqual(found.start, at(120.0))
        self.assertGreaterEqual(found.start, at(119.0))
        self.assertGreaterEqual(found.end, at(120.6))

    def test_the_peak_reflects_the_shake(self):
        self.capture(T0, minutes=5, moves=((120.0, 0.6),))
        found = EventIndex(self.store).between()[0]
        self.assertGreater(found.peak_g, 1.0)
        self.assertGreater(found.peak_dps, 40.0)

    def test_a_quiet_capture_has_nothing_in_it(self):
        self.capture(T0, minutes=3)
        self.assertEqual(EventIndex(self.store).between(), [])

    def test_a_full_rate_capture_reports_nothing_because_nothing_was_promoted(self):
        path = os.path.join(self.bench, "nicla_20260819_100000.csv")
        with open(path, "w", newline="") as handle:
            handle.write(",".join(CSV_COLUMNS) + "\n")
            for i in range(10):
                stamp = (T0 + datetime.timedelta(seconds=i)).isoformat(timespec="milliseconds")
                handle.write(stamp + "," + ",".join(["1"] * (len(CSV_COLUMNS) - 1)) + "\n")
        self.assertEqual(EventIndex(self.store).between(), [])


class Selection(AgainstRealCaptures):
    def test_the_window_filters_episodes_as_well_as_files(self):
        self.capture(T0, minutes=5, moves=((60.0, 0.4), (240.0, 0.4)))
        index = EventIndex(self.store)
        self.assertEqual(len(index.between()), 2)
        early = index.between(start=T0, end=at(120))
        self.assertEqual(len(early), 1)
        self.assertLess(early[0].start, at(120))

    def test_one_board_can_be_asked_for(self):
        other = os.path.join(self.root, "rig-a")
        os.makedirs(other)
        support.restamp(support.write_capture(other, T0, minutes=3, moves=((60.0, 0.4),)), T0)
        self.capture(T0, minutes=3, moves=((60.0, 0.4),))
        index = EventIndex(self.store)
        self.assertEqual(len(index.between()), 2)
        self.assertEqual([one.board for one in index.between(board="bench")], ["bench"])

    def test_episodes_come_back_oldest_first(self):
        self.capture(T0, minutes=5, moves=((240.0, 0.4), (60.0, 0.4)))
        found = EventIndex(self.store).between()
        self.assertEqual([one.start for one in found], sorted(one.start for one in found))


class Caching(AgainstRealCaptures):
    """A year of archive must not be re-read to answer a question about this afternoon."""

    def test_an_unchanged_file_is_scanned_once(self):
        self.capture(T0, minutes=3, moves=((60.0, 0.4),))
        index = EventIndex(self.store)
        index.between()
        self.assertEqual(index.scans, 1)
        index.between()
        index.between()
        self.assertEqual(index.scans, 1, "an unchanged capture was re-read")
        self.assertGreaterEqual(index.hits, 2)

    def test_a_file_that_grew_is_read_again(self):
        """Which is what the capture currently being written does on every pull."""
        path = self.capture(T0, minutes=3, moves=((60.0, 0.4),))
        index = EventIndex(self.store)
        index.between()
        self.assertEqual(index.scans, 1)
        with open(path, "a") as handle:
            stamp = at(400).isoformat(timespec="milliseconds")
            handle.write(stamp + "," + ",".join(["1"] * len(CSV_COLUMNS)) + "\n")
        os.utime(path, (0, 0))   # a different mtime, which is what invalidates it
        index.between()
        self.assertEqual(index.scans, 2)


if __name__ == "__main__":
    unittest.main()
