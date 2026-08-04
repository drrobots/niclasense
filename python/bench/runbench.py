"""Drive nicla_bench.ino and measure achieved throughput per encoding.

Flash nicla_bench/ first, then:

    python bench/runbench.py [baud] [seconds] [port]

Rates are derived from the board's own t_ms field, not from host arrival times.
Host arrival times overstate the rate badly here: the CMSIS-DAP bridge buffers, so a
saturated stream drains faster than the wire for the first seconds of a capture and the
naive measurement reports over 100% of link capacity.
"""
import os
import sys
import time

import serial

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from sources import find_nicla_port

BAUD = int(sys.argv[1]) if len(sys.argv) > 1 else 1000000
DUR = float(sys.argv[2]) if len(sys.argv) > 2 else 5.0
PORT = sys.argv[3] if len(sys.argv) > 3 else find_nicla_port()
if PORT is None:
    sys.exit("No Nicla serial port found. Plug the board in, or pass one as argv[3].")

# The nRF52 BAUDRATE register only hits discrete rates; 921600 is programmed as
# 0x0F000000, which is really 937500 bps. Report against the true wire rate.
WIRE = {921600: 937500, 1000000: 1000000, 460800: 468750, 230400: 234375}.get(BAUD, BAUD)
WIRE_BPS = WIRE / 10.0


def open_port():
    s = serial.Serial(PORT, BAUD, timeout=0.2)
    time.sleep(0.3)
    s.reset_input_buffer()
    return s


def text_command(s, cmd, wait=6.0):
    s.reset_input_buffer()
    s.write(cmd)
    t0 = time.time()
    buf = b""
    while time.time() - t0 < wait:
        buf += s.read(65536)
    for line in buf.split(b"\n"):
        # Profile lines can be prefixed by the sweep's own filler bytes.
        i = line.find(b"#")
        if i >= 0:
            print("   " + line[i:].strip().decode("utf-8", "replace"))


def collect(s, cmd, dur):
    s.write(b"0")
    time.sleep(0.3)
    s.reset_input_buffer()
    s.write(cmd)
    time.sleep(0.5)
    s.reset_input_buffer()
    buf = bytearray()
    t0 = time.time()
    while time.time() - t0 < dur:
        c = s.read(65536)
        if c:
            buf.extend(c)
    s.write(b"0")
    time.sleep(0.3)
    s.reset_input_buffer()
    return buf


def report(label, n, board_ms, nbytes, bytes_per, gaps, extra=""):
    if n < 2 or board_ms <= 0:
        print("%-22s INSUFFICIENT DATA" % label)
        return
    hz = (n - 1) * 1000.0 / board_ms
    bps = hz * bytes_per
    print("%-22s %8.1f Hz  %6.1f B/sample  %8.0f B/s  %5.1f%% wire  gaps=%s  %s"
          % (label, hz, bytes_per, bps, 100.0 * bps / WIRE_BPS, gaps, extra))


def measure_lines(s, cmd, label, dur=DUR, has_time=True):
    buf = collect(s, cmd, dur)
    lines = buf.split(b"\n")[1:-1]
    data = [l for l in lines if l and not l.startswith(b"#")]
    if len(data) < 2:
        print("%-22s NO DATA" % label)
        return
    sizes = [len(l) + 1 for l in data]
    bytes_per = sum(sizes) / len(data)
    seqs, times = [], []
    for l in data:
        f = l.split(b",")
        try:
            seqs.append(int(f[0]))
            if has_time:
                times.append(int(f[1]))
        except Exception:
            pass
    gaps = sum(1 for a, b in zip(seqs, seqs[1:]) if b != a + 1)
    counts = {}
    for l in data:
        k = l.count(b",") + 1
        counts[k] = counts.get(k, 0) + 1
    hist = " ".join("%dcol:%d" % (k, v) for k, v in sorted(counts.items()))
    if has_time and len(times) > 1:
        report(label, len(times), times[-1] - times[0], len(buf), bytes_per, gaps, hist)
    else:
        hz = len(data) / dur
        print("%-22s %8.1f Hz* %6.1f B/sample  %8.0f B/s* %5.1f%% wire  gaps=%s  %s"
              % (label, hz, bytes_per, hz * bytes_per,
                 100.0 * hz * bytes_per / WIRE_BPS, gaps, hist))


def measure_binary(s, cmd, label, dur=DUR):
    buf = collect(s, cmd, dur)
    SIZES = {0x01: 42, 0x02: 37}
    i = 0
    good = {0x01: 0, 0x02: 0}
    bad = 0
    seqs, times = [], []
    while i < len(buf) - 3:
        if buf[i] == 0xAA and buf[i + 1] == 0x55 and buf[i + 2] in SIZES:
            ln = SIZES[buf[i + 2]]
            if i + ln > len(buf):
                break
            fr = buf[i:i + ln]
            chk = 0
            for b in fr[2:ln - 1]:
                chk ^= b
            if chk == fr[ln - 1]:
                good[fr[2]] += 1
                if fr[2] == 0x01:
                    seqs.append(fr[3] | (fr[4] << 8))
                    times.append(int.from_bytes(fr[5:9], "little"))
                i += ln
                continue
            bad += 1
        i += 1
    n = good[0x01]
    if n < 2:
        print("%-22s NO FRAMES" % label)
        return
    gaps = sum(1 for a, b in zip(seqs, seqs[1:]) if ((b - a) & 0xFFFF) != 1)
    bytes_per = 42 + 37.0 * good[0x02] / n
    report(label, n, times[-1] - times[0], len(buf), bytes_per, gaps,
           "motion=%d env=%d badcrc=%d" % (n, good[0x02], bad))


s = open_port()
print("=== bench @ %d baud (nRF wire rate %d bps = %.0f B/s) ===" % (BAUD, WIRE, WIRE_BPS))
print("\n-- MCU cost profile (output discarded) --")
text_command(s, b"P", 8.0)
print("\n-- hub output rates --")
text_command(s, b"O", 6.0)

print("\n-- free-run throughput by encoding (rate from board t_ms) --")
measure_lines(s, b"L", "link blast (64B)", has_time=False)
measure_lines(s, b"A", "CSV full (27 col)")
measure_lines(s, b"B", "CSV ragged")
measure_lines(s, b"M", "CSV motion only")
measure_binary(s, b"C", "binary")
measure_binary(s, b"D", "binary + DMA")
measure_lines(s, b"E", "CSV full + DMA")
measure_lines(s, b"F", "CSV ragged + DMA")
measure_lines(s, b"G", "CSV motion + DMA")
s.close()
