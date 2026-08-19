#!/usr/bin/env python3
"""The historical viewer. Reads the archive; never touches a board.

Separate from `webdash.py` in every way that matters. That one attaches to a running
capture and draws what is arriving; this one opens files that were copied here by
`archive/pull-logs.ps1` and answers questions about what already happened. It holds no
socket to anything, so there is no stream to keep alive, no client registry, and no
per-client backlog -- most of what makes `webhub.py` complicated has no counterpart here.

Bound to 127.0.0.1 and left that way, and there is no authentication in this file. That is
not an omission: a copy of this runs on each person's own machine against the share the
sensors write to, so nothing is ever served to the network and who may read the logs is a
question the share's permissions already answer. There is no web tier to secure because
there is no web tier.

    viewer.cmd                                  double-click; reads viewer.conf
    python viewer.py --archive \\\\fileserver\\NiclaLogs --open
    curl 'http://127.0.0.1:8990/events?from=2026-08-19T09:00:00'
"""

import argparse
import datetime
import json
import os
import socketserver
import sys
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import config
import tiles
from events import EventIndex
from logstore import LogStore
from series import DEFAULT_WIDTH, as_payload, bucket_rows
from webhub import WEB_ROOT, build_spec

DEFAULT_HTTP_PORT = 8990

MIME_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
}

# The columns the tiles actually draw. Asking for all 27 would send three arrays each of
# whatever nobody is looking at; this is the set /spec already told the client about.
TILE_COLUMNS = tuple(
    dict.fromkeys(column for tile in tiles.TILES for column, _l, _c in tile["series"])
)


def parse_when(text):
    """One query timestamp. Raises ValueError, which the handler turns into a 400."""
    return datetime.datetime.fromisoformat(text)


class _Server(ThreadingHTTPServer):
    daemon_threads = True

    def server_bind(self):
        """Bind without asking the resolver who we are.

        The same reverse-DNS hang webhub._Server avoids, and for the same reason -- the
        Server: header field nobody reads is not worth a lookup that can stall start-up on a
        machine with no resolver reachable. The explanation lives in webhub.py; this is four
        lines rather than an import, because the viewer sharing no code with the dashboard
        is the point of it being a separate program.
        """
        socketserver.TCPServer.server_bind(self)
        self.server_name = self.server_address[0]
        self.server_port = self.server_address[1]


class Handler(BaseHTTPRequestHandler):
    index_ref = None
    store_ref = None
    protocol_version = "HTTP/1.1"
    server_version = "niclaviewer"

    def log_message(self, *args):
        """Quiet. A reload is several requests and the console has better things to say."""

    def do_GET(self):
        parsed = urlparse(self.path)
        query = parse_qs(parsed.query)
        try:
            if parsed.path == "/events":
                self._events(query)
            elif parsed.path == "/range":
                self._range(query)
            elif parsed.path == "/spec":
                self._spec()
            elif parsed.path == "/archive":
                self._summary()
            elif parsed.path in ("/", "/index.html"):
                self._file("history.html")
            else:
                # basename() is the whole path-traversal defence, as in webhub: whatever
                # arrives, only a bare name inside web/ can be reached.
                self._file(os.path.basename(parsed.path))
        except ValueError as exc:
            # Only the query parsers raise this, and only on input the caller typed.
            self._text(400, "bad request: %s\n" % exc)

    # -- routes ----------------------------------------------------------------

    def _window(self, query):
        start = query.get("from", [None])[0]
        end = query.get("to", [None])[0]
        board = query.get("board", [None])[0]
        return (parse_when(start) if start else None,
                parse_when(end) if end else None,
                board or None)

    def _events(self, query):
        start, end, board = self._window(query)
        if board is not None and board not in self.store_ref.boards():
            self._text(404, "no such board: %s\n" % board)
            return
        found = self.index_ref.between(start=start, end=end, board=board)
        self._json({
            "events": [one.as_dict() for one in found],
            "count": len(found),
            "from": start.isoformat() if start else None,
            "to": end.isoformat() if end else None,
            "board": board,
        })

    def _range(self, query):
        """Rows for a window, reduced to something a plot can draw.

        One board per call. Two boards bucketed separately would land on different grids
        unless both were given the same window, and the window is exactly what this asks
        for, so they line up by construction.
        """
        start, end, board = self._window(query)
        boards = self.store_ref.boards()
        if board is None:
            if not boards:
                self._json({"board": None, "t": [], "columns": {}, "burst": [],
                            "buckets": 0, "rows": 0, "downsampled": False})
                return
            board = boards[0]
        if board not in boards:
            self._text(404, "no such board: %s\n" % board)
            return

        names = query.get("columns", [None])[0]
        columns = tuple(name for name in names.split(",") if name) if names else TILE_COLUMNS
        try:
            width = int(query.get("width", [DEFAULT_WIDTH])[0])
        except (TypeError, ValueError):
            raise ValueError("width must be a number")

        buckets, seen = bucket_rows(
            self._rows(board, start, end), columns, start=start, end=end, width=width
        )
        payload = as_payload(buckets, columns, seen)
        payload["board"] = board
        payload["from"] = start.isoformat() if start else None
        payload["to"] = end.isoformat() if end else None
        self._json(payload)

    def _rows(self, board, start, end):
        """Every row in the window, across however many captures it spans."""
        for capture in self.store_ref.captures(board=board, start=start, end=end):
            for row in self.store_ref.rows(capture, start=start, end=end):
                yield row

    def _spec(self):
        """The tiles, and what boards there are to draw them for.

        build_spec is webhub's, deliberately: the layout lives in tiles.py and is served
        rather than restated, and a second copy of that mapping here would be the same
        mistake the dashboard already avoids.
        """
        spec = build_spec(sample_hz=0.0, source="archive %s" % self.store_ref.root)
        spec["boards"] = self.store_ref.boards()
        spec["archive"] = self.store_ref.root
        spec["live"] = False
        # What the archive actually spans, from names and mtimes -- no file is opened for
        # this. The page opens on it rather than on a fixed span, because bucketing is
        # uniform across the *window*: ask for a day of an archive holding two minutes and
        # those two minutes land in two buckets and draw as a dot.
        captures = self.store_ref.captures()
        spec["extent"] = {
            "from": captures[0].start.isoformat() if captures else None,
            "to": max(one.end for one in captures).isoformat() if captures else None,
        }
        self._json(spec)

    def _file(self, name):
        full = os.path.join(WEB_ROOT, name)
        extension = os.path.splitext(name)[1]
        if extension not in MIME_TYPES or not os.path.isfile(full):
            self._text(404, "no such route: /%s\n" % name)
            return
        with open(full, "rb") as handle:
            body = handle.read()
        self._send(200, body, MIME_TYPES[extension])

    def _summary(self):
        """What is in the archive, in plain text. The page comes later; until then this is
        how you find out whether the pull is working."""
        store = self.store_ref
        lines = ["archive: %s" % store.root, ""]
        boards = store.boards()
        if not boards:
            lines.append("no boards yet -- has archive\\pull-logs.ps1 run?")
        for board in boards:
            captures = store.captures(board=board)
            if captures:
                lines.append("%-16s %4d capture(s)   %s .. %s" % (
                    board, len(captures), captures[0].start, captures[-1].end))
            else:
                lines.append("%-16s    no captures" % board)
        strays = store.strays()
        if strays:
            lines.append("")
            lines.append("not captures: %s" % ", ".join(strays))
        lines += ["", "routes: / /spec /events /range /archive"]
        self._text(200, "\n".join(lines) + "\n")

    # -- plumbing --------------------------------------------------------------

    def _json(self, payload):
        self._send(200, json.dumps(payload).encode("utf-8"),
                   "application/json; charset=utf-8")

    def _text(self, status, body):
        self._send(status, body.encode("utf-8"), "text/plain; charset=utf-8")

    def _send(self, status, body, content_type):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        # Not a live page, but the archive changes under it every sync, and a browser
        # holding yesterday's answer is the failure this whole design already risks.
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.end_headers()
        self.wfile.write(body)


