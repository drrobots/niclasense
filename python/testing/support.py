"""Shared scaffolding for the tests.

Importing this first puts `python/` on the path, so a test module can `import columns` no
matter which directory the runner was started from. `testing/replay.py` does the same
thing for the same reason; the alternative is making `testing/` a package, which would
mean the tests could only ever be run one way.

The helpers below exist because almost every test needs the same two things: a plausible
27-column sample, and a TCP port nobody else is using.
"""

import csv
import os
import socket
import sys

TESTING_DIR = os.path.dirname(os.path.abspath(__file__))
PYTHON_DIR = os.path.dirname(TESTING_DIR)
REPO_DIR = os.path.dirname(PYTHON_DIR)

if PYTHON_DIR not in sys.path:
    sys.path.insert(0, PYTHON_DIR)

from columns import COLUMNS, INTEGER_COLUMNS  # noqa: E402

SKETCH = os.path.join(REPO_DIR, "nicla_stream", "nicla_stream.ino")

# Column positions used often enough that looking them up per call reads worse.
SEQ = COLUMNS.index("seq")
T_MS = COLUMNS.index("t_ms")


def sample(seq=0, t_ms=0, **overrides):
    """One sample tuple, correctly typed, with named columns overridden.

    Typed matters more than it looks: the integer columns really are Python ints all the
    way through the host, and a float slipped into one of them survives every stage until
    it reaches a CSV that reads `500.0` where the board wrote `500`, or a re-emitted wire
    line that the far end's int() parser rejects as malformed. Building samples through
    here rather than by hand keeps a test from proving something about data the board
    could never have produced.
    """
    values = []
    for i, name in enumerate(COLUMNS):
        if name == "seq":
            value = seq
        elif name == "t_ms":
            value = t_ms
        else:
            value = overrides.get(name, 0)
        values.append(int(value) if name in INTEGER_COLUMNS else float(value))
    unknown = set(overrides) - set(COLUMNS)
    if unknown:
        raise AssertionError("no such column(s): %s" % ", ".join(sorted(unknown)))
    return tuple(values)


def ramp(count, hz=200.0, start_seq=0, start_ms=0, **overrides):
    """`count` samples on an even grid at `hz`, sharing one set of overrides."""
    step = 1000.0 / hz
    return [
        sample(seq=start_seq + i, t_ms=int(round(start_ms + i * step)), **overrides)
        for i in range(count)
    ]


def free_port():
    """A port that was free a moment ago.

    Inherently a race, but binding to 0 and asking what we got is the only way to pick one
    without a registry, and the window is small enough that it has never mattered here.
    The socket is closed before the port is handed out, so the caller can bind it itself.
    """
    probe = socket.socket()
    try:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]
    finally:
        probe.close()


def assert_imports_only_stdlib(case, module):
    """Fail unless importing `module` reaches nothing outside the standard library.

    Two modules claim to be free of third-party weight -- tiles.py, which webhub.py serves
    to the browser as JSON, and pipeline.py, which is the seam both entry points meet at.
    The check used to be spelled "no matplotlib", which stopped meaning anything the day
    matplotlib left the requirements: an import of a package that is not installed cannot
    turn up in sys.modules, so the assertion passed by absence rather than by design.

    Site-packages is the discriminator rather than a list of module names. Every installed
    dependency lands there and nothing in the standard library does, which makes this
    independent of both the Python version and of what happens to be installed -- and it is
    exactly the line the Windows build draws, its bundled interpreter carrying pyserial and
    the stdlib and nothing else.
    """
    import subprocess

    code = (
        "import sys; import %s; "
        "print([m.__name__ for m in list(sys.modules.values()) "
        "if getattr(m, '__file__', None) and 'site-packages' in m.__file__])" % module
    )
    out = subprocess.check_output([sys.executable, "-c", code], cwd=PYTHON_DIR)
    case.assertEqual(out.strip(), b"[]")


def wait_for(predicate, timeout=5.0, interval=0.01):
    """Poll until `predicate()` is true, returning whether it became true.

    Threads and sockets make "has it happened yet" the most common question in this
    suite, and a fixed sleep long enough to be reliable on a loaded machine is long
    enough to make the suite tedious everywhere else.
    """
    import time

    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return predicate()


def write_capture(directory, started, minutes=5.0, moves=(), rate=1 / 60.0, name=None):
    """Write one fleet-shaped capture into `directory` and return its path.

    Real `AdaptiveDecimator` and real `CsvLogger`, driven with the numbers out of
    `packaging/nicla.conf`, so the file has everything the archive readers have to cope
    with: the trailing `burst` column, a row a minute of steady grid, full-rate runs where
    something moved, and the uneven spacing that comes of the grid restarting after each
    one. Synthesising the CSV by hand would produce a file that agrees with whatever the
    reader already believes.

    `moves` is a sequence of (start_s, duration_s) describing when to shake the board. They
    are the ground truth an episode test asserts against.

    `started` only names the file. The rows carry whatever wall clock the logger stamped
    them with, which is now -- call `restamp` to put them where the name says they are.
    """
    from decimator import AdaptiveDecimator
    from logger import CsvLogger

    if name is None:
        name = "nicla_%s.csv" % started.strftime("%Y%m%d_%H%M%S")
    path = os.path.join(directory, name)

    decimator = AdaptiveDecimator(rate=rate, hold=1.0, pre_roll=0.25, tau=0.5,
                                  burst_rate=200.0)
    log = CsvLogger(path, flush_every=1, mark_bursts=True)
    log.open()
    try:
        for i in range(int(200 * 60 * minutes)):
            t_ms = i * 5
            seconds = t_ms / 1000.0
            moving = any(at <= seconds < at + span for at, span in moves)
            one = sample(
                seq=i, t_ms=t_ms,
                ax_g=1.45 if moving else 0.8243,
                gx_dps=42.0 if moving else 0.0,
                temp_C=26.7 + i * 1e-5,
            )
            for row, is_burst in decimator.feed(one):
                log.write(row, burst=is_burst)
    finally:
        if getattr(log, "_handle", None) is not None:
            log._handle.close()
    return path


def restamp(path, started):
    """Rewrite a capture's host_iso from the board clock, beginning at `started`.

    Necessary rather than tidy. CsvLogger stamps host_iso with the wall clock at the moment
    it writes, and a fixture simulating five minutes of board time does it in a fifth of a
    second -- so every row lands inside the same instant and any test about time windows
    passes or fails for the wrong reason.

    Rebuilding the stamp from t_ms is also the more faithful thing: on a real capture the
    two track each other, because the rows are written as they arrive.
    """
    import datetime

    with open(path, "r", newline="") as handle:
        rows = list(csv.reader(handle))
    if len(rows) < 2:
        return path
    header, body = rows[0], rows[1:]
    t_ms_at = header.index("t_ms")
    base = int(body[0][t_ms_at])
    for row in body:
        offset = int(row[t_ms_at]) - base
        row[0] = (started + datetime.timedelta(milliseconds=offset)).isoformat(
            timespec="milliseconds"
        )
    with open(path, "w", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(header)
        writer.writerows(body)
    return path
