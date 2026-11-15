"""Vendor surfaces to solids, Content Center and frames."""
if __package__:
    from .api_common import *  # noqa: F401,F403
    from .flat_checks import flat_pattern_problems, rule_from_name  # noqa: F401
else:  # server.py run as a script: the modules sit on sys.path
    from api_common import *  # noqa: F401,F403
    from flat_checks import flat_pattern_problems, rule_from_name  # noqa: F401


class LibraryMixin:
    # ---------- Surfaces to a solid ----------
    #
    # What works (Inventor 2027.1, tested): KnitFeatures.Add(surfaces, tolerance, False) makes a
    # solid when it gets SEVERAL surfaces that close up — but fails (E_FAIL) on one surface that
    # is already a closed shell, there is nothing to stitch. That one becomes a solid as a
    # NonParametricBaseFeature with OutputType kSolidOutputType. A shell with holes in it gets
    # a BoundaryPatch per loop of open edges first.

    def _visible_surfaces(self, comp, which: str = "all") -> list:
        ws = [comp.WorkSurfaces.Item(i) for i in range(1, comp.WorkSurfaces.Count + 1)]
        if which and which != "all":
            picked = {int(s) for s in which.replace(" ", "").split(",") if s}
            ws = [w for i, w in enumerate(ws, 1) if i in picked]
        return [w for w in ws if w.Visible and w.SurfaceBodies.Count]

    @staticmethod
    def _open_edges(body) -> list:
        return [body.Edges.Item(i) for i in range(1, body.Edges.Count + 1) if body.Edges.Item(i).Faces.Count == 1]

    @staticmethod
    def _edge_loops(edges) -> list[list]:
        """Group open edges into loops by the vertices they share."""
        def key(v):
            p = v.Point
            return (round(p.X, 5), round(p.Y, 5), round(p.Z, 5))
        loops, rest = [], list(edges)
        while rest:
            loop = [rest.pop()]
            ends = {key(loop[0].StartVertex), key(loop[0].StopVertex)}
            grew = True
            while grew:
                grew = False
                for e in list(rest):
                    ks = {key(e.StartVertex), key(e.StopVertex)}
                    if ks & ends:
                        loop.append(e)
                        rest.remove(e)
                        ends |= ks
                        grew = True
            loops.append(loop)
        return loops

    def _solids(self, comp) -> list:
        b = comp.SurfaceBodies
        return [b.Item(i) for i in range(1, b.Count + 1) if b.Item(i).IsSolid]

    def surface_to_solid(self, work_surfaces: str = "all", tolerance: float = 0.01,
                         patch_holes: bool = True) -> dict:
        """Turn the active part's surfaces (imported vendor models, x_t / IGES) into solids:
        stitch them, patch the holes left in the shell, and close it into a solid."""
        doc = self.active_document()
        comp = self.active_part()
        tol = tolerance / 10.0
        surfaces = self._visible_surfaces(comp, work_surfaces)
        if not surfaces:
            raise RuntimeError("No visible surfaces in this part (WorkSurfaces) — nothing to convert.")
        solids_before = len(self._solids(comp))
        report = {"surfaces": len(surfaces), "steps": []}
        coll = self.app.TransientObjects.CreateObjectCollection()
        for w in surfaces:
            coll.Add(w)

        # 1. several surfaces: knit them straight into a solid if they close up
        if len(surfaces) > 1:
            try:
                self._silently(lambda: comp.Features.KnitFeatures.Add(coll, tol, False))
                doc.Update()
            except Exception:
                pass
            report["steps"].append("knitted into a solid" if len(self._solids(comp)) > solids_before
                                   else "knitted, but the shell has holes")
        if len(self._solids(comp)) == solids_before:
            # 2. make one shell (quilt) to find the holes in
            if len(surfaces) > 1:
                n = comp.WorkSurfaces.Count
                try:
                    self._silently(lambda: comp.Features.KnitFeatures.Add(coll, tol, True))
                    doc.Update()
                    quilts = [comp.WorkSurfaces.Item(i) for i in range(n + 1, comp.WorkSurfaces.Count + 1)]
                except Exception:
                    raise RuntimeError("Inventor couldn't stitch these surfaces at all — try a larger tolerance")
            else:
                quilts = surfaces
            for q in quilts:
                for bi in range(1, q.SurfaceBodies.Count + 1):
                    body = q.SurfaceBodies.Item(bi)
                    self._close_shell(comp, doc, q, body, tol, patch_holes, report)
        solids = self._solids(comp)[solids_before:]
        report["solids"] = [{"volume_mm3": round(s.Volume(0.0001) * 1000, 1), "faces": s.Faces.Count} for s in solids]
        if not solids:
            report["result"] = "no solid — see steps / open_edges"
        return report

    def _close_shell(self, comp, doc, ws, body, tol, patch_holes, report):
        """One shell: patch its open loops, then knit shell + patches into a solid; a closed
        shell on its own becomes a solid base feature."""
        open_edges = self._open_edges(body)
        if not open_edges:
            tb = self.app.TransientBRep.Copy(body)
            npb = comp.Features.NonParametricBaseFeatures
            dfn = npb.CreateDefinition()
            c = self.app.TransientObjects.CreateObjectCollection()
            c.Add(tb)
            dfn.BRepEntities = c
            dfn.OutputType = 90114  # kSolidOutputType
            self._silently(lambda: npb.AddByDefinition(dfn))
            ws.Visible = False
            doc.Update()
            report["steps"].append(f"closed shell of {body.Faces.Count} faces → solid")
            return
        loops = self._edge_loops(open_edges)
        if not patch_holes:
            report["steps"].append(f"{len(loops)} hole(s) left open (patch_holes=False)")
            report["open_edges"] = self._where(open_edges)
            return
        n = comp.WorkSurfaces.Count
        patched, failed = 0, []
        for loop in loops:
            dfn = comp.Features.BoundaryPatchFeatures.CreateBoundaryPatchDefinition()
            ec = self.app.TransientObjects.CreateEdgeCollection()
            for e in loop:
                ec.Add(e)
            try:
                dfn.BoundaryPatchLoops.Add(ec)
                self._silently(lambda: comp.Features.BoundaryPatchFeatures.Add(dfn))
                patched += 1
            except Exception:
                failed.extend(loop)
        doc.Update()
        patches = [comp.WorkSurfaces.Item(i) for i in range(n + 1, comp.WorkSurfaces.Count + 1)]
        report["steps"].append(f"{patched} of {len(loops)} hole(s) patched")
        if failed:
            report["open_edges"] = self._where(failed)
            return
        c = self.app.TransientObjects.CreateObjectCollection()
        c.Add(ws)
        for p in patches:
            c.Add(p)
        try:
            self._silently(lambda: comp.Features.KnitFeatures.Add(c, tol, False))
            doc.Update()
            report["steps"].append("shell + patches knitted into a solid")
        except Exception:
            report["steps"].append("shell + patches still don't close — gaps wider than the tolerance?")

    @staticmethod
    def _where(edges) -> list[str]:
        out = []
        for e in edges[:12]:
            a, b = e.StartVertex.Point, e.StopVertex.Point
            out.append(f"({a.X * 10:.2f}, {a.Y * 10:.2f}, {a.Z * 10:.2f}) – ({b.X * 10:.2f}, {b.Y * 10:.2f}, {b.Z * 10:.2f})")
        return out + ([f"… {len(edges) - 12} more"] if len(edges) > 12 else [])

    # ---------- Content Center ----------
    #
    # Walking the whole Content Center tree takes minutes, so it is navigated by a category
    # path ("Structural Shapes/Channels") and every node / family found is cached on the
    # connection — the next call for the same family is instant.

    def _cc_node(self, category: str):
        cache = self.__dict__.setdefault("_cc_nodes", {})
        key = "/".join(s.strip() for s in category.split("/") if s.strip())
        if key in cache:
            return cache[key]
        node, walked = self.app.ContentCenter.TreeViewTopNode, []
        segs, i = (key.split("/") if key else []), 0
        while i < len(segs):
            kids = [node.ChildNodes.Item(k) for k in range(1, node.ChildNodes.Count + 1)]
            names = {k.DisplayName.lower(): k for k in kids}
            # longest match first: a name like "Square/Rectangular Tubes" spans two segments
            j = next((j for j in range(len(segs), i, -1) if "/".join(segs[i:j]).lower() in names), None)
            if j is None:
                raise RuntimeError(f"No category {segs[i]!r} under {'/'.join(walked) or 'Content Center'}. "
                                   f"There: {', '.join(k.DisplayName for k in kids)}")
            node = names["/".join(segs[i:j]).lower()]
            walked.append(node.DisplayName)
            i = j
        cache[key] = node
        return node

    def _cc_family(self, category: str, family: str):
        cache = self.__dict__.setdefault("_cc_families", {})
        key = (category.strip("/ ").lower(), family.lower())
        if key not in cache:
            node = self._cc_node(category)
            fams = [node.Families.Item(i) for i in range(1, node.Families.Count + 1)]
            match = next((f for f in fams if f.DisplayName.lower() == family.lower()), None)
            if match is None:
                raise RuntimeError(f"No family {family!r} in {category!r}. "
                                   f"There: {', '.join(f.DisplayName for f in fams)}")
            cache[key] = match
        return cache[key]

    @staticmethod
    def _cc_columns(fam) -> dict:
        return {fam.TableColumns.Item(i).InternalName: fam.TableColumns.Item(i)
                for i in range(1, fam.TableColumns.Count + 1)}

    def content_center(self, category: str = "", family: str = "", size: str = "",
                       length: float = 0, file_name: str = "") -> str:
        """Browse the Content Center, or make a member's part file to place.

        No family: lists the sub-categories and families of `category`.
        Family, no size: lists its sizes. Family + size: creates the part and returns its
        path. length (mm) makes a custom-length member (structural shapes) saved as
        file_name (default: <project workspace>/Content Center Custom/<family> <size> L<length>.ipt).
        """
        if not family:
            node = self._cc_node(category)
            subs = [node.ChildNodes.Item(i).DisplayName for i in range(1, node.ChildNodes.Count + 1)]
            fams = [node.Families.Item(i).DisplayName for i in range(1, node.Families.Count + 1)]
            out = [f"{category or 'Content Center'}:"]
            if subs:
                out.append("categories: " + ", ".join(subs))
            if fams:
                out.append("families: " + ", ".join(fams))
            return "\n".join(out)
        fam = self._cc_family(category, family)
        cols = self._cc_columns(fam)
        names = [c for c in ("DESIGNATION", "G_D", "SIZE") if c in cols]
        rows = [fam.TableRows.Item(i) for i in range(1, fam.TableRows.Count + 1)]
        label = lambda r: str(r.GetCellValue(cols[names[0]])) if names else str(r.Index)
        if not size:
            return f"{fam.DisplayName} — {len(rows)} sizes: " + ", ".join(label(r) for r in rows)
        norm = lambda s: str(s).replace(" ", "").lower()
        row = next((r for r in rows if any(norm(r.GetCellValue(cols[c])) == norm(size) for c in names)), None)
        if row is None:
            import difflib
            all_sizes = [label(r) for r in rows]
            close = difflib.get_close_matches(size, all_sizes, n=6, cutoff=0.3)
            raise RuntimeError(f"No size {size!r} in {fam.DisplayName}."
                               + (f" Closest: {', '.join(close)}." if close else "")
                               + f" {len(all_sizes)} sizes: {', '.join(all_sizes[:60])}")
        custom = length > 0
        if custom and "B_L" not in cols:
            raise RuntimeError(f"{fam.DisplayName} has no length parameter (B_L) — omit length.")
        values = self.app.TransientObjects.CreateNameValueMap()
        if custom:
            values.Add("B_L", float(length))  # mm
            if not file_name:
                ws = self.app.DesignProjectManager.ActiveDesignProject.WorkspacePath
                safe = "".join(ch for ch in f"{fam.DisplayName} {label(row)} L{length:g}" if ch not in '<>:"/\\|?*')
                file_name = os.path.join(ws, "Content Center Custom", safe + ".ipt")
            file_name = os.path.abspath(file_name)
            os.makedirs(os.path.dirname(file_name), exist_ok=True)
        if custom and os.path.exists(file_name):
            path = file_name  # made before (frames reuse one file per profile and length)
        else:
            result = self._silently(lambda: fam.CreateMember(
                row, 0, "", 51969, custom, file_name if custom else "", values))  # kUseDefaultRefreshSetting
            path = next((r for r in (result if isinstance(result, tuple) else (result,))
                         if isinstance(r, str) and r.lower().endswith(".ipt")), None)
            if not path or not os.path.exists(path):
                raise RuntimeError(f"Content Center did not create the part: {result}")
        return f"{fam.DisplayName} {label(row)}" + (f" L={length:g} mm" if custom else "") + f": {path}"

    # ---------- Frames: profiles along skeleton lines (Frame Generator has no API) ----------

    def _member_geometry(self, path: str, length: float) -> tuple[int, list, list]:
        """Length axis (0/1/2 = X/Y/Z) of a member part and its range box in mm, read once."""
        cache = self.__dict__.setdefault("_member_geom", {})
        if path not in cache:
            doc = self.app.Documents.Open(path, False)
            rb = doc.ComponentDefinition.RangeBox
            lo = [rb.MinPoint.X * 10, rb.MinPoint.Y * 10, rb.MinPoint.Z * 10]
            hi = [rb.MaxPoint.X * 10, rb.MaxPoint.Y * 10, rb.MaxPoint.Z * 10]
            axis = min(range(3), key=lambda i: abs((hi[i] - lo[i]) - length))
            cache[path] = (axis, lo, hi)
            if not doc.ReferencingDocuments.Count:
                doc.Close(True)
        return cache[path]

    @staticmethod
    def _frame_axes(direction, roll_deg: float):
        """World directions for a member's section axes (u, v) and length axis w. u is level
        (perpendicular to world Z) unless the member is vertical; roll turns the section."""
        import math as m
        n = m.sqrt(sum(c * c for c in direction))
        w = [c / n for c in direction]
        ref = [0.0, 0.0, 1.0] if abs(w[2]) < 0.999 else [1.0, 0.0, 0.0]
        u = [ref[1] * w[2] - ref[2] * w[1], ref[2] * w[0] - ref[0] * w[2], ref[0] * w[1] - ref[1] * w[0]]
        nu = m.sqrt(sum(c * c for c in u))
        u = [c / nu for c in u]
        v = [w[1] * u[2] - w[2] * u[1], w[2] * u[0] - w[0] * u[2], w[0] * u[1] - w[1] * u[0]]
        if roll_deg:
            c, s = m.cos(m.radians(roll_deg)), m.sin(m.radians(roll_deg))
            u, v = [c * a + s * b for a, b in zip(u, v)], [-s * a + c * b for a, b in zip(u, v)]
        return u, v, w

    def frame_members(self, members: list[dict], folder: str = "", category: str = "",
                      family: str = "", size: str = "") -> str:
        """Place structural members along skeleton lines in the active assembly.

        members: [{"start": [x,y,z], "end": [x,y,z], "family": ..., "size": ...,
                   "category": ..., "roll": 0, "anchor": "center"|"origin",
                   "extend": [0, 0], "name": ...}] in mm. family/size/category default to
        the call's own arguments. Each member is a Content Center custom-length part, one file
        per profile and length (reused). anchor "center" puts the section's middle on the
        line, "origin" the profile's own origin. Returns the placements and a cut list.
        """
        import math as m
        comp = self.active_assembly()
        if folder:
            folder = os.path.abspath(folder)
            os.makedirs(folder, exist_ok=True)
        placed, problems, cuts = [], [], {}
        for n, mb in enumerate(members, 1):
            try:
                fam_cat = mb.get("category") or category
                fam = mb.get("family") or family
                sz = mb.get("size") or size
                if not (fam_cat and fam and sz):
                    raise ValueError("category, family and size are needed (per member or for the call)")
                start = [float(c) for c in mb["start"]]
                end = [float(c) for c in mb["end"]]
                d = [b - a for a, b in zip(start, end)]
                line = m.sqrt(sum(c * c for c in d))
                if line < 1:
                    raise ValueError("start and end are the same point")
                ext0, ext1 = (list(mb.get("extend") or [0, 0]) + [0, 0])[:2]
                length = round(line + ext0 + ext1, 1)
                file_name = ""
                if folder:
                    safe = "".join(ch for ch in f"{fam} {sz} L{length:g}" if ch not in '<>:"/\\|?*')
                    file_name = os.path.join(folder, safe + ".ipt")
                out = self.content_center(fam_cat, fam, sz, length, file_name)
                path = out.rsplit(": ", 1)[1]
                axis, lo, hi = self._member_geometry(path, length)
                u, v, w = self._frame_axes(d, float(mb.get("roll", 0)))
                # local axes: the length axis goes along w, the next two (cyclic) along u, v
                world = [None, None, None]
                world[axis], world[(axis + 1) % 3], world[(axis + 2) % 3] = w, u, v
                # the local point that must sit on the start of the line
                local = [0.0, 0.0, 0.0]
                local[axis] = lo[axis]
                if mb.get("anchor", "center") == "center":
                    for i in ((axis + 1) % 3, (axis + 2) % 3):
                        local[i] = (lo[i] + hi[i]) / 2
                begin = [s - ext0 * c / line for s, c in zip(start, d)]
                origin = [b - sum(local[i] * world[i][k] for i in range(3)) for k, b in enumerate(begin)]
                occ = comp.Occurrences.Add(path, self._placement(*origin, world[0], world[1]))
                if mb.get("name"):
                    try:
                        occ.Name = mb["name"]
                    except Exception:
                        problems.append(f"#{n}: name {mb['name']!r} taken, kept {occ.Name}")
                placed.append(f"{occ.Name}: {fam} {sz} L={length:g}")
                key = (fam, sz, length)
                cuts[key] = cuts.get(key, 0) + 1
            except Exception as e:
                problems.append(f"#{n}: {e}")
        lines = [f"Placed {len(placed)} of {len(members)} members"] + [f"  {p}" for p in placed]
        if cuts:
            lines.append("Cut list:")
            for (fam, sz, length), qty in sorted(cuts.items()):
                lines.append(f"  {qty} x {fam} {sz}  L={length:g} mm")
        if problems:
            lines.append("Problems:")
            lines += [f"  {p}" for p in problems]
        return "\n".join(lines)
