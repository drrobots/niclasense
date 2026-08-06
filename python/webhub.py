"""Serve the live sample stream to browsers over HTTP, plus the page that draws it.

The socket hub in hub.py fans samples out to programs; this fans the same samples out to
browser tabs, and hands them the static files they need to render. Deliberately the same
shape: a bounded queue per client, drop-oldest when one falls behind, and never a blocking
call inside broadcast(), which runs in the capture's drain loop.

Three routes matter:

    GET /         the dashboard page, and its CSS/JS from web/
    GET /spec     tiles.py and columns.py as JSON -- the layout lives in one place
    GET /stream   Server-Sent Events: sample rows, and the capture's status

SSE rather than WebSockets because the browser dashboard is read-only -- there is no
control channel to carry, so a WebSocket would buy nothing but a handshake and a framing
layer. SSE is a plain HTTP response the browser reconnects on its own, and it needs no
dependency: this module is stdlib top to bottom.

The rows on the wire are the board's own CSV, produced by hub.format_sample(), so the
browser parses exactly what nc would show and there is only one wire format in the project.
"""

import json
import os
import queue
import socketserver
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import tiles
from columns import COLUMNS
from hub import CLIENT_QUEUE, format_sample

DEFAULT_HTTP_PORT = 8988

WEB_ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web")

# What a tab opening mid-capture is handed before live rows start: enough for a full
# default window, so it draws a populated dashboard immediately instead of thirty seconds
# of blank tiles filling in. Not sized to MAX_WINDOW_S -- ten minutes at 200 Hz is 120k
# rows of backlog held forever on the off-chance someone picks the longest window.
PRIME_ROWS = 6000

# How long a client's writer waits to accumulate rows before flushing them as one event.
# At 200 Hz this puts about ten samples in each event, so the browser wakes up twenty times
# a second rather than two hundred -- the event dispatch, not the drawing, is what makes a
# per-sample stream expensive on the client.
FLUSH_INTERVAL = 0.05

# Rows in a single event. A client that has been starved for a while -- or one that has
# just been handed PRIME_ROWS of backlog -- would otherwise emit one enormous event and
# stall the browser parsing it.
MAX_BATCH = 400

# Silence longer than this gets a comment line, so an idle stream is not mistaken for a
# dead connection by the browser or anything between it and here.
KEEPALIVE_INTERVAL = 15.0

MIME_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
}


def build_spec(sample_hz=200.0, source=""):
    """Everything the browser needs to lay itself out, as JSON-safe data.

    Served rather than duplicated in JavaScript so that adding a tile to tiles.py adds it
    to the browser too. Note the accuracy-note keys arrive as strings: JSON has no integer
    keys, and the client indexes with String(acc) to match.
    """
    return {
        "columns": list(COLUMNS),
        "tiles": [
            {
                "name": tile["name"],
                "title": tile["title"],
                "series": [
                    {"column": column, "label": label, "colour": colour}
                    for column, label, colour in tile["series"]
                ],
                "value": tile.get("value"),
                "unit": tile["unit"],
                "fmt": tile["fmt"],
                "min_span": tile["min_span"],
                "quaternion": bool(tile.get("quaternion")),
                "bsec": tile["name"] in tiles.BSEC_TILES,
                "placement": list(tiles.PLACEMENT[tile["name"]]),
            }
            for tile in tiles.TILES
        ],
        "capture_slot": list(tiles.CAPTURE_SLOT),
        "accuracy_notes": {
            str(key): {"note": note, "colour": colour}
            for key, (note, colour) in tiles.BSEC_ACCURACY_NOTES.items()
        },
        "max_points": tiles.MAX_POINTS,
        "min_window_s": tiles.MIN_WINDOW_S,
        "max_window_s": tiles.MAX_WINDOW_S,
        "sample_hz": sample_hz,
        "source": source,
        "palettes": {"dark": tiles.DARK_PALETTE, "light": tiles.LIGHT_PALETTE},
        "light_overrides": tiles.LIGHT_OVERRIDES,
    }


