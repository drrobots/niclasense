"""Keep the capture (or the dashboard) running, under Windows, with nobody watching.

Both installed programs are ordinary entry points from `python/` -- this adds the two
things a service needs that a hand-run capture does not, and it lives in `packaging/`
rather than in the app because both are consequences of Windows rather than of the project.

**Restarting.** `main.py` is entitled to exit. It exits 1 when the board is unplugged, and
at boot it will usually exit before the first sample because the service starts before USB
enumeration finishes -- so the first attempt failing is the normal path, not the exception.
The service manager can restart a service that exits non-zero, but it treats a zero exit as
a deliberate stop, and `main.py` has paths that end cleanly without the operator having
asked for anything. Supervising here rather than leaving it to the SCM means one rule for
both, with a backoff this file can state plainly.

**Streams.** `pythonw.exe`, which is what makes the dashboard invisible at login, leaves
`sys.stdout` and `sys.stderr` set to None. `print()` tolerates that; `sys.stderr.write()`,
which is how both programs report progress, raises AttributeError on the first status line.
So the streams are pointed at a file before anything is imported that might use them. The
capture runs under `python.exe` instead, where the service wrapper supplies real pipes, but
it goes through the same code so there is one thing to reason about.

Stopping is somebody else's business: the service wrapper terminates this process tree and
the logon task ends at logoff. Both look like a signal or a kill, and the flag below is what
keeps that from being read as a crash worth restarting.
"""

import os
import signal
import sys
import time

# Backoff between restarts. The first retry is quick because the overwhelmingly common
# cause is the board not being enumerated yet, which resolves in seconds; the ceiling is
# there so a board that is genuinely absent for a month costs a log line a minute rather
# than a restart a second.
FIRST_DELAY = 5.0
MAX_DELAY = 60.0

# A run that lasted at least this long is treated as having worked, so the next failure
# starts its backoff from the beginning again. Without it, a capture that runs happily for
# three weeks and then loses the board would come back with a minute of delay for no reason.
STABLE_RUN = 300.0

_stopping = False


def _request_stop(_signum=None, _frame=None):
    global _stopping
    _stopping = True


def install_signal_handlers():
    """Catch the stop signals that reach a Python process on Windows.

    SIGBREAK is the one that matters -- it is what a console Ctrl-Break maps to and what
    some service wrappers send first -- and it does not exist off Windows, hence the
    getattr. SIGTERM and SIGINT are caught for the same reason: any of them means somebody
    asked, and asked means do not restart.
    """
    for name in ("SIGTERM", "SIGINT", "SIGBREAK"):
        number = getattr(signal, name, None)
        if number is not None:
            try:
                signal.signal(number, _request_stop)
            except (ValueError, OSError):
                # Not the main thread, or a signal this platform will not let us take.
                # Losing one of the three is not worth failing the service over.
                pass


def redirect_streams(path):
    """Point stdout and stderr at `path`, appending, line buffered.

    Line buffered because the file is the only account of what a service did, and a crash
    that loses the last block of it loses precisely the part worth reading. Both streams go
    to one file on purpose: the progress line and the error that ended it belong together
    and interleaving them is how you can tell how far it got.
    """
    directory = os.path.dirname(os.path.abspath(path))
    if directory and not os.path.isdir(directory):
        os.makedirs(directory)
    handle = open(path, "a", buffering=1)
    sys.stdout = handle
    sys.stderr = handle
    return handle


def stamp():
    return time.strftime("%Y-%m-%dT%H:%M:%S")


# Granularity of the backoff wait. Fine enough that a stop is acted on promptly, coarse
# enough to be free.
SLEEP_SLICE = 0.25


def sleep_until_stopped(seconds, slice_=SLEEP_SLICE):
    """Wait, but wake up if a stop is requested.

    A plain time.sleep will not do, and the reason is easy to miss: since Python 3.5 a
    signal handler does not cut a sleep short -- the handler runs and the sleep then
    resumes for its full term. Most of a crash-looping service's life is spent in this
    wait, so a stop would usually arrive mid-sleep and be sat on for up to a minute, well
    past the service manager's stop timeout, and the service would be killed rather than
    stopped. A killed capture loses whatever the CSV had buffered.
    """
    end = time.time() + seconds
    while not _stopping:
        remaining = end - time.time()
        if remaining <= 0:
            return
        time.sleep(min(slice_, remaining))


