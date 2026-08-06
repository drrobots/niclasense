"""The line protocol, which is the only thing holding the three processes together.

There is exactly one wire format in this project: the board's. The capture re-emits it to
viewers, the browser server re-emits it again, and both ends of every hop parse it with
_ThreadedSource. So the property that matters is a closed loop -- a sample formatted and
then parsed has to be the same sample, including its types -- and it is worth stating
because nothing at runtime would notice if it stopped being true. A float that arrived
where an int was expected does not raise; it just quietly writes "500.0" into a column the
board has always written as "500".

The framing is tested separately from the parsing. Bytes arrive from a socket in whatever
sizes the kernel felt like, so a line is routinely split across two reads, and the partial
tail has to survive between them.
"""

import unittest

import support
from columns import COLUMNS
from hub import DEFAULT_ENDPOINT, format_sample, parse_endpoint
from sources import SourceError, _ThreadedSource, check_schema, safe_rate_for


class RoundTrip(unittest.TestCase):
    """format_sample -> _consume_line -> the sample we started with."""

    def parse(self, line, source=None):
        source = source if source is not None else _ThreadedSource()
        source._consume_line(line)
        self.assertEqual(source.malformed, 0, "counted %r as malformed" % line)
        return source.queue.get_nowait()

    def test_a_plain_sample_survives_the_trip(self):
        original = support.sample(seq=7, t_ms=1234, ax_g=0.5, temp_C=21.25, gas_ohm=98765)
        self.assertEqual(self.parse(format_sample(original)), original)

    def test_types_survive_the_trip(self):
        """int columns come back as int, not as a float that happens to be whole."""
        original = support.sample(seq=1, t_ms=5, co2_eq_ppm=500, press_hPa=1013.25)
        parsed = self.parse(format_sample(original))
        for name, value in zip(COLUMNS, parsed):
            self.assertIs(type(value), type(original[COLUMNS.index(name)]), name)

    def test_awkward_values_survive_the_trip(self):
        """Negatives, very small magnitudes, and the precision the sketch actually sends.

        The quaternion columns are printed with five decimals and the accelerometer with
        four, so values at that scale are the realistic worst case; str() is used to
        format, and it is shortest-round-trip, so this should hold exactly rather than
        approximately.
        """
        original = support.sample(
            seq=4294967295, t_ms=86400000,
            ax_g=-0.0001, qw=0.99999, qx=-1e-05,
            press_hPa=1013.25, bvoc_eq_ppm=0.49,
        )
        self.assertEqual(self.parse(format_sample(original)), original)

    def test_a_whole_stream_survives_the_trip(self):
        source = _ThreadedSource()
        original = support.ramp(500, ax_g=0.25, heading_deg=180.5)
        for one in original:
            source._consume_line(format_sample(one))
        self.assertEqual(source.malformed, 0)
        self.assertEqual(
            [source.queue.get_nowait() for _ in original], original
        )


class Framing(unittest.TestCase):
    """_split_lines holds the partial tail back between reads."""

    def test_a_line_split_across_reads_is_reassembled(self):
        source = _ThreadedSource()
        line = format_sample(support.sample(seq=3, t_ms=15, ax_g=0.5))
        wire = (line + "\n").encode("ascii")
        self.assertEqual(source._split_lines(wire[:20]), [])
        self.assertEqual(source._split_lines(wire[20:]), [line])

    def test_several_lines_in_one_read(self):
        source = _ThreadedSource()
        lines = [format_sample(one) for one in support.ramp(4)]
        chunk = ("\n".join(lines) + "\n").encode("ascii")
        self.assertEqual(source._split_lines(chunk), lines)

    def test_a_trailing_partial_line_is_held_back(self):
        source = _ThreadedSource()
        lines = [format_sample(one) for one in support.ramp(3)]
        chunk = ("\n".join(lines)).encode("ascii")  # no final newline
        self.assertEqual(source._split_lines(chunk), lines[:2])
        self.assertEqual(source._split_lines(b"\n"), [lines[2]])

    def test_endless_bytes_with_no_newline_are_dropped(self):
        """A wrong baud rate streams dense garbage that can contain no 0x0A at all.

        Growing the buffer for it would be an unbounded allocation driven by whatever is
        on the wire, so past a line's length by a wide margin the partial tail is thrown
        away rather than kept.
        """
        source = _ThreadedSource()
        for _ in range(4):
            source._split_lines(b"\xff" * 4096)
        self.assertLessEqual(len(source._rx), 8192)


