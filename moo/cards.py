"""Visual cards shown in Moo's window (and sent to the phone): a day planner
for the diary, an agenda for several days, and a weather card.

Drawn with Pillow at 2x and scaled down, so text and edges stay crisp.
"""
import io
import os
from datetime import datetime, timedelta

from PIL import Image, ImageDraw, ImageFont

from .themes import BG, FAINT, MUTED, SURFACE, SURFACE_HI, TEXT

S = 2  # supersampling

_FONT_FILES = {
    "regular": ["segoeui.ttf", "DejaVuSans.ttf", "Arial.ttf"],
    "bold": ["segoeuib.ttf", "seguisb.ttf", "DejaVuSans-Bold.ttf", "Arialbd.ttf"],
    "light": ["segoeuil.ttf", "segoeui.ttf", "DejaVuSans.ttf"],
}
_FONT_DIRS = [os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "Fonts"),
              "/usr/share/fonts/truetype/dejavu", "/Library/Fonts"]
_cache = {}


def _font(kind, size):
    key = (kind, size)
    if key not in _cache:
        for name in _FONT_FILES[kind]:
            for d in _FONT_DIRS:
                path = os.path.join(d, name)
                if os.path.exists(path):
                    _cache[key] = ImageFont.truetype(path, int(size * S))
                    break
            if key in _cache:
                break
        else:
            _cache[key] = ImageFont.load_default(int(size * S))
    return _cache[key]


def _rgb(h):
    return tuple(int(h[i:i + 2], 16) for i in (1, 3, 5))


def _mix(a, b, t):
    a, b = _rgb(a) if isinstance(a, str) else a, _rgb(b) if isinstance(b, str) else b
    return tuple(int(a[i] + (b[i] - a[i]) * t) for i in range(3))


def _fit(draw, text, font, width):
    """Shorten text with an ellipsis so it fits the width."""
    if draw.textlength(text, font=font) <= width:
        return text
    while text and draw.textlength(text + "…", font=font) > width:
        text = text[:-1]
    return text.rstrip() + "…"


class _Canvas:
    def __init__(self, width, height):
        self.w, self.h = width, height
        self.img = Image.new("RGB", (width * S, height * S), _rgb(BG))
        self.d = ImageDraw.Draw(self.img)

    def rect(self, x0, y0, x1, y1, fill, r=8, outline=None):
        self.d.rounded_rectangle([x0 * S, y0 * S, x1 * S, y1 * S], radius=r * S, fill=fill, outline=outline,
                                 width=S if outline else 0)

    def text(self, x, y, s, kind="regular", size=12, fill=TEXT, anchor="la", maxw=None):
        f = _font(kind, size)
        if maxw:
            s = _fit(self.d, s, f, maxw * S)
        self.d.text((x * S, y * S), s, font=f, fill=_rgb(fill) if isinstance(fill, str) else fill, anchor=anchor)

    def line(self, x0, y0, x1, y1, fill, w=1):
        self.d.line([x0 * S, y0 * S, x1 * S, y1 * S], fill=_rgb(fill) if isinstance(fill, str) else fill, width=w * S)

    def circle(self, cx, cy, r, fill):
        self.d.ellipse([(cx - r) * S, (cy - r) * S, (cx + r) * S, (cy + r) * S], fill=fill)

    def done(self):
        return self.img.resize((self.w, self.h), Image.LANCZOS)


def to_png(img) -> bytes:
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


# ---------- diary ----------
def _when(d):
    return d.strftime("%A %-d %B") if os.name != "nt" else d.strftime("%A %#d %B")


def calendar_card(events, start: datetime, days: int, palette, width=440):
    """events: dicts with start, end (datetime), title, location, all_day, source ('Outlook'/'Google')."""
    colours = {"Outlook": palette["idle"][0], "Google": palette["idle"][1]}
    if days <= 1:
        return _day(events, start, colours, width)
    return _agenda(events, start, days, colours, width)


