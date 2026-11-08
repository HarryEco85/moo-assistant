"""Skills: repeatable jobs taught once, done the same way every time.

Each skill has a name, when to use it, and the steps. They live in
skills.json in Moo's data folder and can be edited in Settings > Skills or
created by voice ("save that as a skill").
"""
import re
import threading
import time

from .config import DATA_DIR, read_json, write_json

SKILLS_FILE = DATA_DIR / "skills.json"

STARTERS = [
    {"name": "Monday ops summary",
     "when": "Monday summary, weekly ops summary, how did last week go, weekly numbers",
     "steps": "1. If VH3 is connected, use vh3_aggregate_jobs for the last 7 days: jobs completed, jobs still open, "
              "first-time fix or completion rate if available, and the busiest engineers.\n"
              "2. Use vh3_run_sentinels and pick out the critical warnings only.\n"
              "3. Use vh3_invoice_summary and vh3_quote_summary for the last 7 days: invoiced total and quotes "
              "outstanding.\n"
              "4. Check list_promises for anything overdue.\n"
              "5. Give a spoken summary in about six sentences: headline numbers, what went well, the two or three "
              "things that need attention this week.\n"
              "6. Send the same summary to the phone with send_to_phone, laid out as a short list."},
    {"name": "Prep me for tomorrow",
     "when": "prep me for tomorrow, what's tomorrow look like, get me ready for tomorrow",
     "steps": "1. get_calendar for tomorrow and check_diary_clashes for tomorrow.\n"
              "2. For each meeting with an address or postcode, work out the drive time from home, and from the "
              "previous meeting.\n"
              "3. For each meeting, search_emails for the person or company to find the latest relevant email, and "
              "note anything open.\n"
              "4. get_weather for tomorrow (flag rain timing, it matters for site work).\n"
              "5. Tell me the shape of the day, when to leave for the first thing, any clashes, and one line per "
              "meeting on what I need to know."},
    {"name": "Inbox sweep",
     "when": "sort my inbox, inbox sweep, go through my emails, clear my inbox",
     "steps": "1. list_emails for the inbox, last 24 hours, both accounts.\n"
              "2. Sort them into: needs a reply from me, FYI, and noise.\n"
              "3. For anything from a customer or colleague that needs a reply and is simple, read it and save a "
              "draft reply (create_email_draft as a reply). Never more than five drafts. Leave [brackets] for "
              "anything you don't know.\n"
              "4. Check the drafts were saved.\n"
              "5. Tell me: how many need me, which ones you drafted, and anything that needs a decision from me."},
    {"name": "RAMS wording",
     "when": "RAMS, method statement, risk assessment wording",
     "steps": "1. Ask for anything essential you don't know: the task, the site, and any specific hazards.\n"
              "2. Write ready-to-paste plain narrative wording (not a full document): scope of works, sequence of "
              "works, key hazards and how they're controlled, PPE, permits and isolations if relevant, emergency "
              "arrangements. Use correct UK regulations where relevant (CDM 2015, Confined Spaces Regulations 1997, "
              "Gas Safety (Installation and Use) Regulations 1998, Electricity at Work Regulations 1989).\n"
              "3. Put it on the clipboard with copy_to_clipboard and say it's ready to paste."},
    {"name": "Quote wording",
     "when": "quote wording, write up a quote, scope for a quote",
     "steps": "1. Ask for anything essential you don't know: the client, site and work.\n"
              "2. Write only the wording to drop into the existing quote template: a short scope of works, "
              "inclusions, exclusions and assumptions. No prices unless given, no letterhead.\n"
              "3. Put it on the clipboard with copy_to_clipboard and say it's ready to paste."},
    {"name": "Chase a quote",
     "when": "chase a quote, follow up a quote, nudge the customer about the quote",
     "steps": "1. search_emails for the quote sent to that person or company and read the latest one.\n"
              "2. Draft a short, friendly chase as a reply (create_email_draft with reply_to_message_id): check "
              "they received it, offer to answer questions or walk the site, no pressure.\n"
              "3. Tell me it's in Drafts."},
]


class Skills:
    def __init__(self):
        self._lock = threading.Lock()
        if not SKILLS_FILE.exists():
            write_json(SKILLS_FILE, STARTERS)
        self.version = time.time()

    def all(self) -> list:
        data = read_json(SKILLS_FILE, [])
        return [s for s in data if isinstance(s, dict) and s.get("name")]

    def _write(self, items):
        write_json(SKILLS_FILE, items)
        self.version = time.time()

    def find(self, name: str):
        key = re.sub(r"\W+", " ", name or "").strip().lower()
        items = self.all()
        for s in items:
            if s["name"].lower() == key or re.sub(r"\W+", " ", s["name"]).strip().lower() == key:
                return s
        words = set(key.split())
        best = max(items, key=lambda s: len(words & set(re.sub(r"\W+", " ", s["name"] + " " + s.get("when", ""))
                                                       .lower().split())), default=None)
        return best if best and words & set(re.sub(r"\W+", " ", best["name"] + " " + best.get("when", ""))
                                            .lower().split()) else None

    def save(self, name, when, steps, old_name=None) -> str:
        name, when, steps = (name or "").strip()[:60], (when or "").strip()[:300], (steps or "").strip()[:4000]
        if not name or not steps:
            return "A skill needs a name and some steps."
        with self._lock:
            items = [s for s in self.all() if s["name"].lower() not in (name.lower(), (old_name or "").lower())]
            items.append({"name": name, "when": when, "steps": steps})
            items.sort(key=lambda s: s["name"].lower())
            self._write(items)
        return f"Saved the skill '{name}'."

    def delete(self, name) -> str:
        with self._lock:
            items = self.all()
            keep = [s for s in items if s["name"].lower() != (name or "").lower()]
            if len(keep) == len(items):
                return f"No skill called '{name}'."
            self._write(keep)
        return f"Deleted the skill '{name}'."

    def as_prompt(self) -> str:
        items = self.all()
        if not items:
            return "(none saved yet)"
        return "\n".join(f"- {s['name']}: use for {s.get('when') or s['name']}" for s in items)
