"""Do the holes of parts that lie on each other line up? Plain geometry in mm — no Inventor needed.

Input is what rules/scan_planar.iLogicVb writes: for every part file its planar faces with
their edge loops (part coordinates), and for the assembly every part occurrence with its
transform. A hole or slot through a sheet shows as an inner loop of the sheet's faces, so
the question becomes: where two faces of different parts lie on each other, what is behind
each opening — an opening of the other part (how far off, what bolt still passes), a stud
or pin of the other part that goes into it, solid material, or nothing.

An inner loop is not always a hole: it also runs around whatever stands on the face (a
locating pin, a welded stud, an embossed form). The rule tells the two apart ("hole").
"""
import math

METRIC = (2, 2.5, 3, 4, 5, 6, 8, 10, 12, 14, 16, 20, 24, 30)
_EPS = 0.02   # mm: two radii / centres this close are the same


# ---------- small vector helpers ----------

def _sub(a, b):
    return (a[0] - b[0], a[1] - b[1], a[2] - b[2])


def _dot(a, b):
    return a[0] * b[0] + a[1] * b[1] + a[2] * b[2]


def _cross(a, b):
    return (a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0])


def _norm(a):
    n = math.sqrt(_dot(a, a)) or 1.0
    return (a[0] / n, a[1] / n, a[2] / n)


def _point(m, p):
    """A part point (mm) in assembly coordinates; m is the 3x4 transform, row by row."""
    return (m[0] * p[0] + m[1] * p[1] + m[2] * p[2] + m[3],
            m[4] * p[0] + m[5] * p[1] + m[6] * p[2] + m[7],
            m[8] * p[0] + m[9] * p[1] + m[10] * p[2] + m[11])


def _vector(m, v):
    return (m[0] * v[0] + m[1] * v[1] + m[2] * v[2],
            m[4] * v[0] + m[5] * v[1] + m[6] * v[2],
            m[8] * v[0] + m[9] * v[1] + m[10] * v[2])


def _basis(n):
    """Two unit vectors that span the plane with normal n."""
    helper = (1.0, 0.0, 0.0) if abs(n[0]) < 0.9 else (0.0, 1.0, 0.0)
    u = _norm(_cross(n, helper))
    return u, _cross(n, u)


def _seg_point(p, a, b) -> float:
    """Distance from p to the segment a-b (3D)."""
    ab, ap = _sub(b, a), _sub(p, a)
    L = _dot(ab, ab)
    t = 0.0 if L < 1e-12 else max(0.0, min(1.0, _dot(ap, ab) / L))
    return math.dist(p, (a[0] + ab[0] * t, a[1] + ab[1] * t, a[2] + ab[2] * t))


def _seg_seg(a, b, c, d) -> float:
    """Distance between two segments that lie in (nearly) one plane."""
    best = min(_seg_point(a, c, d), _seg_point(b, c, d), _seg_point(c, a, b), _seg_point(d, a, b))
    if best < 1e-9:
        return 0.0
    n = _cross(_sub(b, a), _sub(d, c))
    if _dot(n, n) < 1e-12:
        return best
    side = lambda p, q, r: _dot(_cross(_sub(q, p), _sub(r, p)), n)
    if side(a, b, c) * side(a, b, d) < 0 and side(c, d, a) * side(c, d, b) < 0:
        return 0.0
    return best


# ---------- openings of one part face (part coordinates) ----------