class _Server(ThreadingHTTPServer):
    # Otherwise a tab left open holds its /stream thread and the process never exits.
    daemon_threads = True

    def server_bind(self):
        """Bind without asking the resolver who we are.

        HTTPServer.server_bind() runs the bound address through socket.getfqdn() to fill in
        a Server: header field nothing here uses. That is a reverse DNS lookup, and on a
        machine with no resolver reachable it does not fail -- it waits, and start() hangs
        for as long as the lookup takes with no output to explain why.
        """
        socketserver.TCPServer.server_bind(self)
        self.server_name = self.server_address[0]
        self.server_port = self.server_address[1]


# Queued in place of a row to tell a writer to finish. A sentinel object rather than None
# so it can never be confused with "the queue timed out and there was nothing there".
_CLOSED = object()


class _Client(object):
    """One open /stream response: a bounded backlog the request thread drains."""

    def __init__(self):
        self.queue = queue.Queue(maxsize=CLIENT_QUEUE)
        self.dropped = 0

    def put(self, item):
        try:
            self.queue.put_nowait(item)
        except queue.Full:
            # Same trade hub._Client makes: a tab that stopped reading -- backgrounded,
            # laptop lid shut -- loses its oldest rows rather than costing the capture its
            # timing. broadcast() runs in the drain loop and must not block here.
            self.dropped += 1
            try:
                self.queue.get_nowait()
                self.queue.put_nowait(item)
            except (queue.Empty, queue.Full):
                pass

    def close(self):
        # Unblocks the writer if it is parked waiting for the next row.
        try:
            self.queue.put_nowait(_CLOSED)
        except queue.Full:
            pass


