"""The Windows packaging, checked from a machine that is not Windows.

Most of what the installer does can only be tested by installing it, and that is what CI's
`windows-latest` job and a real machine are for. Three things can be pinned from here, and
they are the three that would otherwise be found by a user:

- `supervise.py`'s restart loop, which is the only real logic in `packaging/` and is the
  thing standing between an unplugged board and a service that has quietly stopped.
- The installed `nicla.conf`, parsed with `main.py`'s own parser. A misspelled key in a
  config file nobody opens is a setting that silently does not apply -- and this file is
  where the retention limits and the once-a-minute log rate live, so silently not applying
  is 3.2 GB a day.
- What `build.ps1` stages. Adding a module to `python/` is not enough to ship it if it is
  not a `.py` file, and the failure lands at import time on somebody else's machine.

The supervisor is imported by path because `packaging/service/` is not on any path this
project sets up. In the installed tree the embeddable interpreter's `._pth` does that job.
"""

import os
import re
import unittest

import support

import config
import main as capture_main

PACKAGING_DIR = os.path.join(support.REPO_DIR, "packaging")
SERVICE_DIR = os.path.join(PACKAGING_DIR, "service")


def load_supervise():
    """Import packaging/service/supervise.py under its own name, from its own path."""
    import importlib.util

    path = os.path.join(SERVICE_DIR, "supervise.py")
    spec = importlib.util.spec_from_file_location("supervise", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


supervise = load_supervise()


class Clock(object):
    """A clock the test advances by hand, so a five-minute rule costs no seconds."""

    def __init__(self):
        self.now = 0.0
        self.slept = []

    def time(self):
        return self.now

    def sleep(self, seconds):
        self.slept.append(seconds)
        self.now += seconds

    def advance(self, seconds):
        self.now += seconds


class RestartLoop(unittest.TestCase):
    def setUp(self):
        supervise._stopping = False
        self.addCleanup(setattr, supervise, "_stopping", False)
        self.clock = Clock()

    def run_loop(self, entry, max_runs=3):
        return supervise.run_forever(
            entry, [], name="test", clock=self.clock.time, sleep=self.clock.sleep,
            max_runs=max_runs,
        )

    def test_a_failing_entry_point_is_restarted(self):
        """Exit 1 is what main.py does when the board is not there, which at boot is the
        normal first outcome rather than an error worth stopping for."""
        calls = []
        self.run_loop(lambda argv: calls.append(1) or 1, max_runs=3)
        self.assertEqual(len(calls), 3)

    def test_a_clean_exit_is_restarted_too(self):
        """The reason this loop exists rather than being left to the service manager: the
        SCM reads exit 0 as a deliberate stop, and main.py has paths that end cleanly
        without anybody having asked -- a source that stops with no error, for one."""
        calls = []
        self.run_loop(lambda argv: calls.append(1) or 0, max_runs=2)
        self.assertEqual(len(calls), 2)

    def test_an_unhandled_exception_does_not_end_the_service(self):
        def explode(argv):
            raise RuntimeError("bad")

        self.run_loop(explode, max_runs=2)
        self.assertEqual(len(self.clock.slept), 1)

    def test_the_backoff_doubles_up_to_the_ceiling(self):
        supervise.run_forever(
            lambda argv: 1, [], name="test", clock=self.clock.time,
            sleep=self.clock.sleep, first_delay=5.0, max_delay=20.0, max_runs=6,
        )
        self.assertEqual(self.clock.slept, [5.0, 10.0, 20.0, 20.0, 20.0])

    def test_a_run_that_lasted_resets_the_backoff(self):
        """A capture that worked for three weeks and then lost the board should come back
        at once, not after the minute its last bad night earned."""
        state = {"runs": 0}

        def entry(argv):
            state["runs"] += 1
            # The third run is a long one; the two either side fail immediately.
            self.clock.advance(600.0 if state["runs"] == 3 else 1.0)
            return 1

        supervise.run_forever(
            entry, [], name="test", clock=self.clock.time, sleep=self.clock.sleep,
            first_delay=5.0, max_delay=80.0, max_runs=5,
        )
        self.assertEqual(self.clock.slept, [5.0, 10.0, 5.0, 10.0])

    def test_a_stop_request_ends_the_loop_without_restarting(self):
        def entry(argv):
            supervise._request_stop()
            return 1

        self.assertEqual(self.run_loop(entry, max_runs=5), 0)
        self.assertEqual(self.clock.slept, [])

    def test_a_keyboard_interrupt_counts_as_being_asked(self):
        """Which is how a Ctrl-C or a service stop reaches main.py: run_headless catches
        it, but a stop that arrives between runs lands here instead."""
        def entry(argv):
            raise KeyboardInterrupt()

        self.assertEqual(self.run_loop(entry, max_runs=5), 0)
        self.assertEqual(self.clock.slept, [])

    def test_the_backoff_wait_wakes_up_when_a_stop_is_requested(self):
        """The real sleeper, not the injected one. Since Python 3.5 a signal handler does
        not cut a time.sleep short -- it runs and the sleep resumes -- so a stop arriving
        during a minute of backoff would be sat on well past the service manager's stop
        timeout, and the capture would be killed rather than stopped.
        """
        import threading
        import time as real_time

        started = real_time.time()
        threading.Timer(0.1, supervise._request_stop).start()
        supervise.sleep_until_stopped(30.0)
        self.assertLess(real_time.time() - started, 5.0)

    def test_sys_exit_from_the_entry_point_is_a_code_not_a_crash(self):
        def entry(argv):
            raise SystemExit(2)

        self.run_loop(entry, max_runs=2)
        self.assertEqual(len(self.clock.slept), 1)


class CommandLine(unittest.TestCase):
    def test_a_bad_mode_is_refused(self):
        for argv in ([], ["nonsense"], ["--config", "c.conf"]):
            self.assertRaises(supervise.UsageError, supervise.split_args, argv)
        self.assertEqual(supervise.main(["nonsense"]), 2)

    def test_log_is_stripped_and_everything_else_passes_through(self):
        """Passing the rest through untouched is what keeps the service's command line
        runnable by hand, which is how anybody diagnoses a capture that is not capturing."""
        mode, log, rest = supervise.split_args(
            ["capture", "--config", "c.conf", "--log", "x.log", "--duration", "5"]
        )
        self.assertEqual(mode, "capture")
        self.assertEqual(log, "x.log")
        self.assertEqual(rest, ["--config", "c.conf", "--duration", "5"])

    def test_the_log_flag_is_optional(self):
        mode, log, rest = supervise.split_args(["dashboard", "127.0.0.1:8765"])
        self.assertEqual((mode, log, rest), ("dashboard", None, ["127.0.0.1:8765"]))

    def test_a_dangling_log_flag_is_an_error_not_an_index_crash(self):
        self.assertRaises(supervise.UsageError, supervise.split_args, ["capture", "--log"])

    def test_the_installed_command_lines_parse(self):
        """The two that actually ship, lifted from the service XML and the logon task, so
        a flag reordered in either is caught here rather than at the next boot."""
        mode, log, rest = supervise.split_args(
            ["capture", "--config", r"C:\ProgramData\NiclaSense\nicla.conf"]
        )
        self.assertEqual((mode, log), ("capture", None))
        self.assertEqual(rest[0], "--config")

        mode, log, rest = supervise.split_args(
            ["dashboard", "--log", r"C:\log\dashboard.log", "127.0.0.1:8765",
             "--http-port", "8988"]
        )
        self.assertEqual((mode, log), ("dashboard", r"C:\log\dashboard.log"))
        self.assertEqual(rest, ["127.0.0.1:8765", "--http-port", "8988"])


class InstalledConfig(unittest.TestCase):
    """nicla.conf, read the way the service will read it."""

    def setUp(self):
        self.path = os.path.join(PACKAGING_DIR, "nicla.conf")
        self.values = config.load(self.path, capture_main.build_parser())

    def test_every_key_is_a_real_flag(self):
        """config.load raises on an unknown key, so reaching here is the assertion. A
        misspelled key in a file nobody opens is a setting that silently does not apply."""
        self.assertTrue(self.values)

    def test_the_retention_limits_are_actually_set(self):
        """Off is the default everywhere else, and this is the one deployment where nobody
        is watching the disk. A year, and a ceiling sized for a year of resting data plus a
        day of solid bursting."""
        self.assertEqual(self.values["retain_days"], 365)
        self.assertEqual(self.values["retain_gb"], 4)

    def test_the_file_is_thinned_to_about_a_row_a_minute(self):
        self.assertAlmostEqual(self.values["log_rate"], 1 / 60.0, places=5)

    def test_the_csv_path_is_left_unset(self):
        """A fixed path would make every restart append to one file, and the retention
        sweep never deletes the file being written -- so that file would grow without
        limit, which is the precise thing the limits above exist to prevent."""
        self.assertNotIn("csv", self.values)

    def test_the_capture_does_not_start_its_own_dashboard(self):
        """--plot in a service means a dashboard in session 0, with nobody to show it to,
        holding the port the logon task needs."""
        self.assertFalse(self.values["plot"])

    def test_it_serves_loopback_only(self):
        self.assertTrue(self.values["listen"].startswith("127.0.0.1:"))


class WhatGetsShipped(unittest.TestCase):
    """Everything in python/ that is not a test, a benchmark or an artifact must be staged.

    build.ps1 copies by pattern, so a new .py file is shipped for free and a new asset
    directory -- another web/, a data file -- is not. That failure surfaces as an ImportError
    or a 404 on somebody else's machine, days later, which is worth a static check here.
    """

    NOT_SHIPPED = frozenset(("testing", "bench", "logs", "runs", "__pycache__"))

    def setUp(self):
        with open(os.path.join(PACKAGING_DIR, "build.ps1")) as handle:
            self.build = handle.read()
        # Copy-Item lines only. $PackagingDir and $RepoDir are also joined with the build's
        # own working directories, which are not things it stages.
        self.staged = re.findall(
            r'Copy-Item \(Join-Path \$RepoDir "python\\([^"]+)"\)', self.build
        )

    def test_every_source_entry_is_staged(self):
        missing = []
        for name in os.listdir(os.path.join(support.REPO_DIR, "python")):
            if name in self.NOT_SHIPPED or name.endswith(".csv") or name.startswith("."):
                continue
            covered = any(
                pattern == name or (pattern.endswith("*.py") and name.endswith(".py"))
                for pattern in self.staged
            )
            if not covered:
                missing.append(name)
        self.assertEqual(missing, [], "build.ps1 does not stage: %s" % ", ".join(missing))

    def test_the_test_suite_is_not_shipped(self):
        """Not a size argument -- it is a few hundred kilobytes. The suite spawns
        subprocesses and binds sockets, and none of that belongs on a machine that is only
        meant to be logging."""
        self.assertNotIn("python\\testing", self.build)

    def test_the_installer_ships_all_three_staged_directories(self):
        with open(os.path.join(PACKAGING_DIR, "nicla.iss")) as handle:
            iss = handle.read()
        for part in ("StageDir}\\python\\*", "StageDir}\\app\\*", "StageDir}\\service\\*"):
            self.assertIn(part, iss)

    def test_the_service_files_build_ps1_stages_all_exist(self):
        sources = re.findall(r'Copy-Item \(Join-Path \$PackagingDir "([^"]+)"\)', self.build)
        self.assertTrue(sources)
        for name in sources:
            path = os.path.join(PACKAGING_DIR, name.replace("\\", os.sep))
            self.assertTrue(os.path.exists(path), "%s is staged but missing" % name)


class PortsAgree(unittest.TestCase):
    """Three files name the same two ports, and nothing at runtime would catch a mismatch:
    the dashboard would simply sit there failing to attach to a capture that is fine."""

    def setUp(self):
        def read(*parts):
            with open(os.path.join(*parts)) as handle:
                return handle.read()

        self.conf = config.load(
            os.path.join(PACKAGING_DIR, "nicla.conf"), capture_main.build_parser()
        )
        self.task = read(SERVICE_DIR, "dashboard-task.ps1")
        self.iss = read(PACKAGING_DIR, "nicla.iss")

    def test_the_dashboard_attaches_to_where_the_capture_serves(self):
        self.assertIn('$Endpoint = "%s"' % self.conf["listen"], self.task)

    def test_the_start_menu_points_at_the_dashboards_port(self):
        match = re.search(r'\$HttpPort = (\d+)', self.task)
        self.assertIsNotNone(match)
        self.assertIn("http://127.0.0.1:%s/" % match.group(1), self.iss)


if __name__ == "__main__":
    unittest.main()
