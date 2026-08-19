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
from webhub import (MAX_BATCH, PRIME_ROWS, WebHub, build_spec, host_allowed,
                    host_header_name, is_address_literal)


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

    def test_an_alternative_unit_reaches_the_client(self):
        """The units toggle is built from /spec alone -- it appears only because a tile
        declares an alt_unit, and is labelled from it -- so a field dropped here is a
        control that silently stops existing."""
        described = dict(
            (tile["name"], tile) for tile in self.spec["tiles"]
        )
        for declared in tiles.TILES:
            alt = declared.get("alt_unit")
            served = described[declared["name"]]["alt_unit"]
            if alt is None:
                self.assertIsNone(served)
                continue
            self.assertEqual(served, dict(alt))
            for field in ("unit", "mul", "add", "min_span"):
                self.assertIn(field, served)

    def test_the_alternative_unit_is_a_copy(self):
        """It is about to be JSON, and handing the client a reference to a module-level
        mapping it then owns is the kind of thing that stays harmless until it does not."""
        for name, tile in ((t["name"], t) for t in tiles.TILES):
            if tile.get("alt_unit") is None:
                continue
            served = [t for t in self.spec["tiles"] if t["name"] == name][0]
            self.assertIsNot(served["alt_unit"], tile["alt_unit"])

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

    def read_until(self, sock, enough, timeout=20.0):
        """Read until `enough(buffer)`, or give up after `timeout` and return what came.

        The difference from read_for is what the number means. A fixed duration asserts
        something about the machine -- that it can produce and deliver N rows in 1.6
        seconds -- and a shared CI runner cannot be relied on for that; the first Windows
        and macOS runs of this suite failed here, having managed 51 rows where the test
        wanted 150. A generous timeout with a predicate asserts something about the code
        instead, and costs nothing extra when the machine is quick, which is the only time
        the duration form was actually passing on purpose.
        """
        import time

        sock.settimeout(0.2)
        deadline = time.time() + timeout
        buffer = b""
        while time.time() < deadline and not enough(buffer):
            try:
                chunk = sock.recv(65536)
            except socket.timeout:
                continue
            if not chunk:
                break
            buffer += chunk
        return buffer

    def rows_in(self, count):
        return lambda buffer: buffer.count(b"data: ") >= count

    def contains(self, marker):
        return lambda buffer: marker in buffer


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
        body = rest + self.read_until(sock, self.rows_in(500))
        self.assertEqual(body.count(b"data: "), 500)

    def test_the_backlog_is_capped(self):
        for one in support.ramp(PRIME_ROWS + 500):
            self.hub.broadcast(one)
        self.assertEqual(len(self.hub._prime), PRIME_ROWS)
        # And it is the newest that survives, not the oldest.
        self.assertIn("%d," % (PRIME_ROWS + 499), self.hub._prime[-1])

    def test_rows_arrive_batched_rather_than_one_event_each(self):
        """The reason for FLUSH_INTERVAL: at 200 Hz a per-sample event would give the
        browser two hundred callbacks a second for a dashboard that redraws twenty times.

        The feed used to be paced with a 5 ms sleep, to imitate a board at 200 Hz over a
        second. That made the test a statement about the machine as much as about the code,
        and it failed on the first CI run it ever saw: a loaded runner stretched each 5 ms
        sleep to about 30, so only 51 rows were fed inside the window. Worse, it would have
        failed in the other direction too -- once the gap between samples exceeds
        FLUSH_INTERVAL, every sample honestly does get its own event, and the ratio this
        asserts would be measuring the runner's scheduler rather than the batching.

        So the rows go in as fast as they will go. That is also closer to the truth than the
        even pacing was: the CMSIS-DAP bridge buffers, so samples reach the host in bunches
        (see the README on adaptive logging), and coalescing a bunch into one event is
        exactly the property being claimed.
        """
        sock, rest = self.open_stream()
        fed = 200
        for i in range(fed):
            self.hub.broadcast(support.sample(seq=i, t_ms=i * 5))

        body = rest + self.read_until(sock, self.rows_in(fed))
        rows = body.count(b"data: ")
        events = body.count(b"\n\n")
        self.assertEqual(rows, fed)
        self.assertLess(events, rows / 3, "%d events for %d rows" % (events, rows))

    def test_no_event_exceeds_the_batch_limit(self):
        for one in support.ramp(PRIME_ROWS):
            self.hub.broadcast(one)
        sock, rest = self.open_stream()
        body = (rest + self.read_until(sock, self.rows_in(PRIME_ROWS))).decode("ascii")
        for block in body.split("\n\n"):
            self.assertLessEqual(block.count("data: "), MAX_BATCH)

    def test_status_arrives_as_a_named_event(self):
        sock, rest = self.open_stream()
        self.hub.push_status({"rows": 41231, "csv": "logs/x.csv"})
        body = (rest + self.read_until(sock, self.contains(b"event: status"))).decode("ascii")
        self.assertIn("event: status", body)
        self.assertIn('"rows":41231', body)

    def test_a_tab_opening_later_gets_the_last_status_immediately(self):
        """Otherwise the capture tile sits empty for up to a second after every reload."""
        self.hub.push_status({"rows": 7})
        sock, rest = self.open_stream()
        body = (rest + self.read_until(sock, self.contains(b"event: status"))).decode("ascii")
        self.assertIn("event: status", body)
        self.assertIn('"rows":7', body)

    def test_the_end_of_a_capture_is_announced(self):
        """So an open page says the capture ended rather than freezing."""
        sock, rest = self.open_stream()
        self.hub.push_event("ended", {"reason": "stopped"})
        body = (rest + self.read_until(sock, self.contains(b"event: ended"))).decode("ascii")
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
        """The default is the security model. --http-host can move it, because reaching a
        dashboard from another machine is a real need, but nothing here asks who is asking,
        so staying on loopback until somebody says otherwise is the whole protection."""
        self.assertEqual(WebHub().host, "127.0.0.1")
        self.assertFalse(WebHub().public)

    def test_an_explicit_address_is_honoured(self):
        hub = WebHub(host="0.0.0.0")
        self.assertEqual(hub.host, "0.0.0.0")
        self.assertTrue(hub.public)

    def test_a_wildcard_bind_reports_an_address_a_browser_can_use(self):
        """Nobody can open http://0.0.0.0/, and the point of binding it was that someone
        elsewhere would open it, so url() has to answer with a routable address."""
        url = WebHub(host="0.0.0.0", port=8988).url()
        self.assertNotIn("0.0.0.0", url)
        self.assertTrue(url.endswith(":8988/"))

    def test_loopback_still_reports_itself(self):
        self.assertEqual(WebHub(port=8988).url(), "http://127.0.0.1:8988/")