class WebHub(object):
    """HTTP server publishing the stream to browsers.

    Lifecycle mirrors SampleHub: start(), broadcast() per sample, push_status()
    periodically, stop() at the end.
    """

    def __init__(self, host="127.0.0.1", port=DEFAULT_HTTP_PORT, spec=None):
        self.host = host
        self.port = port
        self.spec = spec if spec is not None else build_spec()
        self._server = None
        self._thread = None
        self._clients = []
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self.served = 0
        self.dropped = 0
        # Replayed to each new tab so it opens on a full window. A deque would need an
        # import for no gain over a list we slice; it is only touched once per sample.
        self._prime = []
        self._status = None
        # Every row served gets a number, and the number goes out as the SSE event id. A
        # browser that reconnects sends the last one it saw back in Last-Event-ID, which is
        # what lets attach() hand it only what it missed. Ids are per-hub and monotonic;
        # _prime_first_id is the id of _prime[0], so the id of _prime[k] needs no storage.
        self._next_id = 0
        self._prime_first_id = 0

    # -- lifecycle -------------------------------------------------------------

    def start(self):
        hub = self

        class Handler(_StreamHandler):
            hub_ref = hub

        try:
            server = _Server((self.host, self.port), Handler)
        except OSError as exc:
            raise OSError(
                "cannot serve on %s:%d (%s). Another web dashboard is probably already "
                "running -- open its page instead, or pass a different --http-port."
                % (self.host, self.port, exc)
            )
        self._server = server
        self._stop.clear()
        self._thread = threading.Thread(target=server.serve_forever, daemon=True)
        self._thread.start()
        return self

    def stop(self):
        self._stop.set()
        with self._lock:
            clients, self._clients = self._clients, []
        for client in clients:
            client.close()
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._server = None
        if self._thread is not None:
            self._thread.join(timeout=1.0)

    @property
    def stopping(self):
        return self._stop.is_set()

    @property
    def clients(self):
        with self._lock:
            return len(self._clients)

    def url(self):
        return "http://%s:%d/" % (self.host, self.port)

    # -- publishing ------------------------------------------------------------

    def broadcast(self, sample):
        """Queue one sample for every open tab. Called from the drain loop; never blocks."""
        line = format_sample(sample)
        with self._lock:
            row_id = self._next_id
            self._next_id += 1
            self._prime.append(line)
            if len(self._prime) > PRIME_ROWS:
                # Trimmed in blocks rather than one row at a time: del of a leading slice
                # is a single memmove, where popping per sample is a memmove per sample.
                dropped = len(self._prime) - PRIME_ROWS
                del self._prime[:dropped]
                self._prime_first_id += dropped
            clients = tuple(self._clients)
        for client in clients:
            client.put(("data", (row_id, line)))

    def push_status(self, status):
        """Publish the capture's status dict to every tab, and to tabs not yet open."""
        text = json.dumps(status, separators=(",", ":"))
        with self._lock:
            self._status = text
            clients = tuple(self._clients)
        for client in clients:
            client.put(("status", text))

    def push_event(self, name, payload):
        """Send a one-off named event -- 'ended' when the capture goes away."""
        text = json.dumps(payload, separators=(",", ":"))
        with self._lock:
            clients = tuple(self._clients)
        for client in clients:
            client.put((name, text))

    # -- client registry -------------------------------------------------------

    def attach(self, last_id=None):
        """Register a new tab, and hand back the (id, row) pairs it has missed.

        The backlog is returned rather than queued because it is three times the size of a
        client's queue: pushing it through would evict its own oldest rows and leave the
        tab with whatever fitted, which looks like a page that only ever buffers ten
        seconds no matter how long it has been open. The caller writes it straight out.

        `last_id` is the Last-Event-ID a reconnecting browser sends, and it is the whole
        difference between a blip and a visible fault. EventSource reconnects on its own
        without reloading the page, so the tab still holds every sample it had; replaying
        the full backlog to it hands it rows it drew thirty seconds ago, and app.js reads
        a row older than its newest as a board reset and clears every tile. Resuming from
        the id instead means a reconnect costs the rows that were in flight and nothing
        else. A tab that has fallen further behind than we still hold gets what there is:
        a gap jumps the traces forward, which is honest and not a wipe.
        """
        client = _Client()
        with self._lock:
            self._clients.append(client)
            self.served += 1
            start = 0
            if last_id is not None:
                start = min(max(last_id + 1 - self._prime_first_id, 0), len(self._prime))
            first_id = self._prime_first_id + start
            backlog = list(
                zip(range(first_id, first_id + len(self._prime) - start),
                    self._prime[start:])
            )
            return client, backlog, self._status

    def detach(self, client):
        with self._lock:
            if client in self._clients:
                self._clients.remove(client)
                self.dropped += client.dropped
        client.close()

    @property
    def client_drops(self):
        with self._lock:
            return self.dropped + sum(c.dropped for c in self._clients)


