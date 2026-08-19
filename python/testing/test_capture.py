"""A whole capture, end to end, with no board.

This is the test the rest of the suite is scaffolding for. main.py is run for real -- its
argument parsing, its decimator, its CSV writer, its socket hub, its shutdown -- with only
the source replaced, which is exactly the substitution testing/replay.py exists to make.
Everything downstream of the serial port runs unmodified, and a viewer attaches over TCP
while it does, so the assertions are about the two artefacts a capture actually produces:
the file on disk and the stream on the socket.

What this cannot cover is SerialSource: auto-detect, the auto-baud sweep, the rate
handshake, and the byte-paced command writer. Those are the board, and standing in for the
board is the whole premise here. They stay verified by running against hardware.

Timings are real, because the replay is paced from the recording's own t_ms. A test here
costs about as long as the capture it runs.
"""

import os
import shutil
import tempfile
import threading
import unittest

import support
import main
import replay
from columns import COLUMNS, CSV_COLUMNS, PARSERS
from logger import CsvLogger
from sources import StreamSource


def read_csv(path):
    """A logged CSV back as (header, [sample tuples])."""
    import csv

    with open(path, newline="") as handle:
        rows = list(csv.reader(handle))
    header, body = rows[0], rows[1:]
    samples = [
        tuple(parse(row[header.index(name)]) for name, parse in zip(COLUMNS, PARSERS))
        for row in body
    ]
    return header, samples


