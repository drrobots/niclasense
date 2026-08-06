"""The CSV writer, which is the only part of a capture that outlives the process.

Two behaviours are worth holding still. Appending has to genuinely append -- a second
header row halfway down a file is not something view.py's tolerant loader should have to
cope with, and it would silently become a "malformed row" it skipped. And the integer
columns have to reach the file as integers: the whole chain from the sketch's DECIMALS
table through columns.PARSERS exists to keep gas_ohm reading 98765 rather than 98765.0,
and this is the last place that can be checked without a board.
"""

import csv
import os
import shutil
import tempfile
import unittest

import support
from columns import BURST_COLUMN, CSV_COLUMNS, INTEGER_COLUMNS
from logger import CsvLogger


class LoggerFixture(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.mkdtemp(prefix="nicla-test-")
        self.addCleanup(shutil.rmtree, self.directory, True)
        self.path = os.path.join(self.directory, "capture.csv")

    def rows(self, path=None):
        with open(path or self.path, newline="") as handle:
            return list(csv.reader(handle))


class Header(LoggerFixture):
    def test_a_new_file_gets_a_header(self):
        with CsvLogger(self.path) as log:
            log.write(support.sample(seq=1, t_ms=5))
        self.assertEqual(self.rows()[0], list(CSV_COLUMNS))

    def test_appending_does_not_repeat_the_header(self):
        for run in range(3):
            with CsvLogger(self.path) as log:
                log.write(support.sample(seq=run, t_ms=run * 5))
        rows = self.rows()
        self.assertEqual(len(rows), 4)
        self.assertEqual(sum(1 for row in rows if row[0] == CSV_COLUMNS[0]), 1)

    def test_an_existing_but_empty_file_still_gets_a_header(self):
        open(self.path, "w").close()
        with CsvLogger(self.path) as log:
            log.write(support.sample())
        self.assertEqual(self.rows()[0], list(CSV_COLUMNS))

    def test_the_burst_column_is_only_there_when_decimating(self):
        """At full rate it would be a column of zeroes, and the schema the README
        documents is the 28-column one."""
        with CsvLogger(self.path) as log:
            log.write(support.sample())
        self.assertNotIn(BURST_COLUMN, self.rows()[0])

        marked = os.path.join(self.directory, "decimated.csv")
        with CsvLogger(marked, mark_bursts=True) as log:
            log.write(support.sample(), burst=1)
        header = self.rows(marked)[0]
        self.assertEqual(header[-1], BURST_COLUMN)
        self.assertEqual(self.rows(marked)[1][-1], "1")

    def test_the_directory_is_created(self):
        nested = os.path.join(self.directory, "runs", "today", "walk.csv")
        with CsvLogger(nested) as log:
            log.write(support.sample())
        self.assertTrue(os.path.isfile(nested))


class RowContents(LoggerFixture):
    def test_the_host_clock_is_prepended_and_parseable(self):
        import datetime

        with CsvLogger(self.path) as log:
            log.write(support.sample(seq=1, t_ms=5))
        stamp = self.rows()[1][0]
        self.assertIsInstance(datetime.datetime.fromisoformat(stamp), datetime.datetime)

    def test_integer_columns_are_written_without_a_decimal_point(self):
        original = support.sample(seq=42, t_ms=1234, gas_ohm=98765, co2_eq_ppm=500)
        with CsvLogger(self.path) as log:
            log.write(original)
        header, row = self.rows()
        for name in INTEGER_COLUMNS:
            self.assertNotIn(".", row[header.index(name)], name)

    def test_a_row_reads_back_as_the_sample_that_went_in(self):
        original = support.sample(seq=9, t_ms=45, ax_g=-0.0001, press_hPa=1013.25)
        with CsvLogger(self.path) as log:
            log.write(original)
        header, row = self.rows()
        from columns import COLUMNS, PARSERS

        read_back = tuple(
            parse(row[header.index(name)]) for name, parse in zip(COLUMNS, PARSERS)
        )
        self.assertEqual(read_back, original)

    def test_rows_written_counts_rows(self):
        log = CsvLogger(self.path).open()
        for one in support.ramp(37):
            log.write(one)
        log.close()
        self.assertEqual(log.rows_written, 37)
        self.assertEqual(len(self.rows()), 38)


class Flushing(LoggerFixture):
    def test_data_is_on_disk_before_the_flush_interval_is_reached(self):
        """Not required for correctness, but a `tail -f` that shows nothing for forty
        seconds is how a healthy capture gets killed and restarted."""
        log = CsvLogger(self.path, flush_every=5).open()
        self.addCleanup(log.close)
        for one in support.ramp(12):
            log.write(one)
        # Ten of the twelve have crossed two flush boundaries; the remainder may still be
        # in the buffer, which is the trade flush_every exists to make.
        self.assertGreaterEqual(len(self.rows()), 11)

    def test_close_flushes_the_remainder(self):
        log = CsvLogger(self.path, flush_every=1000).open()
        for one in support.ramp(7):
            log.write(one)
        log.close()
        self.assertEqual(len(self.rows()), 8)

    def test_close_is_idempotent(self):
        log = CsvLogger(self.path).open()
        log.write(support.sample())
        log.close()
        log.close()


if __name__ == "__main__":
    unittest.main()