class _StreamHandler(BaseHTTPRequestHandler):
    """Routes. `hub_ref` is filled in by the subclass WebHub.start() builds."""

    hub_ref = None
    protocol_version = "HTTP/1.1"
    server_version = "niclaweb"

    def log_message(self, *args):
        """Silence the per-request access log.

        The default writes a line to stderr for every request, which on a page that opens
        a permanent event stream and reloads freely is noise the capture's own progress
        output has to compete with.
        """

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path == "/stream":
            self._serve_stream()
        elif path == "/spec":
            self._serve_json(self.hub_ref.spec)
        elif path in ("/", "/index.html"):
            self._serve_file("index.html")
        else:
            # basename() is the whole path-traversal defence: whatever arrives, only a
            # bare name inside web/ can be reached, and only if it exists there.
            self._serve_file(os.path.basename(path))

    # -- routes ----------------------------------------------------------------

    def _serve_json(self, payload):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _serve_file(self, name):
        full = os.path.join(WEB_ROOT, name)
        extension = os.path.splitext(name)[1]
        if extension not in MIME_TYPES or not os.path.isfile(full):
            self.send_error(404)
            return
        with open(full, "rb") as handle:
            body = handle.read()
        self.send_response(200)
        self.send_header("Content-Type", MIME_TYPES[extension])
        self.send_header("Content-Length", str(len(body)))
        # The page is edited while it is being looked at; a cached copy of yesterday's
        # JavaScript against today's /spec is a confusing way to spend an afternoon.
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _serve_stream(self):
        hub = self.hub_ref
        client, backlog, status = hub.attach(self._last_event_id())
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-store")
        # No Content-Length is possible on a response that never ends, so the connection
        # cannot be reused afterwards and has to be marked as closing.
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True
        try:
            if status is not None:
                self._write("event: status\ndata: %s\n\n" % status)
            # In batches, so a tab opening on a long-running capture is not handed a single
            # six-thousand-row event to parse before it can draw anything.
            for start in range(0, len(backlog), MAX_BATCH):
                self._flush(backlog[start:start + MAX_BATCH])
            self._pump(client)
        except (BrokenPipeError, ConnectionResetError, OSError):
            # A closed tab is how this normally ends, not a failure worth reporting.
            pass
        finally:
            hub.detach(client)

    def _last_event_id(self):
        """The id a reconnecting EventSource reports, or None for a tab opening fresh.

        The browser sets the header itself from the last `id:` it saw; nothing in app.js
        has to remember anything. A value we cannot read as a number is treated as absent,
        which costs that tab one full replay rather than an error.
        """
        raw = self.headers.get("Last-Event-ID")
        if raw is None:
            return None
        try:
            return int(raw)
        except (TypeError, ValueError):
            return None

    def _pump(self, client):
        """Move one client's backlog to its socket, batching rows into events.

        Each pass accumulates for FLUSH_INTERVAL and then sends what it has as one event.
        The waiting is the point: a row arrives every 5 ms, and sending each one as it
        lands would give the browser two hundred event callbacks a second to service for a
        dashboard that redraws twenty times a second. Draining greedily instead -- send as
        soon as something is there -- is the same thing with extra steps, which is why the
        loop watches a deadline rather than the queue going empty.
        """
        hub = self.hub_ref
        last_write = time.monotonic()
        while not hub.stopping:
            rows = []
            deadline = time.monotonic() + FLUSH_INTERVAL
            while len(rows) < MAX_BATCH:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                try:
                    item = client.queue.get(timeout=remaining)
                except queue.Empty:
                    break
                if item is _CLOSED:
                    self._flush(rows)
                    return
                kind, payload = item
                if kind == "data":
                    rows.append(payload)  # (id, line)
                else:
                    # Status and lifecycle events are named, and jump the queue of rows
                    # behind them rather than waiting out the batch they landed in.
                    self._flush(rows)
                    rows = []
                    self._write("event: %s\ndata: %s\n\n" % (kind, payload))
                    last_write = time.monotonic()
            if rows:
                self._flush(rows)
                last_write = time.monotonic()
            elif time.monotonic() - last_write >= KEEPALIVE_INTERVAL:
                # A comment line: ignored by EventSource, but it is traffic, which is what
                # keeps an idle connection from being reaped.
                self._write(":\n\n")
                last_write = time.monotonic()

    def _flush(self, rows):
        """Write one event carrying many rows, as (id, line) pairs.

        The browser joins the data lines with newlines and hands the block to a single
        onmessage call. The trailing `id:` is the id of the *last* row in the event, which
        is what EventSource stores and sends back as Last-Event-ID after a reconnect -- so
        resuming lands on the first row this tab has not already drawn.
        """
        if not rows:
            return
        self._write(
            "".join("data: %s\n" % line for _id, line in rows)
            + "id: %d\n\n" % rows[-1][0]
        )

    def _write(self, text):
        self.wfile.write(text.encode("ascii", "replace"))
        self.wfile.flush()
