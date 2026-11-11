"""Long-term memory: a searchable journal of every conversation and every decision.

Stored in a small SQLite database in Moo's data folder (journal.db). Uses
SQLite's full-text search when available, plain matching otherwise.
"""
import re
import sqlite3
import threading
from datetime import datetime, timedelta

from .config import DATA_DIR

DB_FILE = DATA_DIR / "journal.db"
STOP = set("a an and the of to in on for with what did we say about was is it that this my me i you at by "
           "from last week when where how do does have has any".split())


class Journal:
    def __init__(self, path=DB_FILE):
        self.path = str(path)
        self._lock = threading.Lock()
        self.fts = True
        with self._db() as db:
            try:
                db.execute("CREATE VIRTUAL TABLE IF NOT EXISTS turns USING fts5(ts UNINDEXED, channel UNINDEXED, "
                           "user, reply)")
                db.execute("CREATE VIRTUAL TABLE IF NOT EXISTS decisions USING fts5(ts UNINDEXED, text, "
                           "source UNINDEXED)")
            except sqlite3.OperationalError:
                self.fts = False
                db.execute("CREATE TABLE IF NOT EXISTS turns (ts TEXT, channel TEXT, user TEXT, reply TEXT)")
                db.execute("CREATE TABLE IF NOT EXISTS decisions (ts TEXT, text TEXT, source TEXT)")

    def _db(self):
        db = sqlite3.connect(self.path, timeout=10)
        return db

    @staticmethod
    def _now():
        return datetime.now().strftime("%Y-%m-%d %H:%M")

    # ---------- writing ----------
    def add_turn(self, user, reply, channel="pc"):
        user = re.sub(r"^(\[[^\]]{0,400}\]\s*)+", "", user or "").strip()
        if not user:
            return
        with self._lock, self._db() as db:
            db.execute("INSERT INTO turns (ts, channel, user, reply) VALUES (?,?,?,?)",
                       (self._now(), channel, user[:4000], (reply or "")[:6000]))

    def log_decision(self, text, source="said") -> str:
        text = (text or "").strip().rstrip(".")
        if not text:
            return "Nothing to log."
        with self._lock, self._db() as db:
            recent = db.execute("SELECT text FROM decisions ORDER BY rowid DESC LIMIT 30").fetchall()
            if any(text.lower() == r[0].lower() for r in recent):
                return "Already logged."
            db.execute("INSERT INTO decisions (ts, text, source) VALUES (?,?,?)", (self._now(), text[:500], source))
        return "Decision logged."

    # ---------- reading ----------
    def _terms(self, query):
        words = [w for w in re.findall(r"[A-Za-z0-9]+", (query or "").lower()) if w not in STOP and len(w) > 1]
        return words[:8]

    def _match(self, db, table, cols, query, since, limit):
        words = self._terms(query)
        if not words:
            rows = db.execute(f"SELECT ts, {cols} FROM {table} WHERE ts >= ? ORDER BY rowid DESC LIMIT ?",
                              (since, limit)).fetchall()
            return rows
        if self.fts:
            fts_q = " OR ".join(f'"{w}"*' for w in words)
            try:
                return db.execute(f"SELECT ts, {cols} FROM {table} WHERE {table} MATCH ? AND ts >= ? "
                                  f"ORDER BY bm25({table}) LIMIT ?", (fts_q, since, limit)).fetchall()
            except sqlite3.OperationalError:
                pass
        first = cols.split(",")[0].strip()
        like = " OR ".join(f"lower({c.strip()}) LIKE ?" for c in cols.split(",") for _ in words)
        args = [f"%{w}%" for c in cols.split(",") for w in words]
        return db.execute(f"SELECT ts, {cols} FROM {table} WHERE ({like}) AND ts >= ? ORDER BY rowid DESC LIMIT ?",
                          (*args, since, limit)).fetchall() if first else []

    def search(self, query, days=90, limit=8) -> str:
        since = (datetime.now() - timedelta(days=max(1, int(days or 90)))).strftime("%Y-%m-%d")
        with self._lock, self._db() as db:
            dec = self._match(db, "decisions", "text", query, since, 6)
            turns = self._match(db, "turns", "user, reply", query, since, limit)
        out = []
        if dec:
            out.append("Decisions:")
            out += [f"- {self._when(ts)}: {text}" for ts, text in dec]
        if turns:
            out.append("Past conversations:")
            for ts, user, reply in sorted(turns, key=lambda r: r[0]):
                out.append(f"- {self._when(ts)}. They said: {user[:300]}\n  You replied: {reply[:500]}")
        return "\n".join(out) if out else "Nothing found in past conversations or decisions for that."

    def turns_since(self, since: str) -> list:
        with self._lock, self._db() as db:
            return db.execute("SELECT ts, channel, user, reply FROM turns WHERE ts >= ? ORDER BY ts", (since,)).fetchall()

    def recent_decisions(self, n=8) -> list:
        with self._lock, self._db() as db:
            return db.execute("SELECT ts, text FROM decisions ORDER BY rowid DESC LIMIT ?", (n,)).fetchall()

    def counts(self) -> tuple:
        with self._lock, self._db() as db:
            return (db.execute("SELECT count(*) FROM turns").fetchone()[0],
                    db.execute("SELECT count(*) FROM decisions").fetchone()[0])

    @staticmethod
    def _when(ts):
        try:
            d = datetime.strptime(ts, "%Y-%m-%d %H:%M")
            return d.strftime("%a %d %b %Y, %H:%M")
        except ValueError:
            return ts
