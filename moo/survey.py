"""CCTV drain surveys: watch the line videos, code what's seen (MSCC5), write the report and price the repairs.

How it works:
  1. Each video is sampled: a still every few seconds, plus a still whenever the camera stops
     (operators pause at defects, so those are the frames that matter).
  2. The stills go to Claude's vision model in batches, which reads the on-screen header and distance
     counter and codes the observations to MSCC5.
  3. Duplicates are merged, each defect keeps its clearest photo, and repairs are worked out per line
     from rules you'd recognise (patch, reline, dig and replace, root cut, jet, descale).
  4. Out come a client-facing survey report (PDF, no prices) and an internal cost breakdown (Excel)
     priced from your rate card, and the defects are marked on the drawing if there is one.
The coding is a first pass for you to check: Moo says so on the report.
"""
import base64
import io
import json
import logging
import math
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

from PIL import Image, ImageDraw

from .config import DATA_DIR, read_json, write_json

log = logging.getLogger("moo")
STORE = DATA_DIR / "surveys"
RATES_FILE = DATA_DIR / "rates.json"
VIDEO_EXT = {".mp4", ".avi", ".mov", ".mkv", ".wmv", ".mpg", ".mpeg", ".m4v", ".asf", ".ts", ".mts", ".3gp", ".vob"}
BATCH = 16

# Starter rates (ex VAT). Change them to yours: "Moo, patch liners are 420 each".
DEFAULT_RATES = {
    "jet": {"label": "High-pressure water jet line clear", "unit": "line", "rate": 185},
    "descale": {"label": "Robotic descaling of scale and encrustation", "unit": "line", "rate": 450},
    "root_cut": {"label": "Robotic root cutting, then jet clear", "unit": "line", "rate": 375},
    "patch_100": {"label": "Patch liner (local repair), 100mm", "unit": "each", "rate": 345},
    "patch_150": {"label": "Patch liner (local repair), 150mm", "unit": "each", "rate": 395},
    "patch_225": {"label": "Patch liner (local repair), 225mm", "unit": "each", "rate": 595},
    "reinstate": {"label": "Robotic lateral reinstatement / connection repair", "unit": "each", "rate": 225},
    "line_setup": {"label": "Full-length CIPP lining: set-up and mobilisation", "unit": "line", "rate": 650},
    "line_m": {"label": "Full-length CIPP lining", "unit": "m", "rate": 85},
    "excavate": {"label": "Excavate, replace pipe section and reinstate (up to 1.5 m deep)", "unit": "each", "rate": 1450},
    "post_cctv": {"label": "Post-works CCTV survey and report", "unit": "job", "rate": 295},
}

PROMPT = """You're an experienced CCTV drainage surveyor coding to MSCC5 (WRc Manual of Sewer Condition Classification, 5th edition).
These are stills, in time order, from ONE CCTV survey video of a single drain line{hint}. Each still is labelled with its frame number and video time.
Read the on-screen text: the survey header (start and finish manhole, direction, pipe size, material, location) and the running distance counter in metres on each still.
Record every observation you can actually see: structural defects (cracks CC/CL/CM, fractures FC/FL/FM, broken B, hole H, deformed D, collapse X, displaced joints JDM/JDL, open joints OJM/OJL, infiltration ID/IR/IG), service defects (roots RF/RM/RT, deposits DES/DER/DEG/DEE (encrustation) /DEF, obstacles OB), connections (CN, CX defective), and survey details (start/finish node, change of material MC, change of size, water level WL if significant, survey abandoned SA).
Record each defect ONCE, at the still where it's clearest, even if it shows in several stills. Never invent a defect you can't see; if it's unclear, give a low confidence.
Reply with JSON only, no prose:
{{"header": {{"start": "MH01", "finish": "MH02", "direction": "downstream", "diameter_mm": 150, "material": "VC", "location": ""}},
 "observations": [{{"frame": 3, "distance_m": 4.2, "code": "FL", "description": "Longitudinal fracture at 12 o'clock", "clock": "12", "grade": 3, "kind": "structural", "confidence": 0.8}}],
 "end": {{"distance_m": 23.4, "abandoned": false, "reason": ""}}}}
kind is structural, service, connection or info. grade is the MSCC5 grade 1 (minor) to 5 (worst); 0 for info and sound connections.
Use null for anything not shown (e.g. header on a middle batch, distance if no counter is visible)."""


