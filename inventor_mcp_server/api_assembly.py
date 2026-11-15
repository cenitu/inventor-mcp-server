"""Assemblies: placing, constraints, clearance, motion, faces of components."""
if __package__:
    from .api_common import *  # noqa: F401,F403
    from .flat_checks import flat_pattern_problems, rule_from_name  # noqa: F401
else:  # server.py run as a script: the modules sit on sys.path
    from api_common import *  # noqa: F401,F403
    from flat_checks import flat_pattern_problems, rule_from_name  # noqa: F401


class AssemblyMixin:
    # ---------- Assembly (IAM) ----------

    # Origin geometry of a component, addressable by name. Work planes/axes are
    # indexed 1..3 in YZ/XZ/XY and X/Y/Z order respectively (same convention as
    # parts, see create_sketch).
    _ASM_GEOM = {
        "YZ": ("WorkPlanes", 1), "XZ": ("WorkPlanes", 2), "XY": ("WorkPlanes", 3),
        "X": ("WorkAxes", 1), "Y": ("WorkAxes", 2), "Z": ("WorkAxes", 3),
        "ORIGIN": ("WorkPoints", 1),
    }

    def create_assembly(self, name: str | None = None, template: str = "metric") -> str:
        """Create a new empty assembly document (.iam).

        template: "metric" (Standard (mm).iam), "english", or a full .iam path.
        """
        import glob

        templates_path = self.app.FileLocations.TemplatesPath
        if template.lower() == "metric":
            patterns = [
                os.path.join(templates_path, "Metric", "Standard (mm).iam"),
                os.path.join(templates_path, "**", "Standard (mm).iam"),
            ]
        elif template.lower() == "english":
            patterns = [
                os.path.join(templates_path, "en-US", "Standard.iam"),
                os.path.join(templates_path, "**", "Standard.iam"),
            ]
        else:
            if not os.path.exists(template):
                raise RuntimeError(f"Template not found: {template}")
            patterns = [template]

        template_file = None
        for pattern in patterns:
            if "*" in pattern:
                found = glob.glob(pattern, recursive=True)
                if found:
                    template_file = found[0]
                    break
            elif os.path.exists(pattern):
                template_file = pattern
                break
        if template_file is None:
            fallback = glob.glob(os.path.join(templates_path, "**", "*.iam"), recursive=True)
            if not fallback:
                raise RuntimeError(f"Cannot find an assembly template in {templates_path}")
            template_file = fallback[0]

        doc = self.app.Documents.Add(kAssemblyDocumentObject, template_file, True)
        if name:
            try:
                doc.DisplayName = name
            except Exception:
                pass  # read-only until first save
        doc.Activate()
        return f"Assembly created: {doc.DisplayName} (unsaved — use save_document(path))"

    def _placement(self, x=0.0, y=0.0, z=0.0, x_axis=None, y_axis=None):
        """Matrix putting a component's origin at (x,y,z) mm with its X/Y axes along
        x_axis/y_axis (Z = X × Y). No axes: no rotation."""
        tg = self._tg()
        matrix = tg.CreateMatrix()
        if x_axis is not None or y_axis is not None:
            if x_axis is None or y_axis is None:
                raise ValueError("Give both x_axis and y_axis (or neither).")
            ax, ay = [float(v) for v in x_axis], [float(v) for v in y_axis]
            if abs(sum(a * b for a, b in zip(ax, ay))) > 1e-6:
                raise ValueError(f"x_axis {ax} and y_axis {ay} must be perpendicular.")
            az = (ax[1]*ay[2] - ax[2]*ay[1], ax[2]*ay[0] - ax[0]*ay[2], ax[0]*ay[1] - ax[1]*ay[0])
            matrix.SetCoordinateSystem(tg.CreatePoint(x / 10.0, y / 10.0, z / 10.0),
                                       tg.CreateVector(*ax), tg.CreateVector(*ay), tg.CreateVector(*az))
        elif x or y or z:
            matrix.SetTranslation(tg.CreateVector(x / 10.0, y / 10.0, z / 10.0))
        return matrix

    def _existing_placements(self, comp) -> set:
        keys = set()
        for i in range(1, comp.Occurrences.Count + 1):
            o = comp.Occurrences.Item(i)
            try:
                keys.add(self._placement_key(o.Definition.Document.FullFileName, o.Transformation))
            except Exception:
                pass
        return keys

    @staticmethod
    def _placement_key(path, m) -> tuple:
        return (os.path.normcase(os.path.abspath(path)),
                *(round(m.Cell(r, c) * (10 if c == 4 else 1), 2) for r in (1, 2, 3) for c in (1, 2, 3, 4)))

    def place_many(self, items: list[dict], atomic: bool = True, skip_if_exists: bool = False) -> str:
        """Place many components in one call: [{path, x, y, z, x_axis, y_axis, grounded, name}].
        x/y/z (or origin=[x,y,z]) in mm. Every entry is checked before anything is placed.
        atomic: one undo step — if a placement fails, none stay, so running the list again
        doesn't double what was placed. skip_if_exists: leave out what already sits there."""
        comp = self.active_assembly()
        missing = sorted({os.path.abspath(it["path"]) for it in items
                          if not os.path.exists(os.path.abspath(it.get("path", "")))})
        if missing:
            raise RuntimeError("Files not found (nothing placed): " + ", ".join(missing))
        placed, failed, skipped = [], [], 0
        have = self._existing_placements(comp) if skip_if_exists else set()
        txn = None
        if atomic:
            txn = self.app.TransactionManager.StartTransaction(self.active_document(), "MCP place_components")
        for n, it in enumerate(items, 1):
            try:
                x, y, z = it.get("origin") or (it.get("x", 0), it.get("y", 0), it.get("z", 0))
                where = self._placement(float(x), float(y), float(z), it.get("x_axis"), it.get("y_axis"))
                if skip_if_exists and self._placement_key(it["path"], where) in have:
                    skipped += 1
                    continue
                occ = comp.Occurrences.Add(os.path.abspath(it["path"]), where)
                if it.get("grounded"):
                    occ.Grounded = True
                if it.get("name"):
                    try:
                        occ.Name = it["name"]
                    except Exception:
                        failed.append(f"#{n} placed as {occ.Name}, name {it['name']!r} taken")
                placed.append(occ.Name)
            except Exception as e:
                failed.append(f"#{n} {os.path.basename(it['path'])}: {e}")
                if txn is not None:
                    txn.Abort()
                    raise RuntimeError(f"Nothing placed (atomic) — {failed[-1]}") from None
        if txn is not None:
            txn.End()
        msg = f"Placed {len(placed)} of {len(items)} ({comp.Occurrences.Count} occurrences now)"
        if skipped:
            msg += f", {skipped} already there"
        if len(placed) <= 12:
            msg += f": {', '.join(placed)}"
        return msg + ("\nProblems:\n  " + "\n  ".join(failed) if failed else "")

    def place_component(
        self,
        path: str,
        x: float = 0, y: float = 0, z: float = 0,
        grounded: bool = False,
        x_axis: tuple | None = None,
        y_axis: tuple | None = None,
    ) -> str:
        """Place a part or sub-assembly into the active assembly at (x,y,z) mm.

        x_axis / y_axis: where the component's own X and Y axes point in the
        assembly, e.g. x_axis=(0,0,1), y_axis=(0,1,0). Z follows (X × Y).
        Omit both for no rotation. Lets vendor STEP models (whose axes rarely
        match yours) be placed exactly without constraints.

        The file must exist on disk — save parts before placing them.
        Returns the occurrence index (1-based) for use in constraints.
        """
        comp = self.active_assembly()
        path = os.path.abspath(path)
        if not os.path.exists(path):
            raise RuntimeError(f"Component file not found: {path}")
        occ = comp.Occurrences.Add(path, self._placement(x, y, z, x_axis, y_axis))
        if grounded:
            occ.Grounded = True

        idx = comp.Occurrences.Count
        state = ", grounded" if grounded else ""
        return (f"Placed '{occ.Name}' at ({x},{y},{z})mm{state} "
                f"[occurrence {idx} of {comp.Occurrences.Count}]")

    # ---------- Vendor parts, clearance checks, motion ----------

    def import_step(self, step_path: str, dest_folder: str, name: str | None = None) -> dict:
        """Import a vendor STEP file as Inventor files inside dest_folder.

        Inventor writes the parts of an imported STEP assembly into a folder NEXT
        TO THE .step FILE (e.g. Downloads\\x\\1.ipt ... 7.ipt) — which may be a
        synced or shared folder. So the STEP is first copied into dest_folder and
        imported from there; nothing is written beside the original.
        Returns a summary (path, type, size, components with bbox/volume).
        """
        import shutil
        step_path = os.path.abspath(step_path)
        dest_folder = os.path.abspath(dest_folder)
        if not os.path.exists(step_path):
            raise RuntimeError(f"STEP file not found: {step_path}")
        os.makedirs(dest_folder, exist_ok=True)
        local = os.path.join(dest_folder, os.path.basename(step_path))
        if os.path.normcase(local) != os.path.normcase(step_path):
            shutil.copy2(step_path, local)
        doc = self.app.Documents.Open(local, True)
        doc.Activate()
        base = name or os.path.splitext(os.path.basename(step_path))[0]
        is_asm = doc.DocumentType == kAssemblyDocumentObject
        target = os.path.join(dest_folder, base + (".iam" if is_asm else ".ipt"))
        stray = []
        for ref in list(doc.AllReferencedDocuments):
            if not os.path.normcase(ref.FullFileName or "").startswith(os.path.normcase(dest_folder)):
                stray.append(ref.FullFileName)
        if stray:
            raise RuntimeError(f"Import wrote files outside {dest_folder}: {stray}")
        self._silently(lambda: doc.SaveAs(target, False))
        cd = doc.ComponentDefinition

        def bb(r):
            return [round(v * 10, 2) for v in (r.MinPoint.X, r.MaxPoint.X, r.MinPoint.Y,
                                                r.MaxPoint.Y, r.MinPoint.Z, r.MaxPoint.Z)]
        out = {"path": target, "type": "assembly" if is_asm else "part", "bbox_mm": bb(cd.RangeBox)}
        if is_asm:
            comps = []
            for o in cd.Occurrences:
                try:
                    vol = round(o.MassProperties.Volume * 1000)
                except Exception:
                    vol = None
                comps.append({"name": o.Name, "bbox_mm": bb(o.RangeBox), "volume_mm3": vol})
            out["components"] = comps
        return out

    def _occ(self, name: str):
        """Occurrence by name in the active assembly; 'Sub:1/2' reaches a
        sub-occurrence (e.g. one part of a vendor hinge assembly)."""
        comp = self.active_assembly()
        name = str(name)
        if name.isdigit():
            return comp.Occurrences.Item(int(name))
        parts = name.split("/")
        coll = comp.Occurrences
        occ = None
        for part in parts:
            matches = [o for o in (coll if occ is None else occ.SubOccurrences) if o.Name == part]
            if not matches:
                have = ", ".join(o.Name for o in (coll if occ is None else occ.SubOccurrences))
                raise ValueError(f"No occurrence '{part}'. Available: {have}")
            occ = matches[0]
        return occ

    def _occ_set(self, names: list[str]) -> list:
        """Occurrences for names, paths or patterns (* ?) on the full path of every part."""
        import fnmatch
        out, leaves = [], None
        for n in names:
            if not any(c in n for c in "*?"):
                out.append(self._occ(n))
                continue
            if leaves is None:
                ls = self.active_assembly().Occurrences.AllLeafOccurrences
                leaves = [(self._path_of(ls.Item(i)), ls.Item(i)) for i in range(1, ls.Count + 1)]
            hits = [o for p, o in leaves if fnmatch.fnmatch(p.lower(), n.lower())]
            if not hits:
                raise ValueError(f"No component matches {n!r}")
            out += hits
        return out

    def _leaf_volumes(self, comp) -> dict:
        """Volume of every part occurrence as this assembly sees it (mm³): assembly cuts count."""
        vols = {}
        leaves = comp.Occurrences.AllLeafOccurrences
        for i in range(1, leaves.Count + 1):
            o = leaves.Item(i)
            bodies = o.SurfaceBodies
            vols[self._path_of(o)] = sum(bodies.Item(j).Volume(0.001) for j in range(1, bodies.Count + 1)) * 1000
        return vols

    # Sketch axes of the plane square to each world axis; x × y is the +axis direction
    _CUT_PLANES = {"X": ((0, 1, 0), (0, 0, 1)), "Y": ((0, 0, 1), (1, 0, 0)), "Z": ((1, 0, 0), (0, 1, 0))}

    def assembly_cut(self, axis: str = "z", circles: list | None = None, rectangles: list | None = None,
                     at: float = 0, distance: float = 0, direction: str = "symmetric",
                     participants: list[str] | None = None) -> str:
        """Cut placed components in this assembly only: the part files stay as they are.
        Shapes are in world mm and are projected onto the plane at `at` along `axis`."""
        comp = self.active_assembly()
        ax = axis.upper()
        if ax not in self._CUT_PLANES:
            raise ValueError(f"Unknown axis '{axis}'. Use x, y or z.")
        if not circles and not rectangles:
            raise ValueError("Nothing to cut: give circles and/or rectangles.")
        extent = {"positive": kPositiveExtentDirection, "negative": kNegativeExtentDirection,
                  "symmetric": kSymmetricExtentDirection}.get(direction.lower())
        if extent is None:
            raise ValueError(f"Unknown direction '{direction}'. Use positive, negative or symmetric.")
        wanted = self._occ_set(participants) if participants else None
        doc = self.active_document()
        tg = self.app.TransientGeometry
        n = "XYZ".index(ax)
        origin = [at / 10 if i == n else 0 for i in range(3)]
        xv, yv = self._CUT_PLANES[ax]
        plane = comp.WorkPlanes.AddFixed(tg.CreatePoint(*origin), tg.CreateUnitVector(*xv), tg.CreateUnitVector(*yv))
        plane.Visible = False
        sk = comp.Sketches.Add(plane)

        def on_sketch(p):
            q = [v / 10 for v in p[:3]]
            q[n] = at / 10
            return sk.ModelToSketchSpace(tg.CreatePoint(*q))

        for x, y, z, dia in circles or []:
            sk.SketchCircles.AddByCenterRadius(on_sketch((x, y, z)), dia / 20)
        for corners in rectangles or []:
            first = sk.SketchLines.AddByTwoPoints(on_sketch(corners[0]), on_sketch(corners[1]))
            line = first
            for c in corners[2:]:
                line = sk.SketchLines.AddByTwoPoints(line.EndSketchPoint, on_sketch(c))
            sk.SketchLines.AddByTwoPoints(line.EndSketchPoint, first.StartSketchPoint)
        features = comp.Features.ExtrudeFeatures
        d = features.CreateExtrudeDefinition(sk.Profiles.AddForSolid(), kCutOperation)
        if distance:
            d.SetDistanceExtent(distance / 10, extent)
        else:
            d.SetThroughAllExtent(extent)
        before = self._leaf_volumes(comp)
        feat, cut = None, {}
        try:
            feat = features.Add(d)
            if wanted is not None:
                keep = {self._path_of(o) for o in wanted}
                for o in list(feat.Participants):
                    if self._path_of(o) not in keep:
                        feat.RemoveParticipant(o)
                have = {self._path_of(o) for o in feat.Participants}
                for o in wanted:
                    if self._path_of(o) not in have:
                        feat.AddParticipant(o)
            doc.Update()
            after = self._leaf_volumes(comp)
            cut = {p: before[p] - v for p, v in after.items() if before.get(p, v) - v > 0.01}
        except Exception as e:
            raise RuntimeError(f"Assembly cut failed, nothing was left behind — does the shape "
                               f"meet a component? ({e})") from e
        finally:
            if not cut:
                if feat is not None:
                    feat.Delete()
                sk.Delete()
                plane.Delete()
        if not cut:
            raise RuntimeError("The cut removed no material from any component, so it was taken "
                               "back. Check axis, at, direction and the coordinates.")
        lines = "; ".join(f"{p} -{v:.0f} mm³" for p, v in cut.items())
        return f"Assembly cut {feat.Name}: {lines}. Part files unchanged."

    @staticmethod
    def _is_fastener(occ) -> bool:
        """A small Content Center member — bolt, nut, washer, rivet."""
        try:
            if not occ.Definition.IsContentMember:
                return False
            rb = occ.RangeBox
            return max(rb.MaxPoint.X - rb.MinPoint.X, rb.MaxPoint.Y - rb.MinPoint.Y,
                       rb.MaxPoint.Z - rb.MinPoint.Z) * 10 < 150
        except Exception:
            return False

    def check_interference(self, set_a: list[str], set_b: list[str] | None = None,
                           ignore_fasteners: bool = False) -> list[dict]:
        """Interference between two sets of occurrences (names; 'Asm:1/2' for a
        sub-part). set_b empty = everything else. Returns overlap volume and
        bounding box per pair — touching faces are not interference."""
        comp = self.active_assembly()
        a = self._occ_set(set_a)
        a_names = {o.Name for o in a}
        b = self._occ_set(set_b) if set_b else [o for o in comp.Occurrences if o.Name not in a_names]
        ca = self.app.TransientObjects.CreateObjectCollection()
        for o in a:
            ca.Add(o)
        cb = self.app.TransientObjects.CreateObjectCollection()
        for o in b:
            cb.Add(o)
        res = comp.AnalyzeInterference(ca, cb)
        out = []
        for i in range(1, res.Count + 1):
            r = res.Item(i)
            rb = r.InterferenceBody.RangeBox
            hit = {"a": self._path_of(r.OccurrenceOne), "b": self._path_of(r.OccurrenceTwo),
                   "volume_mm3": round(r.Volume * 1000, 3),
                   "bbox_mm": [round(v * 10, 2) for v in (rb.MinPoint.X, rb.MaxPoint.X, rb.MinPoint.Y,
                                                           rb.MaxPoint.Y, rb.MinPoint.Z, rb.MaxPoint.Z)]}
            if ignore_fasteners and (self._is_fastener(r.OccurrenceOne) or self._is_fastener(r.OccurrenceTwo)):
                hit["fastener"] = True
            out.append(hit)
        return out

    def measure_distance(self, a: str, b: str) -> float:
        """Minimum distance in mm between two occurrences (0 = touching or overlapping)."""
        self.active_assembly()
        return round(self.app.MeasureTools.GetMinimumDistance(self._occ(a), self._occ(b))[0] * 10, 3)

    def sweep_rotation(
        self,
        moving: list[str],
        axis_point: tuple, axis_dir: tuple,
        start: float = 0, stop: float = 110, step: float = 10,
        against: list[str] | None = None,
        measure_to: str | None = None,
    ) -> list[dict]:
        """Rotate occurrences about an axis in steps and check for collisions —
        e.g. a door on its hinge axis, or a lock tongue on its shaft.

        moving: occurrences that rotate together (door, lock, door-mounted studs).
        axis_point (mm) / axis_dir: the rotation axis. Angles in degrees.
        against: occurrences to test (default: everything else).
        measure_to: optional occurrence to report the minimum distance to.
        The original positions are always restored.
        """
        import math
        comp = self.active_assembly()
        tg = self._tg()
        movers = [self._occ(n) for n in moving]
        base = [o.Transformation for o in movers]
        pt = tg.CreatePoint(*(v / 10.0 for v in axis_point))
        vec = tg.CreateVector(*axis_dir)
        target = self._occ(measure_to) if measure_to else None
        rows = []
        try:
            ang = start
            while ang <= stop + 1e-9:
                rot = tg.CreateMatrix()
                rot.SetToRotation(math.radians(ang), vec, pt)
                for o, m0 in zip(movers, base):
                    m = m0.Copy()
                    m.TransformBy(rot)
                    o.Transformation = m
                hits = self.check_interference(moving, against)
                row = {"angle": round(ang, 3), "collisions": len(hits),
                       "volume_mm3": round(sum(h["volume_mm3"] for h in hits), 3),
                       "with": sorted({h["b"] for h in hits})}
                if target is not None:
                    row["min_distance_mm"] = round(min(
                        self.app.MeasureTools.GetMinimumDistance(o, target)[0] for o in movers) * 10, 2)
                rows.append(row)
                ang += step
        finally:
            for o, m0 in zip(movers, base):
                o.Transformation = m0
            self.active_document().Update()
        return rows

    def list_occurrences(self, depth: int = 1) -> str:
        """Compact table of the assembly's components:
        '[i] block:1  pos=(0.0,0.0,0.0) grounded'. depth > 1 also lists what is inside
        sub-assemblies, indented, with the path ("Sub:1/Part:1") other tools take.
        """
        comp = self.active_assembly()
        lines = []

        def row(occ, label, level, path):
            t = occ.Transformation.Translation  # top assembly coordinates, also when nested
            flags = []
            if occ.Grounded:
                flags.append("grounded")
            if occ.Suppressed:
                flags.append("suppressed")
            if occ.SubOccurrences.Count:
                flags.append(f"sub-assembly, {occ.SubOccurrences.Count} inside")
            lines.append(f"{'  ' * level}{label} {occ.Name}  pos=({t.X * 10:.1f},{t.Y * 10:.1f},{t.Z * 10:.1f})"
                         + ("  " + " ".join(flags) if flags else "")
                         + (f"  path={path}" if level else ""))
            if level + 1 < depth and not occ.Suppressed:
                subs = occ.SubOccurrences
                for j in range(1, subs.Count + 1):
                    s = subs.Item(j)
                    row(s, "-", level + 1, f"{path}/{s.Name}")

        for i in range(1, comp.Occurrences.Count + 1):
            occ = comp.Occurrences.Item(i)
            row(occ, f"[{i}]", 0, occ.Name)
        return "\n".join(lines) if lines else "(no components placed)"

    def ground_occurrence(self, index, grounded: bool = True) -> str:
        """Ground (fix in space) or unground a component. Ground the first
        component of every assembly — otherwise constraints move it instead."""
        occ = self._occ(index)
        occ.Grounded = grounded
        return f"{'Grounded' if grounded else 'Ungrounded'}: {occ.Name}"

    # ---------- Faces of nested components, constraints by face ----------
    #
    # A component is named by its path from the top assembly, "Sub:1/Part:1". Faces come from
    # the occurrence's SurfaceBodies — proxies, so geometry is in TOP assembly coordinates and
    # the face can be constrained there directly. A face is referenced as "path#F12" (body 1)
    # or "path#B2F3"; origin geometry as "path#XY" / "#X" / "#ORIGIN".

    _SURFACES = {5890: "plane", 5891: "cylinder", 5892: "elliptic cylinder", 5893: "cone",
                 5895: "torus", 5896: "sphere", 5897: "bspline"}

    def _leaves(self, occ, path: str):
        """Part occurrences at and below occ (whose path is `path`), with their paths."""
        out = []

        def walk(o, path):
            subs = o.SubOccurrences
            if subs.Count:
                for i in range(1, subs.Count + 1):
                    s = subs.Item(i)
                    walk(s, f"{path}/{s.Name}")
            else:
                out.append((path, o))
        walk(occ, path)
        return out

    def _face_info(self, face) -> dict:
        st = face.SurfaceType
        rb = face.Evaluator.RangeBox
        center = [round((a + b) / 2 * 10, 3) for a, b in ((rb.MinPoint.X, rb.MaxPoint.X),
                                                           (rb.MinPoint.Y, rb.MaxPoint.Y),
                                                           (rb.MinPoint.Z, rb.MaxPoint.Z))]
        info = {"type": self._SURFACES.get(st, str(st)),
                "area_mm2": round(face.Evaluator.Area * 100, 1), "center": center}
        g = face.Geometry
        if st == 5890:
            n = g.Normal
            s = -1 if face.IsParamReversed else 1  # outward normal of the face
            info["normal"] = [round(s * n.X, 4) + 0.0, round(s * n.Y, 4) + 0.0, round(s * n.Z, 4) + 0.0]
        elif st in (5891, 5893):
            a, p = g.AxisVector, g.BasePoint
            info["axis"] = [round(a.X, 4) + 0.0, round(a.Y, 4) + 0.0, round(a.Z, 4) + 0.0]
            info["axis_point"] = [round(p.X * 10, 3), round(p.Y * 10, 3), round(p.Z * 10, 3)]
            if st == 5891:
                info["diameter"] = round(g.Radius * 20, 3)
        return info

    @staticmethod
    def _face_matches(info, surface, normal, at, diameter, min_area, tolerance) -> bool:
        import math
        if surface and info["type"] != surface.lower():
            return False
        if info["area_mm2"] < min_area:
            return False
        if normal is not None:
            ln = math.sqrt(sum(float(c) ** 2 for c in normal))
            if "normal" not in info or sum(a * float(b) / ln for a, b in zip(info["normal"], normal)) < 0.9998:
                return False
        if at and any(abs(info["center"]["XYZ".index(k.upper())] - float(v)) > tolerance for k, v in at.items()):
            return False
        return not diameter or abs(info.get("diameter", -1) - diameter) <= tolerance

    def assembly_faces(self, path: str, surface: str = "", normal=None, at: dict | None = None,
                       diameter: float = 0, min_area: float = 0, max_count: int = 30,
                       tolerance: float = 0.05) -> list[dict]:
        """Faces of a component (and everything below it, for a sub-assembly), in top
        assembly coordinates, filtered: surface type, outward normal (planes), center
        coordinates at={"Z": 182.5}, cylinder diameter, minimum area. Each with a ref for
        constrain_faces."""
        import math
        leaves = self._leaves(self._occ(path), path)
        out = []
        for leaf_path, occ in leaves:
            bodies = occ.SurfaceBodies
            for bi in range(1, bodies.Count + 1):
                faces = bodies.Item(bi).Faces
                for fi in range(1, faces.Count + 1):
                    f = faces.Item(fi)
                    st = self._SURFACES.get(f.SurfaceType, "")
                    if surface and st != surface.lower():
                        continue
                    info = self._face_info(f)
                    if not self._face_matches(info, "", normal, at, diameter, min_area, tolerance):
                        continue
                    info["ref"] = f"{leaf_path}#{'B%d' % bi if bi > 1 else ''}F{fi}"
                    out.append(info)
                    if len(out) >= max_count:
                        return out
        return out

    def _ref(self, ref: str):
        """(geometry in the top assembly, occurrence) for "path#F12" / "path#B2F3" / "path#XY"."""
        import re
        path, _, geo = ref.partition("#")
        if not geo:
            raise ValueError(f"{ref!r}: give a face or origin geometry after '#', e.g. '{ref}#F3' or '{ref}#XY'")
        occ = self._occ(path)
        g = geo.upper()
        if g in ("XY", "XZ", "YZ", "X", "Y", "Z", "ORIGIN"):
            return self._origin_proxy(occ, g), occ
        m = re.fullmatch(r"(?:B(\d+))?([FE])(\d+)", g)
        if not m:
            raise ValueError(f"{ref!r}: after '#' use F<n>, E<n>, B<n>F<n> or XY/XZ/YZ/X/Y/Z/ORIGIN")
        bodies = occ.SurfaceBodies
        if not bodies.Count:
            raise ValueError(f"{path} has no bodies — faces belong to parts; give the path down to one")
        body = bodies.Item(int(m[1] or 1))
        return (body.Faces if m[2] == "F" else body.Edges).Item(int(m[3])), occ

    def _poses(self, comp) -> dict:
        """Position and axes of every part occurrence (mm), to see what a change moved."""
        poses = {}
        leaves = comp.Occurrences.AllLeafOccurrences
        for i in range(1, leaves.Count + 1):
            o = leaves.Item(i)
            m = o.Transformation
            poses[self._path_of(o)] = [round(m.Cell(r, c) * (10 if c == 4 else 1), 4)
                                       for r in (1, 2, 3) for c in (1, 2, 3, 4)]
        return poses

    @staticmethod
    def _path_of(occ) -> str:
        try:
            path = occ.OccurrencePath
            return "/".join(path.Item(i).Name for i in range(1, path.Count + 1))
        except Exception:
            return occ.Name

    @staticmethod
    def _moved(before: dict, after: dict, skip: set) -> list[str]:
        """What changed between two _poses, leaving out the components in skip."""
        moved = []
        for p, m in after.items():
            old = before.get(p)
            if old is None or p in skip:
                continue
            d = [round(m[i] - old[i], 3) for i in (3, 7, 11)]
            turned = any(abs(m[i] - old[i]) > 1e-4 for i in (0, 1, 2, 4, 5, 6, 8, 9, 10))
            if any(abs(v) > 0.01 for v in d) or turned:
                moved.append(f"{p} Δ({d[0]}, {d[1]}, {d[2]}) mm" + (" and turned" if turned else ""))
        return moved

    def constrain_faces(self, a: str, b: str, kind: str = "mate", offset: float = 0,
                        angle: float = 0, a_at: dict | None = None, axes_opposed: bool = True) -> str:
        """Constrain two faces / origin geometries of components anywhere in the assembly
        (refs from assembly_faces, or "path#XY"). a_at={"Z": 182.5}: where a's face must end
        up — if it lands elsewhere, the offset is tried with the other sign. Reports every
        other component the constraint moved."""
        import math
        comp = self.active_assembly()
        doc = self.active_document()
        ga, occ_a = self._ref(a)
        gb, occ_b = self._ref(b)
        k = kind.lower()
        before = self._poses(comp)

        def add(off):
            if k == "mate":
                return comp.Constraints.AddMateConstraint(ga, gb, off / 10.0)
            if k == "flush":
                return comp.Constraints.AddFlushConstraint(ga, gb, off / 10.0)
            if k == "angle":
                return comp.Constraints.AddAngleConstraint(ga, gb, math.radians(angle))
            if k == "insert":
                return comp.Constraints.AddInsertConstraint(ga, gb, axes_opposed, off / 10.0)
            raise ValueError(f"Unknown kind '{kind}'. Use mate, flush, angle or insert.")

        def where():
            ga2, _ = self._ref(a)  # re-resolve: the proxy is fresh after the update
            info = self._face_info(ga2) if hasattr(ga2, "SurfaceType") else None
            return info["center"] if info else None

        def lands():
            if not a_at:
                return True
            c = where()
            return c is not None and all(abs(c["XYZ".index(ax.upper())] - float(v)) < 0.05 for ax, v in a_at.items())

        c = add(offset)
        doc.Update()
        flipped = False
        if not lands() and offset and k in ("mate", "flush", "insert"):
            c.Delete()
            c = add(-offset)
            doc.Update()
            flipped = True
        note = ""
        if a_at and not lands():
            note = f"\nWARNING: {a} is at {where()}, not at {a_at} — check the faces and the offset."
        elif flipped:
            note = f"\n(offset {offset} put it on the wrong side; used {-offset} — {a} is now at {a_at})"
        health = ""
        try:
            h = health_name(self.app, c.HealthStatus)
            if h not in ("kUpToDateHealth", "11777"):
                health = f"\nConstraint health: {h} — it conflicts with others or can't be solved."
        except Exception:
            pass
        moved = self._moved(before, self._poses(comp), {self._path_of(occ_a), self._path_of(occ_b)})
        also = ("\nALSO MOVED (other constraints dragged them): " + "; ".join(moved)) if moved else ""
        what = f"{angle}°" if k == "angle" else f"offset {-offset if flipped else offset}mm"
        return f"{k.capitalize()} '{c.Name}': {a} ↔ {b} ({what}){note}{health}{also}"

    def _origin_proxy(self, occ, geometry: str):
        """Resolve 'XY' / 'X' / 'ORIGIN' on an occurrence to an assembly-context
        geometry proxy. Constraints need the PROXY, not the part-level entity —
        passing the raw work plane silently constrains the wrong context."""
        key = geometry.upper()
        if key not in self._ASM_GEOM:
            raise ValueError(
                f"Unknown geometry '{geometry}'. Use one of: "
                + ", ".join(self._ASM_GEOM)
            )
        collection, idx = self._ASM_GEOM[key]
        entity = getattr(occ.Definition, collection).Item(idx)
        return occ.CreateGeometryProxy(entity)

    def constrain(
        self,
        occurrence1: int, geometry1: str,
        occurrence2: int, geometry2: str,
        kind: str = "mate",
        offset: float = 0,
        angle: float = 0,
    ) -> str:
        """Constrain two components by their origin planes/axes/point.

        kind:
          "mate"   — planes face each other / axes are collinear (offset in mm)
          "flush"  — planes point the same way (offset in mm)
          "angle"  — angle between two planes or axes (angle in degrees)
        geometry1/geometry2: "XY", "XZ", "YZ", "X", "Y", "Z", "ORIGIN"

        Origin-geometry constraints are the parametric way to assemble: they
        survive part edits, unlike face picks that break when topology renumbers.
        """
        import math
        comp = self.active_assembly()
        occ1 = comp.Occurrences.Item(occurrence1)
        occ2 = comp.Occurrences.Item(occurrence2)
        e1 = self._origin_proxy(occ1, geometry1)
        e2 = self._origin_proxy(occ2, geometry2)

        k = kind.lower()
        if k == "mate":
            c = comp.Constraints.AddMateConstraint(e1, e2, offset / 10.0)
            detail = f"offset {offset}mm"
        elif k == "flush":
            c = comp.Constraints.AddFlushConstraint(e1, e2, offset / 10.0)
            detail = f"offset {offset}mm"
        elif k == "angle":
            c = comp.Constraints.AddAngleConstraint(e1, e2, math.radians(angle))
            detail = f"{angle}°"
        else:
            raise ValueError(f"Unknown constraint kind '{kind}'. Use mate, flush or angle.")

        self.active_document().Update()
        return (f"{k.capitalize()} constraint '{c.Name}': "
                f"{occ1.Name}.{geometry1.upper()} ↔ {occ2.Name}.{geometry2.upper()} ({detail})")

    def constrain_insert(
        self,
        occurrence1: int, edge1: int,
        occurrence2: int, edge2: int,
        offset: float = 0,
        axes_opposed: bool = True,
    ) -> str:
        """Insert constraint between two circular edges (bolt into a hole).

        edge1/edge2 are 1-based indices into each component's body-1 edge list;
        use list_component_edges() to find the circular ones.
        """
        comp = self.active_assembly()
        occ1 = comp.Occurrences.Item(occurrence1)
        occ2 = comp.Occurrences.Item(occurrence2)
        e1 = occ1.CreateGeometryProxy(
            occ1.Definition.SurfaceBodies.Item(1).Edges.Item(edge1))
        e2 = occ2.CreateGeometryProxy(
            occ2.Definition.SurfaceBodies.Item(1).Edges.Item(edge2))
        c = comp.Constraints.AddInsertConstraint(e1, e2, axes_opposed, offset / 10.0)
        self.active_document().Update()
        return (f"Insert constraint '{c.Name}': {occ1.Name} edge {edge1} ↔ "
                f"{occ2.Name} edge {edge2} (offset {offset}mm)")

    def list_component_edges(
        self, occurrence: int, circular_only: bool = True, max_count: int = 40
    ) -> str:
        """Edge table of one placed component, in its own part coordinates.
        circular_only=True lists just the circular edges — the ones an Insert
        constraint can use."""
        comp = self.active_assembly()
        occ = comp.Occurrences.Item(occurrence)
        body = occ.Definition.SurfaceBodies.Item(1)
        lines = []
        for i in range(1, body.Edges.Count + 1):
            e = body.Edges.Item(i)
            is_circular = e.GeometryType == kCircleCurve
            if circular_only and not is_circular:
                continue
            if is_circular:
                g = e.Geometry
                c = g.Center
                lines.append(
                    f"[{i}] circle D={g.Radius * 20:.1f} "
                    f"c=({c.X * 10:.1f},{c.Y * 10:.1f},{c.Z * 10:.1f})"
                )
            else:
                sv, ev = e.StartVertex.Point, e.StopVertex.Point
                lines.append(
                    f"[{i}] edge mid=({(sv.X + ev.X) / 2 * 10:.1f},"
                    f"{(sv.Y + ev.Y) / 2 * 10:.1f},{(sv.Z + ev.Z) / 2 * 10:.1f})"
                )
            if len(lines) >= max_count:
                break
        return "\n".join(lines) if lines else "(no matching edges)"

    def list_constraints(self) -> str:
        comp = self.active_assembly()
        lines = []
        for i in range(1, comp.Constraints.Count + 1):
            c = comp.Constraints.Item(i)
            health = unhealthy(self.app, c)
            param = self._constraint_param(c)
            lines.append(f"[{i}] {c.Name}" + (f" = {param.Expression}" if param else "")
                         + (" (suppressed)" if c.Suppressed else "")
                         + (f" [{health}]" if health else ""))
        return "\n".join(lines) if lines else "(no constraints)"

    @staticmethod
    def _constraint_param(c):
        """The parameter holding a constraint's offset or angle; None if it has neither."""
        for name in ("Angle", "Offset", "Distance"):
            try:
                return getattr(c, name)
            except Exception:
                continue
        return None

    def edit_constraint(self, index: int, value: float, undo_if_moved: bool = False) -> str:
        """New offset (mm) or angle (degrees) of a constraint, reporting what else moved."""
        comp = self.active_assembly()
        doc = self.active_document()
        c = comp.Constraints.Item(index)
        param = self._constraint_param(c)
        if param is None:
            raise ValueError(f"{c.Name} has no offset or angle to change.")
        is_angle = param.Units.lower().startswith(("deg", "rad"))
        old = param.Expression
        before = self._poses(comp)
        param.Value = math.radians(value) if is_angle else value / 10
        doc.Update()
        own = set()
        for name in ("AffectedOccurrenceOne", "AffectedOccurrenceTwo"):
            try:
                own.add(self._path_of(getattr(c, name)))
            except Exception:
                pass
        moved = self._moved(before, self._poses(comp), own)
        if moved and undo_if_moved:
            param.Expression = old
            doc.Update()
            return f"{c.Name} put back to {old}: it would have moved " + "; ".join(moved)
        health = unhealthy(self.app, c)
        return (f"{c.Name}: {old} → {value:g} {'deg' if is_angle else 'mm'}"
                + (f"\nConstraint health: {health} — it conflicts with others or can't be solved." if health else "")
                + ("\nALSO MOVED: " + "; ".join(moved) if moved else ""))

    def delete_constraint(self, index: int) -> str:
        comp = self.active_assembly()
        c = comp.Constraints.Item(index)
        name = c.Name
        c.Delete()
        self.active_document().Update()
        return f"Deleted constraint: {name} ({comp.Constraints.Count} left)"

    def assembly_info(self) -> dict:
        """Snapshot of the active assembly: components, constraints, mass, bbox."""
        doc = self.active_document()
        if doc.DocumentType != kAssemblyDocumentObject:
            raise RuntimeError("Active document is not an assembly.")
        comp = doc.ComponentDefinition
        info: dict = {
            "document": doc.DisplayName,
            "occurrences": comp.Occurrences.Count,
            "constraints": comp.Constraints.Count,
            "components": [],
        }
        for i in range(1, comp.Occurrences.Count + 1):
            occ = comp.Occurrences.Item(i)
            t = occ.Transformation.Translation
            info["components"].append({
                "index": i,
                "name": occ.Name,
                "position_mm": [round(t.X * 10, 3), round(t.Y * 10, 3), round(t.Z * 10, 3)],
                "grounded": bool(occ.Grounded),
            })
        try:
            mp = self._fresh_mass(doc, comp)
            info["mass_kg"] = round(mp.Mass, 4)
            info["volume_mm3"] = round(mp.Volume * 1000, 1)
        except Exception:
            pass
        return info
