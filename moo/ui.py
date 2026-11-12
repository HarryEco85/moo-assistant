"""Moo's window.

A glowing orb shows what Moo is doing (blue = ready, cyan = listening,
violet = thinking, and it moves with her voice when speaking). Under it, an
at-a-glance strip (next meeting, inbox, approvals, reminders), quick-start
suggestions, the conversation, and a rounded input bar.

The chrome is drawn with Pillow at double resolution, so edges are smooth.
"""
import math
import queue
import time
import tkinter as tk

import numpy as np
from PIL import Image, ImageDraw, ImageTk

from .config import set_api_key
from .themes import BG, FAINT, FONT, MUTED, OFFLINE, SURFACE, SURFACE_HI, TEXT, TEXT_SIZES, palette

LABELS = {"offline": "Starting up", "idle": "Ready", "listening": "Listening", "thinking": "Thinking",
          "speaking": "Speaking"}


def _rgb(hex_colour):
    return np.array([int(hex_colour[i:i + 2], 16) for i in (1, 3, 5)], dtype=np.float32)


class Orb:
    """Renders the orb with numpy each frame: anti-aliased sphere, rotating
    two-tone gradient, soft highlight and glow."""

    def __init__(self, size):
        self.size = size
        c = (size - 1) / 2
        y, x = np.mgrid[0:size, 0:size].astype(np.float32)
        self.dx, self.dy = x - c, y - c
        self.d = np.sqrt(self.dx ** 2 + self.dy ** 2)
        self.theta = np.arctan2(self.dy, self.dx)
        self.bg = _rgb(BG)
        self.col_a = _rgb(OFFLINE[0])
        self.col_b = _rgb(OFFLINE[1])
        self.level = 0.0

    def render(self, state, t, voice_level, pal):
        a_hex, b_hex = OFFLINE if state == "offline" else pal.get(state, pal["idle"])
        # ease colours towards the target so state changes fade
        self.col_a += (_rgb(a_hex) - self.col_a) * 0.12
        self.col_b += (_rgb(b_hex) - self.col_b) * 0.12
        self.level += (voice_level - self.level) * 0.35

        base = self.size * 0.24
        if state == "listening":
            r = base * (1.0 + 0.05 * math.sin(t * 6))
        elif state == "speaking":
            r = base * (1.0 + 0.16 * self.level)
        elif state == "thinking":
            r = base * (1.0 + 0.02 * math.sin(t * 3))
        else:
            r = base * (1.0 + 0.025 * math.sin(t * 1.4))

        spin = {"thinking": 2.4, "listening": 1.2, "speaking": 0.9}.get(state, 0.35)
        # angular two-tone sweep, fading to an even blend at the centre (no pinch point)
        mix = 0.5 + 0.5 * np.cos(self.theta - t * spin) * np.clip(self.d / (r * 0.95), 0, 1) ** 1.5
        colour = self.col_a[None, None, :] * (1 - mix[..., None]) + self.col_b[None, None, :] * mix[..., None]

        # sphere shading: bright upper-left, darker lower-right
        hx, hy = -0.35 * r, -0.4 * r
        hd = np.sqrt((self.dx - hx) ** 2 + (self.dy - hy) ** 2) / (r * 1.6)
        shade = np.clip(1.15 - 0.55 * hd, 0.45, 1.15)[..., None]
        sphere = np.clip(colour * shade, 0, 255)
        highlight = np.clip(1 - hd * 2.4, 0, 1)[..., None] ** 2 * 0.55
        sphere = sphere + (255 - sphere) * highlight

        inside = np.clip(r + 0.75 - self.d, 0, 1.5)[..., None] / 1.5  # anti-aliased edge

        glow_strength = {"listening": 0.75, "speaking": 0.55 + 0.4 * self.level, "thinking": 0.6}.get(state, 0.45)
        outside = np.clip(self.d - r, 0, None)
        glow = (np.exp(-outside / (self.size * 0.07)) * glow_strength)[..., None]
        glow_col = (self.col_a + self.col_b) / 2
        img = self.bg + (glow_col - self.bg) * glow

        if state == "listening":  # expanding ring
            phase = (t * 0.9) % 1.0
            ring_r = r + 8 + phase * self.size * 0.16
            ring = np.clip(1 - np.abs(self.d - ring_r) / 1.6, 0, 1)[..., None] * (1 - phase) * 0.8
            img = img + (_rgb(b_hex) - img) * ring

        img = img * (1 - inside) + sphere * inside
        return Image.fromarray(np.clip(img, 0, 255).astype(np.uint8), "RGB")


