"""Wiring between a source and the things that consume it.

The seam the capture and the dashboard are built on: a source's queue on one side, a list
of plain callables on the other. Each sink takes one sample tuple and knows nothing about
the others -- the CSV writer, the plot's ring buffers and the socket hub are peers, and
adding a fourth would not touch any of the first three.

Nothing here imports an entry point. main.py and dashboard.py both import this; neither
imports the other, which is what keeps the dashboard genuinely independent of the logger
rather than independent-looking but coupled at the module level.
"""

import queue
import sys


def make_log_sink(log, decimator=None):
    """Return a sink that writes samples to the CSV, thinned if we are decimating."""

    if decimator is None:
        return log.write

    def sink(sample):
        for kept, is_burst in decimator.feed(sample):
            log.write(kept, is_burst)

    return sink


def make_drain(source, sinks):
    """Return a function that moves everything queued to each sink in turn.

    Sinks are plain callables taking one sample, which is what keeps the logger and the
    plot independent of each other: the logger's sink is the CSV, the plot's is its ring
    buffers, the hub's is every attached viewer, and no sink knows the others exist.

    Only the CSV sink is ever thinned. Decimation is about the size of the file on disk,
    not about how much the host is willing to look at, so plots -- local or attached --
    always see every sample.
    """

    sinks = tuple(s for s in sinks if s is not None)

    def drain():
        moved = 0
        # Bounded so a backlog can never starve the caller (the animation timer).
        while moved < 5000:
            try:
                sample = source.queue.get_nowait()
            except queue.Empty:
                break
            for sink in sinks:
                sink(sample)
            moved += 1
        return moved

    return drain


def capture_status(source, log, decimator, csv_path, hub=None):
    """The facts the capture tile shows, as a dict.

    Named rather than inlined because the headless logger publishes exactly this dict to
    attached viewers, so a remote plot and a local one report the same capture the same
    way.
    """
    status = {
        "source": source.describe(),
        "csv": csv_path if log is not None else None,
        "rows": log.rows_written if log is not None else 0,
        "dropped": source.dropped,
        "malformed": getattr(source, "malformed", 0),
        "log_rate": decimator.rate if decimator is not None else 0,
        "bursts": decimator.bursts if decimator is not None else 0,
        "bursting": decimator.bursting if decimator is not None else False,
    }
    if hub is not None:
        status["viewers"] = hub.clients
    return status


def attached_status(source):
    """The logger's published status, plus what only this process can know.

    Loss is counted at both ends and means different things -- the logger's rows never
    reached the CSV, ours never reached this window -- but the tile answers one question,
    "is what I am looking at complete", and the honest answer there is the sum. The two
    are kept as separate keys as well, and the exit summary reports this viewer's own
    count on its own, so which end is struggling stays recoverable.
    """

    def status():
        published = dict(source.status)
        capture_dropped = published.get("dropped", 0)
        published["source"] = source.describe()
        published["capture_dropped"] = capture_dropped
        published["link_dropped"] = source.dropped
        published["dropped"] = capture_dropped + source.dropped
        published["malformed"] = published.get("malformed", 0) + source.malformed
        return published

    return status


def watch_source(source, drain, plot):
    """Wrap drain so a source that dies closes the plot window instead of freezing it.

    A headless capture checks source.error on every pass of its own loop; under matplotlib
    there is no such loop -- plt.show() owns the thread -- so the animation callback is the
    only place left to check it from.
    """
    import matplotlib.pyplot as plt

    def guarded():
        moved = drain()
        if source.error is not None or not source.running:
            reason = source.error if source.error is not None else "source stopped"
            sys.stderr.write("\n%s\n" % reason)
            plt.close(plot.figure)
        return moved

    return guarded
