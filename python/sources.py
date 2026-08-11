"""Data sources for the Nicla Sense ME stream.

A source runs its I/O on a background thread and hands finished samples to a queue.Queue.
That keeps the main thread free to poll -- originally for matplotlib, which on macOS insists
on owning it, and now for the drain loops in main.py and webdash.py.

Two of them, interchangeable to everything downstream:

    SerialSource  reads the board over USB.
    StreamSource  reads a headless logger over TCP (see hub.py).

Both speak the same line protocol -- the sketch's -- so the parsing below is shared, and
the plot cannot tell which one it is attached to.

A sample is a tuple in COLUMNS order: (seq, t_ms, ax_g, ..., bsec_acc).
"""

import json
import queue
import socket
import sys
import threading
import time

from columns import (
    COLUMNS,
    NICLA_USB_PIDS,
    NICLA_USB_VID,
    PARSERS,
)

# Baud rates offered in the GUI and swept during auto-detection, fastest first. 1 Mbaud is
# what the sketch boots at and the nRF52832 UARTE's maximum; the rest are the standard
# divisors a host driver is most likely to accept if 1 Mbaud is refused.
BAUD_CHOICES = (1000000, 460800, 230400, 115200, 57600, 9600)

# Streaming rates offered in the GUI. All divide 1000 exactly, because the sketch computes
# its period as the integer 1000/hz -- ask for 150 and you silently get 166.
RATE_CHOICES = (200, 100, 50, 25, 10, 5, 1)

# One full CSV row is ~162 bytes plus the newline, and 8N1 spends 10 bits per byte.
LINE_BITS = 163 * 10

# Fraction of the wire a stream is allowed to occupy. The shipped design point (200 Hz at
# 1 Mbaud) sits at 33%, and staying near it is deliberate: the core's Serial is an
# UnbufferedSerial that busy-waits per byte, so a link pushed to saturation does not
# degrade by dropping samples -- it stalls loop(), starves BHY2.update(), and wedges the
# board until it is reset.
LINK_BUDGET = 0.6

# Gap between command bytes. The board has no RX buffer beyond the UARTE FIFO, so bursts
# written while it is busy-waiting in printCsv() are silently truncated -- see
# SerialSource.send_command(). 2 ms measured clean; 3 ms is margin on a 15 ms command.
COMMAND_BYTE_GAP = 0.003

# Port read timeout. Kept short because _read_lines() enforces its own deadline; reads are
# sized from in_waiting, so a busy stream still moves in bulk.
READ_TIMEOUT = 0.1

# A full CSV row is ~163 bytes. Well past that with no newline in sight means the bytes
# are not this board's stream, so the partial buffer is dropped rather than grown.
MAX_LINE_BYTES = 8192


def safe_rate_for(baud):
    """Fastest offered rate whose CSV stream fits comfortably in `baud`."""
    ceiling = LINK_BUDGET * baud / LINE_BITS
    for rate in RATE_CHOICES:
        if rate <= ceiling:
            return rate
    return RATE_CHOICES[-1]


class SourceError(Exception):
    """Raised when a source cannot be opened or dies unrecoverably."""


def check_schema(line, who, remedy):
    """Validate a '#col,col,...' schema line against COLUMNS.

    Shared by both sources: schema drift is the same failure whether the far end is a
    board running an old sketch or a logger running old code, and it is worth saying so
    precisely rather than letting the field-count check call every row malformed.
    """
    theirs = tuple(line.lstrip("#").split(","))
    if theirs != COLUMNS:
        raise SourceError(
            "%s schema does not match columns.py.\n"
            "  %s:  %s\n  expected: %s\n%s"
            % (who, who.lower(), ",".join(theirs), ",".join(COLUMNS), remedy)
        )
    return theirs