# ---------------------------------------------------------------- rates
def rates():
    saved = read_json(RATES_FILE, None)
    out = json.loads(json.dumps(DEFAULT_RATES))
    for k, v in (saved or {}).get("items", {}).items():
        out.setdefault(k, {"label": k.replace("_", " "), "unit": "each", "rate": 0}).update(v)
    return out, bool(saved)


def set_rates(changes):
    saved = read_json(RATES_FILE, {"items": {}})
    for k, v in (changes or {}).items():
        item = saved["items"].setdefault(k, {})
        if isinstance(v, dict):
            item.update(v)
        else:
            item["rate"] = float(v)
    write_json(RATES_FILE, saved)
    return rates()[0]


# ---------------------------------------------------------------- helpers
def slug(text):
    return re.sub(r"[^a-z0-9]+", "-", (text or "survey").lower()).strip("-")[:60] or "survey"


def node_key(name):
    """MH01, mh 1 and MH-1 are the same chamber."""
    s = re.sub(r"[^A-Z0-9]", "", str(name or "").upper())
    return re.sub(r"0*(\d+)", r"\1", s)


def ids_from_name(filename):
    """'MH01-MH02.mp4' -> ('MH01', 'MH02')."""
    found = re.findall(r"\b([A-Za-z]{1,4})\s*[-_ ]?\s*(\d{1,3})(?![\d.])", Path(filename).stem)
    found = [f"{a.upper()}{b.zfill(2)}" for a, b in found]
    return (found[0], found[1]) if len(found) >= 2 else (None, None)


def videos_in(paths):
    out = []
    for p in paths or []:
        p = Path(str(p).strip().strip('"'))
        if p.is_dir():
            out += sorted(x for x in p.rglob("*") if x.suffix.lower() in VIDEO_EXT)
        elif p.suffix.lower() in VIDEO_EXT and p.exists():
            out.append(p)
    return out


def _jpeg(img, size=896, q=80):
    img = img.convert("RGB")
    img.thumbnail((size, size))
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=q)
    return buf.getvalue()


def _clock(t):
    return f"{int(t // 60)}:{int(t % 60):02d}"


# ---------------------------------------------------------------- 1. stills
def sample_frames(path, max_frames=150):
    """Stills worth looking at: one every few seconds, plus one each time the camera stops."""
    import av
    import numpy as np
    picks = []
    with av.open(str(path)) as c:
        vs = c.streams.video[0]
        vs.thread_type = "AUTO"
        dur = float(vs.duration * vs.time_base) if vs.duration else (c.duration / 1e6 if c.duration else 0)
        every = max(3.0, dur / (max_frames * 0.7)) if dur else 3.0
        from collections import deque
        recent, still_since, still_taken, last = deque(), None, False, -1e9
        for f in c.decode(vs):
            t = float(f.time or 0.0)
            small = f.reformat(width=64, height=36, format="gray").to_ndarray().astype(np.int16)
            # compare with a second ago: a crawler creeping along barely changes frame to frame
            while recent and t - recent[0][0] > 1.0:
                recent.popleft()
            motion = float(np.abs(small - recent[0][1]).mean()) if recent and t - recent[0][0] > 0.6 else 99.0
            recent.append((t, small))
            if motion < 2.5:
                still_since = t if still_since is None else still_since
            else:
                still_since, still_taken = None, False
            why = None
            if t - last >= every:
                why = "interval"
            elif still_since is not None and not still_taken and t - still_since >= 1.2 and t - last >= 1.0:
                why = "pause"
            if why:
                picks.append({"t": t, "jpg": _jpeg(f.to_image()), "why": why})
                last = t
                if why == "pause":
                    still_taken = True
    if len(picks) > max_frames:  # keep every pause, thin the routine ones evenly
        pauses = [p for p in picks if p["why"] == "pause"][:max_frames]
        rest = [p for p in picks if p["why"] != "pause"]
        room = max(0, max_frames - len(pauses))
        step = len(rest) / room if room else 0
        keep = [rest[int(i * step)] for i in range(room)] if room else []
        picks = sorted(pauses + keep, key=lambda p: p["t"])
    return picks, dur


