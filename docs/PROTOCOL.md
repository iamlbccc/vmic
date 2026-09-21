# VMIC Protocol v1

VMIC v1 is a minimal TCP streaming protocol used by the Android sender and the
Windows receiver. It is intentionally simple: one binary header followed by an
unframed PCM byte stream.

## Transport

- TCP, default port **18200**
- Receiver listens on `0.0.0.0`; sender (phone) connects out
- USB usage: `adb forward` / `adb reverse` tunnels the port over localhost
- Single client at a time; on disconnect the receiver returns to listening

## Header (11 bytes, little-endian)

| Offset | Size | Field        | Value            |
|-------:|-----:|--------------|------------------|
| 0      | 4    | magic        | ASCII `"VMIC"`   |
| 4      | 1    | version      | `0x01`           |
| 5      | 4    | sample_rate  | uint32 LE, e.g. `48000` |
| 6      | 1    | channels     | `1` (mono) or `2` (stereo) |
| 7      | 1    | bits         | `16`             |

```python
import struct
HEADER = struct.Struct("<4sBIBB")
HEADER.pack(b"VMIC", 1, 48000, 1, 16)
```

## Payload

- Continuous stream of signed 16-bit little-endian PCM samples
- **No frame boundaries, no timestamps** — the byte stream is played as it
  arrives (a jitter buffer on the receiver side absorbs network jitter)
- Default profile: 48000 Hz / mono / int16 ≈ 94 KB/s

## Session lifecycle

```
sender                                receiver
  | ---- TCP SYN ----------------------> |
  | ---- header (11 B) ----------------> |  validate magic/version/format
  | ---- PCM stream (unbounded) ------> |  jitter buffer -> WASAPI playback
  | <--- TCP FIN / RST ----------------  |  session stats, back to listening
```

- Receiver rejects unknown magic / version / bit depth / channel count with a
  `struct.error` (logged, connection closed)
- Sender (Android app) reconnects automatically every 3 s after a drop

## Compatibility rules

- Bumping `version` requires backward-compatible negotiation or a new magic
- New fields MUST be appended after `bits`; `version` increments on any
  wire-format change
- Future: Opus payloads (version 2) planned for WAN usage
