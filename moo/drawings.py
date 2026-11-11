"""Drawing workbench: drainage layouts as proper CAD plans.

Moo reads a sketch or photo into a small structured model (chambers, pipes, building outlines,
notes), then this module draws it:
  - DXF (opens in AutoCAD, DraftSight, LibreCAD, BricsCAD...) on proper layers, in metres
  - PDF (A3 landscape) with a title block, legend, north arrow and scale bar
  - PNG preview for the chat and the phone app
and checks the design against the usual Approved Document H rules of thumb.

Every change is a new version, so nothing is ever lost.
"""
import json
import math
import re
from datetime import datetime
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from .config import DATA_DIR

STORE = DATA_DIR / "drawings"
SYSTEM_COLOURS = {"foul": (176, 92, 40), "surface": (37, 99, 235), "combined": (124, 58, 237)}
DXF_COLOURS = {"foul": 30, "surface": 5, "combined": 6}  # AutoCAD colour index: orange, blue, magenta
NODE_TYPES = {"manhole": "MH", "inspection_chamber": "IC", "rodding_eye": "RE", "gully": "G", "rwp": "RWP",
              "svp": "SVP", "soakaway": "SA", "interceptor": "INT", "connection": "CONN", "outfall": "OUT",
              "pump": "PS", "other": ""}


def _slug(name):
    return re.sub(r"[^a-z0-9]+", "-", (name or "drawing").lower()).strip("-")[:50] or "drawing"


def output_dir():
    try:
        from .app import documents_dir
        d = documents_dir() / "Moo Drawings"
    except Exception:
        d = Path.home() / "Documents" / "Moo Drawings"
    d.mkdir(parents=True, exist_ok=True)
    return d


# ---------- the model ----------
def blank(title="Drainage layout"):
    return {"title": title, "site": "", "client": "", "drawing_no": "", "drawn_by": "Moo", "units": "m",
            "nodes": [], "pipes": [], "outlines": [], "labels": [], "notes": [], "version": 0}


def normalise(model):
    m = blank()
    m.update({k: v for k, v in (model or {}).items() if v is not None})
    for n in m["nodes"]:
        n["id"] = str(n.get("id") or "").strip() or f"N{m['nodes'].index(n) + 1}"
        n["type"] = n.get("type") if n.get("type") in NODE_TYPES else "manhole"
        n["x"], n["y"] = float(n.get("x") or 0), float(n.get("y") or 0)
    ids = {n["id"] for n in m["nodes"]}
    m["pipes"] = [p for p in m["pipes"] if p.get("from") in ids and p.get("to") in ids]
    for i, p in enumerate(m["pipes"]):
        p["id"] = str(p.get("id") or f"P{i + 1}")
        p["system"] = p.get("system") if p.get("system") in SYSTEM_COLOURS else "foul"
    return m


def apply_changes(model, changes):
    """Upsert nodes/pipes/outlines by id, remove ids, move nodes, set title fields."""
    m = json.loads(json.dumps(model))
    for key in ("title", "site", "client", "drawing_no", "drawn_by"):
        if changes.get(key) is not None:
            m[key] = changes[key]
    for coll in ("nodes", "pipes", "outlines"):
        for item in changes.get(coll) or []:
            iid = str(item.get("id") or item.get("label") or "")
            existing = next((x for x in m[coll] if str(x.get("id") or x.get("label")) == iid), None)
            if existing:
                existing.update({k: v for k, v in item.items() if v is not None})
            else:
                m[coll].append(item)
    for mv in changes.get("move") or []:  # {"id": "MH2", "dx": 3, "dy": 0}
        n = next((x for x in m["nodes"] if x["id"] == mv.get("id")), None)
        if n:
            n["x"] = float(n["x"]) + float(mv.get("dx") or 0)
            n["y"] = float(n["y"]) + float(mv.get("dy") or 0)
    for rid in changes.get("remove") or []:
        m["nodes"] = [n for n in m["nodes"] if n["id"] != rid]
        m["pipes"] = [p for p in m["pipes"] if p["id"] != rid and p.get("from") != rid and p.get("to") != rid]
        m["outlines"] = [o for o in m["outlines"] if o.get("label") != rid]
    if changes.get("notes") is not None:
        m["notes"] = list(changes["notes"])
    if changes.get("add_notes"):
        m["notes"] = list(m.get("notes") or []) + list(changes["add_notes"])
    return normalise(m)


def _node(m, nid):
    return next((n for n in m["nodes"] if n["id"] == nid), None)