# ---------------------------------------------------------------- 2. look
def _parse(text):
    m = re.search(r"\{.*\}", text or "", re.S)
    if not m:
        return {}
    try:
        return json.loads(m.group(0))
    except json.JSONDecodeError:
        return {}


def look(client, model, frames, hint="", usage=None):
    """Send the stills to the vision model in batches; returns (header, observations, end)."""
    batches = [list(range(i, min(i + BATCH, len(frames)))) for i in range(0, len(frames), BATCH)]

    def run(idx):
        content = []
        for i in idx:
            content.append({"type": "text", "text": f"Frame {i} at {_clock(frames[i]['t'])}"})
            content.append({"type": "image", "source": {"type": "base64", "media_type": "image/jpeg",
                                                         "data": base64.b64encode(frames[i]["jpg"]).decode()}})
        content.append({"type": "text", "text": "Code these stills. JSON only."})
        for attempt in range(3):
            try:
                r = client.messages.create(model=model, max_tokens=3000,
                                           system=PROMPT.format(hint=f" ({hint})" if hint else ""),
                                           messages=[{"role": "user", "content": content}])
                if usage:
                    usage.add(model, r.usage.input_tokens, r.usage.output_tokens)
                return _parse("".join(b.text for b in r.content if getattr(b, "type", "") == "text"))
            except Exception as e:
                log.warning("Survey batch failed (%s), attempt %d", e, attempt + 1)
                time.sleep(2 + attempt * 3)
        return {}

    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(run, batches))
    header, end, obs = {}, {}, []
    for res in results:
        for k, v in (res.get("header") or {}).items():
            if v not in (None, "") and not header.get(k):
                header[k] = v
        if res.get("end"):
            end = {**end, **{k: v for k, v in res["end"].items() if v not in (None, "")}}
        for o in res.get("observations") or []:
            try:
                o["frame"] = int(o.get("frame"))
            except (TypeError, ValueError):
                continue
            if 0 <= o["frame"] < len(frames):
                obs.append(o)
    return header, obs, end


def merge(obs, frames):
    """One record per defect: same code within half a metre (or eight seconds) is the same thing."""
    def pos(o):
        d = o.get("distance_m")
        return (0, float(d)) if isinstance(d, (int, float)) else (1, frames[o["frame"]]["t"])
    out = []
    for o in sorted(obs, key=lambda o: frames[o["frame"]]["t"]):
        code = str(o.get("code") or "").upper().strip()
        o["code"] = code
        o["grade"] = int(o.get("grade") or 0) if str(o.get("grade") or "0").isdigit() else 0
        twin = None
        for p in out[-6:]:
            if p["code"] == code:
                a, b = pos(p), pos(o)
                if a[0] == b[0] and abs(a[1] - b[1]) <= (0.5 if a[0] == 0 else 8):
                    twin = p
        if twin:
            if (o.get("confidence") or 0) > (twin.get("confidence") or 0):
                twin.update({k: o[k] for k in ("frame", "description", "clock", "confidence") if o.get(k)})
            twin["grade"] = max(twin["grade"], o["grade"])
        else:
            out.append(o)
    return out


# ---------------------------------------------------------------- 3. what to do about it
STRUCT = ("CC", "CL", "CM", "CS", "FC", "FL", "FM", "FS", "B", "H", "D", "X", "JD", "OJ", "I", "SW", "MB")


def category(o):
    c, desc = o["code"], (o.get("description") or "").lower()
    if c.startswith(("DE", "OB", "R", "E")) or any(w in desc for w in ("root", "deposit", "grease", "debris", "scale", "encrust", "silt")):
        return "service"
    if c.startswith(("CX",)) or ("connection" in desc and "defect" in desc):
        return "connection"
    if c.startswith(STRUCT) and not c.startswith(("CN",)):
        return "structural"
    return o.get("kind") if o.get("kind") in ("structural", "service") else "info"


