# VMic — Turn Your Android Phone into a Windows Microphone

**[简体中文](README.zh-CN.md)** | English

<p>
<img src="docs/assets/screenshot-windows-widget.png" alt="Windows desktop companion" width="480">
&nbsp;
<img src="docs/assets/screenshot-android.png" alt="Android app" width="160">
</p>

VMic captures your phone's microphone and streams it to Windows in real time over WiFi/USB, where it becomes a system-level virtual microphone through VB-CABLE — selectable in Zoom, Discord, OBS, Teams, WeChat and any other app. Turn a spare phone into a high-quality wireless microphone at zero cost.

- **End-to-end latency**: ~40–80ms over USB, ~100–200ms over WiFi
- **Android app**: foreground service with persistent notification keeps capturing in background / with screen off; auto-reconnects on drops
- **Windows companion**: anti-aliased translucent pill widget (status / dB level / 60s chart) + an animated tray icon whose equalizer bars dance with your voice
- **Protocol**: minimal 11-byte-header TCP PCM stream — [spec here](docs/PROTOCOL.md), trivially reimplementable in any language

---

## How It Works

```
┌──────────────┐   TCP PCM 48k/16bit/mono   ┌──────────────────┐   WASAPI  ┌────────────┐
│  Android App │ ─────────────────────────▶ │ vmic_panel.py    │ ───────▶ │ CABLE Input│
│  Foreground  │      (WiFi / USB adb)      │ receiver +       │          │ (VB-CABLE) │
│  Service     │                            │ jitter buffer    │          └─────┬──────┘
└──────────────┘                            │ + widget + tray  │                 │ driver
                                            └──────────────────┘                 ▼
                                                             Zoom/Meet/OBS ◀── CABLE Output (virtual mic)
```

