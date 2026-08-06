"""The seam: one queue in, a list of plain callables out.

Small enough to read in a minute, and load-bearing enough that getting it wrong would be
subtle rather than loud. The two properties worth pinning down are that sinks are
genuinely independent -- the CSV writer must not be able to affect what a plot sees -- and
that decimation applies to the file alone. The second one is easy to break by moving the
decimator one line up, and the symptom is a dashboard that stutters at 5 Hz while the
capture insists it is receiving 200.
"""

import unittest

import support
from decimator import AdaptiveDecimator
from pipeline import attached_status, capture_status, make_drain, make_log_sink


class FakeSource(object):
    """Just the queue and the counters; make_drain touches nothing else."""

    def __init__(self, samples=()):
        import queue

        self.queue = queue.Queue()
        self.dropped = 0
        self.malformed = 0
        self.error = None
        self.running = True
        for one in samples:
            self.queue.put(one)

    def describe(self):
        return "fake"


class Recorder(object):
    def __init__(self):
        self.samples = []

    def __call__(self, sample):
        self.samples.append(sample)


class Drain(unittest.TestCase):
    def test_every_sink_sees_every_sample(self):
        samples = support.ramp(25)
        source = FakeSource(samples)
        sinks = [Recorder(), Recorder(), Recorder()]
        moved = make_drain(source, sinks)()
        self.assertEqual(moved, len(samples))
        for sink in sinks:
            self.assertEqual(sink.samples, samples)

    def test_absent_sinks_are_dropped_not_called(self):
        """main.py passes None where there is no CSV, rather than branching at the call."""
        source = FakeSource(support.ramp(3))
        sink = Recorder()
        self.assertEqual(make_drain(source, [None, sink, None])(), 3)
        self.assertEqual(len(sink.samples), 3)

    def test_an_empty_queue_moves_nothing(self):
        self.assertEqual(make_drain(FakeSource(), [Recorder()])(), 0)

    def test_a_backlog_cannot_starve_the_caller(self):
        """The drain is called from an animation timer; an unbounded loop there is a
        frozen window for as long as the backlog takes to clear."""
        source = FakeSource(support.ramp(12000))
        sink = Recorder()
        drain = make_drain(source, [sink])
        first = drain()
        self.assertEqual(first, 5000)
        self.assertEqual(drain() + drain(), 7000)

    def test_sink_order_is_the_order_given(self):
        order = []
        source = FakeSource(support.ramp(1))
        make_drain(source, [
            lambda s: order.append("a"), lambda s: order.append("b"),
        ])()
        self.assertEqual(order, ["a", "b"])


class LogSink(unittest.TestCase):
    class FakeLog(object):
        def __init__(self):
            self.rows = []

        def write(self, sample, burst=0):
            self.rows.append((sample, burst))

    def test_without_a_decimator_every_sample_is_written(self):
        log = self.FakeLog()
        samples = support.ramp(10)
        sink = make_log_sink(log, None)
        for one in samples:
            sink(one)
        self.assertEqual([row for row, _b in log.rows], samples)

    def test_with_a_decimator_only_the_file_is_thinned(self):
        """The plot's sink is a peer of this one and never sees the decimator at all."""
        log = self.FakeLog()
        plot = Recorder()
        source = FakeSource(support.ramp(2000, hz=200.0))
        drain = make_drain(source, [
            make_log_sink(log, AdaptiveDecimator(rate=5.0)), plot,
        ])
        drain()

        self.assertEqual(len(plot.samples), 2000)
        self.assertLess(len(log.rows), 100)
        self.assertGreater(len(log.rows), 40)

    def test_burst_rows_are_marked_for_the_writer(self):
        log = self.FakeLog()
        sink = make_log_sink(log, AdaptiveDecimator(rate=1.0, hold=1.0))
        for one in support.ramp(200, hz=200.0):
            sink(one)
        for one in support.ramp(200, hz=200.0, start_seq=200, start_ms=1000, ax_g=1.0):
            sink(one)
        self.assertTrue(any(burst for _row, burst in log.rows))


class Status(unittest.TestCase):
    class FakeLog(object):
        rows_written = 4123

    def test_capture_status_reports_the_whole_capture(self):
        source = FakeSource()
        source.dropped = 3
        source.malformed = 1
        decimator = AdaptiveDecimator(rate=5.0)
        status = capture_status(source, self.FakeLog(), decimator, "logs/x.csv")
        self.assertEqual(status["csv"], "logs/x.csv")
        self.assertEqual(status["rows"], 4123)
        self.assertEqual(status["dropped"], 3)
        self.assertEqual(status["malformed"], 1)
        self.assertEqual(status["log_rate"], 5.0)
        self.assertNotIn("viewers", status)

    def test_without_a_csv_the_row_count_is_zero_not_missing(self):
        """The capture tile reads these keys unconditionally."""
        status = capture_status(FakeSource(), None, None, "logs/x.csv")
        self.assertIsNone(status["csv"])
        self.assertEqual(status["rows"], 0)
        self.assertEqual(status["log_rate"], 0)

    def test_viewer_count_appears_only_with_a_hub(self):
        class FakeHub(object):
            clients = 2

        status = capture_status(FakeSource(), None, None, "x.csv", FakeHub())
        self.assertEqual(status["viewers"], 2)

    def test_attached_status_sums_loss_at_both_ends_but_keeps_them_apart(self):
        """The tile answers "is what I am looking at complete", and the honest answer is
        the sum -- but which end is struggling has to stay recoverable."""
        source = FakeSource()
        source.status = {"dropped": 10, "malformed": 2, "rows": 900, "csv": "logs/x.csv"}
        source.dropped = 5
        source.malformed = 1

        status = attached_status(source)()
        self.assertEqual(status["capture_dropped"], 10)
        self.assertEqual(status["link_dropped"], 5)
        self.assertEqual(status["dropped"], 15)
        self.assertEqual(status["malformed"], 3)
        self.assertEqual(status["rows"], 900)
        self.assertEqual(status["source"], "fake")

    def test_attached_status_does_not_mutate_what_the_capture_published(self):
        source = FakeSource()
        source.status = {"dropped": 10}
        source.dropped = 5
        attached_status(source)()
        self.assertEqual(source.status, {"dropped": 10})

    def test_attached_status_copes_with_a_capture_that_has_not_spoken_yet(self):
        source = FakeSource()
        source.status = {}
        status = attached_status(source)()
        self.assertEqual(status["dropped"], 0)
        self.assertEqual(status["malformed"], 0)


class NoRenderingDependency(unittest.TestCase):
    """pipeline.py is the seam, and a seam that imports a renderer is not one.

    It used to hold watch_source, which existed to close a matplotlib window when the
    source died -- carefully written to go through the plot object rather than pyplot so
    that this module needed no matplotlib. That went with the desktop dashboard; both
    remaining consumers poll source.error in their own loop instead. The check below is
    what the care was protecting, stated directly.
    """

    def test_importing_the_seam_pulls_in_no_renderer(self):
        import subprocess
        import sys

        code = (
            "import sys; import pipeline; "
            "print(any(m == 'matplotlib' or m.startswith('matplotlib.') for m in sys.modules))"
        )
        out = subprocess.check_output(
            [sys.executable, "-c", code], cwd=support.PYTHON_DIR
        )
        self.assertEqual(out.strip(), b"False")


if __name__ == "__main__":
    unittest.main()
