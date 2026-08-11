"""Deleting old captures, which is the one thing in the project that destroys data.

Everything else here either writes a file or reads one. This removes them, unattended, on a
machine nobody is logged into, which makes the interesting cases the ones where it must
*not* delete: the file being written, a file inside the limits, anything that is not a
capture, and everything at all when the limits are off. Those are most of what is below.

Times are set with os.utime and read back through an injected `now` rather than by waiting,
so a test about a year of retention takes no longer than one about an hour.
"""

import os
import shutil
import tempfile
import unittest

import support  # noqa: F401  (puts python/ on the path)

import retention

DAY = 86400.0


class TempLogs(unittest.TestCase):
    """A directory of CSVs with controlled ages and sizes."""

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="nicla-retention-")
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.now = 1_000_000_000.0

    def write(self, name, age_days=0.0, size=1024):
        path = os.path.join(self.dir, name)
        with open(path, "wb") as handle:
            handle.write(b"x" * size)
        stamp = self.now - age_days * DAY
        os.utime(path, (stamp, stamp))
        return path

    def names(self):
        return sorted(os.listdir(self.dir))


class AgeLimit(TempLogs):
    def test_older_than_the_limit_goes(self):
        self.write("old.csv", age_days=400)
        self.write("new.csv", age_days=10)
        result = retention.sweep(self.dir, max_age_days=365, now=self.now)
        self.assertEqual(self.names(), ["new.csv"])
        self.assertEqual(len(result.removed), 1)
        self.assertEqual(result.freed, 1024)
        self.assertEqual(result.remaining, 1024)

    def test_a_file_exactly_at_the_limit_stays(self):
        """The boundary is "older than", not "at least as old as".

        Arbitrary either way, but a file dated exactly a year ago is one somebody asking
        for a year of retention would expect to still have, and mtimes land on the boundary
        far more often than chance suggests -- a run of files copied in one operation
        shares a timestamp to the second.
        """
        self.write("edge.csv", age_days=365)
        retention.sweep(self.dir, max_age_days=365, now=self.now)
        self.assertEqual(self.names(), ["edge.csv"])

    def test_zero_means_no_age_limit(self):
        self.write("ancient.csv", age_days=10000)
        result = retention.sweep(self.dir, max_age_days=0, now=self.now)
        self.assertEqual(self.names(), ["ancient.csv"])
        self.assertEqual(result.removed, [])


class SizeLimit(TempLogs):
    def test_the_oldest_go_until_the_total_fits(self):
        for i in range(4):
            self.write("log%d.csv" % i, age_days=10 - i, size=1000)
        result = retention.sweep(self.dir, max_bytes=2500, now=self.now)
        # 4000 bytes against a 2500 ceiling: the two oldest go, the total lands at 2000.
        self.assertEqual(self.names(), ["log2.csv", "log3.csv"])
        self.assertEqual(result.remaining, 2000)

    def test_nothing_goes_when_the_total_already_fits(self):
        self.write("a.csv", size=1000)
        result = retention.sweep(self.dir, max_bytes=10000, now=self.now)
        self.assertEqual(self.names(), ["a.csv"])
        self.assertEqual(result.removed, [])

    def test_age_runs_first_and_size_sees_what_it_left(self):
        """A file removed for being old must not also be counted as freeing space.

        If the size pass worked from the pre-sweep total it would think it still had
        gigabytes to reclaim and would eat into files the age limit was happy with.
        """
        self.write("old.csv", age_days=400, size=3000)
        self.write("mid.csv", age_days=100, size=1000)
        self.write("new.csv", age_days=1, size=1000)
        result = retention.sweep(
            self.dir, max_age_days=365, max_bytes=2500, now=self.now
        )
        self.assertEqual(self.names(), ["mid.csv", "new.csv"])
        self.assertEqual(result.remaining, 2000)


class WhatItRefusesToTouch(TempLogs):
    def test_the_active_file_survives_both_limits(self):
        """The capture's own CSV is the oldest file in the directory the moment a service
        restarts into an append, and it is the one file that must never go."""
        active = self.write("active.csv", age_days=9999, size=5000)
        result = retention.sweep(
            self.dir, max_age_days=1, max_bytes=1, keep=[active], now=self.now
        )
        self.assertEqual(self.names(), ["active.csv"])
        self.assertEqual(result.removed, [])

    def test_the_active_file_still_counts_towards_the_ceiling(self):
        """Otherwise the one file guaranteed to be growing is the one not measured."""
        active = self.write("active.csv", age_days=0, size=5000)
        result = retention.sweep(self.dir, max_bytes=10 ** 9, keep=[active], now=self.now)
        self.assertEqual(result.remaining, 5000)

    def test_only_csvs_are_candidates(self):
        self.write("log.csv", age_days=400)
        other = os.path.join(self.dir, "notes.txt")
        with open(other, "w") as handle:
            handle.write("keep me")
        os.utime(other, (self.now - 400 * DAY,) * 2)
        retention.sweep(self.dir, max_age_days=365, now=self.now)
        self.assertEqual(self.names(), ["notes.txt"])

    def test_both_limits_off_deletes_nothing_but_still_reports(self):
        self.write("a.csv", age_days=10000, size=4096)
        result = retention.sweep(self.dir, now=self.now)
        self.assertEqual(result.removed, [])
        self.assertEqual(result.remaining, 4096)


class Robustness(TempLogs):
    def test_a_file_that_cannot_be_removed_is_stepped_over(self):
        """On Windows another process holding a handle to an old CSV is ordinary. The
        capture is working; a locked file is not a reason to stop it."""
        self.write("locked.csv", age_days=400, size=1000)
        self.write("also-old.csv", age_days=400, size=1000)

        real_remove = os.remove

        def refuse(path):
            if path.endswith("locked.csv"):
                raise OSError(13, "Permission denied")
            return real_remove(path)

        os.remove = refuse
        try:
            result = retention.sweep(self.dir, max_age_days=365, now=self.now)
        finally:
            os.remove = real_remove

        self.assertEqual(self.names(), ["locked.csv"])
        self.assertEqual(len(result.removed), 1)
        self.assertEqual(len(result.failed), 1)
        self.assertIn("could not be removed", result.summary())

    def test_a_missing_directory_is_not_an_error(self):
        """The sweep runs before the first sample and after a restart; a config naming a
        directory that does not exist yet should not be what stops the capture."""
        result = retention.sweep(os.path.join(self.dir, "nope"), max_age_days=1,
                                 now=self.now)
        self.assertEqual(result.removed, [])
        self.assertEqual(result.remaining, 0)

    def test_the_sweeper_closure_carries_its_limits(self):
        self.write("old.csv", age_days=400)
        sweep = retention.make_sweeper(self.dir, max_age_days=365)
        result = sweep()
        self.assertEqual(self.names(), [])
        self.assertEqual(len(result.removed), 1)


class Reporting(TempLogs):
    def test_the_quiet_summary_says_what_is_in_place(self):
        self.write("a.csv", size=2 * 1024 ** 2)
        summary = retention.sweep(self.dir, now=self.now).summary()
        self.assertIn("nothing to remove", summary)
        self.assertIn("2 MB", summary)

    def test_gigabytes_read_as_gigabytes(self):
        result = retention.SweepResult()
        result.remaining = 3 * 1024 ** 3
        self.assertIn("3.0 GB", result.summary())


if __name__ == "__main__":
    unittest.main()
