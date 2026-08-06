"""The socket between the capture and its viewers, over a real socket.

Everything here binds a loopback port and speaks TCP, because the parts worth checking are
exactly the parts a fake would paper over: that a viewer is handed a schema line and a
banner before any data, that a sample put in one end comes out the other as the same
tuple, that a viewer going away is a routine event rather than an error, and that a second
capture on a taken port says so in a sentence instead of a traceback.

The counterpart to the byte-level checks in test_wire.py: those prove the format is a
closed loop, these prove the loop is actually plumbed.
"""

import socket
import unittest

import support
from hub import CLIENT_QUEUE, SampleHub, _Client, format_sample
from sources import SourceError, StreamSource


class HubFixture(unittest.TestCase):
    """A hub on a free port, torn down whatever the test does."""

    banner = "nicla-stream: v3 rate_hz=200 baud=1000000 columns=27"

    def setUp(self):
        self.port = support.free_port()
        self.hub = SampleHub(host="127.0.0.1", port=self.port, banner=lambda: self.banner)
        self.hub.start()
        self.addCleanup(self.hub.stop)
        self._sockets = []

    def raw_client(self):
        """A bare socket attached to the hub, as `nc` would be."""
        sock = socket.create_connection(("127.0.0.1", self.port), timeout=5.0)
        sock.settimeout(5.0)
        self._sockets.append(sock)
        self.addCleanup(sock.close)
        support.wait_for(lambda: self.hub.clients >= len(self._sockets))
        return sock

    def read_lines(self, sock, count):
        """Exactly `count` complete lines, however the kernel chose to split them."""
        buffer = b""
        while buffer.count(b"\n") < count:
            chunk = sock.recv(4096)
            if not chunk:
                self.fail("hub closed the connection after %r" % buffer)
            buffer += chunk
        return buffer.decode("ascii").split("\n")[:count]

    def attached_source(self):
        source = StreamSource(host="127.0.0.1", port=self.port)
        source.open()
        self.addCleanup(source.stop)
        source.start()
        support.wait_for(lambda: self.hub.clients >= 1)
        return source


class Handshake(HubFixture):
    def test_a_viewer_gets_the_schema_and_banner_before_any_data(self):
        """In that order, and before the first row -- which is what lets the far end
        reject a schema mismatch instead of reporting a wall of malformed rows."""
        sock = self.raw_client()
        schema, banner = self.read_lines(sock, 2)
        self.assertEqual(schema, "#" + ",".join(support.COLUMNS))
        self.assertEqual(banner, "# " + self.banner)

    def test_the_banner_is_asked_for_per_connection(self):
        """A viewer attaching after a rate change should be told the rate now in force,
        not the one that was in force when the capture started."""
        self.read_lines(self.raw_client(), 2)
        self.banner = "nicla-stream: v3 rate_hz=50 baud=1000000 columns=27"
        _schema, banner = self.read_lines(self.raw_client(), 2)
        self.assertIn("rate_hz=50", banner)


class Fanout(HubFixture):
    def test_a_broadcast_sample_reaches_a_raw_client_verbatim(self):
        sock = self.raw_client()
        self.read_lines(sock, 2)
        one = support.sample(seq=9, t_ms=45, ax_g=0.5, gas_ohm=98765)
        self.hub.broadcast(one)
        self.assertEqual(self.read_lines(sock, 1)[0], format_sample(one))

    def test_every_viewer_gets_every_sample(self):
        socks = [self.raw_client() for _ in range(3)]
        for sock in socks:
            self.read_lines(sock, 2)
        samples = support.ramp(20, ax_g=0.25)
        for one in samples:
            self.hub.broadcast(one)
        for sock in socks:
            self.assertEqual(
                self.read_lines(sock, len(samples)),
                [format_sample(one) for one in samples],
            )

    def test_broadcasting_with_nobody_attached_is_free(self):
        for one in support.ramp(100):
            self.hub.broadcast(one)
        self.assertEqual(self.hub.clients, 0)

    def test_status_rides_the_same_connection_as_a_comment(self):
        """So a reader that only wants samples skips it exactly as it skips the banner."""
        source = self.attached_source()
        self.hub.push_status({"rows": 41231, "csv": "logs/x.csv", "bursting": False})
        support.wait_for(lambda: source.status.get("rows") == 41231)
        self.assertEqual(source.status["csv"], "logs/x.csv")
        self.assertEqual(source.malformed, 0, "status was parsed as a data row")