def classify_loop(edges: list) -> dict:
    """An inner loop as a circle, a slot or a cut-out of another shape."""
    arcs = [e for e in edges if e["k"] in ("c", "a")]
    lines = [e for e in edges if e["k"] == "l"]
    if len(arcs) == len(edges) and arcs:
        c, r = arcs[0]["c"], arcs[0]["r"]
        if all(math.dist(e["c"], c) < _EPS and abs(e["r"] - r) < _EPS for e in arcs):
            return {"kind": "circle", "c": tuple(c), "r": r}
    if len(arcs) == 2 and len(lines) == 2 and len(edges) == 4 and abs(arcs[0]["r"] - arcs[1]["r"]) < _EPS \
            and math.dist(arcs[0]["c"], arcs[1]["c"]) > _EPS:
        return {"kind": "slot", "c": tuple(arcs[0]["c"]), "c2": tuple(arcs[1]["c"]), "r": arcs[0]["r"]}
    segments = _segments_of(edges)
    return {"kind": "cutout", "c": _interior_point(segments), "segments": segments}


def _interior_point(segments: list) -> tuple:
    """A point inside a closed outline. (Its centroid won't do: for an L or a kidney shape
    that lies in the material.) Cut the outline with a line and take the middle of the
    first stretch that is inside."""
    pts = [p for seg in segments for p in seg]
    origin = pts[0]
    n = (0.0, 0.0, 0.0)
    for a, b in segments:          # the outline's plane, from two edges that are not parallel
        n = _cross(_sub(segments[0][1], segments[0][0]), _sub(b, a))
        if _dot(n, n) > 1e-9:
            break
    if _dot(n, n) <= 1e-9:
        return tuple(sum(p[i] for p in pts) / len(pts) for i in range(3))
    u, v = _basis(_norm(n))
    flat = [((_dot(_sub(a, origin), u), _dot(_sub(a, origin), v)),
             (_dot(_sub(b, origin), u), _dot(_sub(b, origin), v))) for a, b in segments]
    ys = sorted(p[1] for seg in flat for p in seg)
    for share in (0.5, 0.37, 0.63, 0.21, 0.79):   # a height where the line meets no corner
        y = ys[0] + (ys[-1] - ys[0]) * share
        xs = sorted(x1 + (y - y1) * (x2 - x1) / (y2 - y1) for (x1, y1), (x2, y2) in flat if (y1 > y) != (y2 > y))
        if len(xs) >= 2 and xs[1] - xs[0] > 1e-6:
            x = (xs[0] + xs[1]) / 2
            return (origin[0] + u[0] * x + v[0] * y, origin[1] + u[1] * x + v[1] * y, origin[2] + u[2] * x + v[2] * y)
    return tuple(sum(p[i] for p in pts) / len(pts) for i in range(3))


def _segments_of(edges: list) -> list:
    """Every edge of a loop as straight pieces (a full circle becomes a 24-gon only if it
    carries no stroked points — the rule strokes arcs and odd curves itself)."""
    out = []
    for e in edges:
        pts = [tuple(p) for p in e.get("pts", [])]
        out += list(zip(pts, pts[1:]))
    return out


def describe(opening: dict) -> str:
    if opening["kind"] == "circle":
        return f"Ø{opening['r'] * 2:g}"
    if opening["kind"] == "slot":
        return f"slot {opening['r'] * 2:g}x{opening['r'] * 2 + math.dist(opening['c'], opening['c2']):g}"
    return "cut-out"


# ---------- the faces of every occurrence, in assembly coordinates ----------

def _circle_points(c, r, n, count=24):
    u, v = _basis(n)
    return [(c[0] + r * (math.cos(t) * u[0] + math.sin(t) * v[0]),
             c[1] + r * (math.cos(t) * u[1] + math.sin(t) * v[1]),
             c[2] + r * (math.cos(t) * u[2] + math.sin(t) * v[2]))
            for t in (2 * math.pi * k / count for k in range(count + 1))]


def prepare_part(part: dict) -> list:
    """The part's planar faces, ready to be placed: outline segments and classified openings."""
    faces = []
    for f in part.get("faces", []):
        n = _norm(f["n"])
        outline, openings, studs = [], [], []
        for loop in f["loops"]:
            edges = loop["edges"]
            for e in edges:   # a full circle has no stroked points: give it some
                if e["k"] == "c" and not e.get("pts"):
                    e["pts"] = _circle_points(e["c"], e["r"], n)
            if loop["outer"]:
                outline += _segments_of(edges)
            elif loop.get("hole", True):
                openings.append(classify_loop(edges))
            else:                          # the foot of a pin, stud or boss standing on the face
                studs.append(classify_loop(edges))
        if outline:
            faces.append({"p": tuple(f["p"]), "n": n, "area": f.get("area", 0.0),
                          "outline": outline, "openings": openings, "studs": studs})
    return faces


