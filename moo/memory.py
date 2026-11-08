"""Long-term memory, conversation history and spend tracking.

Memory is a plain list of facts you've asked Moo to remember. You can view,
edit or delete them in %APPDATA%\\MooLite\\memory.json, or just say
"Moo, forget that I ..." / "what do you remember about me?".
"""
import re
from datetime import datetime

from .config import DATA_DIR, read_json, write_json

MEMORY_FILE = DATA_DIR / "memory.json"
HISTORY_FILE = DATA_DIR / "history.json"
USAGE_FILE = DATA_DIR / "usage.json"

# Anything that looks like a secret is refused, so passwords and keys never land in memory.
_SECRET_PATTERNS = [
    r"\bpassword\b", r"\bpasscode\b", r"\bpin\b", r"\bsk-ant-", r"\bapi[ _-]?key\b",
    r"\btoken\b", r"\b\d{4}[ -]?\d{4}[ -]?\d{4}[ -]?\d{4}\b", r"\bsort code\b",
]


class Memory:
    def __init__(self):
        self.facts = read_json(MEMORY_FILE, [])

    def all(self):
        return list(self.facts)

    def remember(self, fact: str) -> str:
        fact = fact.strip()
        if not fact:
            return "Nothing to remember."
        if any(re.search(p, fact, re.I) for p in _SECRET_PATTERNS):
            return "Refused: that looks like a password, key or account number, which Moo never stores."
        if any(f["fact"].lower() == fact.lower() for f in self.facts):
            return "Already remembered."
        self.facts.append({"fact": fact, "added": datetime.now().strftime("%Y-%m-%d")})
        write_json(MEMORY_FILE, self.facts)
        return f"Remembered: {fact}"

    def forget(self, text: str) -> str:
        words = [w for w in re.findall(r"\w+", text.lower()) if len(w) > 2]
        if not words:
            return "Tell me what to forget."
        keep, gone = [], []
        for f in self.facts:
            low = f["fact"].lower()
            (gone if all(w in low for w in words) else keep).append(f)
        if not gone:
            return "I couldn't find a matching memory."
        self.facts = keep
        write_json(MEMORY_FILE, self.facts)
        return "Forgotten: " + "; ".join(f["fact"] for f in gone)

    def as_prompt(self) -> str:
        if not self.facts:
            return "(nothing saved yet)"
        return "\n".join(f"- {f['fact']}" for f in self.facts)


class History:
    """Recent conversation so follow-up questions work, kept across restarts."""

    MAX_TURNS = 8  # user+assistant exchanges kept

    def __init__(self):
        self.messages = read_json(HISTORY_FILE, [])

    def add_exchange(self, user_text: str, reply_text: str):
        self.messages.append({"role": "user", "content": user_text})
        self.messages.append({"role": "assistant", "content": reply_text})
        self.messages = self.messages[-self.MAX_TURNS * 2:]
        write_json(HISTORY_FILE, self.messages)

    def clear(self):
        self.messages = []
        write_json(HISTORY_FILE, [])


# Published Anthropic list prices, USD per million tokens (input, output)
PRICES = {
    "claude-haiku-5-5": (0.10, 0.50),
    "claude-sonnet-5-5": (2.0, 10.0),
    "claude-opus-5-5": (4.0, 20.0),
}


class Usage:
    def __init__(self):
        self.data = read_json(USAGE_FILE, {})

    def _month(self):
        return datetime.now().strftime("%Y-%m")

    def add(self, model: str, input_tokens: int, output_tokens: int, cache_read: int = 0, searches: int = 0):
        price_in, price_out = PRICES.get(model, (2.0, 10.0))
        cost = (input_tokens * price_in + cache_read * price_in * 0.1 + output_tokens * price_out) / 1_000_000
        cost += searches * 0.01  # web search: $10 per 1,000
        m = self.data.setdefault(self._month(), {"usd": 0.0, "requests": 0})
        m["usd"] = round(m["usd"] + cost, 6)
        m["requests"] += 1
        write_json(USAGE_FILE, self.data)

    def this_month(self) -> float:
        return self.data.get(self._month(), {}).get("usd", 0.0)
