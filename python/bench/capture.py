"""Capture raw bytes from a running board and report line-length / rate statistics.

Works against nicla_stream (or anything else emitting CSV lines), and is the quickest
way to confirm a firmware change actually holds its rate:

    python bench/capture.py [port] [baud] [seconds]

Reports bytes/line, achieved line rate, link utilisation, and whether the sequence
counter skipped -- a dropped sample shows up as a non-consecutive step.
"""
import os
import statistics
import sys
import time

import serial

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from sources import find_nicla_port

port = sys.argv[1] if len(sys.argv) > 1 else find_nicla_port()
baud = int(sys.argv[2]) if len(sys.argv) > 2 else 1000000
secs = float(sys.argv[3]) if len(sys.argv) > 3 else 10.0
if port is None:
    sys.exit("No Nicla serial port found. Plug the board in, or pass one as argv[1].")

s = serial.Serial(port, baud, timeout=0.5)
time.sleep(0.3)
s.reset_input_buffer()

buf = bytearray()
t0 = time.time()
while time.time() - t0 < secs:
    chunk = s.read(4096)
    if chunk:
        buf.extend(chunk)
elapsed = time.time() - t0
s.close()

total = len(buf)
lines = buf.split(b"\n")
# drop first and last (likely partial)
lines = lines[1:-1]
data = [l for l in lines if l and not l.startswith(b"#")]
lengths = [len(l) + 1 for l in data]  # +1 for the \n; \r is already in l if present

print("port=%s baud=%d elapsed=%.2fs" % (port, baud, elapsed))
print("bytes=%d  byte_rate=%.1f B/s  = %.1f%% of %d baud (8N1 => %d B/s)"
      % (total, total / elapsed, 100.0 * (total / elapsed) / (baud / 10.0), baud, baud // 10))
print("data lines=%d  line_rate=%.2f Hz" % (len(data), len(data) / elapsed))
if lengths:
    print("line bytes: min=%d mean=%.1f median=%d max=%d"
          % (min(lengths), statistics.mean(lengths), statistics.median(lengths), max(lengths)))
    print("line ending CRLF: %s" % (data[0].endswith(b"\r"),))
    print("sample line: %r" % (data[len(data) // 2][:200],))

# sequence continuity
seqs = []
for l in data:
    try:
        seqs.append(int(l.split(b",")[0]))
    except Exception:
        pass
if len(seqs) > 2:
    gaps = sum(1 for a, b in zip(seqs, seqs[1:]) if b != a + 1)
    print("seq range %d..%d, non-consecutive steps=%d, expected=%d received=%d"
          % (seqs[0], seqs[-1], gaps, seqs[-1] - seqs[0] + 1, len(seqs)))