class _ThreadedSource(object):
    """Shared plumbing: a reader thread, an output queue, a stop flag, and the parser.

    The line protocol lives here rather than in SerialSource because the headless logger
    re-serves the board's own format, so a socket reader wants exactly the same parse,
    the same field-count check, and the same banner handling.
    """

    def __init__(self, max_queue=20000):
        self.queue = queue.Queue(maxsize=max_queue)
        self._stop = threading.Event()
        self._thread = None
        self.error = None
        self.dropped = 0
        self.malformed = 0
        self.banner = None
        # Whatever the board last reported in its banner; None until a header arrives.
        self.stream_hz = None
        self.reported_baud = None
        # Partial tail left over between reads; see _split_lines().
        self._rx = b""

    def start(self):
        # Cleared rather than assumed unset, so a source can be stopped and restarted --
        # which is exactly what a baud change does.
        self._stop.clear()
        self._thread = threading.Thread(target=self._run_guarded, daemon=True)
        self._thread.start()

    def _run_guarded(self):
        try:
            self._run()
        except Exception as exc:  # surfaced to the main thread via .error
            self.error = exc

    def _run(self):
        raise NotImplementedError

    def _emit(self, sample):
        try:
            self.queue.put_nowait(sample)
        except queue.Full:
            # The consumer has fallen behind. Drop the oldest sample rather than block
            # the reader, which would back up the serial buffer and corrupt timing.
            self.dropped += 1
            try:
                self.queue.get_nowait()
                self.queue.put_nowait(sample)
            except (queue.Empty, queue.Full):
                pass

    def stop(self):
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)

    @property
    def running(self):
        return self._thread is not None and self._thread.is_alive()

    # -- line protocol ---------------------------------------------------------

    def _split_lines(self, chunk):
        """Complete lines out of a chunk of bytes, holding the partial tail back."""
        self._rx += chunk
        if b"\n" not in self._rx:
            if len(self._rx) > MAX_LINE_BYTES:
                # No newline in far more than a line's worth: this is not our stream.
                self._rx = b""
            return []
        parts = self._rx.split(b"\n")
        self._rx = parts.pop()
        return [line.decode("utf-8", "replace").strip() for line in parts]

    def _consume_line(self, line):
        """Turn one received line into a queued sample, a banner, or a malformed count."""
        if not line:
            return
        if line.startswith("#"):
            self._consume_comment(line)
            return
        fields = line.split(",")
        if len(fields) != len(COLUMNS):
            # Partial line, typically the first read after a reset.
            self.malformed += 1
            return
        try:
            sample = tuple(parse(f) for parse, f in zip(PARSERS, fields))
        except ValueError:
            self.malformed += 1
            return
        self._emit(sample)

    def _consume_comment(self, line):
        # A banner mid-stream means someone sent 'h' -- which set_rate() does on every
        # change, so this is where a rate command is acknowledged.
        if line.startswith("# "):
            self._parse_banner(line[2:])

    def _parse_banner(self, text):
        """Record a '# <banner>' line and pull the board's live settings out of it."""
        self.banner = text
        for field in text.split():
            name, _, value = field.partition("=")
            if not value.isdigit():
                continue
            if name == "rate_hz":
                self.stream_hz = int(value)
            elif name == "baud":
                self.reported_baud = int(value)


# ---------------------------------------------------------------------------
# Serial
# ---------------------------------------------------------------------------


def list_serial_ports():
    """All serial ports, as (device, description) pairs."""
    from serial.tools import list_ports

    return [(p.device, p.description or "") for p in list_ports.comports()]


def find_nicla_port():
    """Best guess at the Nicla's port, by USB VID/PID then by name."""
    from serial.tools import list_ports

    ports = list(list_ports.comports())
    for port in ports:
        if port.vid == NICLA_USB_VID and port.pid in NICLA_USB_PIDS:
            return port.device
    for port in ports:
        text = "%s %s" % (port.description or "", port.product or "")
        if "nicla" in text.lower():
            return port.device
    # Fall back to any USB modem device, excluding the Bluetooth pseudo-ports.
    for port in ports:
        if "usbmodem" in port.device and "Bluetooth" not in port.device:
            return port.device
    return None


