"""Moo's office: a live board of her background workers, wired to her core.

Rendered with Pillow (2x supersampled, then smoothed down) for crisp,
anti-aliased cards, soft shadows, glowing wires and a shaded orb. The heavy
"static" layer is only redrawn when something changes; each animation frame
just adds the orb, pulses and status lights on top.
"""
import math
import time
import tkinter as tk

import numpy as np
from PIL import Image, ImageDraw, ImageFilter, ImageTk

from .cards import _font
from .config import save_settings
from .themes import BG, palette

S = 2
GREEN, AMBER, RED, GREY = (74, 222, 128), (251, 191, 36), (248, 113, 113), (90, 98, 116)
INK, MUTED_C, FAINT_C = (232, 236, 243), (138, 147, 166), (88, 96, 114)

WORKERS = [  # key, title, what it does, on/off setting, can run now, icon
    ("email", "Inbox watch", "Flags important new emails", "email_alerts", True, "mail"),
    ("meetings", "Meetings", "Warns before meetings, with drive time", "meeting_alerts", True, "calendar"),
    ("promises", "Promise tracker", "Promises in sent mail, quiet quotes", "promise_tracker", True, "check"),
    ("clashes", "Diary check", "Double bookings and impossible drives", "clash_alerts", True, "clash"),
    ("vh3", "VH3 · BigChange", "Critical job and SLA issues", "vh3_alerts", True, "bolt"),
    ("waiting", "Waiting on you", "Who's owed a reply · 4pm nudge", "waiting_nudge", True, "hourglass"),
    ("reminders", "Reminders", "Your reminders, on time", None, False, "bell"),
    ("phone", "Phone link", "Telegram messages and alerts", "phone_enabled", False, "phone"),
]
LABEL = {"ok": "On duty", "busy": "Working", "quiet": "Quiet hours", "warn": "Check", "error": "Problem", "off": "Paused"}


def _rgb(h):
    return tuple(int(h[i:i + 2], 16) for i in (1, 3, 5))


def _mix(a, b, t):
    return tuple(int(a[i] + (b[i] - a[i]) * t) for i in range(3))


def _ago(ts):
    if not ts:
        return "not yet"
    s = int(time.time() - ts)
    return ("just now" if s < 60 else f"{s // 60} min ago" if s < 3600 else
            f"{s // 3600} h ago" if s < 86400 else f"{s // 86400} d ago")


def _bez(p0, p1, p2, p3, t):
    u = 1 - t
    return (u ** 3 * p0[0] + 3 * u * u * t * p1[0] + 3 * u * t * t * p2[0] + t ** 3 * p3[0],
            u ** 3 * p0[1] + 3 * u * u * t * p1[1] + 3 * u * t * t * p2[1] + t ** 3 * p3[1])


def _fit(draw, text, font, width):
    text = " ".join(str(text).split())
    if draw.textlength(text, font=font) <= width:
        return text
    while text and draw.textlength(text + "…", font=font) > width:
        text = text[:-1]
    return text.rstrip() + "…"


def _glow_sprite(colour, r=11):
    """A soft glowing dot, used for the pulses travelling along the wires."""
    size = r * 4
    y, x = np.mgrid[0:size, 0:size].astype(np.float32) - (size - 1) / 2
    d = np.sqrt(x * x + y * y)
    alpha = np.clip(np.exp(-(d / (r * 0.55)) ** 2) * 1.2, 0, 1)
    core = np.clip(1 - d / (r * 0.32), 0, 1)
    rgb = np.empty((size, size, 3), np.float32)
    for i in range(3):
        rgb[..., i] = colour[i] + (255 - colour[i]) * core
    return Image.fromarray(np.dstack([rgb, alpha * 255]).astype(np.uint8), "RGBA")


