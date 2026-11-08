"""Moo's office: a live board of her background workers, wired to her core.

Each card shows what that worker watches, when it last checked, what it
found, and lets you pause it or run it now. A pulse travels along the wire
while a worker is busy or has just raised something.
"""
import math
import time
import tkinter as tk

from PIL import Image, ImageDraw, ImageTk

from .config import save_settings
from .themes import BG, FAINT, FONT, MUTED, SURFACE, SURFACE_HI, TEXT, palette

GREEN, AMBER, RED = "#4ADE80", "#FBBF24", "#F87171"

WORKERS = [  # key, title, what it does, on/off setting, can run now
    ("email", "Inbox watch", "Flags important new emails", "email_alerts", True),
    ("meetings", "Meeting heads-up", "Warns before meetings, with drive time", "meeting_alerts", True),
    ("promises", "Promise tracker", "Promises in sent mail, quiet quotes", "promise_tracker", True),
    ("clashes", "Diary check", "Double bookings, impossible drives", "clash_alerts", True),
    ("vh3", "VH3 / BigChange", "Critical job and SLA issues", "vh3_alerts", True),
    ("waiting", "Waiting on you", "Who's owed a reply, 4pm nudge", "waiting_nudge", True),
    ("reminders", "Reminders", "Your reminders, on time", None, False),
    ("phone", "Phone link", "Telegram: messages and alerts", "phone_enabled", False),
]


def _ago(ts):
    if not ts:
        return "not yet"
    s = int(time.time() - ts)
    if s < 60:
        return "just now"
    if s < 3600:
        return f"{s // 60} min ago"
    if s < 86400:
        return f"{s // 3600} h ago"
    return f"{s // 86400} d ago"


def _bezier(p0, p1, p2, p3, t):
    u = 1 - t
    return (u ** 3 * p0[0] + 3 * u * u * t * p1[0] + 3 * u * t * t * p2[0] + t ** 3 * p3[0],
            u ** 3 * p0[1] + 3 * u * u * t * p1[1] + 3 * u * t * t * p2[1] + t ** 3 * p3[1])