class HudOrb:
    """Jarvis-style display: a glowing core inside rotating rings, tick marks and arcs that react to her
    state and her voice. Drawn at twice the size each frame, then smoothed down."""

    def __init__(self, size, side=0):
        self.size = size
        self.side = side  # width of the readout panels either side (0 = orb only)
        self.info = {}
        self.W = size * 2
        c = (self.W - 1) / 2
        h = (size - 1) / 2  # the soft glow is worked out at normal size (it's smooth), lines at double
        y, x = np.mgrid[0:size, 0:size].astype(np.float32)
        d = np.sqrt((x - h) ** 2 + (y - h) ** 2) / (size / 2)  # 0 at centre, 1 at the edge
        self.glow_mask = (np.exp(-np.clip(d - 0.16, 0, None) / 0.22) * 0.55)[..., None]
        self.core_mask = np.clip(1 - d / 0.17, 0, 1)[..., None] ** 1.6
        self.bg = _rgb(BG)
        self.col_a = _rgb(OFFLINE[0])
        self.col_b = _rgb(OFFLINE[1])
        self.level = 0.0
        self.c = c
        self.rng = np.random.default_rng(3).random(96) * 6.28

    def _arc(self, d, r, start, extent, col, w):
        c = self.c
        d.arc([c - r, c - r, c + r, c + r], start, start + extent, fill=col, width=max(1, int(w)))

    def render(self, state, t, voice_level, pal):
        a_hex, b_hex = OFFLINE if state == "offline" else pal.get(state, pal["idle"])
        self.col_a += (_rgb(a_hex) - self.col_a) * 0.12
        self.col_b += (_rgb(b_hex) - self.col_b) * 0.12
        self.level += (voice_level - self.level) * 0.35
        A, B = self.col_a, self.col_b
        W, c = self.W, self.c
        R = W / 2

        pulse = {"listening": 0.5 + 0.5 * math.sin(t * 5), "speaking": self.level,
                 "thinking": 0.5 + 0.5 * math.sin(t * 9)}.get(state, 0.5 + 0.5 * math.sin(t * 1.3))
        glow_k = {"listening": 1.25, "speaking": 1.0 + 0.6 * self.level, "thinking": 1.1}.get(state, 0.85)
        glow_col = (A * 0.6 + B * 0.4)
        img = self.bg + (glow_col - self.bg) * np.clip(self.glow_mask * glow_k * (0.85 + 0.15 * pulse), 0, 1)
        core_col = np.minimum(255, B * 0.5 + 255 * 0.5)
        core_k = 0.75 + 0.25 * pulse
        img = img + (core_col - img) * np.clip(self.core_mask * core_k * 1.4, 0, 1)
        base = Image.fromarray(np.clip(img, 0, 255).astype(np.uint8), "RGB").resize((W, W), Image.BILINEAR)
        d = ImageDraw.Draw(base, "RGBA")

        def col(colour, alpha):
            return (int(colour[0]), int(colour[1]), int(colour[2]), int(alpha))
        speed = {"thinking": 3.0, "listening": 1.6, "speaking": 1.2}.get(state, 0.5)
        deg = t * 20 * speed

        # core rim
        r_core = R * (0.19 + 0.012 * pulse)
        self._arc(d, r_core, 0, 360, col(B, 230), R * 0.012)
        # inner segmented ring (8 segments) turning clockwise
        r1 = R * 0.30
        for i in range(8):
            self._arc(d, r1, deg + i * 45 + 4, 37, col(A, 200), R * 0.03)
        # thin full ring
        self._arc(d, R * 0.36, 0, 360, col(B, 90), R * 0.006)
        # tick ring turning the other way
        r_t = R * 0.44
        for i in range(72):
            ang = math.radians(-deg * 0.6 + i * 5)
            long = i % 6 == 0
            r_in = r_t - (R * 0.05 if long else R * 0.025)
            d.line([(c + math.cos(ang) * r_in, c + math.sin(ang) * r_in),
                    (c + math.cos(ang) * r_t, c + math.sin(ang) * r_t)],
                   fill=col(B if long else A, 220 if long else 120), width=max(1, int(R * (0.01 if long else 0.006))))
        # the big arcs, like the reactor
        r3 = R * 0.53
        self._arc(d, r3, deg * 1.4, 115, col(A, 235), R * 0.035)
        self._arc(d, r3, deg * 1.4 + 180, 60, col(B, 235), R * 0.035)
        self._arc(d, r3 + R * 0.05, -deg * 0.8 + 30, 40, col(B, 160), R * 0.012)
        self._arc(d, r3 + R * 0.05, -deg * 0.8 + 210, 90, col(A, 120), R * 0.012)
        # thinking: a fast scanning sweep
        if state == "thinking":
            self._arc(d, R * 0.66, t * 300 % 360, 50, col(B, 220), R * 0.02)
            self._arc(d, R * 0.66, t * 300 % 360 + 180, 50, col(A, 160), R * 0.02)
        # voice ring: bars that move with her voice (speaking) or listen (listening)
        if state in ("speaking", "listening"):
            amp = self.level if state == "speaking" else 0.25 + 0.2 * math.sin(t * 6)
            r_v = R * 0.70
            for i in range(96):
                ang = math.radians(i * 3.75 + deg * 0.3)
                wob = 0.5 + 0.5 * math.sin(t * 7 + self.rng[i] + i * 0.4)
                ln = R * (0.02 + 0.16 * amp * wob)
                d.line([(c + math.cos(ang) * r_v, c + math.sin(ang) * r_v),
                        (c + math.cos(ang) * (r_v + ln), c + math.sin(ang) * (r_v + ln))],
                       fill=col(B if i % 2 else A, 210), width=max(1, int(R * 0.012)))
        else:  # resting: a faint outer ring with a slow marker
            self._arc(d, R * 0.72, 0, 360, col(A, 45), R * 0.005)
            self._arc(d, R * 0.72, deg * 0.5, 14, col(B, 180), R * 0.012)
        if state == "listening":  # rings expanding outwards
            for k in range(2):
                ph = (t * 0.8 + k * 0.5) % 1.0
                self._arc(d, R * (0.6 + ph * 0.36), 0, 360, col(B, 160 * (1 - ph)), R * 0.008)
        if self.side:
            base = self._panels(base, state, t)
            return base.resize((self.size + 2 * self.side, self.size), Image.LANCZOS)
        return base.resize((self.size, self.size), Image.LANCZOS)

    def _panels(self, orb, state, t):
        """Readouts either side: the time on the left, her systems on the right."""
        from datetime import datetime
        P, W = self.side * 2, self.W
        canvas = Image.new("RGB", (W + 2 * P, W), tuple(int(v) for v in self.bg))
        canvas.paste(orb, (P, 0))
        d = ImageDraw.Draw(canvas, "RGBA")
        A, B = self.col_a, self.col_b
        acc = (int(B[0]), int(B[1]), int(B[2]))
        acc2 = (int(A[0]), int(A[1]), int(A[2]))
        mid = W / 2
        now = datetime.now()
        # left: clock
        xr = P - W * 0.06
        d.text((xr, mid - W * 0.02), now.strftime("%H:%M"), font=_font("light", 30), fill=INK, anchor="rs")
        d.text((xr, mid + W * 0.035), now.strftime("%A %d %B").upper(), font=_font("semibold", 7.5),
               fill=MUTED_RGB, anchor="rm")
        d.text((xr, mid + W * 0.085), self.info.get("left", ""), font=_font("semibold", 7.5), fill=acc, anchor="rm")
        # bracket from the clock into the rings
        y0 = mid + W * 0.13
        d.line([(xr - W * 0.38, y0), (P + W * 0.12, y0)], fill=(*acc2, 110), width=2)
        d.line([(P + W * 0.12, y0), (P + W * 0.2, y0 - W * 0.06)], fill=(*acc2, 110), width=2)
        sec = now.second + (t % 1)
        d.line([(xr - W * 0.38, y0), (xr - W * 0.38 + (W * 0.38 - (xr - P - W * 0.12)) * 0 + W * 0.38 * sec / 60, y0)],
               fill=(*acc, 230), width=4)
        # right: systems
        xl = P + W + W * 0.06
        rows = self.info.get("rows") or []
        label_state = {"idle": "STANDING BY", "listening": "LISTENING", "thinking": "WORKING",
                       "speaking": "SPEAKING", "offline": "STARTING UP"}.get(state, state.upper())
        y = mid - W * 0.2
        blink = 160 + int(95 * (0.5 + 0.5 * math.sin(t * 4)))
        d.ellipse([xl, y - 7, xl + 14, y + 7], fill=(*acc, blink))
        d.text((xl + 28, y), label_state, font=_font("semibold", 9), fill=acc, anchor="lm")
        y += W * 0.085
        for k, v in rows[:5]:
            d.text((xl, y), k.upper(), font=_font("semibold", 6.5), fill=FAINT_RGB, anchor="lm")
            d.text((xl + W * 0.2, y), _fit(d, v, _font("semibold", 8), P - W * 0.3), font=_font("semibold", 8),
                   fill=INK, anchor="lm")
            y += W * 0.065
        y1 = mid + W * 0.2
        d.line([(xl - W * 0.02, y1), (xl + P * 0.7, y1)], fill=(*acc2, 90), width=2)
        d.line([(P + W - W * 0.12, mid - W * 0.26), (xl - W * 0.02, mid - W * 0.26)], fill=(*acc2, 90), width=2)
        return canvas


