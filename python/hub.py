"""Serve the live sample stream to attached viewers over a TCP socket.

The wire format is the board's own: a '#col,col,...' schema line, a '# <banner>' line,
then one CSV row per sample -- exactly what nicla_stream.ino emits. So a viewer parses
the daemon with the same code it uses to parse the board (see StreamSource), and

    nc 127.0.0.1 8765

shows you the raw stream with no tooling at all.

Nothing here may ever block the caller. broadcast() runs inside the drain loop that also
feeds the CSV writer, and a viewer that stops reading -- a suspended shell, a laptop lid,
a plot window wedged behind a modal -- must cost that viewer its samples, not the capture
its timing. Every client therefore gets a bounded queue and its own writer thread, and a
full queue drops the oldest row rather than waiting.
"""

import json
import os
import queue
import socket
import threading

from columns import COLUMNS

# Per-client backlog. At 200 Hz this is ten seconds of slack, which covers a viewer
# stalled by a garbage collection or a window drag; past that the viewer is not really
# watching and stale rows are worth less than fresh ones.
CLIENT_QUEUE = 2000

DEFAULT_ENDPOINT = "127.0.0.1:8765"

# SO_REUSEADDR does not mean the same thing on both platforms, and taking the Unix meaning
# to Windows breaks the guarantee this module is built on.
#
# On Unix it means "rebind a port still in TIME_WAIT from a previous process", which is
# what a capture restarted immediately after being stopped needs, and it does not let two
# live sockets hold the same port. On Windows it means very nearly the opposite: a second
# socket may bind an address another socket is actively listening on, and which of them a
# connection reaches is unspecified. A second capture would therefore start silently on
# Windows and steal an arbitrary half of the viewers, instead of exiting with the message
# that says another logger already has the port. CI caught this the first time the suite
# ran on Windows; nothing on a Mac could have.
#
# The Windows behaviour that matters -- not being blocked by our own TIME_WAIT -- is what
# Windows does by default, so the correct setting there is simply not to set it.
REUSE_ADDR = os.name != "nt"


def parse_endpoint(text, default=DEFAULT_ENDPOINT):
    """Split 'host:port' into its parts, filling in whichever half was left out."""
    default_host, _, default_port = default.partition(":")
    text = (text or "").strip()
    if not text:
        return default_host, int(default_port)
    host, sep, port = text.rpartition(":")
    if not sep:
        # Bare number is a port, bare name is a host.
        return (default_host, int(text)) if text.isdigit() else (text, int(default_port))
    return (host or default_host), int(port)


def format_sample(sample):
    """One sample tuple as the board would have printed it."""
    # str() round-trips the parse: ints came from integer columns and print without a
    # decimal point, floats print with the digits they were parsed from.
    return ",".join(str(value) for value in sample)


class _Client(object):
    """One attached viewer: a socket, a bounded backlog, and a thread draining it."""

    def __init__(self, conn, address):
        self.conn = conn
        self.address = address
        self.queue = queue.Queue(maxsize=CLIENT_QUEUE)
        self.dropped = 0
        self.thread = None

    def describe(self):
        return "%s:%d" % self.address[:2]

    def put(self, line):
        try:
            self.queue.put_nowait(line)
        except queue.Full:
            # Same trade the source makes when the host falls behind: shed the oldest
            # row, count it, keep moving. See _ThreadedSource._emit.
            self.dropped += 1
            try:
                self.queue.get_nowait()
                self.queue.put_nowait(line)
            except (queue.Empty, queue.Full):
                pass

    def close(self):
        # Unblocks the writer thread if it is parked on the queue.
        try:
            self.queue.put_nowait(None)
        except queue.Full:
            pass
        try:
            self.conn.close()
        except OSError:
            pass


class SampleHub(object):
    """Fans samples out to every attached viewer.

    banner() is called per connection rather than stored, so a viewer that attaches after
    a rate change sees what the board is doing now, not what it was doing at start-up.
    """

    def __init__(self, host="127.0.0.1", port=8765, banner=None):
        self.host = host
        self.port = port
        self._banner = banner
        self._server = None
        self._clients = []
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = None
        self.served = 0
        self.dropped = 0

    # -- lifecycle -------------------------------------------------------------

    def start(self):
        server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        if REUSE_ADDR:
            server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            server.bind((self.host, self.port))
        except OSError as exc:
            server.close()
            raise OSError(
                "cannot listen on %s:%d (%s). Another logger is probably already "
                "running -- attach to it instead, or pass a different --listen address."
                % (self.host, self.port, exc)
            )
        server.listen(8)
        # So stop() is not left waiting on accept() for a client that never comes.
        server.settimeout(0.5)
        self._server = server
        self._stop.clear()
        self._thread = threading.Thread(target=self._accept_loop, daemon=True)
        self._thread.start()
        return self

    def stop(self):
        self._stop.set()
        if self._server is not None:
            try:
                self._server.close()
            except OSError:
                pass
            self._server = None
        if self._thread is not None:
            self._thread.join(timeout=1.0)
        with self._lock:
            clients, self._clients = self._clients, []
        for client in clients:
            client.close()

    @property
    def clients(self):
        with self._lock:
            return len(self._clients)

    def describe(self):
        return "%s:%d" % (self.host, self.port)

    # -- accepting -------------------------------------------------------------

    def _accept_loop(self):
        while not self._stop.is_set():
            try:
                conn, address = self._server.accept()
            except socket.timeout:
                continue
            except OSError:
                # The listening socket was closed under us by stop().
                break
            self._attach(conn, address)

    def _attach(self, conn, address):
        # Rows are small and frequent; Nagle would hold them back to fill a segment and
        # add tens of milliseconds of lag to a live plot for no bandwidth worth having.
        try:
            conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        except OSError:
            pass
        client = _Client(conn, address)
        banner = self._banner() if self._banner is not None else ""
        client.put("#" + ",".join(COLUMNS))
        client.put("# " + banner)
        client.thread = threading.Thread(
            target=self._writer, args=(client,), daemon=True
        )
        with self._lock:
            self._clients.append(client)
            self.served += 1
        client.thread.start()

    def _writer(self, client):
        """Drain one client's backlog to its socket until it dies or we stop."""
        try:
            while not self._stop.is_set():
                try:
                    line = client.queue.get(timeout=0.5)
                except queue.Empty:
                    continue
                if line is None:
                    break
                client.conn.sendall((line + "\n").encode("ascii", "replace"))
        except OSError:
            # A viewer closing its window is the normal way this ends, not an error.
            pass
        finally:
            self._drop(client)

    def _drop(self, client):
        with self._lock:
            if client in self._clients:
                self._clients.remove(client)
                self.dropped += client.dropped
        client.close()

    # -- publishing ------------------------------------------------------------

    def broadcast(self, sample):
        """Queue one sample for every viewer. Called from the drain loop; never blocks."""
        with self._lock:
            if not self._clients:
                return
            clients = tuple(self._clients)
        line = format_sample(sample)
        for client in clients:
            client.put(line)

    def push_status(self, status):
        """Publish the capture's status dict on the '#' comment channel.

        Sent as a comment so it rides the same connection as the data and a reader that
        only cares about samples -- nc, or anything reusing the board's parser -- skips it
        the same way it skips the board's own banner.
        """
        with self._lock:
            clients = tuple(self._clients)
        if not clients:
            return
        line = "# status " + json.dumps(status, separators=(",", ":"))
        for client in clients:
            client.put(line)

    @property
    def client_drops(self):
        """Rows shed across all viewers, live and departed."""
        with self._lock:
            return self.dropped + sum(c.dropped for c in self._clients)
