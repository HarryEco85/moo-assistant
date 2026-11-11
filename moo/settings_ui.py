"""Moo's settings window: six tabs, and almost everything applies on Save
without restarting."""
import threading
import tkinter as tk
from tkinter import messagebox, ttk

from .config import get_api_key, get_azure_key, get_secret, save_settings, set_api_key, set_azure_key, set_secret
from .themes import BG, FAINT, FONT, MUTED, SURFACE, SURFACE_HI, TEXT, THEMES, accent

TEXT_SIZES = ["Small", "Medium", "Large"]
HUMOUR_CHOICES = [("straight", "Straight: warm, no jokes"), ("light", "Light: the odd dry remark"),
                  ("quirky", "Quirky: witty and playful"), ("comedian", "Full comedian")]
LENGTH_CHOICES = [("short", "Short and snappy"), ("normal", "Normal"), ("detailed", "Detailed")]
WHISPER_CHOICES = [("tiny.en", "Fastest (least accurate)"), ("base.en", "Fast"), ("small.en", "Balanced"),
                   ("medium.en", "Most accurate (best with a graphics card)")]
MODEL_CHOICES = [("claude-haiku-5-5", "Haiku: fast and cheapest"), ("claude-sonnet-5-5", "Sonnet: smarter, about 20x the cost")]
PLAN_MODEL_CHOICES = [("", "Same as above"), ("claude-sonnet-5-5", "Sonnet: better at long jobs, costs more")]