def make_orb(size, settings=None, width=0):
    style = ((settings or {}).get("orb_style") or "Jarvis").lower()
    if style.startswith("sphere"):
        return Orb(size)
    big = int(size * 1.3)
    side = 0
    if width >= 1100:
        side = max(0, min(320, int((min(width, 1500) - big) / 2) - 20))
    return HudOrb(big, side)


S = 2  # draw chrome at 2x, then smooth down
INK, MUTED_RGB, FAINT_RGB = (232, 236, 243), (138, 147, 166), (88, 96, 114)
GREEN, AMBER = (74, 222, 128), (251, 191, 36)
COLUMN = 780  # the chat column never gets wider than this, so lines stay readable
SUGGESTIONS = [("Summarise my day", "Summarise my day"), ("What's in my inbox?", "Anything important in my inbox?"),
               ("Prep me for tomorrow", "Prep me for tomorrow"), ("Open the office", "__office__")]


def _hex(c):
    return tuple(int(c[i:i + 2], 16) for i in (1, 3, 5))


def _mix(a, b, t):
    return tuple(int(a[i] + (b[i] - a[i]) * t) for i in range(3))


def _font(kind, size):
    from .cards import _font as f
    return f(kind, size)  # cards fonts are already sized for 2x drawing


def _fit(draw, text, font, width):
    text = " ".join(str(text).split())
    if draw.textlength(text, font=font) <= width:
        return text
    while text and draw.textlength(text + "…", font=font) > width:
        text = text[:-1]
    return text.rstrip() + "…"


def _wrap(draw, text, font, width):
    lines = []
    for para in str(text).split("\n"):
        line = ""
        for word in para.split():
            trial = f"{line} {word}".strip()
            if draw.textlength(trial, font=font) <= width or not line:
                line = trial
            else:
                lines.append(line)
                line = word
        lines.append(line)
    return lines


def _canvas(w, h, bg):
    img = Image.new("RGB", (int(w * S), int(h * S)), bg)
    return img, ImageDraw.Draw(img)