class CaptureFixture(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.mkdtemp(prefix="nicla-test-")
        self.addCleanup(shutil.rmtree, self.directory, True)
        self.port = support.free_port()
        self.csv = os.path.join(self.directory, "out.csv")
        self.original_create_source = main.create_source
        self.addCleanup(self.restore)

    def restore(self):
        main.create_source = self.original_create_source

    def recording(self, samples, name="source.csv"):
        """A log file for the replay source to read back."""
        path = os.path.join(self.directory, name)
        with CsvLogger(path) as log:
            for one in samples:
                log.write(one)
        return path

    def capture(self, source_path, argv):
        """Run main.main() against a recording, returning its exit code.

        Started on a thread so a viewer can attach while it is running, which is the only
        way to exercise the hub against a live capture rather than a hub on its own.
        """
        main.create_source = lambda _args: replay.ReplaySource(source_path)
        result = {}
        argv = ["--listen", "127.0.0.1:%d" % self.port] + argv

        def run():
            result["code"] = main.main(argv)

        thread = threading.Thread(target=run)
        thread.start()
        return thread, result


class FullRateCapture(CaptureFixture):
    def test_the_csv_is_the_stream_that_went_in(self):
        samples = support.ramp(1000, hz=200.0, ax_g=0.5, temp_C=21.25, gas_ohm=98765)
        recording = self.recording(samples)

        thread, result = self.capture(recording, ["--csv", self.csv, "--duration", "2"])
        thread.join(timeout=30.0)
        self.assertFalse(thread.is_alive(), "the capture did not stop on --duration")
        self.assertEqual(result["code"], 0)

        header, written = read_csv(self.csv)
        self.assertEqual(header, list(CSV_COLUMNS))
        # Two seconds of a five-second recording, so what landed is a prefix of it --
        # every row, in order, with its types intact.
        self.assertTrue(written)
        self.assertEqual(written, samples[:len(written)])
        self.assertGreater(len(written), 200)

    def test_a_viewer_attached_mid_capture_sees_the_same_samples(self):
        samples = support.ramp(1000, hz=200.0, ax_g=0.25, gy_dps=12.5)
        recording = self.recording(samples)

        thread, result = self.capture(recording, ["--csv", self.csv, "--duration", "3"])
        self.addCleanup(thread.join, 30.0)

        viewer = StreamSource(host="127.0.0.1", port=self.port, timeout=10.0)
        self.assertTrue(
            support.wait_for(lambda: self._try_open(viewer), timeout=10.0),
            "could not attach to the capture",
        )
        self.addCleanup(viewer.stop)
        viewer.start()

        thread.join(timeout=30.0)
        self.assertEqual(result["code"], 0)

        received = []
        while not viewer.queue.empty():
            received.append(viewer.queue.get_nowait())

        self.assertTrue(received, "the viewer received nothing")
        self.assertEqual(viewer.malformed, 0)
        self.assertEqual(viewer.dropped, 0)
        # Whatever the viewer saw, it saw as an unbroken run of the recording.
        first = samples.index(received[0])
        self.assertEqual(received, samples[first:first + len(received)])
        self.assertEqual(viewer.stream_hz, 200)

    def _try_open(self, source):
        from sources import SourceError

        try:
            source.open()
        except SourceError:
            return False
        return True

    def test_no_csv_is_asked_for_and_none_is_written(self):
        recording = self.recording(support.ramp(400, hz=200.0))
        thread, result = self.capture(recording, ["--csv", "none", "--duration", "1"])
        thread.join(timeout=30.0)
        self.assertEqual(result["code"], 0)
        self.assertFalse(os.path.exists(self.csv))

    def test_a_capture_appends_to_an_existing_log(self):
        recording = self.recording(support.ramp(600, hz=200.0))
        for _run in range(2):
            thread, result = self.capture(
                recording, ["--csv", self.csv, "--duration", "1"]
            )
            thread.join(timeout=30.0)
            self.assertEqual(result["code"], 0)
        with open(self.csv) as handle:
            lines = handle.readlines()
        self.assertEqual(sum(1 for line in lines if line.startswith("host_iso")), 1)


class PlotFlag(CaptureFixture):
    """--plot starts webdash.py as a child attached over our own socket.

    A real child process, because that is the whole claim: the dashboard is not a mode of
    the capture, it is a viewer that happens to have been started for you. If it were
    imported instead, its HTTP server and its clients would share this interpreter with
    the thread that has to keep draining the serial queue.

    $BROWSER is pointed at /usr/bin/true for the duration so webdash.py's --open does not
    put a window on the screen of whoever is running the suite. It is inherited by the
    child, which is where the open actually happens.
    """

    def setUp(self):
        CaptureFixture.setUp(self)
        self.http_port = support.free_port()
        previous = os.environ.get("BROWSER")
        os.environ["BROWSER"] = "/usr/bin/true"
        self.addCleanup(self._restore_browser, previous)

    @staticmethod
    def _restore_browser(previous):
        if previous is None:
            os.environ.pop("BROWSER", None)
        else:
            os.environ["BROWSER"] = previous

    def fetch(self, path):
        import urllib.request

        response = urllib.request.urlopen(
            "http://127.0.0.1:%d%s" % (self.http_port, path), timeout=5.0
        )
        return response.status, response.read()

    def test_the_dashboard_comes_up_and_serves_its_page(self):
        import json

        recording = self.recording(support.ramp(2000, hz=200.0, ax_g=0.5))
        thread, result = self.capture(recording, [
            "--csv", self.csv, "--plot",
            "--http-port", "%d" % self.http_port, "--duration", "5",
        ])
        self.addCleanup(thread.join, 30.0)

        self.assertTrue(
            support.wait_for(lambda: self._serving(), timeout=15.0, interval=0.1),
            "the dashboard never started serving",
        )
        # It is the real page, and the spec it hands out is this project's.
        self.assertEqual(self.fetch("/")[0], 200)
        spec = json.loads(self.fetch("/spec")[1].decode("utf-8"))
        self.assertEqual(spec["columns"], list(COLUMNS))

        thread.join(timeout=30.0)
        self.assertEqual(result["code"], 0)

    def _serving(self):
        try:
            return self.fetch("/")[0] == 200
        except Exception:
            return False

    def test_the_dashboard_attaches_as_an_ordinary_viewer(self):
        """Over the same socket anything else would use -- so the capture counts it, and
        killing it would leave the capture running."""
        recording = self.recording(support.ramp(2000, hz=200.0))
        thread, result = self.capture(recording, [
            "--csv", self.csv, "--plot",
            "--http-port", "%d" % self.http_port, "--duration", "5",
        ])
        self.addCleanup(thread.join, 30.0)

        self.assertTrue(support.wait_for(self._serving, timeout=15.0, interval=0.1))
        # A second viewer alongside it, proving the socket is not exclusive to the child.
        viewer = StreamSource(host="127.0.0.1", port=self.port, timeout=10.0)
        viewer.open()
        self.addCleanup(viewer.stop)
        viewer.start()

        thread.join(timeout=30.0)
        self.assertEqual(result["code"], 0)
        self.assertGreater(viewer.queue.qsize(), 0)

    def test_the_capture_survives_a_dashboard_that_will_not_start(self):
        """A convenience viewer failing is not a reason to lose a good capture.

        Forced here by handing --plot a port that is already taken, which is exactly what
        a second `main.py --plot` on one machine does.
        """
        import socket

        squatter = socket.socket()
        squatter.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        squatter.bind(("127.0.0.1", self.http_port))
        squatter.listen(1)
        self.addCleanup(squatter.close)

        recording = self.recording(support.ramp(1000, hz=200.0))
        thread, result = self.capture(recording, [
            "--csv", self.csv, "--plot",
            "--http-port", "%d" % self.http_port, "--duration", "2",
        ])
        thread.join(timeout=30.0)
        self.assertEqual(result["code"], 0)
        _header, written = read_csv(self.csv)
        self.assertGreater(len(written), 200)


class DecimatedCapture(CaptureFixture):
    def test_the_file_is_thinned_and_the_socket_is_not(self):
        """The property make_drain exists to guarantee, checked through both artefacts."""
        quiet = support.ramp(600, hz=200.0)
        shake = support.ramp(400, hz=200.0, start_seq=600, start_ms=3000, ax_g=1.0)
        recording = self.recording(quiet + shake)

        thread, result = self.capture(
            recording, ["--csv", self.csv, "--log-rate", "5", "--duration", "4"]
        )
        self.addCleanup(thread.join, 30.0)

        viewer = StreamSource(host="127.0.0.1", port=self.port, timeout=10.0)
        self.assertTrue(support.wait_for(lambda: self._try_open(viewer), timeout=10.0))
        self.addCleanup(viewer.stop)
        viewer.start()

        thread.join(timeout=30.0)
        self.assertEqual(result["code"], 0)

        header, written = read_csv(self.csv)
        self.assertEqual(header, list(CSV_COLUMNS) + ["burst"])
        seen_by_viewer = viewer.queue.qsize()
        self.assertGreater(seen_by_viewer, len(written) * 2,
                           "the viewer was decimated too")

    def _try_open(self, source):
        from sources import SourceError

        try:
            source.open()
        except SourceError:
            return False
        return True

    def test_the_burst_column_marks_the_shake(self):
        import csv

        quiet = support.ramp(400, hz=200.0)
        shake = support.ramp(400, hz=200.0, start_seq=400, start_ms=2000, ax_g=1.0)
        recording = self.recording(quiet + shake)

        thread, result = self.capture(
            recording, ["--csv", self.csv, "--log-rate", "2", "--duration", "4"]
        )
        thread.join(timeout=30.0)
        self.assertEqual(result["code"], 0)

        with open(self.csv, newline="") as handle:
            rows = list(csv.reader(handle))
        flags = [row[-1] for row in rows[1:]]
        self.assertIn("1", flags, "the shake produced no burst rows")
        self.assertIn("0", flags, "nothing was written on the steady grid")


class Failures(CaptureFixture):
    def test_a_bad_listen_address_is_an_error_not_a_traceback(self):
        recording = self.recording(support.ramp(200, hz=200.0))
        main.create_source = lambda _args: replay.ReplaySource(recording)
        code = main.main(
            ["--listen", "nonsense:", "--csv", self.csv, "--duration", "1"]
        )
        self.assertEqual(code, 1)

    def test_a_taken_port_is_an_error_not_a_traceback(self):
        from hub import SampleHub

        squatter = SampleHub(port=self.port, banner=lambda: "")
        squatter.start()
        self.addCleanup(squatter.stop)

        recording = self.recording(support.ramp(200, hz=200.0))
        main.create_source = lambda _args: replay.ReplaySource(recording)
        code = main.main([
            "--listen", "127.0.0.1:%d" % self.port,
            "--csv", self.csv, "--duration", "1",
        ])
        self.assertEqual(code, 1)

    @unittest.expectedFailure
    def test_a_csv_that_cannot_be_opened_is_an_error_not_a_traceback(self):
        """A known gap, recorded rather than described.

        Every other start-up failure in main.py -- no board, a bad --listen, a taken port,
        a bad trigger -- prints one line and returns 1. CsvLogger.open() is called outside
        any of that handling, so an unwritable path (a read-only directory, a typo'd mount,
        a full disk) comes out as a traceback instead, with the source already opened and
        no exit code worth acting on. It is the most likely of the lot to happen to a
        long-running capture started from cron.
        """
        recording = self.recording(support.ramp(200, hz=200.0))
        main.create_source = lambda _args: replay.ReplaySource(recording)
        code = main.main([
            "--listen", "127.0.0.1:%d" % self.port,
            "--csv", "/nope/cannot/write.csv", "--duration", "1",
        ])
        self.assertEqual(code, 1)

    def test_a_bad_trigger_is_an_error_not_a_traceback(self):
        recording = self.recording(support.ramp(200, hz=200.0))
        main.create_source = lambda _args: replay.ReplaySource(recording)
        code = main.main([
            "--listen", "127.0.0.1:%d" % self.port,
            "--csv", self.csv, "--log-rate", "5", "--burst-on", "nope:1", "--duration", "1",
        ])
        self.assertEqual(code, 1)


class ReplayHarness(unittest.TestCase):
    """testing/replay.py's own front door."""

    def setUp(self):
        self.directory = tempfile.mkdtemp(prefix="nicla-test-")
        self.addCleanup(shutil.rmtree, self.directory, True)

    def test_no_arguments_prints_usage_and_exits_two(self):
        self.assertEqual(replay.main_([]), 2)

    def test_a_leading_flag_is_treated_as_a_missing_log(self):
        self.assertEqual(replay.main_(["--duration", "1"]), 2)

    def test_a_missing_log_exits_one(self):
        self.assertEqual(replay.main_([os.path.join(self.directory, "nope.csv")]), 1)

    def test_a_log_with_no_usable_rows_is_refused(self):
        path = os.path.join(self.directory, "empty.csv")
        with open(path, "w") as handle:
            handle.write(",".join(CSV_COLUMNS) + "\n")
        self.assertRaises(SystemExit, replay.ReplaySource, path)

    def test_a_recording_loads_as_the_boards_own_columns(self):
        """The logger prepends host_iso and may append burst; what comes back out is the
        27 columns in between, typed as the board sent them."""
        path = os.path.join(self.directory, "log.csv")
        samples = support.ramp(20, ax_g=0.5, gas_ohm=98765)
        with CsvLogger(path) as log:
            for one in samples:
                log.write(one)
        source = replay.ReplaySource(path)
        self.assertEqual(source.rows, samples)

    def test_a_decimated_recording_loads_too(self):
        """A log with the trailing burst column is still a valid thing to replay."""
        path = os.path.join(self.directory, "decimated.csv")
        samples = support.ramp(20, ax_g=0.5)
        with CsvLogger(path, mark_bursts=True) as log:
            for i, one in enumerate(samples):
                log.write(one, 1 if i > 10 else 0)
        self.assertEqual(replay.ReplaySource(path).rows, samples)

    def test_a_torn_final_row_is_skipped_not_fatal(self):
        path = os.path.join(self.directory, "torn.csv")
        samples = support.ramp(20)
        with CsvLogger(path) as log:
            for one in samples:
                log.write(one)
        with open(path, "a") as handle:
            handle.write("2026-08-06T12:00:00.000,1,2,3")
        self.assertEqual(replay.ReplaySource(path).rows, samples)

    def test_the_banner_names_the_file_rather_than_a_port(self):
        """So a dashboard attached to a replay never looks like one attached to hardware."""
        path = os.path.join(self.directory, "log.csv")
        with CsvLogger(path) as log:
            log.write(support.sample())
        banner = replay.ReplaySource(path).describe()
        self.assertIn("Replay(log.csv)", banner)
        self.assertIn("rate_hz=200", banner)
        self.assertIn("columns=%d" % len(COLUMNS), banner)


class ReplayPacing(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.mkdtemp(prefix="nicla-test-")
        self.addCleanup(shutil.rmtree, self.directory, True)

    def test_a_recording_replays_at_the_rate_it_was_recorded(self):
        """From the file's own t_ms, so a decimated log replays with its gaps intact."""
        import time

        path = os.path.join(self.directory, "log.csv")
        with CsvLogger(path) as log:
            for one in support.ramp(400, hz=200.0):   # two seconds
                log.write(one)

        source = replay.ReplaySource(path).open()
        source.start()
        self.addCleanup(source.stop)

        time.sleep(1.0)
        emitted = source.queue.qsize()
        self.assertGreater(emitted, 120, "replaying far slower than 200 Hz")
        self.assertLess(emitted, 320, "replaying far faster than 200 Hz")

    def test_the_replay_loops(self):
        """Each wrap sends t_ms backwards, which is what a board reset looks like -- so a
        short recording both serves indefinitely and exercises that path for free."""
        import time

        path = os.path.join(self.directory, "short.csv")
        with CsvLogger(path) as log:
            for one in support.ramp(40, hz=200.0):    # a fifth of a second
                log.write(one)

        source = replay.ReplaySource(path).open()
        source.start()
        self.addCleanup(source.stop)

        time.sleep(1.0)
        seen = [source.queue.get_nowait() for _ in range(source.queue.qsize())]
        self.assertGreater(len(seen), 40, "the replay stopped at the end of the file")
        times = [one[support.T_MS] for one in seen]
        self.assertTrue(
            any(b < a for a, b in zip(times, times[1:])), "t_ms never went backwards"
        )

class ViewerArguments(unittest.TestCase):
    """What --plot hands its child. The bind address has to make the trip: it is a property
    of the dashboard process rather than of a tab, so unlike window length there is nobody
    downstream who could ask for it instead."""

    def argv_for(self, **kwargs):
        import main
        recorded = []

        class FakePopen(object):
            def __init__(self, argv):
                recorded.append(argv)

        original = main.subprocess.Popen
        main.subprocess.Popen = FakePopen
        try:
            main.launch_viewer("127.0.0.1:8765", 8988, **kwargs)
        finally:
            main.subprocess.Popen = original
        return recorded[0]

    def test_the_default_stays_on_loopback(self):
        argv = self.argv_for()
        self.assertIn("--http-host", argv)
        self.assertEqual(argv[argv.index("--http-host") + 1], "127.0.0.1")

    def test_the_bind_address_reaches_the_child(self):
        argv = self.argv_for(http_host="0.0.0.0")
        self.assertEqual(argv[argv.index("--http-host") + 1], "0.0.0.0")

    def test_every_allowed_name_reaches_the_child(self):
        argv = self.argv_for(allow_hosts=["nicla-01.lan", "nicla-02.lan"])
        pairs = [argv[i + 1] for i, item in enumerate(argv) if item == "--allow-host"]
        self.assertEqual(pairs, ["nicla-01.lan", "nicla-02.lan"])

    def test_no_names_means_no_flags(self):
        self.assertNotIn("--allow-host", self.argv_for())


if __name__ == "__main__":
    unittest.main()
