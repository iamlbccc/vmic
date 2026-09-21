#!/usr/bin/env python3
"""vmic_rx - Windows-side receiver for the VMic project.

Listens for a TCP connection from the Android VMic app, validates the
VMIC header, and plays the incoming PCM stream into a playback device
(ideally "CABLE Input" of VB-CABLE, which exposes it as a microphone
via "CABLE Output").

Protocol (VMIC v1):
    11-byte header, little-endian:
        magic       4 bytes  "VMIC"
        version     1 byte   0x01
        sample_rate uint32
        channels    uint8    1 or 2
        bits        uint8    16
    followed by an unframed stream of int16 LE PCM samples.

Usage:
    python vmic_rx.py --list
    python vmic_rx.py                     # auto-detect CABLE Input, port 18200
    python vmic_rx.py --device 15 --port 18200 --prebuffer 120
    python vmic_rx.py --null              # no audio output (protocol testing)
"""

from __future__ import annotations

import argparse
import array
import socket
import struct
import sys
import threading
import time

MAGIC = b"VMIC"
VERSION = 1
HEADER = struct.Struct("<4sBIBB")
DEFAULT_PORT = 18200
BYTES_PER_SAMPLE = 2  # int16

CABLE_NAME_HINTS = ("cable input", "voicemeeter input")


# ---------------------------------------------------------------- utilities

def recv_exact(conn: socket.socket, n: int) -> bytes:
    buf = bytearray()
    while len(buf) < n:
        chunk = conn.recv(n - len(buf))
        if not chunk:
            raise ConnectionError("connection closed during header read")
        buf.extend(chunk)
    return bytes(buf)


def fmt_addr(addr) -> str:
    return f"{addr[0]}:{addr[1]}"


def fmt_duration(total_secs: float) -> str:
    s = int(total_secs)
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    if h:
        return f"{h}h {m:02d}m"
    if m:
        return f"{m}m {sec:02d}s"
    return f"{sec}s"


# ------------------------------------------------------------- jitter buffer

class JitterBuffer:
    """Thread-safe sample buffer with prebuffering, overflow drop and
    underrun padding. Works on whole samples (frames * channels)."""

    def __init__(self, rate: int, channels: int, prebuffer_ms: int, max_ms: int):
        self.rate = rate
        self.channels = channels
        self.samples = bytearray()
        self.lock = threading.Lock()
        self._pre_target = max(1, int(rate * prebuffer_ms / 1000)) * channels
        self._max_samples = max(2, int(rate * max_ms / 1000)) * channels
        self._primed = False
        self.underruns = 0
        self.overflows = 0

    def put(self, pcm: bytes) -> None:
        with self.lock:
            self.samples.extend(pcm)
            if len(self.samples) > self._max_samples * BYTES_PER_SAMPLE:
                drop = (len(self.samples) - self._pre_target * BYTES_PER_SAMPLE) & ~1
                del self.samples[:drop]
                self.overflows += 1

    def take(self, frames: int) -> bytes:
        need = frames * self.channels * BYTES_PER_SAMPLE
        with self.lock:
            avail = len(self.samples)
            if not self._primed:
                if avail < self._pre_target * BYTES_PER_SAMPLE:
                    return b"\x00" * need
                self._primed = True
            if avail < need:
                self.underruns += 1
                data = bytes(self.samples)
                del self.samples[:]
                return data + b"\x00" * (need - len(data))
            data = bytes(self.samples[:need])
            del self.samples[:need]
            return data

    def buffered_ms(self) -> float:
        with self.lock:
            return len(self.samples) / (BYTES_PER_SAMPLE * max(1, self.channels) * self.rate) * 1000.0


# --------------------------------------------------------------------- sinks