def pipe_figures(m, p):
    """Length (from the drawing if not given) and gradient (from invert levels if given)."""
    a, b = _node(m, p["from"]), _node(m, p["to"])
    length = p.get("length_m") or (math.hypot(b["x"] - a["x"], b["y"] - a["y"]) if a and b else None)
    grad = p.get("gradient")
    fall = None
    up = p.get("upstream_il", a.get("invert_level") if a else None)
    down = p.get("downstream_il", b.get("invert_level") if b else None)
    if up is not None and down is not None and length:
        fall = float(up) - float(down)
        if not grad and fall > 0:
            grad = f"1:{round(length / fall)}"
    return length, grad, fall


def _ratio(grad):
    try:
        return float(str(grad).split(":")[1]) if ":" in str(grad) else None
    except (ValueError, IndexError):
        return None


def check(m) -> list:
    """Design checks: rules of thumb from Approved Document H. Advisory, for a designer to confirm."""
    out = []
    for p in m["pipes"]:
        length, grad, fall = pipe_figures(m, p)
        dia = int(p.get("diameter_mm") or 0)
        r = _ratio(grad)
        if fall is not None and fall <= 0:
            out.append(f"{p['id']} ({p['from']} to {p['to']}) has no fall or runs backwards on the levels given "
                       f"(fall {fall:.3f} m). Check the invert levels.")
        elif r and p.get("system") == "surface":
            if r > 100:
                out.append(f"{p['id']}: surface water at {grad} is flatter than 1:100, the usual minimum for 75 and "
                           "100mm drains (Approved Document H, H3).")
        elif r:
            if dia and dia <= 100 and r > 80:
                out.append(f"{p['id']}: {dia}mm at {grad} is flatter than 1:80. Approved Document H allows 1:80 "
                           "for 100mm only with at least one WC connected; 1:40 is the usual target.")
            if dia >= 150 and r > 150:
                out.append(f"{p['id']}: {dia}mm at {grad} is flatter than 1:150 (needs at least one WC "
                           "connected to go that flat).")
            if r < 10:
                out.append(f"{p['id']}: {grad} is very steep; check velocity and whether a backdrop is needed.")
        elif p.get("system") != "surface":
            out.append(f"{p['id']}: no gradient or invert levels given.")
        if length and length > 45 and not any(_node(m, x) and _node(m, x)["type"] == "manhole"
                                               for x in (p["from"], p["to"])):
            out.append(f"{p['id']} runs {length:.1f} m between access fittings: over 45 m needs a manhole or "
                       "extra access (Approved Document H, Table 13).")
        if not dia:
            out.append(f"{p['id']}: pipe size not given.")
    for n in m["nodes"]:
        cl, il = n.get("cover_level"), n.get("invert_level")
        if cl is not None and il is not None:
            depth = float(cl) - float(il)
            dia = max([int(p.get("diameter_mm") or 100) for p in m["pipes"] if n["id"] in (p["from"], p["to"])] or [100])
            cover = depth - dia / 1000
            if cover < 0.6:
                out.append(f"{n['id']}: about {cover:.2f} m of cover over the pipe ({depth:.2f} m to invert). Pipes "
                           "generally need 0.6 m in gardens and 0.9 m under drives and roads, or protection.")
            if n["type"] == "inspection_chamber" and depth > 1.0:
                out.append(f"{n['id']}: {depth:.2f} m deep. Inspection chambers are normally limited to about "
                           "1.0 m; deeper needs a manhole.")
    return out


# ---------- versions ----------
def save(model, name=None) -> str:
    name = name or _slug(model.get("title"))
    folder = STORE / name
    folder.mkdir(parents=True, exist_ok=True)
    versions = sorted(int(p.stem[1:]) for p in folder.glob("v*.json") if p.stem[1:].isdigit())
    v = (versions[-1] + 1) if versions else 1
    model["version"] = v
    (folder / f"v{v}.json").write_text(json.dumps(model, indent=1), encoding="utf-8")
    return name


def load(name, version=None):
    folder = STORE / name
    versions = sorted(int(p.stem[1:]) for p in folder.glob("v*.json") if p.stem[1:].isdigit())
    if not versions:
        return None
    v = version if version in versions else versions[-1]
    return json.loads((folder / f"v{v}.json").read_text(encoding="utf-8"))


def listing() -> list:
    if not STORE.exists():
        return []
    out = []
    for d in sorted(STORE.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True):
        m = load(d.name)
        if m:
            out.append((d.name, m.get("title"), m.get("version"), datetime.fromtimestamp(d.stat().st_mtime)))
    return out


