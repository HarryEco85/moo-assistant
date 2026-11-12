"""The home-screen dashboard: weather, today's diary, inbox, football, the car, music and headlines.

Collected quietly in the background (each source on its own timer) so the screen never waits on anything.
Every tile is optional: if a source isn't connected or doesn't answer, its tile just isn't shown.
"""
import logging
import re
import threading
import time
from datetime import datetime, timedelta

log = logging.getLogger("moo")
EVERY = {"weather": 1800, "diary": 300, "inbox": 180, "football": 1800, "car": 900, "music": 30, "news": 1800,
         "jobs": 900}
LINE = re.compile(r"- id=\S+ \|(?: [^|]+ \|)? (\d{4}-\d\d-\d\d \d\d:\d\d) \| from (.*?) \| (.*?)(?: \| (.*))?$", re.M)


class Dashboard:
    def __init__(self, app):
        self.app = app
        self.data = {}
        self._due = {}
        self._stop = threading.Event()
        self.changed = lambda: None

    def start(self):
        threading.Thread(target=self._loop, daemon=True, name="dashboard").start()

    def stop(self):
        self._stop.set()

    def _loop(self):
        time.sleep(4)
        while not self._stop.is_set():
            changed = False
            for key, fn in (("weather", self._weather), ("diary", self._diary), ("inbox", self._inbox),
                            ("football", self._football), ("car", self._car), ("music", self._music),
                            ("news", self._news)):
                if not self.app.settings.get("dashboard", True) or time.time() < self._due.get(key, 0):
                    continue
                self._due[key] = time.time() + EVERY[key]
                try:
                    val = fn()
                except Exception as e:
                    log.debug("Dashboard %s: %s", key, e)
                    self._due[key] = time.time() + min(EVERY[key], 300)
                    continue
                if val != self.data.get(key):
                    self.data[key] = val
                    changed = True
            if changed:
                try:
                    self.changed()
                except Exception:
                    pass
            self._stop.wait(5)

    def refresh(self, key=None):
        for k in ([key] if key else list(EVERY)):
            self._due[k] = 0

    # ---------- sources: each returns a small dict, or None to hide the tile ----------
    def _weather(self):
        place = self.app.settings.get("home_location") or ""
        if not place:
            return None
        from .extras import weather_data
        w = weather_data(place, 3)
        today = w["days"][0]
        rest = [f"{d['date']:%a} {d['min']}–{d['max']}° {d['words']}" for d in w["days"][1:3]]
        wet = w["wet_hours"]
        return {"place": w["place"].split(",")[0], "now": f"{today['max']}°", "words": today["words"].capitalize(),
                "line": f"{today['min']}–{today['max']}°C · rain {today['rain_pct']}% · wind {today['wind']} mph",
                "wet": f"Wet from {wet[0]}" if wet else "Dry through the working day", "next": rest}

    def _diary(self):
        brain = self.app.brain
        now = datetime.now()
        start, end = now.replace(hour=0, minute=0, second=0, microsecond=0), now.replace(hour=23, minute=59)
        events = []
        from .gmail import GmailNotConfigured, google_calendar_events
        from .outlook import OutlookNotConfigured
        ok = False
        for fn in (lambda: brain.outlook.calendar_events(start, end), lambda: google_calendar_events(start, end)):
            try:
                events += fn()
                ok = True
            except (OutlookNotConfigured, GmailNotConfigured):
                pass
        if not ok:
            return None
        rows = []
        for e in sorted(events, key=lambda e: e["start"]):
            if e.get("all_day"):
                rows.append(("All day", e["title"], False))
            else:
                done = e.get("end", e["start"]) < now
                rows.append((f"{e['start']:%H:%M}", e["title"], done))
        return {"rows": rows[:6], "count": len(rows)}

    def _inbox(self):
        brain = self.app.brain
        found = []
        for box in (brain.outlook, brain.gmail):
            try:
                found.append(box.list_emails("inbox", 48, True, 8))
            except Exception:
                pass
        if not found:
            return None
        rows = []
        for m in LINE.finditer("\n".join(found)):
            when, sender, subject, flags = m.group(1), m.group(2), m.group(3), m.group(4) or ""
            sender = re.sub(r"\s*<.*?>", "", sender).strip('" ') or sender
            rows.append((when, sender[:28], subject[:70], "HIGH" in flags))
        rows.sort(reverse=True)
        waiting = len(brain.desk.waiting()) if getattr(brain, "desk", None) else 0
        return {"rows": rows[:4], "unread": len(rows), "review": waiting}

    def _football(self):
        team = self.app.settings.get("favourite_team") or ""
        if not team:
            return None
        from .football import find_team, _events
        t = find_team(team)
        if not t:
            return None
        ms = _events(t[0], t[1])
        now = datetime.now().astimezone()
        last = next((m for m in reversed(ms) if m["done"]), None)
        live = next((m for m in ms if m["state"] == "in"), None)
        nxt = next((m for m in ms if not m["done"] and m["state"] != "in" and m["when"] > now), None)
        out = {"team": t[2]}
        if live:
            out["live"] = f"LIVE {live['home']} {live['hs']}-{live['as']} {live['away']} ({live['detail']})"
        if last:
            out["last"] = f"{last['home']} {last['hs']}-{last['as']} {last['away']}"
        if nxt:
            out["next"] = f"{nxt['home']} v {nxt['away']}"
            out["when"] = f"{nxt['when']:%a %d %b, %H:%M}" + (f" · {nxt['tv']}" if nxt["tv"] else "")
        return out

    def _car(self):
        car = self.app.brain.car
        if not car.enabled:
            return None
        txt = car.status(wake=False)
        if "asleep" in txt.lower():
            prev = self.data.get("car") or {}
            return {**prev, "asleep": True} if prev else {"asleep": True, "battery": "", "line": "Asleep"}
        bat = re.search(r"Battery (\d+)%(?:, about (\d+) miles)?", txt)
        where = re.search(r"Location: ([^.,]+)", txt)
        return {"asleep": False, "battery": bat.group(1) if bat else "",
                "miles": bat.group(2) if bat and bat.group(2) else "",
                "line": ("Locked" if "Locked" in txt else "UNLOCKED") +
                        (" · charging" if "Charging: Charging" in txt else "") +
                        (f" · {where.group(1)}" if where else "")}

    def _music(self):
        sp = self.app.brain.spotify
        if not sp.enabled:
            return None
        txt = sp.now_playing()
        m = re.match(r"(Playing|Paused): (.*?) by (.*?) \((.*?)\) on (.*?), volume", txt)
        if not m:
            return {"state": "Nothing playing", "song": "", "artist": ""}
        return {"state": m.group(1), "song": m.group(2), "artist": m.group(3), "device": m.group(5)}

    def _news(self):
        from .jarvis import news
        txt = news(self.app.settings.get("dashboard_news", "top"), 4)
        rows = [re.sub(r"^- ", "", x).split(":")[0] for x in txt.splitlines()[1:] if x.startswith("- ")]
        return {"rows": rows[:3]} if rows else None