def serve(root, port=DEFAULT_HTTP_PORT, host="127.0.0.1"):
    """Build a server around one archive. Returns it un-started, for tests."""
    store = LogStore(root)

    class Bound(Handler):
        store_ref = store
        index_ref = EventIndex(store)

    return _Server((host, port), Bound)


def build_parser():
    parser = argparse.ArgumentParser(
        description="Serve a browser view of an archive of Nicla captures.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "--config", default=None, metavar="FILE",
        help="INI file setting any of the options below. The command line wins.",
    )
    parser.add_argument("--archive", default=None, metavar="DIR",
                        help="Archive root: one directory per board. A UNC path is fine.")
    parser.add_argument("--http-port", type=int, default=DEFAULT_HTTP_PORT,
                        help="Port to serve on.")
    parser.add_argument("--open", action="store_true",
                        help="Open a browser at the viewer once it is up.")
    return parser


def parse_args(argv=None):
    """Command line over config file over defaults, exactly as main.py does it.

    Which is what makes the double-click launcher possible: viewer.conf carries the archive
    path, and the shortcut runs one fixed command that never has to be edited.
    """
    parser = build_parser()

    finder = argparse.ArgumentParser(add_help=False)
    finder.add_argument("--config", default=None)
    preview, _rest = finder.parse_known_args(argv)

    if preview.config is not None:
        try:
            parser.set_defaults(**config.load(preview.config, parser))
        except config.ConfigError as exc:
            parser.error(str(exc))

    args = parser.parse_args(argv)
    if not args.archive:
        parser.error("no archive given: pass --archive, or set archive in a --config file")
    return args


def main(argv=None):
    args = parse_args(argv)

    # Said plainly at start-up, because the usual reason is a share that has not mounted and
    # the alternative is a page that just says there are no boards yet -- which is what an
    # empty archive says too, and they are very different problems.
    reachable = os.path.isdir(args.archive)
    if not reachable:
        print("warning: %s is not readable -- is the share mounted?" % args.archive,
              file=sys.stderr)

    try:
        server = serve(args.archive, port=args.http_port)
    except OSError as exc:
        print("error: cannot serve on 127.0.0.1:%d (%s). Another viewer is probably "
              "already running -- use the window it opened." % (args.http_port, exc),
              file=sys.stderr)
        return 1

    url = "http://127.0.0.1:%d/" % args.http_port
    store = LogStore(args.archive)
    print("archive %s -- %d board(s)" % (args.archive, len(store.boards())))
    print("serving %s" % url)
    print("")
    print("Close this window to stop the viewer.")

    if args.open:
        # After the socket is bound but from another thread, so the browser opening cannot
        # delay the server that is about to answer it.
        threading.Thread(target=_open_later, args=(url,), daemon=True).start()

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


def _open_later(url, delay=0.4):
    time.sleep(delay)
    try:
        webbrowser.open(url)
    except Exception:
        # A machine with no browser association is not a reason to take the server down;
        # the URL is on screen either way.
        pass


if __name__ == "__main__":
    sys.exit(main())