def works(line, rate_card):
    """The repairs a line needs, as priced items. Each item says which defects it deals with."""
    dia = int(line.get("diameter_mm") or 150)
    patch_key = "patch_100" if dia <= 110 else "patch_150" if dia <= 160 else "patch_225"
    items, patches, digs = [], [], []
    service = [d for d in line["defects"] if category(d) == "service" and d["grade"] >= 2]
    conns = [d for d in line["defects"] if category(d) == "connection"]
    for d in line["defects"]:
        if category(d) != "structural":
            continue
        desc = (d.get("description") or "").lower()
        if d["code"].startswith("X") or "collapse" in desc or (d["code"].startswith("D") and d["grade"] >= 4) \
                or d["grade"] >= 5:
            digs.append(d)
        elif d["grade"] >= 3 or d["code"].startswith("I"):
            patches.append(d)
    # patches close together share one liner
    groups = []
    for d in sorted(patches, key=lambda d: float(d.get("distance_m") or 0)):
        if groups and isinstance(d.get("distance_m"), (int, float)) and isinstance(groups[-1][-1].get("distance_m"), (int, float)) \
                and float(d["distance_m"]) - float(groups[-1][-1]["distance_m"]) <= 0.6:
            groups[-1].append(d)
        else:
            groups.append([d])

    def add(key, qty, why):
        r = rate_card[key]
        items.append({"key": key, "label": r["label"], "qty": qty, "unit": r["unit"], "rate": float(r["rate"]),
                      "total": round(qty * float(r["rate"]), 2), "why": why})

    def at(ds):
        return ", ".join(f"{d['code']} at {d['distance_m']:.1f} m" if isinstance(d.get("distance_m"), (int, float))
                         else d["code"] for d in ds)

    roots = [d for d in service if d["code"].startswith("R") or "root" in (d.get("description") or "").lower()]
    scale = [d for d in service if d["code"].startswith(("DEE", "E")) or any(
        w in (d.get("description") or "").lower() for w in ("scale", "encrust"))]
    if roots:
        add("root_cut", 1, at(roots))
    if scale:
        add("descale", 1, at(scale))
    if service or groups or conns:
        add("jet", 1, at(service) or "clean before repairs")
    length = float(line.get("length_m") or 0)
    patch_cost = len(groups) * float(rate_card[patch_key]["rate"])
    line_cost = float(rate_card["line_setup"]["rate"]) + length * float(rate_card["line_m"]["rate"])
    if len(groups) >= 3 and length and line_cost <= patch_cost * 1.15:
        add("line_setup", 1, f"{len(groups)} defects along the line: a full liner is better value than patches")
        add("line_m", round(length, 1), at(patches))
    elif groups:
        add(patch_key, len(groups), at(patches))
    if conns:
        add("reinstate", len(conns), at(conns))
    if digs:
        add("excavate", len(digs), at(digs))
    return items


# ---------------------------------------------------------------- 4. run a whole survey
def analyse(paths, client, model, site="", title="", usage=None, progress=None):
    vids = videos_in(paths)
    if not vids:
        raise RuntimeError("I couldn't find any videos there.")
    name = slug(f"{site or title or vids[0].parent.name}")
    folder = STORE / name
    (folder / "photos").mkdir(parents=True, exist_ok=True)
    lines = []
    for n, v in enumerate(vids, 1):
        if progress:
            progress(f"Watching {v.name} ({n} of {len(vids)})")
        try:
            frames, dur = sample_frames(v)
        except Exception as e:
            lines.append({"file": v.name, "name": v.stem, "error": f"couldn't read the video: {e}", "defects": []})
            continue
        a, b = ids_from_name(v.name)
        header, obs, end = look(client, model, frames, hint=f"file {v.name}", usage=usage)
        defects = merge(obs, frames)
        start, finish = a or header.get("start"), b or header.get("finish")
        dists = [float(d["distance_m"]) for d in defects if isinstance(d.get("distance_m"), (int, float))]
        length = end.get("distance_m") if isinstance(end.get("distance_m"), (int, float)) else (max(dists) if dists else None)
        key = slug(f"{start}-{finish}" if start and finish else v.stem)
        keep = []
        for i, d in enumerate(defects, 1):
            d["category"] = category(d)
            if d["category"] == "info" and d["code"] not in ("SA", "MC", "WL"):
                continue
            photo = folder / "photos" / f"{key}-{i:02d}.jpg"
            photo.write_bytes(frames[d["frame"]]["jpg"])
            d.update({"photo": str(photo), "time": _clock(frames[d["frame"]]["t"])})
            keep.append(d)
        lines.append({"file": v.name, "name": f"{start} to {finish}" if start and finish else v.stem,
                      "start": start, "finish": finish, "direction": header.get("direction"),
                      "diameter_mm": header.get("diameter_mm"), "material": header.get("material"),
                      "location": header.get("location"), "length_m": length, "video_minutes": round(dur / 60, 1),
                      "abandoned": bool(end.get("abandoned")), "abandon_reason": end.get("reason") or "",
                      "defects": keep})
    survey = {"name": name, "site": site, "title": title or "CCTV drainage survey", "date": datetime.now().strftime("%d/%m/%Y"),
              "lines": lines, "extras": [], "exclude": []}
    price(survey)
    save(survey)
    return survey


