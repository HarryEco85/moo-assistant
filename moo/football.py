"""Your football team: fixtures, results, the league table and match-day alerts.

Uses ESPN's public scores feed (no account needed). If it ever stops answering,
Moo falls back to searching the web.
"""
import logging
import time
from datetime import datetime, timezone

import requests

log = logging.getLogger("moo")
BASE = "https://site.api.espn.com/apis/site/v2/sports/soccer"
LEAGUES = ["eng.1", "eng.2", "eng.3", "eng.4", "sco.1"]
CUPS = ["uefa.champions", "uefa.europa", "uefa.europa.conf", "eng.fa", "eng.league_cup"]
KNOWN = {"tottenham": ("367", "eng.1", "Tottenham Hotspur"), "spurs": ("367", "eng.1", "Tottenham Hotspur")}
_cache = {}


def _get(url, ttl=600):
    hit = _cache.get(url)
    if hit and time.time() - hit[0] < ttl:
        return hit[1]
    r = requests.get(url, timeout=15, headers={"User-Agent": "Mozilla/5.0 (Moo assistant)"})
    r.raise_for_status()
    data = r.json()
    _cache[url] = (time.time(), data)
    return data


def find_team(name):
    """(team id, league, display name) for a team name."""
    key = (name or "").lower().strip()
    for k, v in KNOWN.items():
        if k in key:
            return v
    for lg in LEAGUES:
        try:
            data = _get(f"{BASE}/{lg}/teams", ttl=86400)
            for t in data["sports"][0]["leagues"][0]["teams"]:
                team = t["team"]
                names = " ".join(str(team.get(f, "")) for f in ("displayName", "shortDisplayName", "name",
                                                                  "nickname", "abbreviation")).lower()
                if key and (key in names or names.startswith(key)):
                    return team["id"], lg, team["displayName"]
        except Exception:
            continue
    return None


def _score(c):
    s = c.get("score")
    if isinstance(s, dict):
        return s.get("displayValue") or (str(int(s["value"])) if s.get("value") is not None else "")
    return str(s or "")


def _events(team_id, league):
    seen, out = set(), []
    for lg in [league] + CUPS:
        for extra in ("", "?fixture=true"):
            try:
                data = _get(f"{BASE}/{lg}/teams/{team_id}/schedule{extra}")
            except Exception:
                continue
            comp_name = (data.get("leagues") or [{}])[0].get("name") or data.get("season", {}).get("name") or lg
            for e in data.get("events", []):
                if e.get("id") in seen:
                    continue
                seen.add(e.get("id"))
                c = (e.get("competitions") or [{}])[0]
                teams = c.get("competitors") or []
                home = next((t for t in teams if t.get("homeAway") == "home"), teams[0] if teams else {})
                away = next((t for t in teams if t.get("homeAway") == "away"), teams[1] if len(teams) > 1 else {})
                st = (c.get("status") or e.get("status") or {}).get("type", {})
                tv = ", ".join(b.get("media", {}).get("shortName", "") for b in (c.get("broadcasts") or [])
                               if b.get("media")) or ", ".join(n for b in (c.get("broadcasts") or [])
                                                             for n in b.get("names", []))
                try:
                    when = datetime.fromisoformat(e["date"].replace("Z", "+00:00")).astimezone()
                except Exception:
                    continue
                out.append({"id": e.get("id"), "when": when, "comp": comp_name,
                            "home": home.get("team", {}).get("displayName", "?"),
                            "away": away.get("team", {}).get("displayName", "?"),
                            "hs": _score(home), "as": _score(away),
                            "state": st.get("state", ""), "done": bool(st.get("completed")),
                            "detail": st.get("shortDetail", ""), "tv": tv,
                            "venue": (c.get("venue") or {}).get("fullName", "")})
    out.sort(key=lambda x: x["when"])
    return out


def _line(m, mine):
    when = m["when"].strftime("%a %d %b, %H:%M")
    if m["done"]:
        return f"{when}: {m['home']} {m['hs']}-{m['as']} {m['away']} ({m['comp']})"
    if m["state"] == "in":
        return f"LIVE: {m['home']} {m['hs']}-{m['as']} {m['away']} ({m['detail']}, {m['comp']})"
    return f"{when}: {m['home']} v {m['away']} ({m['comp']})" + (f", on {m['tv']}" if m["tv"] else "") + \
        (f", at {m['venue']}" if m["venue"] else "")


def report(team_name, what="overview") -> str:
    team = find_team(team_name)
    if not team:
        return f"I couldn't find {team_name} in the football feed. Use web search instead."
    tid, league, display = team
    try:
        ms = _events(tid, league)
    except Exception as e:
        return f"The football feed didn't answer ({e}). Use web search instead."
    now = datetime.now(timezone.utc).astimezone()
    past = [m for m in ms if m["done"]][-5:]
    live = [m for m in ms if m["state"] == "in"]
    nxt = [m for m in ms if not m["done"] and m["state"] != "in" and m["when"] > now][:5]
    out = [f"{display}:"]
    if what in ("overview", "live") and live:
        out += ["Live now:"] + [_line(m, display) for m in live]
    if what in ("overview", "results"):
        out += ["Recent results:"] + [_line(m, display) for m in reversed(past)]
    if what in ("overview", "fixtures"):
        out += ["Coming up:"] + [_line(m, display) for m in nxt]
    if what in ("overview", "table"):
        out += [table(league, display)]
    if what == "news":
        out += [news(tid, league)]
    return "\n".join(out)


def table(league, highlight="") -> str:
    try:
        data = _get(f"https://site.api.espn.com/apis/v2/sports/soccer/{league}/standings", ttl=1800)
        group = (data.get("children") or [data])[0]
        rows = []
        for e in group["standings"]["entries"]:
            st = {s.get("name"): s.get("displayValue", s.get("value")) for s in e.get("stats", [])}
            rows.append((int(float(st.get("rank") or 99)), e["team"]["displayName"], st.get("gamesPlayed"),
                         st.get("points"), st.get("pointDifferential")))
        rows.sort()
        idx = next((i for i, r in enumerate(rows) if highlight and r[1] == highlight), None)
        pick = rows[:4] + ([] if idx is None or idx < 4 else rows[max(4, idx - 2):idx + 3])
        return "Table (pos, team, played, points, GD):\n" + "\n".join(
            f"{r[0]}. {r[1]}  P{r[2]}  {r[3]}pts  GD {r[4]}" for r in pick)
    except Exception as e:
        return f"(League table unavailable: {e})"


def news(team_id, league) -> str:
    try:
        data = _get(f"{BASE}/{league}/news?team={team_id}", ttl=1800)
        arts = data.get("articles", [])[:5]
        return "Latest news:\n" + "\n".join(f"- {a.get('headline')}: {a.get('description', '')[:160]}" for a in arts)
    except Exception as e:
        return f"(News unavailable: {e}; use web search.)"


def today_and_finished(team_name):
    """For match-day alerts: (today's upcoming matches, matches finished in the last 4 hours)."""
    team = find_team(team_name)
    if not team:
        return [], [], ""
    ms = _events(team[0], team[1])
    now = datetime.now(timezone.utc).astimezone()
    today = [m for m in ms if m["when"].date() == now.date() and not m["done"] and m["state"] != "in"]
    done = [m for m in ms if m["done"] and 0 < (now - m["when"]).total_seconds() < 6 * 3600]
    return today, done, team[2]