def place(faces: list, m: list, occ: int) -> list:
    """Those faces for one occurrence: assembly coordinates, plane offset, bounding box."""
    out = []
    for f in faces:
        n = _norm(_vector(m, f["n"]))
        outline = [(_point(m, a), _point(m, b)) for a, b in f["outline"]]
        xs = [p for seg in outline for p in seg]
        def placed(o):
            w = {"kind": o["kind"], "c": _point(m, o["c"])}
            if "r" in o:
                w["r"] = o["r"]
            if "c2" in o:
                w["c2"] = _point(m, o["c2"])
            if "segments" in o:
                w["segments"] = [(_point(m, a), _point(m, b)) for a, b in o["segments"]]
            return w
        openings = [placed(o) for o in f["openings"]]
        out.append({"occ": occ, "n": n, "d": _dot(n, _point(m, f["p"])), "outline": outline, "openings": openings,
                    "studs": [placed(o) for o in f.get("studs", [])],
                    "lo": tuple(min(p[i] for p in xs) for i in range(3)),
                    "hi": tuple(max(p[i] for p in xs) for i in range(3))})
    return out


# ---------- point in a face ----------

def _flat(face):
    """The face's outline in its own plane (computed once)."""
    if "flat" not in face:
        u, v = _basis(face["n"])
        face["uv"] = (u, v)
        face["flat"] = [((_dot(a, u), _dot(a, v)), (_dot(b, u), _dot(b, v))) for a, b in face["outline"]]
    return face["flat"]


def _inside(point2, segments2) -> bool:
    """Even-odd rule: works on any set of closed loops, in any order."""
    x, y = point2
    inside = False
    for (x1, y1), (x2, y2) in segments2:
        if (y1 > y) != (y2 > y) and x < x1 + (y - y1) * (x2 - x1) / (y2 - y1):
            inside = not inside
    return inside


def covers(face, point) -> bool:
    """Does the face's outline (holes not counted) contain this assembly point?"""
    if any(point[i] < face["lo"][i] - _EPS or point[i] > face["hi"][i] + _EPS for i in range(3)):
        return False
    flat = _flat(face)
    u, v = face["uv"]
    return _inside((_dot(point, u), _dot(point, v)), flat)


# ---------- one opening against the openings of the face behind it ----------

def _axis(o):
    """The segment an opening's bolt centre can sit on: a point for a circle."""
    return (o["c"], o.get("c2", o["c"]))


def _offset(a, b):
    """How far the two openings are off each other (0 = a bolt can sit centred in both)."""
    (a1, a2), (b1, b2) = _axis(a), _axis(b)
    if a1 == a2 and b1 == b2:
        return math.dist(a1, b1)
    if a1 == a2:
        return _seg_point(a1, b1, b2)
    if b1 == b2:
        return _seg_point(b1, a1, a2)
    return _seg_seg(a1, a2, b1, b2)


def largest_bolt(a, b, offset: float) -> float:
    """Diameter of the largest pin through both openings (0 = none passes)."""
    return max(0.0, min(2 * a["r"], 2 * b["r"], a["r"] + b["r"] - offset))


def metric(diameter: float) -> str:
    fits = [m for m in METRIC if m <= diameter + 1e-6]
    return f"M{fits[-1]:g}" if fits else "nothing"