class OfficeWindow:
    CW, CH = 268, 122  # card size

    def __init__(self, ui):
        self.ui, self.app = ui, ui.app
        self.win = tk.Toplevel(ui.root)
        self.win.title(f"{self.app.settings.get('assistant_name', 'Moo')}'s office")
        self.win.configure(bg=BG)
        self.win.geometry("1040x700")
        self.win.minsize(860, 600)
        self.c = tk.Canvas(self.win, bg=BG, highlightthickness=0)
        self.c.pack(fill="both", expand=True)
        self._bg_img, self._bg_size, self._core_img = None, None, None
        self.t0 = time.time()
        for key, *_ in WORKERS:
            for action in ("toggle", "run"):
                tag = f"btn-{key}-{action}"
                self.c.tag_bind(tag, "<Button-1>", lambda e, k=key, a=action: self._click(k, a))
                self.c.tag_bind(tag, "<Enter>", lambda e: self.c.configure(cursor="hand2"))
                self.c.tag_bind(tag, "<Leave>", lambda e: self.c.configure(cursor=""))
        for tag, fn in (("btn-snooze", self._snooze), ("btn-master", self._master)):
            self.c.tag_bind(tag, "<Button-1>", lambda e, f=fn: f())
            self.c.tag_bind(tag, "<Enter>", lambda e: self.c.configure(cursor="hand2"))
            self.c.tag_bind(tag, "<Leave>", lambda e: self.c.configure(cursor=""))
        self._tick()

    # ---------- actions ----------
    def _click(self, key, action):
        s = self.app.settings
        spec = next(w for w in WORKERS if w[0] == key)
        if action == "toggle" and spec[3]:
            s[spec[3]] = not s.get(spec[3], key != "phone")
            save_settings(s)
            if key == "phone":
                self.app.phone.restart()
        elif action == "run" and spec[4]:
            self.app.watcher.run_now(key)

    def _snooze(self):
        w = self.app.watcher
        if w.snoozed_until > time.time():
            w.snoozed_until = 0
        else:
            w.snooze(60)

    def _master(self):
        s = self.app.settings
        s["proactive"] = not s.get("proactive", True)
        save_settings(s)

    # ---------- state of each worker ----------
    def _state(self, key, setting):
        s, w = self.app.settings, self.app.watcher
        st = w.stats.get(key, {})
        if key == "reminders":
            items = self.app.reminders.list()
            nxt = items[0] if items else None
            text = f"{len(items)} set" + (f" · next {nxt['due'][11:16]} {nxt['text']}" if nxt else "")
            return "ok", text, "", ""
        if key == "phone":
            ph = self.app.phone
            if not s.get("phone_enabled"):
                return "off", "Switched off", "", ""
            paired = "paired" if ph.paired_id() else "not paired yet"
            return ("ok" if ph.paired_id() and ph.status.startswith("connected") else "warn"), \
                f"{ph.status} · {paired}", "", ""
        if key == "vh3" and not self.app.brain.vh3.enabled:
            return "off", "Not connected (⚙ Accounts)", "", ""
        on = s.get(setting, key not in ("wrap",))
        master = s.get("proactive", True)
        if not on or not master:
            return "off", "Paused" if master else "Proactive switched off", st.get("alert", ""), _ago(st.get("last"))
        if st.get("error"):
            return "error", st["error"], st.get("alert", ""), _ago(st.get("last"))
        if st.get("busy"):
            return "busy", st.get("text", "Working…"), st.get("alert", ""), "now"
        if w.quiet_now():
            return "quiet", st.get("text", "Quiet hours"), st.get("alert", ""), _ago(st.get("last"))
        return "ok", st.get("text", "Waiting for first check"), st.get("alert", ""), _ago(st.get("last"))

    # ---------- drawing ----------
    def _background(self, W, H):
        if self._bg_size == (W, H):
            return
        img = Image.new("RGB", (W, H), tuple(int(BG[i:i + 2], 16) for i in (1, 3, 5)))
        d = ImageDraw.Draw(img)
        dot = tuple(int(FAINT[i:i + 2], 16) // 2 + 10 for i in (1, 3, 5))
        for x in range(12, W, 24):
            for y in range(12, H, 24):
                d.point((x, y), fill=dot)
        self._bg_img, self._bg_size = ImageTk.PhotoImage(img), (W, H)

    def _tick(self):
        if not self.win.winfo_exists():
            return
        try:
            self._draw()
        except Exception:
            pass
        self.win.after(120, self._tick)

    def _draw(self):
        c, s = self.c, self.app.settings
        W, H = max(c.winfo_width(), 860), max(c.winfo_height(), 600)
        pal = palette(s)
        accent, accent2 = pal["idle"]
        c.delete("all")
        self._background(W, H)
        c.create_image(0, 0, image=self._bg_img, anchor="nw")
        t = time.time() - self.t0

        # header
        name = s.get("assistant_name", "Moo") or "Moo"
        states = [self._state(k, st) for k, _, _, st, _ in WORKERS]
        active = sum(1 for x in states if x[0] in ("ok", "busy", "quiet"))
        c.create_text(28, 30, text=f"{name}'s office", fill=TEXT, font=(FONT, 16, "bold"), anchor="w")
        c.create_text(28, 54, text=f"{len(WORKERS)} workers · {active} on duty", fill=MUTED, font=(FONT, 10), anchor="w")

        cx, cy, cr = W / 2, H / 2 + 14, 74
        top, bottom = 92, H - 30
        gap = (bottom - top - 4 * self.CH) / 3
        positions = []
        for i in range(8):
            col, row = (0, i) if i < 4 else (1, i - 4)
            x = 28 if col == 0 else W - 28 - self.CW
            y = top + row * (self.CH + gap)
            positions.append((x, y, col))

        # wires (under the cards)
        for (key, *_), (x, y, col), st in zip(WORKERS, positions, states):
            sx = x + self.CW if col == 0 else x
            sy = y + self.CH / 2
            ex = cx - cr if col == 0 else cx + cr
            ey = cy + (sy - cy) * 0.35
            mid = (sx + ex) / 2
            p0, p1, p2, p3 = (sx, sy), (mid, sy), (mid, ey), (ex, ey)
            colour = {"off": "#262B36", "error": RED, "quiet": "#5B4A20"}.get(st[0], accent)
            c.create_line(*p0, *p1, *p2, *p3, smooth=True, splinesteps=24, fill=colour, width=1.5)
            alerted = time.time() - self.app.watcher.stats.get(key, {}).get("alert_at", 0) < 90
            if st[0] == "busy" or alerted:
                for k in range(2):
                    px, py = _bezier(p0, p1, p2, p3, ((t * 0.6) + k * 0.5) % 1.0)
                    c.create_oval(px - 4, py - 4, px + 4, py + 4, fill=accent2, outline="")
            c.create_oval(sx - 3, sy - 3, sx + 3, sy + 3, fill=colour, outline="")

        # core
        self._core(cx, cy, cr, accent, accent2, t)

        # cards
        for (key, title, desc, setting, can_run), (x, y, col), st in zip(WORKERS, positions, states):
            self._card(x, y, key, title, desc, setting, can_run, st, accent, t)

    def _core(self, cx, cy, r, a, b, t):
        c, s, w = self.c, self.app.settings, self.app.watcher
        pulse = 1 + 0.03 * math.sin(t * 1.6)
        for i in range(8, 0, -1):  # glow
            rr = r * pulse + i * 7
            c.create_oval(cx - rr, cy - rr, cx + rr, cy + rr, outline="", fill=self._mix(BG, a, 0.03 * (9 - i)))
        rr = r * pulse
        c.create_oval(cx - rr, cy - rr, cx + rr, cy + rr, fill=self._mix(BG, a, 0.35), outline=a, width=2)
        c.create_oval(cx - rr * 0.55, cy - rr * 0.55, cx + rr * 0.55, cy + rr * 0.55, fill=self._mix(a, b, 0.5), outline="")
        name = (s.get("assistant_name", "Moo") or "Moo").upper()
        c.create_text(cx, cy - 6, text=name, fill=TEXT, font=(FONT, 15, "bold"))
        state = {"idle": "Ready", "listening": "Listening", "thinking": "Thinking", "speaking": "Speaking"}.get(
            self.ui.state, "Starting")
        c.create_text(cx, cy + 16, text=state, fill=TEXT, font=(FONT, 9))
        spend = self.app.brain.usage.this_month() if self.app.brain else 0
        model = s.get("model", "").replace("claude-", "").replace("-", " ").title()
        c.create_text(cx, cy + r + 30, text=f"{model} · ${spend:.2f} this month", fill=MUTED, font=(FONT, 9))
        snoozed = w.snoozed_until > time.time()
        quiet = w.quiet_now()
        status = (f"Paused until {time.strftime('%H:%M', time.localtime(w.snoozed_until))}" if snoozed
                  else ("Quiet hours" if quiet else "On duty"))
        c.create_text(cx, cy + r + 50, text=status, fill=AMBER if (snoozed or quiet) else GREEN, font=(FONT, 9, "bold"))
        self._pill(cx - 6, cy + r + 78, "Resume" if snoozed else "Pause 1 hour", "btn-snooze", anchor="e")
        self._pill(cx + 6, cy + r + 78, "Proactive: " + ("on" if s.get("proactive", True) else "off"), "btn-master",
                   anchor="w", strong=s.get("proactive", True))

    def _card(self, x, y, key, title, desc, setting, can_run, st, accent, t):
        c = self.c
        kind, text, alert, last = st
        c.create_rectangle(x, y, x + self.CW, y + self.CH, fill=SURFACE,
                           outline=accent if kind == "busy" else "#232836", width=1)
        dot = {"ok": GREEN, "busy": accent, "quiet": AMBER, "warn": AMBER, "error": RED, "off": "#4A5263"}[kind]
        r = 4 + (1.5 * (1 + math.sin(t * 6)) if kind == "busy" else 0)
        c.create_oval(x + 16 - r, y + 20 - r, x + 16 + r, y + 20 + r, fill=dot, outline="")
        c.create_text(x + 28, y + 20, text=title, fill=TEXT, font=(FONT, 11, "bold"), anchor="w")
        label = {"ok": "on duty", "busy": "working", "quiet": "quiet hours", "warn": "check", "error": "problem",
                 "off": "off"}[kind]
        c.create_text(x + self.CW - 14, y + 20, text=label, fill=dot, font=(FONT, 8, "bold"), anchor="e")
        c.create_text(x + 16, y + 40, text=desc, fill=FAINT, font=(FONT, 8), anchor="w")
        c.create_text(x + 16, y + 60, text=self._fit(text, 40), fill=TEXT if kind != "off" else MUTED,
                      font=(FONT, 9), anchor="w")
        if alert:
            c.create_text(x + 16, y + 78, text="↳ " + self._fit(alert, 42), fill=MUTED, font=(FONT, 8), anchor="w")
        if last:
            c.create_text(x + 16, y + self.CH - 14, text=f"Last check {last}", fill=FAINT, font=(FONT, 8), anchor="w")
        bx = x + self.CW - 12
        if can_run and kind not in ("off",):
            bx = self._pill(bx, y + self.CH - 16, "Run now", f"btn-{key}-run", anchor="e") - 6
        if setting:
            self._pill(bx, y + self.CH - 16, "Resume" if kind == "off" else "Pause", f"btn-{key}-toggle", anchor="e")

    def _pill(self, x, y, text, tag, anchor="e", strong=False):
        c = self.c
        w = len(text) * 6.4 + 18
        x0 = x - w if anchor == "e" else x
        fill = palette(self.app.settings)["idle"][0] if strong else SURFACE_HI
        c.create_rectangle(x0, y - 10, x0 + w, y + 10, fill=fill, outline="", tags=(tag,))
        c.create_text(x0 + w / 2, y, text=text, fill=BG if strong else TEXT, font=(FONT, 8), tags=(tag,))
        return x0

    @staticmethod
    def _fit(text, n):
        text = " ".join(str(text).split())
        return text if len(text) <= n else text[: n - 1] + "…"

    @staticmethod
    def _mix(a, b, t):
        ra = [int(a[i:i + 2], 16) for i in (1, 3, 5)]
        rb = [int(b[i:i + 2], 16) for i in (1, 3, 5)]
        return "#" + "".join(f"{int(ra[i] + (rb[i] - ra[i]) * t):02x}" for i in range(3))