def tiles(data, side):
    """Tiles for one side of the screen: [(icon, title, lines, prompt)], lines = [(text, style)]."""
    out = []
    if side == "left":
        w = data.get("weather")
        if w:
            out.append(("sun", f"WEATHER · {w['place'].upper()}",
                        [(f"{w['now']}  {w['words']}", "big"), (w["line"], "sub"), (w["wet"], "accent")] +
                        [(x, "sub") for x in w["next"]], "What's the weather doing today?"))
        dia = data.get("diary")
        if dia is not None:
            rows = [(f"{t}  {title}", "done" if done else "row") for t, title, done in dia["rows"]] or \
                [("Nothing in the diary today", "sub")]
            out.append(("calendar", f"TODAY · {dia['count']} IN THE DIARY", rows, "What's my diary looking like today?"))
        f = data.get("football")
        if f:
            lines = []
            if f.get("live"):
                lines.append((f["live"], "accent"))
            if f.get("next"):
                lines += [(f"Next: {f['next']}", "row"), (f.get("when", ""), "sub")]
            if f.get("last"):
                lines.append((f"Last: {f['last']}", "sub"))
            out.append(("ball", f"{f['team'].upper()}", lines, f"How are {f['team']} getting on?"))
    else:
        ib = data.get("inbox")
        if ib is not None:
            rows = [(f"{s}: {subj}", "accent" if high else "row") for _, s, subj, high in ib["rows"]] or \
                [("Inbox clear: nothing unread", "sub")]
            title = f"INBOX · {ib['unread']} UNREAD" + (f" · {ib['review']} TO REVIEW" if ib.get("review") else "")
            out.append(("mail", title, rows, "Anything important in my inbox?"))
        c = data.get("car")
        if c:
            lines = [(f"{c['battery']}%" + (f"  ·  {c['miles']} miles" if c.get("miles") else ""), "big")] \
                if c.get("battery") else []
            lines.append((c.get("line", "") + (" · asleep" if c.get("asleep") and c.get("battery") else ""), "sub"))
            out.append(("car", "TESLA", lines, "How's the car?"))
        mu = data.get("music")
        if mu:
            lines = [(mu["song"] or mu["state"], "row")]
            if mu.get("artist"):
                lines.append((f"{mu['artist']} · {mu.get('device', '')}", "sub"))
            out.append(("music", f"SPOTIFY · {mu['state'].upper()}", lines,
                        "Pause the music" if mu["state"] == "Playing" else "Play some music"))
        nw = data.get("news")
        if nw:
            out.append(("news", "HEADLINES", [(x, "row") for x in nw["rows"]], "What's in the news?"))
    return out
