"""The browser server: the spec it hands out, its routes, and its event stream.

The claim webhub.py makes is that the layout lives in exactly one place -- tiles.py -- and
the browser is handed it rather than holding a second copy. That claim is only worth
anything if /spec really does carry everything the client needs, so the spec is checked
against tiles.py directly rather than against a fixture.

The routes are checked over real HTTP, including the two that are security-shaped: only
files that exist in web/ with a known extension are served, and a path with .. in it
resolves to nothing. And the event stream is checked for the property it exists for --
rows arrive batched into a few events a second, not one event per sample.
"""

import json
import socket
import unittest
from http.client import HTTPConnection

import support
import tiles
from columns import COLUMNS
from webhub import MAX_BATCH, PRIME_ROWS, WebHub, build_spec


class Spec(unittest.TestCase):
    def setUp(self):
        self.spec = build_spec(sample_hz=200.0, source="serial /dev/x @ 1000000")

    def test_it_is_json_serialisable(self):
        """It is served as JSON, so a tuple or a frozenset in tiles.py has to have been
        converted here rather than blowing up on the first request."""
        json.loads(json.dumps(self.spec))

    def test_every_tile_is_described(self):
        self.assertEqual(
            [tile["name"] for tile in self.spec["tiles"]],
            [tile["name"] for tile in tiles.TILES],
        )

    def test_a_tile_carries_what_the_client_needs_to_draw_it(self):
        for described, declared in zip(self.spec["tiles"], tiles.TILES):
            self.assertEqual(described["min_span"], declared["min_span"])
            self.assertEqual(described["placement"], list(tiles.PLACEMENT[declared["name"]]))
            self.assertEqual(
                [s["column"] for s in described["series"]],
                [column for column, _l, _c in declared["series"]],
            )
            self.assertEqual(
                [s["colour"] for s in described["series"]],
                [colour for _c, _l, colour in declared["series"]],
            )

    def test_the_bsec_tiles_are_flagged(self):
        flagged = set(t["name"] for t in self.spec["tiles"] if t["bsec"])
        self.assertEqual(flagged, set(tiles.BSEC_TILES))

    def test_accuracy_notes_are_keyed_by_string(self):
        """JSON has no integer keys, and the client indexes with String(acc)."""
        self.assertEqual(set(self.spec["accuracy_notes"]), set("0123"))

    def test_the_columns_are_the_wire_columns_in_order(self):
        self.assertEqual(self.spec["columns"], list(COLUMNS))

    def test_both_palettes_are_served(self):
        self.assertEqual(set(self.spec["palettes"]), set(("dark", "light")))
        self.assertEqual(self.spec["light_overrides"], tiles.LIGHT_OVERRIDES)

    def test_the_banner_facts_come_through(self):
        self.assertEqual(self.spec["sample_hz"], 200.0)
        self.assertIn("serial", self.spec["source"])


class ServerFixture(unittest.TestCase):
    def setUp(self):
        self.port = support.free_port()
        self.hub = WebHub(host="127.0.0.1", port=self.port)
        self.hub.start()
        self.addCleanup(self.hub.stop)

    def get(self, path):
        conn = HTTPConnection("127.0.0.1", self.port, timeout=5.0)
        self.addCleanup(conn.close)
        conn.request("GET", path)
        response = conn.getresponse()
        return response.status, response.read()

    def open_stream(self):
        """A raw socket on /stream, since http.client wants to read to the end."""
        sock = socket.create_connection(("127.0.0.1", self.port), timeout=5.0)
        self.addCleanup(sock.close)
        sock.sendall(
            b"GET /stream HTTP/1.1\r\nHost: 127.0.0.1\r\nAccept: text/event-stream\r\n\r\n"
        )
        buffer = b""
        while b"\r\n\r\n" not in buffer:
            buffer += sock.recv(4096)
        headers, _sep, rest = buffer.partition(b"\r\n\r\n")
        self.assertIn(b"text/event-stream", headers)
        support.wait_for(lambda: self.hub.clients >= 1)
        return sock, rest

    def read_for(self, sock, seconds):
        import time

        sock.settimeout(0.2)
        deadline = time.time() + seconds
        buffer = b""
        while time.time() < deadline:
            try:
                chunk = sock.recv(65536)
            except socket.timeout:
                continue
            if not chunk:
                break
            buffer += chunk
        return buffer


