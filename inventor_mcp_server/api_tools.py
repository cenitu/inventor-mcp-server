"""The 1.0 entry points that merged several older ones."""
if __package__:
    from .api_common import *  # noqa: F401,F403
    from .flat_checks import flat_pattern_problems, rule_from_name  # noqa: F401
    from . import api_scan as _scan
else:  # server.py run as a script: the modules sit on sys.path
    from api_common import *  # noqa: F401,F403
    from flat_checks import flat_pattern_problems, rule_from_name  # noqa: F401
    import api_scan as _scan


class ToolsMixin:
    # ---------- 1.0: one tool per job ----------

    def find_faces(self, path="", surface="", normal=None, at=None, diameter=0.0, min_area=0.0,
                   max_count=30) -> list[dict]:
        """Faces of a component (path) or of the active part's bodies, filtered. Read inside
        Inventor when that is possible (a rule: seconds where COM takes minutes), else through
        COM — same rows, same refs either way."""
        self.active_assembly() if path else self.active_part()   # the same refusals as through COM
        leaves = self._read_brep("faces", path)
        if leaves is None:
            return self._find_faces_com(path, surface, normal, at, diameter, min_area, max_count)
        rows = []
        for leaf in leaves:
            for bi, faces in enumerate(leaf["bodies"], 1):
                for fi, row in enumerate(faces, 1):
                    if path:   # as assembly_faces: an unknown surface type never matches a name
                        if surface and self._SURFACES.get(int(row[0]), "") != surface.lower():
                            continue
                        info = _scan.face_info(row, self._SURFACES)
                        if not self._face_matches(info, "", normal, at, diameter, min_area, 0.05):
                            continue
                        info["ref"] = f"{leaf['path']}#{'B%d' % bi if bi > 1 else ''}F{fi}"
                    else:
                        info = _scan.face_info(row, self._SURFACES)
                        if not self._face_matches(info, surface, normal, at, diameter, min_area, 0.05):
                            continue
                        info["index"] = fi
                        if bi > 1:
                            info["body"] = bi
                    rows.append(info)
                    if len(rows) >= max_count:
                        return rows
        return rows

    def _find_faces_com(self, path="", surface="", normal=None, at=None, diameter=0.0, min_area=0.0,
                        max_count=30) -> list[dict]:
        """find_faces through COM: one call per face property."""
        if path:
            return self.assembly_faces(path, surface, normal, at, diameter, min_area, max_count)
        comp = self.active_part()
        rows = []
        bodies = comp.SurfaceBodies
        for bi in range(1, bodies.Count + 1):
            faces = bodies.Item(bi).Faces
            for fi in range(1, faces.Count + 1):
                info = self._face_info(faces.Item(fi))
                if self._face_matches(info, surface, normal, at, diameter, min_area, 0.05):
                    info["index"] = fi
                    if bi > 1:
                        info["body"] = bi
                    rows.append(info)
                    if len(rows) >= max_count:
                        return rows
        return rows

    def _edge_info(self, e) -> dict:
        """Type, length, midpoint (or circle center, diameter) of an edge, in mm."""
        kind = e.GeometryType
        if kind == kCircleCurve:
            g = e.Geometry
            c = g.Center
            return {"type": "circle", "diameter": round(g.Radius * 20, 3),
                    "at": [round(c.X * 10, 3), round(c.Y * 10, 3), round(c.Z * 10, 3)]}
        a, b = e.StartVertex.Point, e.StopVertex.Point
        return {"type": {5123: "line", 5125: "arc"}.get(kind, "curve"),
                "length": round(sum((p - q) ** 2 for p, q in ((a.X, b.X), (a.Y, b.Y), (a.Z, b.Z))) ** 0.5 * 10, 3),
                "at": [round((a.X + b.X) * 5, 3), round((a.Y + b.Y) * 5, 3), round((a.Z + b.Z) * 5, 3)]}

    def find_edges(self, path="", at=None, circular=False, diameter=0.0, min_length=0.0,
                   max_count=40, tolerance=0.1) -> list[dict]:
        """Edges of a component (path) or the active part, filtered by midpoint / circle center."""
        def wanted(info):
            if circular and info["type"] != "circle":
                return False
            if diameter and abs(info.get("diameter", -1) - diameter) > tolerance:
                return False
            if min_length and info.get("length", 1e9) < min_length:
                return False
            return not at or all(abs(info["at"]["XYZ".index(k.upper())] - float(v)) <= tolerance
                                 for k, v in at.items())

        rows = []
        self.active_assembly() if path else self.active_part()
        leaves = self._read_brep("edges", path)
        if leaves is not None:
            for leaf in leaves:
                for bi, edges in enumerate(leaf["bodies"], 1):
                    for ei, row in enumerate(edges, 1):
                        info = _scan.edge_info(row)
                        if wanted(info):
                            if path:
                                info["ref"] = f"{leaf['path']}#{'B%d' % bi if bi > 1 else ''}E{ei}"
                            else:
                                info["index"] = ei
                            rows.append(info)
                            if len(rows) >= max_count:
                                return rows
            return rows
        # through COM: one call per edge property
        sources = [(p, o.SurfaceBodies) for p, o in self._leaves(self._occ(path), path)] if path \
            else [("", self.active_part().SurfaceBodies)]
        for prefix, bodies in sources:
            for bi in range(1, bodies.Count + 1):
                edges = bodies.Item(bi).Edges
                for ei in range(1, edges.Count + 1):
                    info = self._edge_info(edges.Item(ei))
                    if wanted(info):
                        if prefix:
                            info["ref"] = f"{prefix}#{'B%d' % bi if bi > 1 else ''}E{ei}"
                        else:
                            info["index"] = ei
                        rows.append(info)
                        if len(rows) >= max_count:
                            return rows
        return rows

    def inspect_model(self) -> dict:
        doc = self.active_document()
        return self.assembly_info() if doc.DocumentType == kAssemblyDocumentObject else self.inspect()

    def find_occurrences(self, definition="", axis=None, near=None, within=0.0) -> list[dict]:
        """Part occurrences by definition name pattern, Z axis direction (either way) and
        distance from a point (mm)."""
        import fnmatch, math
        comp = self.active_assembly()
        pat = definition.lower()
        want = None
        if axis is not None:
            n = math.sqrt(sum(float(c) ** 2 for c in axis))
            want = [float(c) / n for c in axis]
        rows = []
        leaves = comp.Occurrences.AllLeafOccurrences
        for i in range(1, leaves.Count + 1):
            o = leaves.Item(i)
            if pat:
                d = o.Definition.Document
                names = (d.DisplayName.lower(), os.path.basename(d.FullFileName).lower(), o.Name.lower())
                if not any(fnmatch.fnmatch(n, pat) for n in names):
                    continue
            m = o.Transformation
            z = [m.Cell(r, 3) for r in (1, 2, 3)]
            pos = [m.Cell(r, 4) * 10 for r in (1, 2, 3)]
            if want is not None and abs(sum(a * b for a, b in zip(z, want))) < 0.999:
                continue
            if near is not None and within and math.dist(pos, [float(c) for c in near]) > within:
                continue
            rows.append({"path": self._path_of(o), "position": [round(c, 3) for c in pos],
                         "z_axis": [round(c, 4) + 0.0 for c in z]})
        return rows

    def to_top(self, path, point) -> list[float]:
        """A point in a component's own coordinates (mm) → active assembly coordinates (mm).
        path may reach into sub-assemblies ("Sub:1/Part:1")."""
        m = self._occ(str(path)).Transformation
        p = [float(c) for c in point]
        return [round(sum(m.Cell(r, c) * p[c - 1] for c in (1, 2, 3)) + m.Cell(r, 4) * 10, 4) + 0.0
                for r in (1, 2, 3)]

    def to_local(self, path, point) -> list[float]:
        """A point in active assembly coordinates (mm) → the component's own coordinates (mm)."""
        m = self._occ(str(path)).Transformation
        d = [float(point[r - 1]) - m.Cell(r, 4) * 10 for r in (1, 2, 3)]
        return [round(sum(m.Cell(r, c) * d[r - 1] for r in (1, 2, 3)), 4) + 0.0 for c in (1, 2, 3)]

    def replace_occurrence(self, path, with_file: str, all_same: bool = True,
                           archive_old: bool = False) -> str:
        """Put another file in a component's place — all occurrences of the same file at
        that level (all_same), or only this one. Constraints are kept where Inventor can
        match the geometry; the ones it lost and the components that moved are reported.
        archive_old moves the old file into an "_old" folder beside it, once no open
        document uses it any more."""
        comp = self.active_assembly()
        occ = self._occ(str(path))
        name = self._path_of(occ)
        new = os.path.abspath(with_file)
        if not os.path.isfile(new):
            raise RuntimeError(f"File not found: {new}")
        old_doc = occ.Definition.Document
        old = old_doc.FullFileName
        if os.path.normcase(old) == os.path.normcase(new):
            raise RuntimeError(f"{name} already is {os.path.basename(new)}")
        if archive_old:
            reason = self._library_reason(old_doc, self.library_folders())
            if reason:
                raise RuntimeError(f"{os.path.basename(old)} is not archived: {reason}. "
                                   "Replace without archive_old.")
        before = self._poses(comp)
        sick_before = self._sick_constraints(comp)
        n_cons = comp.Constraints.Count
        try:
            self._silently(lambda: occ.Replace(new, bool(all_same)))
            self.active_document().Update()
        except Exception as e:
            blockers = self._open_parents(occ)
            raise RuntimeError(f"{name} can't be replaced: {describe_com_error(e)}"
                               + (f"\nOpen documents that reference this level block it: {', '.join(blockers)} — "
                                  "close them (manage_documents) and retry." if blockers else "")) from None
        after = self._poses(comp)
        swapped = sum(1 for p in after if p not in before)
        moved = self._moved(before, after, set())
        sick = sorted(set(self._sick_constraints(comp)) - set(sick_before))
        lost = n_cons - comp.Constraints.Count
        out = [f"Replaced {name} ({os.path.basename(old)}) with {os.path.basename(new)}"
               + (f" — {swapped} part occurrence(s) are new" if swapped else "")]
        if lost > 0:
            out.append(f"{lost} constraint(s) are gone — add them again (manage_constraints list)")
        if sick:
            out.append(f"Constraints that lost their geometry: {', '.join(sick[:20])}")
        if moved:
            out.append(f"MOVED — check these: {'; '.join(moved[:20])}")
        if archive_old:
            out.append(self._archive_file(old))
        return "\n".join(out)

    def _sick_constraints(self, comp) -> list[str]:
        """Names of the constraints Inventor can't solve."""
        cons = [comp.Constraints.Item(i) for i in range(1, comp.Constraints.Count + 1)]
        return [c.Name for c in cons if unhealthy(self.app, c)]

    def _archive_file(self, path: str) -> str:
        """Move a file no open document uses into "_old" beside it (numbered if taken)."""
        name = os.path.basename(path)
        for d in self.app.Documents:
            if os.path.normcase(d.FullFileName or "") != os.path.normcase(path):
                continue
            users = [r.DisplayName for r in d.ReferencingDocuments]
            if users:
                return (f"{name} stays where it is: still used by {', '.join(users[:10])} "
                        "(replace it there too, or close them)")
            if d.Dirty:
                return f"{name} stays where it is: it has unsaved changes"
            d.Close(True)
            break
        folder = os.path.join(os.path.dirname(path), "_old")
        os.makedirs(folder, exist_ok=True)
        stem, ext = os.path.splitext(name)
        dest, n = os.path.join(folder, name), 1
        while os.path.exists(dest):
            n += 1
            dest = os.path.join(folder, f"{stem} ({n}){ext}")
        try:
            os.replace(path, dest)
        except OSError as e:
            return f"{name} stays where it is: {e}"
        return (f"Old file moved to {dest}. Assemblies that are not open and still reference "
                f"{name} will ask for it — replace it there too.")

    def _dependents(self, comp, occ) -> dict:
        """What hangs on an occurrence: constraints, and component patterns it is the parent of."""
        name = self._path_of(occ)
        cons, pats = [], []
        for i in range(1, comp.Constraints.Count + 1):
            c = comp.Constraints.Item(i)
            try:
                if name in (self._path_of(c.OccurrenceOne), self._path_of(c.OccurrenceTwo)):
                    cons.append(c.Name)
            except Exception:
                pass
        try:
            ps = comp.OccurrencePatterns
            for i in range(1, ps.Count + 1):
                p = ps.Item(i)
                parents = p.ParentComponents
                if any(self._path_of(parents.Item(j)) == name for j in range(1, parents.Count + 1)):
                    pats.append(p)
        except Exception:
            pass
        return {"constraints": cons, "patterns": pats}

    def delete_occurrence(self, path, cascade: bool = False, force: bool = False) -> str:
        """Delete a component. With dependents it first says what depends on it; force
        deletes (cascade: its component patterns too). Reports what moved afterwards."""
        comp = self.active_assembly()
        occ = self._occ(str(path))
        name = self._path_of(occ)
        deps = self._dependents(comp, occ)
        pats = [f"{p.Name} ({p.OccurrencePatternElements.Count} elements)" for p in deps["patterns"]]
        if (pats or deps["constraints"]) and not force and not cascade:
            return (f"{name} is not deleted. Depends on it: "
                    + "; ".join(filter(None, [f"patterns {', '.join(pats)}" if pats else "",
                                              f"constraints {', '.join(deps['constraints'][:12])}" if deps["constraints"] else ""]))
                    + ". A pattern's members don't go with it — they recompute somewhere else. "
                      "cascade=True deletes the patterns too; force=True deletes only this component.")
        before = self._poses(comp)
        try:
            if cascade:
                for p in deps["patterns"]:
                    p.Delete()
                occ = self._occ(str(path)) if self._exists(path) else None
            if occ is not None:
                occ.Delete()
            self.active_document().Update()
        except Exception as e:
            blockers = self._open_parents(occ)
            raise RuntimeError(f"{name} can't be deleted: {describe_com_error(e)}"
                               + (f"\nOpen documents that reference this level block it: {', '.join(blockers)} — "
                                  "close them (manage_documents) and retry." if blockers else "")) from None
        after = self._poses(comp)
        moved = []
        for p, m in after.items():
            old = before.get(p)
            if old is not None and any(abs(m[i] - old[i]) > 0.01 for i in (3, 7, 11)):
                moved.append(f"{p} Δ({m[3] - old[3]:.1f}, {m[7] - old[7]:.1f}, {m[11] - old[11]:.1f}) mm")
        return (f"Deleted {name} ({len(before) - len(after)} part occurrence(s) gone)"
                + (f"\nMOVED — check these: {'; '.join(moved[:20])}" if moved else ""))

    def _exists(self, path) -> bool:
        try:
            self._occ(str(path))
            return True
        except Exception:
            return False

    def _open_parents(self, occ) -> list[str]:
        try:
            parent = occ.ContextDefinition.Document
            top = self.active_document()
            return [d.DisplayName for d in parent.ReferencingDocuments if d.InternalName != top.InternalName]
        except Exception:
            return []

    def open_document(self, path: str) -> str:
        path = os.path.abspath(path)
        if not os.path.isfile(path):
            raise RuntimeError(f"File not found: {path}")
        doc = self._silently(lambda: self.app.Documents.Open(path, True))
        return f"Opened {doc.DisplayName} ({doc.AllReferencedDocuments.Count} referenced)"

    def _find_document(self, name_or_path: str):
        key = name_or_path.lower()
        for d in self.app.Documents:
            if key in (d.DisplayName.lower(), (d.FullFileName or "").lower(),
                       os.path.splitext(d.DisplayName)[0].lower()):
                return d
        raise RuntimeError(f"No open document {name_or_path!r} — see manage_documents list")

    def close_document(self, name_or_path: str, discard: bool = False) -> str:
        d = self._find_document(name_or_path)
        if d.Dirty and not discard and not self._library_reason(d, self.library_folders()):
            raise RuntimeError(f"{d.DisplayName} has unsaved changes — save_document, or discard=True")
        name = d.DisplayName
        d.Close(True)
        return f"Closed {name}"

    def close_all(self, discard: bool = False, quit_inventor: bool = False) -> str:
        """Close every document. Refuses while the user's own files are dirty; protected
        (library / Content Center) files never count — Inventor marks those dirty by itself."""
        folders = self.library_folders()
        mine = [d.DisplayName for d in self.app.Documents if d.Dirty and d.FullFileName
                and not self._library_reason(d, folders)]
        if mine and not discard:
            raise RuntimeError(f"{len(mine)} of your files have unsaved changes: {', '.join(mine[:20])} — "
                               "save them, or discard=True")
        n = self.app.Documents.Count
        self.app.Documents.CloseAll(False)
        left = self.app.Documents.Count
        if left:  # CloseAll leaves documents something still references
            self.app.Documents.CloseAll(True)
        msg = f"Closed {n - self.app.Documents.Count} of {n} document(s)"
        if quit_inventor:
            self.app.Quit()
            self._app = None
            msg += ", Inventor quit"
        return msg

    def hole_along(self, point, direction, diameter: float, depth: float = 0, tapped: bool = False,
                   pitch: float | None = None) -> str:
        """A hole at an absolute point, drilling along direction — no sketch axes involved.
        Fails, leaving nothing behind, if no material was removed."""
        import math
        doc = self.active_document()
        comp = self.active_part()
        tg = self._tg()
        hf = comp.Features.HoleFeatures
        n = math.sqrt(sum(float(c) ** 2 for c in direction))
        d = [float(c) / n for c in direction]
        p = tg.CreatePoint(*(float(c) / 10 for c in point))
        wp = comp.WorkPoints.AddFixed(p)
        wa = comp.WorkAxes.AddFixed(p, tg.CreateUnitVector(*d))
        wp.Visible = wa.Visible = False
        placement = hf.CreatePointPlacementDefinition(wp, wa)
        if tapped:
            nominal = int(round(diameter))
            pitch = pitch or self._METRIC_COARSE_PITCH.get(nominal)
            if pitch is None:
                raise ValueError(f"No standard coarse pitch for M{nominal} — pass pitch")
            arg = hf.CreateTapInfo(True, "ISO Metric profile", f"M{nominal}x{pitch:g}", "6H", True)
        else:
            arg = diameter / 10
        v0 = comp.MassProperties.Volume
        for way in (kPositiveExtentDirection, kNegativeExtentDirection):
            try:
                f = (hf.AddDrilledByThroughAllExtent(placement, arg, way) if depth <= 0
                     else hf.AddDrilledByDistanceExtent(placement, arg, depth / 10, way))
            except Exception:
                continue
            doc.Update()
            removed = (v0 - comp.MassProperties.Volume) * 1000
            if removed > 1e-3:
                return f"Hole {f.Name} Ø{diameter} at {list(point)} along {list(direction)}: removed {removed:.0f} mm³"
            f.Delete()
        wa.Delete()
        wp.Delete()
        raise RuntimeError(f"A hole at {list(point)} along {list(direction)} removes no material — is the "
                           "point on or above the body, the direction pointing into it?")
