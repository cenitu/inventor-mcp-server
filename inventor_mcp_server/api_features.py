"""Part features, diagnostics, transactions, parameters and queries."""
if __package__:
    from .api_common import *  # noqa: F401,F403
    from .flat_checks import flat_pattern_problems, rule_from_name  # noqa: F401
else:  # server.py run as a script: the modules sit on sys.path
    from api_common import *  # noqa: F401,F403
    from flat_checks import flat_pattern_problems, rule_from_name  # noqa: F401


class FeaturesMixin:
    # ---------- Features ----------

    def extrude(
        self,
        distance: float = 10,
        direction: str = "positive",
        operation: str = "join",
        sketch_index: int | None = None,
        profile_index: int = 1,
        extent_type: str = "distance",
    ) -> str:
        comp = self.active_part()
        sketch = self._get_sketch(sketch_index)
        profile = self._solid_profile(sketch)

        dir_map = {
            "positive": kPositiveExtentDirection,
            "negative": kNegativeExtentDirection,
            "symmetric": kSymmetricExtentDirection,
        }
        op_map = {
            "join": kJoinOperation,
            "cut": kCutOperation,
            "intersect": kIntersectOperation,
            "surface": kSurfaceOperation,
            "new": kNewBodyOperation,
        }

        direction_enum = dir_map.get(direction.lower(), kPositiveExtentDirection)
        operation_enum = op_map.get(operation.lower(), kJoinOperation)
        before = self._vol_state()

        if extent_type.lower() == "all":
            feature = comp.Features.ExtrudeFeatures.AddByThroughAllExtent(
                profile, direction_enum, operation_enum
            )
            return f"Extrude through-all {direction} ({operation}): {feature.Name}{self._fmt_delta(before)}"
        else:
            dist_cm = distance / 10.0
            feature = comp.Features.ExtrudeFeatures.AddByDistanceExtent(
                profile, dist_cm, direction_enum, operation_enum
            )
            return f"Extrude {distance}mm {direction} ({operation}): {feature.Name}{self._fmt_delta(before)}"

    def revolve(
        self,
        angle: float = 360,
        sketch_index: int | None = None,
        axis: str = "Y",
        operation: str = "join",
        axis_line_index: int | None = None,
    ) -> str:
        """Revolve a sketch profile around an axis.

        axis: "X" / "Y" / "Z"  — use a named WorkAxis (preferred, no construction line needed)
        axis_line_index: sketch line index (legacy); overrides axis when provided.
        """
        import math
        comp = self.active_part()
        sketch = self._get_sketch(sketch_index)
        profile = self._solid_profile(sketch)

        # Resolve axis entity: named WorkAxis (preferred) or sketch line (legacy)
        _work_axis_map = {"X": 1, "Y": 2, "Z": 3}
        if axis_line_index is not None:
            # Explicit keyword: legacy sketch-line mode
            axis_entity = sketch.SketchLines.Item(axis_line_index)
            axis_label = f"sketch line {axis_line_index}"
        elif isinstance(axis, str) and axis.upper() in _work_axis_map:
            # Named work axis — preferred, no construction line needed
            axis_entity = comp.WorkAxes.Item(_work_axis_map[axis.upper()])
            axis_label = f"{axis.upper()} work axis"
        elif isinstance(axis, int):
            # Backward compat: integer passed positionally from old server.py
            axis_entity = sketch.SketchLines.Item(axis)
            axis_label = f"sketch line {axis}"
        else:
            axis_entity = sketch.SketchLines.Item(1)
            axis_label = "sketch line 1"

        op_map = {
            "join": kJoinOperation,
            "cut": kCutOperation,
            "intersect": kIntersectOperation,
            "new": kNewBodyOperation,
        }
        operation_enum = op_map.get(operation.lower(), kJoinOperation)

        revolve_features = comp.Features.RevolveFeatures
        before = self._vol_state()

        if angle >= 360:
            revolve_features.AddFull(profile, axis_entity, operation_enum)
        else:
            angle_rad = math.radians(angle)
            revolve_features.AddByAngle(
                profile, axis_entity, angle_rad,
                kPositiveExtentDirection, operation_enum
            )

        return f"Revolve {angle}° around {axis_label} ({operation}){self._fmt_delta(before)}"

    def fillet(self, radius: float = 2, edge_indices: list[int] | None = None) -> str:
        comp = self.active_part()
        r_cm = radius / 10.0

        edges_collection = self.app.TransientObjects.CreateEdgeCollection()
        body = comp.SurfaceBodies.Item(1)

        if edge_indices:
            for idx in edge_indices:
                edges_collection.Add(body.Edges.Item(idx))
        else:
            edges_collection.Add(body.Edges.Item(1))

        before = self._vol_state()
        fillet_def = comp.Features.FilletFeatures.CreateFilletDefinition()
        fillet_def.AddConstantRadiusEdgeSet(edges_collection, r_cm)
        comp.Features.FilletFeatures.Add(fillet_def)
        return f"Fillet R={radius}mm on {edges_collection.Count} edge(s){self._fmt_delta(before)}"

    def chamfer(self, distance: float = 2, edge_indices: list[int] | None = None) -> str:
        """Add equal-distance chamfer to edges.
        Inventor 2026 API: ChamferFeatures.AddUsingDistance(EdgeCollection, dist_cm)
        NOT CreateChamferDefinition (removed in 2026).
        Requires EdgeCollection (not ObjectCollection).
        """
        comp = self.active_part()
        d_cm = distance / 10.0

        ec = self.app.TransientObjects.CreateEdgeCollection()
        body = comp.SurfaceBodies.Item(1)

        if edge_indices:
            for idx in edge_indices:
                ec.Add(body.Edges.Item(idx))
        else:
            ec.Add(body.Edges.Item(1))

        before = self._vol_state()
        comp.Features.ChamferFeatures.AddUsingDistance(ec, d_cm)
        return f"Chamfer {distance}mm on {ec.Count} edge(s){self._fmt_delta(before)}"

    _OPERATIONS = {"join": kJoinOperation, "cut": kCutOperation,
                   "intersect": kIntersectOperation, "new": kNewBodyOperation}

    def _operation(self, operation: str) -> int:
        try:
            return self._OPERATIONS[operation.lower()]
        except KeyError:
            raise ValueError(f"operation must be one of {', '.join(self._OPERATIONS)}, not {operation!r}")

    def shell(self, thickness: float = 2, remove_faces: list[int] | None = None,
              direction: str = "inside") -> str:
        """Hollow body 1 to a wall of `thickness` mm, leaving the faces in remove_faces
        (1-based, from find_faces) open. No faces = a closed hollow body.
        direction: "inside" (outer size kept), "outside" or "both"."""
        comp = self.active_part()
        dirs = {"inside": kInsideShellDirection, "outside": kOutsideShellDirection,
                "both": kBothSidesShellDirection}
        if direction.lower() not in dirs:
            raise ValueError(f"direction must be inside, outside or both, not {direction!r}")
        body = comp.SurfaceBodies.Item(1)
        faces = self.app.TransientObjects.CreateFaceCollection()
        for idx in remove_faces or []:
            if not 1 <= idx <= body.Faces.Count:
                raise ValueError(f"face {idx} out of range: the body has {body.Faces.Count} faces")
            faces.Add(body.Faces.Item(idx))
        before = self._vol_state()
        sf = comp.Features.ShellFeatures
        definition = sf.CreateShellDefinition(faces, thickness / 10.0, dirs[direction.lower()])
        feature = sf.Add(definition)
        return (f"Shell {thickness}mm {direction}, {faces.Count} face(s) open: {feature.Name}"
                f"{self._fmt_delta(before)}")

    def loft(self, sketch_indices: list[int], operation: str = "join", closed: bool = False) -> str:
        """Blend the closed profiles of two or more sketches, in the order given.
        closed: also join the last section back to the first (a ring)."""
        comp = self.active_part()
        if len(sketch_indices) < 2:
            raise ValueError("a loft needs at least two sketches")
        sections = self.app.TransientObjects.CreateObjectCollection()
        for idx in sketch_indices:
            sections.Add(self._solid_profile(self._get_sketch(idx)))
        before = self._vol_state()
        lf = comp.Features.LoftFeatures
        definition = lf.CreateLoftDefinition(sections, self._operation(operation))
        definition.Closed = closed
        feature = lf.Add(definition)
        return (f"Loft through {len(sketch_indices)} sketch(es) ({operation}): {feature.Name}"
                f"{self._fmt_delta(before)}")

    def sweep(self, path_sketch: int, sketch_index: int | None = None, operation: str = "join",
              orientation: str = "normal", taper: float = 0, twist: float = 0) -> str:
        """Sweep a sketch's closed profile along the curves of another sketch.

        path_sketch: sketch holding the path; its curves joined end to end are followed.
        orientation: "normal" (profile stays square to the path) or "parallel"
        (profile keeps its original direction). taper / twist in degrees.
        """
        import math
        comp = self.active_part()
        if path_sketch == sketch_index or (sketch_index is None and path_sketch == comp.Sketches.Count):
            raise ValueError("the path must be in another sketch than the profile")
        orient = {"normal": kNormalToPath, "parallel": kParallelToOriginalProfile}
        if orientation.lower() not in orient:
            raise ValueError(f"orientation must be normal or parallel, not {orientation!r}")
        profile = self._solid_profile(self._get_sketch(sketch_index))
        curves = self._sketch_curves(self._get_sketch(path_sketch))
        if not curves:
            raise ValueError(f"Sketch{path_sketch} has no curves for a path")
        sf = comp.Features.SweepFeatures
        path = comp.Features.CreatePath(curves[0][1])
        before = self._vol_state()
        definition = sf.CreateSweepDefinition(kPathSweepType, profile, path, self._operation(operation))
        definition.ProfileOrientation = orient[orientation.lower()]
        if taper:
            definition.TaperAngle = math.radians(taper)
        if twist:
            definition.TwistAngle = math.radians(twist)
        feature = sf.Add(definition)
        return (f"Sweep along Sketch{path_sketch} ({path.Count} curve(s), {operation}): {feature.Name}"
                f"{self._fmt_delta(before)}")

    # Standard ISO metric coarse thread pitches (mm)
    _METRIC_COARSE_PITCH = {
        1: 0.25, 1.2: 0.25, 1.4: 0.3, 1.6: 0.35, 2: 0.4, 2.5: 0.45,
        3: 0.5, 3.5: 0.6, 4: 0.7, 5: 0.8, 6: 1.0, 8: 1.25, 10: 1.5,
        12: 1.75, 14: 2.0, 16: 2.0, 18: 2.5, 20: 2.5, 22: 2.5, 24: 3.0,
        27: 3.0, 30: 3.5, 33: 3.5, 36: 4.0, 39: 4.0, 42: 4.5, 45: 4.5,
        48: 5.0, 52: 5.0, 56: 5.5, 60: 5.5, 64: 6.0,
    }

    def hole(
        self,
        x: float = 0, y: float = 0,
        diameter: float = 10,
        depth: float = 0,
        tapped: bool = False,
        pitch: float | None = None,
    ) -> str:
        """Create a proper Inventor HoleFeature using native HoleFeatures API.
        depth=0 means through-all (default).
        tapped=True creates a tapped (threaded) hole with ISO Metric profile.
        pitch: thread pitch in mm (auto-selected from standard coarse series if None).
        Uses CreateSketchPlacementDefinition(ObjectCollection_of_SketchPoints) API.
        """
        comp = self.active_part()
        tg = self._tg()
        hf = comp.Features.HoleFeatures
        before = self._vol_state()

        # Create sketch on XY work plane for hole center point
        wp = comp.WorkPlanes.Item(3)  # XY plane
        sketch = comp.Sketches.Add(wp)
        pt = sketch.SketchPoints.Add(tg.CreatePoint2d(x / 10.0, y / 10.0))

        # Create ObjectCollection with the SketchPoint (required by API)
        col = self.app.TransientObjects.CreateObjectCollection()
        col.Add(pt)

        # Create sketch-based placement definition
        placement = hf.CreateSketchPlacementDefinition(col)

        diameter_cm = diameter / 10.0

        # Build second argument: TapInfo for tapped holes, float diameter for drilled holes
        if tapped:
            # Determine pitch (standard coarse if not given)
            if pitch is None:
                dia_int = int(round(diameter))
                pitch = self._METRIC_COARSE_PITCH.get(dia_int)
                if pitch is None:
                    raise ValueError(
                        f"No standard coarse pitch for M{dia_int}. "
                        "Pass pitch= explicitly."
                    )
            # Format designation: "M16x2" (not "M16x2.0")
            dia_int = int(round(diameter))
            pitch_str = str(int(pitch)) if pitch == int(pitch) else str(pitch)
            designation = f"M{dia_int}x{pitch_str}"
            tap_info = hf.CreateTapInfo(
                True,                   # RightHanded
                "ISO Metric profile",   # ThreadType (exact string from thread.xlsx)
                designation,            # ThreadDesignation e.g. "M16x2"
                "6H",                   # Class (standard internal metric)
                True,                   # FullTapDepth
            )
            hole_arg = tap_info
            type_str = f"M{dia_int} tapped (pitch {pitch}mm, 6H)"
        else:
            hole_arg = diameter_cm
            type_str = f"drilled D={diameter}mm"

        def add(arg, dist):
            if depth <= 0:
                return hf.AddDrilledByThroughAllExtent(placement, arg, kNegativeExtentDirection)
            return hf.AddDrilledByDistanceExtent(placement, arg, dist, kNegativeExtentDirection)

        extent_str = "through-all" if depth <= 0 else f"{depth}mm deep"
        try:
            feature = add(hole_arg, depth / 10.0)
        except Exception:
            # some parts (Content Center members) reject plain numbers — values with units work
            if tapped:
                raise
            feature = add(f"{diameter} mm", f"{depth} mm")

        self.active_document().Update()
        after = self._vol_state()
        if before and after and after[0] >= before[0] - 1e-3:
            # the feature exists but cut nothing (e.g. the point is off the body, or the
            # feature failed) — don't leave it and its sketch behind
            feature.Delete()
            try:
                sketch.Delete()
            except Exception:
                pass
            raise RuntimeError(f"Hole at ({x},{y}) removed no material — the point is sketched on "
                               "the XY plane and drills into +Z: is it inside the body's outline, "
                               "with the body on the +Z side? Feature deleted. hole_linear places "
                               "by a face instead.")

        # --- Add position dimensions in a separate sketch (after hole creation) ---
        # HoleFeature placement sketch must have ONLY SketchPoints — construction lines
        # cannot be in the same sketch. So we create a dedicated dimension sketch.
        self._add_hole_position_dims(comp, x, y)

        return f"Hole {type_str} {extent_str} at ({x},{y}): {feature.Name}{self._fmt_delta(before)}"

    def _add_hole_position_dims(self, comp, x: float, y: float) -> None:
        """Add a dimension sketch showing the hole center position from origin.
        Uses a separate sketch with construction line(s) to allow AddTwoPointDistance.
        Only adds dimensions if hole is not at origin.
        """
        tg = self._tg()
        x_cm = x / 10.0
        y_cm = y / 10.0

        if abs(x) < 0.01 and abs(y) < 0.01:
            return  # hole at origin — nothing to dimension

        wp = comp.WorkPlanes.Item(3)  # XY plane
        dim_sketch = comp.Sketches.Add(wp)
        dc = dim_sketch.DimensionConstraints

        if abs(y) < 0.01:
            # Hole on X axis — one horizontal construction line
            line = dim_sketch.SketchLines.AddByTwoPoints(
                tg.CreatePoint2d(0.0, 0.0),
                tg.CreatePoint2d(x_cm, 0.0)
            )
            line.Construction = True
            text_pt = tg.CreatePoint2d(x_cm / 2, -1.5)
            dc.AddTwoPointDistance(line.StartSketchPoint, line.EndSketchPoint,
                                   kHorizontalDim, text_pt)
        elif abs(x) < 0.01:
            # Hole on Y axis — one vertical construction line
            line = dim_sketch.SketchLines.AddByTwoPoints(
                tg.CreatePoint2d(0.0, 0.0),
                tg.CreatePoint2d(0.0, y_cm)
            )
            line.Construction = True
            text_pt = tg.CreatePoint2d(-1.5, y_cm / 2)
            dc.AddTwoPointDistance(line.StartSketchPoint, line.EndSketchPoint,
                                   kVerticalDim, text_pt)
        else:
            # Hole at (x, y) — two construction lines: H and V legs
            h_line = dim_sketch.SketchLines.AddByTwoPoints(
                tg.CreatePoint2d(0.0, 0.0),
                tg.CreatePoint2d(x_cm, 0.0)
            )
            h_line.Construction = True
            v_line = dim_sketch.SketchLines.AddByTwoPoints(
                tg.CreatePoint2d(x_cm, 0.0),
                tg.CreatePoint2d(x_cm, y_cm)
            )
            v_line.Construction = True
            # Horizontal dim (X distance from center)
            dc.AddTwoPointDistance(h_line.StartSketchPoint, h_line.EndSketchPoint,
                                   kHorizontalDim, tg.CreatePoint2d(x_cm / 2, -1.5))
            # Vertical dim (Y distance)
            dc.AddTwoPointDistance(v_line.StartSketchPoint, v_line.EndSketchPoint,
                                   kVerticalDim, tg.CreatePoint2d(x_cm + 1.5, y_cm / 2))

    # ---------- Diagnostics / token-saving helpers ----------

    @staticmethod
    def _fresh_mass(doc, comp):
        """MassProperties recomputed now. An assembly otherwise returns the value cached in
        the file — stale after its sub-assemblies changed (3607 kg vs 3343 kg seen).
        The recompute marks a document as changed although nothing in it was edited; a
        document that was clean stays clean, so that looking at a model does not leave
        Inventor asking to save it."""
        try:
            clean = not doc.Dirty
        except Exception:
            clean = False
        try:
            doc.Update()
        except Exception:
            pass
        if clean:
            try:
                if doc.Dirty:
                    doc.Dirty = False
            except Exception:
                pass
        mp = comp.MassProperties
        for prop, value in (("CacheResultsOnCompute", False), ("Accuracy", 25603)):  # kHigh
            try:
                setattr(mp, prop, value)
            except Exception:
                pass
        return mp

    def _vol_state(self):
        """(volume_mm3, faces, edges) of body 1, or None if no body."""
        try:
            comp = self.active_part()
            mp = comp.MassProperties
            body = comp.SurfaceBodies.Item(1)
            return (mp.Volume * 1000, body.Faces.Count, body.Edges.Count)
        except Exception:
            return None

    def _fmt_delta(self, before):
        """Compact ' | V 35271 (−7854) | F7 E16' suffix comparing to 'before' state."""
        after = self._vol_state()
        if not after:
            return ""
        if not before:
            # first feature — no 'before' body existed
            return f" | V {after[0]:.0f} mm³ | F{after[1]} E{after[2]}"
        dv = after[0] - before[0]
        return f" | V {after[0]:.0f} ({dv:+.0f} mm³) | F{after[1]} E{after[2]}"

    def inspect(self) -> dict:
        """Compact one-call snapshot of the active part."""
        doc = self.active_document()
        comp = doc.ComponentDefinition
        info: dict = {"document": doc.DisplayName}
        try:
            body = comp.SurfaceBodies.Item(1)
            rb = body.RangeBox
            mp = comp.MassProperties
            info["volume_mm3"] = round(mp.Volume * 1000)
            info["bbox_mm"] = {
                "x": [round(rb.MinPoint.X * 10, 2), round(rb.MaxPoint.X * 10, 2)],
                "y": [round(rb.MinPoint.Y * 10, 2), round(rb.MaxPoint.Y * 10, 2)],
                "z": [round(rb.MinPoint.Z * 10, 2), round(rb.MaxPoint.Z * 10, 2)],
            }
            info["faces"] = body.Faces.Count
            info["edges"] = body.Edges.Count
            shallow = self.faceting(body)
            if shallow >= 8:
                info["warning"] = (f"{shallow} shallow kinks between straight edges — curves look drawn as "
                                   "many short lines. Use arcs: draw_closed_profile radii / through, "
                                   "draw_arc, draw_slot, draw_rectangle(corner_radius), project_edges.")
        except Exception:
            info["body"] = "none"
        try:
            surfaces = self._surface_bodies(comp)
            if surfaces:
                info["surface_bodies"] = (f"{surfaces} surface body(ies) — no volume, so the mass is "
                                          "missing or too low. properties(mass_kg=…) sets it.")
        except Exception:
            pass
        feats = []
        for i in range(1, comp.Features.Count + 1):
            f = comp.Features.Item(i)
            nm = f.Name
            if getattr(f, "Suppressed", False):
                nm += " [suppressed]"
            health = unhealthy(self.app, f)
            if health:
                nm += f" [{health}]"
            feats.append(f"[{i}] {nm}")
        info["features"] = feats
        info["sketches"] = [comp.Sketches.Item(i).Name for i in range(1, comp.Sketches.Count + 1)]
        try:
            info["sheet_metal_thickness"] = str(comp.ActiveSheetMetalStyle.Thickness)
        except Exception:
            pass
        return info

    def list_edges(self, body_index: int = 1, min_length: float = 0, max_count: int = 60) -> str:
        """Compact edge table: '[i] L=200.0 mid=(0.0,-225.0,0.8)' one per line.
        min_length filters out tiny edges (mm)."""
        body = self.active_part().SurfaceBodies.Item(body_index)
        lines = []
        for i in range(1, body.Edges.Count + 1):
            try:
                e = body.Edges.Item(i)
                sv, ev = e.StartVertex.Point, e.StopVertex.Point
                L = ((ev.X - sv.X) ** 2 + (ev.Y - sv.Y) ** 2 + (ev.Z - sv.Z) ** 2) ** 0.5 * 10
                if L < min_length:
                    continue
                mx, my, mz = (sv.X + ev.X) / 2 * 10, (sv.Y + ev.Y) / 2 * 10, (sv.Z + ev.Z) / 2 * 10
                lines.append(f"[{i}] L={L:.1f} mid=({mx:.1f},{my:.1f},{mz:.1f})")
            except Exception:
                lines.append(f"[{i}] (closed curve)")
            if len(lines) >= max_count:
                lines.append(f"... truncated at {max_count}")
                break
        return "\n".join(lines) if lines else "(no edges match)"

    def list_faces(self, body_index: int = 1, min_area: float = 0, max_count: int = 60) -> str:
        """Compact face table: '[i] Plane A=90000 c=(0.0,225.0,12.5)' one per line.
        min_area in mm²."""
        type_names = {5890: "Plane", 5891: "Cyl", 5892: "EllCyl", 5893: "Cone",
                      5894: "EllCone", 5895: "Torus", 5896: "Sphere", 5897: "BSpline"}
        body = self.active_part().SurfaceBodies.Item(body_index)
        lines = []
        for i in range(1, body.Faces.Count + 1):
            try:
                f = body.Faces.Item(i)
                a = f.Evaluator.Area * 100
                if a < min_area:
                    continue
                xs, ys, zs = [], [], []
                for v in range(1, f.Vertices.Count + 1):
                    p = f.Vertices.Item(v).Point
                    xs.append(p.X * 10); ys.append(p.Y * 10); zs.append(p.Z * 10)
                t = type_names.get(f.SurfaceType, str(f.SurfaceType))
                if xs:
                    c = f"c=({sum(xs)/len(xs):.1f},{sum(ys)/len(ys):.1f},{sum(zs)/len(zs):.1f})"
                else:
                    c = "c=(closed)"
                lines.append(f"[{i}] {t} A={a:.0f} {c}")
            except Exception:
                lines.append(f"[{i}] ?")
            if len(lines) >= max_count:
                lines.append(f"... truncated at {max_count}")
                break
        return "\n".join(lines) if lines else "(no faces match)"

    # ---------- Transactions (rollback support) ----------

    def transaction(self, action: str = "begin", name: str = "MCP batch") -> str:
        """begin: start an undo-transaction wrapping subsequent operations.
        commit: finalize. abort: roll back EVERYTHING since begin (one undo unit)."""
        action = action.lower()
        if action == "begin":
            if getattr(self, "_txn", None) is not None:
                return "Transaction already open — commit or abort it first"
            self._txn = self.app.TransactionManager.StartTransaction(
                self.app.ActiveDocument, name)
            return f"Transaction '{name}' started"
        if action == "commit":
            if getattr(self, "_txn", None) is None:
                return "No open transaction"
            self._txn.End()
            self._txn = None
            return "Transaction committed"
        if action == "abort":
            if getattr(self, "_txn", None) is None:
                return "No open transaction"
            self._txn.Abort()
            self._txn = None
            return "Transaction aborted — all changes since 'begin' rolled back"
        return f"Unknown action '{action}'. Use begin / commit / abort."

    def _plane_face(self, axis: str, value: float, body_index: int = 1, tol: float = 0.1):
        """Largest planar face whose vertices ALL lie at <axis>==value (mm).
        Re-resolve after every feature — face objects/indices go stale."""
        body = self.active_part().SurfaceBodies.Item(body_index)
        best_f, best_a = None, 0
        for i in range(1, body.Faces.Count + 1):
            f = body.Faces.Item(i)
            if f.SurfaceType != 5890:  # plane
                continue
            ok = True
            for v in range(1, f.Vertices.Count + 1):
                p = f.Vertices.Item(v).Point
                c = {"x": p.X, "y": p.Y, "z": p.Z}[axis.lower()] * 10
                if abs(c - value) > tol:
                    ok = False
                    break
            if ok:
                a = f.Evaluator.Area
                if a > best_a:
                    best_a, best_f = a, f
        if best_f is None:
            raise ValueError(f"No planar face at {axis}={value}mm")
        return best_f

    def _face_edge(self, face, axis: str, value: float, tol: float = 0.1):
        """Edge of a face whose BOTH vertices lie at <axis>==value (mm)."""
        for i in range(1, face.Edges.Count + 1):
            e = face.Edges.Item(i)
            sv, ev = e.StartVertex.Point, e.StopVertex.Point
            cs = {"x": sv.X, "y": sv.Y, "z": sv.Z}[axis.lower()] * 10
            ce = {"x": ev.X, "y": ev.Y, "z": ev.Z}[axis.lower()] * 10
            if abs(cs - value) < tol and abs(ce - value) < tol:
                return e
        raise ValueError(f"No edge of face at {axis}={value}mm")

    def hole_linear(
        self,
        face_axis: str, face_value: float,
        ref1_axis: str, ref1_value: float, dist1: float,
        ref2_axis: str, ref2_value: float, dist2: float,
        diameter: float = 5.5,
        depth: float = 0,
        tapped: bool = False,
        pitch: float | None = None,
        cbore_diameter: float = 0,
        cbore_depth: float = 0,
    ) -> str:
        """Hole placed parametrically from two face edges (Linear placement).

        The hole stays attached to the edges — change the part length and the
        hole keeps its 8mm-from-edge distance. This is the preferred way to
        place fastener holes (vs sketch-point placement with absolute coords).

        face_axis/face_value: the planar face to drill into, e.g. ("z", 25) = top.
        ref1/ref2: two edges OF THAT FACE by their constant coordinate,
                   e.g. ("x", -34.5, 8) = 8mm from the left edge.
        depth: 0 = through-all, otherwise mm.
        tapped: ISO metric thread (uses standard coarse pitch unless pitch given).
        cbore_diameter/cbore_depth: >0 adds a counterbore (e.g. 10 and 5.5 for
                   an M5 ISO 4762 socket head cap screw seat).

        Direction is resolved automatically: try positive, check that material
        was actually removed (volume), flip to negative if not.
        """
        comp = self.active_part()
        tg = self._tg()
        hf = comp.Features.HoleFeatures
        doc = self.active_document()

        # Build the hole argument (diameter or TapInfo)
        if tapped:
            if pitch is None:
                dia_int = int(round(diameter))
                pitch = self._METRIC_COARSE_PITCH.get(dia_int)
                if pitch is None:
                    raise ValueError(f"No standard coarse pitch for M{dia_int}")
            dia_int = int(round(diameter))
            pitch_str = str(int(pitch)) if pitch == int(pitch) else str(pitch)
            hole_arg = hf.CreateTapInfo(
                True, "ISO Metric profile", f"M{dia_int}x{pitch_str}", "6H", True)
            type_str = f"M{dia_int} tapped"
        else:
            hole_arg = diameter / 10.0
            type_str = f"D={diameter}mm"

        v0 = comp.MassProperties.Volume * 1000

        for direction in (kPositiveExtentDirection, kNegativeExtentDirection):
            # Re-resolve topology fresh each attempt
            face = self._plane_face(face_axis, face_value)
            e1 = self._face_edge(face, ref1_axis, ref1_value)
            e2 = self._face_edge(face, ref2_axis, ref2_value)

            # BiasPoint: 3D point near the intended hole location. Compute by
            # stepping from each reference edge toward the face centroid.
            xs, ys, zs = [], [], []
            for v in range(1, face.Vertices.Count + 1):
                p = face.Vertices.Item(v).Point
                xs.append(p.X * 10); ys.append(p.Y * 10); zs.append(p.Z * 10)
            centroid = {"x": sum(xs)/len(xs), "y": sum(ys)/len(ys), "z": sum(zs)/len(zs)}
            coords = dict(centroid)
            coords[face_axis.lower()] = face_value
            sgn1 = 1 if centroid[ref1_axis.lower()] >= ref1_value else -1
            coords[ref1_axis.lower()] = ref1_value + sgn1 * dist1
            sgn2 = 1 if centroid[ref2_axis.lower()] >= ref2_value else -1
            coords[ref2_axis.lower()] = ref2_value + sgn2 * dist2
            bias = tg.CreatePoint(coords["x"] / 10, coords["y"] / 10, coords["z"] / 10)

            placement = hf.CreateLinearPlacementDefinition(
                face, e1, dist1 / 10.0, e2, dist2 / 10.0, bias)

            if cbore_diameter > 0:
                d_cm = depth / 10.0 if depth > 0 else 10.0  # cbore needs finite depth
                feature = hf.AddCBoreByDistanceExtent(
                    placement, hole_arg, d_cm, direction,
                    cbore_diameter / 10.0, cbore_depth / 10.0)
                extent_str = f"{depth}mm deep, cbore Ø{cbore_diameter}x{cbore_depth}"
            elif depth > 0:
                feature = hf.AddDrilledByDistanceExtent(
                    placement, hole_arg, depth / 10.0, direction)
                extent_str = f"{depth}mm deep"
            else:
                feature = hf.AddDrilledByThroughAllExtent(
                    placement, hole_arg, direction)
                extent_str = "through-all"

            doc.Update()
            v1 = comp.MassProperties.Volume * 1000
            if v1 < v0 - 1:  # material removed — direction correct
                return (f"Hole {type_str} {extent_str} on {face_axis}={face_value} "
                        f"({dist1}mm from {ref1_axis}={ref1_value}, "
                        f"{dist2}mm from {ref2_axis}={ref2_value}): {feature.Name}"
                        f" | V {v1:.0f} ({v1-v0:+.0f} mm³)")
            feature.Delete()
            doc.Update()

        raise RuntimeError("Hole removed no material in either direction — check placement")

    def circular_pattern(
        self,
        feature_index: int,
        count: int = 8,
        angle: float = 360,
        axis: str = "Z",
    ) -> str:
        """Create a circular pattern of a feature around a work axis.
        Distributes 'count' instances evenly within 'angle' degrees.
        """
        import math
        comp = self.active_part()
        cpf = comp.Features.CircularPatternFeatures

        # Get the feature to pattern
        feature = comp.Features.Item(feature_index)

        feat_col = self.app.TransientObjects.CreateObjectCollection()
        feat_col.Add(feature)

        # Work axis selection (X=1, Y=2, Z=3)
        axis_map = {"X": 1, "Y": 2, "Z": 3}
        axis_index = axis_map.get(axis.upper(), 3)
        axis_obj = comp.WorkAxes.Item(axis_index)

        angle_rad = math.radians(angle)

        pattern = cpf.Add(
            feat_col,    # ParentFeatures — ObjectCollection
            axis_obj,    # AxisEntity — WorkAxis
            True,        # NaturalAxisDirection
            count,       # Count
            angle_rad,   # Angle (radians, total span)
            True,        # FitWithinAngle — distribute evenly within angle
            47363,       # ComputeType = kOptimizedCompute (default)
        )

        return (
            f"Circular pattern: {count}x '{feature.Name}' "
            f"around {axis.upper()}-axis, {angle} deg: {pattern.Name}"
        )

    def _features(self, comp, features) -> tuple:
        """(ObjectCollection, names) for feature indices or names — a list, or "2,3" / "Hole1"."""
        if isinstance(features, (int, str)):
            features = [s.strip() for s in str(features).split(",") if s.strip()]
        if not features:
            raise ValueError("Name at least one feature (index or name from manage_features list)")
        col = self.app.TransientObjects.CreateObjectCollection()
        names = []
        for f in features:
            key = str(f).strip()
            try:
                feat = comp.Features.Item(int(key) if key.isdigit() else key)
            except Exception:
                have = ", ".join(f"[{i}] {comp.Features.Item(i).Name}" for i in range(1, comp.Features.Count + 1))
                raise ValueError(f"No feature {key!r}. Features: {have}") from None
            col.Add(feat)
            names.append(feat.Name)
        return col, names

    def _require_changed(self, feat, before, label: str, hint: str):
        """A pattern or mirror whose copies all miss the body is created without an error
        and changes nothing. Delete it and say so."""
        after = self._vol_state()
        if before and after and abs(after[0] - before[0]) < 1e-3 and after[1:] == before[1:]:
            name = feat.Name
            feat.Delete(False, False, False)
            raise RuntimeError(f"{label} '{name}' changed nothing and was deleted — {hint}")

    def rectangular_pattern(self, features, x_count: int = 2, x_spacing: float = 10, x_axis: str = "X",
                            y_count: int = 1, y_spacing: float = 0, y_axis: str = "Y") -> str:
        """Repeat features in rows: x_count instances x_spacing mm apart along x_axis, and
        optionally y_count rows y_spacing mm apart along y_axis. Counts include the original;
        a negative spacing goes the other way along the axis."""
        comp = self.active_part()
        col, names = self._features(comp, features)
        if x_count < 1 or y_count < 1 or x_count * y_count < 2:
            raise ValueError("A pattern needs at least 2 instances (x_count x y_count)")
        if (x_count > 1 and not x_spacing) or (y_count > 1 and not y_spacing):
            raise ValueError("Give the spacing for every direction that has more than one instance")
        axes = {"X": 1, "Y": 2, "Z": 3}
        try:
            ax, ay = comp.WorkAxes.Item(axes[x_axis.upper()]), comp.WorkAxes.Item(axes[y_axis.upper()])
        except KeyError:
            raise ValueError("x_axis and y_axis are X, Y or Z") from None
        if y_count > 1 and x_axis.upper() == y_axis.upper():
            raise ValueError("x_axis and y_axis must differ")
        before = self._vol_state()
        rpf = comp.Features.RectangularPatternFeatures
        d = rpf.CreateDefinition(col, ax, x_spacing >= 0, int(x_count), abs(x_spacing) / 10 or 1.0)
        if y_count > 1:
            d.YDirectionEntity = ay
            d.NaturalYDirection = y_spacing >= 0
            d.YCount = int(y_count)
            d.YSpacing = abs(y_spacing) / 10
        feat = rpf.AddByDefinition(d)
        self._require_changed(feat, before, "Rectangular pattern",
                              "every copy lies off the body; check spacing, sign and axes")
        grid = f"{x_count} x {x_spacing:g} mm along {x_axis.upper()}" + (
            f", {y_count} x {y_spacing:g} mm along {y_axis.upper()}" if y_count > 1 else "")
        return f"Rectangular pattern {feat.Name}: {', '.join(names)} — {grid}{self._fmt_delta(before)}"

    _ORIGIN_PLANES = {"YZ": 1, "XZ": 2, "XY": 3}

    def mirror(self, features="", plane: str = "YZ", body: bool = False, remove_original: bool = False) -> str:
        """Mirror features across an origin plane (XY / XZ / YZ) or a named work plane.
        body=True mirrors the whole solid instead — model one half, mirror it, and the two
        halves join; remove_original then keeps only the mirrored half (a left-hand part
        from a right-hand one)."""
        comp = self.active_part()
        key = plane.strip().upper()
        if key in self._ORIGIN_PLANES:
            wp = comp.WorkPlanes.Item(self._ORIGIN_PLANES[key])
        else:
            try:
                wp = comp.WorkPlanes.Item(plane.strip())
            except Exception:
                have = ", ".join(comp.WorkPlanes.Item(i).Name for i in range(1, comp.WorkPlanes.Count + 1))
                raise ValueError(f"No work plane {plane!r}. Use XY, XZ, YZ or one of: {have}") from None
        if body:
            if comp.SurfaceBodies.Count == 0:
                raise RuntimeError("There is no body to mirror yet")
            col = self.app.TransientObjects.CreateObjectCollection()
            col.Add(comp.SurfaceBodies.Item(1))
            names = ["the body"]
        else:
            if remove_original:
                raise ValueError("remove_original is for body=True")
            col, names = self._features(comp, features)
        before = self._vol_state()
        mf = comp.Features.MirrorFeatures
        d = mf.CreateDefinition(col, wp)
        if body and remove_original:
            d.RemoveOriginal = True
        feat = mf.AddByDefinition(d)
        if not (body and remove_original):
            self._require_changed(feat, before, "Mirror",
                                  "the mirrored copy lies off the body, or exactly on the original "
                                  "(the body is already symmetric about that plane)")
        return f"Mirror {feat.Name}: {', '.join(names)} across {wp.Name}{self._fmt_delta(before)}"

    # ---------- Parameters ----------

    def get_parameters(self) -> list[dict]:
        doc = self.active_document()
        params = doc.ComponentDefinition.Parameters
        result = []
        for i in range(1, params.Count + 1):
            p = params.Item(i)
            try:
                result.append({
                    "name": p.Name,
                    "value": p.Value,
                    "expression": p.Expression,
                    "unit": p.Units if hasattr(p, "Units") else "",
                })
            except Exception:
                pass
        return result

    def set_parameter(self, name: str, expression: str) -> str:
        doc = self.active_document()
        params = doc.ComponentDefinition.Parameters
        for i in range(1, params.Count + 1):
            p = params.Item(i)
            if p.Name.lower() == name.lower():
                p.Expression = expression
                doc.Update()
                return f"Parameter '{name}' set to '{expression}' (value={p.Value})"
        raise ValueError(f"Parameter '{name}' not found.")

    def add_parameter(self, name: str, expression: str, units: str = "mm") -> str:
        doc = self.active_document()
        params = doc.ComponentDefinition.Parameters.UserParameters
        params.AddByExpression(name, expression, units)
        doc.Update()
        return f"User parameter '{name}' = '{expression}' ({units}) added"

    # ---------- Query ----------

    def get_document_info(self) -> dict:
        doc = self.active_document()
        info = {
            "name": doc.DisplayName,
            "full_path": doc.FullFileName,
            "type_name": {kPartDocumentObject: "part", kAssemblyDocumentObject: "assembly",
                          kDrawingDocumentObject: "drawing"}.get(doc.DocumentType, "other"),
        }
        if doc.DocumentType == kDrawingDocumentObject:  # a drawing has no ComponentDefinition
            info["sheets_count"] = doc.Sheets.Count
            return info
        comp = doc.ComponentDefinition
        if doc.DocumentType == kAssemblyDocumentObject:
            info["components_count"] = comp.Occurrences.Count
            info["constraints_count"] = comp.Constraints.Count
            return info
        info["sketches_count"] = comp.Sketches.Count
        info["features_count"] = comp.Features.Count if hasattr(comp.Features, "Count") else 0
        info["bodies_count"] = comp.SurfaceBodies.Count
        if comp.SurfaceBodies.Count > 0:
            body = comp.SurfaceBodies.Item(1)
            info["edges_count"] = body.Edges.Count
            info["faces_count"] = body.Faces.Count
        return info

    def list_features(self) -> list[dict]:
        comp = self.active_part()
        result = []
        for i in range(1, comp.Features.Count + 1):
            f = comp.Features.Item(i)
            result.append({
                "index": i,
                "name": f.Name,
                "type": str(f.Type),
                "suppressed": f.Suppressed if hasattr(f, "Suppressed") else False,
            })
            health = unhealthy(self.app, f)
            if health:
                result[-1]["health"] = health
        return result

    def delete_feature(self, indices: list[int]) -> str:
        """Delete features by index. Always deletes in reverse order to avoid index shifting."""
        comp = self.active_part()
        total = comp.Features.Count
        for idx in sorted(indices, reverse=True):
            if idx < 1 or idx > total:
                raise ValueError(f"Feature index {idx} out of range (1–{total})")
            name = comp.Features.Item(idx).Name
            comp.Features.Item(idx).Delete()
            total -= 1
        deleted = sorted(indices)
        return f"Deleted {len(indices)} feature(s): indices {deleted}"

    def suppress_feature(self, indices: list[int], suppressed: bool = True) -> str:
        """Suppress or unsuppress features by index."""
        comp = self.active_part()
        names = []
        for idx in indices:
            f = comp.Features.Item(idx)
            f.Suppressed = suppressed
            names.append(f.Name)
        state = "Suppressed" if suppressed else "Unsuppressed"
        return f"{state}: {', '.join(names)}"