class Routes(ServerFixture):
    def test_the_page_is_served_at_both_of_its_names(self):
        for path in ("/", "/index.html"):
            status, body = self.get(path)
            self.assertEqual(status, 200, path)
            self.assertIn(b"<", body)

    def test_the_spec_route_serves_the_spec(self):
        status, body = self.get("/spec")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body.decode("utf-8"))["columns"], list(COLUMNS))

    def test_the_client_files_are_served(self):
        for path in ("/app.js", "/dash.css", "/uPlot.iife.min.js", "/uPlot.min.css"):
            status, _body = self.get(path)
            self.assertEqual(status, 200, path)

    def test_a_query_string_does_not_confuse_the_router(self):
        self.assertEqual(self.get("/spec?cachebust=1")[0], 200)

    def test_an_unknown_file_is_a_404(self):
        self.assertEqual(self.get("/nope.js")[0], 404)

    def test_an_unknown_extension_is_a_404(self):
        """Only html, css and js are servable, so a stray file in web/ is not reachable."""
        self.assertEqual(self.get("/app.py")[0], 404)

    def test_traversal_gets_nowhere(self):
        for path in (
            "/../main.py",
            "/../../etc/passwd",
            "/%2e%2e/main.py",
            "/....//main.py",
        ):
            status, _body = self.get(path)
            self.assertEqual(status, 404, path)


class Stream(ServerFixture):
    def test_a_tab_opening_mid_capture_is_handed_the_backlog(self):
        """So it draws a populated dashboard instead of thirty seconds of blank tiles."""
        for one in support.ramp(500, ax_g=0.5):
            self.hub.broadcast(one)
        sock, rest = self.open_stream()
        body = rest + self.read_for(sock, 1.0)
        self.assertEqual(body.count(b"data: "), 500)

    def test_the_backlog_is_capped(self):
        for one in support.ramp(PRIME_ROWS + 500):
            self.hub.broadcast(one)
        self.assertEqual(len(self.hub._prime), PRIME_ROWS)
        # And it is the newest that survives, not the oldest.
        self.assertIn("%d," % (PRIME_ROWS + 499), self.hub._prime[-1])

    def test_rows_arrive_batched_rather_than_one_event_each(self):
        """The reason for FLUSH_INTERVAL: at 200 Hz a per-sample event would give the
        browser two hundred callbacks a second for a dashboard that redraws twenty times."""
        sock, rest = self.open_stream()
        self.read_for(sock, 0.2)

        import threading
        import time

        def feed():
            for i in range(200):
                self.hub.broadcast(support.sample(seq=i, t_ms=i * 5))
                time.sleep(0.005)

        thread = threading.Thread(target=feed)
        thread.start()
        body = rest + self.read_for(sock, 1.6)
        thread.join()

        rows = body.count(b"data: ")
        events = body.count(b"\n\n")
        self.assertGreaterEqual(rows, 150)
        self.assertLess(events, rows / 3, "%d events for %d rows" % (events, rows))

    def test_no_event_exceeds_the_batch_limit(self):
        for one in support.ramp(PRIME_ROWS):
            self.hub.broadcast(one)
        sock, rest = self.open_stream()
        body = (rest + self.read_for(sock, 1.5)).decode("ascii")
        for block in body.split("\n\n"):
            self.assertLessEqual(block.count("data: "), MAX_BATCH)

    def test_status_arrives_as_a_named_event(self):
        sock, rest = self.open_stream()
        self.hub.push_status({"rows": 41231, "csv": "logs/x.csv"})
        body = (rest + self.read_for(sock, 0.8)).decode("ascii")
        self.assertIn("event: status", body)
        self.assertIn('"rows":41231', body)

    def test_a_tab_opening_later_gets_the_last_status_immediately(self):
        """Otherwise the capture tile sits empty for up to a second after every reload."""
        self.hub.push_status({"rows": 7})
        sock, rest = self.open_stream()
        body = (rest + self.read_for(sock, 0.5)).decode("ascii")
        self.assertIn("event: status", body)
        self.assertIn('"rows":7', body)

    def test_the_end_of_a_capture_is_announced(self):
        """So an open page says the capture ended rather than freezing."""
        sock, rest = self.open_stream()
        self.hub.push_event("ended", {"reason": "stopped"})
        body = (rest + self.read_for(sock, 0.8)).decode("ascii")
        self.assertIn("event: ended", body)

    def test_a_closed_tab_is_forgotten(self):
        sock, _rest = self.open_stream()
        self.assertEqual(self.hub.clients, 1)
        sock.close()
        self.assertTrue(support.wait_for(
            lambda: self._poke() or self.hub.clients == 0, timeout=5.0, interval=0.05
        ))

    def _poke(self):
        self.hub.broadcast(support.sample())
        return False


class PortInUse(unittest.TestCase):
    def test_a_second_dashboard_says_what_is_wrong(self):
        port = support.free_port()
        first = WebHub(port=port)
        first.start()
        self.addCleanup(first.stop)

        second = WebHub(port=port)
        try:
            second.start()
        except OSError as exc:
            message = str(exc)
        else:
            second.stop()
            self.fail("two servers bound the same port")
        self.assertIn(str(port), message)
        self.assertIn("--http-port", message)


class Binding(unittest.TestCase):
    def test_the_default_is_loopback(self):
        """Deliberate and not configurable: this serves an unauthenticated live feed of
        someone's sensor data, and a --bind flag is an invitation to put it on a network."""
        self.assertEqual(WebHub().host, "127.0.0.1")


if __name__ == "__main__":
    unittest.main()