# ---------- drawing ----------
def _bounds(m):
    pts = [(n["x"], n["y"]) for n in m["nodes"]]
    for o in m["outlines"]:
        pts += [tuple(map(float, p)) for p in o.get("points") or []]
    pts += [(float(l.get("x", 0)), float(l.get("y", 0))) for l in m.get("labels") or []]
    if not pts:
        return 0, 0, 10, 10
    xs, ys = [p[0] for p in pts], [p[1] for p in pts]
    return min(xs), min(ys), max(xs), max(ys)


def _node_label(n):
    bits = [n["id"]]
    if n.get("cover_level") is not None:
        bits.append(f"CL {float(n['cover_level']):.2f}")
    if n.get("invert_level") is not None:
        bits.append(f"IL {float(n['invert_level']):.2f}")
    if n.get("size"):
        bits.append(str(n["size"]))
    return bits


def _pipe_label(m, p):
    length, grad, _ = pipe_figures(m, p)
    bits = [p["id"]]
    if p.get("diameter_mm"):
        bits.append(f"{int(p['diameter_mm'])}mm")
    if p.get("material"):
        bits.append(str(p["material"]))
    if grad:
        bits.append(str(grad))
    if length:
        bits.append(f"{length:.1f}m")
    return " ".join(bits)


def to_dxf(m, path):
    import ezdxf
    from ezdxf.enums import TextEntityAlignment
    doc = ezdxf.new("R2010", setup=True)
    doc.units = ezdxf.units.M
    for name, col in (("BUILDING", 8), ("FOUL", 30), ("SURFACE", 5), ("COMBINED", 6), ("CHAMBERS", 7),
                      ("TEXT", 7), ("NOTES", 7), ("TITLE", 7)):
        if name not in doc.layers:
            doc.layers.add(name, color=col)
    msp = doc.modelspace()
    for o in m["outlines"]:
        pts = [tuple(map(float, p)) for p in o.get("points") or []]
        if len(pts) >= 2:
            msp.add_lwpolyline(pts, close=bool(o.get("closed", True)), dxfattribs={"layer": "BUILDING"})
            if o.get("label"):
                cx, cy = sum(p[0] for p in pts) / len(pts), sum(p[1] for p in pts) / len(pts)
                msp.add_text(o["label"], height=0.5, dxfattribs={"layer": "TEXT"}).set_placement(
                    (cx, cy), align=TextEntityAlignment.MIDDLE_CENTER)
    for p in m["pipes"]:
        a, b = _node(m, p["from"]), _node(m, p["to"])
        layer = p["system"].upper()
        msp.add_line((a["x"], a["y"]), (b["x"], b["y"]), dxfattribs={"layer": layer})
        ang = math.atan2(b["y"] - a["y"], b["x"] - a["x"])
        mx, my = (a["x"] + b["x"]) / 2, (a["y"] + b["y"]) / 2
        # flow arrow
        for side in (2.6, -2.6):
            msp.add_line((mx, my), (mx - 0.6 * math.cos(ang - side / 6), my - 0.6 * math.sin(ang - side / 6)),
                         dxfattribs={"layer": layer})
        rot = math.degrees(ang)
        if rot > 90 or rot < -90:
            rot += 180
        msp.add_text(_pipe_label(m, p), height=0.25, rotation=rot, dxfattribs={"layer": "TEXT"}).set_placement(
            (mx - 0.4 * math.sin(ang), my + 0.4 * math.cos(ang)), align=TextEntityAlignment.BOTTOM_CENTER)
    for n in m["nodes"]:
        r = 0.45 if n["type"] in ("manhole", "soakaway", "interceptor", "pump") else 0.3
        if n["type"] in ("inspection_chamber", "gully"):
            msp.add_lwpolyline([(n["x"] - r, n["y"] - r), (n["x"] + r, n["y"] - r), (n["x"] + r, n["y"] + r),
                                (n["x"] - r, n["y"] + r)], close=True, dxfattribs={"layer": "CHAMBERS"})
        else:
            msp.add_circle((n["x"], n["y"]), r, dxfattribs={"layer": "CHAMBERS"})
        for i, line in enumerate(_node_label(n)):
            msp.add_text(line, height=0.25, dxfattribs={"layer": "TEXT"}).set_placement(
                (n["x"] + r + 0.2, n["y"] + 0.15 - i * 0.35), align=TextEntityAlignment.LEFT)
    for l in m.get("labels") or []:
        msp.add_text(str(l.get("text", "")), height=float(l.get("size", 0.3)), dxfattribs={"layer": "TEXT"}
                     ).set_placement((float(l.get("x", 0)), float(l.get("y", 0))), align=TextEntityAlignment.LEFT)
    x0, y0, x1, y1 = _bounds(m)
    for i, note in enumerate(m.get("notes") or []):
        msp.add_text(f"{i + 1}. {note}", height=0.25, dxfattribs={"layer": "NOTES"}).set_placement(
            (x0, y0 - 2 - i * 0.45), align=TextEntityAlignment.LEFT)
    msp.add_text(f"{m['title']}  |  {m.get('site', '')}  |  Rev {m.get('version', 1)}  |  "
                 f"{datetime.now():%d/%m/%Y}  |  Drawn: {m.get('drawn_by', 'Moo')}", height=0.4,
                 dxfattribs={"layer": "TITLE"}).set_placement((x0, y1 + 2), align=TextEntityAlignment.LEFT)
    doc.saveas(str(path))


