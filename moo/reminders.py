"""Reminders: stored in reminders.json, checked every 15 seconds.

When one is due Moo shows a Windows notification and says it out loud.
Reminders survive restarts; any that came due while Moo was closed fire
when she next starts.
"""
import threading
import uuid
from datetime import datetime, timedelta

from .config import DATA_DIR, read_json, write_json

REMINDERS_FILE = DATA_DIR / "reminders.json"


def _parse(when: str) -> datetime:
    """Accepts ISO 'YYYY-MM-DDTHH:MM' (what the brain is told to send)."""
    when = when.strip().replace(" ", "T")
    dt = datetime.fromisoformat(when)
    return dt.replace(tzinfo=None)  # local time


class Reminders:
    def __init__(self, on_due):
        self.on_due = on_due  # callback(text)
        self.items = read_json(REMINDERS_FILE, [])
        self._lock = threading.Lock()
        self._stop = threading.Event()

    def _save(self):
        write_json(REMINDERS_FILE, self.items)

    def add(self, text: str, when: str, repeat: str = "none") -> str:
        try:
            due = _parse(when)
        except ValueError:
            return f"Couldn't understand the time '{when}'. Use YYYY-MM-DDTHH:MM."
        if repeat not in ("none", "daily", "weekdays", "weekly"):
            repeat = "none"
        if due < datetime.now() - timedelta(minutes=1) and repeat == "none":
            return "That time has already passed."
        with self._lock:
            item = {"id": uuid.uuid4().hex[:6], "text": text.strip(), "due": due.isoformat(timespec="minutes"),
                    "repeat": repeat}
            self.items.append(item)
            self._save()
        return f"Reminder set for {due.strftime('%a %d %b at %H:%M')}" + (f", repeating {repeat}" if repeat != "none" else "") + f" (id {item['id']})."

    def list(self) -> list:
        with self._lock:
            return sorted(self.items, key=lambda r: r["due"])

    def cancel(self, ref: str) -> str:
        ref = ref.strip().lower()
        with self._lock:
            match = [r for r in self.items if r["id"] == ref or ref in r["text"].lower()]
            if not match:
                return "No matching reminder."
            for r in match:
                self.items.remove(r)
            self._save()
        return "Cancelled: " + "; ".join(r["text"] for r in match)

    @staticmethod
    def _next(due: datetime, repeat: str) -> datetime:
        step = timedelta(days=7) if repeat == "weekly" else timedelta(days=1)
        nxt = due + step
        now = datetime.now()
        while nxt <= now or (repeat == "weekdays" and nxt.weekday() >= 5):
            nxt += timedelta(days=1)
        return nxt

    def _tick(self):
        now = datetime.now()
        fired = []
        with self._lock:
            for r in list(self.items):
                due = datetime.fromisoformat(r["due"])
                if due <= now:
                    fired.append(r["text"])
                    if r.get("repeat", "none") == "none":
                        self.items.remove(r)
                    else:
                        r["due"] = self._next(due, r["repeat"]).isoformat(timespec="minutes")
            if fired:
                self._save()
        for text in fired:
            self.on_due(text)

    def start(self):
        def loop():
            while not self._stop.is_set():
                try:
                    self._tick()
                except Exception:
                    pass
                self._stop.wait(15)
        threading.Thread(target=loop, daemon=True, name="reminders").start()

    def stop(self):
        self._stop.set()


def notify(title: str, message: str):
    """Windows toast notification; silently skipped if unavailable."""
    try:
        from winotify import Notification, audio
        toast = Notification(app_id="Moo", title=title, msg=message[:250], duration="long")
        toast.set_audio(audio.Reminder, loop=False)
        toast.show()
    except Exception:
        pass