class HostHeaderParsing(unittest.TestCase):
    def test_the_port_is_dropped_and_the_name_lowercased(self):
        self.assertEqual(host_header_name("Nicla-01.LAN:8988"), "nicla-01.lan")

    def test_an_ipv6_literal_survives_its_brackets(self):
        """Splitting a bracketed literal on ":" the way a name is split returns "[", which
        would then match nothing and lock out a legitimately bound IPv6 dashboard."""
        self.assertEqual(host_header_name("[::1]:8988"), "::1")
        self.assertEqual(host_header_name("[fe80::1]"), "fe80::1")

    def test_missing_or_malformed_headers_yield_nothing(self):
        self.assertEqual(host_header_name(None), "")
        self.assertEqual(host_header_name(""), "")
        self.assertEqual(host_header_name("[::1"), "")

    def test_address_literals_are_recognised(self):
        for value in ("127.0.0.1", "192.168.1.5", "::1", "fe80::1"):
            self.assertTrue(is_address_literal(value), value)
        for value in ("localhost", "nicla-01.lan", "evil.example.com", ""):
            self.assertFalse(is_address_literal(value), value)


class HostAllowlist(unittest.TestCase):
    """The DNS-rebinding guard. Rebinding needs a *name* to re-resolve, so the rule is that
    addresses are always fine and names have to have been asked for."""

    def test_addresses_and_localhost_are_always_allowed(self):
        for value in ("127.0.0.1:8988", "192.168.1.5:8988", "[::1]:8988", "localhost:8988"):
            self.assertTrue(host_allowed(value), value)

    def test_an_unknown_name_is_refused(self):
        self.assertFalse(host_allowed("evil.example.com"))
        self.assertFalse(host_allowed("nicla-01.lan"))

    def test_a_named_host_is_allowed_once_asked_for(self):
        self.assertTrue(host_allowed("nicla-01.lan:8988", {"nicla-01.lan"}))

    def test_a_missing_header_is_refused(self):
        self.assertFalse(host_allowed(None))
        self.assertFalse(host_allowed(""))

    def test_names_are_normalised_when_the_hub_takes_them(self):
        """They arrive from a command line and an INI file, so case and stray whitespace
        are the caller's habits rather than anything to hold against them."""
        hub = WebHub(allowed_hosts=[" Nicla-01.LAN ", "", "  "])
        self.assertEqual(hub.allowed_hosts, frozenset(["nicla-01.lan"]))


class HostEnforcement(ServerFixture):
    def _get_with_host(self, host_header, path="/spec"):
        conn = HTTPConnection("127.0.0.1", self.port, timeout=5.0)
        self.addCleanup(conn.close)
        conn.putrequest("GET", path, skip_host=True)
        conn.putheader("Host", host_header)
        conn.endheaders()
        response = conn.getresponse()
        return response.status, response.read()

    def test_a_rebound_name_is_refused_on_every_route(self):
        for path in ("/", "/spec", "/stream", "/app.js"):
            status, _body = self._get_with_host("evil.example.com", path)
            self.assertEqual(status, 403, path)

    def test_the_refusal_says_how_to_fix_it(self):
        """Whoever meets this is far more likely to be the person who put a name in front
        of the dashboard than an attacker, and a bare 403 tells them nothing."""
        _status, body = self._get_with_host("nicla-01.lan")
        self.assertIn(b"--allow-host nicla-01.lan", body)

    def test_an_address_is_served_as_before(self):
        status, _body = self._get_with_host("127.0.0.1:%d" % self.port)
        self.assertEqual(status, 200)


if __name__ == "__main__":
    unittest.main()