def _orb(size, a, b, t, state):
    """Shaded sphere with rotating two-tone sweep, highlight and glow, with transparency."""
    c = (size - 1) / 2
    y, x = np.mgrid[0:size, 0:size].astype(np.float32)
    dx, dy = x - c, y - c
    d = np.sqrt(dx * dx + dy * dy)
    r = size * 0.27 * (1 + (0.05 if state == "listening" else 0.025) * math.sin(t * (6 if state == "listening" else 1.5)))
    spin = {"thinking": 2.4, "listening": 1.2, "speaking": 0.9}.get(state, 0.35)
    theta = np.arctan2(dy, dx)
    mix = 0.5 + 0.5 * np.cos(theta - t * spin) * np.clip(d / (r * 0.95), 0, 1) ** 1.5
    A, B = np.array(a, np.float32), np.array(b, np.float32)
    colour = A * (1 - mix[..., None]) + B * mix[..., None]
    hd = np.sqrt((dx + 0.35 * r) ** 2 + (dy + 0.4 * r) ** 2) / (r * 1.6)
    sphere = np.clip(colour * np.clip(1.15 - 0.55 * hd, 0.45, 1.15)[..., None], 0, 255)
    hl = (np.clip(1 - hd * 2.4, 0, 1) ** 2 * 0.55)[..., None]
    sphere = sphere + (255 - sphere) * hl
    inside = np.clip(r + 0.75 - d, 0, 1.5) / 1.5
    glow = np.exp(-np.clip(d - r, 0, None) / (size * 0.075)) * 0.55 * np.clip(1 - d / (size / 2), 0, 1) ** 2
    alpha = np.maximum(inside, glow * (1 - inside))
    glow_rgb = (A + B) / 2
    rgb = sphere * inside[..., None] + glow_rgb * (1 - inside[..., None])
    return Image.fromarray(np.dstack([np.clip(rgb, 0, 255), np.clip(alpha, 0, 1) * 255]).astype(np.uint8), "RGBA")


# ---------- icons (drawn at 2x inside a box) ----------
def _icon(d, kind, x, y, s, col):
    w = max(2, int(s * 0.09))
    L, T, R, Bm = x + s * 0.2, y + s * 0.26, x + s * 0.8, y + s * 0.74
    if kind == "mail":
        d.rounded_rectangle([L, T, R, Bm], radius=s * 0.06, outline=col, width=w)
        d.line([L + w, T + w, (L + R) / 2, (T + Bm) / 2 + w, R - w, T + w], fill=col, width=w, joint="curve")
    elif kind == "calendar":
        d.rounded_rectangle([L, T, R, Bm + s * 0.04], radius=s * 0.06, outline=col, width=w)
        d.line([L, T + s * 0.14, R, T + s * 0.14], fill=col, width=w)
        for i in (0.38, 0.62):
            d.line([x + s * i, T - s * 0.07, x + s * i, T + s * 0.05], fill=col, width=w)
        for gx in (0.36, 0.5, 0.64):
            d.ellipse([x + s * gx - w * 0.8, y + s * 0.6 - w * 0.8, x + s * gx + w * 0.8, y + s * 0.6 + w * 0.8], fill=col)
    elif kind == "check":
        d.ellipse([L, y + s * 0.2, R, y + s * 0.8], outline=col, width=w)
        d.line([x + s * 0.36, y + s * 0.51, x + s * 0.46, y + s * 0.61, x + s * 0.65, y + s * 0.41],
               fill=col, width=w, joint="curve")
    elif kind == "clash":
        d.rounded_rectangle([L, T, x + s * 0.62, y + s * 0.6], radius=s * 0.05, outline=col, width=w)
        d.rounded_rectangle([x + s * 0.38, y + s * 0.42, R, Bm], radius=s * 0.05, outline=col, width=w)
    elif kind == "bolt":
        d.polygon([(x + s * 0.56, y + s * 0.16), (x + s * 0.3, y + s * 0.55), (x + s * 0.48, y + s * 0.55),
                   (x + s * 0.42, y + s * 0.84), (x + s * 0.7, y + s * 0.44), (x + s * 0.52, y + s * 0.44)], fill=col)
    elif kind == "hourglass":
        d.line([L + s * 0.06, T - s * 0.04, R - s * 0.06, T - s * 0.04], fill=col, width=w)
        d.line([L + s * 0.06, Bm + s * 0.04, R - s * 0.06, Bm + s * 0.04], fill=col, width=w)
        d.line([L + s * 0.1, T, R - s * 0.1, Bm, L + s * 0.1, Bm, R - s * 0.1, T, L + s * 0.1, T], fill=col, width=w,
               joint="curve")
    elif kind == "bell":
        d.chord([x + s * 0.27, y + s * 0.2, x + s * 0.73, y + s * 0.78], 180, 360, outline=col, width=w)
        d.line([x + s * 0.27, y + s * 0.49, x + s * 0.24, y + s * 0.66, x + s * 0.76, y + s * 0.66,
                x + s * 0.73, y + s * 0.49], fill=col, width=w, joint="curve")
        d.ellipse([x + s * 0.45, y + s * 0.7, x + s * 0.55, y + s * 0.8], fill=col)
    elif kind == "phone":
        d.rounded_rectangle([x + s * 0.33, y + s * 0.16, x + s * 0.67, y + s * 0.84], radius=s * 0.08, outline=col, width=w)
        d.line([x + s * 0.45, y + s * 0.74, x + s * 0.55, y + s * 0.74], fill=col, width=w)


