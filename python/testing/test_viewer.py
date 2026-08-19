"""The viewer's routes, against a real archive on disk."""

import datetime
import json
import os
import shutil
import tempfile
import threading
import unittest
from http.client import HTTPConnection

import support

import config
import viewer

T0 = datetime.datetime(2026, 8, 19, 9, 0, 0)


class ServedArchive(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="nicla-viewer-")
        self.addCleanup(shutil.rmtree, self.root, True)
        self.bench = os.path.join(self.root, "bench")
        os.makedirs(self.bench)
        self.populate()

        self.port = support.free_port()
        self.server = viewer.serve(self.root, port=self.port)
        self.addCleanup(self.server.server_close)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.server.shutdown)

    def populate(self):
        support.restamp(
            support.write_capture(self.bench, T0, minutes=5,
                                  moves=((120.0, 0.6), (240.0, 0.4))),
            T0,
        )

    def get(self, path):
        conn = HTTPConnection("127.0.0.1", self.port, timeout=10.0)
        self.addCleanup(conn.close)
        conn.request("GET", path)
        response = conn.getresponse()
        return response.status, response.read(), response

    def events(self, query=""):
        status, body, _response = self.get("/events" + query)
        self.assertEqual(status, 200, body)
        return json.loads(body.decode("utf-8"))


class Events(ServedArchive):
    def test_the_two_shakes_come_back(self):
        payload = self.events()
        self.assertEqual(payload["count"], 2)
        self.assertEqual(len(payload["events"]), 2)

    def test_an_event_carries_what_the_list_view_needs(self):
        one = self.events()["events"][0]
        for key in ("board", "start", "end", "duration_s", "rows", "peak_g", "peak_dps"):
            self.assertIn(key, one)
        self.assertEqual(one["board"], "bench")
        self.assertGreater(one["rows"], 0)
        self.assertGreater(one["peak_g"], 1.0)

    def test_a_window_narrows_it(self):
        payload = self.events("?from=2026-08-19T09:00:00&to=2026-08-19T09:02:30")
        self.assertEqual(payload["count"], 1)
        self.assertEqual(payload["from"], "2026-08-19T09:00:00")

    def test_a_window_with_nothing_in_it_is_empty_rather_than_missing(self):
        payload = self.events("?from=2026-08-20T00:00:00")
        self.assertEqual(payload["count"], 0)
        self.assertEqual(payload["events"], [])

    def test_a_board_can_be_asked_for(self):
        payload = self.events("?board=bench")
        self.assertEqual(payload["board"], "bench")
        self.assertEqual(payload["count"], 2)

    def test_a_board_that_does_not_exist_is_a_404_not_an_empty_list(self):
        """An empty list would read as 'nothing happened there', which is a different and
        much more misleading answer than 'there is no such board'."""
        status, body, _r = self.get("/events?board=nope")
        self.assertEqual(status, 404)
        self.assertIn(b"no such board", body)

    def test_an_unparseable_time_is_a_400_with_the_reason(self):
        status, body, _r = self.get("/events?from=tuesday")
        self.assertEqual(status, 400)
        self.assertIn(b"bad request", body)

    def test_events_are_oldest_first(self):
        starts = [one["start"] for one in self.events()["events"]]
        self.assertEqual(starts, sorted(starts))