def _moved(opening, n, distance):
    """The opening shifted along n — onto the plane of the face it is compared with."""
    if abs(distance) < 1e-9:
        return opening
    shift = lambda p: (p[0] + n[0] * distance, p[1] + n[1] * distance, p[2] + n[2] * distance)
    out = dict(opening, c=shift(opening["c"]))
    if "c2" in opening:
        out["c2"] = shift(opening["c2"])
    if "segments" in opening:
        out["segments"] = [(shift(a), shift(b)) for a, b in opening["segments"]]
    return out


def _behind(opening, face):
    """What the face has where this opening is: ('stud', its stud, offset),
    ('opening', its opening, offset), ('solid',) or ('nothing',)."""
    flat, (u, v) = _flat(face), face["uv"]
    for stud in face.get("studs", ()):
        if "r" in stud and "r" in opening:
            e = _offset(opening, stud)
            if e < opening["r"] + stud["r"]:
                return ("stud", stud, e)
        else:   # a foot or a hole of another shape: does one hold the inner point of the other?
            probe, shape = (opening, stud) if "segments" in stud else (stud, opening)
            seg2 = [((_dot(a, u), _dot(a, v)), (_dot(b, u), _dot(b, v))) for a, b in shape["segments"]]
            if _inside((_dot(probe["c"], u), _dot(probe["c"], v)), seg2):
                return ("stud", stud, 0.0)
    best = None
    for other in face["openings"]:
        if other["kind"] == "cutout" or opening["kind"] == "cutout":
            probe, shape = (opening, other) if other["kind"] == "cutout" else (other, opening)
            seg2 = [((_dot(a, u), _dot(a, v)), (_dot(b, u), _dot(b, v))) for a, b in shape["segments"]]
            if _inside((_dot(probe["c"], u), _dot(probe["c"], v)), seg2):
                return ("opening", other, 0.0)
            continue
        e = _offset(opening, other)
        # Two openings belong together when they are off by less than the smaller one is wide.
        # Further apart they only graze each other (a Ø10 hole at the rim of a Ø30 one): no
        # bolt was meant to go through both, and what is behind the centre decides instead.
        if e < 2 * min(opening["r"], other["r"]) and (best is None or e < best[2]):
            best = ("opening", other, e)
    if best:
        return best
    return ("solid",) if covers(face, opening["c"]) else ("nothing",)


# ---------- the whole assembly ----------

def _direction_key(n):
    """The same key for a normal and its opposite."""
    for c in n:
        if abs(c) > 1e-3:
            s = 1.0 if c > 0 else -1.0
            return (round(n[0] * s, 2) + 0.0, round(n[1] * s, 2) + 0.0, round(n[2] * s, 2) + 0.0), s
    return (0.0, 0.0, 0.0), 1.0


