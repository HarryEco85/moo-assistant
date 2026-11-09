"""Wires everything together: window, voice, wake word, brain, reminders, briefing."""
import json
import logging
import queue
import re
import threading
import time
from datetime import datetime
from logging.handlers import RotatingFileHandler

from .config import DATA_DIR, LOG_FILE, user_name, ensure_dirs, get_api_key, load_settings, read_json, write_json
from .reminders import Reminders, notify


def documents_dir():
    """Your Documents folder (follows OneDrive if Windows moved it there)."""
    from pathlib import Path
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                            r"Software\Microsoft\Windows\CurrentVersion\Explorer\User Shell Folders") as k:
            import os
            return Path(os.path.expandvars(winreg.QueryValueEx(k, "Personal")[0]))
    except Exception:
        return Path.home() / "Documents"

log = logging.getLogger("moo")
BRIEF_FILE = DATA_DIR / "last_briefing.json"
STOP_WORDS = re.compile(r"^(stop|shush|quiet|be quiet|that's enough|cancel|never ?mind)[.!]?$", re.I)
OUTLOOK_SIGNIN = re.compile(r"\b(sign|log) ?in(to)? (to )?outlook\b|\bconnect outlook\b", re.I)
SNOOZE = re.compile(r"\b(quiet|shh|no (alerts|interruptions)|do ?n[o']t disturb|leave me alone)\b.*?\b(for )?"
                    r"(an?|one|\d+|half an?)\s*(hour|hours|hr|minutes?|mins?)\b", re.I)
YES = re.compile(r"^(yes|yeah|yep|yup|go on|please( do)?|sure|ok(ay)?|do it)\b.{0,30}$", re.I)
NO = re.compile(r"^(no|nope|nah|don'?t|no thanks?|leave it|skip( it)?)\b.{0,30}$", re.I)
OFFICE = re.compile(r"\b(show|open)\b.{0,12}\b(office|workers|board)\b", re.I)
UPDATE_CHECK = re.compile(r"\b(check|look) (for )?(an? )?updates?\b|\bany updates?\b", re.I)
TAKE_NOTES = re.compile(r"\b(take|start( taking)?|keep) (some )?(notes|minutes)\b|\b(record|minute) (this|the) "
                        r"(meeting|call|conversation)\b", re.I)
STOP_NOTES = re.compile(r"\b(stop|finish|end|done|wrap up)( taking)?( the)? (notes|minutes|recording)\b|"
                        r"\bthat'?s the end of the (meeting|call)\b|\bstop notes\b", re.I)
APPROVE = re.compile(r"^(approve(d)?( it| that)?|go ahead( with it)?|yes,? (do|go ahead with) (it|that))[.!]?$", re.I)
REJECT = re.compile(r"^(reject( it| that)?|don'?t do (it|that)|no,? (leave|skip) (it|that))[.!]?$", re.I)
UPDATE_INSTALL = re.compile(r"\b(install|do|run|get|apply) (the |that )?update\b|\bupdate yourself\b", re.I)


