#!/usr/bin/env python3
"""vmic_panel - anti-aliased frosted pill widget for the VMic receiver.

Renders the widget with Pillow (4x supersampled: rounded pill, text and
sparkline all smoothly anti-aliased) and presents it through
UpdateLayeredWindow for true per-pixel alpha translucency with no
color-key residue and no region jaggies. Hover raises the tint.
Launch via start_panel.cmd (pythonw.exe) so no terminal appears.
"""

from __future__ import annotations

import ctypes
import math
import queue
import sys
import threading
import time
from collections import deque
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import vmic_rx  # noqa: E402

import tkinter as tk  # noqa: E402
from PIL import Image, ImageDraw, ImageFont  # noqa: E402
import numpy as np  # noqa: E402

try:
    import pystray
    HAVE_PYSTRAY = True
except Exception:
    HAVE_PYSTRAY = False

CARD = (242, 243, 246)
CARD_HOVER = (249, 250, 252)
EDGE = (255, 255, 255, 170)
LINE = (5, 150, 105, 255)
LIVE = (5, 150, 105, 255)
IDLE = (156, 163, 175, 255)
ERR = (220, 38, 38, 255)
TEXT = (55, 65, 81, 255)
TEXT2 = (107, 114, 128, 255)

FILL_IDLE = 205
FILL_HOVER = 240

WIDGET_W = 184
WIDGET_H = 30
SS = 4
CHART_POINTS = 120  # 60 s at one point per ~0.5 s

DOT_CX = 14
WORD_X = 22
GAP_WORD_DB = 5
GAP_DB_SPARK = 8

ULW_ALPHA = 2
AC_SRC_OVER = 0
AC_SRC_ALPHA = 1


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [
        ("biSize", ctypes.c_uint32),
        ("biWidth", ctypes.c_int32),
        ("biHeight", ctypes.c_int32),
        ("biPlanes", ctypes.c_uint16),
        ("biBitCount", ctypes.c_uint16),
        ("biCompression", ctypes.c_uint32),
        ("biSizeImage", ctypes.c_uint32),
        ("biXPelsPerMeter", ctypes.c_int32),
        ("biYPelsPerMeter", ctypes.c_int32),
        ("biClrUsed", ctypes.c_uint32),
        ("biClrImportant", ctypes.c_uint32),
    ]


class BITMAPINFO(ctypes.Structure):
    _fields_ = [
        ("bmiHeader", BITMAPINFOHEADER),
        ("bmiColors", ctypes.c_uint32 * 3),
    ]


class BLENDFUNCTION(ctypes.Structure):
    _fields_ = [
        ("BlendOp", ctypes.c_byte),
        ("BlendFlags", ctypes.c_byte),
        ("SourceConstantAlpha", ctypes.c_byte),
        ("AlphaFormat", ctypes.c_byte),
    ]


class POINT(ctypes.Structure):
    _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]


class SIZE(ctypes.Structure):
    _fields_ = [("cx", ctypes.c_long), ("cy", ctypes.c_long)]


gdi32 = ctypes.windll.gdi32
user32 = ctypes.windll.user32

user32.GetParent.restype = ctypes.c_void_p
user32.GetParent.argtypes = [ctypes.c_void_p]
gdi32.CreateCompatibleDC.restype = ctypes.c_void_p
gdi32.CreateCompatibleDC.argtypes = [ctypes.c_void_p]
gdi32.CreateDIBSection.restype = ctypes.c_void_p
gdi32.CreateDIBSection.argtypes = [
    ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint,
    ctypes.POINTER(ctypes.c_void_p), ctypes.c_void_p, ctypes.c_uint,
]
gdi32.SelectObject.restype = ctypes.c_void_p
gdi32.SelectObject.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
user32.GetWindowLongPtrW.restype = ctypes.c_longlong
user32.GetWindowLongPtrW.argtypes = [ctypes.c_void_p, ctypes.c_int]
user32.SetWindowLongPtrW.restype = ctypes.c_longlong
user32.SetWindowLongPtrW.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_longlong]
user32.UpdateLayeredWindow.restype = ctypes.c_int
user32.UpdateLayeredWindow.argtypes = [
    ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p,
    ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint32,
    ctypes.c_void_p, ctypes.c_uint32,
]


