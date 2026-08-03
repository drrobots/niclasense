"""Data sources for the Nicla Sense ME stream.

Both SerialSource and BleSource run their I/O on a background thread and hand finished
samples to a queue.Queue. That keeps the main thread free for matplotlib, which on macOS
insists on owning it.

A sample is a tuple in COLUMNS order: (seq, t_ms, ax_g, ..., bsec_acc).
"""

import queue
import struct
import sys
import threading

from columns import (
    BLE_DEFAULT_NAME,
    BLE_SAMPLE_CHAR_UUID,
    BLE_SERVICE_UUID,
    BLE_STRUCT_FORMAT,
    COLUMNS,
    NICLA_USB_PIDS,
    NICLA_USB_VID,
    PARSERS,
)

BLE_FRAME_SIZE = struct.calcsize(BLE_STRUCT_FORMAT)


class SourceError(Exception):
    """Raised when a source cannot be opened or dies unrecoverably."""


class _ThreadedSource(object):
    """Shared plumbing: a reader thread, an output queue, and a stop flag."""

    def __init__(self, max_queue=20000):
        self.queue = queue.Queue(maxsize=max_queue)
        self._stop = threading.Event()
        self._thread = None
        self.error = None
        self.dropped = 0

    def start(self):
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

    def __init__(self, port=None, baud=115200, max_queue=20000):
        _ThreadedSource.__init__(self, max_queue=max_queue)
        self.port = port
        self.baud = baud
        self.banner = None
        self.malformed = 0
        self._serial = None

    def open(self):
        import serial

        if self.port is None:
            self.port = find_nicla_port()
            if self.port is None:
                raise SourceError(
                    "No Nicla serial port found. Plug the board in, or pass --port. "
                    "Run with --list-ports to see what is available."
                )
        try:
            self._serial = serial.Serial(self.port, self.baud, timeout=1.0)
        except serial.SerialException as exc:
            raise SourceError("Could not open %s: %s" % (self.port, exc))

        # Ask for the header so we can check the board's schema against ours.
        try:
            self._serial.reset_input_buffer()
            self._serial.write(b"h")
        except Exception:
            pass
        self._read_header()
        return self

    def _read_header(self):
        """Consume metadata lines until the column list arrives (or we give up)."""
        import time

        deadline = time.time() + 3.0
        while time.time() < deadline:
            raw = self._serial.readline()
            if not raw:
                continue
            line = raw.decode("utf-8", "replace").strip()
            if not line.startswith("#"):
                continue
            if line.startswith("# "):
                self.banner = line[2:]
                continue
            board_columns = tuple(line.lstrip("#").split(","))
            if board_columns != COLUMNS:
                raise SourceError(
                    "Board schema does not match columns.py.\n"
                    "  board:  %s\n  expected: %s\n"
                    "Reflash nicla_stream.ino, or update COLUMNS to match."
                    % (",".join(board_columns), ",".join(COLUMNS))
                )
            return
        # No header seen. The sketch only prints it at boot or on 'h', so a board that is
        # already mid-stream is fine -- carry on and let field-count validation catch a
        # genuine mismatch.

    def _run(self):
        expected = len(COLUMNS)
        while not self._stop.is_set():
            raw = self._serial.readline()
            if not raw:
                continue
            line = raw.decode("utf-8", "replace").strip()
            if not line or line.startswith("#"):
                continue
            fields = line.split(",")
            if len(fields) != expected:
                # Partial line, typically the first read after a reset.
                self.malformed += 1
                continue
            try:
                sample = tuple(parse(f) for parse, f in zip(PARSERS, fields))
            except ValueError:
                self.malformed += 1
                continue
            self._emit(sample)

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
# BLE
# ---------------------------------------------------------------------------


class BleSource(_ThreadedSource):
    """Subscribes to the sketch's BLE notify characteristic.

    Requires the sketch to be built with STREAM_BLE 1.
    """

    def __init__(self, name=BLE_DEFAULT_NAME, address=None, max_queue=20000):
        _ThreadedSource.__init__(self, max_queue=max_queue)
        self.name = name
        self.address = address
        self.malformed = 0
        self._loop = None

    def open(self):
        # Connection happens on the reader thread's event loop; nothing to do here.
        return self

    def _on_notify(self, _sender, data):
        if len(data) != BLE_FRAME_SIZE:
            self.malformed += 1
            return
        values = struct.unpack(BLE_STRUCT_FORMAT, bytes(data))
        # Everything after seq/t_ms travels as float32 for a uniform layout; restore the
        # integral columns so BLE rows match serial rows exactly.
        self._emit(tuple(parse(v) for parse, v in zip(PARSERS, values)))

    def _run(self):
        import asyncio

        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_until_complete(self._stream())
        finally:
            self._loop.close()

    async def _stream(self):
        import asyncio

        from bleak import BleakClient, BleakScanner

        address = self.address
        if address is None:
            device = await BleakScanner.find_device_by_filter(
                lambda d, ad: (d.name or "") == self.name
                or BLE_SERVICE_UUID.lower() in [u.lower() for u in ad.service_uuids],
                timeout=15.0,
            )
            if device is None:
                raise SourceError(
                    "No BLE device named %r found. Check the sketch was built with "
                    "STREAM_BLE 1, or pass --ble-address." % self.name
                )
            address = device

        async with BleakClient(address) as client:
            await client.start_notify(BLE_SAMPLE_CHAR_UUID, self._on_notify)
            while not self._stop.is_set():
                if not client.is_connected:
                    raise SourceError("BLE connection lost.")
                await asyncio.sleep(0.2)
            await client.stop_notify(BLE_SAMPLE_CHAR_UUID)

    def describe(self):
        return "BLE %s" % (self.address or self.name)


def create_source(args):
    """Build the source the CLI asked for."""
    if args.source == "ble":
        return BleSource(name=args.ble_name, address=args.ble_address)
    return SerialSource(port=args.port, baud=args.baud)