class OfficeWindow:
    CW, CH = 304, 132

    def __init__(self, ui):
        self.ui, self.app = ui, ui.app
        self.win = tk.Toplevel(ui.root)
        self.win.title(f"{self.app.settings.get('assistant_name', 'Moo')}'s office")
        self.win.configure(bg=BG)
        self.win.geometry("1120x740")
        self.win.minsize(940, 640)
        self.view = tk.Label(self.win, bg=BG, bd=0, padx=0, pady=0, highlightthickness=0)
        self.view.pack(fill="both", expand=True)
        self.view.bind("<Button-1>", self._on_click)
        self.view.bind("<Motion>", self._on_move)
        self.view.bind("<Leave>", lambda e: self._set_hover(None))
        self.t0 = time.time()
        self.hover = None
        self.hits = []  # (x0, y0, x1, y1, action)
        self.base, self.sig, self.anim = None, None, {}
        self._sprites = {}
        self._photo = None
        self._tick()

    # ---------- input ----------
    def _hit(self, e):
        for x0, y0, x1, y1, action in self.hits:
            if x0 <= e.x <= x1 and y0 <= e.y <= y1:
                return action
        return None

    def _set_hover(self, action):
        if action != self.hover:
            self.hover = action
            self.view.configure(cursor="hand2" if action else "")
            self.sig = None  # redraw buttons with hover state

    def _on_move(self, e):
        self._set_hover(self._hit(e))

    def _on_click(self, e):
        action = self._hit(e)
        if not action:
            return
        s, w = self.app.settings, self.app.watcher
        kind, _, key = action.partition(":")
        if kind == "toggle":
            spec = next(x for x in WORKERS if x[0] == key)
            s[spec[3]] = not s.get(spec[3], key != "phone")
            save_settings(s)
            if key == "phone":
                self.app.phone.restart()
        elif kind == "run":
            w.run_now(key)
        elif kind == "snooze":
            if w.snoozed_until > time.time():
                w.snoozed_until = 0
            else:
                w.snooze(60)
        elif kind == "master":
            s["proactive"] = not s.get("proactive", True)
            save_settings(s)
        self.sig = None

    # ---------- data ----------
    def _state(self, key, setting):
        s, w = self.app.settings, self.app.watcher
        st = w.stats.get(key, {})
        if key == "reminders":
            items = self.app.reminders.list()
            nxt = items[0] if items else None
            return ("ok", f"{len(items)} set" + (f" · next {nxt['due'][11:16]}, {nxt['text']}" if nxt else ""),
                    "", "")
        if key == "phone":
            ph = self.app.phone
            if not s.get("phone_enabled"):
                return "off", "Switched off", "", ""
            ok = ph.paired_id() and ph.status.startswith("connected")
            return ("ok" if ok else "warn"), f"{ph.status} · {'paired' if ph.paired_id() else 'not paired yet'}", "", ""
        if key == "vh3" and not self.app.brain.vh3.enabled:
            return "off", "Not connected yet (Settings › Accounts)", "", ""
        master = s.get("proactive", True)
        if not s.get(setting, True) or not master:
            return "off", ("Paused" if master else "Proactive mode is off"), st.get("alert", ""), _ago(st.get("last"))
        if st.get("error"):
            return "error", st["error"], st.get("alert", ""), _ago(st.get("last"))
        if st.get("busy"):
            return "busy", st.get("text", "Working…"), st.get("alert", ""), "now"
        if w.quiet_now():
            return "quiet", st.get("text", "Resting in quiet hours"), st.get("alert", ""), _ago(st.get("last"))
        return "ok", st.get("text", "Waiting for its first check"), st.get("alert", ""), _ago(st.get("last"))

    # ---------- loop ----------
    def _tick(self):
        if not self.win.winfo_exists():
            return
        try:
            self._frame()
        except Exception:
            import logging
            logging.getLogger("moo").exception("Office frame failed")
        self.win.after(70, self._tick)

    def _frame(self):
        W, H = max(self.view.winfo_width(), 940), max(self.view.winfo_height(), 640)
        s, w = self.app.settings, self.app.watcher
        states = [self._state(k, st) for k, _, _, st, _, _ in WORKERS]
        snoozed = w.snoozed_until > time.time()
        sig = (W, H, tuple(states), self.hover, s.get("theme"), s.get("proactive", True), snoozed, w.quiet_now(),
               int(time.time() // 20), tuple(int(time.time() - w.stats.get(k, {}).get("alert_at", 0) < 90)
                                             for k, *_ in WORKERS))
        if sig != self.sig or self.base is None:
            self.sig = sig
            self.base = self._render_static(W, H, states, snoozed)
        self.view.configure(image=self._compose(W, H, states))

    # ---------- static layer ----------
    def _layout(self, W, H):
        top, bottom = 104, H - 28
        gap = max(12, (bottom - top - 4 * self.CH) / 3)
        cards = []
        for i in range(8):
            col, row = (0, i) if i < 4 else (1, i - 4)
            x = 32 if col == 0 else W - 32 - self.CW
            cards.append((x, top + row * (self.CH + gap), col))
        core = (W / 2, H / 2 + 6, 190)  # centre and orb box size
        return cards, core

    def _wire(self, card, core):
        x, y, col = card
        cx, cy, _ = core
        sx, sy = (x + self.CW, y + self.CH / 2) if col == 0 else (x, y + self.CH / 2)
        oy, r = cy - 10, 55
        ey = oy + max(-r * 0.8, min(r * 0.8, (sy - oy) * 0.32))
        dx = math.sqrt(r * r - (ey - oy) ** 2)
        ex = cx - dx if col == 0 else cx + dx
        mid = (sx + ex) / 2
        return (sx, sy), (mid, sy), (mid, ey), (ex, ey)

    def _render_static(self, W, H, states, snoozed):
        s, w = self.app.settings, self.app.watcher
        pal = palette(s)
        acc, acc2 = _rgb(pal["idle"][0]), _rgb(pal["idle"][1])
        bg = _rgb(BG)
        cards, core = self._layout(W, H)
        cx, cy, _ = core
        self.hits = []

        # background: vignette + accent haze + dot grid (at 1x, then upscaled)
        yy, xx = np.mgrid[0:H, 0:W].astype(np.float32)
        dist = np.sqrt(((xx - cx) / W) ** 2 + ((yy - cy) / H) ** 2)
        haze = np.clip(1 - dist * 2.2, 0, 1) ** 2 * 0.22
        vign = np.clip(dist * 0.9, 0, 0.35)
        base = np.array(bg, np.float32)[None, None, :] * (1 - vign[..., None]) + \
            (np.array(acc, np.float32) * 0.6)[None, None, :] * haze[..., None]
        img1 = Image.fromarray(np.clip(base, 0, 255).astype(np.uint8), "RGB").convert("RGBA")
        dots = ImageDraw.Draw(img1)
        dotc = _mix(bg, (120, 130, 150), 0.18)
        for gx in range(14, W, 26):
            for gy in range(14, H, 26):
                dots.point((gx, gy), fill=dotc)

        # glow layer: card shadows + wire glow, blurred at 1x
        glow = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        g = ImageDraw.Draw(glow)
        for (x, y, col), st in zip(cards, states):
            g.rounded_rectangle([x + 2, y + 8, x + self.CW - 2, y + self.CH + 10], radius=16, fill=(0, 0, 0, 150))
            if st[0] == "busy":
                g.rounded_rectangle([x - 4, y - 4, x + self.CW + 4, y + self.CH + 4], radius=18, fill=acc + (90,))
        for card, st in zip(cards, states):
            if st[0] != "off":
                pts = [_bez(*self._wire(card, core), t / 40) for t in range(41)]
                g.line(pts, fill=(acc if st[0] != "error" else RED) + (110,), width=7, joint="curve")
        glow = glow.filter(ImageFilter.GaussianBlur(9))
        img1 = Image.alpha_composite(img1, glow)

        big = img1.resize((W * S, H * S), Image.BICUBIC)
        d = ImageDraw.Draw(big)

        # wires
        for card, st in zip(cards, states):
            colour = {"off": _mix(bg, GREY, 0.45), "error": RED, "quiet": _mix(acc, AMBER, 0.6)}.get(st[0], acc)
            pts = [tuple(v * S for v in _bez(*self._wire(card, core), t / 48)) for t in range(49)]
            d.line(pts, fill=colour, width=int(1.6 * S), joint="curve")
            sx, sy = pts[0]
            d.ellipse([sx - 4 * S, sy - 4 * S, sx + 4 * S, sy + 4 * S], fill=_mix(bg, colour, 0.9))
            ex, ey = pts[-1]
            d.ellipse([ex - 3 * S, ey - 3 * S, ex + 3 * S, ey + 3 * S], fill=colour)

        # header
        name = s.get("assistant_name", "Moo") or "Moo"
        active = sum(1 for x in states if x[0] in ("ok", "busy", "quiet"))
        self._text(d, 32, 30, f"{name}'s office", "semibold", 19, INK)
        self._text(d, 32, 60, f"{len(WORKERS)} workers  ·  {active} on duty  ·  {time.strftime('%A %H:%M')}",
                   "regular", 10.5, MUTED_C)
        quiet = w.quiet_now()
        chip = (f"Paused until {time.strftime('%H:%M', time.localtime(w.snoozed_until))}" if snoozed
                else "Quiet hours" if quiet else "All systems on duty")
        cw = self._chip(d, W - 32, 44, chip, AMBER if (snoozed or quiet) else GREEN, anchor="right")
        bw = 124
        bx = W - 32 - cw - 14 - bw
        self._button(d, bx, 44, bw, "Proactive: " + ("on" if s.get("proactive", True) else "off"),
                     "master:", primary=s.get("proactive", True), accent=acc)
        self._button(d, bx - 10 - bw, 44, bw, "Resume all" if snoozed else "Pause 1 hour", "snooze:")

        # core captions and controls
        spend = self.app.brain.usage.this_month() if self.app.brain else 0
        parts = s.get("model", "").replace("claude-", "").split("-")
        model = (parts[0].title() + " " + ".".join(parts[1:])).strip()
        self._text(d, cx, cy + 74, f"{model}  ·  ${spend:.2f} this month", "regular", 10, MUTED_C, anchor="mm")

        # cards
        for (key, title, desc, setting, can_run, icon), (x, y, col), st in zip(WORKERS, cards, states):
            self._card(big, d, x, y, key, title, desc, setting, can_run, icon, st, acc, acc2)

        return big.resize((W, H), Image.LANCZOS)

    def _text(self, d, x, y, text, kind, size, fill, anchor="lm", maxw=None):
        f = _font(kind, size)
        if maxw:
            text = _fit(d, text, f, maxw * S)
        d.text((x * S, y * S), text, font=f, fill=fill, anchor=anchor)

    def _chip(self, d, x, y, text, colour, anchor="left"):
        f = _font("semibold", 9.5)
        tw = d.textlength(text, font=f) / S
        w = tw + 34
        x0 = x - w if anchor == "right" else x
        d.rounded_rectangle([x0 * S, (y - 13) * S, (x0 + w) * S, (y + 13) * S], radius=13 * S,
                            fill=_mix(_rgb(BG), colour, 0.16), outline=_mix(_rgb(BG), colour, 0.38), width=S)
        d.ellipse([(x0 + 12) * S, (y - 3.5) * S, (x0 + 19) * S, (y + 3.5) * S], fill=colour)
        d.text(((x0 + 25) * S, y * S), text, font=f, fill=colour, anchor="lm")
        return w

    def _button(self, d, x, y, w, text, action, primary=False, accent=None, h=28):
        hov = self.hover == action
        bg = _rgb(BG)
        if primary:
            fill, ink, edge = (_mix(accent, (255, 255, 255), 0.12) if hov else accent), (12, 14, 20), None
        else:
            fill, ink = (_mix(bg, (255, 255, 255), 0.13) if hov else _mix(bg, (255, 255, 255), 0.07)), INK
            edge = _mix(bg, (255, 255, 255), 0.16)
        d.rounded_rectangle([x * S, (y - h / 2) * S, (x + w) * S, (y + h / 2) * S], radius=h / 2 * S, fill=fill,
                            outline=edge, width=S if edge else 0)
        d.text(((x + w / 2) * S, y * S), text, font=_font("semibold", 9.5), fill=ink, anchor="mm")
        self.hits.append((x, y - h / 2, x + w, y + h / 2, action))

    def _card(self, img, d, x, y, key, title, desc, setting, can_run, icon, st, acc, acc2):
        kind, text, alert, last = st
        bg = _rgb(BG)
        surface = _mix(bg, (255, 255, 255), 0.055)
        off = kind == "off"
        X0, Y0, X1, Y1 = int(x * S), int(y * S), int((x + self.CW) * S), int((y + self.CH) * S)
        d.rounded_rectangle([X0, Y0, X1, Y1], radius=16 * S, fill=surface,
                            outline=acc if kind == "busy" else _mix(bg, (255, 255, 255), 0.11), width=S)
        # top sheen (glass feel)
        sheen = Image.new("L", (X1 - X0, 40 * S), 0)
        sd = ImageDraw.Draw(sheen)
        for i in range(40 * S):
            sd.line([(0, i), (X1 - X0, i)], fill=int(14 * (1 - i / (40 * S))))
        mask = Image.new("L", (X1 - X0, 40 * S), 0)
        ImageDraw.Draw(mask).rounded_rectangle([S, S, X1 - X0 - S, 80 * S], radius=16 * S, fill=255)
        white = Image.new("RGBA", (X1 - X0, 40 * S), (255, 255, 255, 255))
        white.putalpha(Image.fromarray(np.minimum(np.array(sheen), np.array(mask))))
        img.alpha_composite(white, (X0, Y0))
        # icon tile
        tile = acc if not off else GREY
        d.rounded_rectangle([(x + 16) * S, (y + 16) * S, (x + 50) * S, (y + 50) * S], radius=10 * S,
                            fill=_mix(surface, tile, 0.2))
        _icon(d, icon, (x + 16) * S, (y + 16) * S, 34 * S, _mix(tile, (255, 255, 255), 0.25))
        # title + status
        self._text(d, x + 62, y + 44, desc, "regular", 8.8, FAINT_C, maxw=self.CW - 76)
        colour = {"ok": GREEN, "busy": acc2, "quiet": AMBER, "warn": AMBER, "error": RED, "off": GREY}[kind]
        f = _font("semibold", 8.5)
        lab = LABEL[kind]
        tw = d.textlength(lab, font=f) / S
        cx1 = x + self.CW - 14
        cx0 = cx1 - tw - 26
        d.rounded_rectangle([cx0 * S, (y + 17) * S, cx1 * S, (y + 35) * S], radius=9 * S, fill=_mix(surface, colour, 0.16))
        d.text(((cx0 + 18) * S, (y + 26) * S), lab, font=f, fill=colour, anchor="lm")
        self.anim[key] = (cx0 + 10, y + 26, colour, kind)  # status light, drawn per frame
        self._text(d, x + 62, y + 26, title, "semibold", 12.5, INK if not off else MUTED_C, maxw=cx0 - x - 70)
        # main line + last alert
        self._text(d, x + 16, y + 72, text, "regular", 10.2, INK if not off else MUTED_C, maxw=self.CW - 32)
        if alert:
            d.rounded_rectangle([(x + 16) * S, (y + 84) * S, (x + 19) * S, (y + 98) * S], radius=S, fill=acc2)
            self._text(d, x + 25, y + 91, alert, "regular", 9, MUTED_C, maxw=self.CW - 42)
        # footer
        if last:
            self._text(d, x + 16, y + self.CH - 17, f"Last check {last}", "regular", 8.5, FAINT_C)
        bx = x + self.CW - 12
        if setting:
            bw = 62
            bx -= bw
            self._button(d, bx, y + self.CH - 18, bw, "Resume" if off else "Pause", f"toggle:{key}", h=24,
                         primary=off, accent=acc)
        if can_run and not off:
            bw = 70
            bx -= bw + 6
            self._button(d, bx, y + self.CH - 18, bw, "Run now", f"run:{key}", h=24)

    # ---------- per-frame layer ----------
    def _sprite(self, colour):
        if colour not in self._sprites:
            self._sprites[colour] = _glow_sprite(colour)
        return self._sprites[colour]

    def _compose(self, W, H, states):
        s = self.app.settings
        pal = palette(s)
        acc, acc2 = _rgb(pal["idle"][0]), _rgb(pal["idle"][1])
        t = time.time() - self.t0
        frame = self.base.copy()
        cards, core = self._layout(W, H)
        cx, cy, size = core
        # orb
        ui_state = self.ui.state if self.ui.state in ("listening", "thinking", "speaking") else "idle"
        a, b = (_rgb(x) for x in pal.get(ui_state, pal["idle"]))
        orb = _orb(size, a, b, t, ui_state)
        frame.alpha_composite(orb, (int(cx - size / 2), int(cy - size / 2 - 10)))
        # pulses along busy / recently alerting wires
        sprite = self._sprite(acc2)
        for (key, *_), card, st in zip(WORKERS, cards, states):
            alerted = time.time() - self.app.watcher.stats.get(key, {}).get("alert_at", 0) < 90
            if st[0] == "busy" or alerted:
                p = self._wire(card, core)
                for k in range(3):
                    px, py = _bez(*p, ((t * 0.45) + k / 3) % 1.0)
                    frame.alpha_composite(sprite, (int(px - sprite.width / 2), int(py - sprite.height / 2)))
        # status lights (breathing when working)
        d = ImageDraw.Draw(frame)
        for key, (lx, ly, colour, kind) in self.anim.items():
            r = 3.4 + (1.4 * (1 + math.sin(t * 5)) / 2 if kind == "busy" else 0)
            d.ellipse([lx - r, ly - r, lx + r, ly + r], fill=colour)
        # name on the orb
        name = (s.get("assistant_name", "Moo") or "Moo").upper()
        f = _font("semibold", 9)  # _font sizes are 2x; this is ~18px on the 1x frame
        d.text((cx + 1, cy - 9), name, font=f, fill=(0, 0, 0, 90), anchor="mm")
        d.text((cx, cy - 10), name, font=f, fill=(255, 255, 255), anchor="mm")
        self._photo = ImageTk.PhotoImage(frame.convert("RGB"))
        return self._photo