class SerialSource(_ThreadedSource):
    """Reads CSV lines over USB serial."""

    def __init__(self, port=None, baud=1000000, max_queue=20000, autobaud=True):
        _ThreadedSource.__init__(self, max_queue=max_queue)
        self.port = port
        self.baud = baud
        self.autobaud = autobaud
        self._serial = None
        # Commands are written from the GUI thread while the reader thread is parked in a
        # read. The two directions are independent, but a lock costs nothing and keeps a
        # reconnect from closing the port mid-write.
        self._write_lock = threading.Lock()

    def open(self):
        if self.port is None:
            self.port = find_nicla_port()
            if self.port is None:
                raise SourceError(
                    "No Nicla serial port found. Plug the board in, or pass --port. "
                    "Run with --list-ports to see what is available."
                )

        order = [self.baud]
        if self.autobaud:
            order += [b for b in BAUD_CHOICES if b != self.baud]

        failure = None
        for baud in order:
            try:
                # Only the first choice gets the patient treatment; the rest are probes.
                self._open_at(baud, probe=baud != order[0])
            except SourceError as exc:
                failure = exc
                continue
            if baud != order[0]:
                sys.stderr.write(
                    "note: no valid stream at %d baud, connected at %d instead. "
                    "Reset the board to return it to its boot rate.\n" % (order[0], baud)
                )
            return self

        raise failure or SourceError("Could not read a Nicla stream from %s." % self.port)

    def _open_at(self, baud, probe=False):
        """Open the port at one baud and confirm a real stream is coming out of it."""
        import serial

        try:
            # Constructed unopened so DTR/RTS can be deasserted before the port comes up.
            # The Nicla's USB side is a CMSIS-DAP probe, and it turns an asserted DTR into
            # an nRF52 reset -- so opening the port the ordinary way reboots the board.
            # That silently wipes BSEC's calibration state, and since the gas sensor needs
            # several minutes of continuous run-in, IAQ/CO2/bVOC sit pinned at their
            # stabilisation defaults (25 / 500 / 0.49, bsec_acc 0) for every session short
            # enough to be reset again first. The auto-baud sweep below makes it worse: one
            # reset per candidate rate.
            port = serial.Serial()
            port.port = self.port
            port.baudrate = baud
            # Short timeout so _read_lines() can enforce its own deadline; the reads are
            # driven by in_waiting, so this costs nothing when the stream is busy.
            port.timeout = READ_TIMEOUT
            port.dtr = False
            port.rts = False
            port.open()
        except serial.SerialException as exc:
            raise SourceError("Could not open %s at %d baud: %s" % (self.port, baud, exc))

        self._serial = port
        self.baud = baud
        self._rx = b""

        # Ask for the header so we can check the board's schema against ours. The request
        # is a single byte and can still be swallowed by a full RX FIFO, so ask more than
        # once -- otherwise a stale banner survives a reconnect and the GUI reports the
        # baud we just left. A probe is only deciding signal from noise, so one ask is
        # enough and keeps the auto-baud sweep short.
        outcome = "none"
        failure = None
        for _ in range(1 if probe else 3):
            try:
                port.reset_input_buffer()
                port.write(b"h")
                port.flush()
                outcome = self._read_header(timeout=1.2 if probe else 1.5)
            except SourceError:
                raise
            except Exception as exc:
                # A wrong baud can upset the driver itself, and pyserial surfaces that as
                # SerialException rather than silence. Treat it as this candidate failing,
                # not as the sweep failing.
                failure = exc
                break
            if outcome == "header":
                break

        if outcome == "none":
            try:
                port.close()
            except Exception:
                pass
            self._serial = None
            raise SourceError(
                "Nothing recognisable arrived from %s at %d baud%s."
                % (self.port, baud, ": %s" % failure if failure else "")
            )

    def _read_lines(self, timeout):
        """Complete lines arriving within `timeout` seconds, as decoded strings.

        Deliberately not readline(). pyserial's readline has no wall-clock bound -- its
        timeout resets on every byte received -- so it only returns when a 0x0A turns up.
        A baud mismatch streams dense structured garbage that can contain none at all:
        probing a 115200 stream at 1 Mbaud yields ~12 kB/s of it, and readline() hangs
        outright rather than timing out. Everything here is bounded by the deadline.
        """
        deadline = time.time() + timeout
        lines = []
        while time.time() < deadline and not lines:
            waiting = self._serial.in_waiting
            chunk = self._serial.read(waiting if waiting else 1)
            if not chunk:
                continue
            lines.extend(self._split_lines(chunk))
        return lines

    def _read_header(self, timeout=3.0):
        """Consume metadata until the column list arrives (or we give up).

        Returns "header" if the board's schema was seen and matched, "data" if only
        well-formed sample lines showed up, or "none" if nothing usable did -- which is
        how open() recognises a wrong baud, since a mismatched UART yields noise that
        fails both tests.
        """
        expected = len(COLUMNS)
        deadline = time.time() + timeout
        saw_data = False
        while time.time() < deadline:
            remaining = max(0.0, deadline - time.time())
            for line in self._read_lines(min(0.3, remaining)):
                if not line:
                    continue
                if line.startswith("#"):
                    if line.startswith("# "):
                        self._parse_banner(line[2:])
                        continue
                    check_schema(line, "Board", "Reflash nicla_stream.ino, or update "
                                               "COLUMNS to match.")
                    return "header"
                if len(line.split(",")) == expected:
                    # The sketch only prints its header at boot or on 'h', so a board
                    # already mid-stream is fine -- the field count is proof enough that
                    # the baud is right and the schema plausible.
                    saw_data = True
        return "data" if saw_data else "none"

    # -- runtime control -------------------------------------------------------

    def send_command(self, text):
        """Write a sketch command, one byte at a time.

        The pacing is not politeness. The core's Serial is an mbed UnbufferedSerial with
        no RX ring buffer, so incoming bytes live in the UARTE's few-byte hardware FIFO
        until loop() reads them -- and loop() spends milliseconds at a time busy-waiting
        inside printCsv(). A command written as one burst lands mid-line and gets partly
        eaten, silently: measured 7/12 delivery for "s100h" at 200 Hz, against 12/12 with
        the gap below.
        """
        with self._write_lock:
            if self._serial is None:
                raise SourceError("Serial port is not open.")
            try:
                for char in text:
                    self._serial.write(char.encode("ascii"))
                    self._serial.flush()
                    time.sleep(COMMAND_BYTE_GAP)
            except Exception as exc:
                raise SourceError("Could not write to %s: %s" % (self.port, exc))

    def _refresh_banner(self, attempts=4):
        """Re-ask for the banner until it describes the baud we are actually on.

        _open_at() asks too, but that request races the reopen and is the one most likely
        to be dropped. Retrying here -- with the reader thread live, so the parse goes
        through the same path as every other banner -- keeps the GUI from reporting the
        baud it just left.
        """
        for _ in range(attempts):
            try:
                self.send_command("h")
            except SourceError:
                return False
            deadline = time.time() + 0.5
            while time.time() < deadline:
                if self.reported_baud == self.baud:
                    return True
                time.sleep(0.02)
        return False

    def set_rate(self, hz, attempts=4):
        """Ask the board to stream at `hz`, confirming it took. Returns True if it did.

        Terminated with 'h' rather than a newline: the sketch acts on the digits when any
        non-digit arrives and then falls through to the switch, so 'h' both ends the
        argument and makes the board reprint its banner. _run() parses that back into
        stream_hz, giving a free acknowledgement -- which is also what makes the retry
        possible, since a dropped command is otherwise silent.
        """
        hz = int(hz)
        for _ in range(attempts):
            self.send_command("s%dh" % hz)
            if not self.running:
                # Nobody is parsing banners yet, so there is nothing to confirm against.
                return False
            deadline = time.time() + 0.6
            while time.time() < deadline:
                if self.stream_hz == hz:
                    return True
                time.sleep(0.02)
        return False

    def set_baud(self, baud, rate=None):
        """Move both ends of the link to `baud`, returning the rate settled on.

        Serial here is a real UART bridged by the CMSIS-DAP probe, not USB CDC, so baud
        is the wire speed and both ends must agree. The board switches as soon as it sees
        the terminator, which means the host has to follow immediately and blind -- there
        is no acknowledgement possible at the old rate.
        """
        baud = int(baud)
        limit = safe_rate_for(baud)
        target = limit if rate is None else min(int(rate), limit)

        # Rate first, and only then baud. The reverse order leaves the board briefly
        # streaming more bytes than the new wire can carry, which stalls it rather than
        # merely dropping samples (see LINK_BUDGET).
        if target != self.stream_hz:
            if not self.set_rate(target):
                raise SourceError(
                    "Board did not accept %d Hz, so the baud change was not attempted."
                    % target
                )
            time.sleep(0.2)

        if baud == self.baud:
            return self.baud, target

        # No verification is possible here: the board switches on the terminator, so its
        # reply would arrive at a baud we are no longer listening at.
        self.send_command("b%d\n" % baud)
        landed = self._reconnect(baud)
        return landed, self.stream_hz or target

    def _reconnect(self, wanted):
        """Reopen the port after a baud change, returning the baud actually reached.

        The 'b' command is as droppable as any other (see send_command), and a dropped one
        leaves the board where it was. So rather than trust the switch, look for the board
        at the new rate and fall back to sweeping -- which turns the worst case from "reset
        the board" into "nothing happened".
        """
        was_running = self.running
        if was_running:
            # Base stop() only joins the reader thread; SerialSource.stop() would also
            # close the port, which we want to do ourselves under the write lock.
            _ThreadedSource.stop(self)

        with self._write_lock:
            if self._serial is not None:
                try:
                    self._serial.close()
                except Exception:
                    pass
                self._serial = None

        # The sketch waits 10 ms between Serial.end() and Serial.begin(); leave margin for
        # the probe to reprogram its side of the bridge too.
        time.sleep(0.3)
        previous = self.baud

        order = [wanted, previous] + [b for b in BAUD_CHOICES if b not in (wanted, previous)]
        for baud in order:
            try:
                self._open_at(baud, probe=baud != wanted)
            except SourceError:
                continue
            # A clean switch is not a failure, even if the old reader thread recorded one.
            self.error = None
            if was_running:
                self.start()
                self._refresh_banner()
            return baud

        raise SourceError(
            "Lost the board after switching to %d baud, and could not find it at any "
            "other rate. Reset it to return to %d." % (wanted, previous)
        )

    def _run(self):
        while not self._stop.is_set():
            # Bounded, so stopping stays responsive and a baud mismatch cannot wedge the
            # reader the way readline() would -- see _read_lines().
            for line in self._read_lines(0.25):
                self._consume_line(line)

    def stop(self):
        _ThreadedSource.stop(self)
        if self._serial is not None:
            try:
                self._serial.close()
            except Exception:
                pass

    def describe(self):
        return "serial %s @ %d%s" % (
            self.port,
            self.baud,
            " (%s)" % self.banner if self.banner else "",
        )


