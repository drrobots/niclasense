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


class Summary(ServedArchive):
    def test_the_root_says_what_is_in_the_archive(self):
        status, body, _r = self.get("/")
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
        status, body = self.get("/")
        self.assertEqual(status, 200)
        self.assertIn(b"no boards yet", body)

    def test_events_is_empty_not_broken(self):
        status, body = self.get("/events")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body.decode("utf-8"))["count"], 0)


class BindsLoopbackOnly(unittest.TestCase):
    def test_the_default_host_is_loopback(self):
        """There is no authentication in viewer.py and there is not meant to be -- exposure
        is a reverse proxy's job, with AD doing the asking."""
        import inspect
        signature = inspect.signature(viewer.serve)
        self.assertEqual(signature.parameters["host"].default, "127.0.0.1")


if __name__ == "__main__":
    unittest.main()
