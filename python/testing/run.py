#!/usr/bin/env python3
"""Run the test suite.

    python testing/run.py                 # everything
    python testing/run.py decimator hub   # just those modules
    python testing/run.py -v              # name each test as it runs

A runner rather than a bare `python -m unittest discover` line in the README for two
reasons. It buffers stdout, which matters more here than in most suites: test_capture.py
runs main.py for real, and a capture prints its progress, its CSV path and its decimator
summary on the way past -- pages of it, interleaved from several threads, with the actual
result somewhere underneath. Buffered, that output is kept and shown only for the tests
that fail, which is when it is worth reading. And it fixes the working directory, so the
suite runs the same way from `python/`, from the repo root, or from an editor.

There is no dependency to install. The suite is stdlib unittest, deliberately: the project
ships pyserial and nothing else, and a test runner that has to be
installed before the tests can be run is a step between a change and knowing whether it
worked.
"""

import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
PYTHON_DIR = os.path.dirname(HERE)

# The suite imports the modules under test by bare name (`import main`), the way the
# programs themselves do, so python/ has to be the working directory and on the path.
os.chdir(PYTHON_DIR)
if PYTHON_DIR not in sys.path:
    sys.path.insert(0, PYTHON_DIR)
if HERE not in sys.path:
    sys.path.insert(0, HERE)


def main(argv):
    verbose = "-v" in argv or "--verbose" in argv
    names = [arg for arg in argv if not arg.startswith("-")]

    loader = unittest.TestLoader()
    if names:
        suite = loader.loadTestsFromNames(
            ["test_%s" % name if not name.startswith("test_") else name for name in names]
        )
    else:
        suite = loader.discover(start_dir=HERE, pattern="test_*.py", top_level_dir=HERE)

    runner = unittest.TextTestRunner(
        verbosity=2 if verbose else 1,
        # Captures what the code under test prints and replays it only for failures.
        buffer=True,
    )
    return 0 if runner.run(suite).wasSuccessful() else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
