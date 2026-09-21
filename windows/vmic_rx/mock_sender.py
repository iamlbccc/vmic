#!/usr/bin/env python3
"""mock_sender - protocol test client for vmic_rx.

Sends a sine wave as a VMIC v1 stream (same protocol as the Android app).
Use it to verify the receiver without a phone:

    python vmic_rx.py --null &
    python mock_sender.py 127.0.0.1 18200 --seconds 5 --freq 440
"""

from __future__ import annotations

import argparse
import math
import socket
import struct
import sys
import time

sys.path.insert(0, __file__.rsplit("/", 1)[0])
from vmic_rx import HEADER, MAGIC, VERSION  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("host")
    ap.add_argument("port", type=int)
    ap.add_argument("--seconds", type=float, default=5.0)
    ap.add_argument("--freq", type=float, default=440.0)
    ap.add_argument("--rate", type=int, default=48000)
    ap.add_argument("--channels", type=int, default=1, choices=(1, 2))
    args = ap.parse_args()

    chunk_frames = args.rate // 50  # 20 ms
    total_frames = int(args.seconds * args.rate)

    phase = 0.0
    step = 2.0 * math.pi * args.freq / args.rate

    sock = socket.create_connection((args.host, args.port), timeout=5)
    sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    sock.sendall(HEADER.pack(MAGIC, VERSION, args.rate, args.channels, 16))
    print(f"[*] Connected to {args.host}:{args.port}, "
          f"streaming {args.seconds:.1f}s of {args.freq:.0f} Hz sine")

    sent = 0
    start = time.monotonic()
    for _ in range(0, total_frames, chunk_frames):
        n = min(chunk_frames, total_frames - sent)
        fmt = f"<{n * args.channels}h"
        samples = []
        for _ in range(n):
            s = int(20000 * math.sin(phase))
            phase += step
            samples.extend([s] * args.channels)
        sock.sendall(struct.pack(fmt, *samples))
        sent += n
        # real-time pacing with drift compensation
        target = sent / args.rate
        delay = target - (time.monotonic() - start)
        if delay > 0:
            time.sleep(delay)

    sock.close()
    print(f"[*] Sent {sent} frames ({sent / args.rate:.1f}s) "
          f"in {time.monotonic() - start:.1f}s wall time")
    return 0


if __name__ == "__main__":
    sys.exit(main())