def _done(img):
    return img.resize((img.width // S, img.height // S), Image.LANCZOS)


def _icon(d, kind, cx, cy, s, col):
    """Small line icons, drawn at 2x."""
    w = max(2, int(s * 0.1))
    if kind == "gear":
        r1, r2 = s * 0.30, s * 0.42
        for k in range(8):
            a = k * math.pi / 4
            d.line([(cx + math.cos(a) * r1, cy + math.sin(a) * r1), (cx + math.cos(a) * r2, cy + math.sin(a) * r2)],
                   fill=col, width=int(w * 1.6))
        d.ellipse([cx - r1, cy - r1, cx + r1, cy + r1], outline=col, width=w)
        d.ellipse([cx - s * 0.12, cy - s * 0.12, cx + s * 0.12, cy + s * 0.12], outline=col, width=w)
    elif kind == "grid":
        g, q = s * 0.08, s * 0.34
        for ox in (-1, 1):
            for oy in (-1, 1):
                x0 = cx + (g if ox > 0 else -g - q)
                y0 = cy + (g if oy > 0 else -g - q)
                d.rounded_rectangle([x0, y0, x0 + q, y0 + q], radius=s * 0.07, outline=col, width=w)
    elif kind == "stop":
        q = s * 0.3
        d.rounded_rectangle([cx - q, cy - q, cx + q, cy + q], radius=s * 0.08, fill=col)
    elif kind == "mic":
        d.rounded_rectangle([cx - s * 0.13, cy - s * 0.38, cx + s * 0.13, cy + s * 0.08], radius=s * 0.13, fill=col)
        d.arc([cx - s * 0.26, cy - s * 0.2, cx + s * 0.26, cy + s * 0.24], 0, 180, fill=col, width=w)
        d.line([(cx, cy + s * 0.24), (cx, cy + s * 0.38)], fill=col, width=w)
        d.line([(cx - s * 0.14, cy + s * 0.38), (cx + s * 0.14, cy + s * 0.38)], fill=col, width=w)
    elif kind == "send":
        d.polygon([(cx - s * 0.3, cy - s * 0.32), (cx + s * 0.36, cy), (cx - s * 0.3, cy + s * 0.32),
                   (cx - s * 0.18, cy)], fill=col)
    elif kind == "calendar":
        d.rounded_rectangle([cx - s * 0.32, cy - s * 0.26, cx + s * 0.32, cy + s * 0.32], radius=s * 0.06,
                            outline=col, width=w)
        d.line([(cx - s * 0.32, cy - s * 0.1), (cx + s * 0.32, cy - s * 0.1)], fill=col, width=w)
        for ox in (-0.14, 0.14):
            d.line([(cx + s * ox, cy - s * 0.36), (cx + s * ox, cy - s * 0.2)], fill=col, width=w)
    elif kind == "mail":
        d.rounded_rectangle([cx - s * 0.34, cy - s * 0.24, cx + s * 0.34, cy + s * 0.24], radius=s * 0.05,
                            outline=col, width=w)
        d.line([(cx - s * 0.3, cy - s * 0.2), (cx, cy + s * 0.04), (cx + s * 0.3, cy - s * 0.2)], fill=col,
               width=w, joint="curve")
    elif kind == "check":
        d.ellipse([cx - s * 0.32, cy - s * 0.32, cx + s * 0.32, cy + s * 0.32], outline=col, width=w)
        d.line([(cx - s * 0.13, cy), (cx - s * 0.03, cy + s * 0.11), (cx + s * 0.15, cy - s * 0.1)], fill=col,
               width=w, joint="curve")
    elif kind == "clip":
        d.arc([cx - s * 0.16, cy - s * 0.38, cx + s * 0.16, cy - s * 0.06], 180, 360, fill=col, width=w)
        d.line([(cx - s * 0.16, cy - s * 0.22), (cx - s * 0.16, cy + s * 0.2)], fill=col, width=w)
        d.line([(cx + s * 0.16, cy - s * 0.22), (cx + s * 0.16, cy + s * 0.12)], fill=col, width=w)
        d.arc([cx - s * 0.16, cy + s * 0.04, cx + s * 0.16, cy + s * 0.36], 0, 180, fill=col, width=w)
        d.line([(cx, cy - s * 0.12), (cx, cy + s * 0.14)], fill=col, width=w)
        d.arc([cx - s * 0.08, cy + s * 0.06, cx + s * 0.08, cy + s * 0.22], 0, 180, fill=col, width=w)
        d.line([(cx + s * 0.08, cy + s * 0.14), (cx + s * 0.08, cy - s * 0.12)], fill=col, width=w)
    elif kind == "bell":
        d.chord([cx - s * 0.24, cy - s * 0.32, cx + s * 0.24, cy + s * 0.22], 180, 360, outline=col, width=w)
        d.line([(cx - s * 0.24, cy - s * 0.05), (cx - s * 0.28, cy + s * 0.18), (cx + s * 0.28, cy + s * 0.18),
                (cx + s * 0.24, cy - s * 0.05)], fill=col, width=w, joint="curve")
        d.ellipse([cx - s * 0.06, cy + s * 0.22, cx + s * 0.06, cy + s * 0.34], fill=col)


class MooWindow:
    def __init__(self, app):
        self.app = app
        self.root = tk.Tk()
        self.root.title("Moo")
        self.root.geometry("520x820")
        self.root.minsize(400, 600)
        self.root.configure(bg=BG)
        self.calls = queue.Queue()
        self.state = "offline"
        self.detail = ""
        self.t0 = time.time()
        self.orb = make_orb(220, app.settings)
        self._orb_img = None
        self._has_placeholder = False
        self._card_images = []
        self._imgs = {}  # keep PhotoImages alive
        self.chat_width = 480
        self._hover = None
        self._focus = False
        self._chatted = False
        self._glance_sig = None
        self._size = (0, 0)
        self._build()
        self.apply_appearance()
        self.root.after(33, self._animate)
        self.root.after(50, self._pump)
        self.root.after(400, self._refresh_glance)
        self.root.protocol("WM_DELETE_WINDOW", self.app.shutdown)
        self._dark_title_bar()

    def _dark_title_bar(self):
        try:  # Windows 10/11 dark title bar
            import ctypes
            self.root.update_idletasks()
            hwnd = ctypes.windll.user32.GetParent(self.root.winfo_id())
            value = ctypes.c_int(1)
            ctypes.windll.dwmapi.DwmSetWindowAttribute(hwnd, 20, ctypes.byref(value), ctypes.sizeof(value))
        except Exception:
            pass

    # ---------- thread-safe helpers ----------
    def later(self, fn, *args):
        self.calls.put((fn, args))

    def _pump(self):
        try:
            while True:
                fn, args = self.calls.get_nowait()
                try:
                    fn(*args)
                except Exception:
                    import logging
                    logging.getLogger("moo").exception("UI update failed")
        except queue.Empty:
            pass
        self.root.after(50, self._pump)

    def _keep(self, key, pil):
        self._imgs[key] = ImageTk.PhotoImage(pil)
        return self._imgs[key]

    def _accent(self):
        return _hex(palette(self.app.settings)["idle"][0])

    def set_state(self, state, detail=""):
        self.later(self._set_state, state, detail)

    def _set_state(self, state, detail):
        self.state, self.detail = state, detail
        label = detail or LABELS.get(state, state)
        self.status.configure(text=label, fg=TEXT if state in ("listening", "thinking", "speaking") else MUTED)
        self._draw_chip()
        self._draw_input()

    # ---------- conversation ----------
    def _stamp(self, who):
        name = self.app.settings.get("assistant_name", "Moo") or "Moo"
        if who == "Moo":
            self.chat.insert("end", f"{name}  ", "who_moo")
            self.chat.insert("end", time.strftime("%H:%M") + "\n", "when")

    def _hide_suggestions(self):
        if not self._chatted:
            self._chatted = True
            self.chips.pack_forget()

    def add_message(self, who, text):
        self.later(self._add_message, who, text)

    def _add_message(self, who, text):
        self.chat.configure(state="normal")
        text = text.strip()
        if who == "You":
            self._hide_suggestions()
            self.chat.insert("end", " ", "you")
            self.chat.image_create("end", image=self._bubble(text), pady=2)
            self.chat.insert("end", "\n", "you")
        elif who == "Moo":
            self._stamp("Moo")
            self.chat.insert("end", text + "\n", "moo")
        else:
            self.chat.insert("end", text + "\n", "sys")
        self.chat.insert("end", "\n", "gap")
        self.chat.configure(state="disabled")
        self.chat.see("end")

    def _bubble(self, text):
        """Your message as a rounded bubble on the right."""
        big, small = TEXT_SIZES.get(self.app.settings.get("text_size", "Medium"), TEXT_SIZES["Medium"])
        f = _font("regular", small + 0.5)
        maxw = max(160, min(int(self._column() * 0.72), 520))
        probe = ImageDraw.Draw(Image.new("RGB", (4, 4)))
        lines = _wrap(probe, text, f, (maxw - 32) * S)
        lh = (small + 9) * S
        tw = max(probe.textlength(x, font=f) for x in lines) / S
        w, h = int(tw + 32), int(len(lines) * lh / S + 20)
        acc = self._accent()
        img, d = _canvas(w, h, _hex(BG))
        d.rounded_rectangle([0, 0, w * S - 1, h * S - 1], radius=16 * S, fill=_mix(_hex(SURFACE_HI), acc, 0.18))
        y = 10 * S
        for ln in lines:
            d.text((16 * S, y), ln, font=f, fill=INK)
            y += lh
        photo = ImageTk.PhotoImage(_done(img))
        self._card_images.append(photo)
        self._card_images = self._card_images[-80:]
        return photo

    def add_action(self, label, callback):
        self.later(self._add_action, label, callback)

    def _pill_button(self, parent, text, primary, width=None):
        acc = self._accent()
        f = _font("semibold", 9.5)
        probe = ImageDraw.Draw(Image.new("RGB", (4, 4)))
        w = width or int(probe.textlength(text, font=f) / S + 30)
        h = 30

        def make(hover):
            img, d = _canvas(w, h, _hex(BG))
            if primary:
                fill, ink = (_mix(acc, (255, 255, 255), 0.15) if hover else acc), _hex(BG)
            else:
                fill, ink = (_mix(_hex(SURFACE_HI), (255, 255, 255), 0.08 if hover else 0)), INK
            d.rounded_rectangle([0, 0, w * S - 1, h * S - 1], radius=h / 2 * S, fill=fill)
            d.text((w / 2 * S, h / 2 * S), text, font=f, fill=ink, anchor="mm")
            return ImageTk.PhotoImage(_done(img))
        normal, hover = make(False), make(True)
        b = tk.Label(parent, image=normal, bg=BG, bd=0, cursor="hand2")
        b._imgs = (normal, hover)
        b.bind("<Enter>", lambda e: b.configure(image=hover) if b.cget("cursor") else None)
        b.bind("<Leave>", lambda e: b.configure(image=normal))
        return b

    def _add_action(self, label, callback):
        btn = self._pill_button(self.chat, label, True)

        def go(_e):
            btn.unbind("<Button-1>")
            btn.configure(cursor="")
            callback()
        btn.bind("<Button-1>", go)
        self.chat.configure(state="normal")
        self.chat.insert("end", " ", "sys")
        self.chat.window_create("end", window=btn)
        self.chat.insert("end", "\n\n", "gap")
        self.chat.configure(state="disabled")
        self.chat.see("end")

    def add_choice(self, question, options):
        self.later(self._add_choice, question, options)

    def _add_choice(self, question, options):
        self.chat.configure(state="normal")
        self.chat.insert("end", question + "\n", "choice_q")
        buttons = []
        for i, (label, cb) in enumerate(options):
            b = self._pill_button(self.chat, label, i == 0)
            buttons.append(b)

            def click(_e, cb=cb):
                for x in buttons:
                    x.configure(cursor="")
                    x.unbind("<Button-1>")
                cb()
            b.bind("<Button-1>", click)
            self.chat.insert("end", " " if i else "", "choice")
            self.chat.window_create("end", window=b, padx=3)
        self.chat.insert("end", "\n\n", "gap")
        self.chat.configure(state="disabled")
        self.chat.see("end")

    def add_card(self, pil_image):
        self.later(self._add_card, pil_image)

    def _add_card(self, pil_image):
        photo = ImageTk.PhotoImage(pil_image)
        self._card_images.append(photo)  # keep a reference or Tk drops the picture
        self._card_images = self._card_images[-80:]
        self.chat.configure(state="normal")
        self.chat.insert("end", " ", "card")
        self.chat.image_create("end", image=photo, padx=0, pady=4)
        self.chat.insert("end", "\n\n", "gap")
        self.chat.configure(state="disabled")
        self.chat.see("end")

    def add_draft(self, text, label):
        self.later(self._add_draft, text, label)

    def _add_draft(self, text, label):
        self.chat.configure(state="normal")
        self.chat.insert("end", label.upper() + "\n", "draft_label")
        self.chat.insert("end", text.strip() + "\n", "draft")
        self.chat.insert("end", "\n", "gap")
        self.chat.configure(state="disabled")
        self.chat.see("end")

    def set_footer(self, text):
        self.later(lambda: self.footer.configure(text=text))

    # ---------- layout ----------
    def _build(self):
        # header: name, status chip, icon buttons
        header = tk.Frame(self.root, bg=BG)
        header.pack(side="top", fill="x", padx=20, pady=(14, 0))
        self.title_label = tk.Label(header, text="", bg=BG, fg=TEXT, font=(FONT, 16, "bold"))
        self.title_label.pack(side="left")
        self.title_rest = tk.Label(header, text="", bg=BG, fg=MUTED, font=(FONT, 11, "bold"))
        self.title_rest.pack(side="left", padx=(6, 0), pady=(4, 0))
        self.chip = tk.Label(header, bg=BG, bd=0)
        self.chip.pack(side="left", padx=(12, 0), pady=(3, 0))
        for kind, cmd in (("gear", self.open_settings), ("grid", self.open_office), ("stop", self.app.stop_speaking), ("clip", self.pick_files)):
            b = tk.Label(header, bg=BG, bd=0, cursor="hand2")
            b.pack(side="right", padx=(6, 0))
            normal, hover = self._icon_button(kind, False), self._icon_button(kind, True)
            b._imgs = (normal, hover)
            b.configure(image=normal)
            b.bind("<Button-1>", lambda e, c=cmd: c())
            b.bind("<Enter>", lambda e, w=b: w.configure(image=w._imgs[1]))
            b.bind("<Leave>", lambda e, w=b: w.configure(image=w._imgs[0]))

        # bottom first, so it is never pushed off-screen
        self.footer = tk.Label(self.root, text="", bg=BG, fg=FAINT, font=(FONT, 8))
        self.footer.pack(side="bottom", pady=(0, 10))
        self.hint = tk.Label(self.root, text="", bg=BG, fg=FAINT, font=(FONT, 9))
        self.hint.pack(side="bottom", pady=(2, 0))
        self.input = tk.Canvas(self.root, height=64, bg=BG, highlightthickness=0)
        self.input.pack(side="bottom", fill="x", padx=18, pady=(6, 0))
        self.entry = tk.Entry(self.input, bg=SURFACE, fg=TEXT, relief="flat", bd=0, insertbackground=TEXT,
                              font=(FONT, 11), highlightthickness=0)
        self.entry.bind("<Return>", self._send)
        self.entry.bind("<FocusIn>", lambda e: (self._placeholder(False), self._set_focus(True)))
        self.entry.bind("<FocusOut>", lambda e: (self._placeholder(True), self._set_focus(False)))
        self.entry.bind("<KeyRelease>", lambda e: self._draw_input())
        self._entry_win = self.input.create_window(0, 0, window=self.entry, anchor="w")
        self._pill_item = self.input.create_image(0, 0, anchor="nw")
        self.input.tag_lower(self._pill_item)
        self.input.bind("<Configure>", lambda e: self._draw_input())
        self.input.bind("<Button-1>", self._input_click)
        self.input.bind("<Motion>", self._input_motion)
        self._placeholder(True)

        # orb and status
        self.canvas = tk.Label(self.root, bg=BG, bd=0)
        self.canvas.pack(side="top", pady=(0, 0))
        self.status = tk.Label(self.root, text="Starting up", bg=BG, fg=MUTED, font=(FONT, 10))
        self.status.pack(side="top", pady=(0, 12))

        # at-a-glance cards and suggestions
        self.glance = tk.Frame(self.root, bg=BG)
        self.glance.pack(side="top", pady=(0, 10))
        self.chips = tk.Frame(self.root, bg=BG)
        self.chips.pack(side="top", pady=(0, 6))
        self._build_suggestions()

        self.chat = tk.Text(self.root, bg=BG, fg=TEXT, wrap="word", relief="flat", bd=0, padx=24, pady=8,
                            font=(FONT, 11), state="disabled", cursor="arrow", highlightthickness=0,
                            spacing1=1, spacing3=1, insertbackground=BG, selectbackground=SURFACE_HI)
        self.chat.pack(side="top", fill="both", expand=True)
        self.chat.bind("<Configure>", self._on_chat_resize)
        self.root.bind("<Configure>", self._on_root_resize)
        self.chat.tag_configure("moo", foreground=TEXT, font=(FONT, 12), rmargin=30, spacing2=4)
        self.chat.tag_configure("who_moo", foreground=palette(self.app.settings)["idle"][0], font=(FONT, 9, "bold"))
        self.chat.tag_configure("when", foreground=FAINT, font=(FONT, 8))
        self.chat.tag_configure("you", justify="right")
        self.chat.tag_configure("sys", foreground=FAINT, font=(FONT, 9), justify="center", lmargin1=30, lmargin2=30,
                                rmargin=30)
        self.chat.tag_configure("choice_q", foreground=MUTED, font=(FONT, 10), spacing3=6)
        self.chat.tag_configure("choice")
        self.chat.tag_configure("gap", font=(FONT, 6))
        self.chat.tag_configure("draft_label", foreground=FAINT, font=(FONT, 8, "bold"), lmargin1=12, spacing1=4)
        self.chat.tag_configure("draft", foreground=TEXT, background=SURFACE, font=(FONT, 10), lmargin1=12,
                                lmargin2=12, rmargin=24, spacing1=2, spacing3=2)

    def _icon_button(self, kind, hover):
        size = 34
        img, d = _canvas(size, size, _hex(BG))
        if hover:
            d.rounded_rectangle([0, 0, size * S - 1, size * S - 1], radius=10 * S, fill=_hex(SURFACE_HI))
        col = INK if hover else MUTED_RGB
        if kind == "stop" and hover:
            col = (248, 113, 113)
        _icon(d, kind, size / 2 * S, size / 2 * S, 18 * S, col)
        return ImageTk.PhotoImage(_done(img))

    def _draw_chip(self):
        state = self.state
        acc = self._accent()
        colour = {"idle": GREEN, "offline": FAINT_RGB, "listening": _hex(palette(self.app.settings)["listening"][0]),
                  "thinking": _hex(palette(self.app.settings)["thinking"][0]), "speaking": acc}.get(state, acc)
        text = {"idle": "Online", "offline": "Starting", "listening": "Listening", "thinking": "Thinking",
                "speaking": "Speaking"}.get(state, state.title())
        ears = getattr(self.app, "ears", None)
        started = getattr(ears, "notes_started", None) if ears else None
        rec = getattr(self.app, "recorder", None)
        if rec and rec.running and state in ("idle", "offline"):
            secs = int(time.time() - rec.started)
            colour, text = (248, 113, 113), f"Recording  {secs // 60}:{secs % 60:02d}"
        elif started and state in ("idle", "offline"):
            secs = int(time.time() - started)
            colour, text = (248, 113, 113), f"Taking notes  {secs // 60}:{secs % 60:02d}"
        f = _font("semibold", 8.5)
        probe = ImageDraw.Draw(Image.new("RGB", (4, 4)))
        w, h = int(probe.textlength(text, font=f) / S + 30), 22
        img, d = _canvas(w, h, _hex(BG))
        d.rounded_rectangle([0, 0, w * S - 1, h * S - 1], radius=h / 2 * S, fill=_mix(_hex(BG), colour, 0.16))
        d.ellipse([9 * S, (h / 2 - 3.5) * S, 16 * S, (h / 2 + 3.5) * S], fill=colour)
        d.text((21 * S, h / 2 * S), text, font=f, fill=colour, anchor="lm")
        self.chip.configure(image=self._keep("chip", _done(img)))

    def _build_suggestions(self):
        for w in self.chips.winfo_children():
            w.destroy()
        for label, prompt in SUGGESTIONS:
            b = self._pill_button(self.chips, label, False)
            b.pack(side="left", padx=4)
            b.bind("<Button-1>", lambda e, p=prompt: self._suggest(p))

    def _suggest(self, prompt):
        if prompt == "__office__":
            self.open_office()
        else:
            self.app.text_request(prompt)

    # ---------- at a glance ----------
    def _glance_items(self):
        app = self.app
        w = getattr(app, "watcher", None)
        stats = w.stats if w else {}
        meet = stats.get("meetings", {}).get("text") or "Checking your diary…"
        meet = meet.replace("Next: ", "")
        email = stats.get("email", {})
        inbox = email.get("alert") if email.get("alert") and time.time() - email.get("alert_at", 0) < 6 * 3600 else None
        if not inbox:
            txt = email.get("text") or "Watching for new emails"
            import re as _re
            m = _re.search(r"(\d+) new", txt)
            inbox = "Nothing urgent" if m else txt
        pending = len(app.approvals.pending()) if getattr(app, "approvals", None) else 0
        try:
            rem = app.reminders.list() if getattr(app, "reminders", None) else []
        except Exception:
            rem = []
        nxt = rem[0] if rem else None
        rem_txt = f"{nxt['due'][11:16]} · {nxt['text']}" if nxt else "None set"
        return [("calendar", "NEXT UP", meet, "What's my diary looking like today?"),
                ("mail", "INBOX", inbox, "Anything important in my inbox?"),
                ("check", "NEEDS YOUR OK", f"{pending} waiting" if pending else "All clear", "__office__"),
                ("bell", "REMINDERS", rem_txt, "What reminders have I got?")]

    def _refresh_glance(self):
        try:
            self._draw_glance()
        except Exception:
            import logging
            logging.getLogger("moo").exception("Glance strip failed")
        self.root.after(20000, self._refresh_glance)

    def _hud_info(self):
        """What the Jarvis readouts show beside her."""
        if not isinstance(self.orb, HudOrb) or not self.orb.side:
            return
        app, rows = self.app, []
        try:
            ears = getattr(app, "ears", None)
            cloud = getattr(ears, "last_cloud", None) if ears else None
            from .config import get_secret
            rows.append(("Hearing", "Cloud" if (cloud or (cloud is None and get_secret("groq_key"))) else "On this PC"))
            rows.append(("Wake", "Hello Moo · Ctrl+Alt+M"))
            mouth = getattr(app, "mouth", None)
            v = getattr(mouth, "voice", None) or app.settings.get("voice", "")
            rows.append(("Voice", str(v).split("|")[-1].replace("_", " ")[:28] or "Off"))
            vault = getattr(getattr(app, "brain", None), "vault", None)
            if vault is not None and vault.ready:
                rows.append(("Brain", f"{len(vault.notes())} notes"))
            pend = len(app.approvals.pending()) if getattr(app, "approvals", None) else 0
            rows.append(("Your OK", f"{pend} waiting" if pend else "None waiting"))
        except Exception:
            pass
        from . import __version__
        self.orb.info = {"rows": rows, "left": f"MOO {__version__} · ONLINE"}

    def _draw_glance(self, force=False):
        self._hud_info()
        items = self._glance_items()
        col = self._column()
        n = 4 if col >= 640 else 2
        items = items[:n]
        gap = 10
        cw = int(min(200, (col - gap * (n - 1)) / n))
        sig = (tuple(items), cw, self.app.settings.get("theme"), self._hover_card)
        if sig == self._glance_sig and not force:
            return
        self._glance_sig = sig
        for wdg in self.glance.winfo_children():
            wdg.destroy()
        acc = self._accent()
        for i, (icon, title, value, prompt) in enumerate(items):
            h = 62
            img, d = _canvas(cw, h, _hex(BG))
            hover = self._hover_card == i
            fill = _mix(_hex(SURFACE), (255, 255, 255), 0.03 if hover else 0)
            d.rounded_rectangle([0, 0, cw * S - 1, h * S - 1], radius=14 * S, fill=fill,
                                outline=_mix(_hex(BG), acc, 0.5) if hover else _mix(_hex(BG), (255, 255, 255), 0.09),
                                width=S)
            d.rounded_rectangle([12 * S, 15 * S, 44 * S, 47 * S], radius=9 * S, fill=_mix(_hex(SURFACE), acc, 0.2))
            _icon(d, icon, 28 * S, 31 * S, 20 * S, _mix(acc, (255, 255, 255), 0.25))
            d.text((54 * S, 22 * S), title, font=_font("semibold", 7.5), fill=FAINT_RGB, anchor="lm")
            fv = _font("semibold", 9.5)
            d.text((54 * S, 41 * S), _fit(d, value, fv, (cw - 64) * S), font=fv, fill=INK, anchor="lm")
            lab = tk.Label(self.glance, image=self._keep(f"glance{i}", _done(img)), bg=BG, bd=0, cursor="hand2")
            lab.pack(side="left", padx=(0 if i == 0 else gap, 0))
            lab.bind("<Button-1>", lambda e, p=prompt: self._suggest(p))
            lab.bind("<Enter>", lambda e, k=i: self._set_hover_card(k))
            lab.bind("<Leave>", lambda e: self._set_hover_card(None))

    _hover_card = None

    def _set_hover_card(self, k):
        self._hover_card = k
        self._draw_glance()

    # ---------- sizing ----------
    def _column(self):
        return max(320, min(self.root.winfo_width() - 40, COLUMN))

    def _on_chat_resize(self, e):
        pad = max(22, int((e.width - COLUMN) / 2))
        if abs(pad - int(str(self.chat.cget("padx")))) > 2:
            self.chat.configure(padx=pad)
        self.chat_width = min(e.width - 2 * pad, COLUMN)

    def _on_root_resize(self, e):
        if e.widget is not self.root:
            return
        size = (e.width, e.height)
        if abs(size[0] - self._size[0]) < 8 and abs(size[1] - self._size[1]) < 8:
            return
        self._size = size
        orb = 280 if e.height >= 1000 else (220 if e.height >= 860 else (180 if e.height >= 700 else 140))
        side = max(18, int((e.width - COLUMN - 40) / 2))
        self.input.pack_configure(padx=side)
        wide = e.width >= 1100
        if (orb, wide) != (getattr(self, "_orb_base", None), getattr(self, "_orb_wide", None)) or \
                getattr(self, "_orb_style", None) != self.app.settings.get("orb_style"):
            self._orb_base, self._orb_wide, self._orb_style = orb, wide, self.app.settings.get("orb_style")
            self.orb = make_orb(orb, self.app.settings, e.width)
            self._hud_info()
        self.root.after(60, self._draw_glance)

    # ---------- input ----------
    def _placeholder(self, show):
        if show and not self.entry.get():
            self.entry.insert(0, f"Ask {self.app.settings.get('assistant_name', 'Moo') or 'Moo'} anything")
            self.entry.configure(fg=FAINT)
            self._has_placeholder = True
        elif not show and getattr(self, "_has_placeholder", False):
            self.entry.delete(0, "end")
            self.entry.configure(fg=TEXT)
            self._has_placeholder = False

    def _set_focus(self, on):
        self._focus = on
        self._draw_input()

    def _buttons(self, w, h):
        r = (h - 16) / 2
        mic = (w - 8 - r, h / 2, r - 4)
        send = (w - 8 - 2 * r - 10 - r + 6, h / 2, r - 8)
        return mic, send

    def _typing(self):
        return bool(self.entry.get().strip()) and not self._has_placeholder

    def _input_click(self, e):
        w, h = max(self.input.winfo_width(), 200), 64
        mic, send = self._buttons(w, h)
        if (e.x - mic[0]) ** 2 + (e.y - mic[1]) ** 2 <= (mic[2] + 3) ** 2:
            self.app.voice_request()
        elif self._typing() and (e.x - send[0]) ** 2 + (e.y - send[1]) ** 2 <= (send[2] + 4) ** 2:
            self._send()
        else:
            self.entry.focus_set()

    def _input_motion(self, e):
        w, h = max(self.input.winfo_width(), 200), 64
        mic, send = self._buttons(w, h)
        over = None
        if (e.x - mic[0]) ** 2 + (e.y - mic[1]) ** 2 <= (mic[2] + 3) ** 2:
            over = "mic"
        elif self._typing() and (e.x - send[0]) ** 2 + (e.y - send[1]) ** 2 <= (send[2] + 4) ** 2:
            over = "send"
        if over != self._hover:
            self._hover = over
            self.input.configure(cursor="hand2" if over else "")
            self._draw_input()

    def _draw_input(self):
        c = self.input
        w, h = max(c.winfo_width(), 200), 64
        pal = palette(self.app.settings)
        acc = _hex(pal["idle"][0])
        img, d = _canvas(w, h, _hex(BG))
        y0, y1 = 8, h - 8
        if self._focus or self.state == "listening":
            glow = Image.new("RGBA", (w * S, h * S), (0, 0, 0, 0))
            ImageDraw.Draw(glow).rounded_rectangle([2 * S, (y0 - 2) * S, (w - 2) * S, (y1 + 2) * S],
                                                   radius=(h - 12) / 2 * S, fill=acc + (70,))
            from PIL import ImageFilter
            img.paste(glow.filter(ImageFilter.GaussianBlur(8)), (0, 0), glow.filter(ImageFilter.GaussianBlur(8)))
        d.rounded_rectangle([4 * S, y0 * S, (w - 4) * S, y1 * S], radius=(y1 - y0) / 2 * S, fill=_hex(SURFACE),
                            outline=_mix(_hex(SURFACE), acc, 0.55) if self._focus else _mix(_hex(BG), (255, 255, 255), 0.1),
                            width=S)
        (mx, my, mr), (sx, sy, sr) = self._buttons(w, h)
        listening = self.state == "listening"
        mfill = _hex(pal["listening"][1]) if listening else acc
        if self._hover == "mic":
            mfill = _mix(mfill, (255, 255, 255), 0.15)
        d.ellipse([(mx - mr) * S, (my - mr) * S, (mx + mr) * S, (my + mr) * S], fill=mfill)
        _icon(d, "stop" if listening else "mic", mx * S, my * S, (mr * 1.15) * S, _hex(BG))
        if self._typing():
            sfill = _mix(_hex(SURFACE_HI), (255, 255, 255), 0.1 if self._hover == "send" else 0.03)
            d.ellipse([(sx - sr) * S, (sy - sr) * S, (sx + sr) * S, (sy + sr) * S], fill=sfill)
            _icon(d, "send", sx * S, sy * S, sr * 1.1 * S, INK)
        c.itemconfigure(self._pill_item, image=self._keep("pill", _done(img)))
        r = (y1 - y0) / 2
        c.coords(self._entry_win, 4 + r * 0.8, h / 2)
        right = (2 * mr + 2 * sr + 34) if self._typing() else (2 * mr + 22)
        c.itemconfigure(self._entry_win, width=max(60, int(w - 8 - r * 0.8 - right)))

    def _send(self, _event=None):
        if getattr(self, "_has_placeholder", False):
            return
        text = self.entry.get().strip()
        if text:
            self.entry.delete(0, "end")
            self._draw_input()
            self.app.text_request(text)

    # ---------- animation ----------
    def _animate(self):
        t = time.time() - self.t0
        ears = getattr(self.app, "ears", None)
        notes = bool(ears and getattr(ears, "notes_started", None)) or bool(
            getattr(self.app, "recorder", None) and self.app.recorder.running)
        if notes or getattr(self, "_chip_notes", False):
            if int(t) != getattr(self, "_chip_tick", -1):
                self._chip_tick = int(t)
                self._draw_chip()
            self._chip_notes = notes
        level = self.app.mouth.level if (self.app.mouth and self.state == "speaking") else 0.0
        try:
            self._orb_img = ImageTk.PhotoImage(self.orb.render(self.state, t, level, palette(self.app.settings)))
            self.canvas.configure(image=self._orb_img)
        except Exception:
            pass
        self.root.after(33, self._animate)

    # ---------- settings & appearance ----------
    def pick_files(self):
        from tkinter import filedialog
        vids = "*.mp4 *.avi *.mov *.mkv *.wmv *.mpg *.mpeg *.m4v *.asf *.ts *.mts *.vob"
        paths = filedialog.askopenfilenames(parent=self.root, title="Attach files for Moo", filetypes=[
            ("Videos, photos, drawings, documents", vids + " *.jpg *.jpeg *.png *.pdf *.dxf *.docx *.xlsx"),
            ("Survey videos", vids), ("All files", "*.*")])
        self.app.attach_files(list(paths or []))

    def open_settings(self):
        from .settings_ui import SettingsWindow
        if getattr(self, "_settings", None) and self._settings.win.winfo_exists():
            self._settings.win.lift()
            return
        self._settings = SettingsWindow(self)

    def open_office(self):
        from .office import OfficeWindow
        if getattr(self, "_office", None) and self._office.win.winfo_exists():
            self._office.win.lift()
            return
        self._office = OfficeWindow(self)

    def apply_appearance(self):
        s = self.app.settings
        if getattr(self, "_orb_style", None) != s.get("orb_style") and getattr(self, "_orb_base", None):
            self._orb_style = s.get("orb_style")
            self.orb = make_orb(self._orb_base, s, self.root.winfo_width())
            self._hud_info()
        name = s.get("assistant_name", "Moo") or "Moo"
        self.root.title(name)
        title = (s.get("header_title") or "").strip()
        if title:  # e.g. "MOO MY AGENT": first word bold, the rest in her colour
            first, _, rest = title.partition(" ")
            self.title_label.configure(text=first)
            self.title_rest.configure(text=rest, fg=palette(s)["idle"][0])
            self.root.title(title)
        else:
            self.title_label.configure(text=name.lower())
            self.title_rest.configure(text="")
        big, small = TEXT_SIZES.get(s.get("text_size", "Medium"), TEXT_SIZES["Medium"])
        self.chat.tag_configure("moo", font=(FONT, big))
        self.chat.tag_configure("sys", font=(FONT, max(8, small - 2)))
        self.chat.tag_configure("who_moo", foreground=palette(s)["idle"][0])
        self.entry.configure(font=(FONT, small))
        try:
            self.root.attributes("-topmost", bool(s.get("always_on_top", False)))
            self.root.attributes("-alpha", max(0.6, min(float(s.get("window_opacity", 100)) / 100, 1.0)))
        except Exception:
            pass
        self.root.after(10, self._draw_input)
        self.root.after(10, self._draw_chip)
        self.root.after(20, lambda: self._draw_glance(force=True))
        if not self._chatted:
            self.root.after(20, self._build_suggestions)
        if self._has_placeholder:
            self.entry.delete(0, "end")
            self._has_placeholder = False
            self._placeholder(True)

    def ask_api_key(self):
        from tkinter import simpledialog
        key = simpledialog.askstring("Moo setup", "Paste your Claude API key (from console.anthropic.com):",
                                     show="•", parent=self.root)
        if key and key.strip().startswith("sk-ant-"):
            set_api_key(key.strip())
            return True
        return False

    def run(self):
        self.root.mainloop()