def run_forever(entry, argv, name="capture", clock=time.time, sleep=sleep_until_stopped,
                first_delay=FIRST_DELAY, max_delay=MAX_DELAY, max_runs=0):
    """Call `entry(argv)` until asked to stop, backing off between attempts.

    `entry` is `main.main` or `webdash.main`: it returns an exit code and is expected to
    block for as long as it works. Anything it raises is caught and treated as a failed run,
    because an unhandled exception in the capture is exactly the case where restarting is
    the right answer and a traceback into a service log is the wrong one -- the traceback is
    printed either way, it just does not end the service.

    `max_runs` bounds the loop for the tests. Zero, the installed value, means forever.
    """
    delay = first_delay
    runs = 0
    while not _stopping:
        started = clock()
        runs += 1
        try:
            code = entry(argv)
        except SystemExit as exc:
            code = exc.code if isinstance(exc.code, int) else 1
        except KeyboardInterrupt:
            # A stop that arrived as a signal the entry point handled first. Same meaning
            # as the flag: somebody asked.
            _request_stop()
            code = 0
        except Exception:  # noqa: BLE001 -- deliberately everything; see the docstring
            import traceback
            traceback.print_exc()
            code = 1

        if _stopping:
            sys.stdout.write("%s %s: stopping as asked\n" % (stamp(), name))
            return 0
        if max_runs and runs >= max_runs:
            return code

        lasted = clock() - started
        if lasted >= STABLE_RUN:
            delay = first_delay
        sys.stdout.write(
            "%s %s: exited %s after %.0fs; restarting in %.0fs\n"
            % (stamp(), name, code, lasted, delay)
        )
        sleep(delay)
        delay = min(delay * 2, max_delay)
    # Reached when the stop arrived during the backoff rather than during a run -- a real
    # case, since most of a crash-looping service's life is spent in that sleep. Saying so
    # is what distinguishes it in the log from the process having been killed outright.
    sys.stdout.write("%s %s: stopping as asked\n" % (stamp(), name))
    return 0


class UsageError(Exception):
    """A command line this cannot act on."""


def split_args(argv):
    """`(mode, log_path, rest)` from `capture|dashboard [--log PATH] [...]`.

    Hand-written rather than argparse because everything that is not `--log` has to survive
    untouched -- argparse would need to be told about every flag main.py and webdash.py
    have between them, which is a third list to keep in step with the other two. The way to
    work out why a service is not capturing is to run its exact command line in a console,
    and that only stays true while this passes the rest through.
    """
    argv = list(argv)
    if not argv or argv[0] not in ("capture", "dashboard"):
        raise UsageError("supervise.py capture|dashboard [--log PATH] [args...]")
    mode = argv.pop(0)

    log_path = None
    if "--log" in argv:
        index = argv.index("--log")
        if index + 1 >= len(argv):
            raise UsageError("--log needs a path")
        log_path = argv[index + 1]
        del argv[index:index + 2]

    return mode, log_path, argv


def main(argv=None):
    """`supervise.py capture|dashboard [--log PATH] [...]`."""
    try:
        mode, log_path, argv = split_args(sys.argv[1:] if argv is None else argv)
    except UsageError as exc:
        # sys.stderr can be None here, under pythonw with nothing set up yet, and a usage
        # error is the one failure that happens before there is anywhere to report it.
        if sys.stderr is not None:
            sys.stderr.write("usage: %s\n" % exc)
        return 2

    # Before the entry point is imported, not just before it is called: an import that
    # writes a warning would hit the same None stream.
    if log_path:
        redirect_streams(log_path)
    elif sys.stderr is None:
        # pythonw with no --log. Nowhere to report anything, including this, but a program
        # whose every status write raises is worse than a silent one.
        sys.stdout = sys.stderr = open(os.devnull, "w")

    install_signal_handlers()

    sys.stdout.write("%s %s: supervisor starting (pid %d)\n"
                     % (stamp(), mode, os.getpid()))
    if mode == "capture":
        import main as capture
        return run_forever(capture.main, argv, name="capture")
    import webdash
    return run_forever(webdash.main, argv, name="dashboard")


if __name__ == "__main__":
    sys.exit(main())
