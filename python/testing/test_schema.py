"""The schema is two-sided; this is the side that can be checked without a board.

CLAUDE.md states the rule -- the column list in nicla_stream.ino and COLUMNS in columns.py
must match -- and SerialSource enforces it at connect time. But that enforcement needs the
board plugged in, and the two files are edited on different days by a person who has just
added a sensor. Everything the sketch declares about its own output is a literal sitting in
the source, so the drift is visible statically, and finding it here costs a second instead
of a flash cycle.

Three declarations are checked, because all three can drift independently:

    the header line          -> COLUMNS, in order
    COLUMN_COUNT             -> len(COLUMNS)
    the DECIMALS table       -> INTEGER_COLUMNS

That last one is the quiet one. A column printed with zero decimals is an integer on the
wire, and columns.py has to parse it as one or the CSV gains a ".0" the board never sent.
Nothing at runtime notices the difference, since int and float both parse a bare "500".
"""

import re
import unittest

import support
from columns import COLUMNS, CSV_COLUMNS, HOST_TIME_COLUMN, INTEGER_COLUMNS, PARSERS


def sketch_text():
    with open(support.SKETCH) as handle:
        return handle.read()


def sketch_columns(text):
    """The column names out of printHeader()'s '#seq,t_ms,...' literal.

    The literal is split across source lines as adjacent C string constants, which the
    compiler concatenates; this does the same, then strips the leading '#' the wire format
    uses to mark the line as metadata.
    """
    match = re.search(r'F\(\s*("#seq(?:[^)]*?))\s*\)\s*\)\s*;', text, re.S)
    if match is None:
        raise AssertionError("could not find the header literal in %s" % support.SKETCH)
    joined = "".join(re.findall(r'"([^"]*)"', match.group(1)))
    return tuple(joined.lstrip("#").split(","))


def sketch_column_count(text):
    match = re.search(r"COLUMN_COUNT\s*=\s*(\d+)", text)
    if match is None:
        raise AssertionError("could not find COLUMN_COUNT in %s" % support.SKETCH)
    return int(match.group(1))


def sketch_decimals(text):
    """The DECIMALS table as a list of ints, comments discarded."""
    match = re.search(r"DECIMALS\[\d+\]\s*=\s*\{(.*?)\}", text, re.S)
    if match is None:
        raise AssertionError("could not find DECIMALS in %s" % support.SKETCH)
    body = re.sub(r"//[^\n]*", "", match.group(1))
    return [int(piece) for piece in re.findall(r"\d+", body)]


class SketchAgreesWithColumns(unittest.TestCase):
    def setUp(self):
        self.text = sketch_text()

    def test_column_names_and_order_match(self):
        self.assertEqual(sketch_columns(self.text), COLUMNS)

    def test_column_count_constant_matches(self):
        self.assertEqual(sketch_column_count(self.text), len(COLUMNS))

    def test_integer_columns_match_the_decimals_table(self):
        """A column the sketch prints with 0 decimals must be an int on this side.

        DECIMALS covers the 24 values between seq/t_ms and bsec_acc; printCsv() writes
        those three itself, from integer types, so they are integers by construction and
        are added here rather than being looked up.
        """
        decimals = sketch_decimals(self.text)
        middle = COLUMNS[2:-1]
        self.assertEqual(
            len(decimals), len(middle),
            "DECIMALS has %d entries for %d value columns" % (len(decimals), len(middle)),
        )
        expected = set(("seq", "t_ms", "bsec_acc"))
        expected.update(
            name for name, places in zip(middle, decimals) if places == 0
        )
        self.assertEqual(set(INTEGER_COLUMNS), expected)


class ColumnsInternalConsistency(unittest.TestCase):
    """columns.py derives several things from COLUMNS; they should stay derived."""

    def test_parsers_line_up_with_columns(self):
        self.assertEqual(len(PARSERS), len(COLUMNS))
        for name, parser in zip(COLUMNS, PARSERS):
            self.assertIs(parser, int if name in INTEGER_COLUMNS else float, name)

    def test_no_duplicate_column_names(self):
        self.assertEqual(len(set(COLUMNS)), len(COLUMNS))

    def test_csv_columns_are_the_wire_columns_plus_the_host_clock(self):
        self.assertEqual(CSV_COLUMNS, (HOST_TIME_COLUMN,) + COLUMNS)
        self.assertNotIn(HOST_TIME_COLUMN, COLUMNS)

    def test_integer_columns_all_exist(self):
        self.assertEqual(set(INTEGER_COLUMNS) - set(COLUMNS), set())


if __name__ == "__main__":
    unittest.main()