class AttachedViewer(HubFixture):
    """StreamSource against a real hub: the path webdash.py takes."""

    def test_samples_arrive_as_the_tuples_that_went_in(self):
        source = self.attached_source()
        samples = support.ramp(50, ax_g=0.5, temp_C=21.25, co2_eq_ppm=500)
        for one in samples:
            self.hub.broadcast(one)
        support.wait_for(lambda: source.queue.qsize() >= len(samples))
        received = [source.queue.get_nowait() for _ in range(len(samples))]
        self.assertEqual(received, samples)
        self.assertEqual(source.malformed, 0)
        self.assertEqual(source.dropped, 0)

    def test_the_banner_is_picked_up_at_connect(self):
        source = self.attached_source()
        self.assertEqual(source.stream_hz, 200)
        self.assertEqual(source.reported_baud, 1000000)
        self.assertIn("127.0.0.1:%d" % self.port, source.describe())

    def test_a_capture_that_ends_is_reported_rather_than_a_freeze(self):
        source = self.attached_source()
        self.hub.stop()
        self.assertTrue(support.wait_for(lambda: source.error is not None))
        self.assertIn("closed the connection", str(source.error))

    def test_detaching_leaves_the_capture_alone(self):
        source = self.attached_source()
        self.assertEqual(self.hub.clients, 1)
        source.stop()
        # Still serving: another viewer can attach to the same capture.
        self.assertIsNotNone(self.attached_source())

    def test_a_departed_viewer_is_forgotten_once_traffic_resumes(self):
        """Not the moment it leaves, which is worth stating rather than assuming.

        A client's writer thread only learns the socket is gone by writing to it, so with
        the stream idle the count stays where it was. In a real capture something is
        always going out -- samples at 200 Hz, and push_status once a second even when the
        board has gone quiet -- so the staleness is bounded by that second. It is only
        visible here because a test can hold a hub perfectly silent.
        """
        source = self.attached_source()
        source.stop()
        self.assertEqual(self.hub.clients, 1)
        self.assertTrue(support.wait_for(
            lambda: self._poke() or self.hub.clients == 0, timeout=5.0, interval=0.05
        ))

    def _poke(self):
        self.hub.broadcast(support.sample())
        return False


class AttachFailures(unittest.TestCase):
    def test_nothing_listening_names_the_address_and_the_remedy(self):
        port = support.free_port()
        source = StreamSource(host="127.0.0.1", port=port, timeout=1.0)
        try:
            source.open()
        except SourceError as exc:
            message = str(exc)
        else:
            source.stop()
            self.fail("attached to a port with nothing on it")
        self.assertIn(str(port), message)
        self.assertIn("main.py", message)

    def test_a_server_that_is_not_a_capture_is_refused(self):
        """Rather than being parsed as an endless stream of malformed rows."""
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        self.addCleanup(listener.close)
        port = listener.getsockname()[1]

        import threading

        def serve():
            conn, _address = listener.accept()
            try:
                conn.sendall(b"HTTP/1.1 200 OK\r\n\r\nhello\n")
                # Held open so the refusal comes from the missing header, not from EOF.
                support.wait_for(lambda: False, timeout=2.0)
            finally:
                conn.close()

        thread = threading.Thread(target=serve, daemon=True)
        thread.start()

        source = StreamSource(host="127.0.0.1", port=port, timeout=1.0)
        self.assertRaises(SourceError, source.open)

    def test_a_schema_mismatch_is_refused_by_name(self):
        wrong = support.COLUMNS[:-1]
        hub_port = support.free_port()

        listener = socket.socket()
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind(("127.0.0.1", hub_port))
        listener.listen(1)
        self.addCleanup(listener.close)

        import threading

        def serve():
            conn, _address = listener.accept()
            try:
                conn.sendall(("#" + ",".join(wrong) + "\n# banner\n").encode("ascii"))
                support.wait_for(lambda: False, timeout=2.0)
            finally:
                conn.close()

        threading.Thread(target=serve, daemon=True).start()

        source = StreamSource(host="127.0.0.1", port=hub_port, timeout=2.0)
        try:
            source.open()
        except SourceError as exc:
            message = str(exc)
        else:
            source.stop()
            self.fail("a mismatched schema was accepted")
        self.assertIn("Logger", message)
        self.assertIn("columns.py", message)


class PortInUse(unittest.TestCase):
    def test_a_second_capture_says_what_is_wrong(self):
        port = support.free_port()
        first = SampleHub(port=port, banner=lambda: "")
        first.start()
        self.addCleanup(first.stop)

        second = SampleHub(port=port, banner=lambda: "")
        try:
            second.start()
        except OSError as exc:
            message = str(exc)
        else:
            second.stop()
            self.fail("two hubs bound the same port")
        self.assertIn(str(port), message)
        self.assertIn("already running", message)


class Backpressure(unittest.TestCase):
    """A viewer that stops reading must cost itself samples, never the capture its timing.

    Tested on _Client directly. Driving a real socket into this state means filling the
    kernel's send buffer as well as the queue, which is hundreds of kilobytes of timing
    -dependent setup to reach a decision that is four lines long and entirely local.
    """

    def test_a_full_backlog_sheds_its_oldest_row(self):
        client = _Client(conn=None, address=("127.0.0.1", 1))
        for i in range(CLIENT_QUEUE + 10):
            client.put("row %d" % i)
        self.assertEqual(client.dropped, 10)
        self.assertEqual(client.queue.qsize(), CLIENT_QUEUE)
        # The oldest went, not the newest: a late viewer should see the present.
        self.assertEqual(client.queue.get_nowait(), "row 10")

    def test_put_never_blocks(self):
        client = _Client(conn=None, address=("127.0.0.1", 1))
        for i in range(CLIENT_QUEUE * 3):
            client.put("row %d" % i)
        self.assertEqual(client.queue.qsize(), CLIENT_QUEUE)


if __name__ == "__main__":
    unittest.main()