def load_font(candidates: list[str], size: int) -> ImageFont.FreeTypeFont:
    for path in candidates:
        try:
            return ImageFont.truetype(path, size)
        except Exception:
            continue
    return ImageFont.load_default()


class LayeredWindow:
    def __init__(self, root: tk.Tk, w: int, h: int):
        self.root = root
        self.w = w
        self.h = h
        self.diag_lines: list[str] = []
        self._last_ulw = -1

        root.update_idletasks()
        root.update()

        self.memdc = gdi32.CreateCompatibleDC(None)
        dib_size = w * h * 4
        self._bmi = BITMAPINFO()
        self._bmi.bmiHeader = BITMAPINFOHEADER(
            biSize=ctypes.sizeof(BITMAPINFOHEADER),
            biWidth=w,
            biHeight=-h,
            biPlanes=1,
            biBitCount=32,
            biCompression=0,
            biSizeImage=dib_size,
        )
        self._ppv = ctypes.c_void_p()
        self.hbm = gdi32.CreateDIBSection(
            None, ctypes.byref(self._bmi), 0, ctypes.byref(self._ppv), None, 0
        )
        gdi32.SelectObject(self.memdc, self.hbm)
        self.diag_lines.append(
            f"memdc={self.memdc} hbm={self.hbm} ppv={self._ppv.value} dib={dib_size}"
        )

        candidates: list[tuple[str, int]] = []
        try:
            candidates.append(("frame", int(root.frame(), 16)))
        except Exception as e:
            self.diag_lines.append(f"frame_err={e}")
        candidates.append(("winfo", root.winfo_id()))
        gp = user32.GetParent(ctypes.c_void_p(root.winfo_id()))
        candidates.append(("parent", gp or 0))

        self.hwnd = None
        for name, cand in candidates:
            if not cand:
                self.diag_lines.append(f"cand {name}=0 skip")
                continue
            self._prep_hwnd(cand)
            ret = self._ulw(cand)
            self.diag_lines.append(f"cand {name}={cand} ret={ret} gle={ctypes.GetLastError()}")
            if ret:
                self.hwnd = cand
                break
        if not self.hwnd:
            self.hwnd = candidates[0][1]

    def _prep_hwnd(self, hwnd: int) -> None:
        GWL_EXSTYLE = -20
        WS_EX_LAYERED = 0x00080000
        WS_EX_NOREDIRECTIONBITMAP = 0x00200000
        try:
            ex = user32.GetWindowLongPtrW(ctypes.c_void_p(hwnd), GWL_EXSTYLE)
            self.diag_lines.append(
                f"prep {hwnd} tk={self.root.tk.call('info', 'patchlevel')} "
                f"exstyle={ex & 0xFFFFFFFF:#x}"
            )
            new = (ex & ~WS_EX_NOREDIRECTIONBITMAP) | WS_EX_LAYERED
            if new != ex:
                user32.SetWindowLongPtrW(ctypes.c_void_p(hwnd), GWL_EXSTYLE, new)
                ex2 = user32.GetWindowLongPtrW(ctypes.c_void_p(hwnd), GWL_EXSTYLE)
                self.diag_lines.append(f"prep {hwnd} exstyle_now={ex2 & 0xFFFFFFFF:#x}")
        except Exception as e:
            self.diag_lines.append(f"prep_err={e}")

    def _ulw(self, hwnd: int) -> int:
        size = SIZE(self.w, self.h)
        pt = POINT(0, 0)
        blend = BLENDFUNCTION(AC_SRC_OVER, 0, 255, AC_SRC_ALPHA)
        return user32.UpdateLayeredWindow(
            ctypes.c_void_p(hwnd), None, None, ctypes.byref(size),
            ctypes.c_void_p(self.memdc), ctypes.byref(pt), 0,
            ctypes.byref(blend), ULW_ALPHA,
        )

    def update(self, img: Image.Image) -> None:
        if not self._ppv.value or not self.hbm:
            return
        arr = np.asarray(img, dtype=np.float32)
        a = arr[..., 3:4] / 255.0
        rgb = (arr[..., :3] * a + 0.5).astype(np.uint8)
        alpha8 = arr[..., 3].astype(np.uint8)
        bgra = np.dstack([rgb[..., 2], rgb[..., 1], rgb[..., 0], alpha8])
        data = bgra.tobytes()
        try:
            ctypes.windll.kernel32.RtlMoveMemory(
                ctypes.c_void_p(self._ppv.value), ctypes.c_char_p(data), len(data)
            )
        except Exception as exc:
            self.diag_lines.append(f"memmove_err={exc} len={len(data)}")
            self._flush_diag()
            return
        self._last_ulw = self._ulw(self.hwnd)
        if not self._last_ulw:
            self.diag_lines.append(f"ulw_ret0 gle={ctypes.GetLastError()}")
            self._flush_diag()

    def _flush_diag(self) -> None:
        try:
            import os
            with open(os.path.join(os.environ.get("TEMP", "."), "vmic_panel.log"), "a") as f:
                f.write("\n".join(self.diag_lines) + "\n")
            self.diag_lines = []
        except Exception:
            pass