# ---------------------------------------------------------------------------
# Attached to a headless logger
# ---------------------------------------------------------------------------


class StreamSource(_ThreadedSource):
    """Reads the same CSV lines from a logger listening on TCP (see hub.py).

    Interchangeable with SerialSource from the outside: same queue, same counters, same
    describe(). That is the whole point -- a viewer cannot tell the difference, so
    attaching a dashboard to a running capture needs no capture code at all.

    Note the two kinds of loss are counted separately. `dropped` is this process falling
    behind its own socket; the logger's own drops arrive in the status dict and belong to
    the capture, not to the viewer.
    """

    def __init__(self, host="127.0.0.1", port=8765, max_queue=20000, timeout=3.0):
        _ThreadedSource.__init__(self, max_queue=max_queue)
        self.host = host
        self.port = port
        self.timeout = timeout
        # Last '# status {...}' the logger published: rows written, CSV path, burst state.
        self.status = {}
        self._socket = None

    def open(self):
        try:
            sock = socket.create_connection((self.host, self.port), timeout=self.timeout)
        except OSError as exc:
            raise SourceError(
                "Could not attach to %s:%d (%s). Start a capture there with "
                "'main.py', or name a different one."
                % (self.host, self.port, exc)
            )
        sock.settimeout(self.timeout)
        self._socket = sock
        self._rx = b""

        # The logger leads with its schema line and then its banner, so a mismatch is
        # caught before a single sample is parsed rather than showing up as a wall of
        # malformed rows. Both usually arrive in one packet; the loop keeps reading until
        # it has the banner too, so describe() is complete from the very first frame.
        deadline = time.time() + self.timeout
        seen_schema = False
        while time.time() < deadline:
            try:
                chunk = sock.recv(4096)
            except socket.timeout:
                break
            except OSError as exc:
                self.close()
                raise SourceError("Lost %s:%d while reading its header: %s"
                                  % (self.host, self.port, exc))
            if not chunk:
                break
            for line in self._split_lines(chunk):
                if not line:
                    continue
                if line.startswith("#") and not line.startswith("# "):
                    try:
                        check_schema(line, "Logger",
                                     "Both ends must run the same columns.py.")
                    except SourceError:
                        self.close()
                        raise
                    seen_schema = True
                    continue
                self._consume_line(line)
            if seen_schema and self.banner is not None:
                sock.settimeout(0.25)
                return self

        if seen_schema:
            # Schema checked out; a logger that has not printed a banner yet is odd but
            # not a reason to refuse to plot it.
            sock.settimeout(0.25)
            return self

        self.close()
        raise SourceError(
            "%s:%d did not send a Nicla header. Is that a main.py capture?"
            % (self.host, self.port)
        )

    def _consume_comment(self, line):
        if line.startswith("# status "):
            try:
                self.status = json.loads(line[len("# status "):])
            except ValueError:
                pass
            return
        _ThreadedSource._consume_comment(self, line)

    def _run(self):
        while not self._stop.is_set():
            try:
                chunk = self._socket.recv(4096)
            except socket.timeout:
                # Short by design so stopping stays responsive; silence is not an error,
                # a logger between samples looks exactly like this.
                continue
            except OSError as exc:
                if self._stop.is_set():
                    return
                raise SourceError("Lost the logger at %s:%d: %s"
                                  % (self.host, self.port, exc))
            if not chunk:
                raise SourceError("The logger at %s:%d closed the connection."
                                  % (self.host, self.port))
            for line in self._split_lines(chunk):
                self._consume_line(line)

    def close(self):
        if self._socket is not None:
            try:
                self._socket.close()
            except OSError:
                pass
            self._socket = None

    def stop(self):
        _ThreadedSource.stop(self)
        self.close()

    def describe(self):
        return "stream %s:%d%s" % (
            self.host,
            self.port,
            " (%s)" % self.banner if self.banner else "",
        )