> VB-CABLE is a free (donationware) virtual audio cable by
> [VB-Audio Software](https://vb-audio.com/Cable/). It provides a playback device pair:
> "CABLE Input" (write side) and "CABLE Output" (recording side = the virtual microphone).
> This playback+recording pair is how virtual microphones are universally exposed under
> the Windows audio architecture — every comparable solution (VoiceMeeter, paid VAC) works
> the same way.

## Quick Start

### Windows (one-time setup)

1. **Install VB-CABLE**: download `VBCABLE_Driver_Pack45.zip` from the
   [official site](https://vb-audio.com/Cable/), run Setup as administrator, **reboot**.
2. **Align sample rates**: Sound settings → playback device `CABLE Input` → Properties →
   Advanced → **48000 Hz**; set recording device `CABLE Output` to 48000 Hz as well
   (avoids Windows resampler artifacts).
3. **Install dependencies and launch**:
   ```powershell
   cd windows\vmic_rx
   python -m pip install -r requirements.txt
   start_panel.cmd        # no-console launch: pill widget + tray icon
   # or: python vmic_rx.py  # classic console mode
   ```

> To autostart on boot: `Win+R` → `shell:startup` → drop a shortcut to `start_panel.cmd`.

### Android

1. Open `android/` in Android Studio and run (minSdk 24), or grab the APK from Releases.
2. Enter your PC's LAN IP (phone and PC on the same WiFi); the app connects automatically
   and remembers the setting.
3. First launch requests: microphone, notifications, and battery-optimization exemption
   (keep-alive).
4. Select **`CABLE Output (VB-Audio Virtual Cable)`** as the microphone in your meeting
   app. Done.

**Connection options**:
- **Direct WiFi (default)**: enter the PC IP, same LAN required. If the firewall blocks it
  (common on Public networks), run as administrator:
  `netsh advfirewall firewall add rule name="vmic-rx" dir=in action=allow protocol=TCP localport=18200`
- **USB / wireless adb** (most stable, bypasses the firewall):
  `adb forward tcp:18200 tcp:18200` (USB) or
  `adb connect <phone-ip>:5555 && adb reverse tcp:18200 tcp:18200`, and set Host to
  `127.0.0.1` in the app.

## Usage

### Android App

| Element | Description |
|---|---|
| Main button (3 states) | `Start` (idle) → `Auto …` (connecting / retrying every 3s) → `Stop` (streaming) |
| IN dB curve | Input level, logarithmic dB scale (-60..0), EMA-smoothed, 60s scrolling window |
| BAT % curve | Battery level — watch the drain while streaming |
| Test tone | Plays a 2s 880Hz tone on the phone to self-test the full chain (speaker → mic → network → virtual mic) |
| Host / port | Remembered via SharedPreferences |

**Keep-alive stack**: foreground service + persistent notification (with a Stop action) →
PARTIAL_WAKE_LOCK → low-latency **WifiLock** → battery-optimization exemption. Verified on
an Android 10 device: process, service and stream all alive after 90s+ of screen-off. Some
OEM ROMs additionally need manual "app launch management" → allow background activity.

### Windows Companion (vmic_panel.py)

| Element | Description |
|---|---|
| Pill widget | Per-pixel-alpha layered window (`UpdateLayeredWindow`) rendered by Pillow with 4x supersampling: anti-aliased rounded corners, translucent glass, brightens on hover; draggable, right-click → Exit |
| Contents | Status dot (green/gray/amber) + live dB + 60s level chart |
| Tray icon | State-tinted glass badge with three white equalizer bars that **bounce with real audio** (fast-attack / slow-release VU envelope); bars breathe while waiting; tooltip shows `live · -18 dB` |
| Tray menu | Show / hide panel, Exit |
| Diagnostics | `%TEMP%\vmic_panel.log` |

### End-to-End Test (no phone required)

```powershell
start python vmic_rx.py                                     # console receiver
python mock_sender.py 127.0.0.1 18200 --seconds 5           # 5s of 440Hz sine
```

## VMIC Protocol v1

An 11-byte little-endian header (`"VMIC"` + version + sample_rate + channels + bits)
followed by an unframed PCM byte stream; default profile 48kHz/mono/16bit (≈94KB/s).
Full spec: [docs/PROTOCOL.md](docs/PROTOCOL.md).

## Building from Source

### Android

```bash
cd android
gradle assembleDebug    # zero third-party dependencies, AGP 8.9 + Kotlin 1.9, compileSdk 35
adb install -r app/build/outputs/apk/debug/app-debug.apk
```

### Windows

Python 3.10+ and `requirements.txt` (sounddevice / numpy / Pillow / pystray). Nothing to compile.

## Troubleshooting FAQ

| Symptom | Fix |
|---|---|
| Three devices: CABLE Input / CABLE In 16ch / CABLE Output | Normal: the first two are the cable's playback (intake) ends — 16ch is a multichannel variant (can be disabled); the microphone apps should pick is the recording end, `CABLE Output` |
| Don't want a CABLE "playback device" | `CABLE Input` has no physical speaker, makes no sound and won't hijack your default output; disabling it breaks the bridge and silences the virtual mic — it must stay enabled |
| Periodic crackling | WiFi jitter: raise receiver `--prebuffer 200` (ms); or switch to USB |
| Buzz / pitch shift | Sample-rate mismatch: verify both CABLE Input/Output advanced properties are 48000 Hz |
| App killed after screen-off | Check the battery-optimization exemption; on OEM ROMs also enable "launch management" background activity; rooted devices: `adb shell dumpsys deviceidle whitelist +com.vmic.app` (not persistent across reboot) |
| WiFi won't connect | Allow inbound TCP 18200 in the firewall, or use the adb route |
| Widget misbehaving | Check `%TEMP%\vmic_panel.log`; `--null` mode isolates audio issues |

## Known Limitations / Roadmap

- [x] Auto-reconnect on both ends
- [x] Background / screen-off keep-alive
- [ ] Opus encoding (protocol v2, for WAN / low-bandwidth scenarios)
- [ ] Multi-client receiver
- [ ] Prebuilt Release APK
- The receiver handles a single client; reinstalling the app clears `adb reverse`

## Tested Environment

- Windows 11 (Python 3.14 + VB-CABLE Pack45) and a ZTE EC520S (Android 10, 240×320 screen,
  direct WiFi) verified end-to-end: background capture with screen off, automatic recovery
  after drops, Test tone reading ~55%.
- Developed under WSL2 (mirrored networking): the receiver runs via the Windows-side
  Python and can be launched straight from WSL via
  `cmd.exe /c start "" /min D:\wsp\vmic\serve.cmd`.

## License

[MIT](LICENSE). VB-CABLE is a separate product by VB-Audio Software (free for personal
use) — please comply with its terms.