class MooApp:
    def __init__(self):
        ensure_dirs()
        handler = RotatingFileHandler(LOG_FILE, maxBytes=1_000_000, backupCount=2, encoding="utf-8")
        logging.basicConfig(level=logging.INFO, handlers=[handler],
                            format="%(asctime)s %(levelname)s %(threadName)s: %(message)s")
        self.settings = load_settings()
        self.busy = threading.Lock()

        from .ui import MooWindow
        self.ui = MooWindow(self)
        self.reminders = Reminders(self._reminder_due)
        self.mouth = None
        self.ears = None
        self.brain = None
        from .phone import Phone
        from .proactive import Watcher
        from .approvals import Approvals
        self.phone = Phone(self)
        self.watcher = Watcher(self)
        self.approvals = Approvals()
        self.perf = {"date": "", "replies": 0, "secs": 0.0, "plans": 0}
        self._plans_seen = set()

    # ---------- startup ----------
    def start(self):
        if not get_api_key() and not self.ui.ask_api_key():
            self.ui.add_message("sys", "No API key yet. Open settings (⚙) to add one.")
        self._make_brain()
        self.reminders.start()
        threading.Thread(target=self._load_voice, daemon=True, name="voice-load").start()
        threading.Thread(target=self._briefing_loop, daemon=True, name="briefing").start()
        self._register_hotkey()
        self._footer()
        self.pending_update = None
        self.phone.start()
        self.watcher.start()
        if self.settings.get("auto_update", True):
            threading.Thread(target=self._check_updates, args=(True,), daemon=True, name="update-check").start()
        self.ui.run()

    # ---------- updates ----------
    def _check_updates(self, automatic=False):
        from . import updater
        if automatic:
            time.sleep(20)  # let her finish starting up first
        try:
            ver, notes = updater.check(self.settings.get("update_repo", "HarryEco85/moo-assistant"))
        except Exception as e:
            log.info("Update check failed: %s", e)
            if not automatic:
                self.say("I couldn't check for updates just now. Is the internet connected?")
            return
        if not ver:
            if not automatic:
                self.say("You're on the latest version.")
            return
        self.pending_update = ver
        self.ui.add_message("sys", f"Update available: version {ver}. {notes}".strip())
        self.ui.add_action(f"Install version {ver}", self.install_update)
        self.say(f"There's an update available. {notes} Say install the update, or click the button.", show=False)

    def install_update(self):
        if getattr(self, "_updating", False):
            return
        self._updating = True

        def run():
            from . import updater
            try:
                updater.install(self.settings.get("update_repo", "HarryEco85/moo-assistant"),
                                progress=lambda s: self.ui.set_state("thinking", s))
            except Exception as e:
                log.exception("Update failed")
                self._updating = False
                self.ui.set_state("idle")
                self.say(f"The update didn't work: {str(e)[:120]}. Nothing has changed.")
                return
            self.say("All updated. Restarting now.")
            updater.restart()
            self.ui.later(self.shutdown)
        threading.Thread(target=run, daemon=True, name="update").start()

    def _make_brain(self):
        from .brain import Brain
        self.brain = Brain(self.settings, self.reminders, status=lambda s: self.ui.set_state("thinking", s),
                           show_draft=lambda text, label: self.ui.add_draft(text, label),
                           connections=self.connections_report, phone=self.phone,
                           show_card=self.show_card)
        self.brain.watcher = self.watcher
        self.brain.approvals = self.approvals
        self.brain.on_approval = self.show_approval
        self.brain.on_plan = self._plan_changed

    def _load_voice(self):
        from .voice import Ears, Mouth
        self.mouth = Mouth(self.settings)
        self.ears = Ears(self.settings, on_wake=self._woken, on_barge=self._barged,
                         is_speaking=lambda: bool(self.mouth and self.mouth.speaking))
        self.ears.start()
        # wait for speech recognition to load (first run downloads the Whisper model)
        for _ in range(600):
            if self.ears.models_ready or not self.ears.available:
                break
            time.sleep(0.5)
        hotkey = self.settings.get("hotkey", "ctrl+alt+m").upper()
        self._update_hint()
        if self.ears.wake_error:
            self.ui.add_message("sys", f"Wake word couldn't start ({self.ears.wake_error[:120]}). "
                                       f"Use {hotkey} or the mic button.")
        self.ui.set_state("idle")
        hour = datetime.now().hour
        greeting = "Good morning" if hour < 12 else ("Good afternoon" if hour < 18 else "Good evening")
        self.say(f"{greeting} {user_name(self.settings)}. What would you like me to look at?",
                 show=True)

    def _register_hotkey(self):
        try:
            import keyboard
            if getattr(self, "_hotkey", None) is not None:
                keyboard.remove_hotkey(self._hotkey)
                self._hotkey = None
            self._hotkey = keyboard.add_hotkey(self.settings.get("hotkey", "ctrl+alt+m"), self.voice_request)
        except Exception as e:
            log.warning("Hotkey unavailable: %s", e)
            self.ui.add_message("sys", f"Couldn't set the shortcut '{self.settings.get('hotkey')}'. Try another in settings.")

    def _update_hint(self):
        if not self.ears:
            return
        hotkey = self.settings.get("hotkey", "ctrl+alt+m").upper()
        if self.ears.available and self.ears.whisper is not None:
            wake = (f'Say "{self.ears.wake_phrase}", ' if self.ears.wake_phrase and
                    self.settings.get("wake_word_enabled", True) else "")
            text = f"{wake}press {hotkey} or tap the mic"
        else:
            text = "No microphone found: type below"
        self.ui.later(lambda: self.ui.hint.configure(text=text))

    def apply_settings(self, whisper_changed=False, hotkey_changed=False):
        """Called by the settings window after Save: everything takes effect now."""
        if self.brain:
            self.brain.refresh()
            # a fresh start so she doesn't copy the old style or name from earlier replies
            self.brain.history.clear()
        self.ui.later(self.ui.apply_appearance)
        if hotkey_changed:
            self._register_hotkey()
        if whisper_changed and self.ears:
            self.ears.reload_whisper()
        if self.ears and self.settings.get("wake_engine", "moo") == "moo" and self.ears.tiny is None:
            threading.Thread(target=self.ears._load_tiny, daemon=True).start()
        self.phone.restart()
        self._update_hint()
        self._footer()
        self.ui.add_message("sys", "Settings saved.")

    def preview_voice(self, trial_settings):
        """Play a sample of a voice from the settings window without changing anything."""
        if not self.mouth:
            return

        def run():
            from . import voice
            from .voice import Mouth
            if trial_settings.get("tts_engine") == "kokoro" and voice._kokoro is None:
                self.ui.add_message("sys", "Getting the Kokoro voices ready (first time only: a 120 MB download, "
                                           "about a minute)…")
            self.mouth.stop()
            m = Mouth(trial_settings, piper=self.mouth.piper, load=False)
            m.say(f"Hello {user_name(self.settings)}, this is how I'd sound. "
                  "You've got two meetings this afternoon, and three emails that look like they need you.", force=True)
        threading.Thread(target=run, daemon=True, name="preview").start()

    # ---------- system helpers ----------
    @staticmethod
    def _startup_link():
        import os
        from pathlib import Path
        return Path(os.environ.get("APPDATA", "")) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup" / "Moo.lnk"

    def autostart_enabled(self) -> bool:
        try:
            return self._startup_link().exists()
        except Exception:
            return False

    def set_autostart(self, on: bool):
        import os
        import subprocess
        import sys
        from pathlib import Path
        if os.name != "nt" or on == self.autostart_enabled():
            return
        link = self._startup_link()
        try:
            if not on:
                link.unlink()
                return
            pyw = Path(sys.executable).with_name("pythonw.exe")
            script = Path(__file__).resolve().parent.parent / "run_moo.pyw"
            ps = (f"$s=(New-Object -ComObject WScript.Shell).CreateShortcut('{link}');"
                  f"$s.TargetPath='{pyw}';$s.Arguments='\"{script}\"';$s.WorkingDirectory='{script.parent}';$s.Save()")
            subprocess.run(["powershell", "-NoProfile", "-Command", ps], creationflags=0x08000000, timeout=20)
        except Exception as e:
            log.warning("Autostart change failed: %s", e)

    def open_data_folder(self):
        import os
        if os.name == "nt":
            os.startfile(str(DATA_DIR))

    def edit_memories(self):
        import subprocess
        from .memory import MEMORY_FILE
        if not MEMORY_FILE.exists():
            write_json(MEMORY_FILE, [])
        try:
            subprocess.Popen(["notepad.exe", str(MEMORY_FILE)])
        except Exception as e:
            log.warning("Couldn't open memories: %s", e)

    def clear_conversation(self):
        if self.brain:
            self.brain.history.clear()
        self.ui.add_message("sys", "Conversation cleared. She'll start fresh (memories kept).")

    # ---------- talking ----------
    def say(self, text, show=True):
        if show:
            self.ui.add_message("Moo", text)  # (label only; the window shows no names)
        if self.mouth and self.mouth.voice and self.settings.get("speak_aloud", True):
            self.mouth.say(text, on_start=lambda: self.ui.set_state("speaking"),
                           on_end=lambda: self.ui.set_state("idle"))
        else:
            self.ui.set_state("idle")

    def stop_speaking(self):
        if self.mouth:
            self.mouth.stop()
        self.ui.set_state("idle")

    def _woken(self, command=None):
        """The wake phrase. If you said the request in the same breath ("Hey Moo, what's on today?"),
        go straight to it."""
        if not command or len(command.split()) < 2:
            self.voice_request()
            return
        if self.mouth and self.mouth.speaking:
            self.mouth.stop()

        def run():
            if not self.busy.acquire(timeout=8):
                return
            try:
                self.ui.add_message("You", command)
                self._answer(command)
            finally:
                if not (self.mouth and self.mouth.speaking):
                    self.ui.set_state("idle")
                self.busy.release()
        threading.Thread(target=run, daemon=True, name="wake-command").start()

    def _barged(self, preroll):
        """You started talking over her: stop, and listen to what you're saying."""
        log.info("Interrupted by voice")
        self.voice_request(preroll)

    def voice_request(self, preroll=None):
        """Wake word, hotkey or mic button: interrupt Moo if she's talking, then listen."""
        if self.mouth and self.mouth.speaking:
            self.mouth.stop()
        threading.Thread(target=self._listen_then_answer, args=(preroll,), daemon=True, name="listen").start()

    def _listen_then_answer(self, preroll=None):
        # wait for an interrupted answer to finish stopping
        if not self.busy.acquire(timeout=8):
            return
        try:
            if not self.ears or not self.ears.available:
                self.ui.add_message("sys", "No microphone available. Type your question instead.")
                return
            self.ui.set_state("listening")
            try:
                text = self.ears.listen(preroll=preroll)
                self._heard = self.ears.last_transcribe_s
            except RuntimeError as e:
                self.ui.add_message("sys", str(e))
                return
            text = re.sub(r"^(hey )?(moo|jarvis)[,.!]?\s*", "", text, flags=re.I).strip()
            if not text:
                self.ui.set_state("idle")
                return
            self.ui.add_message("You", text)
            self._answer(text)
        finally:
            if not (self.mouth and self.mouth.speaking):
                self.ui.set_state("idle")
            self.busy.release()

    def text_request(self, text):
        def run():
            with self.busy:
                self.ui.add_message("You", text)
                self._answer(text)
        if self.mouth and self.mouth.speaking:
            self.mouth.stop()
        threading.Thread(target=run, daemon=True, name="text").start()

    def _answer(self, text):
        if STOP_WORDS.match(text.strip()):
            self.stop_speaking()
            return
        if self.ears and self.ears.notes_started and STOP_NOTES.search(text):
            self.finish_notes()
            return None
        if TAKE_NOTES.search(text) and not (self.ears and self.ears.notes_started):
            self.start_notes()
            return None
        if getattr(self, "pending_memory", None) and (YES.match(text.strip()) or NO.match(text.strip())):
            self.say(self.answer_memory(bool(YES.match(text.strip()))))
            return None
        if self.approvals.pending() and (APPROVE.match(text.strip()) or REJECT.match(text.strip())):
            item = next((a for a in reversed(self.approvals.pending()) if a["status"] == "pending"), None)
            if not item:
                self.say("That one's already under way.")
            elif APPROVE.match(text.strip()):
                self.approve(item["id"])
            else:
                self.reject(item["id"])
            return None
        if OFFICE.search(text):
            self.ui.later(self.ui.open_office)
            self.say("Here's the office. Everyone's at their desks.")
            return None
        m = SNOOZE.search(text)
        if m:
            amount, unit = m.group(4).lower(), m.group(5).lower()
            n = 1 if amount in ("a", "an", "one") else (0.5 if amount.startswith("half") else int(amount) if amount.isdigit() else 1)
            minutes = int(n * 60) if unit.startswith("h") else int(n)
            self.say(self.watcher.snooze(max(5, minutes)))
            return None
        if UPDATE_INSTALL.search(text) and getattr(self, "pending_update", None):
            self.install_update()
            return
        if UPDATE_CHECK.search(text) or UPDATE_INSTALL.search(text):
            self.ui.set_state("thinking", "Checking for updates")
            self._check_updates()
            return
        if OUTLOOK_SIGNIN.search(text):
            self.ui.set_state("thinking", "Opening Microsoft sign-in")
            self.say(self.brain.connect_outlook())
            return None
        self.ui.set_state("thinking")
        t0 = time.time()
        timing = {}
        speaking = self.mouth and self.mouth.voice and self.settings.get("speak_aloud", True)
        sentences = queue.Queue()
        speaker = None
        if speaking:
            def first_word():
                timing.setdefault("first", time.time() - t0)
                self.ui.set_state("speaking")
            speaker = threading.Thread(
                target=self.mouth.speak_iter, name="speak",
                args=(iter(sentences.get, None), first_word, lambda: self.ui.set_state("idle")))
            speaker.start()
        def first_text(sent):
            timing.setdefault("think", time.time() - t0)
            sentences.put(sent)
        try:
            reply = self.brain.ask(text, on_sentence=first_text if speaking else None)
        except Exception as e:
            log.exception("Brain failed")
            msg = str(e)
            if "authentication" in msg.lower() or "api key" in msg.lower() or "401" in msg:
                reply = "My API key isn't working. Please check it in settings."
            elif "credit" in msg.lower() or "billing" in msg.lower():
                reply = "My Claude account has run out of credit. You can top it up in the Claude Console."
            elif "connect" in msg.lower() or "timeout" in msg.lower():
                reply = "I can't reach my brain right now. Is the internet connected?"
            else:
                reply = f"Sorry, something went wrong: {msg[:160]}"
            if speaking:
                sentences.put(reply)
        timing["total"] = time.time() - t0
        sentences.put(None)
        self.ui.add_message("Moo", reply)
        if not speaking:
            self.ui.set_state("idle")
        threading.Thread(target=self._learn, args=(text, reply), daemon=True, name="learn").start()
        if getattr(self, "_heard", None) is not None:
            timing["heard"] = self._heard
            self._heard = None
        self.last_timing = timing
        self._count_reply(timing["total"])
        self._footer()
        return reply

    def _footer(self):
        try:
            spend = self.brain.usage.this_month() if self.brain else 0
            model = self.settings.get("model", "").replace("claude-", "").replace("-", " ").title()
            parts = [model, f"this month ${spend:.2f}"]
            t = getattr(self, "last_timing", {})
            if t:
                heard = f"heard {t['heard']:.1f}s · " if "heard" in t else ""
                first = (f"first word {t['first']:.1f}s" if "first" in t else "") + \
                    (f" (thinking {t['think']:.1f}s)" if "think" in t else "") if "first" in t else f"reply {t['total']:.1f}s"
                parts.append(heard + first)
            if self.ears and self.ears.whisper_device:
                parts.append(f"hearing on {self.ears.whisper_device}")
            self.ui.set_footer("  ·  ".join(parts))
        except Exception:
            pass

    # ---------- reminders and briefing ----------
    def _reminder_due(self, text):
        notify("Moo reminder", text)
        self.ui.add_message("Moo", f"Reminder: {text}")
        if self.settings.get("phone_reminders", True):
            threading.Thread(target=self.phone.notify, args=(f"Reminder: {text}",), daemon=True).start()
        threading.Thread(target=lambda: self.say(f"Just a reminder, {text}", show=False), daemon=True).start()

    def _briefing(self):
        with self.busy:
            self.ui.add_message("You", "Give me my morning briefing.")
            reply = self._answer("Give me my morning briefing.")
        if reply and self.settings.get("phone_briefing", True):
            self.phone.notify("Morning briefing\n\n" + reply)

    # ---------- learning memory ----------
    def _learn(self, user_text, reply, from_phone=False):
        """Notice a lasting fact or preference in the exchange and offer to remember it."""
        want_fact = self.settings.get("learn_memory", True) and not getattr(self, "pending_memory", None)
        want_decision = self.settings.get("log_decisions", True)
        if len(user_text) < 15 or not (want_fact or want_decision):
            return
        try:
            known = self.brain.memory.as_prompt()
            declined = "; ".join(self.watcher.state.get("declined_facts", [])[-30:]) or "none"
            reply_json = self.brain.quick(
                f"You help an assistant learn about {user_name(self.settings)}. From this exchange:\n"
                "1. fact: is there ONE lasting fact or preference about them worth remembering (how they like things "
                "done, people and roles in their work, recurring routines)? Not one-off tasks, not things already "
                "known, never passwords, money details, health or anything sensitive.\n"
                "2. decision: did THEY clearly decide something in this exchange (chose a supplier, a price, an "
                "approach, a date)? One line, what and why if given. Not things the assistant suggested that they "
                "didn't accept.\n"
                f"Already known:\n{known}\nDeclined before: {declined}\n"
                'Reply JSON only: {"fact": "<short, third person, e.g. Prefers calls before 10am>" or null, '
                '"decision": "<e.g. Going with Jewson for the Tilbury roof materials (cheaper delivery)>" or null}.',
                f"USER: {user_text}\nASSISTANT: {reply}")
            m = re.search(r"\{.*\}", reply_json or "", re.S)
            found = json.loads(m.group(0)) if m else {}
            fact = (found.get("fact") if want_fact else None) or ""
            decision = (found.get("decision") if want_decision else None) or ""
        except Exception:
            log.exception("Learning check failed")
            return
        if decision and len(decision) < 300:
            try:
                self.brain.journal.log_decision(decision, source="noticed")
            except Exception:
                log.exception("Decision log failed")
        fact = fact.strip().rstrip(".")
        if not fact or len(fact) > 160 or fact.lower() in known.lower():
            return
        self.pending_memory = fact
        if from_phone:
            self.phone.notify(f"Shall I remember: \"{fact}\"? Reply yes or no.")
        else:
            self.ui.add_choice(f"Shall I remember: \"{fact}\"?",
                               [("Remember", lambda: self.say(self.answer_memory(True))),
                                ("No thanks", lambda: self.ui.add_message("sys", self.answer_memory(False)))])

    def answer_memory(self, yes: bool) -> str:
        fact, self.pending_memory = getattr(self, "pending_memory", None), None
        if not fact:
            return "Nothing to remember."
        if yes:
            out = self.brain.memory.remember(fact)
            return "Noted." if out.startswith("Remembered") else out
        self.watcher.state.setdefault("declined_facts", []).append(fact)
        self.watcher._save()
        return "Fair enough, I'll forget I noticed."

    def show_card(self, kind, data):
        """Draw a visual card (diary or weather) in the window, and keep it for the phone if needed."""
        from . import cards
        from .themes import palette
        try:
            width = max(340, min(self.ui.chat_width - 56, 560))
            if kind == "calendar":
                img = cards.calendar_card(data["events"], data["start"], data["days"], palette(self.settings), width)
            elif kind == "weather":
                img = cards.weather_card(data, palette(self.settings), width)
            else:
                return
        except Exception:
            log.exception("Card failed")
            return
        self.ui.add_card(img)
        if getattr(self, "_phone_cards", None) is not None:
            self._phone_cards.append(cards.to_png(cards.calendar_card(
                data["events"], data["start"], data["days"], palette(self.settings), 480))
                if kind == "calendar" else cards.to_png(cards.weather_card(data, palette(self.settings), 480)))

    def connections_report(self) -> str:
        from .config import get_secret
        s, lines = self.settings, []
        if s.get("outlook_mode", "desktop") == "online":
            lines.append("Outlook: Microsoft 365 online " + ("(IDs entered)" if s.get("ms_client_id") else "(not set up: needs IT company IDs)"))
        else:
            try:
                lines.append("Outlook: " + self.brain.outlook.test())
            except Exception as e:
                lines.append(f"Outlook (classic app on this PC): not reachable ({str(e)[:80]})")
        gm = s.get("gmail_address") and get_secret("gmail_app_password")
        lines.append(f"Gmail: {'set up for ' + s['gmail_address'] if gm else 'not set up'}")
        lines.append(f"Google Calendar: {'set up' if get_secret('gcal_ics_url') else 'not set up'}")
        lines.append(f"VH3 (BigChange jobs, quotes, invoices, Connie): {self.brain.vh3.status()}")
        ph = self.phone
        if not s.get("phone_enabled"):
            lines.append("Phone link (Telegram): switched off")
        else:
            lines.append(f"Phone link (Telegram): {ph.status}; " + ("paired with the user's phone" if ph.paired_id()
                         else "NOT paired yet: send the pairing code from Settings > Phone to the bot"))
            if ph.paired_id():
                lines.append(f"  Reminders to phone: {'on' if s.get('phone_reminders', True) else 'off'}; "
                             f"briefing to phone: {'on' if s.get('phone_briefing', True) else 'off'}")
        lines.append(f"Web search: {'on' if s.get('web_search', True) else 'off'}")
        lines.append(f"Voice: {s.get('tts_engine', 'natural')} ({s.get('natural_voice') if s.get('tts_engine', 'natural') == 'natural' else s.get('azure_voice', '')})")
        if self.ears:
            lines.append(f"Hearing: {'on ' + (self.ears.whisper_device or 'loading') if self.ears.available else 'no microphone'}"
                         f"; wake word {'on' if s.get('wake_word_enabled', True) and self.ears.wake_phrase else 'off'}")
        return "\n".join(lines)

    def ask_from_phone(self, text, heard=False, images=None):
        """A message from Telegram: answer in text (no speaking on the PC)."""
        drafts = []
        self._phone_cards = []
        if not self.busy.acquire(timeout=90):
            return "I'm tied up with something on the PC. Try again in a moment.", drafts
        old_show = self.brain.show_draft
        if getattr(self, "pending_memory", None) and (YES.match(text.strip()) or NO.match(text.strip())):
            self.busy.release()
            self._phone_cards = None
            return self.answer_memory(bool(YES.match(text.strip()))), drafts
        try:
            self.brain.show_draft = lambda body, label: (drafts.append((label, body)), old_show(body, label))
            self.ui.add_message("You", f"(phone) {text}")
            self.ui.set_state("thinking", "Message from your phone")
            who = user_name(self.settings)
            if heard:  # van mode: they're likely driving, the answer will be played back as a voice note
                note = (f"[Voice note from {who}'s phone; they're probably driving. Answer in one to three short "
                        "spoken sentences, no lists, no reading out long details (offer to text them instead). "
                        "Don't open files on the PC.] ")
            else:
                note = (f"[Sent from {who}'s phone. {who} is away from the PC: reply in text for a phone screen, "
                        "don't open files on the PC, and keep it brief.] ")
            if images:
                note += "[They've attached a photo.] "
            try:
                reply = self.brain.ask(note + text, channel="phone", images=images or None)
                self._count_reply(None)
            except Exception as e:
                log.exception("Phone request failed")
                reply = f"Sorry, something went wrong: {str(e)[:160]}"
            self.ui.add_message("Moo", reply)
            threading.Thread(target=self._learn, args=(text, reply, True), daemon=True, name="learn").start()
            for png in self._phone_cards:
                drafts.append(("__image__", png))
            return reply, drafts
        finally:
            self.brain.show_draft = old_show
            self._phone_cards = None
            self.ui.set_state("idle")
            self.busy.release()
            self._footer()

    # ---------- meeting notes ----------
    def start_notes(self):
        if not (self.ears and self.ears.available and self.ears.whisper is not None):
            self.say("I can't take notes without the microphone and speech recognition running, sorry.")
            return
        self.ears.start_notes()
        self.ui.add_action("Stop notes", lambda: threading.Thread(target=self._notes_button, daemon=True).start())
        wake = self.ears.wake_phrase or "the mic button"
        self.say(f"Righto, I'm taking notes. When you're done, say {wake}, stop notes, or press the button.")

    def _notes_button(self):
        if not self.busy.acquire(timeout=30):
            return
        try:
            self.finish_notes()
        finally:
            self.busy.release()

    def finish_notes(self):
        if not (self.ears and self.ears.notes_started):
            return
        self.ui.set_state("thinking", "Writing up the notes")
        lines = self.ears.stop_notes()
        words = sum(len(t.split()) for _, t in lines)
        if words < 15:
            self.say("I didn't catch enough to write up, I'm afraid. Was the mic near you?")
            return
        who = user_name(self.settings)
        stamp = datetime.now()
        transcript = "\n".join(f"[{int(s // 60):02d}:{int(s % 60):02d}] {t}" for s, t in lines)
        try:
            notes = self.brain.quick(
                f"Turn this meeting or call transcript into tidy notes for {who}. Only their microphone was "
                "recorded, so the other side may be partial; don't invent anything that isn't there. Use these "
                "headings: Summary (2-3 sentences), Key points, Decisions, Actions (who, what, by when), and "
                "Open questions. Plain text, short lines starting with a dash. British English.", transcript[:60000])
        except Exception as e:
            log.exception("Notes write-up failed")
            notes = f"(Couldn't write up the notes: {str(e)[:120]})"
        path = None
        try:
            folder = documents_dir() / "Moo Notes"
            folder.mkdir(parents=True, exist_ok=True)
            path = folder / f"{stamp.strftime('%Y-%m-%d %H%M')} meeting notes.txt"
            path.write_text(f"Meeting notes, {stamp.strftime('%A %d %B %Y, %H:%M')}\n\n{notes}\n\n"
                            f"----- Transcript -----\n{transcript}\n", encoding="utf-8")
        except Exception:
            log.exception("Couldn't save the notes")
        self.ui.add_draft(notes, "Meeting notes")
        if path:
            self.ui.add_action("Open the notes", lambda p=str(path): __import__("os").startfile(p))
        try:
            reply = self.brain.ask(
                f"[Meeting notes, recorded on the PC. Tidy notes below.] Now: 1) log_decision for each decision "
                f"{who} made. 2) set_reminder for each action {who} owns that has a time; for important ones "
                "without a time, 9am the next working day. 3) If other people or companies were involved, "
                "request_approval to draft a follow-up email to them summarising decisions and actions (put the "
                "summary and who it's for in the instruction). 4) Then tell me in three or four spoken sentences "
                "what the meeting landed on, what you've set up, and that the full notes are saved in Documents, "
                f"Moo Notes.\n\n{notes}", channel="notes")
        except Exception as e:
            log.exception("Notes follow-up failed")
            reply = f"The notes are saved in Documents, Moo Notes, but I couldn't set up the follow-ups: {str(e)[:120]}"
        self.say(reply)

    # ---------- approvals ----------
    def show_approval(self, item, notify=True):
        """A new request for the user's OK: buttons in the chat, and on the phone."""
        aid = item["id"]
        self.ui.add_choice(f"Needs your OK: {item['title']}" + (f" ({item['detail']})" if item.get("detail") else ""),
                           [("Approve", lambda: self.approve(aid)), ("Reject", lambda: self.reject(aid))])
        if notify and self.settings.get("alerts_to_phone", True):
            threading.Thread(target=self.phone.send_approval, args=(item,), daemon=True).start()

    def approve(self, aid, from_phone=False):
        item = self.approvals.claim(aid)
        if not item:
            return False

        def run():
            if not self.busy.acquire(timeout=180):
                self.approvals.set(aid, "pending")
                return
            try:
                self.ui.add_message("sys", f"Approved: {item['title']}")
                self.ui.set_state("thinking", "On it")
                who = user_name(self.settings)
                try:
                    reply = self.brain.ask(f"[Approved by {who} from the approval queue. Do this now, then confirm in "
                                           f"one short sentence.] {item['instruction']}", channel="approval")
                    self.approvals.set(aid, "done", reply)
                except Exception as e:
                    log.exception("Approved job failed")
                    reply = f"Sorry, that didn't work: {str(e)[:150]}"
                    self.approvals.set(aid, "failed", reply)
                self.ui.add_message("Moo", reply)
                if from_phone:
                    self.phone.notify(reply)
                elif self.mouth and self.mouth.voice and self.settings.get("speak_aloud", True):
                    self.say(reply, show=False)
            finally:
                self.ui.set_state("idle")
                self.busy.release()
        threading.Thread(target=run, daemon=True, name="approved").start()
        return True

    def reject(self, aid, from_phone=False):
        item = self.approvals.set(aid, "rejected")
        if item:
            self.ui.add_message("sys", f"Rejected: {item['title']}")
        return bool(item)

    # ---------- plans and performance ----------
    def _plan_changed(self, plan):
        key = plan["started"]
        if plan["status"] == "working" and key not in self._plans_seen:
            self._plans_seen.add(key)
            steps = "  ".join(f"{i}. {st['text']}" for i, st in enumerate(plan["steps"], 1))
            self.ui.add_message("sys", f"Plan: {plan['goal']}.  {steps}")
        elif plan["status"] in ("done", "issues") and ("end", key) not in self._plans_seen:
            self._plans_seen.add(("end", key))
            self._count_plan()
            mark = "Checked ✓" if plan["status"] == "done" else "Checked: some issues"
            self.ui.add_message("sys", f"{mark}  {plan.get('check', '')}".strip())

    def _perf_today(self):
        today = datetime.now().strftime("%Y-%m-%d")
        if self.perf["date"] != today:
            self.perf = {"date": today, "replies": 0, "secs": 0.0, "timed": 0, "plans": 0}
        return self.perf

    def _count_reply(self, secs):
        p = self._perf_today()
        p["replies"] += 1
        if secs:
            p["secs"] += secs
            p["timed"] = p.get("timed", 0) + 1

    def _count_plan(self):
        self._perf_today()["plans"] += 1

    def _briefing_loop(self):
        while True:
            try:
                bt = (self.settings.get("briefing_time") or "").strip()
                now = datetime.now()
                last = read_json(BRIEF_FILE, {}).get("date")
                if bt and now.weekday() < 5 and now.strftime("%H:%M") >= bt and last != now.strftime("%Y-%m-%d"):
                    # only brief within 3 hours of the set time (e.g. PC switched on late)
                    h, m = map(int, bt.split(":"))
                    if (now.hour * 60 + now.minute) - (h * 60 + m) <= 180:
                        write_json(BRIEF_FILE, {"date": now.strftime("%Y-%m-%d")})
                        while self.mouth is None:
                            time.sleep(1)
                        threading.Thread(target=self._briefing, daemon=True, name="briefing-run").start()
            except Exception:
                log.exception("Briefing check failed")
            time.sleep(30)

    def shutdown(self):
        try:
            self.reminders.stop()
            self.phone.stop()
            self.watcher.stop()
            if self.mouth:
                self.mouth.stop()
            if self.ears and self.ears.stream:
                self.ears.stream.stop()
        finally:
            self.ui.root.destroy()


def main():
    MooApp().start()