def price(survey):
    card, _ = rates()
    total = 0.0
    for ln in survey["lines"]:
        ln["works"] = [w for w in works(ln, card) if f"{ln['name']}|{w['key']}" not in survey.get("exclude", [])] \
            if not ln.get("error") else []
        total += sum(w["total"] for w in ln["works"])
    extras = []
    if any(ln["works"] for ln in survey["lines"]) and "post_cctv" not in survey.get("exclude", []):
        r = card["post_cctv"]
        extras.append({"key": "post_cctv", "label": r["label"], "qty": 1, "unit": r["unit"], "rate": float(r["rate"]),
                       "total": float(r["rate"]), "why": "prove the repairs"})
    for x in survey.get("extras") or []:
        extras.append({"key": "extra", "label": x["label"], "qty": float(x.get("qty", 1)), "unit": x.get("unit", "each"),
                       "rate": float(x.get("rate", 0)), "total": round(float(x.get("qty", 1)) * float(x.get("rate", 0)), 2),
                       "why": x.get("why", "")})
    survey["job_items"] = extras
    survey["subtotal"] = round(total + sum(x["total"] for x in extras), 2)
    return survey


def save(survey):
    folder = STORE / survey["name"]
    folder.mkdir(parents=True, exist_ok=True)
    write_json(folder / "survey.json", survey)


def load(name=None):
    if not STORE.exists():
        return None
    if not name:
        found = sorted(STORE.glob("*/survey.json"), key=lambda p: p.stat().st_mtime)
        return read_json(found[-1], None) if found else None
    p = STORE / slug(name) / "survey.json"
    if not p.exists():
        hits = [x for x in STORE.glob("*/survey.json") if slug(name) in x.parent.name]
        p = hits[-1] if hits else p
    return read_json(p, None) if p.exists() else None


def listing():
    return [(p.parent.name, read_json(p, {}).get("date", "")) for p in sorted(STORE.glob("*/survey.json"))] \
        if STORE.exists() else []


def summary(survey, detail=True):
    out = [f"Survey '{survey['title']}' at {survey.get('site') or '?'}: {len(survey['lines'])} lines."]
    for ln in survey["lines"]:
        if ln.get("error"):
            out.append(f"- {ln['file']}: {ln['error']}")
            continue
        worst = max([d["grade"] for d in ln["defects"] if d["category"] == "structural"] or [0])
        serv = max([d["grade"] for d in ln["defects"] if d["category"] == "service"] or [0])
        out.append(f"- {ln['name']} ({ln.get('diameter_mm') or '?'}mm {ln.get('material') or ''}, "
                   f"{ln['length_m'] or '?'} m{', ABANDONED: ' + ln['abandon_reason'] if ln.get('abandoned') else ''}): "
                   f"structural grade {worst}, service grade {serv}, {len(ln['defects'])} records")
        if detail:
            for d in ln["defects"]:
                where = f"{d['distance_m']:.1f} m" if isinstance(d.get("distance_m"), (int, float)) else d.get("time")
                out.append(f"    {where} {d['code']} gr{d['grade']} {d.get('description', '')} "
                           f"(confidence {d.get('confidence', '?')})")
            for w in ln["works"]:
                out.append(f"    WORKS: {w['label']} x{w['qty']} {w['unit']} @ £{w['rate']:.0f} = £{w['total']:.2f} [{w['why']}]")
    for x in survey.get("job_items") or []:
        out.append(f"- JOB: {x['label']} x{x["qty"]:g} @ £{x['rate']:.0f} = £{x['total']:.2f}")
    out.append(f"Subtotal ex VAT: £{survey['subtotal']:,.2f}")
    return "\n".join(out)