class Range(ServedArchive):
    def range(self, query=""):
        status, body, _r = self.get("/range" + query)
        self.assertEqual(status, 200, body)
        return json.loads(body.decode("utf-8"))

    def test_it_returns_parallel_arrays(self):
        payload = self.range()
        self.assertEqual(len(payload["t"]), payload["buckets"])
        for column in payload["columns"].values():
            self.assertEqual(len(column["min"]), len(payload["t"]))
            self.assertEqual(len(column["max"]), len(payload["t"]))
        self.assertEqual(len(payload["burst"]), len(payload["t"]))

    def test_a_narrow_width_forces_an_envelope(self):
        payload = self.range("?width=20")
        self.assertLessEqual(payload["buckets"], 20)
        self.assertTrue(payload["downsampled"])

    def test_a_burst_window_at_full_width_is_not_downsampled(self):
        """Zooming into one episode is the detail mode: few enough rows that every one gets
        its own bucket, so min == max and the band draws as a line."""
        payload = self.range("?from=2026-08-19T09:01:59&to=2026-08-19T09:02:01&width=900")
        self.assertGreater(payload["rows"], 0)
        self.assertFalse(payload["downsampled"])
        column = payload["columns"]["ax_g"]
        self.assertEqual(column["min"], column["max"])

    def test_the_burst_flag_survives_bucketing(self):
        payload = self.range("?width=60")
        self.assertGreater(sum(payload["burst"]), 0)

    def test_a_window_with_nothing_in_it_is_empty_not_broken(self):
        payload = self.range("?from=2026-08-20T00:00:00&to=2026-08-20T01:00:00")
        self.assertEqual(payload["buckets"], 0)
        self.assertEqual(payload["t"], [])

    def test_columns_can_be_asked_for(self):
        payload = self.range("?columns=temp_C")
        self.assertEqual(list(payload["columns"]), ["temp_C"])

    def test_an_unknown_board_is_a_404(self):
        status, body, _r = self.get("/range?board=nope")
        self.assertEqual(status, 404)
        self.assertIn(b"no such board", body)

    def test_a_bad_width_is_a_400(self):
        status, _body, _r = self.get("/range?width=wide")
        self.assertEqual(status, 400)


class Spec(ServedArchive):
    def test_the_tiles_come_from_tiles_py(self):
        """Served rather than restated, which is what stops the layout existing twice."""
        status, body, _r = self.get("/spec")
        self.assertEqual(status, 200)
        spec = json.loads(body.decode("utf-8"))
        import tiles
        self.assertEqual(len(spec["tiles"]), len(tiles.TILES))
        self.assertIn("palettes", spec)

    def test_it_names_the_boards_and_says_it_is_not_live(self):
        _status, body, _r = self.get("/spec")
        spec = json.loads(body.decode("utf-8"))
        self.assertEqual(spec["boards"], ["bench"])
        self.assertFalse(spec["live"])


class Page(ServedArchive):
    def test_the_root_serves_the_page(self):
        status, body, response = self.get("/")
        self.assertEqual(status, 200)
        self.assertIn("text/html", response.getheader("Content-Type"))
        self.assertIn(b"history.js", body)

    def test_the_assets_are_reachable(self):
        for name in ("/history.js", "/history.css", "/uPlot.iife.min.js"):
            status, body, _r = self.get(name)
            self.assertEqual(status, 200, name)
            self.assertTrue(body)

    def test_traversal_gets_nowhere(self):
        """basename() is the whole defence, exactly as in webhub."""
        for path in ("/../main.py", "/../../etc/passwd", "/..%2fmain.py"):
            status, _body, _r = self.get(path)
            self.assertEqual(status, 404, path)

    def test_the_archive_summary_moved_but_still_exists(self):
        status, body, _r = self.get("/archive")
        self.assertEqual(status, 200)
        self.assertIn(b"bench", body)


class Summary(ServedArchive):
    def test_the_archive_route_says_what_is_in_it(self):
        status, body, _r = self.get("/archive")
        self.assertEqual(status, 200)
        text = body.decode("utf-8")
        self.assertIn("bench", text)
        self.assertIn("capture(s)", text)

    def test_an_unknown_route_is_a_404(self):
        status, body, _r = self.get("/nope")
        self.assertEqual(status, 404)
        self.assertIn(b"no such route", body)


class Headers(ServedArchive):
    def test_responses_are_not_cached(self):
        """The archive changes under the page on every sync."""
        _status, _body, response = self.get("/events")
        self.assertEqual(response.getheader("Cache-Control"), "no-store")

    def test_the_hardening_headers_are_set(self):
        """Cheap, and absent from webhub.py -- no reason to repeat that here."""
        _status, _body, response = self.get("/events")
        self.assertEqual(response.getheader("X-Content-Type-Options"), "nosniff")
        self.assertEqual(response.getheader("X-Frame-Options"), "DENY")


