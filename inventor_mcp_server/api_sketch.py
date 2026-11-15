"""Sketches: geometry, patterns, checks and offsets."""
if __package__:
    from .api_common import *  # noqa: F401,F403
    from .flat_checks import flat_pattern_problems, rule_from_name  # noqa: F401
else:  # server.py run as a script: the modules sit on sys.path
    from api_common import *  # noqa: F401,F403
    from flat_checks import flat_pattern_problems, rule_from_name  # noqa: F401


class SketchMixin:
    # ---------- Sketches ----------

    def create_sketch(
        self,
        plane: str = "XY",
        face_index: int | None = None,
        offset: float = 0,
    ) -> str:
        """Create a sketch on a work plane, a body face, or an OFFSET work plane.

        offset (mm): creates a hidden work plane parallel to 'plane' at this
        distance and sketches on it. Offset direction: positive = along the
        plane's normal (XY→+Z, XZ→+Y, YZ→+X). Essential for features that
        don't start at the origin planes (ribs, bosses at a height).
        """
        comp = self.active_part()

        if face_index is not None:
            # Create sketch on a body face
            body = comp.SurfaceBodies.Item(1)
            if face_index < 1 or face_index > body.Faces.Count:
                raise ValueError(
                    f"face_index={face_index} out of range. Body has {body.Faces.Count} faces."
                )
            face = body.Faces.Item(face_index)
            sketch = comp.Sketches.Add(face)
            return f"Sketch created on face {face_index} (Sketch{comp.Sketches.Count})"

        planes = {
            "XY": comp.WorkPlanes.Item(3),  # XY plane
            "XZ": comp.WorkPlanes.Item(2),  # XZ plane
            "YZ": comp.WorkPlanes.Item(1),  # YZ plane
        }
        wp = planes.get(plane.upper())
        if wp is None:
            raise ValueError(f"Unknown plane: {plane}. Use XY, XZ, or YZ.")

        if offset:
            wp = comp.WorkPlanes.AddByPlaneAndOffset(wp, offset / 10.0)
            wp.Visible = False
            sketch = comp.Sketches.Add(wp)
            return (f"Sketch created on {plane.upper()}{offset:+g}mm offset plane "
                    f"(Sketch{comp.Sketches.Count})")

        sketch = comp.Sketches.Add(wp)
        return f"Sketch created on {plane.upper()} plane (Sketch{comp.Sketches.Count})"

    def _get_sketch(self, sketch_index: int | None = None):
        comp = self.active_part()
        if sketch_index is not None:
            return comp.Sketches.Item(sketch_index)
        return comp.Sketches.Item(comp.Sketches.Count)

    def _tg(self):
        return self.app.TransientGeometry

    def draw_rectangle(
        self,
        x: float = 0, y: float = 0,
        width: float = 100, height: float = 50,
        sketch_index: int | None = None,
        corner_radius: float = 0,
    ) -> str:
        if corner_radius > 0:
            if corner_radius * 2 > min(width, height) + 1e-9:
                raise ValueError(f"corner_radius {corner_radius} is more than half of {min(width, height)}")
            w, h, r = width / 2, height / 2, corner_radius
            corners = [[x - w, y - h, r], [x + w, y - h, r], [x + w, y + h, r], [x - w, y + h, r]]
            if abs(corner_radius * 2 - min(width, height)) < 1e-9:
                return self.draw_slot(x, y, max(width, height), min(width, height),
                                      0 if width >= height else 90, sketch_index)
            self.draw_closed_profile(corners, sketch_index)
            return f"Rectangle {width}x{height}mm R{corner_radius} at ({x},{y})"
        sketch = self._get_sketch(sketch_index)
        tg = self._tg()
        # All dimensions in cm (Inventor internal units)
        w = width / 10.0
        h = height / 10.0
        cx = x / 10.0
        cy = y / 10.0
        sketch.SketchLines.AddAsTwoPointRectangle(
            tg.CreatePoint2d(cx - w / 2, cy - h / 2),
            tg.CreatePoint2d(cx + w / 2, cy + h / 2),
        )
        return f"Rectangle {width}x{height}mm at ({x},{y})"

    def draw_circle(
        self,
        x: float = 0, y: float = 0,
        diameter: float = 50,
        sketch_index: int | None = None,
        construction: bool = False,
    ) -> str:
        """Draw a circle with an automatic diameter dimension constraint.
        construction=True draws a reference (non-profile) circle, e.g. bolt PCD.
        """
        sketch = self._get_sketch(sketch_index)
        tg = self._tg()
        r = (diameter / 10.0) / 2.0
        cx = x / 10.0
        cy = y / 10.0
        circle = sketch.SketchCircles.AddByCenterRadius(
            tg.CreatePoint2d(cx, cy), r
        )
        if construction:
            circle.Construction = True

        # Add diameter dimension — text placed just outside the circle
        text_x = cx + r * 0.8
        text_y = cy + r * 0.6
        try:
            sketch.DimensionConstraints.AddDiameter(
                circle, tg.CreatePoint2d(text_x, text_y)
            )
        except Exception:
            pass  # dimension already exists or sketch is consumed

        suffix = " [construction]" if construction else ""
        return f"Circle D={diameter}mm at ({x},{y}){suffix}"

    def draw_line(
        self,
        x1: float = 0, y1: float = 0,
        x2: float = 100, y2: float = 0,
        sketch_index: int | None = None,
        construction: bool = False,
    ) -> str:
        sketch = self._get_sketch(sketch_index)
        tg = self._tg()
        line = sketch.SketchLines.AddByTwoPoints(
            tg.CreatePoint2d(x1 / 10.0, y1 / 10.0),
            tg.CreatePoint2d(x2 / 10.0, y2 / 10.0),
        )
        if construction:
            line.Construction = True
        suffix = " [construction]" if construction else ""
        return f"Line from ({x1},{y1}) to ({x2},{y2})mm{suffix}"

    def draw_polygon(
        self,
        x: float = 0, y: float = 0,
        radius: float = 25,
        sides: int = 6,
        sketch_index: int | None = None,
    ) -> str:
        import math
        sketch = self._get_sketch(sketch_index)
        tg = self._tg()
        r = radius / 10.0
        cx = x / 10.0
        cy = y / 10.0
        points = []
        for i in range(sides):
            angle = 2 * math.pi * i / sides - math.pi / 2
            px = cx + r * math.cos(angle)
            py = cy + r * math.sin(angle)
            points.append(tg.CreatePoint2d(px, py))
        # Draw connected lines — share endpoints for proper profile
        first_line = sketch.SketchLines.AddByTwoPoints(points[0], points[1])
        prev_line = first_line
        for i in range(2, sides):
            prev_line = sketch.SketchLines.AddByTwoPoints(
                prev_line.EndSketchPoint, points[i]
            )
        # Close the polygon
        sketch.SketchLines.AddByTwoPoints(
            prev_line.EndSketchPoint, first_line.StartSketchPoint
        )
        return f"Polygon {sides} sides, R={radius}mm at ({x},{y})"

    def draw_closed_profile(
        self,
        points: list,
        sketch_index: int | None = None,
    ) -> str:
        """Draw a closed profile: corners [x, y], rounded corners [x, y, r], arcs
        {"through": [x, y]} between corners (see profile_segments). mm. Segments share
        their end points, so Inventor sees one closed loop for extrude/revolve. Arcs are
        drawn through three points — no clockwise/counter-clockwise flag to get wrong on a
        sketch whose axes are flipped."""
        segs = profile_segments(points)
        sketch = self._get_sketch(sketch_index)
        arcs = self._draw_segments(sketch, segs)
        lines = len(segs) - arcs
        return f"Closed profile: {lines} line(s), {arcs} arc(s)"

    def _draw_segments(self, sketch, segs) -> int:
        """Draw a closed chain of segments (mm), each starting on the previous one's end
        point and the last one ending on the first one's start. Returns the arc count."""
        tg = self._tg()
        pt = lambda p: tg.CreatePoint2d(p[0] / 10.0, p[1] / 10.0)

        def nearest(entity, p):  # an arc's start and end may come out swapped
            a, b = entity.StartSketchPoint, entity.EndSketchPoint
            da = (a.Geometry.X * 10 - p[0]) ** 2 + (a.Geometry.Y * 10 - p[1]) ** 2
            db = (b.Geometry.X * 10 - p[0]) ** 2 + (b.Geometry.Y * 10 - p[1]) ** 2
            return a if da <= db else b

        first = cur = None
        prev = None
        arcs = 0
        for k, seg in enumerate(segs):
            start = cur if cur is not None else pt(seg[1])
            end = first if k == len(segs) - 1 else pt(seg[-1])
            if seg[0] == "line":
                ent = sketch.SketchLines.AddByTwoPoints(start, end)
            else:  # arc or fillet
                ent = sketch.SketchArcs.AddByThreePoints(start, pt(seg[2]), end)
                arcs += 1
            if first is None:
                first = nearest(ent, seg[1])
            cur = nearest(ent, seg[-1])
            if prev is not None and "fillet" in (prev[0], seg[0]) and "line" in (prev[0], seg[0]):
                # rounded corners stay tangent when the sketch is edited later. Only those:
                # the solver would bend an arc "through" a point to make it tangent — a
                # D shape collapsed to a point that way
                try:
                    sketch.GeometricConstraints.AddTangent(prev[1], ent)
                except Exception:
                    pass
            prev = (seg[0], ent)
        return arcs

    def draw_arc(self, x1: float, y1: float, x2: float, y2: float,
                 xm: float | None = None, ym: float | None = None,
                 cx: float | None = None, cy: float | None = None,
                 sketch_index: int | None = None) -> str:
        """An open arc from (x1, y1) to (x2, y2), mm: through (xm, ym), or around the
        center (cx, cy) counter-clockwise in sketch coordinates (start and end must be
        equally far from it)."""
        import math
        if xm is None or ym is None:
            if cx is None or cy is None:
                raise ValueError("Give a point on the arc (xm, ym) or its center (cx, cy)")
            r1, r2 = math.hypot(x1 - cx, y1 - cy), math.hypot(x2 - cx, y2 - cy)
            if abs(r1 - r2) > 0.01:
                raise ValueError(f"Start and end are {r1:.3f} and {r2:.3f} mm from the center — not one circle")
            a1 = math.atan2(y1 - cy, x1 - cx)
            a2 = math.atan2(y2 - cy, x2 - cx)
            sweep = (a2 - a1) % (2 * math.pi) or 2 * math.pi
            am = a1 + sweep / 2
            xm, ym = cx + r1 * math.cos(am), cy + r1 * math.sin(am)
        sketch = self._get_sketch(sketch_index)
        tg = self._tg()
        arc = sketch.SketchArcs.AddByThreePoints(tg.CreatePoint2d(x1 / 10, y1 / 10),
                                                 tg.CreatePoint2d(xm / 10, ym / 10),
                                                 tg.CreatePoint2d(x2 / 10, y2 / 10))
        return (f"Arc R{arc.Radius * 10:.3f} from ({x1},{y1}) to ({x2},{y2}), "
                f"center ({arc.CenterSketchPoint.Geometry.X * 10:.3f}, {arc.CenterSketchPoint.Geometry.Y * 10:.3f})")

    def draw_slot(self, x: float, y: float, length: float, width: float, angle: float = 0,
                  sketch_index: int | None = None) -> str:
        """A slot (two straight sides, round ends) centered on (x, y): overall length and
        width in mm, turned by angle degrees in the sketch."""
        import math
        if length <= width:
            raise ValueError("A slot is longer than it is wide — use draw_circle for length == width")
        a = math.radians(angle)
        d = (math.cos(a), math.sin(a))          # along the slot
        n = (-d[1], d[0])                       # across it
        h, r = (length - width) / 2, width / 2
        p = lambda s, t: [x + d[0] * s + n[0] * t, y + d[1] * s + n[1] * t]
        self.draw_closed_profile([p(-h, r), p(h, r), {"through": p(h + r, 0)},
                                  p(h, -r), p(-h, -r), {"through": p(-h - r, 0)}], sketch_index)
        return f"Slot {length}x{width}mm at ({x},{y}), {angle}°"

    def project_edges(self, face_index: int = 0, edge_indices: str = "", body_index: int = 1,
                      sketch_index: int | None = None) -> str:
        """Project edges of a body onto the sketch — exact lines, arcs, circles and splines,
        linked to the model. face_index: all edges of that face; edge_indices: "3,7,8"."""
        sketch = self._get_sketch(sketch_index)
        body = self.active_part().SurfaceBodies.Item(body_index)
        if face_index:
            edges = body.Faces.Item(face_index).Edges
            edges = [edges.Item(i) for i in range(1, edges.Count + 1)]
        elif edge_indices:
            edges = [body.Edges.Item(int(s)) for s in edge_indices.replace(" ", "").split(",") if s]
        else:
            raise ValueError("Give face_index or edge_indices (list_faces / list_edges show them)")
        kinds = {}
        for e in edges:
            kind = self._curve_kind(sketch.AddByProjectingEntity(e))
            kinds[kind] = kinds.get(kind, 0) + 1
        return f"Projected {len(edges)} edge(s): " + ", ".join(f"{v} {k}" for k, v in sorted(kinds.items()))

    @staticmethod
    def _curve_kind(entity) -> str:
        """line / arc / circle / curve for a sketch entity, by the properties it has."""
        for prop, kind in (("SweepAngle", "arc"), ("Radius", "circle")):
            try:
                getattr(entity, prop)
                return kind
            except Exception:
                pass
        try:
            entity.Geometry.Direction
            return "line"
        except Exception:
            return "curve"

    def faceting(self, body) -> int:
        """How many vertices join two straight edges at a shallow angle (under 20°) — the
        trace of a curve drawn as many short lines. A designed part has almost none."""
        import math
        ends = {}
        for i in range(1, body.Edges.Count + 1):
            e = body.Edges.Item(i)
            if e.GeometryType != 5123:  # kLineSegmentCurve
                continue
            a, b = e.StartVertex.Point, e.StopVertex.Point
            pa, pb = (a.X, a.Y, a.Z), (b.X, b.Y, b.Z)
            for p, q in ((pa, pb), (pb, pa)):
                ends.setdefault(tuple(round(v, 5) for v in p), []).append((q[0] - p[0], q[1] - p[1], q[2] - p[2]))
        def turn(u, v):  # degrees the outline turns at the vertex: 0 = straight on
            nu, nv = math.sqrt(sum(c * c for c in u)), math.sqrt(sum(c * c for c in v))
            if nu < 1e-9 or nv < 1e-9:
                return 90.0
            return 180 - math.degrees(math.acos(max(-1, min(1, sum(a * b for a, b in zip(u, v)) / (nu * nv)))))

        # a vertex of a solid joins three edges (two of the outline, one across the thickness):
        # look at every pair
        return sum(1 for dirs in ends.values()
                   if any(0.5 < turn(dirs[i], dirs[j]) < 20
                          for i in range(len(dirs)) for j in range(i + 1, len(dirs))))


    # ---------- Sketch patterns, checks, offsets ----------

    _CURVES = ("SketchLines", "SketchArcs", "SketchCircles", "SketchEllipses",
               "SketchEllipticalArcs", "SketchSplines")

    def _sketch_curves(self, sketch) -> list[tuple[str, object]]:
        """(collection name, entity) of every curve that isn't construction geometry."""
        out = []
        for coll in self._CURVES:
            try:
                c = getattr(sketch, coll)
            except Exception:
                continue
            for i in range(1, c.Count + 1):
                e = c.Item(i)
                if not e.Construction:
                    out.append((coll, e))
        return out

    @staticmethod
    def _key(point) -> tuple:
        g = point.Geometry
        return (round(g.X * 10, 3), round(g.Y * 10, 3))

    def _solid_profile(self, sketch):
        """sketch.Profiles.AddForSolid(), with what is wrong with the sketch when it fails."""
        try:
            return sketch.Profiles.AddForSolid()
        except Exception as e:
            raise RuntimeError(f"No closed profile in {sketch.Name}: {self._sketch_problems(sketch)}") from e

    def _sketch_problems(self, sketch) -> str:
        info = self.check_sketch_of(sketch, try_profile=False)
        return "; ".join(info["problems"]) or "no open ends found — maybe nothing is drawn"

    def check_sketch(self, sketch_index: int | None = None) -> dict:
        return self.check_sketch_of(self._get_sketch(sketch_index))

    def check_sketch_of(self, sketch, try_profile: bool = True) -> dict:
        """What a profile needs, checked: every end point shared by exactly two curves.
        Open ends, near misses (two ends a hair apart), branches and doubled lines, with
        coordinates in sketch mm; then whether Inventor builds a profile, and how many loops."""
        import math
        curves = self._sketch_curves(sketch)
        ends, counts, lines = {}, {}, {}
        for coll, e in curves:
            counts[coll] = counts.get(coll, 0) + 1
            if coll in ("SketchCircles", "SketchEllipses"):
                continue  # closed by themselves
            try:
                a, b = self._key(e.StartSketchPoint), self._key(e.EndSketchPoint)
            except Exception:
                continue
            for k in (a, b):
                ends[k] = ends.get(k, 0) + 1
            if coll == "SketchLines":
                lines[tuple(sorted((a, b)))] = lines.get(tuple(sorted((a, b))), 0) + 1
        open_ends = [k for k, n in ends.items() if n == 1]
        problems = []
        near = set()
        for i, p in enumerate(open_ends):
            for q in open_ends[i + 1:]:
                d = math.dist(p, q)
                if d < 1.0:
                    problems.append(f"gap of {d:.3f} mm between {p} and {q} — not joined")
                    near.update((p, q))
        lone = [p for p in open_ends if p not in near]
        if lone:
            problems.append(f"{len(lone)} open end(s): " + ", ".join(map(str, lone[:10]))
                            + (" …" if len(lone) > 10 else ""))
        branches = [k for k, n in ends.items() if n > 2]
        if branches:
            problems.append(f"{len(branches)} point(s) where 3+ curves meet (overlap or a stray line): "
                            + ", ".join(map(str, branches[:10])))
        doubled = [k for k, n in lines.items() if n > 1]
        if doubled:
            problems.append(f"{len(doubled)} line(s) drawn twice: " + ", ".join(f"{a}-{b}" for a, b in doubled[:5]))
        info = {"sketch": sketch.Name,
                "curves": {k.replace("Sketch", "").lower(): v for k, v in counts.items()},
                "problems": problems}
        if try_profile:
            try:
                profile = sketch.Profiles.AddForSolid()
                info["profile"] = f"ok — {profile.Count} closed loop(s)"
                try:
                    profile.Delete()
                except Exception:
                    pass
            except Exception:
                info["profile"] = "Inventor can't build a profile from this sketch"
        return info

    def sketch_pattern(self, shape: str = "circle", diameter: float = 0, length: float = 0,
                       width: float = 0, corner_radius: float = 0, angle: float = 0,
                       x: float = 0, y: float = 0, rows: int = 1, columns: int = 1,
                       dx: float = 0, dy: float = 0, count: int = 0, pcd: float = 0,
                       start_angle: float = 0, sketch_index: int | None = None) -> str:
        """Many copies of one shape in one sketch, mm: a grid (first one at x, y; rows x
        columns, pitch dx / dy) or, with count, a circle of them (center x, y, diameter pcd,
        from start_angle; each copy turned with the circle)."""
        import math
        shape = shape.lower()
        if shape not in ("circle", "slot", "rectangle"):
            raise ValueError("shape is circle, slot or rectangle")
        if shape == "circle" and diameter <= 0:
            raise ValueError("circle needs diameter")
        if shape != "circle" and (length <= 0 or width <= 0):
            raise ValueError(f"{shape} needs length and width")
        if count:
            if pcd <= 0:
                raise ValueError("a circular pattern needs pcd (the diameter it runs on)")
            places = [(x + pcd / 2 * math.cos(math.radians(start_angle + 360 * k / count)),
                       y + pcd / 2 * math.sin(math.radians(start_angle + 360 * k / count)),
                       start_angle + 360 * k / count) for k in range(count)]
        else:
            if (rows > 1 and not dy) or (columns > 1 and not dx):
                raise ValueError("a grid needs dx (columns) and dy (rows)")
            places = [(x + c * dx, y + r * dy, 0.0) for r in range(rows) for c in range(columns)]
        if len(places) > 2000:
            raise ValueError(f"{len(places)} copies — more than 2000; use a feature pattern instead")
        sketch = self._get_sketch(sketch_index)
        tg = self._tg()
        sketch.DeferUpdates = True  # no solve after every curve: much faster for many copies
        try:
            self._pattern_copies(sketch, tg, places, shape, diameter, length, width,
                                 corner_radius, angle, sketch_index)
        finally:
            sketch.DeferUpdates = False
        return f"{len(places)} {shape}(s) in {sketch.Name}" + (f" on PCD {pcd}" if count else f" ({rows} x {columns})")

    def _pattern_copies(self, sketch, tg, places, shape, diameter, length, width,
                        corner_radius, angle, sketch_index):
        import math
        for px, py, turn in places:
            if shape == "circle":
                sketch.SketchCircles.AddByCenterRadius(tg.CreatePoint2d(px / 10, py / 10), diameter / 20)
            elif shape == "slot":
                self.draw_slot(px, py, length, width, angle + turn, sketch_index)
            else:
                a = math.radians(angle + turn)
                c, s = math.cos(a), math.sin(a)
                corner = lambda u, v: [px + u * c - v * s, py + u * s + v * c] + ([corner_radius] if corner_radius else [])
                self.draw_closed_profile([corner(-length / 2, -width / 2), corner(length / 2, -width / 2),
                                          corner(length / 2, width / 2), corner(-length / 2, width / 2)],
                                         sketch_index)

    def offset_profile(self, distance: float, side: str = "inside",
                       sketch_index: int | None = None) -> str:
        """Offset every closed loop of the sketch by distance mm, inside or outside."""
        side = side.lower()
        if side not in ("inside", "outside"):
            raise ValueError("side is inside or outside")
        sketch = self._get_sketch(sketch_index)
        loops = self._loops(sketch)
        if not loops:
            raise RuntimeError(f"No closed loop in {sketch.Name} to offset")
        done = []
        for loop in loops:
            before = self._span(loop)
            for natural in (True, False):
                # one curve of the loop + IncludeConnectedEntities: Inventor refuses the whole
                # loop handed over curve by curve ("Failed to create offset")
                coll = self.app.TransientObjects.CreateObjectCollection()
                coll.Add(loop[0])
                made = list(sketch.OffsetSketchEntitiesUsingDistance(coll, distance / 10, natural, True, False))
                after = self._span(made)
                if (after < before) == (side == "inside"):
                    done.append(len(made))
                    break
                for e in made:
                    e.Delete()
            else:
                raise RuntimeError("Inventor couldn't offset a loop to the requested side")
        return f"Offset {len(done)} loop(s) {distance} mm {side} ({sum(done)} curve(s))"

    def _loops(self, sketch) -> list[list]:
        """Closed loops of the sketch's curves, by shared end points (circles alone)."""
        curves = [e for coll, e in self._sketch_curves(sketch)]
        loops, rest = [], []
        for e in curves:
            try:
                e.StartSketchPoint
                rest.append(e)
            except Exception:
                loops.append([e])  # circle / ellipse
        while rest:
            loop = [rest.pop()]
            keys = {self._key(loop[0].StartSketchPoint), self._key(loop[0].EndSketchPoint)}
            grew = True
            while grew:
                grew = False
                for e in list(rest):
                    ks = {self._key(e.StartSketchPoint), self._key(e.EndSketchPoint)}
                    if ks & keys:
                        loop.append(e)
                        rest.remove(e)
                        keys |= ks
                        grew = True
            loops.append(loop)
        return loops

    def _span(self, entities) -> float:
        """Width + height of the entities' points (circles: center ± radius), mm."""
        xs, ys = [], []
        for e in entities:
            try:
                for p in (e.StartSketchPoint, e.EndSketchPoint):
                    xs.append(p.Geometry.X); ys.append(p.Geometry.Y)
            except Exception:
                pass
            try:
                c, r = e.CenterSketchPoint.Geometry, e.Radius
                xs += [c.X - r, c.X + r]; ys += [c.Y - r, c.Y + r]
            except Exception:
                pass
        return (max(xs) - min(xs) + max(ys) - min(ys)) * 10 if xs else 0.0
