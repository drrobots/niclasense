#!/usr/bin/env python3
"""The historical viewer. Reads the archive; never touches a board.

Separate from `webdash.py` in every way that matters. That one attaches to a running
capture and draws what is arriving; this one opens files that were copied here by
`archive/pull-logs.ps1` and answers questions about what already happened. It holds no
socket to anything, so there is no stream to keep alive, no client registry, and no
per-client backlog -- most of what makes `webhub.py` complicated has no counterpart here.

Bound to 127.0.0.1 and left that way. There is no authentication in this file and there is
not going to be: putting it in front of people is a reverse proxy's job, where Windows
Integrated Auth against AD is configuration rather than code. See HISTORICAL-VIEWER.md.

So far it serves the burst index, which is the first useful question about an archive --
not "draw me a day" but "what happened, and when". The range and the page come next.

    python viewer.py --archive D:\\NiclaArchive
    curl 'http://127.0.0.1:8990/events?from=2026-08-19T09:00:00'
"""

import argparse
import datetime
import json
import socketserver
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from events import EventIndex
from logstore import LogStore

DEFAULT_HTTP_PORT = 8990


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
            elif parsed.path in ("/", "/index.html"):
                self._summary()
            else:
                self._text(404, "no such route: %s\n" % parsed.path)
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
        lines += ["", "routes: /events?from=&to=&board="]
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


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Serve the burst index for an archive of Nicla captures.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--archive", required=True, metavar="DIR",
                        help="Archive root: one directory per board.")
    parser.add_argument("--http-port", type=int, default=DEFAULT_HTTP_PORT,
                        help="Port to serve on.")
    args = parser.parse_args(argv)

    try:
        server = serve(args.archive, port=args.http_port)
    except OSError as exc:
        print("error: cannot serve on 127.0.0.1:%d (%s)" % (args.http_port, exc),
              file=sys.stderr)
        return 1

    store = LogStore(args.archive)
    print("archive %s -- %d board(s)" % (args.archive, len(store.boards())))
    print("serving http://127.0.0.1:%d/ -- Ctrl-C to stop" % args.http_port)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