def _day(events, day, colours, W):
    all_day = [e for e in events if e["all_day"]]
    timed = sorted([e for e in events if not e["all_day"]], key=lambda e: e["start"])
    first_h = min([7] + [e["start"].hour for e in timed])
    last_h = max([18] + [min(24, e["end"].hour + (1 if e["end"].minute else 0)) for e in timed])
    HH = 38  # pixels per hour
    top = 64 + (30 if all_day else 0)
    H = top + (last_h - first_h) * HH + 18
    c = _Canvas(W, H)
    c.rect(0, 0, W - 1, H - 1, _rgb(SURFACE), r=14)
    n = len(events)
    c.text(18, 16, _when(day), "bold", 15)
    sub = "Nothing booked" if not n else f"{n} event{'s' if n != 1 else ''}" + \
        (f" · first at {timed[0]['start']:%H:%M}" if timed else "")
    c.text(18, 40, sub, size=10.5, fill=MUTED)
    # all-day chips
    x = 18
    for e in all_day[:3]:
        label = e["title"][:28]
        wpx = int(c.d.textlength(label, font=_font("regular", 10)) / S) + 20
        col = _rgb(colours.get(e["source"], colours["Outlook"]))
        c.rect(x, 62, x + wpx, 84, _mix(SURFACE, col, 0.3), r=11)
        c.text(x + 10, 73, label, size=10, anchor="lm")
        x += wpx + 6
    # hour grid
    left, right = 62, W - 16
    for h in range(first_h, last_h + 1):
        y = top + (h - first_h) * HH
        c.line(left, y, right, y, _mix(SURFACE, FAINT, 0.45))
        if h < last_h:
            c.text(left - 10, y, f"{h:02d}:00", size=9.5, fill=FAINT, anchor="rm")
    # lay out overlapping events side by side; events that overlap nothing get the full width
    placed, cluster, cluster_end = [], [], None

    def flush():
        cols_end, out = [], []
        for ev in cluster:
            for i, end in enumerate(cols_end):
                if end <= ev["start"]:
                    cols_end[i] = ev["end"]
                    out.append((ev, i))
                    break
            else:
                cols_end.append(ev["end"])
                out.append((ev, len(cols_end) - 1))
        placed.extend((ev, i, len(cols_end)) for ev, i in out)

    for e in timed:
        if cluster and e["start"] >= cluster_end:
            flush()
            cluster = []
        cluster.append(e)
        cluster_end = max(cluster_end, e["end"]) if cluster_end and len(cluster) > 1 else e["end"]
    if cluster:
        flush()
    for e, i, ncols in placed:
        colw = (right - left - 6) / ncols
        s = max(e["start"], day.replace(hour=first_h, minute=0))
        en = min(e["end"], day.replace(hour=0, minute=0) + timedelta(hours=last_h))
        y0 = top + ((s.hour - first_h) + s.minute / 60) * HH + 1
        y1 = max(y0 + 22, top + ((en.hour - first_h) + en.minute / 60) * HH - 1) if en > s else y0 + 22
        x0 = left + 6 + i * colw
        x1 = x0 + colw - 4
        col = _rgb(colours.get(e["source"], colours["Outlook"]))
        c.rect(x0, y0, x1, y1, _mix(SURFACE, col, 0.26), r=6)
        c.rect(x0, y0, x0 + 4, y1, col, r=2)
        tw = x1 - x0 - 16
        c.text(x0 + 11, y0 + 5, e["title"] or "(no title)", "bold", 11, maxw=tw)
        if y1 - y0 >= 36:
            detail = f"{e['start']:%H:%M}–{e['end']:%H:%M}" + (f" · {e['location']}" if e.get("location") else "")
            c.text(x0 + 11, y0 + 21, detail, size=9.5, fill=MUTED, maxw=tw)
    # now line
    now = datetime.now()
    if now.date() == day.date() and first_h <= now.hour < last_h:
        y = top + ((now.hour - first_h) + now.minute / 60) * HH
        red = (255, 107, 107)
        c.line(left, y, right, y, red, 2)
        c.circle(left, y, 4, red)
    # legend
    sources = sorted({e["source"] for e in events})
    if len(sources) > 1:
        x = W - 18
        for src in reversed(sources):
            tw = c.d.textlength(src, font=_font("regular", 9.5)) / S
            c.text(x, 26, src, size=9.5, fill=MUTED, anchor="rm")
            c.circle(x - tw - 8, 26, 4, _rgb(colours.get(src, colours["Outlook"])))
            x -= tw + 26
    return c.done()


def _agenda(events, start, days, colours, W):
    by_day = {}
    for e in events:
        by_day.setdefault(e["start"].date(), []).append(e)
    rows = []
    for i in range(days):
        d = (start + timedelta(days=i)).date()
        evs = sorted(by_day.get(d, []), key=lambda e: (not e["all_day"], e["start"]))
        rows.append((d, evs))
    H = 58 + sum(30 + max(1, len(evs)) * 24 + 8 for _, evs in rows) + 8
    c = _Canvas(W, H)
    c.rect(0, 0, W - 1, H - 1, _rgb(SURFACE), r=14)
    total = sum(len(e) for _, e in rows)
    c.text(18, 16, f"Next {days} days", "bold", 15)
    c.text(18, 40, f"{total} event{'s' if total != 1 else ''}", size=10.5, fill=MUTED)
    y = 66
    today = datetime.now().date()
    for d, evs in rows:
        label = "Today" if d == today else ("Tomorrow" if d == today + timedelta(days=1) else d.strftime("%A"))
        c.text(18, y + 10, label, "bold", 11.5, fill=TEXT if evs else MUTED)
        c.text(W - 18, y + 10, d.strftime("%d %b"), size=10, fill=FAINT, anchor="ra")
        y += 30
        if not evs:
            c.text(30, y + 2, "Free", size=10.5, fill=FAINT)
            y += 24
        for e in evs:
            col = _rgb(colours.get(e["source"], colours["Outlook"]))
            c.circle(24, y + 9, 4, col)
            t = "All day" if e["all_day"] else f"{e['start']:%H:%M}"
            c.text(36, y + 2, t, size=10.5, fill=MUTED)
            title = e["title"] + (f" · {e['location']}" if e.get("location") else "")
            c.text(98, y + 2, title, size=10.5, maxw=W - 98 - 18)
            y += 24
        y += 8
        c.line(18, y - 4, W - 18, y - 4, _mix(SURFACE, FAINT, 0.35))
    return c.done()


