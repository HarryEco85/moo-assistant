"""The approval queue: things Moo would like to do that you haven't asked for.

She (or one of her background workers) adds a request with a short title,
a detail line and the exact instruction she'll carry out. Nothing happens
until you press Approve, on the PC (office view or chat) or on your phone.
"""
import threading
import time
import uuid

from .config import DATA_DIR, read_json, write_json

APPROVALS_FILE = DATA_DIR / "approvals.json"


class Approvals:
    def __init__(self, path=APPROVALS_FILE):
        self.path = path
        self._lock = threading.Lock()
        self.items = read_json(path, [])
        # anything left mid-run when Moo closed goes back to waiting
        for a in self.items:
            if a.get("status") == "running":
                a["status"] = "pending"
        self.version = time.time()

    def _save(self):
        pending = [a for a in self.items if a["status"] in ("pending", "running")]
        old = [a for a in self.items if a["status"] not in ("pending", "running")][-60:]
        self.items = old + pending
        write_json(self.path, self.items)
        self.version = time.time()

    def add(self, title, detail, instruction, source="moo", key=None):
        """Queue a request. `key` stops the same suggestion being queued twice."""
        with self._lock:
            if key and any(a.get("key") == key for a in self.items):
                return None
            a = {"id": uuid.uuid4().hex[:6], "title": title.strip()[:120], "detail": (detail or "").strip()[:240],
                 "instruction": instruction.strip()[:2000], "source": source, "key": key,
                 "status": "pending", "created": time.time()}
            self.items.append(a)
            self._save()
            return a

    def pending(self) -> list:
        return [a for a in self.items if a["status"] in ("pending", "running")]

    def get(self, aid):
        return next((a for a in self.items if a["id"] == aid), None)

    def set(self, aid, status, result=None):
        with self._lock:
            a = self.get(aid)
            if not a:
                return None
            a["status"] = status
            a["updated"] = time.time()
            if result is not None:
                a["result"] = result[:600]
            self._save()
            return a

    def claim(self, aid):
        """Mark as running if still pending; returns the item, or None if already handled."""
        with self._lock:
            a = self.get(aid)
            if not a or a["status"] != "pending":
                return None
            a["status"] = "running"
            self._save()
            return a

    def done_today(self) -> int:
        start = time.mktime(time.localtime()[:3] + (0, 0, 0, 0, 0, -1))
        return sum(1 for a in self.items if a["status"] == "done" and a.get("updated", 0) >= start)

    def as_text(self) -> str:
        items = self.pending()
        if not items:
            return "Nothing waiting for approval."
        return "\n".join(f"- [{a['id']}] {a['title']} ({a['detail']}) status {a['status']}" for a in items)