class PanelHook(vmic_rx.ConsoleHook):
    def __init__(self, q: queue.Queue):
        self.q = q

    def _put(self, kind, **kw):
        self.q.put((kind, kw))

    def listening(self, bind, port):
        self._put("listening", bind=bind, port=port)

    def device(self, idx, name):
        self._put("device", idx=idx, name=name)

    def no_device(self):
        self._put("no_device")

    def connected(self, addr):
        self._put("connected", addr=addr)

    def header(self, rate, channels):
        self._put("header", rate=rate, channels=channels)

    def stats(self, level_pct, buffered_ms, underruns, overflows, elapsed_s):
        self._put(
            "stats",
            level_pct=level_pct,
            buffered_ms=buffered_ms,
            underruns=underruns,
            elapsed_s=elapsed_s,
        )

    def disconnected(self, addr, reason, elapsed_s, kib):
        self._put("disconnected", addr=addr)

    def error(self, addr, message):
        self._put("error", message=message)


def to_db(pct: float) -> float:
    if pct <= 0.1:
        return -60.0
    db = 20.0 * math.log10(pct / 100.0)
    return max(-60.0, min(0.0, db))


def tray_icon(bars: tuple[float, float, float], rgb: tuple[int, int, int]) -> Image.Image:
    ss = 4
    size = 32 * ss
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    m = 1 * ss
    d.rounded_rectangle([m, m, size - m, size - m], radius=8 * ss,
                        fill=rgb + (64,), outline=rgb + (170,), width=4)
    bw = 5 * ss
    gap = 3 * ss
    total = 3 * bw + 2 * gap
    x0 = (size - total) // 2
    base_h = 6 * ss
    max_h = 21 * ss
    for i, hv in enumerate(bars):
        v = min(1.0, max(0.0, hv))
        bh = int(base_h + (max_h - base_h) * v)
        x = x0 + i * (bw + gap)
        top = (size - bh) // 2
        d.rounded_rectangle([x, top, x + bw, top + bh], radius=bw // 2,
                            fill=(255, 255, 255, 255))
    return img.resize((32, 32), Image.LANCZOS)


class Tray:
    def __init__(self, root: tk.Tk, on_toggle, on_quit):
        self.root = root
        self.bars = [0.15, 0.15, 0.15]
        self.norm = 0.0
        self.state = "listening"
        self.t0 = time.monotonic()
        self.last_text = ""
        self.icon = pystray.Icon(
            "VMic",
            tray_icon((0.15, 0.15, 0.15), (156, 163, 175)),
            "VMic - waiting",
            menu=pystray.Menu(
                pystray.MenuItem("Show / hide panel", self._toggle),
                pystray.MenuItem("Exit", self._quit),
            ),
        )
        self._on_toggle = on_toggle
        self._on_quit = on_quit
        threading.Thread(target=self.icon.run, daemon=True).start()

    def _toggle(self, *_):
        self.root.after(0, self._on_toggle)

    def _quit(self, *_):
        self.root.after(0, self._on_quit)

    def set_state(self, state: str) -> None:
        self.state = state

    def set_level(self, norm: float, db_text: str) -> None:
        self.norm = max(0.0, min(1.0, norm))
        if db_text != self.last_text:
            self.last_text = db_text
            try:
                self.icon.title = db_text
            except Exception:
                pass

    def tick(self) -> None:
        now = time.monotonic() - self.t0
        if self.state == "connected":
            rgb = (16, 185, 129)
            targets = [
                self.norm * (0.55 + 0.45 * (0.5 + 0.5 * math.sin(now * 2.2 + i * 2.4)))
                for i in range(3)
            ]
        elif self.state == "error":
            rgb = (245, 158, 11)
            targets = [0.0, 0.0, 0.0]
        else:
            rgb = (156, 163, 175)
            breath = 0.14 + 0.08 * math.sin(now * 1.6)
            targets = [breath * (1.0 - 0.25 * i) for i in range(3)]
        for i in range(3):
            k = 0.4 if targets[i] > self.bars[i] else 0.12
            self.bars[i] += (targets[i] - self.bars[i]) * k
        try:
            self.icon.icon = tray_icon(tuple(self.bars), rgb)
        except Exception:
            pass