class NullSink:
    """Consumes audio without output; used for protocol testing."""

    def __init__(self, rate: int, channels: int):
        self.rate = rate
        self.channels = channels
        self.frames = 0

    def put(self, pcm: bytes) -> None:
        self.frames += len(pcm) // (BYTES_PER_SAMPLE * self.channels)

    def close(self) -> None:
        pass

    def buffered_ms(self) -> float:
        return 0.0

    @property
    def underruns(self) -> int:
        return 0

    @property
    def overflows(self) -> int:
        return 0


class PortAudioSink:
    """Plays PCM into a sounddevice (PortAudio/WASAPI) output device."""

    def __init__(self, device: int | None, rate: int, channels: int, prebuffer_ms: int):
        import numpy as np
        import sounddevice as sd

        self.rate = rate
        self.channels = channels
        self.jb = JitterBuffer(rate, channels, prebuffer_ms, max_ms=500)

        def callback(outdata, frames, time_info, status):
            data = self.jb.take(frames)
            block = np.frombuffer(data, dtype="<i2").reshape(frames, channels)
            outdata[:] = block

        self.stream = sd.OutputStream(
            device=device,
            samplerate=rate,
            channels=channels,
            dtype="int16",
            blocksize=0,
            callback=callback,
        )
        self.stream.start()

    def put(self, pcm: bytes) -> None:
        self.jb.put(pcm)

    def close(self) -> None:
        try:
            self.stream.stop()
            self.stream.close()
        except Exception:
            pass

    def buffered_ms(self) -> float:
        return self.jb.buffered_ms()

    @property
    def underruns(self) -> int:
        return self.jb.underruns

    @property
    def overflows(self) -> int:
        return self.jb.overflows


# ---------------------------------------------------------------------- hooks

class ConsoleHook:
    """Default event sink: prints to stdout like the classic CLI."""

    def listening(self, bind: str, port: int) -> None:
        print(f"[*] VMic receiver listening on {bind}:{port}")

    def device(self, idx: int, name: str) -> None:
        print(f"[*] Writing audio into playback device #{idx}: {name}")
        print("[*] Apps should select 'CABLE Output (VB-Audio Virtual Cable)' as microphone")

    def no_device(self) -> None:
        print("[!] 'CABLE Input' not found; falling back to system default output.")
        print("[!] Install VB-CABLE or pass --device. See --list.")

    def connected(self, addr: str) -> None:
        print(f"[+] Client connected: {addr}")

    def header(self, rate: int, channels: int) -> None:
        print(f"[+] Header OK: {rate} Hz, {channels} ch, 16 bit PCM")

    def stats(self, level_pct, buffered_ms, underruns, overflows, elapsed_s) -> None:
        width = 20
        filled = int(level_pct / 100 * width)
        bar = "#" * filled + "." * (width - filled)
        print(
            f"\r    {bar}  {fmt_duration(elapsed_s):>7}  buffer {buffered_ms:4.0f}ms"
            f"  underruns {underruns}",
            end="", flush=True,
        )

    def disconnected(self, addr: str, reason: str, elapsed_s: float, kib: float) -> None:
        print(f"\n[-] Client {addr} disconnected: {reason}")
        print(f"[*] Session stats: {kib:.1f} KiB, {fmt_duration(elapsed_s)} of audio")

    def error(self, addr: str, message: str) -> None:
        print(f"\n[-] Error with {addr}: {message}")


# --------------------------------------------------------------- device utils

def find_cable_input() -> int | None:
    """Return device id whose name looks like a VB-CABLE playback input.
    Prefers the WASAPI incarnation for lower latency."""
    import sounddevice as sd

    fallback = None
    for idx, dev in enumerate(sd.query_devices()):
        if dev.get("max_output_channels", 0) > 0:
            name = dev["name"].lower()
            if any(h in name for h in CABLE_NAME_HINTS):
                try:
                    host = sd.query_hostapis(dev["hostapi"])["name"].lower()
                except Exception:
                    host = ""
                if "wasapi" in host:
                    return idx
                if fallback is None:
                    fallback = idx
    return fallback