# ---------------------------------------------------------------- 5. paperwork
def output_dir(survey):
    from .drawings import output_dir as dd
    d = dd().parent / "Moo Surveys" / survey["name"]
    d.mkdir(parents=True, exist_ok=True)
    return d


def _font(size, bold=False):
    from .drawings import _font as f
    return f(size, bold)


def report_pages(survey):
    """A4 portrait pages at 150 dpi: summary, then a page or more per line with photos. No prices."""
    W, H, M = 1240, 1754, 70
    ink, grey, line_col, red, amber = (25, 30, 40), (110, 118, 130), (210, 214, 220), (200, 50, 40), (215, 140, 20)
    pages = []
    f_t, f_h, f_b, f_s, f_bb = _font(40, True), _font(26, True), _font(20), _font(17), _font(20, True)

    def new(sub):
        img = Image.new("RGB", (W, H), "white")
        d = ImageDraw.Draw(img)
        d.text((M, M), survey["title"], font=f_t, fill=ink)
        d.text((M, M + 54), f"{survey.get('site') or ''}   ·   {survey['date']}   ·   {sub}", font=f_s, fill=grey)
        d.line([(M, M + 90), (W - M, M + 90)], fill=ink, width=3)
        d.text((M, H - M + 10), "MOOLANDS LTD  ·  Defects coded to MSCC5 from the survey video. Checked by: ________",
               font=f_s, fill=grey)
        d.text((W - M, H - M + 10), f"Page {len(pages) + 1}", font=f_s, fill=grey, anchor="ra")
        pages.append(img)
        return img, d, M + 120

    def grade_col(g):
        return red if g >= 4 else amber if g == 3 else ink

    img, d, y = new("Summary")
    d.text((M, y), "Lines surveyed", font=f_h, fill=ink)
    y += 50
    cols = [M, M + 260, M + 470, M + 590, M + 720, M + 860]
    for x, h in zip(cols, ("Line", "Size / material", "Length", "Structural", "Service", "Recommended")):
        d.text((x, y), h, font=f_bb, fill=grey)
    y += 36
    for ln in survey["lines"]:
        if y > H - 260:
            img, d, y = new("Summary (cont.)")
        if ln.get("error"):
            d.text((cols[0], y), ln["file"][:24], font=f_b, fill=ink)
            d.text((cols[1], y), "Video unreadable", font=f_b, fill=red)
            y += 40
            continue
        st = max([x["grade"] for x in ln["defects"] if x["category"] == "structural"] or [0])
        sv = max([x["grade"] for x in ln["defects"] if x["category"] == "service"] or [0])
        rec = "; ".join(sorted({w["label"].split(",")[0].split("(")[0].strip() for w in ln["works"]})) or "No action"
        d.text((cols[0], y), ln["name"][:22], font=f_bb, fill=ink)
        d.text((cols[1], y), f"{ln.get('diameter_mm') or '?'}mm {ln.get('material') or ''}"[:16], font=f_b, fill=ink)
        d.text((cols[2], y), f"{ln['length_m']:.1f} m" if isinstance(ln.get("length_m"), (int, float)) else "?", font=f_b, fill=ink)
        d.text((cols[3], y), f"Grade {st}", font=f_bb, fill=grade_col(st))
        d.text((cols[4], y), f"Grade {sv}", font=f_bb, fill=grade_col(sv))
        from .drawings import _wrap as _wrap_text
        rows = _wrap_text(d, rec, f_s, W - M - cols[5])
        for i, r in enumerate(rows[:3]):
            d.text((cols[5], y + 2 + i * 22), r, font=f_s, fill=ink)
        y += max(44, 22 * min(3, len(rows)) + 18)
        d.line([(M, y - 8), (W - M, y - 8)], fill=line_col, width=1)
    y += 30
    d.text((M, y), "Grades: MSCC5, 1 (minor) to 5 (worst). Structural grade 4-5 needs repair soon; 3 should be planned.",
           font=f_s, fill=grey)

    for ln in survey["lines"]:
        if ln.get("error"):
            continue
        img, d, y = new(ln["name"])
        d.text((M, y), ln["name"], font=f_h, fill=ink)
        meta = (f"{ln.get('diameter_mm') or '?'}mm {ln.get('material') or ''} · "
                f"{ln['length_m']:.1f} m surveyed" if isinstance(ln.get("length_m"), (int, float)) else "")
        meta += f" · {ln.get('direction') or ''} · video {ln['file']}"
        d.text((M, y + 38), meta, font=f_s, fill=grey)
        if ln.get("abandoned"):
            d.text((M, y + 62), f"Survey abandoned: {ln.get('abandon_reason') or 'reason not given'}", font=f_bb, fill=red)
        y += 100
        if not ln["defects"]:
            d.text((M, y), "No defects recorded.", font=f_b, fill=ink)
        for x in ln["defects"]:
            if y > H - 330:
                img, d, y = new(f"{ln['name']} (cont.)")
            try:
                ph = Image.open(x["photo"]).convert("RGB")
                ph.thumbnail((360, 210))
                img.paste(ph, (M, y))
                ph_h = ph.height
            except Exception:
                ph_h = 40
            tx = M + 390
            where = f"{x['distance_m']:.1f} m" if isinstance(x.get("distance_m"), (int, float)) else f"at {x.get('time')}"
            d.text((tx, y), f"{where}   {x['code']}", font=f_h, fill=ink)
            g = x["grade"]
            d.text((W - M, y), f"Grade {g}" if g else x["category"].title(), font=f_bb, fill=grade_col(g), anchor="ra")
            from .drawings import _wrap as _wrap_text
            for i, r in enumerate(_wrap_text(d, x.get("description") or "", f_b, W - M - tx)[:4]):
                d.text((tx, y + 40 + i * 26), r, font=f_b, fill=ink)
            d.text((tx, y + ph_h - 22), f"{x['category'].title()} · clock {x.get('clock') or '-'} · video {x.get('time')}",
                   font=f_s, fill=grey)
            y += max(ph_h, 150) + 26
            d.line([(M, y - 13), (W - M, y - 13)], fill=line_col, width=1)
        if ln["works"]:
            if y > H - 260:
                img, d, y = new(f"{ln['name']} (cont.)")
            d.text((M, y), "Recommended works", font=f_bb, fill=ink)
            y += 34
            for w in ln["works"]:
                qty = f"{w['qty']:g} {w['unit']}" if w["unit"] == "m" else f"x{w['qty']:g}"
                d.text((M + 20, y), f"• {w['label']} ({qty})", font=f_b, fill=ink)
                y += 30
    return pages