class Malformed(unittest.TestCase):
    def test_a_short_row_is_counted_not_raised(self):
        source = _ThreadedSource()
        source._consume_line("1,2,3")
        self.assertEqual(source.malformed, 1)
        self.assertTrue(source.queue.empty())

    def test_a_non_numeric_field_is_counted(self):
        source = _ThreadedSource()
        fields = format_sample(support.sample()).split(",")
        fields[4] = "banana"
        source._consume_line(",".join(fields))
        self.assertEqual(source.malformed, 1)

    def test_a_float_in_an_integer_column_is_counted(self):
        """Which is how a mismatched producer shows up rather than as silent corruption."""
        source = _ThreadedSource()
        fields = format_sample(support.sample()).split(",")
        fields[COLUMNS.index("gas_ohm")] = "1234.5"
        source._consume_line(",".join(fields))
        self.assertEqual(source.malformed, 1)

    def test_blank_lines_are_ignored(self):
        source = _ThreadedSource()
        source._consume_line("")
        self.assertEqual(source.malformed, 0)


class Banners(unittest.TestCase):
    def test_a_banner_yields_the_rate_and_baud(self):
        source = _ThreadedSource()
        source._consume_line(
            "# nicla-stream: v3 rate_hz=200 baud=1000000 columns=27"
        )
        self.assertEqual(source.stream_hz, 200)
        self.assertEqual(source.reported_baud, 1000000)
        self.assertTrue(source.queue.empty())

    def test_a_later_banner_replaces_the_earlier_one(self):
        """This is how a rate command is acknowledged; a stale banner would look like
        the board refusing one."""
        source = _ThreadedSource()
        source._consume_line("# nicla-stream: v3 rate_hz=200 baud=1000000")
        source._consume_line("# nicla-stream: v3 rate_hz=50 baud=1000000")
        self.assertEqual(source.stream_hz, 50)

    def test_a_banner_without_settings_is_not_an_error(self):
        source = _ThreadedSource()
        source._consume_line("# something else entirely")
        self.assertEqual(source.malformed, 0)
        self.assertEqual(source.stream_hz, None)


class Schema(unittest.TestCase):
    def test_the_matching_schema_line_is_accepted(self):
        self.assertEqual(check_schema("#" + ",".join(COLUMNS), "Board", ""), COLUMNS)

    def test_drift_names_both_sides(self):
        theirs = COLUMNS[:-1]
        try:
            check_schema("#" + ",".join(theirs), "Board", "Reflash it.")
        except SourceError as exc:
            message = str(exc)
        else:
            self.fail("a short schema line was accepted")
        self.assertIn("Board", message)
        self.assertIn(",".join(COLUMNS), message)
        self.assertIn("Reflash it.", message)

    def test_reordering_is_drift_too(self):
        swapped = (COLUMNS[1], COLUMNS[0]) + COLUMNS[2:]
        self.assertRaises(
            SourceError, check_schema, "#" + ",".join(swapped), "Logger", ""
        )


class Endpoints(unittest.TestCase):
    def test_the_forms_a_person_actually_types(self):
        default_host, default_port = parse_endpoint(DEFAULT_ENDPOINT)
        cases = {
            "": (default_host, default_port),
            None: (default_host, default_port),
            "8790": (default_host, 8790),
            "bench.local": ("bench.local", default_port),
            "bench.local:8790": ("bench.local", 8790),
            "0.0.0.0:8765": ("0.0.0.0", 8765),
            ":8790": (default_host, 8790),
            "  8790  ": (default_host, 8790),
        }
        for text, expected in cases.items():
            self.assertEqual(parse_endpoint(text), expected, repr(text))

    def test_nonsense_raises_rather_than_guessing(self):
        """main.py turns this into a one-line error and exit 1, not a traceback."""
        for text in ("nonsense:", "host:port", "1.2.3.4:", ":"):
            self.assertRaises(ValueError, parse_endpoint, text)

    def test_the_last_colon_is_the_separator(self):
        """Which is what lets a bracketed IPv6 literal keep its own colons.

        It also means a typo like 'host:80:80' parses as a host called 'host:80' rather
        than being rejected. That is deliberate here only in the sense that rpartition
        buys the IPv6 form for free; the failure lands at connect time instead, with the
        bad host named in the message.
        """
        self.assertEqual(parse_endpoint("[::1]:8765"), ("[::1]", 8765))
        self.assertEqual(parse_endpoint("host:80:80"), ("host:80", 80))


class LinkBudget(unittest.TestCase):
    """safe_rate_for is what stops a rate that would wedge the board rather than drop
    samples; see LINK_BUDGET."""

    def test_the_shipped_design_point_is_allowed(self):
        self.assertEqual(safe_rate_for(1000000), 200)

    def test_slower_links_are_clamped_and_never_to_nothing(self):
        self.assertLess(safe_rate_for(115200), 200)
        self.assertGreaterEqual(safe_rate_for(9600), 1)

    def test_the_clamp_is_monotonic(self):
        rates = [safe_rate_for(b) for b in (9600, 57600, 115200, 230400, 460800, 1000000)]
        self.assertEqual(rates, sorted(rates))


if __name__ == "__main__":
    unittest.main()
