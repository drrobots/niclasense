"""The INI loader, whose whole design claim is that it stays in step with main.py.

config.py derives the set of settable keys, and their types, from the parser main.py
already builds. That is a good design and an untestable-looking one, so the tests below
lean on it deliberately: they use main.build_parser() rather than a parser of their own,
which means a flag added to main.py is covered here the moment it is added, and a flag
whose type argparse and configparser disagree about fails here rather than in a file
someone wrote at midnight.

The two argparse edges the README documents -- repeatable flags accumulate, store_true
flags cannot be turned back off -- are pinned down too. They are surprising enough to be
worth a test that says "yes, on purpose".
"""

import os
import shutil
import tempfile
import unittest

import support  # noqa: F401 -- puts python/ on the path
import config
import main


class ConfigFixture(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.mkdtemp(prefix="nicla-test-")
        self.addCleanup(shutil.rmtree, self.directory, True)

    def write(self, text, name="test.conf"):
        path = os.path.join(self.directory, name)
        with open(path, "w") as handle:
            handle.write(text)
        return path

    def load(self, text):
        return config.load(self.write(text), main.build_parser())

    def failure(self, text):
        try:
            self.load(text)
        except config.ConfigError as exc:
            return str(exc)
        self.fail("that config file was accepted")

    def args(self, text, argv):
        """What main.py would end up with, given a file and a command line."""
        return main.parse_args(["--config", self.write(text)] + argv)


class Types(ConfigFixture):
    def test_values_come_back_as_the_flag_produces_them(self):
        values = self.load(
            "[capture]\n"
            "baud = 460800\n"
            "log_rate = 5\n"
            "csv = runs/walk.csv\n"
            "listen = 127.0.0.1:8790\n"
        )
        self.assertEqual(values["baud"], 460800)
        self.assertIs(type(values["baud"]), int)
        self.assertEqual(values["log_rate"], 5.0)
        self.assertIs(type(values["log_rate"]), float)
        self.assertEqual(values["csv"], "runs/walk.csv")
        self.assertEqual(values["listen"], "127.0.0.1:8790")

    def test_dashes_and_underscores_are_the_same_key(self):
        self.assertEqual(self.load("log-rate = 5\n")["log_rate"], 5.0)

    def test_booleans_take_every_form_configparser_knows(self):
        for text in ("true", "yes", "on", "1"):
            self.assertIs(self.load("plot = %s\n" % text)["plot"], True)
        for text in ("false", "no", "off", "0"):
            self.assertIs(self.load("plot = %s\n" % text)["plot"], False)

    def test_a_repeatable_flag_reads_as_a_list(self):
        values = self.load(
            "[burst]\nburst_on =\n    ax_g:0.2\n    gz_dps:30\n"
        )
        self.assertEqual(values["burst_on"], ["ax_g:0.2", "gz_dps:30"])

    def test_a_repeatable_flag_also_takes_commas(self):
        self.assertEqual(
            self.load("burst_on = ax_g:0.2, gz_dps:30\n")["burst_on"],
            ["ax_g:0.2", "gz_dps:30"],
        )

    def test_an_empty_list_means_the_defaults(self):
        self.assertEqual(self.load("burst_on =\n")["burst_on"], [])


class Errors(ConfigFixture):
    def test_an_unknown_key_is_refused_with_a_suggestion(self):
        message = self.failure("log_rat = 5\n")
        self.assertIn("log_rat", message)
        self.assertIn("log_rate", message)

    def test_a_key_that_is_not_a_flag_at_all_is_refused(self):
        self.assertIn("banana", self.failure("banana = 5\n"))

    def test_the_flags_that_print_and_exit_are_not_settable(self):
        """`config` and `list_ports` mean nothing in a file, so they read as typos."""
        for key in ("config", "list_ports", "help"):
            self.assertIn("unknown setting", self.failure("%s = 1\n" % key))

    def test_a_bad_number_names_the_key_and_the_type(self):
        message = self.failure("baud = fast\n")
        self.assertIn("baud", message)
        self.assertIn("int", message)

    def test_a_bad_boolean_lists_what_would_have_worked(self):
        self.assertIn("yes/no", self.failure("plot = maybe\n"))

    def test_a_missing_file_is_reported_not_raised(self):
        try:
            config.load(os.path.join(self.directory, "nope.conf"), main.build_parser())
        except config.ConfigError as exc:
            self.assertIn("nope.conf", str(exc))
        else:
            self.fail("a missing config file was accepted")

    def test_a_duplicated_key_is_refused(self):
        self.assertIn("twice", self.failure("baud = 9600\nbaud = 115200\n"))


class LineNumbers(ConfigFixture):
    """A file that opens with keys gets a synthetic section header, which shifts every
    line configparser counts. An error pointing one line off is worse than no line."""

    def test_a_syntax_error_in_a_headerless_file_points_at_the_right_line(self):
        message = self.failure("baud = 9600\nlog_rate = 5\nthis is not a setting\n")
        self.assertIn("line 3", message)

    def test_a_syntax_error_under_a_header_points_at_the_right_line(self):
        message = self.failure("[capture]\nbaud = 9600\nthis is not a setting\n")
        self.assertIn("line 3", message)


class SectionsAreForTheReader(ConfigFixture):
    def test_a_key_under_the_wrong_heading_still_applies(self):
        """Silently ignoring a correctly spelled key sitting in plain sight is the worst
        way for a config file to fail."""
        self.assertEqual(self.load("[plot]\nbaud = 460800\n")["baud"], 460800)

    def test_keys_before_any_heading_apply(self):
        self.assertEqual(self.load("baud = 460800\n[plot]\nplot = true\n")["baud"], 460800)

    def test_comments_and_blank_lines_are_fine(self):
        values = self.load("# a comment\n\n; another\n\n[capture]\nbaud = 9600\n")
        self.assertEqual(values["baud"], 9600)

    def test_a_percent_sign_in_a_value_is_not_a_substitution(self):
        self.assertEqual(self.load("csv = runs/100%%.csv\n")["csv"], "runs/100%%.csv")


class Precedence(ConfigFixture):
    def test_the_file_beats_the_default(self):
        self.assertEqual(self.args("baud = 460800\n", []).baud, 460800)

    def test_the_command_line_beats_the_file(self):
        self.assertEqual(self.args("baud = 460800\n", ["--baud", "9600"]).baud, 9600)

    def test_the_default_survives_a_file_that_says_nothing(self):
        self.assertEqual(self.args("# nothing here\n", []).baud, 1000000)

    def test_the_shipped_example_config_loads(self):
        """example.conf is documentation that can go stale; here it has to parse against
        the parser as it stands, and every key in it has to still exist."""
        example = os.path.join(support.PYTHON_DIR, "example.conf")
        values = config.load(example, main.build_parser())
        self.assertTrue(values)


class ArgparseEdges(ConfigFixture):
    """Both of these are inherited from argparse and documented in the README. They are
    tested because they are surprising, not because they are ideal."""

    def test_command_line_triggers_add_to_the_file_rather_than_replacing_them(self):
        args = self.args(
            "burst_on = ax_g:0.2\n", ["--burst-on", "gz_dps:30"]
        )
        self.assertEqual(args.burst_on, ["ax_g:0.2", "gz_dps:30"])

    def test_a_flag_turned_on_by_the_file_cannot_be_turned_off_on_the_command_line(self):
        """There is no --no-plot to counter --plot with, so a file that sets it wins."""
        self.assertIs(self.args("plot = true\n", []).plot, True)
        self.assertIs(self.args("plot = false\n", ["--plot"]).plot, True)


if __name__ == "__main__":
    unittest.main()