class SettingsWindow:
    def __init__(self, ui):
        self.ui, self.app, self.s = ui, ui.app, ui.app.settings
        self.vars = {}
        self.voices = []
        win = self.win = tk.Toplevel(ui.root)
        win.title(f"{self.s.get('assistant_name', 'Moo')} settings")
        win.configure(bg=BG)
        h = min(760, win.winfo_screenheight() - 120)
        win.geometry(f"860x{h}")
        win.minsize(520, 420)
        win.transient(ui.root)
        self._style()

        # Save bar first, pinned to the bottom, so it's always visible
        bar = tk.Frame(win, bg=BG)
        bar.pack(side="bottom", fill="x", padx=18, pady=14)
        self._button(bar, "Save", self.save, primary=True).pack(side="right")
        self._button(bar, "Cancel", win.destroy).pack(side="right", padx=(0, 8))
        tk.Label(bar, text="Changes apply straight away.", bg=BG, fg=FAINT, font=(FONT, 8)).pack(side="left")

        nb = ttk.Notebook(win, style="Moo.TNotebook")
        nb.pack(side="top", fill="both", expand=True, padx=14, pady=(14, 0))
        self.tab_voice = self._tab(nb, "Voice")
        self.tab_look = self._tab(nb, "Look")
        self.tab_persona = self._tab(nb, "Personality")
        self.tab_listen = self._tab(nb, "Listening")
        self.tab_pro = self._tab(nb, "Proactive")
        self.tab_skills = self._tab(nb, "Skills")
        self.tab_accounts = self._tab(nb, "Accounts")
        self.tab_phone = self._tab(nb, "Telegram")
        self.tab_app = self._tab(nb, "Phone app")
        self.tab_system = self._tab(nb, "System")
        self._build_voice()
        self._build_look()
        self._build_persona()
        self._build_listening()
        self._build_proactive()
        self._build_skills()
        self._build_accounts()
        self._build_phone()
        self._build_app()
        self._build_system()

        win.bind_all("<MouseWheel>", self._wheel, add="+")
        win.bind("<Destroy>", lambda e: e.widget is win and win.unbind_all("<MouseWheel>"))

    # ---------- styling helpers ----------
    def _style(self):
        st = ttk.Style(self.win)
        try:
            st.theme_use("clam")
        except tk.TclError:
            pass
        st.configure("Moo.TNotebook", background=BG, borderwidth=0, tabmargins=0)
        st.configure("Moo.TNotebook.Tab", background=BG, foreground=MUTED, padding=(12, 7), borderwidth=0,
                     font=(FONT, 9))
        st.map("Moo.TNotebook.Tab", background=[("selected", SURFACE)], foreground=[("selected", TEXT)])
        st.configure("Moo.TFrame", background=SURFACE)
        st.configure("Moo.TCombobox", fieldbackground=SURFACE_HI, background=SURFACE_HI, foreground=TEXT,
                     arrowcolor=MUTED, bordercolor=SURFACE_HI, lightcolor=SURFACE_HI, darkcolor=SURFACE_HI)
        st.map("Moo.TCombobox", fieldbackground=[("readonly", SURFACE_HI)], foreground=[("readonly", TEXT)],
               selectbackground=[("readonly", SURFACE_HI)], selectforeground=[("readonly", TEXT)])
        self.win.option_add("*TCombobox*Listbox.background", SURFACE_HI)
        self.win.option_add("*TCombobox*Listbox.foreground", TEXT)
        self.win.option_add("*TCombobox*Listbox.selectBackground", accent(self.s))
        self.win.option_add("*TCombobox*Listbox.font", (FONT, 10))

    def _tab(self, nb, title):
        """A tab whose contents scroll if they don't fit."""
        outer = ttk.Frame(nb, style="Moo.TFrame")
        nb.add(outer, text=title)
        canvas = tk.Canvas(outer, bg=SURFACE, highlightthickness=0, bd=0)
        bar = ttk.Scrollbar(outer, orient="vertical", command=canvas.yview)
        canvas.configure(yscrollcommand=bar.set)
        inner = tk.Frame(canvas, bg=SURFACE, padx=20, pady=8)
        win_id = canvas.create_window(0, 0, window=inner, anchor="nw")

        def fit(_e=None):
            canvas.configure(scrollregion=canvas.bbox("all"))
            needs = inner.winfo_reqheight() > canvas.winfo_height()
            if needs and not bar.winfo_ismapped():
                bar.pack(side="right", fill="y")
            elif not needs and bar.winfo_ismapped():
                bar.pack_forget()

        inner.bind("<Configure>", fit)
        canvas.bind("<Configure>", lambda e: (canvas.itemconfigure(win_id, width=e.width), fit()))
        canvas.pack(side="left", fill="both", expand=True)
        self._canvases = getattr(self, "_canvases", {})
        self._canvases[str(outer)] = canvas
        self._nb = nb
        return inner

    def _wheel(self, event):
        try:
            canvas = self._canvases.get(self._nb.select())
            if canvas and canvas.winfo_exists():
                top, bottom = canvas.yview()
                if top > 0 or bottom < 1:
                    canvas.yview_scroll(int(-event.delta / 120) * 3, "units")
        except Exception:
            pass

    def _button(self, parent, text, cmd, primary=False):
        b = tk.Label(parent, text=text, bg=accent(self.s) if primary else SURFACE_HI, fg=BG if primary else TEXT,
                     font=(FONT, 10, "bold" if primary else "normal"), padx=18, pady=7, cursor="hand2")
        b.bind("<Button-1>", lambda e: cmd())
        return b

    def _label(self, parent, text, note=""):
        tk.Label(parent, text=text, bg=SURFACE, fg=TEXT, font=(FONT, 10), anchor="w").pack(fill="x", pady=(12, 3))
        if note:
            tk.Label(parent, text=note, bg=SURFACE, fg=FAINT, font=(FONT, 8), anchor="w", justify="left",
                     wraplength=460).pack(fill="x", pady=(0, 3))

    def _entry(self, parent, key, label, value, note="", show=None):
        self._label(parent, label, note)
        e = tk.Entry(parent, bg=SURFACE_HI, fg=TEXT, relief="flat", insertbackground=TEXT, font=(FONT, 10),
                     show=show, highlightthickness=0, bd=0)
        e.insert(0, value)
        e.pack(fill="x", ipady=6)
        self.vars[key] = e
        return e

    def _combo(self, parent, key, label, choices, current, note=""):
        """choices: list of (value, label)."""
        self._label(parent, label, note)
        labels = [c[1] for c in choices]
        idx = next((i for i, c in enumerate(choices) if c[0] == current), 0)
        var = tk.StringVar(value=labels[idx] if labels else "")
        cb = ttk.Combobox(parent, values=labels, textvariable=var, state="readonly", style="Moo.TCombobox",
                          font=(FONT, 10))
        cb.pack(fill="x", ipady=3)
        self.vars[key] = (var, choices)
        return cb

    def _slider(self, parent, key, label, lo, hi, value, step, left, right):
        self._label(parent, label)
        row = tk.Frame(parent, bg=SURFACE)
        row.pack(fill="x")
        tk.Label(row, text=left, bg=SURFACE, fg=FAINT, font=(FONT, 8), width=12, anchor="w").pack(side="left")
        var = tk.DoubleVar(value=value)
        sc = tk.Scale(row, from_=lo, to=hi, resolution=step, orient="horizontal", variable=var, showvalue=False,
                      bg=SURFACE, troughcolor=SURFACE_HI, highlightthickness=0, bd=0, sliderrelief="flat",
                      activebackground=accent(self.s), fg=TEXT, length=260)
        sc.pack(side="left", fill="x", expand=True)
        tk.Label(row, text=right, bg=SURFACE, fg=FAINT, font=(FONT, 8), width=12, anchor="e").pack(side="left")
        self.vars[key] = var
        return var

    def _check(self, parent, key, text, value):
        var = tk.BooleanVar(value=bool(value))
        tk.Checkbutton(parent, text=text, variable=var, bg=SURFACE, fg=TEXT, selectcolor=SURFACE_HI,
                       activebackground=SURFACE, activeforeground=TEXT, font=(FONT, 10), anchor="w",
                       highlightthickness=0, bd=0).pack(fill="x", pady=(10, 0))
        self.vars[key] = var
        return var

    # ---------- tabs ----------
    def _build_voice(self):
        t, s = self.tab_voice, self.s
        self._label(t, "Voice", "Male and female, British first. Kokoro runs on this PC (first use downloads "
                    "120 MB, then it's free). ElevenLabs needs a key (Accounts tab) and sounds the most real.")
        row = tk.Frame(t, bg=SURFACE)
        row.pack(fill="x")
        self.voice_var = tk.StringVar(value="Loading voices…")
        self.voice_combo = ttk.Combobox(row, textvariable=self.voice_var, state="readonly", style="Moo.TCombobox",
                                        font=(FONT, 10), height=24)
        self.voice_combo.pack(side="left", fill="x", expand=True, ipady=3)
        self._button(row, "▶  Hear it", self.preview).pack(side="left", padx=(8, 0))
        self.voice_filter = tk.StringVar(value=s.get("voice_filter", "British male"))
        frow = tk.Frame(t, bg=SURFACE)
        frow.pack(fill="x", pady=(6, 0))
        for opt in ("British male", "British female", "All"):
            tk.Radiobutton(frow, text=opt, value=opt, variable=self.voice_filter, bg=SURFACE, fg=TEXT,
                           selectcolor=SURFACE_HI, activebackground=SURFACE, activeforeground=TEXT, font=(FONT, 9),
                           highlightthickness=0, bd=0, command=self._refilter).pack(side="left", padx=(0, 14))
        threading.Thread(target=self._load_voices, daemon=True).start()

        self._slider(t, "speech_rate", "Pace", -30, 30, s.get("speech_rate", 0), 1, "Slower", "Quicker")
        self._slider(t, "speech_pitch", "Pitch", -20, 20, s.get("speech_pitch", 0), 1, "Lower", "Higher")
        self._slider(t, "speech_volume", "Volume", 40, 150, s.get("speech_volume", 100), 5, "Quieter", "Louder")
        self._check(t, "clear_speech", "Clearer speech (a touch slower, evened-out volume)", s.get("clear_speech", True))
        self._check(t, "speak_aloud", "Speak replies out loud (untick for text only)", s.get("speak_aloud", True))

    def _load_voices(self):
        from .voice import list_voices
        try:
            voices = list_voices(self.s)
        except Exception:
            voices = [{"engine": "natural", "id": self.s.get("natural_voice", "en-GB-SoniaNeural"),
                       "label": "Sonia · British"}]
        self.ui.later(self._show_voices, voices)

    def _refilter(self):
        self.s["voice_filter"] = self.voice_filter.get()
        if getattr(self, "all_voices", None):
            self._show_voices(self.all_voices)

    def _show_voices(self, voices):
        if not self.win.winfo_exists():
            return
        self.all_voices = voices
        f = self.voice_filter.get()
        if f != "All":
            accent, gender = f.split()
            keep = [v for v in voices if v.get("accent") == accent and v.get("gender") == gender]
            voices = keep or voices
        self.voices = voices
        self.voice_combo.configure(values=[v["label"] for v in voices])
        eng = self.s.get("tts_engine", "natural")
        cur = {"natural": self.s.get("natural_voice"), "azure": self.s.get("azure_voice"),
               "kokoro": self.s.get("kokoro_voice"), "elevenlabs": self.s.get("elevenlabs_voice"), "piper": ""}.get(eng)
        match = next((v for v in voices if v["engine"] == eng and v["id"] == cur), voices[0] if voices else None)
        self.voice_combo.set("")
        if match:
            self.voice_var.set(match["label"])

    def _chosen_voice(self):
        return next((v for v in self.voices if v["label"] == self.voice_var.get()), None)

    def preview(self):
        v = self._chosen_voice()
        if not v:
            return
        trial = dict(self.s)
        self._apply_voice(trial, v)
        for k in ("speech_rate", "speech_pitch", "speech_volume"):
            trial[k] = int(self.vars[k].get())
        trial["clear_speech"] = self.vars["clear_speech"].get()
        self.app.preview_voice(trial)

    @staticmethod
    def _apply_voice(target, v):
        target["tts_engine"] = v["engine"]
        key = {"natural": "natural_voice", "azure": "azure_voice", "kokoro": "kokoro_voice",
               "elevenlabs": "elevenlabs_voice"}.get(v["engine"])
        if key:
            target[key] = v["id"]

    def _build_look(self):
        t, s = self.tab_look, self.s
        self._combo(t, "theme", "Colour theme", [(n, n) for n in THEMES], s.get("theme", "Ocean"),
                    note="Changes the orb, mic button and highlights.")
        self._entry(t, "header_title", "Name in the top corner", s.get("header_title", ""),
                    note="Shown on the PC and in the phone app. Leave empty to just show her name.")
        self._combo(t, "text_size", "Text size", [(n, n) for n in TEXT_SIZES], s.get("text_size", "Medium"))
        self._slider(t, "window_opacity", "Window see-through", 60, 100, s.get("window_opacity", 100), 5,
                     "See-through", "Solid")
        self._check(t, "always_on_top", "Keep Moo on top of other windows", s.get("always_on_top", False))
        self._entry(t, "assistant_name", "Assistant's name", s.get("assistant_name", "Moo"),
                    note="What she calls herself. The wake word stays the same.")

    def _build_persona(self):
        t, s = self.tab_persona, self.s
        self._entry(t, "user_name", "What she calls you", s.get("user_name", ""))
        self._label(t, "About you and your work", "A few lines so she understands your world, e.g. your role, "
                    "company, what it does, main customers.")
        about = tk.Text(t, height=4, bg=SURFACE_HI, fg=TEXT, relief="flat", insertbackground=TEXT, font=(FONT, 10),
                        wrap="word", highlightthickness=0, bd=0, padx=8, pady=6)
        about.insert("1.0", s.get("about_you", ""))
        about.pack(fill="x")
        self.vars["about_you"] = about
        self._combo(t, "humour", "Humour", HUMOUR_CHOICES, s.get("humour", "quirky"),
                    note="She always turns the jokes off for bad news, complaints or anything urgent.")
        self._combo(t, "reply_length", "Reply length", LENGTH_CHOICES, s.get("reply_length", "normal"))
        self._label(t, "Personality and style",
                    "Plain English, e.g. 'More dry humour. Football references now and then. "
                    "Never say \"certainly\".'")
        box = tk.Text(t, height=6, bg=SURFACE_HI, fg=TEXT, relief="flat", insertbackground=TEXT, font=(FONT, 10),
                      wrap="word", highlightthickness=0, bd=0, padx=8, pady=6)
        box.insert("1.0", s.get("personality_notes", ""))
        box.pack(fill="x")
        self.vars["personality_notes"] = box
        self._entry(t, "banned_words", "Words she must never say", s.get("banned_words", "noted"),
                    note="Comma separated. You can also just tell her: \"stop saying sorted\".")
        self._entry(t, "favourite_team", "Football team you support", s.get("favourite_team", ""),
                    note="Results, fixtures, the table and match-day alerts. Or just tell her who you support.")
        self._entry(t, "home_location", "Home postcode or town", s.get("home_location", ""),
                    note="For weather and drive times, e.g. a postcode or town.")
        self._label(t, "Email sign-off", "Added to emails she drafts for you.")
        sig = tk.Text(t, height=3, bg=SURFACE_HI, fg=TEXT, relief="flat", insertbackground=TEXT, font=(FONT, 10),
                      wrap="word", highlightthickness=0, bd=0, padx=8, pady=6)
        sig.insert("1.0", s.get("email_signoff", ""))
        sig.pack(fill="x")
        self.vars["email_signoff"] = sig
        self._entry(t, "briefing_time", "Morning briefing time (weekdays)", s.get("briefing_time", "08:00"),
                    note="24-hour, e.g. 07:45. Leave blank to switch the briefing off.")

    def _build_listening(self):
        t, s = self.tab_listen, self.s
        self._check(t, "wake_word_enabled", "Listen for the wake word", s.get("wake_word_enabled", True))
        self._combo(t, "wake_engine", "Wake word", [("moo", f"Hey {s.get('assistant_name', 'Moo') or 'Moo'} (her name)"),
                                                    ("jarvis", "Hey Jarvis (older, lighter on the PC)")],
                    s.get("wake_engine", "moo"))
        self._slider(t, "wake_word_threshold", "Wake word sensitivity", 0.3, 0.85,
                     s.get("wake_word_threshold", 0.5), 0.05, "Hears it easily", "Fewer false starts")
        self._slider(t, "listen_pause", "How long a pause means you've finished", 0.6, 3.0,
                     s.get("listen_pause", 1.2), 0.1, "Quick", "Plenty of time")
        self._check(t, "barge_in", "Stop talking when I start speaking", s.get("barge_in", True))
        self._check(t, "follow_up", "Keep listening after she answers, so I can carry on without the wake word",
                    s.get("follow_up", True))
        self._slider(t, "follow_up_seconds", "How long she waits for a follow-up", 3, 12,
                     s.get("follow_up_seconds", 6), 1, "Short", "Long")
        self._combo(t, "barge_sensitivity", "How easily I can interrupt her",
                    [("low", "Low (use if she stops for no reason)"), ("medium", "Medium"),
                     ("high", "High (use with a headset)")], s.get("barge_sensitivity", "medium"))
        self._entry(t, "hotkey", "Talk shortcut", s.get("hotkey", "ctrl+alt+m"),
                    note="e.g. ctrl+alt+m, ctrl+shift+space, f9")
        self._entry(t, "vocabulary", "Words she should recognise", s.get("vocabulary", ""),
                    note="Names and trade terms you say often, separated by commas.")
        self._combo(t, "whisper_model", "Speech recognition", WHISPER_CHOICES, s.get("whisper_model", "small.en"),
                    note="A change downloads the new model in the background the first time.")

    def _build_proactive(self):
        t, s = self.tab_pro, self.s
        tk.Label(t, text="She keeps an eye on things and speaks up when it matters.", bg=SURFACE, fg=MUTED,
                 font=(FONT, 9), anchor="w").pack(fill="x", pady=(10, 0))
        self._check(t, "proactive", "Be proactive (master switch)", s.get("proactive", True))
        self._check(t, "email_alerts", "Tell me about important new emails", s.get("email_alerts", True))
        self._slider(t, "email_check_min", "Check Gmail every (Outlook is instant)", 1, 15,
                     s.get("email_check_min", 2), 1, "1 min", "15 min")
        self._entry(t, "vip_senders", "Always important (names or emails)", s.get("vip_senders", ""),
                    note="Comma separated, e.g. your MD, key customers. Their emails always get flagged.")
        self._check(t, "meeting_alerts", "Heads-up before meetings (with drive time)", s.get("meeting_alerts", True))
        self._slider(t, "meeting_lead_min", "How long before", 5, 60, s.get("meeting_lead_min", 15), 5, "5 min", "1 hour")
        self._check(t, "waiting_nudge", "Afternoon nudge: who's waiting on a reply", s.get("waiting_nudge", True))
        self._entry(t, "waiting_time", "Nudge time (weekdays)", s.get("waiting_time", "16:00"))
        self._check(t, "promise_tracker", "Track promises I make in emails, and chase quiet quotes",
                    s.get("promise_tracker", True))
        self._slider(t, "quote_followup_days", "Chase a quote after", 3, 21, s.get("quote_followup_days", 7), 1,
                     "3 days", "3 weeks")
        self._check(t, "clash_alerts", "Morning diary check for clashes", s.get("clash_alerts", True))
        self._check(t, "vh3_alerts", "Morning VH3 check: tell me about new critical BigChange issues",
                    s.get("vh3_alerts", True))
        self._check(t, "learn_memory", "Offer to remember things she notices about me", s.get("learn_memory", True))
        self._check(t, "football_alerts", "Match-day and full-time football alerts", s.get("football_alerts", True))
        self._check(t, "second_brain", "Keep my second brain up to date (Documents > Moo Brain)",
                    s.get("second_brain", True))
        self._check(t, "welcome_back", "Welcome me back when I return to the PC, with what I missed",
                    s.get("welcome_back", True))
        self._slider(t, "away_minutes", "Counts as away after (minutes)", 5, 60, s.get("away_minutes", 20), 5,
                     "5 min", "An hour")
        self._check(t, "suggest_actions", "Offer to draft replies and quote chases (only with my OK)",
                    s.get("suggest_actions", True))
        self._check(t, "log_decisions", "Keep a log of decisions I make, for later", s.get("log_decisions", True))
        self._check(t, "wrap_up", "End-of-day wrap-up", s.get("wrap_up", True))
        self._entry(t, "wrap_time", "Wrap-up time (weekdays)", s.get("wrap_time", "17:30"))
        self._label(t, "Quiet hours", "No alerts between these times. Reminders you set still come through.")
        row = tk.Frame(t, bg=SURFACE)
        row.pack(fill="x")
        for key, default in (("quiet_from", "19:00"), ("quiet_to", "07:00")):
            e = tk.Entry(row, bg=SURFACE_HI, fg=TEXT, relief="flat", insertbackground=TEXT, font=(FONT, 10),
                         width=8, highlightthickness=0, bd=0)
            e.insert(0, s.get(key, default))
            e.pack(side="left", ipady=6, padx=(0, 6))
            self.vars[key] = e
            if key == "quiet_from":
                tk.Label(row, text="to", bg=SURFACE, fg=MUTED, font=(FONT, 10)).pack(side="left", padx=(0, 6))
        self._check(t, "quiet_weekends", "Quiet at weekends", s.get("quiet_weekends", True))
        self._check(t, "alerts_spoken", "Say alerts out loud on the PC", s.get("alerts_spoken", True))
        self._check(t, "alerts_to_phone", "Send alerts to my phone", s.get("alerts_to_phone", True))
        tk.Label(t, text='Say "quiet for an hour" any time to pause her.', bg=SURFACE, fg=FAINT, font=(FONT, 8),
                 anchor="w").pack(fill="x", pady=(8, 0))

    def _build_accounts(self):
        t, s = self.tab_accounts, self.s
        self._entry(t, "api_key", "Claude API key", "", show="•",
                    note="Saved. Paste a new one to replace it." if get_api_key()
                    else "Needed. From console.anthropic.com, starts sk-ant-")
        self._combo(t, "model", "Brain", MODEL_CHOICES, s.get("model", "claude-haiku-5-5"))
        self._combo(t, "plan_model", "Brain for big jobs", PLAN_MODEL_CHOICES, s.get("plan_model", ""),
                    note="Used once she starts a multi-step plan (inbox sweep, weekly summary).")
        self._entry(t, "monthly_budget_usd", "Monthly spending cap (US$)", str(s.get("monthly_budget_usd", 10)))
        tk.Frame(t, bg=FAINT, height=1).pack(fill="x", pady=(18, 0))
        self._combo(t, "outlook_mode", "Outlook connection",
                    [("desktop", "Classic Outlook app on this PC (no IT needed)"),
                     ("online", "Microsoft 365 online (needs IT company setup)")],
                    s.get("outlook_mode", "desktop"),
                    note="Classic Outlook: she works through the Outlook app you're signed into. "
                         "Online: only if your IT company has registered her (IDs below).")
        self._check(t, "open_drafts", "Open drafts on screen after she saves them", s.get("open_drafts", True))
        self._entry(t, "ms_client_id", "Online only: application (client) ID", s.get("ms_client_id", ""))
        self._entry(t, "ms_tenant_id", "Online only: directory (tenant) ID", s.get("ms_tenant_id", ""))
        self._check(t, "outlook_drafts", "Online only: let her save drafts (needs Mail.ReadWrite)",
                    s.get("outlook_drafts", True))
        self._check(t, "web_search", "Allow web search (about 1p per search)", s.get("web_search", True))
        row = tk.Frame(t, bg=SURFACE)
        row.pack(fill="x", pady=(8, 0))
        self._button(row, "Test Outlook", lambda: (self.save(close=False),
                                                      self.app.text_request("sign in to outlook"))).pack(side="left")
        tk.Frame(t, bg=FAINT, height=1).pack(fill="x", pady=(18, 0))
        vh3 = self.app.brain.vh3
        self._entry(t, "vh3_url", "VH3 connection address", s.get("vh3_url", ""),
                    note="claude.ai > Settings > Connectors > Vh3: copy its URL. Status: " + vh3.status())
        self._check(t, "vh3_allow_actions", "Let her ask Connie too (she'll only ask questions, never send)",
                    s.get("vh3_allow_actions", False))
        vrow = tk.Frame(t, bg=SURFACE)
        vrow.pack(fill="x", pady=(8, 0))
        self.vh3_result = tk.Label(vrow, text="", bg=SURFACE, fg=MUTED, font=(FONT, 9))

        def connect_vh3():
            self.s["vh3_url"] = self.vars["vh3_url"].get().strip()
            save_settings(self.s)
            self.vh3_result.configure(text="Opening VH3 sign-in in your browser…", fg=MUTED)

            def run():
                msg = vh3.connect()
                self.ui.later(lambda: self.vh3_result.winfo_exists() and self.vh3_result.configure(
                    text=msg[:80], fg=TEXT if msg.startswith("Connected") else "#FF8A80"))
                self.ui.add_message("sys", msg)
            threading.Thread(target=run, daemon=True).start()
        self._button(vrow, "Connect VH3", connect_vh3).pack(side="left")
        self.vh3_result.pack(side="left", padx=10)
        tk.Frame(t, bg=FAINT, height=1).pack(fill="x", pady=(18, 0))
        self._entry(t, "gmail_address", "Gmail address", s.get("gmail_address", ""))
        self._entry(t, "gmail_app_password", "Gmail app password", "", show="•",
                    note=("Saved. Paste a new one to replace it, or type 'remove'." if get_secret("gmail_app_password")
                          else "16 letters from myaccount.google.com/apppasswords (see README)."))
        self._entry(t, "gcal_ics_url", "Google Calendar secret address (optional)", "", show="•",
                    note=("Saved. Paste a new one to replace it, or type 'remove'." if get_secret("gcal_ics_url")
                          else "Google Calendar > Settings > your calendar > 'Secret address in iCal format'."))
        grow = tk.Frame(t, bg=SURFACE)
        grow.pack(fill="x", pady=(8, 0))
        self._button(grow, "Test Gmail", self._test_gmail).pack(side="left")
        self.gmail_result = tk.Label(grow, text="", bg=SURFACE, fg=MUTED, font=(FONT, 9))
        self.gmail_result.pack(side="left", padx=10)
        tk.Frame(t, bg=FAINT, height=1).pack(fill="x", pady=(18, 0))
        self._entry(t, "elevenlabs_key", "ElevenLabs API key (optional)", "", show="•",
                    note=("Saved. Paste a new one to replace it, or type 'remove'." if get_secret("elevenlabs_key")
                          else "elevenlabs.io > Profile > API keys. Unlocks the most lifelike voices."))
        tk.Frame(t, bg=FAINT, height=1).pack(fill="x", pady=(18, 0))
        self._entry(t, "groq_key", "Cloud hearing: Groq API key (free)", "", show="•",
                    note=("Saved: she hears you in the cloud (faster, fewer mistakes). Type 'remove' to stop."
                          if get_secret("groq_key") else
                          "console.groq.com > API Keys > Create. Much faster, more accurate hearing on any PC."))
        tk.Frame(t, bg=FAINT, height=1).pack(fill="x", pady=(18, 0))
        self._entry(t, "tesla_token", "Tesla access token (Teslemetry)", "", show="•",
                    note=("Saved. Paste a new one to replace it, or type 'remove'." if get_secret("tesla_token")
                          else "teslemetry.com > sign in with Tesla > Console > Access tokens. See the README."))
        self._entry(t, "tesla_api_base", "Tesla API address", s.get("tesla_api_base", "https://api.teslemetry.com"),
                    note="Leave as it is for Teslemetry.")
        tk.Frame(t, bg=FAINT, height=1).pack(fill="x", pady=(18, 0))
        self._entry(t, "ha_url", "Smart home: Home Assistant address", s.get("ha_url", ""),
                    note="e.g. http://homeassistant.local:8123 (or your Nabu Casa address to work away from home).")
        self._entry(t, "ha_token", "Home Assistant long-lived token", "", show="•",
                    note=("Saved. Paste a new one to replace it, or type 'remove'." if get_secret("ha_token")
                          else "Home Assistant > your profile > Security > Long-lived access tokens > Create."))
        self._entry(t, "azure_key", "Azure Speech key (optional)", "", show="•",
                    note=("Saved. Paste a new one to replace it, or type 'remove'." if get_azure_key()
                          else "Free tier unlocks more British voices. See the README for setup."))
        self._entry(t, "azure_region", "Azure region", s.get("azure_region", "uksouth"))

    # ---------- phone app ----------
    def _build_app(self):
        t, s = self.tab_app, self.s
        tk.Label(t, text="Moo as an app on your phone: tap to talk, she answers in her voice. One-off setup, "
                         "about 5 minutes, using a free Supabase project.", bg=SURFACE, fg=MUTED, font=(FONT, 9),
                 anchor="w", justify="left", wraplength=760).pack(fill="x", pady=(10, 4))
        steps = ("1. Make a free account at supabase.com and create a project called Moo (region: London).\n"
                 "2. Click 'Copy setup script' below. In Supabase open SQL Editor, New query, paste, press Run.\n"
                 "3. In Supabase go to Project Settings, then API Keys (or Data API). Copy the Project URL and the "
                 "publishable key (or 'anon public' key) into the boxes below.\n"
                 "4. Click Connect, then scan the QR code with your phone's camera.\n"
                 "5. On your phone, open the browser menu (⋮) and choose 'Add to Home screen' or 'Install app'.")
        tk.Label(t, text=steps, bg=SURFACE, fg=TEXT, font=(FONT, 9), anchor="w", justify="left",
                 wraplength=760).pack(fill="x", pady=(2, 8))
        row = tk.Frame(t, bg=SURFACE)
        row.pack(fill="x", pady=(0, 6))
        self._button(row, "Copy setup script", self._copy_sql).pack(side="left")
        self._entry(t, "mobile_url", "Project URL", s.get("mobile_url", ""), note="Looks like https://abcdefg.supabase.co")
        self._entry(t, "mobile_key", "Publishable (anon) key", s.get("mobile_key", ""),
                    note="Starts sb_publishable_ or eyJ. This one is safe to share with the app.")
        self._check(t, "mobile_voice", "Speak replies in the app (her voice)", s.get("mobile_voice", True))
        row = tk.Frame(t, bg=SURFACE)
        row.pack(fill="x", pady=(10, 4))
        self._button(row, "Connect", self._connect_app, primary=True).pack(side="left")
        self._button(row, "Show QR code", self._show_qr).pack(side="left", padx=8)
        mob = getattr(self.app, "mobile", None)
        self.app_status = tk.Label(t, text=f"Status: {mob.status if mob else 'off'}", bg=SURFACE, fg=FAINT,
                                   font=(FONT, 9), anchor="w")
        self.app_status.pack(fill="x", pady=(6, 0))

    def _copy_sql(self):
        from pathlib import Path
        sql = (Path(__file__).resolve().parent.parent / "phone_app" / "setup.sql").read_text(encoding="utf-8")
        self.win.clipboard_clear()
        self.win.clipboard_append(sql)
        messagebox.showinfo("Moo", "Setup script copied. In Supabase: SQL Editor, New query, paste (Ctrl+V), Run.",
                            parent=self.win)

    def _connect_app(self):
        for key in ("mobile_url", "mobile_key"):
            self.s[key] = self.vars[key].get().strip()
        self.s["mobile_voice"] = bool(self.vars["mobile_voice"].get())
        save_settings(self.s)
        self.app_status.configure(text="Status: connecting…")

        def run():
            out = self.app.mobile.connect()
            def done():
                self.app_status.configure(text=f"Status: {self.app.mobile.status}")
                messagebox.showinfo("Moo", out, parent=self.win)
                if out.startswith("Connected"):
                    self._show_qr()
            self.win.after(0, done)
        threading.Thread(target=run, daemon=True).start()

    def _show_qr(self):
        mob = self.app.mobile
        if not (self.s.get("mobile_url") and self.s.get("mobile_key")):
            messagebox.showinfo("Moo", "Connect first.", parent=self.win)
            return
        from PIL import ImageTk
        top = tk.Toplevel(self.win)
        top.title("Scan with your phone")
        top.configure(bg=BG)
        tk.Label(top, text="Point your phone's camera at this and tap the link", bg=BG, fg=TEXT,
                 font=(FONT, 11, "bold")).pack(padx=24, pady=(20, 10))
        try:
            img = ImageTk.PhotoImage(mob.qr_image(320))
            lab = tk.Label(top, image=img, bg=BG)
            lab.image = img
        except Exception:
            lab = tk.Label(top, text="(QR code unavailable: use 'Copy link instead' below)", bg=BG, fg=MUTED,
                           font=(FONT, 10))
        lab.pack(padx=24)
        tk.Label(top, text="Keep this private: the code lets that phone talk to Moo.", bg=BG, fg=FAINT,
                 font=(FONT, 9)).pack(pady=(10, 4))

        def copy():
            top.clipboard_clear()
            top.clipboard_append(mob.pair_link())
            messagebox.showinfo("Moo", "Link copied. Send it to yourself and open it on the phone.", parent=top)
        b = self._button(top, "Copy link instead", copy)
        b.configure(bg=SURFACE_HI)
        b.pack(pady=(4, 20))

    def _build_phone(self):
        t, s, phone = self.tab_phone, self.s, self.app.phone
        tk.Label(t, text="Message or voice-note her from your phone with Telegram. Works anywhere, as long as "
                         "this PC is on and she's open.", bg=SURFACE, fg=MUTED, font=(FONT, 9), justify="left",
                 wraplength=460, anchor="w").pack(fill="x", pady=(10, 0))
        self._check(t, "phone_enabled", "Turn on the phone link", s.get("phone_enabled", False))
        self._entry(t, "telegram_token", "Telegram bot token", "", show="•",
                    note=("Saved. Paste a new one to replace it." if get_secret("telegram_token")
                          else "From @BotFather in Telegram (see README: 2 minutes)."))
        self._label(t, "Status")
        paired = phone.paired_id()
        status = phone.status if phone.status != "off" else "off (tick the box, add the token, Save)"
        tk.Label(t, text=status + (" · paired with your phone" if paired else ""), bg=SURFACE, fg=TEXT,
                 font=(FONT, 10), anchor="w").pack(fill="x")
        if not paired:
            self._label(t, "Pairing code", "After saving, open your bot in Telegram and send it this code. "
                                           "Only the phone that sends it can use her.")
            tk.Label(t, text=phone.pairing_code(), bg=SURFACE_HI, fg=TEXT, font=(FONT, 20, "bold"), padx=16,
                     pady=6).pack(anchor="w")
        self._check(t, "phone_voice_replies", "Reply to voice notes with a voice note", s.get("phone_voice_replies", True))
        self._check(t, "phone_voice_always", "Send a voice note with every reply (even to typed messages)",
                    s.get("phone_voice_always", False))
        self._check(t, "phone_reminders", "Send reminders to my phone", s.get("phone_reminders", True))
        self._check(t, "phone_briefing", "Send the morning briefing to my phone", s.get("phone_briefing", True))
        if paired:
            row = tk.Frame(t, bg=SURFACE)
            row.pack(fill="x", pady=(12, 0))
            self._button(row, "Send a test message", lambda: threading.Thread(
                target=phone.notify, args=("Test from your PC. All working.",), daemon=True).start()).pack(side="left")
            self._button(row, "Unpair phone", lambda: (phone.unpair(), self.win.destroy(),
                                                       self.ui.add_message("sys", "Phone unpaired."))).pack(side="left", padx=8)

    def _build_system(self):
        t, s = self.tab_system, self.s
        self._check(t, "autostart", "Start Moo when Windows starts", self.app.autostart_enabled())
        self._check(t, "auto_update", "Check for updates when she starts", s.get("auto_update", True))
        from . import __version__
        tk.Label(t, text=f"Version {__version__} · say \"check for updates\" any time", bg=SURFACE, fg=FAINT,
                 font=(FONT, 8), anchor="w").pack(fill="x", pady=(2, 0))
        self._label(t, "Folders to search if Windows Search can't help",
                    "One per line. Leave empty for Documents, Desktop, Downloads and OneDrive.")
        box = tk.Text(t, height=4, bg=SURFACE_HI, fg=TEXT, relief="flat", insertbackground=TEXT, font=(FONT, 10),
                      wrap="none", highlightthickness=0, bd=0, padx=8, pady=6)
        box.insert("1.0", "\n".join(s.get("search_folders") or []))
        box.pack(fill="x")
        self.vars["search_folders"] = box
        self._label(t, "Moo's data")
        row = tk.Frame(t, bg=SURFACE)
        row.pack(fill="x")
        self._button(row, "Open her folder", self.app.open_data_folder).pack(side="left")
        self._button(row, "Edit memories", self.app.edit_memories).pack(side="left", padx=8)
        self._button(row, "Clear conversation", self._clear).pack(side="left")

    # ---------- skills ----------
    def _build_skills(self):
        t = self.tab_skills
        tk.Label(t, text="Jobs you've taught her. Say the name (or something like it) and she follows the steps.",
                 bg=SURFACE, fg=MUTED, font=(FONT, 9), anchor="w", justify="left", wraplength=470).pack(fill="x", pady=(10, 6))
        self.skill_list = tk.Listbox(t, height=7, bg=SURFACE_HI, fg=TEXT, selectbackground=accent(self.s),
                                     selectforeground=BG, relief="flat", highlightthickness=0, bd=0,
                                     font=(FONT, 10), activestyle="none")
        self.skill_list.pack(fill="x")
        self.skill_list.bind("<<ListboxSelect>>", lambda e: self._skill_pick())
        self._label(t, "Name")
        self.skill_name = tk.Entry(t, bg=SURFACE_HI, fg=TEXT, relief="flat", insertbackground=TEXT, font=(FONT, 10),
                                   highlightthickness=0, bd=0)
        self.skill_name.pack(fill="x", ipady=6)
        self._label(t, "When to use it", "Phrases you might say, e.g. 'weekly numbers, how did last week go'.")
        self.skill_when = tk.Entry(t, bg=SURFACE_HI, fg=TEXT, relief="flat", insertbackground=TEXT, font=(FONT, 10),
                                   highlightthickness=0, bd=0)
        self.skill_when.pack(fill="x", ipady=6)
        self._label(t, "Steps", "Numbered, plain English. Mention where to look: email, calendar, VH3, files.")
        self.skill_steps = tk.Text(t, height=11, bg=SURFACE_HI, fg=TEXT, relief="flat", insertbackground=TEXT,
                                   font=(FONT, 10), wrap="word", highlightthickness=0, bd=0, padx=8, pady=6)
        self.skill_steps.pack(fill="x")
        row = tk.Frame(t, bg=SURFACE)
        row.pack(fill="x", pady=(10, 4))
        self._button(row, "Save skill", self._skill_save, primary=True).pack(side="left")
        self._button(row, "New", self._skill_new).pack(side="left", padx=8)
        self._button(row, "Delete", self._skill_delete).pack(side="left")
        tk.Label(t, text='Or just tell her: "save that as a skill called Friday cash check".', bg=SURFACE, fg=FAINT,
                 font=(FONT, 8), anchor="w").pack(fill="x", pady=(6, 0))
        self._skill_current = None
        self._skill_refresh()

    def _skills(self):
        return self.app.brain.skills if self.app.brain else None

    def _skill_refresh(self, select=None):
        sk = self._skills()
        self.skill_list.delete(0, "end")
        self._skill_items = sk.all() if sk else []
        for i, item in enumerate(self._skill_items):
            self.skill_list.insert("end", "  " + item["name"])
            if select and item["name"].lower() == select.lower():
                self.skill_list.selection_set(i)

    def _skill_fill(self, item):
        self._skill_current = item["name"] if item else None
        for w, v in ((self.skill_name, item.get("name", "") if item else ""),
                     (self.skill_when, item.get("when", "") if item else "")):
            w.delete(0, "end")
            w.insert(0, v)
        self.skill_steps.delete("1.0", "end")
        self.skill_steps.insert("1.0", item.get("steps", "") if item else "")

    def _skill_pick(self):
        sel = self.skill_list.curselection()
        if sel:
            self._skill_fill(self._skill_items[sel[0]])

    def _skill_new(self):
        self.skill_list.selection_clear(0, "end")
        self._skill_fill(None)
        self.skill_name.focus_set()

    def _skill_save(self):
        sk = self._skills()
        if not sk:
            return
        name = self.skill_name.get().strip()
        out = sk.save(name, self.skill_when.get(), self.skill_steps.get("1.0", "end"), old_name=self._skill_current)
        if out.startswith("Saved"):
            self._skill_current = name
            self._skill_refresh(select=name)
        messagebox.showinfo("Moo", out, parent=self.win)

    def _skill_delete(self):
        sk = self._skills()
        name = self._skill_current or self.skill_name.get().strip()
        if sk and name and messagebox.askyesno("Moo", f"Delete the skill '{name}'?", parent=self.win):
            sk.delete(name)
            self._skill_fill(None)
            self._skill_refresh()

    def _test_gmail(self):
        self._store_secrets()
        self.s["gmail_address"] = self.vars["gmail_address"].get().strip()
        self.gmail_result.configure(text="Testing…", fg=MUTED)

        def run():
            from .gmail import Gmail
            try:
                msg, ok = Gmail(self.s).test(), True
            except Exception as e:
                msg, ok = (str(e) if str(e) else "Add the address and app password first."), False
            self.ui.later(lambda: self.gmail_result.winfo_exists() and self.gmail_result.configure(
                text=msg[:70], fg=TEXT if ok else "#FF8A80"))
        threading.Thread(target=run, daemon=True).start()

    def _store_secrets(self):
        for key in ("gmail_app_password", "gcal_ics_url", "telegram_token", "elevenlabs_key", "tesla_token", "groq_key", "ha_token"):
            v = self.vars[key].get().strip()
            if v:
                set_secret(key, "" if v.lower() == "remove" else v)

    def _clear(self):
        if messagebox.askyesno("Moo", "Clear the conversation history? Her memories stay.", parent=self.win):
            self.app.clear_conversation()

    # ---------- save ----------
    def save(self, close=True):
        s = self.s
        key = self.vars["api_key"].get().strip()
        if key and not key.startswith("sk-ant-"):
            messagebox.showwarning("Moo", "That doesn't look like a Claude API key. They start with sk-ant-.",
                                   parent=self.win)
            return
        if key:
            set_api_key(key)
        self._store_secrets()
        az = self.vars["azure_key"].get().strip()
        if az:
            set_azure_key("" if az.lower() == "remove" else az)
        bt = self.vars["briefing_time"].get().strip()
        if bt:
            try:
                h, m = bt.replace(".", ":").split(":")
                bt = f"{int(h):02d}:{int(m):02d}"
                assert 0 <= int(h) < 24 and 0 <= int(m) < 60
            except Exception:
                messagebox.showwarning("Moo", "Briefing time should look like 08:00.", parent=self.win)
                return
        old_whisper, old_hotkey = s.get("whisper_model"), s.get("hotkey")
        if self.vars["whisper_model"][0].get() != next(c[1] for c in WHISPER_CHOICES if c[0] == old_whisper):
            s["whisper_model_chosen"] = True  # user picked one: respect it even on a slow PC

        for k, w in self.vars.items():
            if k in ("api_key", "azure_key", "autostart", "gmail_app_password", "gcal_ics_url", "telegram_token",
                     "elevenlabs_key", "tesla_token", "groq_key", "ha_token"):
                continue
            if isinstance(w, tuple):  # combo
                var, choices = w
                s[k] = next((c[0] for c in choices if c[1] == var.get()), s.get(k))
            elif isinstance(w, tk.Text):
                text = w.get("1.0", "end").strip()
                s[k] = [ln.strip() for ln in text.splitlines() if ln.strip()] if k == "search_folders" else text
            elif isinstance(w, tk.BooleanVar):
                s[k] = bool(w.get())
            elif isinstance(w, tk.DoubleVar):
                v = w.get()
                s[k] = round(v, 2) if k in ("wake_word_threshold", "listen_pause") else int(round(v))
            else:
                v = w.get().strip()
                if k == "monthly_budget_usd":
                    try:
                        v = float(v)
                    except ValueError:
                        continue
                s[k] = v
        s["briefing_time"] = bt
        s["assistant_name"] = s.get("assistant_name") or "Moo"
        v = self._chosen_voice()
        if v:
            self._apply_voice(s, v)
        save_settings(s)
        self.app.set_autostart(self.vars["autostart"].get())
        self.app.apply_settings(whisper_changed=s.get("whisper_model") != old_whisper,
                                hotkey_changed=s.get("hotkey") != old_hotkey)
        if close:
            self.win.destroy()