def write_report(survey):
    pages = report_pages(survey)
    out = output_dir(survey) / f"{survey['name']} survey report.pdf"
    pages[0].save(out, "PDF", resolution=150, save_all=True, append_images=pages[1:])
    return out, pages[0]


def write_costs(survey):
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill, Alignment
    wb = Workbook()
    ws = wb.active
    ws.title = "Cost breakdown"
    _, set_by_user = rates()
    ws.append([f"{survey['title']} - {survey.get('site') or ''}", "", "", "", "", "", survey["date"]])
    ws["A1"].font = Font(bold=True, size=13)
    if not set_by_user:
        ws.append(["Starter rates: check and change them to yours before this goes out."])
        ws["A2"].font = Font(color="C00000", bold=True)
    ws.append([])
    ws.append(["Line", "Item", "Qty", "Unit", "Rate £", "Total £", "Why"])
    hdr = ws.max_row
    for c in ws[hdr]:
        c.font = Font(bold=True, color="FFFFFF")
        c.fill = PatternFill("solid", fgColor="1F2A37")
    first = hdr + 1
    for ln in survey["lines"]:
        for w in ln["works"]:
            ws.append([ln["name"], w["label"], w["qty"], w["unit"], w["rate"], None, w["why"]])
            ws.cell(ws.max_row, 6, f"=C{ws.max_row}*E{ws.max_row}")
    for x in survey.get("job_items") or []:
        ws.append(["Whole job", x["label"], x["qty"], x["unit"], x["rate"], None, x["why"]])
        ws.cell(ws.max_row, 6, f"=C{ws.max_row}*E{ws.max_row}")
    last = ws.max_row
    ws.append([])
    for label, formula in (("Subtotal", f"=SUM(F{first}:F{last})"), ("VAT 20%", f"=F{ws.max_row + 1}*0.2"),
                           ("Total", f"=F{ws.max_row + 1}+F{ws.max_row + 2}")):
        ws.append(["", label, "", "", "", formula])
        ws.cell(ws.max_row, 2).font = Font(bold=True)
        ws.cell(ws.max_row, 6).font = Font(bold=True)
    for col, w in zip("ABCDEFG", (16, 58, 8, 8, 10, 12, 60)):
        ws.column_dimensions[col].width = w
    for row in ws.iter_rows(min_row=first):
        for c in row[4:6]:
            c.number_format = "£#,##0.00"
        row[6].alignment = Alignment(wrap_text=True, vertical="top")
    out = output_dir(survey) / f"{survey['name']} cost breakdown.xlsx"
    wb.save(out)
    return out


