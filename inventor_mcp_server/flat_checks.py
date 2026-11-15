"""Flat pattern checks: plain 2D geometry in mm — no Inventor needed."""
import math


def _seg_point_dist(p, a, b) -> float:
    ax, ay, bx, by = a[0], a[1], b[0], b[1]
    dx, dy = bx - ax, by - ay
    L = dx * dx + dy * dy
    t = 0.0 if L < 1e-18 else max(0.0, min(1.0, ((p[0] - ax) * dx + (p[1] - ay) * dy) / L))
    return math.hypot(p[0] - ax - t * dx, p[1] - ay - t * dy)


def _segs_cross(a, b, c, d) -> bool:
    def side(p, q, r):
        return (q[0] - p[0]) * (r[1] - p[1]) - (q[1] - p[1]) * (r[0] - p[0])
    return side(a, b, c) * side(a, b, d) < 0 and side(c, d, a) * side(c, d, b) < 0


def _seg_seg_dist(a, b, c, d) -> float:
    if _segs_cross(a, b, c, d):
        return 0.0
    return min(_seg_point_dist(a, c, d), _seg_point_dist(b, c, d),
               _seg_point_dist(c, a, b), _seg_point_dist(d, a, b))


def _ray_hit(p, n, segments) -> float:
    """Distance from p along the unit direction n to the nearest of the segments (inf: none)."""
    best = math.inf
    for a, b in segments:
        ex, ey = b[0] - a[0], b[1] - a[1]
        den = n[0] * ey - n[1] * ex
        if abs(den) < 1e-12:
            continue
        wx, wy = a[0] - p[0], a[1] - p[1]
        t = (wx * ey - wy * ex) / den
        u = (wx * n[1] - wy * n[0]) / den
        if t > 1e-6 and -1e-9 <= u <= 1 + 1e-9:
            best = min(best, t)
    return best


def _segments(polyline) -> list:
    return list(zip(polyline, polyline[1:]))


def flat_pattern_problems(outer: list, holes: list, bends: list, thickness: float, v_die: float,
                          min_flange: float = 0, min_hole_to_bend: float = 0) -> dict:
    """What a press brake can't do with this flat pattern.

    outer: the contour, a list of polylines [(x, y), …] (one per edge); holes: [{"points":
    polyline, "diameter": mm or None}]; bends: [{"a": (x, y), "b": (x, y), "radius": mm, …}]
    — the bend centre lines. All in mm.

    The sheet lies across a V die while it is bent, so on both sides of the bend line it has
    to reach past the die's shoulder: at least V/2 + thickness of flat sheet (min_flange),
    measured in the flat pattern from the bend line to the edge of the part or to the next
    bend. A hole closer to the bend line than V/2 (or radius + 2 x thickness, if that is
    more — min_hole_to_bend) is over the opening and gets pulled out of shape.
    """
    limit = min_flange or v_die / 2 + thickness
    contour = [s for pl in outer for s in _segments(pl)]
    problems, legs = [], []
    for i, b in enumerate(bends, 1):
        a, c = b["a"], b["b"]
        length = math.dist(a, c)
        if length < 1e-9:
            continue
        ux, uy = (c[0] - a[0]) / length, (c[1] - a[1]) / length
        others = [(o["a"], o["b"]) for j, o in enumerate(bends, 1) if j != i]
        sides = []
        for n in ((-uy, ux), (uy, -ux)):
            reach, to_bend = 0.0, False
            for f in (0.1, 0.3, 0.5, 0.7, 0.9):
                p = (a[0] + ux * length * f, a[1] + uy * length * f)
                edge, bend = _ray_hit(p, n, contour), _ray_hit(p, n, others)
                d = min(edge, bend)
                if d != math.inf and d > reach:
                    reach, to_bend = d, bend < edge
            sides.append((reach, to_bend))
        legs.append({"bend": i, "length_mm": round(length, 2),
                     "legs_mm": [round(s[0], 2) for s in sides],
                     "to": ["next bend" if s[1] else "edge" for s in sides]})
        for reach, to_bend in sides:
            if reach < limit - 1e-6:
                problems.append(
                    f"Bend {i}: only {reach:.1f} mm of sheet to the {'next bend' if to_bend else 'edge'} — "
                    f"needs {limit:.1f} mm to reach across a V{v_die:g} die")
        hole_limit = min_hole_to_bend or max(v_die / 2, b.get("radius", 0) + 2 * thickness)
        for k, h in enumerate(holes, 1):
            d = min(_seg_seg_dist(p, q, a, c) for p, q in _segments(h["points"]))
            what = f"Ø{h['diameter']:g} hole" if h.get("diameter") else "cut-out"
            if d == 0:
                problems.append(f"Bend {i}: {what} {k} lies across the bend line — fine as a relief "
                                "window, otherwise move it")
            elif d < hole_limit - 1e-6:
                problems.append(f"Bend {i}: {what} {k} is {d:.1f} mm from the bend line — closer than "
                                f"{hole_limit:.1f} mm it is pulled out of shape; move it or cut it after bending")
    for k, h in enumerate(holes, 1):
        if h.get("diameter") and h["diameter"] < thickness - 1e-6:
            problems.append(f"Ø{h['diameter']:g} hole {k} is smaller than the {thickness:g} mm sheet — "
                            "too small to punch, and a laser leaves it rough")
    return {"min_flange_mm": round(limit, 2), "bends": legs, "problems": problems}


def rule_from_name(name: str):
    """(thickness mm, V die mm) from a shop rule named TXX-VYY (T15-V10 = 1.5 mm on a V10 die)."""
    import re
    m = re.fullmatch(r"\s*T(\d+)\s*-\s*V(\d+)\s*", name or "", re.I)
    return (int(m.group(1)) / 10, float(m.group(2))) if m else None