def list_devices() -> None:
    import sounddevice as sd

    print(sd.query_devices())
    cable = find_cable_input()
    if cable is not None:
        print(f"\n>>> Auto-detected CABLE Input as device {cable}: "
              f"{sd.query_devices(cable)['name']}")
    else:
        print("\n>>> No 'CABLE Input' device found. Is VB-CABLE installed?")


# ---------------------------------------------------------------------- serve

def serve(args, hook: ConsoleHook | None = None) -> None:
    hook = hook or ConsoleHook()
    device: int | None = args.device
    if not args.null and device is None:
        device = find_cable_input()
        if device is None:
            hook.no_device()
    if not args.null and device is not None:
        import sounddevice as sd
        hook.device(device, sd.query_devices(device)["name"])

    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind((args.bind, args.port))
    srv.listen(1)
    hook.listening(args.bind, args.port)

    while True:
        conn, addr = srv.accept()
        try:
            handle_client(conn, addr, args, device, hook)
        except (ConnectionError, OSError):
            pass
        except struct.error as exc:
            hook.error(fmt_addr(addr), f"bad header: {exc}")
        except Exception as exc:
            hook.error(fmt_addr(addr), repr(exc))
        finally:
            conn.close()


def handle_client(conn, addr, args, device, hook) -> None:
    conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    raw = recv_exact(conn, HEADER.size)
    magic, version, rate, channels, bits = HEADER.unpack(raw)
    if magic != MAGIC:
        raise struct.error(f"bad magic {magic!r}")
    if version != VERSION:
        raise struct.error(f"unsupported version {version}")
    if bits != 16:
        raise struct.error(f"unsupported bit depth {bits}")
    if channels not in (1, 2):
        raise struct.error(f"unsupported channel count {channels}")
    if not 8000 <= rate <= 192000:
        raise struct.error(f"implausible sample rate {rate}")

    hook.connected(fmt_addr(addr))
    hook.header(rate, channels)

    sink = NullSink(rate, channels) if args.null else \
        PortAudioSink(device, rate, channels, args.prebuffer_ms)

    total_bytes = 0
    peak = 0
    start = time.monotonic()
    last_stats = start

    try:
        while True:
            data = conn.recv(65536)
            if not data:
                raise ConnectionError("client closed stream")
            sink.put(data)
            total_bytes += len(data)
            samples = array.array("h")
            samples.frombytes(data[: len(data) - len(data) % 2])
            if samples:
                p = max(abs(s) for s in samples)
                if p > peak:
                    peak = p
            now = time.monotonic()
            if now - last_stats >= 0.5:
                hook.stats(
                    level_pct=peak * 100 / 32768,
                    buffered_ms=sink.buffered_ms(),
                    underruns=sink.underruns,
                    overflows=sink.overflows,
                    elapsed_s=now - start,
                )
                peak = 0
                last_stats = now
    finally:
        sink.close()
        elapsed = time.monotonic() - start
        kib = total_bytes / 1024
        hook.disconnected(fmt_addr(addr), "client closed stream", elapsed, kib)


# ---------------------------------------------------------------------- main

def main() -> int:
    for stream in (sys.stdout, sys.stderr):
        if stream is not None and hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(description="VMic Windows receiver")
    ap.add_argument("--port", type=int, default=DEFAULT_PORT,
                    help=f"TCP port to listen on (default {DEFAULT_PORT})")
    ap.add_argument("--bind", default="0.0.0.0", help="address to bind")
    ap.add_argument("--device", type=int, default=None,
                    help="output device id (see --list); default: auto-detect CABLE Input")
    ap.add_argument("--prebuffer", dest="prebuffer_ms", type=int, default=120,
                    help="jitter buffer target before playback starts, ms (default 120)")
    ap.add_argument("--list", action="store_true", help="list audio devices and exit")
    ap.add_argument("--null", action="store_true",
                    help="consume stream without audio output (testing)")
    args = ap.parse_args()

    if args.list:
        list_devices()
        return 0
    try:
        serve(args)
    except KeyboardInterrupt:
        print("\n[*] Bye.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