def check(occurrences: list, parts: dict, max_gap: float = 0.5, tolerance: float = 0.1) -> dict:
    """occurrences: [{"path", "file", "m"}]; parts: {file: scan of that part}.

    Faces of two different occurrences lie on each other when they face each other and are
    at most max_gap mm apart. For each opening in such a face: aligned (off by at most
    tolerance mm), misaligned, engaged (a stud or pin of the other part goes into it, or
    does not fit), blocked (solid material of the other part behind it) or open (the other
    part does not reach there)."""
    prepared = {}
    faces = []
    for i, occ in enumerate(occurrences):
        part = parts.get(occ["file"])
        if part is None:
            continue
        if occ["file"] not in prepared:
            prepared[occ["file"]] = prepare_part(part)
        faces += place(prepared[occ["file"]], occ["m"], i)

    step = max(max_gap, 0.25) * 2
    buckets = {}
    for f in faces:
        key, s = _direction_key(f["n"])
        f["key"], f["level"] = key, f["d"] * s
        buckets.setdefault((key, math.floor(f["level"] / step)), []).append(f)

    aligned, misaligned, blocked, opened, engaged, misfit = {}, {}, [], [], [], []
    rank = {"stud": 3, "opening": 2, "solid": 1, "nothing": 0}
    contact_pairs = set()
    for f in faces:
        if not f["openings"]:
            continue
        cell = math.floor(f["level"] / step)
        partners = []
        for k in (cell - 1, cell, cell + 1):
            for g in buckets.get((f["key"], k), ()):
                if g["occ"] == f["occ"] or _dot(f["n"], g["n"]) > -0.999:
                    continue
                gap = -g["d"] - f["d"]        # along f's normal, from f's surface to g's
                if abs(gap) > max_gap + 1e-6:
                    continue
                reach = max_gap + _EPS   # the two faces may be that far apart along the normal
                if any(f["lo"][i] > g["hi"][i] + reach or g["lo"][i] > f["hi"][i] + reach for i in range(3)):
                    continue
                partners.append((g, gap))
        if not partners:
            continue
        for g, _ in partners:
            contact_pairs.add((min(f["occ"], g["occ"]), max(f["occ"], g["occ"])))
        for o in f["openings"]:
            found = None
            for g, gap in partners:
                what = _behind(_moved(o, f["n"], gap), g)   # the opening, seen on g's plane
                if found is None or rank[what[0]] > rank[found[0]] \
                        or (what[0] == found[0] == "opening" and what[2] < found[2]):
                    found = what + (g, gap)
            a = occurrences[f["occ"]]["path"]
            at = [round(c, 2) for c in o["c"]]
            axis = [round(c, 3) + 0.0 for c in f["n"]]   # out of this part, towards the other one
            if found[0] == "stud":
                stud, offset, g = found[1], found[2], found[3]
                row = {"part": a, "opening": describe(o), "at": at, "axis": axis,
                       "stud_of": occurrences[g["occ"]]["path"], "stud": describe(stud),
                       "offset_mm": round(offset, 3)}
                if "r" in o and "r" in stud:   # room around the stud, at its tightest
                    row["clearance_mm"] = round(o["r"] - stud["r"] - offset, 3)
                (misfit if row.get("clearance_mm", 0) < -1e-6 else engaged).append(row)
            elif found[0] == "opening":
                other, offset, g = found[1], found[2], found[3]
                b = occurrences[g["occ"]]["path"]
                first = f["occ"] < g["occ"]
                key = (min(f["occ"], g["occ"]), max(f["occ"], g["occ"]),
                       tuple(round((x + y) / 2, 1) for x, y in zip(o["c"], other["c"])))
                # where the bolt goes: the round hole's centre when one of the two is a slot
                spot = o if o["kind"] == "circle" or other["kind"] != "circle" else other
                at = [round(c, 2) for c in spot["c"]]
                row = {"a": a if first else b, "b": b if first else a,
                       "a_opening": describe(o if first else other), "b_opening": describe(other if first else o),
                       "at": at, "axis": axis if first else [-c + 0.0 for c in axis],
                       "offset_mm": round(offset, 3)}
                if "r" in o and "r" in other:
                    d = largest_bolt(o, other, offset)
                    row["largest_bolt_mm"] = round(d, 2)
                    row["passes"] = metric(d)
                (misaligned if offset > tolerance + 1e-9 else aligned).setdefault(key, row)
            elif found[0] == "solid":
                blocked.append({"part": a, "opening": describe(o), "at": at, "axis": axis,
                                "behind": occurrences[found[1]["occ"]]["path"]})
            else:
                opened.append({"part": a, "opening": describe(o), "at": at, "axis": axis,
                               "touching": occurrences[partners[0][0]["occ"]]["path"]})
    worst = sorted(misaligned.values(), key=lambda r: -r["offset_mm"])
    return {"parts": len({f["occ"] for f in faces}), "planar_faces": len(faces),
            "pairs_of_parts_in_contact": len(contact_pairs),
            "aligned": len(aligned), "misaligned": worst, "blocked": blocked, "open": opened,
            "engaged_rows": engaged, "stud_does_not_fit": sorted(misfit, key=lambda r: r["clearance_mm"]),
            "aligned_rows": list(aligned.values())}