class Panel:
    def __init__(self, root: tk.Tk, q: queue.Queue, port: int):
        self.root = root
        self.q = q
        self.port = port
        self.points: deque[float] = deque(maxlen=CHART_POINTS)
        self.state = "listening"
        self.level_db = -60.0
        self.hover = False

        self.font_word = load_font(
            ["C:/Windows/Fonts/segoeui.ttf", "C:/Windows/Fonts/arial.ttf"], 10 * SS)
        self.font_db = load_font(
            ["C:/Windows/Fonts/consolab.ttf", "C:/Windows/Fonts/consola.ttf",
             "C:/Windows/Fonts/arialbd.ttf"], 10 * SS)

        root.title("VMic")
        root.overrideredirect(True)
        root.attributes("-topmost", True)
        root.geometry(f"{WIDGET_W}x{WIDGET_H}+0+0")
        self._place_bottom_right()

        self.lw = LayeredWindow(root, WIDGET_W, WIDGET_H)

        menu = tk.Menu(root, tearoff=0, bg="#F2F3F6", fg="#374151",
                       activebackground="#059669", activeforeground="#FFFFFF")
        menu.add_command(label="Exit", command=root.destroy)
        self.menu = menu

        self._bind_drag()
        root.bind("<Escape>", lambda e: root.destroy())
        root.bind("<Button-3>", self._show_menu)
        root.bind("<Enter>", lambda e: self._set_hover(True))
        root.bind("<Leave>", lambda e: self._set_hover(False))

        self.lw.diag_lines.append(f"chosen_hwnd={self.lw.hwnd}")
        self.lw._flush_diag()

        self.tray = None
        if HAVE_PYSTRAY:
            self.tray = Tray(root, self._toggle_panel, root.destroy)
            self.tray.set_state(self.state)

        self._render()
        root.after(150, self._poll)

    def _toggle_panel(self) -> None:
        if self.root.state() == "withdrawn":
            self.root.deiconify()
            self.root.attributes("-topmost", True)
        else:
            self.root.withdraw()

    def _tray_state(self, db: float | None = None) -> None:
        if not self.tray:
            return
        self.tray.set_state(self.state)
        if db is not None:
            self.tray.set_level((db + 60.0) / 60.0, f"VMic - live · {db:.0f} dB")

    def _log(self, text: str) -> None:
        try:
            import os
            with open(os.path.join(os.environ.get("TEMP", "."), "vmic_panel.log"), "a") as f:
                f.write(text + "\n")
        except Exception:
            pass

    def _place_bottom_right(self):
        self.root.update_idletasks()

        class RECT(ctypes.Structure):
            _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long),
                        ("right", ctypes.c_long), ("bottom", ctypes.c_long)]

        rect = RECT()
        ok = user32.SystemParametersInfoW(0x0030, 0, ctypes.byref(rect), 0)
        if ok:
            wa_right, wa_bottom = rect.right, rect.bottom
        else:
            wa_right = self.root.winfo_screenwidth()
            wa_bottom = self.root.winfo_screenheight() - 48
        x = max(0, wa_right - WIDGET_W - 12)
        y = max(0, wa_bottom - WIDGET_H - 4)
        self.root.geometry(f"{WIDGET_W}x{WIDGET_H}+{x}+{y}")

    def _show_menu(self, event):
        self.menu.post(event.x_root, event.y_root)

    def _bind_drag(self):
        state = {"x": 0, "y": 0}

        def press(e):
            state["x"], state["y"] = e.x_root, e.y_root

        def move(e):
            dx, dy = e.x_root - state["x"], e.y_root - state["y"]
            state["x"], state["y"] = e.x_root, e.y_root
            self.root.geometry(f"+{self.root.winfo_x() + dx}+{self.root.winfo_y() + dy}")

        self.root.bind("<Button-1>", press)
        self.root.bind("<B1-Motion>", move)

    def _poll(self):
        try:
            while True:
                kind, kw = self.q.get_nowait()
                handler = getattr(self, f"_on_{kind}", None)
                if handler:
                    handler(**kw)
        except queue.Empty:
            pass
        if self.tray:
            self.tray.tick()
        self._render()
        self.root.after(150, self._poll)

    def _set_hover(self, on: bool) -> None:
        if self.hover != on:
            self.hover = on
            self._render()

    def _on_listening(self, bind, port):
        self.state = "listening"

    def _on_connected(self, addr):
        self.state = "connected"
        self.points.clear()
        self._tray_state()

    def _on_disconnected(self, addr):
        self.state = "listening"
        self._tray_state()

    def _on_error(self, message):
        self.state = "error"
        self._tray_state()

    def _on_stats(self, level_pct, buffered_ms, underruns, elapsed_s):
        self.level_db = to_db(level_pct)
        self.points.append(self.level_db)
        self._tray_state(self.level_db)

    def _render(self) -> None:
        W, H = WIDGET_W * SS, WIDGET_H * SS
        cy = WIDGET_H / 2 * SS
        img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        d = ImageDraw.Draw(img)

        fill = (CARD_HOVER if self.hover else CARD) + (FILL_HOVER if self.hover else FILL_IDLE,)
        d.rounded_rectangle([SS, SS, W - SS - 1, H - SS - 1], radius=(WIDGET_H - 4) * SS // 2,
                            fill=fill, outline=EDGE, width=SS)

        if self.state == "connected":
            dotc, word = LIVE, "live"
        elif self.state == "error":
            dotc, word = ERR, "err"
        else:
            dotc, word = IDLE, "wait"

        r = 3.5 * SS
        dcx = DOT_CX * SS
        d.ellipse([dcx - r, cy - r, dcx + r, cy + r], fill=dotc)

        base_y = cy + 4 * SS
        wx = WORD_X * SS
        d.text((wx, base_y), word, font=self.font_word, fill=TEXT2, anchor="ls")
        word_w = d.textlength(word, font=self.font_word) / SS

        db_x = (WORD_X + word_w + GAP_WORD_DB) * SS
        db_txt = f"{self.level_db:.0f} dB"
        d.text((db_x, base_y), db_txt, font=self.font_db, fill=TEXT, anchor="ls")
        db_w = d.textlength(db_txt, font=self.font_db) / SS

        x0 = (WORD_X + word_w + GAP_WORD_DB + db_w + GAP_DB_SPARK) * SS
        x1 = W - 9 * SS
        pts = list(self.points)
        if len(pts) >= 2 and x1 > x0:
            amp = 8 * SS
            coords = []
            n = len(pts)
            for j, v in enumerate(pts):
                x = x1 - (x1 - x0) * (n - 1 - j) / (CHART_POINTS - 1)
                y = cy + amp - 2 * amp * ((v + 60.0) / 60.0)
                coords.extend((x, y))
            d.line(coords, fill=LINE, width=2 * SS, joint="curve")

        self.lw.update(img.resize((WIDGET_W, WIDGET_H), Image.LANCZOS))


def main() -> int:
    import argparse

    ap = argparse.ArgumentParser(description="VMic desktop widget")
    ap.add_argument("--port", type=int, default=vmic_rx.DEFAULT_PORT)
    ap.add_argument("--bind", default="0.0.0.0")
    ap.add_argument("--device", type=int, default=None)
    ap.add_argument("--prebuffer", dest="prebuffer_ms", type=int, default=120)
    ap.set_defaults(null=False)
    args = ap.parse_args()

    q: queue.Queue = queue.Queue()
    threading.Thread(target=vmic_rx.serve, args=(args, PanelHook(q)), daemon=True).start()

    root = tk.Tk()
    Panel(root, q, args.port)
    root.mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