def mark_drawing(survey, drawing_name):
    """Put each line's defects on the matching pipe of a drawing. Returns how many lines matched."""
    from . import drawings as D
    model = D.load(drawing_name)
    if not model:
        return 0, None
    by_key = {}
    for p in model["pipes"]:
        by_key[(node_key(p["from"]), node_key(p["to"]))] = (p, False)
        by_key[(node_key(p["to"]), node_key(p["from"]))] = (p, True)
    hit = 0
    for ln in survey["lines"]:
        found = by_key.get((node_key(ln.get("start")), node_key(ln.get("finish"))))
        if not found:
            continue
        p, reverse = found
        length = float(ln.get("length_m") or 0) or None
        marks = []
        for x in ln["defects"]:
            if x["category"] not in ("structural", "service", "connection") or not x["grade"]:
                continue
            frac = None
            if isinstance(x.get("distance_m"), (int, float)) and length:
                frac = max(0.03, min(0.97, float(x["distance_m"]) / length))
                frac = 1 - frac if reverse else frac
            marks.append({"code": x["code"], "grade": x["grade"], "at": frac, "d": x.get("distance_m")})
        p["defects"] = marks
        if ln.get("diameter_mm") and not p.get("diameter_mm"):
            p["diameter_mm"] = ln["diameter_mm"]
        if ln.get("material") and not p.get("material"):
            p["material"] = ln["material"]
        hit += 1
    if hit:
        D.save(model, drawing_name)
    return hit, model


class SurveyJob:
    """Runs a survey in the background so Moo can carry on talking meanwhile."""
    def __init__(self, brain):
        self.brain = brain
        self.running = None

    def start(self, paths, site="", title="", drawing=None, on_done=None, on_progress=None):
        if self.running and self.running.is_alive():
            return "I'm already watching a survey. I'll tell you when that one's done."
        vids = videos_in(paths)
        if not vids:
            return ("I couldn't find any videos there. Give me the folder they're in (e.g. Documents\\Moo Surveys\\"
                    "Kaizen) or attach them with the paperclip.")
        mins = 0.0
        try:
            import av
            for v in vids:
                with av.open(str(v)) as c:
                    mins += (c.duration or 0) / 60e6
        except Exception:
            pass
        model = self.brain.settings.get("vision_model") or "claude-sonnet-5-5"

        def run():
            try:
                s = analyse(vids, self.brain.client(), model, site=site, title=title, usage=self.brain.usage,
                            progress=on_progress)
                report, first = write_report(s)
                costs = write_costs(s)
                marked, model_d = (0, None)
                if drawing:
                    marked, model_d = mark_drawing(s, drawing)
                s.update({"report": str(report), "costs": str(costs), "drawing": drawing if marked else None})
                save(s)
                out_drawing = None
                if model_d is not None:
                    from . import drawings as D
                    out_drawing = D.export(model_d, drawing)
                if on_done:
                    on_done(s, first, out_drawing, None)
            except Exception as e:
                log.exception("Survey failed")
                if on_done:
                    on_done(None, None, None, str(e))

        self.running = threading.Thread(target=run, daemon=True, name="survey")
        self.running.start()
        est = max(1, round(len(vids) * 0.6 + mins * 0.15))
        cost = max(0.05, mins * 0.12)
        return (f"Started on {len(vids)} video{'s' if len(vids) != 1 else ''}"
                + (f" ({mins:.0f} minutes of footage)" if mins >= 1 else "")
                + f". Roughly {est} minute{'s' if est != 1 else ''} and about ${cost:.2f} of Claude time. "
                "I'll tell you when the report's ready; carry on meanwhile.")
