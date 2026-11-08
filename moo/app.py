"""Wires everything together: window, voice, wake word, brain, reminders, briefing."""
import logging
import queue
import re
import threading
import time
from datetime import datetime
from logging.handlers import RotatingFileHandler

from .config import DATA_DIR, LOG_FILE, user_name, ensure_dirs, get_api_key, load_settings, read_json, write_json
from .reminders import Reminders, notify

log = logging.getLogger("moo")
BRIEF_FILE = DATA_DIR / "last_briefing.json"
STOP_WORDS = re.compile(r"^(stop|shush|quiet|be quiet|that's enough|cancel|never ?mind)[.!]?$", re.I)
OUTLOOK_SIGNIN = re.compile(r"\b(sign|log) ?in(to)? (to )?outlook\b|\bconnect outlook\b", re.I)
UPDATE_CHECK = re.compile(r"\b(check|look) (for )?(an? )?updates?\b|\bany updates?\b", re.I)
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
        self.phone = Phone(self)

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

    def _load_voice(self):
        from .voice import Ears, Mouth
        self.mouth = Mouth(self.settings)
        self.ears = Ears(self.settings, on_wake=self._woken)
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
        self.ui.later(self.ui.apply_appearance)
        if hotkey_changed:
            self._register_hotkey()
        if whisper_changed and self.ears:
            self.ears.reload_whisper()
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

    def _woken(self):
        self.voice_request()

    def voice_request(self):
        """Wake word, hotkey or mic button: interrupt Moo if she's talking, then listen."""
        if self.mouth and self.mouth.speaking:
            self.mouth.stop()
        threading.Thread(target=self._listen_then_answer, daemon=True, name="listen").start()

    def _listen_then_answer(self):
        # wait briefly for an interrupted answer to finish stopping
        if not self.busy.acquire(timeout=2):
            return
        try:
            if not self.ears or not self.ears.available:
                self.ui.add_message("sys", "No microphone available. Type your question instead.")
                return
            self.ui.set_state("listening")
            try:
                text = self.ears.listen()
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
        if getattr(self, "_heard", None) is not None:
            timing["heard"] = self._heard
            self._heard = None
        self.last_timing = timing
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

    def ask_from_phone(self, text, heard=False):
        """A message from Telegram: answer in text (no speaking on the PC)."""
        drafts = []
        self._phone_cards = []
        if not self.busy.acquire(timeout=90):
            return "I'm tied up with something on the PC. Try again in a moment.", drafts
        old_show = self.brain.show_draft
        try:
            self.brain.show_draft = lambda body, label: (drafts.append((label, body)), old_show(body, label))
            self.ui.add_message("You", f"(phone) {text}")
            self.ui.set_state("thinking", "Message from your phone")
            who = user_name(self.settings)
            note = (f"[Sent from {who}'s phone{' as a voice note' if heard else ''}. {who} is away from the PC: "
                    "reply in text for a phone screen, don't open files on the PC, and keep it brief.] ")
            try:
                reply = self.brain.ask(note + text)
            except Exception as e:
                log.exception("Phone request failed")
                reply = f"Sorry, something went wrong: {str(e)[:160]}"
            self.ui.add_message("Moo", reply)
            for png in self._phone_cards:
                drafts.append(("__image__", png))
            return reply, drafts
        finally:
            self.brain.show_draft = old_show
            self._phone_cards = None
            self.ui.set_state("idle")
            self.busy.release()
            self._footer()

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
            if self.mouth:
                self.mouth.stop()
            if self.ears and self.ears.stream:
                self.ears.stream.stop()
        finally:
            self.ui.root.destroy()


def main():
    MooApp().start()
