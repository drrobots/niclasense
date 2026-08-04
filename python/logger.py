"""Append Nicla samples to a CSV file."""

import csv
import datetime
import os

from columns import CSV_COLUMNS


class CsvLogger(object):
    """Appends samples to a CSV, writing the header only when the file is new.

    Re-running against an existing log genuinely appends: no duplicate header row.
    """

    def __init__(self, path, flush_every=200):
        self.path = path
        self.flush_every = flush_every
        self.rows_written = 0
        self._handle = None
        self._writer = None
        self._since_flush = 0

    def open(self):
        directory = os.path.dirname(os.path.abspath(self.path))
        if directory and not os.path.isdir(directory):
            os.makedirs(directory)

        needs_header = (
            not os.path.exists(self.path) or os.path.getsize(self.path) == 0
        )
        self._handle = open(self.path, "a", newline="")
        self._writer = csv.writer(self._handle)
        if needs_header:
            self._writer.writerow(CSV_COLUMNS)
            self._handle.flush()
        return self

    def write(self, sample):
        """Write one sample tuple, stamped with the host's wall clock."""
        host_iso = datetime.datetime.now().isoformat(timespec="milliseconds")
        self._writer.writerow((host_iso,) + tuple(sample))
        self.rows_written += 1
        self._since_flush += 1
        if self._since_flush >= self.flush_every:
            self._handle.flush()
            self._since_flush = 0

    def close(self):
        if self._handle is not None:
            self._handle.flush()
            self._handle.close()
            self._handle = None

    def __enter__(self):
        return self.open()

    def __exit__(self, *_exc):
        self.close()