# ---------------------------------------------------------------------------
# Runtime control
# ---------------------------------------------------------------------------


class SerialControl(object):
    """What the plot's target-rate field is allowed to do to a SerialSource.

    Keeps the plot free of transport detail: it asks for a rate, gets a status string
    back, and reads the current settings off the properties.
    """

    def __init__(self, source):
        self._source = source

    @property
    def rate(self):
        return self._source.stream_hz

    @property
    def baud(self):
        return self._source.baud

    @property
    def max_rate(self):
        """Fastest rate the current baud can carry; faster buttons are disabled."""
        return safe_rate_for(self._source.baud)

    def set_rate(self, hz):
        if hz > self.max_rate:
            return "%d Hz needs more than %d baud" % (hz, self.baud)
        try:
            ok = self._source.set_rate(hz)
        except SourceError as exc:
            return "rate failed: %s" % exc
        return "rate -> %d Hz" % hz if ok else "rate failed: no reply from board"

    def set_baud(self, baud):
        if baud == self.baud:
            return "already at %d baud" % baud
        try:
            landed, rate = self._source.set_baud(baud)
        except SourceError as exc:
            return "baud failed: %s" % exc
        if landed != baud:
            return "baud failed: still at %d" % landed
        return "%d baud, %d Hz" % (landed, rate)


def create_source(args):
    """Build the source the CLI asked for.

    Serial only: StreamSource is constructed directly by webdash.py, which has no
    argparse namespace in common with the capture.
    """
    return SerialSource(
        port=args.port, baud=args.baud, autobaud=not args.no_autobaud
    )