def _font(size, bold=False):
    names = (["segoeuib.ttf", "arialbd.ttf", "DejaVuSans-Bold.ttf"] if bold else
             ["segoeui.ttf", "arial.ttf", "DejaVuSans.ttf"])
    for n in names:
        try:
            return ImageFont.truetype(n, size)
        except OSError:
            continue
    return ImageFont.load_default()


def render(m, width=3508, height=2480):
    """A3 landscape at 300 dpi: plan on the left, legend/notes/title block on the right."""
    S = width / 3508
    img = Image.new("RGB", (width, height), "white")
    d = ImageDraw.Draw(img)
    ink, grey = (25, 30, 40), (120, 128, 140)
    pad = int(90 * S)
    side_w = int(900 * S)
    d.rectangle([pad // 2, pad // 2, width - pad // 2, height - pad // 2], outline=ink, width=max(2, int(5 * S)))
    plan = (pad, pad, width - side_w - pad, height - pad)
    d.line([(plan[2] + pad // 2, pad // 2), (plan[2] + pad // 2, height - pad // 2)], fill=ink, width=max(2, int(3 * S)))
    # fit plan
    x0, y0, x1, y1 = _bounds(m)
    span_x, span_y = max(x1 - x0, 1), max(y1 - y0, 1)
    k = min((plan[2] - plan[0] - 400 * S) / span_x, (plan[3] - plan[1] - 300 * S) / span_y)
    ox = plan[0] + (plan[2] - plan[0] - span_x * k) / 2
    oy = plan[3] - (plan[3] - plan[1] - span_y * k) / 2

    def P(x, y):
        return ox + (x - x0) * k, oy - (y - y0) * k

    f_small, f_mid, f_big = _font(int(30 * S)), _font(int(38 * S)), _font(int(60 * S), True)
    taken = []  # boxes already used by text, so node labels steer round them
    for o in m["outlines"]:
        pts = [P(*map(float, p)) for p in o.get("points") or []]
        if len(pts) >= 2:
            d.line(pts + ([pts[0]] if o.get("closed", True) else []), fill=grey, width=max(2, int(6 * S)))
            if o.get("label"):
                xs, ys = [p[0] for p in pts], [p[1] for p in pts]
                big = (max(xs) - min(xs)) * (max(ys) - min(ys)) > 0.35 * (plan[2] - plan[0]) * (plan[3] - plan[1])
                if big:  # site boundary and the like: label in the corner, out of the way
                    d.text((min(xs) + 20 * S, min(ys) + 14 * S), o["label"].upper(), fill=grey, font=f_small)
                else:
                    cx, cy = sum(xs) / len(xs), sum(ys) / len(ys)
                    d.text((cx, cy), o["label"], fill=grey, font=f_mid, anchor="mm")
                    tw = d.textlength(o["label"], font=f_mid)
                    taken.append((cx - tw / 2, cy - 22 * S, cx + tw / 2, cy + 22 * S))
    for p in m["pipes"]:
        a, b = _node(m, p["from"]), _node(m, p["to"])
        col = SYSTEM_COLOURS[p["system"]]
        (ax, ay), (bx, by) = P(a["x"], a["y"]), P(b["x"], b["y"])
        d.line([(ax, ay), (bx, by)], fill=col, width=max(3, int(8 * S)))
        ang = math.atan2(by - ay, bx - ax)
        mx, my = (ax + bx) / 2, (ay + by) / 2
        L = 34 * S
        d.polygon([(mx + L * math.cos(ang), my + L * math.sin(ang)),
                   (mx + L * math.cos(ang + 2.5), my + L * math.sin(ang + 2.5)),
                   (mx + L * math.cos(ang - 2.5), my + L * math.sin(ang - 2.5))], fill=col)
        seg = math.hypot(bx - ax, by - ay)
        label = _pipe_label(m, p)
        bare = not (p.get("diameter_mm") or p.get("material") or pipe_figures(m, p)[1])
        if bare and "connection" in (a["type"], b["type"]):
            continue  # plain branch: the BR label on the node says it all, keep the plan readable
        if d.textlength(label, font=f_small) > seg * 0.9:
            label = p["id"]  # short run: just its name, details are in the DXF and the checks
        if d.textlength(label, font=f_small) > seg * 0.6:
            continue
        tw = d.textlength(label, font=f_small)
        txt = Image.new("RGBA", (int(tw + 20), int(46 * S)), (255, 255, 255, 0))
        ImageDraw.Draw(txt).text((10, 4), label, fill=col, font=f_small)
        deg = -math.degrees(ang)
        if deg > 90 or deg < -90:
            deg += 180
        rt = txt.rotate(deg, expand=True, resample=Image.BICUBIC)
        off = 34 * S
        img.paste(rt, (int(mx - rt.width / 2 + off * math.sin(ang)), int(my - rt.height / 2 - off * math.cos(ang))), rt)
    placed = taken
    for n in m["nodes"]:  # every symbol counts as taken space before any label goes down
        x, y = P(n["x"], n["y"])
        rr = 28 * S
        placed.append((x - rr, y - rr, x + rr, y + rr))
    for n in m["nodes"]:
        x, y = P(n["x"], n["y"])
        r = (26 if n["type"] in ("manhole", "soakaway", "interceptor", "pump") else 18) * S
        if n["type"] in ("inspection_chamber", "gully", "rwp"):
            d.rectangle([x - r, y - r, x + r, y + r], fill="white", outline=ink, width=max(2, int(5 * S)))
        else:
            d.ellipse([x - r, y - r, x + r, y + r], fill="white", outline=ink, width=max(2, int(5 * S)))
        lines = _node_label(n)
        fonts = [_font(int(32 * S), i == 0) for i in range(len(lines))]
        w = max(d.textlength(t, font=f) for t, f in zip(lines, fonts))
        h = len(lines) * 36 * S
        # which way do the pipes leave this node? put the label where they don't go
        dirs = []
        for p in m["pipes"]:
            other = p["to"] if p["from"] == n["id"] else p["from"] if p["to"] == n["id"] else None
            if other:
                o = _node(m, other)
                ox_, oy_ = P(o["x"], o["y"])
                dirs.append(math.atan2(oy_ - y, ox_ - x))
        g = 10 * S
        spots = [(x + r + g, y - r), (x - r - g - w, y - r), (x - w / 2, y - r - g - h), (x - w / 2, y + r + g),
                 (x + r + g, y - h - r / 2), (x + r + g, y + r / 2), (x - r - g - w, y - h - r / 2),
                 (x - r - g - w, y + r / 2)]

        def score(sp):
            bx0, by0, bx1, by1 = sp[0], sp[1], sp[0] + w, sp[1] + h
            hit = sum(1 for q in placed if not (bx1 < q[0] or bx0 > q[2] or by1 < q[1] or by0 > q[3])) * 10
            cx, cy = (bx0 + bx1) / 2 - x, (by0 + by1) / 2 - y
            ang = math.atan2(cy, cx)
            clash = sum(max(0, 1 - abs(math.atan2(math.sin(ang - t), math.cos(ang - t))) / 0.9) for t in dirs)
            return hit + clash * 3
        sx, sy = min(spots, key=score)
        placed.append((sx, sy, sx + w, sy + h))
        for i, (line, f) in enumerate(zip(lines, fonts)):
            d.text((sx, sy + i * 36 * S), line, fill=ink if i == 0 else grey, font=f)
    for l in m.get("labels") or []:
        d.text(P(float(l.get("x", 0)), float(l.get("y", 0))), str(l.get("text", "")), fill=grey, font=f_mid)
    # north arrow + scale bar
    nx, ny = plan[2] - 90 * S, plan[1] + 110 * S
    d.polygon([(nx, ny - 70 * S), (nx - 30 * S, ny + 30 * S), (nx, ny + 10 * S), (nx + 30 * S, ny + 30 * S)], fill=ink)
    d.text((nx, ny - 80 * S), "N", fill=ink, font=f_big, anchor="ms")
    step = next(s for s in (1, 2, 5, 10, 20, 50, 100) if s * k >= 180 * S)
    sx, sy = plan[0] + 20 * S, plan[3] - 40 * S
    for i in range(4):
        d.rectangle([sx + i * step * k, sy, sx + (i + 1) * step * k, sy + 16 * S], outline=ink,
                    fill=ink if i % 2 == 0 else "white")
    for i in range(5):
        d.text((sx + i * step * k, sy - 8 * S), f"{i * step}", fill=ink, font=f_small, anchor="ms")
    d.text((sx + 4 * step * k + 16 * S, sy + 16 * S), "m", fill=ink, font=f_small, anchor="ls")
    # side panel: legend, notes, checks, title block
    sx0, y = plan[2] + pad, pad
    d.text((sx0, y), "LEGEND", fill=ink, font=_font(int(40 * S), True))
    y += 70 * S
    for sysname, col in SYSTEM_COLOURS.items():
        if any(p["system"] == sysname for p in m["pipes"]):
            d.line([(sx0, y + 18 * S), (sx0 + 90 * S, y + 18 * S)], fill=col, width=max(3, int(8 * S)))
            d.text((sx0 + 110 * S, y), f"{sysname.title()} water drain", fill=ink, font=f_mid)
            y += 55 * S
    d.ellipse([sx0 + 25 * S, y, sx0 + 65 * S, y + 40 * S], outline=ink, width=max(2, int(4 * S)))
    d.text((sx0 + 110 * S, y), "Manhole / chamber", fill=ink, font=f_mid)
    y += 55 * S
    d.rectangle([sx0 + 28 * S, y + 3 * S, sx0 + 62 * S, y + 37 * S], outline=ink, width=max(2, int(4 * S)))
    d.text((sx0 + 110 * S, y), "Inspection chamber / gully", fill=ink, font=f_mid)
    y += 55 * S
    d.text((sx0, y), "CL cover level  ·  IL invert level (m)", fill=grey, font=f_small)
    y += 80 * S
    notes = list(m.get("notes") or [])
    if notes:
        d.text((sx0, y), "NOTES", fill=ink, font=_font(int(40 * S), True))
        y += 60 * S
        for i, note in enumerate(notes[:12]):
            for j, line in enumerate(_wrap(d, f"{i + 1}. {note}", f_small, side_w - pad)):
                d.text((sx0 + (0 if j == 0 else 30 * S), y), line, fill=ink, font=f_small)
                y += 38 * S
            y += 8 * S
    # title block
    tb_top = height - pad // 2 - int(520 * S)
    d.line([(plan[2] + pad // 2, tb_top), (width - pad // 2, tb_top)], fill=ink, width=max(2, int(3 * S)))
    ty = tb_top + 30 * S
    d.text((sx0, ty), "MOOLANDS LTD", fill=ink, font=_font(int(52 * S), True))
    ty += 80 * S
    for label, value in (("Drawing", m.get("title")), ("Site", m.get("site") or "-"), ("Client", m.get("client") or "-"),
                         ("Drawing no.", m.get("drawing_no") or "-"), ("Revision", str(m.get("version") or 1)),
                         ("Date", datetime.now().strftime("%d/%m/%Y")), ("Drawn", m.get("drawn_by") or "Moo"),
                         ("Scale", "NTS: verify on site")):
        d.text((sx0, ty), label.upper(), fill=grey, font=f_small)
        d.text((sx0 + 230 * S, ty), str(value)[:40], fill=ink, font=f_small)
        ty += 44 * S
    return img


def _wrap(d, text, font, width):
    words, lines, line = text.split(), [], ""
    for w in words:
        trial = f"{line} {w}".strip()
        if d.textlength(trial, font=font) <= width or not line:
            line = trial
        else:
            lines.append(line)
            line = w
    lines.append(line)
    return lines


def export(m, name) -> dict:
    """Write DXF + PDF + PNG for this version; returns the file paths."""
    folder = output_dir()
    base = folder / f"{name} rev{m.get('version', 1)}"
    sheet = render(m)
    png, pdf, dxf = base.with_suffix(".png"), base.with_suffix(".pdf"), base.with_suffix(".dxf")
    sheet.save(png)
    sheet.save(pdf, "PDF", resolution=300)
    to_dxf(m, dxf)
    return {"png": png, "pdf": pdf, "dxf": dxf, "image": sheet}
