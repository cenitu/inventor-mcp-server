"""Sheet metal: rules, faces, flanges, profiles and cuts."""
if __package__:
    from .api_common import *  # noqa: F401,F403
    from .flat_checks import flat_pattern_problems, rule_from_name  # noqa: F401
else:  # server.py run as a script: the modules sit on sys.path
    from api_common import *  # noqa: F401,F403
    from flat_checks import flat_pattern_problems, rule_from_name  # noqa: F401


class SheetMetalMixin:
    # ---------- Sheet Metal Features ----------

    def set_sheet_metal_thickness(self, thickness_mm: float) -> str:
        """Set the thickness of the active sheet metal style. Inventor 2026.
        Uses local decimal (comma) — required by Inventor for European locales.
        """
        comp = self.active_part()
        style = comp.ActiveSheetMetalStyle
        # Try comma decimal first (European), fall back to dot
        try:
            style.Thickness = f"{thickness_mm:.3f}".replace(".", ",") + " mm"
        except Exception:
            style.Thickness = f"{thickness_mm:.3f} mm"
        return f"Sheet metal thickness set to {thickness_mm}mm (style: {style.Name})"

    def list_sheet_metal_rules(self) -> list[dict]:
        """Sheet metal rules (styles) available in the active part.

        Rules come from the template / style library, so a shop's own rules
        (e.g. 'T15-V10' = 1.5 mm with a V10 die) show up here. Thickness and
        bend radius are returned as Inventor expressions, exactly as stored.
        """
        comp = self.active_part()
        if not hasattr(comp, "SheetMetalStyles"):
            raise RuntimeError("Active part is not a sheet metal part. "
                               "Use create_part(template='sheet_metal').")
        active = comp.ActiveSheetMetalStyle.Name
        rules = []
        styles = comp.SheetMetalStyles
        for i in range(1, styles.Count + 1):
            s = styles.Item(i)
            try:
                radius = s.BendRadius
            except Exception:
                radius = None
            rules.append({"name": s.Name, "thickness": s.Thickness,
                          "bend_radius": radius, "active": s.Name == active})
        return rules

    def set_sheet_metal_rule(self, name: str) -> str:
        """Activate a sheet metal rule by name (case-insensitive).

        Prefer this over set_sheet_metal_thickness when the shop has its own
        rules: a rule carries thickness, bend radius AND unfold method together,
        so flat patterns match the real press brake.

        NOTE: ComponentDefinition.ActiveSheetMetalStyle is READ-ONLY —
        assigning to it raises. The style activates itself via .Activate().
        """
        comp = self.active_part()
        if not hasattr(comp, "SheetMetalStyles"):
            raise RuntimeError("Active part is not a sheet metal part. "
                               "Use create_part(template='sheet_metal').")
        styles = comp.SheetMetalStyles
        names = [styles.Item(i).Name for i in range(1, styles.Count + 1)]
        match = next((i for i, n in enumerate(names, 1) if n.lower() == name.lower()), None)
        if match is None:
            raise ValueError(f"Sheet metal rule '{name}' not found. Available: {', '.join(names)}")
        styles.Item(match).Activate()
        s = comp.ActiveSheetMetalStyle
        try:
            thickness_mm = comp.Thickness.Value * 10   # resolved Parameter, cm -> mm
            t = f"{thickness_mm:g} mm"
        except Exception:
            t = s.Thickness
        radius = s.BendRadius
        try:
            # Expression may reference other parameters ('Thickness', '10 / 6');
            # resolve it now that this rule is the active one. Result is in cm.
            r_mm = self.active_document().UnitsOfMeasure.GetValueFromExpression(radius, "mm") * 10
            radius = f"{radius} = {r_mm:.3f} mm"
        except Exception:
            pass
        return f"Sheet metal rule '{s.Name}' active: thickness {t}, bend radius {radius}"

    def sheet_metal_face(self, sketch_index: int | None = None) -> str:
        """Create a base sheet-metal Face from a closed sketch profile.
        Equivalent of the 'Face' command in Inventor Sheet Metal UI.
        Uses the active sheet metal style's thickness automatically.
        """
        comp = self.active_part()
        sketch = self._get_sketch(sketch_index)
        profile = self._solid_profile(sketch)
        ff = comp.Features.FaceFeatures
        before = self._vol_state()
        fdef = ff.CreateFaceFeatureDefinition(profile)
        feat = ff.Add(fdef)
        return f"Sheet metal face: {feat.Name}{self._fmt_delta(before)}"

    @staticmethod
    def _profile_path(kind: str, width: float, height: float, flange: float, t: float) -> list:
        """Points of an open profile (mm) on the XY plane, with the outer size starting at 0,0.
        A contour flange puts the material on the right of the direction of travel."""
        if kind == "L":
            return [(t, height), (t, t), (width, t)]
        if kind == "U":
            return [(t, height), (t, t), (width - t, t), (width - t, height)]
        if kind == "C":
            return [(flange, height - t), (t, height - t), (t, t), (width - t, t),
                    (width - t, height - t), (width - flange, height - t)]
        if kind == "Z":
            return [(0, t), (flange - t, t), (flange - t, height), (flange + width - t, height)]
        if kind == "HAT":
            return [(0, t), (flange, t), (flange, height), (flange + width, height),
                    (flange + width, t), (2 * flange + width, t)]
        raise ValueError(f"Unknown profile '{kind}'. Use L, U, C, Z or hat.")

    def sheet_profile(self, kind: str, width: float, height: float, length: float,
                      flange: float = 0, bend_radius: float = 0) -> str:
        """One bent profile as one contour flange: outer sizes in the XY plane, length along +Z.
        The sheet thickness comes from the active rule, the bend radius too unless given."""
        comp = self.active_part()
        if not hasattr(comp, "SheetMetalStyles"):
            raise RuntimeError("Active part is not a sheet metal part. "
                               "Use create_document(kind='sheet_metal').")
        if comp.SurfaceBodies.Count:
            raise ValueError("sheet_profile starts a part, and this one already has a body.")
        k = kind.upper()
        t = comp.Thickness.Value * 10
        if k in ("C", "Z", "HAT") and flange <= 0:
            raise ValueError(f"A {kind} profile needs flange > 0.")
        pts = self._profile_path(k, width, height, flange, t)
        for p, q in zip(pts, pts[1:]):
            if math.dist(p, q) <= 0:
                raise ValueError(f"Sizes leave no leg between {p} and {q} at {t:g} mm thickness.")
        sk = comp.Sketches.Add(comp.WorkPlanes.Item(3))
        tg = self.app.TransientGeometry
        first = sk.SketchLines.AddByTwoPoints(tg.CreatePoint2d(pts[0][0] / 10, pts[0][1] / 10),
                                              tg.CreatePoint2d(pts[1][0] / 10, pts[1][1] / 10))
        last = first
        for x, y in pts[2:]:
            last = sk.SketchLines.AddByTwoPoints(last.EndSketchPoint, tg.CreatePoint2d(x / 10, y / 10))
        features = comp.Features.ContourFlangeFeatures
        d = features.CreateContourFlangeDefinition(comp.Features.CreatePath(first))
        if bend_radius:
            d.BendRadius = bend_radius / 10
        d.SetDistanceExtent(length / 10, kPositiveExtentDirection)
        before = self._vol_state()
        try:
            feat = features.Add(d)
        except Exception as e:
            sk.Delete()
            raise RuntimeError(f"Contour flange failed — is a leg too short for the bend radius? ({e})") from e
        self.active_document().Update()
        rb = comp.SurfaceBodies.Item(1).RangeBox
        size = [(rb.MaxPoint.X - rb.MinPoint.X) * 10, (rb.MaxPoint.Y - rb.MinPoint.Y) * 10,
                (rb.MaxPoint.Z - rb.MinPoint.Z) * 10]
        want = [{"Z": flange + width - t, "HAT": 2 * flange + width}.get(k, width), height, length]
        note = "" if all(abs(a - b) < 0.01 for a, b in zip(size, want)) else \
            f"\nWARNING: outer size {[round(v, 2) for v in size]} mm, expected {want}."
        return (f"{kind} profile {feat.Name}: {size[0]:g} x {size[1]:g} x {size[2]:g} mm, "
                f"{t:g} mm sheet{self._fmt_delta(before)}{note}")

    # HeightDatumTypeEnum — how the flange height is measured
    _HEIGHT_DATUM = {"outer": 75521, "inner": 75522, "tangent": 75523,
                     "outer_ortho": 75524, "inner_ortho": 75525}

    def flange(
        self,
        edge_indices: list[int],
        distance: float = 25,
        angle: float = 90,
        body_index: int = 1,
        auto_miter: bool = True,
        miter_gap: str = "GapSize",
        height_datum: str = "outer",
    ) -> str:
        """Add a Sheet Metal Flange to one or more edges.

        distance: flange height in mm (default 25mm = Inventor's own default)
        angle: bend angle in degrees, default 90° (perpendicular)
        edge_indices: 1-based indices into the body's Edges collection. Give all
            edges of a loop in ONE call so Inventor auto-miters the corners —
            separate flanges need corner seams to close the gaps.
        auto_miter: miter the corners between the edges (UI: Auto-Mitering).
        miter_gap: gap expression; "GapSize" takes it from the sheet metal rule.
        height_datum: "outer" (from outer intersection, UI default), "inner",
            "tangent", "outer_ortho", "inner_ortho".

        Quirks (Inventor 2026/2027):
          - The Distance argument of CreateFlangeDefinition is ignored; the
            height must be set with SetDistanceHeightExtent before Add.
          - The API default MiterGap is the sheet THICKNESS, not the rule's
            GapSize that the UI uses — corners come out visibly open unless
            MiterGap is set explicitly.
          - Angle must be in RADIANS. Passing 90 → 5156°.
          - The flange grows from the face the selected edge belongs to: pick the
            edge on the side the flange should bend toward (e.g. the inner-face
            edge of a wall for an inward flange).
        With "outer" the distance equals the height from the outer mold line, so
        distance=200 on a base panel at Z=0..t gives walls reaching Z=200.
        """
        import math
        datum = self._HEIGHT_DATUM.get(height_datum.lower())
        if datum is None:
            raise ValueError(f"height_datum must be one of {', '.join(self._HEIGHT_DATUM)}")
        comp = self.active_part()
        body = comp.SurfaceBodies.Item(body_index)
        ec = self.app.TransientObjects.CreateEdgeCollection()
        for idx in edge_indices:
            ec.Add(body.Edges.Item(idx))

        flf = comp.Features.FlangeFeatures
        before = self._vol_state()
        fdef = flf.CreateFlangeDefinition(ec, math.radians(angle), distance / 10.0)
        fdef.SetDistanceHeightExtent(distance / 10.0, kPositiveExtentDirection, datum)
        fdef.ApplyAutoMitering = auto_miter
        if auto_miter and miter_gap:
            fdef.MiterGap = miter_gap
        feat = flf.Add(fdef)
        # Update so subsequent feature/edge operations see correct geometry
        self.active_document().Update()
        gap = ""
        if auto_miter:
            try:
                g = feat.Definition.MiterGap
                gap = f", miter gap {g.Expression} = {g.Value * 10:.2f} mm"
            except Exception:
                pass
        return (f"Flange {distance}mm @ {angle}° on {ec.Count} edge(s){gap}: "
                f"{feat.Name}{self._fmt_delta(before)}")

    def sheet_metal_cut(
        self,
        sketch_index: int | None = None,
    ) -> str:
        """Cut through the sheet using a sketch profile. Sheet Metal Cut.
        Cuts perpendicular to the face (through thickness).
        """
        comp = self.active_part()
        sketch = self._get_sketch(sketch_index)
        profile = self._solid_profile(sketch)

        cf = comp.Features.CutFeatures
        before = self._vol_state()
        cdef = cf.CreateCutDefinition(profile)
        # Default extent = through thickness (kThroughAllExtent style)
        feat = cf.Add(cdef)
        self._require_removed(feat, before, "Sheet metal cut")
        return f"Sheet metal cut: {feat.Name}{self._fmt_delta(before)}"

    def _require_removed(self, feat, before, label):
        """A sheet metal Cut sketched on a work plane (not on a face of the sheet)
        is created without error but removes nothing. Delete it and raise."""
        after = self._vol_state()
        if before and after and before[0] - after[0] < 1e-3:
            name = feat.Name
            feat.Delete(False, False, False)
            raise RuntimeError(
                f"{label} '{name}' removed no material and was deleted — the sketch must "
                "lie on a face of the sheet (create_sketch(face_index=...) or cut_on_face), "
                "not on a work plane, and the profile must overlap the sheet.")

    def cut_on_face(
        self,
        face_index: int,
        circles: list | None = None,
        rectangles: list | None = None,
        body_index: int = 1,
    ) -> str:
        """Cut holes through the sheet at WORLD coordinates (mm), sketched on a face.

        Face sketches have their own origin and axes (often flipped), so placing
        geometry with sketch coordinates is error-prone. Here every point is given
        in model space and mapped with PlanarSketch.ModelToSketchSpace.

        circles:    [[x, y, z, diameter], ...]
        rectangles: [[[x, y, z], [x, y, z], [x, y, z], [x, y, z]], ...] — 4 corners in order
        The removed volume is verified; a cut that removes nothing raises.
        """
        comp = self.active_part()
        body = comp.SurfaceBodies.Item(body_index)
        face = body.Faces.Item(face_index)
        sketch = comp.Sketches.Add(face)
        tg = self._tg()

        def p2(x, y, z):
            return sketch.ModelToSketchSpace(tg.CreatePoint(x / 10.0, y / 10.0, z / 10.0))

        n = 0
        for x, y, z, d in circles or []:
            sketch.SketchCircles.AddByCenterRadius(p2(x, y, z), d / 20.0)
            n += 1
        for corners in rectangles or []:
            pts = [p2(*c) for c in corners]
            first = sketch.SketchLines.AddByTwoPoints(pts[0], pts[1])
            last = first
            for pt in pts[2:]:
                last = sketch.SketchLines.AddByTwoPoints(last.EndSketchPoint, pt)
            sketch.SketchLines.AddByTwoPoints(last.EndSketchPoint, first.StartSketchPoint)
            n += 1
        if n == 0:
            sketch.Delete()
            raise ValueError("Give at least one circle or rectangle.")
        cf = comp.Features.CutFeatures
        before = self._vol_state()
        feat = cf.Add(cf.CreateCutDefinition(self._solid_profile(sketch)))
        self.active_document().Update()
        self._require_removed(feat, before, "cut_on_face")
        return f"Cut {n} shape(s) on face {face_index}: {feat.Name}{self._fmt_delta(before)}"

    def corner_round(self, edge_indices: list[int], radius: float = 5, body_index: int = 1) -> str:
        """Round sheet metal corners (Corner Round feature — also rounds the flat
        pattern, so the laser cuts it). Edges are the short edges running through
        the thickness at the corners; locate them with find_edge / list_edges."""
        comp = self.active_part()
        body = comp.SurfaceBodies.Item(body_index)
        ec = self.app.TransientObjects.CreateEdgeCollection()
        for idx in edge_indices:
            ec.Add(body.Edges.Item(idx))
        before = self._vol_state()
        crf = comp.Features.CornerRoundFeatures
        feat = crf.Add(crf.CreateCornerRoundDefinition(ec, f"{radius} mm"))
        self.active_document().Update()
        return f"Corner round R{radius} on {ec.Count} corner(s): {feat.Name}{self._fmt_delta(before)}"

    def find_edge(
        self,
        body_index: int = 1,
        x: float | None = None,
        y: float | None = None,
        z: float | None = None,
        tolerance: float = 0.1,
    ) -> int:
        """Locate a body edge by its midpoint coordinates (mm). Returns 1-based index.
        Pass any subset of x/y/z — None values are not constrained.
        Useful for finding the edge to flange (no need to print all 20 edges).
        """
        comp = self.active_part()
        body = comp.SurfaceBodies.Item(body_index)
        for i in range(1, body.Edges.Count + 1):
            e = body.Edges.Item(i)
            sv, ev = e.StartVertex.Point, e.StopVertex.Point
            mx = (sv.X + ev.X) / 2 * 10
            my = (sv.Y + ev.Y) / 2 * 10
            mz = (sv.Z + ev.Z) / 2 * 10
            if x is not None and abs(mx - x) > tolerance: continue
            if y is not None and abs(my - y) > tolerance: continue
            if z is not None and abs(mz - z) > tolerance: continue
            return i
        criteria = ", ".join(
            f"{k}={v}" for k, v in [("x", x), ("y", y), ("z", z)] if v is not None
        )
        raise ValueError(f"No edge found matching midpoint ({criteria}) tol={tolerance}mm")

    def find_face(
        self,
        body_index: int = 1,
        x: float | None = None,
        y: float | None = None,
        z: float | None = None,
        min_area: float = 100,
        surface_type: int = 5890,
        tolerance: float = 0.5,
    ) -> int:
        """Locate a body face by its centroid coordinates (mm). Returns 1-based index.

        Useful for: finding a panel face to sketch on for sheet metal Cut.
        Centroid is computed as the average of the face's vertices.

        surface_type: 5890=Plane (default), 5891=Cylinder, 5893=Cone, 5895=Torus, 5896=Sphere
                      (SURFACE_TYPES has them all)
        min_area: filter out small auxiliary faces (mm²)
        tolerance: how close the centroid must be (mm)
        """
        comp = self.active_part()
        body = comp.SurfaceBodies.Item(body_index)
        for i in range(1, body.Faces.Count + 1):
            f = body.Faces.Item(i)
            if surface_type is not None and f.SurfaceType != surface_type: continue
            try:
                xs, ys, zs = [], [], []
                for vi in range(1, f.Vertices.Count + 1):
                    v = f.Vertices.Item(vi)
                    xs.append(v.Point.X * 10)
                    ys.append(v.Point.Y * 10)
                    zs.append(v.Point.Z * 10)
                if not xs: continue
                cx = sum(xs) / len(xs)
                cy = sum(ys) / len(ys)
                cz = sum(zs) / len(zs)
                area_mm2 = f.Evaluator.Area * 100
                if area_mm2 < min_area: continue
                if x is not None and abs(cx - x) > tolerance: continue
                if y is not None and abs(cy - y) > tolerance: continue
                if z is not None and abs(cz - z) > tolerance: continue
                return i
            except Exception:
                continue
        criteria = ", ".join(
            f"{k}={v}" for k, v in [("x", x), ("y", y), ("z", z)] if v is not None
        )
        raise ValueError(f"No face matching centroid ({criteria}) area≥{min_area} tol={tolerance}mm")