# ---------- weather ----------
def _icon(c, cx, cy, kind, r=14):
    sun, cloud, rain = (255, 196, 87), (200, 208, 222), (110, 170, 255)
    if kind in ("sun", "partly"):
        c.circle(cx - (5 if kind == "partly" else 0), cy - (4 if kind == "partly" else 0), r * 0.62, sun)
    if kind in ("cloud", "partly", "rain", "snow", "storm"):
        c.circle(cx - 6, cy + 2, r * 0.5, cloud)
        c.circle(cx + 4, cy - 2, r * 0.62, cloud)
        c.circle(cx + 12, cy + 4, r * 0.42, cloud)
        c.rect(cx - 13, cy + 2, cx + 17, cy + 10, cloud, r=4)
    if kind in ("rain", "storm"):
        for dx in (-6, 2, 10):
            c.line(cx + dx, cy + 14, cx + dx - 3, cy + 21, rain, 2)
    if kind == "snow":
        for dx in (-6, 2, 10):
            c.circle(cx + dx, cy + 17, 2, (235, 240, 250))


def _kind(code):
    if code in (0, 1):
        return "sun"
    if code == 2:
        return "partly"
    if code in (3, 45, 48):
        return "cloud"
    if code in (71, 73, 75, 77, 85, 86):
        return "snow"
    if code in (95, 96, 99):
        return "storm"
    return "rain"


def weather_card(w, palette, width=440):
    """w: dict from extras.weather_data()."""
    W = width
    days = w["days"]
    H = 154 + (len(days) - 1) * 28 + (8 if len(days) > 1 else 0)
    c = _Canvas(W, H)
    c.rect(0, 0, W - 1, H - 1, _rgb(SURFACE), r=14)
    t = days[0]
    c.text(18, 16, w["place"], "bold", 13, maxw=W - 120)
    c.text(18, 36, f"{t['date']:%A} · {t['words']}", size=10.5, fill=MUTED)
    _icon(c, W - 48, 34, _kind(t["code"]), 18)
    c.text(18, 58, f"{t['max']}°", "light", 30)
    tw = c.d.textlength(f"{t['max']}°", font=_font("light", 30)) / S
    c.text(24 + tw, 78, f"low {t['min']}°", size=10.5, fill=MUTED)
    c.text(W - 18, 64, f"Rain {t['rain_pct']}%", size=10.5, fill=TEXT, anchor="ra")
    c.text(W - 18, 80, f"Wind {t['wind']} mph, gusts {t['gust']}", size=10, fill=MUTED, anchor="ra")
    # working-day rain strip, 07:00–18:00
    x0, x1, y0 = 18, W - 18, 112
    c.text(x0, y0 - 6, "Chance of rain, 07:00 to 18:00", size=8.5, fill=FAINT, anchor="ls")
    hours = w.get("hours", [])
    bw = (x1 - x0) / max(1, len(hours))
    blue = (110, 170, 255)
    for i, (h, p) in enumerate(hours):
        bx = x0 + i * bw
        c.rect(bx + 1, y0, bx + bw - 1, y0 + 14, _mix(SURFACE_HI, blue, min(1, (p or 0) / 100) * 0.9), r=3)
        if h % 3 == 0:
            c.text(bx + 1, y0 + 26, f"{h:02d}", size=8.5, fill=FAINT)
    y = 154
    for d in days[1:]:
        c.line(18, y - 6, W - 18, y - 6, _mix(SURFACE, FAINT, 0.35))
        c.text(18, y + 2, f"{d['date']:%a}", "bold", 10.5)
        _icon(c, 74, y + 8, _kind(d["code"]), 9)
        c.text(96, y + 2, d["words"], size=10, fill=MUTED, maxw=150)
        c.text(W - 18, y + 2, f"{d['min']}° / {d['max']}°  ·  rain {d['rain_pct']}%", size=10, anchor="ra")
        y += 28
    return c.done()