class EmptyArchive(unittest.TestCase):
    """The state everything is in until the first pull lands."""

    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="nicla-viewer-empty-")
        self.addCleanup(shutil.rmtree, self.root, True)
        self.port = support.free_port()
        self.server = viewer.serve(self.root, port=self.port)
        self.addCleanup(self.server.server_close)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.shutdown)

    def get(self, path):
        conn = HTTPConnection("127.0.0.1", self.port, timeout=10.0)
        self.addCleanup(conn.close)
        conn.request("GET", path)
        response = conn.getresponse()
        return response.status, response.read()

    def test_it_serves_rather_than_failing(self):
        status, body = self.get("/archive")
        self.assertEqual(status, 200)
        self.assertIn(b"no boards yet", body)

    def test_events_is_empty_not_broken(self):
        status, body = self.get("/events")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body.decode("utf-8"))["count"], 0)


class Launching(unittest.TestCase):
    """What the double-click has to work through.

    The launcher runs one fixed command, so everything site-specific lives in viewer.conf
    and nobody has to edit a script to point it at a different share.
    """

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="nicla-conf-")
        self.addCleanup(shutil.rmtree, self.dir, True)

    def conf(self, text):
        path = os.path.join(self.dir, "viewer.conf")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(text)
        return path

    def test_the_archive_can_come_from_the_file(self):
        args = viewer.parse_args(["--config", self.conf("archive = /srv/logs\n")])
        self.assertEqual(args.archive, "/srv/logs")

    def test_a_unc_path_survives_the_file(self):
        r"""Backslashes are the whole point of the path and must not be eaten."""
        unc = r"\\fileserver\NiclaLogs"
        path = self.conf("archive = " + unc + "\n")
        self.assertEqual(viewer.parse_args(["--config", path]).archive, unc)

    def test_the_command_line_still_wins(self):
        path = self.conf("archive = /srv/logs\nhttp_port = 8990\n")
        args = viewer.parse_args(["--config", path, "--http-port", "9100"])
        self.assertEqual(args.http_port, 9100)

    def test_no_archive_anywhere_is_a_usage_error_not_a_traceback(self):
        with self.assertRaises(SystemExit) as caught:
            viewer.parse_args([])
        self.assertEqual(caught.exception.code, 2)

    def test_open_is_available_for_the_launcher(self):
        args = viewer.parse_args(["--archive", self.dir, "--open"])
        self.assertTrue(args.open)


class Launcher(unittest.TestCase):
    """The .cmd, read as text. It is Windows and cannot run here."""

    def setUp(self):
        with open(os.path.join(support.PYTHON_DIR, "viewer.cmd"), encoding="utf-8") as handle:
            self.cmd = handle.read()

    def test_it_uses_pushd_for_its_own_directory(self):
        r"""cmd cannot hold a UNC path as a working directory -- it warns and leaves you in
        C:\Windows, where neither viewer.py nor viewer.conf is. pushd maps a drive letter,
        which is what lets the launcher live on the share beside the logs."""
        self.assertIn('pushd "%~dp0"', self.cmd)
        self.assertNotIn('cd /d "%~dp0"', self.cmd)
        self.assertIn("popd", self.cmd)

    def test_every_failure_pauses(self):
        """A window that vanishes takes its error message with it, and the user is left with
        a double-click that does nothing at all."""
        for label in (":noshare", ":nopython", ":failed"):
            tail = self.cmd.split(label, 1)[1]
            self.assertIn("pause", tail.split("exit /b", 1)[0], label)

    def test_it_reads_the_config_rather_than_hardcoding_a_path(self):
        self.assertIn("--config viewer.conf", self.cmd)
        self.assertIn("--open", self.cmd)

    def test_the_example_config_parses(self):
        """It is what everyone copies, so a key that is not a flag would be found by the
        first person to try it rather than here."""
        example = os.path.join(support.PYTHON_DIR, "viewer.conf.example")
        values = config.load(example, viewer.build_parser())
        self.assertIn("archive", values)
        self.assertEqual(values["http_port"], 8990)


class BindsLoopbackOnly(unittest.TestCase):
    def test_the_default_host_is_loopback(self):
        """There is no authentication in viewer.py and there is not meant to be -- exposure
        is a reverse proxy's job, with AD doing the asking."""
        import inspect
        signature = inspect.signature(viewer.serve)
        self.assertEqual(signature.parameters["host"].default, "127.0.0.1")


if __name__ == "__main__":
    unittest.main()
