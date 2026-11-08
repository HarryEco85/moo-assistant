"""Moo's window: a calm, minimal design.

A soft glowing orb shows what Moo is doing (blue = ready, cyan = listening,
violet = thinking, and it moves with her voice when speaking). Below it,
the conversation and a rounded input bar with the mic button.
"""
import math
import queue
import time
import tkinter as tk

import numpy as np
from PIL import Image, ImageTk

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


class MooWindow:
    def __init__(self, app):
        self.app = app
        self.root = tk.Tk()
        self.root.title("Moo")
        self.root.geometry("460x760")
        self.root.minsize(380, 560)
        self.root.configure(bg=BG)
        self.calls = queue.Queue()
        self.state = "offline"
        self.t0 = time.time()
        self.orb = Orb(240)
        self._orb_img = None
        self._has_placeholder = False
        self._card_images = []
        self.chat_width = 420
        self._build()
        self.apply_appearance()
        self.root.after(33, self._animate)
        self.root.after(50, self._pump)
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
                fn(*args)
        except queue.Empty:
            pass
        self.root.after(50, self._pump)

    def set_state(self, state, detail=""):
        self.later(self._set_state, state, detail)

    def _set_state(self, state, detail):
        self.state = state
        label = detail or LABELS.get(state, state)
        self.status.configure(text=label, fg=TEXT if state in ("listening", "thinking", "speaking") else MUTED)
        self._draw_input()

    def add_message(self, who, text):
        self.later(self._add_message, who, text)

    def _add_message(self, who, text):
        self.chat.configure(state="normal")
        tag = {"You": "you", "Moo": "moo"}.get(who, "sys")
        self.chat.insert("end", text.strip() + "\n", tag)
        self.chat.insert("end", "\n", "gap")
        self.chat.configure(state="disabled")
        self.chat.see("end")

    def add_action(self, label, callback):
        self.later(self._add_action, label, callback)

    def _add_action(self, label, callback):
        btn = tk.Label(self.chat, text=label, bg=palette(self.app.settings)["idle"][0], fg=BG,
                       font=(FONT, 10, "bold"), padx=14, pady=6, cursor="hand2")
        btn.bind("<Button-1>", lambda e: (btn.configure(text="Updating…"), callback()))
        self.chat.configure(state="normal")
        self.chat.insert("end", " ", "sys")
        self.chat.window_create("end", window=btn)
        self.chat.insert("end", "\n\n", "gap")
        self.chat.configure(state="disabled")
        self.chat.see("end")

    def add_card(self, pil_image):
        self.later(self._add_card, pil_image)

    def _add_card(self, pil_image):
        photo = ImageTk.PhotoImage(pil_image)
        self._card_images.append(photo)  # keep a reference or Tk drops the picture
        self._card_images = self._card_images[-30:]
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
        header = tk.Frame(self.root, bg=BG)
        header.pack(side="top", fill="x", padx=22, pady=(16, 0))
        self.title_label = tk.Label(header, text="", bg=BG, fg=TEXT, font=(FONT, 15, "bold"))
        self.title_label.pack(side="left")
        for symbol, cmd, tip in (("⚙", self.open_settings, "Settings"), ("◼", self.app.stop_speaking, "Stop")):
            b = tk.Label(header, text=symbol, bg=BG, fg=FAINT, font=("Segoe UI Symbol", 13), cursor="hand2")
            b.pack(side="right", padx=(14, 0))
            b.bind("<Button-1>", lambda e, c=cmd: c())
            b.bind("<Enter>", lambda e, w=b: w.configure(fg=TEXT))
            b.bind("<Leave>", lambda e, w=b: w.configure(fg=FAINT))

        # bottom first, so it is never pushed off-screen
        self.footer = tk.Label(self.root, text="", bg=BG, fg=FAINT, font=(FONT, 8))
        self.footer.pack(side="bottom", pady=(0, 10))
        self.hint = tk.Label(self.root, text="", bg=BG, fg=FAINT, font=(FONT, 9))
        self.hint.pack(side="bottom", pady=(2, 2))
        self.input = tk.Canvas(self.root, height=56, bg=BG, highlightthickness=0)
        self.input.pack(side="bottom", fill="x", padx=18, pady=(8, 0))
        self.entry = tk.Entry(self.input, bg=SURFACE, fg=TEXT, relief="flat", bd=0, insertbackground=TEXT,
                              font=(FONT, 11), highlightthickness=0)
        self.entry.bind("<Return>", self._send)
        self.entry.bind("<FocusIn>", lambda e: self._placeholder(False))
        self.entry.bind("<FocusOut>", lambda e: self._placeholder(True))
        self._entry_win = self.input.create_window(0, 0, window=self.entry, anchor="w")
        self.input.bind("<Configure>", lambda e: self._draw_input())
        self.input.tag_bind("mic", "<Button-1>", lambda e: self.app.voice_request())
        self.input.tag_bind("mic", "<Enter>", lambda e: self.input.configure(cursor="hand2"))
        self.input.tag_bind("mic", "<Leave>", lambda e: self.input.configure(cursor=""))
        self._placeholder(True)

        self.canvas = tk.Label(self.root, bg=BG, bd=0)
        self.canvas.pack(side="top", pady=(4, 0))
        self.status = tk.Label(self.root, text="Starting up", bg=BG, fg=MUTED, font=(FONT, 10))
        self.status.pack(side="top", pady=(0, 10))

        self.chat = tk.Text(self.root, bg=BG, fg=TEXT, wrap="word", relief="flat", bd=0, padx=24, pady=6,
                            font=(FONT, 11), state="disabled", cursor="arrow", highlightthickness=0,
                            spacing1=1, spacing3=1, insertbackground=BG, selectbackground=SURFACE_HI)
        self.chat.pack(side="top", fill="both", expand=True)
        self.chat.bind("<Configure>", lambda e: setattr(self, "chat_width", e.width))
        self.chat.tag_configure("moo", foreground=TEXT, font=(FONT, 12), rmargin=40, spacing2=3)
        self.chat.tag_configure("you", foreground=MUTED, font=(FONT, 11), justify="right", lmargin1=70, lmargin2=70)
        self.chat.tag_configure("sys", foreground=FAINT, font=(FONT, 9), justify="center", lmargin1=30, lmargin2=30,
                                rmargin=30)
        self.chat.tag_configure("gap", font=(FONT, 6))
        self.chat.tag_configure("draft_label", foreground=FAINT, font=(FONT, 8, "bold"), lmargin1=12, spacing1=4)
        self.chat.tag_configure("draft", foreground=TEXT, background=SURFACE, font=(FONT, 10), lmargin1=12,
                                lmargin2=12, rmargin=24, spacing1=2, spacing3=2)

    def _placeholder(self, show):
        if show and not self.entry.get():
            self.entry.insert(0, f"Ask {self.app.settings.get('assistant_name', 'Moo') or 'Moo'} anything")
            self.entry.configure(fg=FAINT)
            self._has_placeholder = True
        elif not show and getattr(self, "_has_placeholder", False):
            self.entry.delete(0, "end")
            self.entry.configure(fg=TEXT)
            self._has_placeholder = False

    def _draw_input(self):
        c = self.input
        c.delete("pill")
        w, h = max(c.winfo_width(), 200), 56
        y0, y1, r = 6, h - 6, (h - 12) / 2
        # rounded pill
        c.create_oval(2, y0, 2 + 2 * r, y1, fill=SURFACE, outline="", tags="pill")
        c.create_oval(w - 2 - 2 * r, y0, w - 2, y1, fill=SURFACE, outline="", tags="pill")
        c.create_rectangle(2 + r, y0, w - 2 - r, y1, fill=SURFACE, outline="", tags="pill")
        # mic button
        listening = self.state == "listening"
        mx, my, mr = w - 2 - r, h / 2, r - 6
        pal = palette(self.app.settings)
        c.create_oval(mx - mr, my - mr, mx + mr, my + mr, fill=pal["listening"][1] if listening else pal["idle"][0],
                      outline="", tags=("pill", "mic"))
        # simple microphone glyph
        g = BG
        c.create_rectangle(mx - 3, my - 9, mx + 3, my + 3, fill=g, outline=g, tags=("pill", "mic"))
        c.create_oval(mx - 3, my - 11, mx + 3, my - 6, fill=g, outline=g, tags=("pill", "mic"))
        c.create_oval(mx - 3, my, mx + 3, my + 5, fill=g, outline=g, tags=("pill", "mic"))
        c.create_arc(mx - 7, my - 6, mx + 7, my + 7, start=200, extent=140, style="arc", outline=g, width=2,
                     tags=("pill", "mic"))
        c.create_line(mx, my + 7, mx, my + 11, fill=g, width=2, tags=("pill", "mic"))
        c.tag_lower("pill")
        c.coords(self._entry_win, 2 + r, h / 2)
        c.itemconfigure(self._entry_win, width=max(60, w - 4 - 3 * r - 16))

    def _send(self, _event=None):
        if getattr(self, "_has_placeholder", False):
            return
        text = self.entry.get().strip()
        if text:
            self.entry.delete(0, "end")
            self.app.text_request(text)

    # ---------- animation ----------
    def _animate(self):
        t = time.time() - self.t0
        level = self.app.mouth.level if (self.app.mouth and self.state == "speaking") else 0.0
        try:
            self._orb_img = ImageTk.PhotoImage(self.orb.render(self.state, t, level, palette(self.app.settings)))
            self.canvas.configure(image=self._orb_img)
        except Exception:
            pass
        self.root.after(33, self._animate)

    # ---------- settings & appearance ----------
    def open_settings(self):
        from .settings_ui import SettingsWindow
        if getattr(self, "_settings", None) and self._settings.win.winfo_exists():
            self._settings.win.lift()
            return
        self._settings = SettingsWindow(self)

    def apply_appearance(self):
        s = self.app.settings
        name = s.get("assistant_name", "Moo") or "Moo"
        self.root.title(name)
        self.title_label.configure(text=name.lower())
        big, small = TEXT_SIZES.get(s.get("text_size", "Medium"), TEXT_SIZES["Medium"])
        self.chat.tag_configure("moo", font=(FONT, big))
        self.chat.tag_configure("you", font=(FONT, small))
        self.chat.tag_configure("sys", font=(FONT, max(8, small - 2)))
        self.entry.configure(font=(FONT, small))
        try:
            self.root.attributes("-topmost", bool(s.get("always_on_top", False)))
            self.root.attributes("-alpha", max(0.6, min(float(s.get("window_opacity", 100)) / 100, 1.0)))
        except Exception:
            pass
        self.root.after(10, self._draw_input)
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

