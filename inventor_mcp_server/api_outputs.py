"""What leaves the model: flat pattern, BOM, properties, drawings, and whether it can be made."""
if __package__:
    from .api_common import *  # noqa: F401,F403
    from .flat_checks import flat_pattern_problems, rule_from_name, judge_sheet, sheet_facts, SHEETS  # noqa: F401
else:  # server.py run as a script: the modules sit on sys.path
    from api_common import *  # noqa: F401,F403
    from flat_checks import flat_pattern_problems, rule_from_name, judge_sheet, sheet_facts, SHEETS  # noqa: F401


class OutputsMixin:
    # ---------- Manufacturing outputs: flat pattern, BOM, properties, drawings ----------

    # DXF/DWG layers Inventor writes for a flat pattern. The cut contour always goes out;
    # bend lines are optional (press-brake marks); everything else is hidden.
    _FLAT_HIDDEN = ("IV_TANGENT", "IV_ARC_CENTERS", "IV_TOOL_CENTER", "IV_TOOL_CENTER_DOWN",
                    "IV_FEATURE_PROFILES", "IV_FEATURE_PROFILES_DOWN", "IV_ALTREP_FRONT",
                    "IV_ALTREP_BACK", "IV_UNCONSUMED_SKETCHES", "IV_ROLL_TANGENT", "IV_ROLL")
    _BEND_LAYERS = ("IV_BEND", "IV_BEND_DOWN", "IV_BEND_UP")

    def flat_pattern(self, path: str = "", format: str = "DXF", bend_lines: bool = True,
                     acad_version: str = "2018") -> dict:
        """Flat pattern of the active sheet metal part: size, bends, and optionally a
        DXF/DWG for the laser. Creates the flat pattern if the part has none."""
        doc = self.active_document()
        comp = doc.ComponentDefinition
        if not hasattr(comp, "HasFlatPattern"):
            raise RuntimeError(f"{doc.DisplayName} is not a sheet metal part.")
        if not comp.HasFlatPattern:
            self._silently(comp.Unfold)
            try:
                comp.FlatPattern.ExitEdit()
            except Exception:
                pass
        fp = comp.FlatPattern
        info = {"part": doc.DisplayName,
                "flat_mm": [round(fp.Length * 10, 2), round(fp.Width * 10, 2)],
                "thickness_mm": round(comp.Thickness.Value * 10, 3)}
        try:
            info["bends"] = fp.FlatBendResults.Count
        except Exception:
            pass
        if path:
            fmt = format.upper()
            if fmt not in ("DXF", "DWG"):
                raise ValueError("format is DXF or DWG")
            path = os.path.abspath(path)
            if not path.lower().endswith("." + fmt.lower()):
                raise ValueError(f"path must end in .{fmt.lower()}")
            if not os.path.isdir(os.path.dirname(path)):
                raise RuntimeError(f"Folder does not exist: {os.path.dirname(path)}")
            hidden = self._FLAT_HIDDEN + (() if bend_lines else self._BEND_LAYERS)
            spec = (f"FLAT PATTERN {fmt}?AcadVersion={acad_version}"
                    f"&OuterProfileLayer=OUTER&InteriorProfilesLayer=INNER"
                    f"&InvisibleLayers={';'.join(hidden)}")
            if os.path.exists(path):
                os.remove(path)
            self._silently(lambda: comp.DataIO.WriteDataToFile(spec, path))
            if not os.path.exists(path):
                raise RuntimeError(f"Inventor wrote no file: {path}")
            info["file"] = path
            info["layers"] = "OUTER (contour), INNER (holes/cut-outs)" + (", IV_BEND* (bend lines)" if bend_lines else "")
        return info

    _DTP = "Design Tracking Properties"
    _PROPS = {"part_number": (_DTP, "Part Number"), "description": (_DTP, "Description"),
              "stock_number": (_DTP, "Stock Number"), "vendor": (_DTP, "Vendor"),
              "revision": ("Inventor Summary Information", "Revision Number"),
              "title": ("Inventor Summary Information", "Title"),
              "material": (_DTP, "Material"), "cost_center": (_DTP, "Cost Center")}

    @classmethod
    def _prop(cls, doc, key):
        try:
            group, name = cls._PROPS[key]
            return doc.PropertySets.Item(group).Item(name).Value
        except Exception:
            return None

    def _mass_kg(self, doc):
        try:
            return round(doc.ComponentDefinition.MassProperties.Mass, 3)
        except Exception:
            return None

    def bom(self, view: str = "parts_only", path: str = "") -> str:
        """Bill of materials of the active assembly — parts_only (flat, totals per part)
        or structured (with sub-assemblies, indented). path: also write a CSV (UTF-8, ;)."""
        doc = self.active_document()
        if doc.DocumentType != kAssemblyDocumentObject:
            raise RuntimeError("The active document is not an assembly.")
        b = doc.ComponentDefinition.BOM
        structured = view.lower().startswith("struct")
        if structured:
            b.StructuredViewEnabled = True
            b.StructuredViewFirstLevelOnly = False
        else:
            b.PartsOnlyViewEnabled = True
        wanted = 62466 if structured else 62467  # kStructured / kPartsOnly BOMViewType
        bv = next(b.BOMViews.Item(i) for i in range(1, b.BOMViews.Count + 1)
                  if b.BOMViews.Item(i).ViewType == wanted)
        rows = []

        def walk(collection, depth):
            for i in range(1, collection.Count + 1):
                r = collection.Item(i)
                d = r.ComponentDefinitions.Item(1).Document
                rows.append({"item": r.ItemNumber, "level": depth, "qty": r.ItemQuantity,
                             "part_number": self._prop(d, "part_number") or "",
                             "description": self._prop(d, "description") or "",
                             "material": self._prop(d, "material") or "",
                             "stock_number": self._prop(d, "stock_number") or "",
                             "mass_kg": self._mass_kg(d),
                             "file": os.path.basename(d.FullFileName)})
                if structured and r.ChildRows is not None:
                    walk(r.ChildRows, depth + 1)

        walk(bv.BOMRows, 0)
        cols = ["item", "qty", "part_number", "description", "material", "stock_number", "mass_kg", "file"]
        if path:
            import csv
            path = os.path.abspath(path)
            with open(path, "w", newline="", encoding="utf-8-sig") as f:
                w = csv.writer(f, delimiter=";")
                w.writerow(cols)
                for r in rows:
                    w.writerow([r[c] if r[c] is not None else "" for c in cols])
        lines = [f"BOM {doc.DisplayName} ({'structured' if structured else 'parts only'}, {len(rows)} rows)"]
        for r in rows:
            mass = f"{r['mass_kg']} kg" if r["mass_kg"] is not None else ""
            lines.append(f"{'  ' * r['level']}{r['item']:>4}  {r['qty']:>4} x  {r['part_number']}"
                         f"  | {r['description']} | {r['material']} | {mass} | {r['file']}")
        if path:
            lines.append(f"CSV: {path}")
        return "\n".join(lines)

    def properties(self, values: dict | None = None, material: str = "", mass_kg: float = 0) -> dict:
        """Read the active document's iProperties; set the given ones first. values keys:
        part_number, description, stock_number, vendor, revision, title, cost_center, or
        any other name (a custom property). material: a material name from the library."""
        doc = self.active_document()
        changed = []
        for key, value in (values or {}).items():
            if key in self._PROPS and key != "material":
                group, name = self._PROPS[key]
                doc.PropertySets.Item(group).Item(name).Value = str(value)
            else:
                custom = doc.PropertySets.Item("Inventor User Defined Properties")
                try:
                    custom.Item(key).Value = value
                except Exception:
                    custom.Add(value, key)
            changed.append(key)
        if material:
            self._set_material(doc, material)
            changed.append("material")
        if mass_kg:
            # surface-body vendor models have no volume, so no mass: give it by hand
            doc.ComponentDefinition.MassProperties.Mass = float(mass_kg)
            changed.append("mass")
        out = {k: self._prop(doc, k) for k in self._PROPS}
        custom = doc.PropertySets.Item("Inventor User Defined Properties")
        out["custom"] = {custom.Item(i).Name: custom.Item(i).Value for i in range(1, custom.Count + 1)}
        out["mass_kg"] = self._mass_kg(doc)
        try:
            if doc.ComponentDefinition.MassProperties.MassOverridden:
                out["mass_overridden"] = True
        except Exception:
            pass
        if changed:
            out["changed"] = changed
        return out

    def _set_material(self, doc, name: str):
        if doc.DocumentType != kPartDocumentObject:
            raise RuntimeError("Material is set on parts.")
        key = name.lower()
        for i in range(1, doc.MaterialAssets.Count + 1):  # already in the document
            a = doc.MaterialAssets.Item(i)
            if a.DisplayName.lower() == key:
                doc.ActiveMaterial = a
                return
        found = self._material_index().get(key)
        if found is None:
            import difflib
            close = difflib.get_close_matches(key, list(self._material_index()), n=5, cutoff=0.5)
            names = [self._material_index()[c][0] for c in close]
            raise RuntimeError(f"No material {name!r} in the libraries."
                               + (f" Close: {', '.join(names)}" if names else " See list_materials."))
        lib, idx = found[1], found[2]
        doc.ActiveMaterial = self.app.AssetLibraries.Item(lib).MaterialAssets.Item(idx).CopyTo(doc)

    def _material_index(self) -> dict:
        """lower name -> (name, library index, asset index) over every material library —
        read once per connection, the libraries hold hundreds of materials."""
        index = self.__dict__.get("_materials")
        if index is None:
            index = {}
            libs = self.app.AssetLibraries
            for i in range(1, libs.Count + 1):
                assets = libs.Item(i).MaterialAssets
                for j in range(1, assets.Count + 1):
                    name = assets.Item(j).DisplayName
                    index.setdefault(name.lower(), (name, i, j))
            self._materials = index
        return index

    def list_materials(self, search: str = "") -> list[str]:
        key = search.lower()
        names = [n for n, _, _ in self._material_index().values() if key in n.lower()]
        return sorted(names, key=lambda n: (not n.lower().startswith(key), n.lower()))

    _GENERIC = ("generic", "default")

    def _part_docs(self, path: str = "") -> list:
        """The active part, or every distinct part document in the active assembly (below
        `path` if given), first occurrence order."""
        doc = self.active_document()
        if doc.DocumentType == kPartDocumentObject:
            return [doc]
        if doc.DocumentType != kAssemblyDocumentObject:
            raise RuntimeError("Open a part or an assembly.")
        if path:
            root = self._occ(path)
            leaves = [o for _, o in self._leaves(root, path)]
        else:
            all_leaves = doc.ComponentDefinition.Occurrences.AllLeafOccurrences
            leaves = [all_leaves.Item(i) for i in range(1, all_leaves.Count + 1)]
        seen, docs = set(), []
        for o in leaves:
            try:
                d = o.Definition.Document
            except Exception:
                continue
            key = d.FullFileName or d.DisplayName
            if key not in seen and d.DocumentType == kPartDocumentObject:
                seen.add(key)
                docs.append(d)
        return docs

    def _part_row(self, d, folders) -> dict:
        comp = d.ComponentDefinition
        row = {"part": d.DisplayName, "material": self._material_name(d), "mass_kg": self._mass_kg(d)}
        try:
            row["bodies"] = comp.SurfaceBodies.Count
            surfaces = self._surface_bodies(comp)
            if surfaces:
                row["surface_bodies"] = surfaces
        except Exception:
            pass
        try:
            if comp.MassProperties.MassOverridden:
                row["mass_overridden"] = True
        except Exception:
            pass
        why = self._library_reason(d, folders)
        if why:
            row["library"] = why
        return row

    def _part_row_read(self, r: dict, folders) -> dict:
        """_part_row from what rules/read_parts.iLogicVb read."""
        row = {"part": r["name"], "material": r["material"],
               "mass_kg": None if r["mass"] is None else round(r["mass"], 3)}
        if r["bodies"] >= 0:
            row["bodies"] = r["bodies"]
            if r["surfaces"]:
                row["surface_bodies"] = r["surfaces"]
        if r["overridden"]:
            row["mass_overridden"] = True
        why = "Content Center member" if r["content"] else (self._path_reason(r["file"], folders) if r["file"] else "")
        if why:
            row["library"] = why
        return row

    @staticmethod
    def _surface_bodies(comp) -> int:
        """Bodies without volume: open shells among the solids, and surfaces (WorkSurfaces —
        where an imported vendor model's faces often end up)."""
        n = 0
        bodies = comp.SurfaceBodies
        n += sum(1 for i in range(1, bodies.Count + 1) if not bodies.Item(i).IsSolid)
        try:
            ws = comp.WorkSurfaces
            n += sum(ws.Item(i).SurfaceBodies.Count for i in range(1, ws.Count + 1))
        except Exception:
            pass
        return n

    @staticmethod
    def _material_name(d) -> str:
        try:
            return d.ActiveMaterial.DisplayName
        except Exception:
            try:
                return d.ComponentDefinition.Material.Name
            except Exception:
                return "?"

    def material_report(self, path: str = "", only_problems: bool = False) -> dict:
        """Material and mass of every part (active part / assembly, below path if given),
        grouped, with what needs a look: Generic material, surface bodies (their volume — and
        mass — is missing: set mass_kg), zero mass."""
        folders = self.library_folders()
        read = None if path else self._read_parts()     # inside Inventor; through COM it is a dozen calls a part
        if read is not None:
            rows = [self._part_row_read(r, folders) for r in read]
        else:
            rows = [self._part_row(d, folders) for d in self._part_docs(path)]
        problems = []
        for r in rows:
            why = []
            if r["material"].lower() in self._GENERIC and "library" not in r:
                why.append("generic material")
            if r.get("surface_bodies") and not r.get("mass_overridden"):
                why.append(f"{r['surface_bodies']} surface body(ies) — no volume, mass too low; set mass_kg")
            if not r.get("mass_kg") and not r.get("surface_bodies"):
                why.append("no mass" + (" — a Content Center member without a length? give it one"
                                        if r.get("library") == "Content Center member" else ""))
            if why:
                problems.append(f"{r['part']}: {'; '.join(why)}")
        by_material = {}
        for r in rows:
            by_material[r["material"]] = by_material.get(r["material"], 0) + 1
        out = {"parts": len(rows), "by_material": by_material,
               "total_mass_kg": round(sum(r.get("mass_kg") or 0 for r in rows), 3),
               "problems": problems}
        if not only_problems:
            out["rows"] = rows
        return out

    def set_material(self, material: str, parts: str = "*", only_generic: bool = False,
                     path: str = "") -> str:
        """Give every matching part (active part / assembly, below path) a material.
        parts: name patterns, ","-separated ("*PLATE*,BRACKET-?"), matched on the part name
        and file name, case-insensitive. Library / Content Center parts are left alone."""
        import fnmatch
        pats = [p.strip().lower() for p in parts.split(",") if p.strip()] or ["*"]
        folders = self.library_folders()
        changed, kept, skipped = [], [], []
        for d in self._part_docs(path):
            names = [d.DisplayName.lower(), os.path.basename(d.FullFileName or "").lower()]
            if not any(fnmatch.fnmatch(n, p) for n in names for p in pats):
                continue
            why = self._library_reason(d, folders)
            if why:
                skipped.append(f"{d.DisplayName} ({why})")
                continue
            if only_generic and self._material_name(d).lower() not in self._GENERIC:
                kept.append(d.DisplayName)
                continue
            self._set_material(d, material)
            changed.append(d.DisplayName)
        msg = f"{material}: {len(changed)} part(s)" + (f" — {', '.join(changed[:30])}" if changed else "")
        if kept:
            msg += f"\nKept (already have a material): {len(kept)}"
        if skipped:
            msg += f"\nLeft alone: {', '.join(skipped[:20])}"
        if changed:
            msg += "\nUnsaved — save_document saves the changed parts with the assembly."
        return msg

    def _drawing_template(self, template: str) -> str:
        import glob
        if os.path.isfile(template):
            return template
        root = self.app.FileLocations.TemplatesPath
        for pattern in (os.path.join(root, "Metric", f"{template}.idw"),
                        os.path.join(root, "**", "Metric", f"{template}.idw"),
                        os.path.join(root, "**", f"{template}.idw")):
            found = glob.glob(pattern, recursive=True)
            if found:
                return found[0]
        raise RuntimeError(f"No drawing template {template!r} under {root}")

    _SCALES = (10, 5, 2, 1, 1 / 2, 1 / 5, 1 / 10, 1 / 20, 1 / 50, 1 / 100)

    # Where views go, as fractions of the sheet — all above the title block (bottom right).
    # Projected views take their direction from where they sit relative to the base view:
    # "top" below it and "right" beside it is ISO (first angle) — the template's standard
    # decides what Inventor calls them.
    _SPOTS = {"top": (0.25, 0.30), "right": (0.58, 0.66), "iso": (0.85, 0.66)}

    # Where a section view goes, by the direction of its cutting line: the place of the
    # projected view it would otherwise hide.
    _SECTION_SPOTS = {"vertical": "right", "horizontal": "top"}
    _DETAIL_SPOT = (0.58, 0.36)

    def make_drawing(self, pdf_path: str, views: str = "front,top,right,iso",
                     template: str = "ISO", scale: float = 0, flat: bool = False,
                     drawing_path: str = "", parts_list: bool = False, balloons: bool = False,
                     section: str = "", detail=None, bend_notes: bool = True) -> str:
        """2D drawing of the active (saved) part or assembly: a base view, projected views,
        overall dimensions on the base view and the model dimensions Inventor can retrieve,
        exported to PDF. flat=True draws a sheet metal part's flat pattern (one view), with a
        note on every bend line and a table of the bends (bend_notes).
        parts_list / balloons: assemblies — the list above the title block, a balloon per
        first-level component. section: "vertical" or "horizontal" cut through the base
        view, optionally ":<mm>" off its middle. detail: [x, y, z, radius] in model mm."""
        model = self.active_document()
        if not model.FullFileName:
            raise RuntimeError("Save the model first — a drawing references the file.")
        pdf_path = os.path.abspath(pdf_path)
        if not pdf_path.lower().endswith(".pdf"):
            raise ValueError("pdf_path must end in .pdf")
        names = [v.strip().lower() for v in views.split(",") if v.strip()]
        base_name = "front" if flat else names[0]
        orient = {"front": 10764, "top": 10754, "right": 10755, "left": 10758,
                  "back": 10756, "bottom": 10757}.get(base_name)
        if orient is None:
            raise ValueError("The first view must be front, top, right, left, back or bottom")
        extra = [] if flat else names[1:]
        unknown = [n for n in extra if n not in self._SPOTS]
        if unknown:
            raise ValueError(f"Projected views can be {', '.join(self._SPOTS)} — not {', '.join(unknown)}")
        is_assembly = model.DocumentType == kAssemblyDocumentObject
        if (parts_list or balloons) and not is_assembly:
            raise ValueError("parts_list and balloons are for an assembly")
        if flat and (section or detail is not None):
            raise ValueError("section and detail are for the folded model — not with flat=True")
        cut_kind, cut_offset = "", 0.0
        if section:
            cut_kind, _, off = section.strip().lower().partition(":")
            if cut_kind not in self._SECTION_SPOTS:
                raise ValueError('section is "vertical" or "horizontal", optionally ":<mm>" off the middle')
            cut_offset = float(off) if off.strip() else 0.0
            if self._SECTION_SPOTS[cut_kind] in extra:
                raise ValueError(f"A {cut_kind} section goes where the '{self._SECTION_SPOTS[cut_kind]}' view "
                                 "is — leave that view out of `views`")
        if detail is not None and (len(detail) != 4 or float(detail[3]) <= 0):
            raise ValueError("detail is [x, y, z, radius] in model mm")
        comp = model.ComponentDefinition
        if flat:
            if not getattr(comp, "HasFlatPattern", False):
                self.flat_pattern()  # creates it
            size = (comp.FlatPattern.Length, comp.FlatPattern.Width)
        else:
            rb = comp.RangeBox
            dx, dy, dz = (rb.MaxPoint.X - rb.MinPoint.X, rb.MaxPoint.Y - rb.MinPoint.Y,
                          rb.MaxPoint.Z - rb.MinPoint.Z)
            size = (max(dx, dz), max(dy, dz))
        drawing = self.app.Documents.Add(kDrawingDocumentObject, self._drawing_template(template), True)
        try:
            sheet = drawing.ActiveSheet
            cell = (0.6, 0.6) if flat else (0.28, 0.30)  # share of the sheet for the base view
            if not scale:
                fit = min(sheet.Width * cell[0] / max(size[0], 1e-6), sheet.Height * cell[1] / max(size[1], 1e-6))
                scale = next((s for s in self._SCALES if s <= fit), self._SCALES[-1])
            tg = self._tg()
            pt = lambda fx, fy: tg.CreatePoint2d(sheet.Width * fx, sheet.Height * fy)
            opts = self.app.TransientObjects.CreateNameValueMap()
            if flat:
                opts.Add("SheetMetalFoldedModel", False)
            base = sheet.DrawingViews.AddBaseView(model, pt(0.4, 0.55) if flat else pt(0.25, 0.66),
                                                  scale, orient, 32258, "", "", opts)  # hidden lines removed
            placed = [base_name if not flat else "flat pattern"]
            for name in extra:
                if name == "iso":  # shaded, at half scale so it stays in its corner
                    sheet.DrawingViews.AddProjectedView(base, pt(*self._SPOTS[name]), 32259, scale / 2)
                else:
                    sheet.DrawingViews.AddProjectedView(base, pt(*self._SPOTS[name]), 32258)
                placed.append(name)
            notes = []  # what was added besides the views, and what could not be
            by_name = {placed[0]: base}
            for i, name in enumerate(extra):
                by_name[name] = sheet.DrawingViews.Item(i + 2)
            if cut_kind:
                self._annotate(notes, "section", lambda: self._section_view(
                    sheet, base, cut_kind, cut_offset, scale, pt(*self._SPOTS[self._SECTION_SPOTS[cut_kind]])))
            if detail is not None:
                self._annotate(notes, "detail", lambda: self._detail_view(
                    sheet, base, detail, scale, pt(*self._DETAIL_SPOT)))
            if parts_list:
                self._annotate(notes, "parts list", lambda: self._parts_list(sheet, base))
            if balloons:
                self._annotate(notes, "balloons", lambda: self._balloons(sheet, by_name.get("iso", base), comp))
            if flat and bend_notes:
                self._annotate(notes, "bends", lambda: self._bend_notes(sheet, base))
            overall = self._overall_dimensions(sheet, base)
            retrieved = 0
            try:
                gd = sheet.DrawingDimensions.GeneralDimensions
                for i in range(1, sheet.DrawingViews.Count + 1):
                    v = sheet.DrawingViews.Item(i)
                    got = gd.GetRetrievableDimensions(v)
                    if got is not None and got.Count:
                        gd.Retrieve(v, got)
                        retrieved += got.Count
            except Exception:
                pass
            if drawing_path:
                drawing_path = os.path.abspath(drawing_path)
                self._silently(lambda: drawing.SaveAs(drawing_path, False))
            self._export_pdf(drawing, pdf_path)
            ratio = f"{scale:g}:1" if scale >= 1 else f"1:{1 / scale:g}"
            return (f"Drawing of {model.DisplayName}: {', '.join(placed)}, scale {ratio}, "
                    f"overall {' x '.join(overall) or '-'} mm, {retrieved} retrieved dimension(s), "
                    f"sheet {sheet.Width * 10:.0f}x{sheet.Height * 10:.0f} mm"
                    + "".join(f"\n{n}" for n in notes)
                    + f"\nPDF: {pdf_path} ({os.path.getsize(pdf_path) // 1024} KB)"
                    + (f"\nDrawing: {drawing_path}" if drawing_path else ""))
        finally:
            if not drawing_path:
                drawing.Close(True)

    @staticmethod
    def _annotate(notes: list, what: str, fn):
        """Run one annotation step; the drawing is still worth having if it fails."""
        try:
            notes.append(fn())
        except Exception as e:
            notes.append(f"{what}: FAILED — {describe_com_error(e)}")

    def _section_view(self, sheet, base, kind: str, offset_mm: float, scale: float, position) -> str:
        """A full section of the base view along a vertical / horizontal line through its
        middle (offset_mm in model mm, to the right / up)."""
        tg = self._tg()
        off = offset_mm / 10 * scale
        cx, cy = base.Center.X, base.Center.Y
        w, h = base.Width / 2 + 0.8, base.Height / 2 + 0.8
        ends = (((cx + off, cy + h), (cx + off, cy - h)) if kind == "vertical"
                else ((cx - w, cy + off), (cx + w, cy + off)))
        sketch = base.Sketches.Add()
        sketch.Edit()
        try:
            a, b = (sketch.SheetToSketchSpace(tg.CreatePoint2d(*e)) for e in ends)
            sketch.SketchLines.AddByTwoPoints(a, b)
        finally:
            sketch.ExitEdit()
        view = sheet.DrawingViews.AddSectionView(base, sketch, position, 32258, scale, True, "A")
        return f"section A-A ({kind}" + (f", {offset_mm:g} mm off the middle" if offset_mm else "") + f"): {view.Name}"

    def _detail_view(self, sheet, base, detail, scale: float, position) -> str:
        """A circular detail of the base view around a model point, at the next larger scale."""
        x, y, z, r = (float(v) for v in detail)
        center = base.ModelToSheetSpace(self._tg().CreatePoint(x / 10, y / 10, z / 10))
        bigger = min((s for s in self._SCALES if s >= scale * 2), default=scale * 2)
        view = sheet.DrawingViews.AddDetailView(base, position, 32258, True, center, r / 10 * scale,
                                                None, bigger, True, "B")
        ratio = f"{bigger:g}:1" if bigger >= 1 else f"1:{1 / bigger:g}"
        return f"detail B (R{r:g} mm at {x:g}, {y:g}, {z:g}), scale {ratio}: {view.Name}"

    @staticmethod
    def _boxes_overlap(a, b) -> bool:
        return not (a.MaxPoint.X <= b.MinPoint.X or b.MaxPoint.X <= a.MinPoint.X
                    or a.MaxPoint.Y <= b.MinPoint.Y or b.MaxPoint.Y <= a.MinPoint.Y)

    def _seat_table(self, sheet, table):
        """Move a table so that it sits on the title block, right edges in line (or into the
        border's top right corner when the sheet has no title block)."""
        box = table.RangeBox
        try:
            tb = sheet.TitleBlock.RangeBox
            dx, dy = tb.MaxPoint.X - box.MaxPoint.X, tb.MaxPoint.Y - box.MinPoint.Y
        except Exception:
            try:
                corner = sheet.Border.RangeBox.MaxPoint
                dx, dy = corner.X - box.MaxPoint.X, corner.Y - box.MaxPoint.Y
            except Exception:
                return
        pos = table.Position
        table.Position = self._tg().CreatePoint2d(pos.X + dx, pos.Y + dy)

    def _table_clash(self, sheet, table) -> str:
        box = table.RangeBox
        hit = [v.Name for v in (sheet.DrawingViews.Item(i) for i in range(1, sheet.DrawingViews.Count + 1))
               if box.MaxPoint.X > v.Left and box.MinPoint.X < v.Left + v.Width
               and box.MaxPoint.Y > v.Top - v.Height and box.MinPoint.Y < v.Top]
        return f" — it COVERS {', '.join(hit)}: fewer views, or a smaller scale" if hit else ""

    def _parts_list(self, sheet, base) -> str:
        """The assembly's first-level parts list, seated on the title block."""
        table = sheet.PartsLists.Add(base, self._tg().CreatePoint2d(sheet.Width * 0.9, sheet.Height * 0.9),
                                     46593)  # kStructured: first-level components
        self._seat_table(sheet, table)
        return f"parts list: {table.PartsListRows.Count} row(s)" + self._table_clash(sheet, table)

    def _balloons(self, sheet, view, comp) -> str:
        """A balloon per first-level component (one for each file), pointing at one of its
        edges in the view, set on a ring around the view."""
        tg = self._tg()
        cx, cy = view.Center.X, view.Center.Y
        ring = math.hypot(view.Width, view.Height) / 2 + 1.0
        # The outermost edge of each first-level component in the view. (DrawingCurves(occ)
        # is a type mismatch late-bound, so every curve is asked whose edge it is.)
        outermost = {}
        for j in range(1, view.DrawingCurves.Count + 1):
            c = view.DrawingCurves.Item(j)
            try:
                owner = c.ModelGeometry.ContainingOccurrence
                name = self._path_of(owner).split("/")[0]
                m, how = c.MidPoint, 57859            # kMidPointIntent
                if m is None:                          # a full circle has no middle
                    m, how = c.CenterPoint, 57863      # kCircularTopPointIntent
            except Exception:
                continue
            if m is None:
                continue
            d = math.hypot(m.X - cx, m.Y - cy)
            if name not in outermost or d > outermost[name][0]:
                outermost[name] = (d, c, m, how)
        targets, seen, hidden = [], set(), []
        for i in range(1, comp.Occurrences.Count + 1):
            occ = comp.Occurrences.Item(i)
            key = occ.Definition.Document.InternalName
            if key in seen:
                continue
            best = outermost.get(occ.Name)
            if best is None:
                continue
            seen.add(key)
            targets.append((math.atan2(best[2].Y - cy, best[2].X - cx), best[1], occ.Name, best[3]))
        for i in range(1, comp.Occurrences.Count + 1):
            occ = comp.Occurrences.Item(i)
            if occ.Definition.Document.InternalName not in seen and occ.Name not in hidden:
                hidden.append(occ.Name)
        targets.sort(key=lambda t: t[0])
        gap = 1.4 / ring  # balloons this far apart on the ring don't touch
        placed, failed, last = 0, [], None
        for angle, curve, name, how in targets:
            if last is not None and angle - last < gap:
                angle = last + gap
            last = angle
            try:
                points = self.app.TransientObjects.CreateObjectCollection()
                points.Add(tg.CreatePoint2d(cx + ring * math.cos(angle), cy + ring * math.sin(angle)))
                try:
                    intent = sheet.CreateGeometryIntent(curve, how)
                except Exception:
                    intent = sheet.CreateGeometryIntent(curve)
                points.Add(intent)
                sheet.Balloons.Add(points)
                placed += 1
            except Exception as e:
                failed.append(f"{name} ({describe_com_error(e)})")
        return (f"balloons: {placed} on {view.Name}"
                + (f"; not visible in that view: {', '.join(hidden[:15])}" if hidden else "")
                + (f"; FAILED: {'; '.join(failed[:8])}" if failed else ""))

    def _bend_notes(self, sheet, view) -> str:
        """A bend note (direction, angle, radius) on every bend line of a flat pattern view,
        and a table that counts the bends of each kind — what the press brake needs set up."""
        notes = sheet.DrawingNotes.BendNotes
        texts, failed = [], 0
        for i in range(1, view.DrawingCurves.Count + 1):
            c = view.DrawingCurves.Item(i)
            try:
                if c.EdgeType not in (82690, 82691):  # kBendUpEdge, kBendDownEdge
                    continue
            except Exception:
                continue
            try:
                texts.append(" ".join(notes.Add(c).Text.split()))
            except Exception:
                failed += 1
        if not texts and not failed:
            return "bends: none in this part"
        kinds = {}
        for t in texts:
            kinds[t] = kinds.get(t, 0) + 1
        rows = sorted(kinds.items(), key=lambda kv: (-kv[1], kv[0]))
        out = f"bends: {len(texts)} noted — " + ", ".join(f"{n} x {t}" for t, n in rows)
        if failed:
            out += f"; {failed} bend line(s) got no note"
        try:
            # Late-bound, the two string arrays go as arrays of variants, which Add refuses
            # (E_INVALIDARG) — so the call is made with its argument types spelled out.
            tables = sheet.CustomTables
            strings = pythoncom.VT_ARRAY | pythoncom.VT_BSTR
            cells = [v for t, n in rows for v in (str(n), t)]
            table = win32com.client.Dispatch(tables._oleobj_.InvokeTypes(
                tables._oleobj_.GetIDsOfNames("Add"), 0, pythoncom.DISPATCH_METHOD, (pythoncom.VT_DISPATCH, 0),
                ((pythoncom.VT_BSTR, 1), (pythoncom.VT_DISPATCH, 1), (pythoncom.VT_I4, 1), (pythoncom.VT_I4, 1),
                 (strings, 1), (strings, 1), (pythoncom.VT_ARRAY | pythoncom.VT_R8, 1)),
                "BENDS", self._tg().CreatePoint2d(sheet.Width * 0.9, sheet.Height * 0.9),
                2, len(rows), ["QTY", "BEND"], cells, [1.2, 4.8]))  # column widths, cm
            self._seat_table(sheet, table)
            out += self._table_clash(sheet, table)
        except Exception as e:
            out += f"; bend table FAILED — {describe_com_error(e)}"
        return out

    def _overall_dimensions(self, sheet, view) -> list[str]:
        """Overall width and height on a view: between its outermost vertical lines and its
        outermost horizontal lines. Returns the values (mm) it placed."""
        tg = self._tg()
        lines = []
        for i in range(1, view.DrawingCurves.Count + 1):
            c = view.DrawingCurves.Item(i)
            try:
                if c.CurveType == 5123:  # kLineSegmentCurve
                    lines.append((c, c.StartPoint, c.EndPoint))
            except Exception:
                pass
        vertical = [l for l in lines if abs(l[1].X - l[2].X) < 1e-4]
        horizontal = [l for l in lines if abs(l[1].Y - l[2].Y) < 1e-4]
        gd = sheet.DrawingDimensions.GeneralDimensions
        out = []
        cx, cy = view.Center.X, view.Center.Y
        for group, key, pos, kind in (
                (vertical, lambda l: l[1].X, tg.CreatePoint2d(cx, view.Top + 1.2), 60162),          # horizontal dim
                (horizontal, lambda l: l[1].Y, tg.CreatePoint2d(view.Left - 1.2, cy), 60163)):      # vertical dim
            if len(group) < 2:
                continue
            a, b = min(group, key=key), max(group, key=key)
            try:
                dim = gd.AddLinear(pos, sheet.CreateGeometryIntent(a[0]), sheet.CreateGeometryIntent(b[0]), kind)
                out.append(f"{dim.ModelValue * 10:g}")
            except Exception:
                pass
        return out

    @staticmethod
    def _nvm_set(nvm, key, value):
        """NameValueMap[key] = value. Value is a property with a parameter, which late-bound
        pywin32 can only read — so put it through IDispatch directly."""
        try:
            nvm.Add(key, value)
        except Exception:
            dispid = nvm._oleobj_.GetIDsOfNames("Value")
            nvm._oleobj_.Invoke(dispid, 0, pythoncom.DISPATCH_PROPERTYPUT, 0, key, value)

    def _export_pdf(self, drawing, path: str):
        addin = self.app.ApplicationAddIns.ItemById("{0AC6FD96-2F4D-42CE-8BE0-8AEA580399E4}")
        if not addin.Activated:
            addin.Activate()
        context = self.app.TransientObjects.CreateTranslationContext()
        context.Type = 13059  # kFileBrowseIOMechanism
        options = self.app.TransientObjects.CreateNameValueMap()
        if addin.HasSaveCopyAsOptions(drawing, context, options):
            for key, value in (("All_Color_AS_Black", 0), ("Vector_Resolution", 400),
                               ("Sheet_Range", 14082)):  # kPrintAllSheets
                self._nvm_set(options, key, value)
        medium = self.app.TransientObjects.CreateDataMedium()
        medium.FileName = path
        if os.path.exists(path):
            os.remove(path)
        self._silently(lambda: addin.SaveCopyAs(drawing, context, options, medium))
        if not os.path.exists(path):
            raise RuntimeError(f"PDF export wrote no file: {path}")

    # ---------- Can it be made? ----------

    _SHEETS = SHEETS

    def _strokes(self, edge) -> list:
        """An edge as a polyline of (x, y) in mm (flat pattern edges lie in one plane)."""
        ev = edge.Evaluator
        lo, hi = ev.GetParamExtents()
        got = ev.GetStrokes(lo, hi, 0.002)
        coords = next(v for v in got if isinstance(v, (tuple, list)))
        return [(coords[i] * 10, coords[i + 1] * 10) for i in range(0, len(coords) - 2, 3)]

    def _flat_geometry(self, comp) -> tuple:
        """Contour, holes and bend lines of the flat pattern (mm), for flat_pattern_problems."""
        fp = comp.FlatPattern
        top = fp.TopFace
        outer, holes = [], []
        for i in range(1, top.EdgeLoops.Count + 1):
            loop = top.EdgeLoops.Item(i)
            lines = [self._strokes(loop.Edges.Item(j)) for j in range(1, loop.Edges.Count + 1)]
            if loop.IsOuterEdgeLoop:
                outer += lines
                continue
            diameter = None
            if loop.Edges.Count == 1 and loop.Edges.Item(1).GeometryType == 5124:  # kCircleCurve
                diameter = round(loop.Edges.Item(1).Geometry.Radius * 20, 3)
            holes.append({"points": [p for line in lines for p in line], "diameter": diameter})
        bends, skipped = [], 0
        results = fp.FlatBendResults
        for i in range(1, results.Count + 1):
            r = results.Item(i)
            try:
                if r.IsOnBottomFace:  # every bend is listed twice: top and bottom face
                    continue
            except Exception:
                pass
            try:
                e = r.Edge
                a, b = e.StartVertex.Point, e.StopVertex.Point
                bends.append({"a": (a.X * 10, a.Y * 10), "b": (b.X * 10, b.Y * 10),
                              "radius": round(r.InnerRadius * 10, 3),
                              "angle": round(math.degrees(r.Angle), 2), "up": bool(r.IsDirectionUp)})
            except Exception:
                skipped += 1  # a rolled bend has no straight centre line
        return outer, holes, bends, skipped

    def _sheet_facts_com(self, comp) -> dict:
        """Rule, thickness and flat pattern of a sheet metal part that has one, read through COM."""
        fp = comp.FlatPattern
        outer, holes, bends, skipped = self._flat_geometry(comp)
        return {"rule": comp.ActiveSheetMetalStyle.Name, "thickness": round(comp.Thickness.Value * 10, 3),
                "has_flat": True,
                "flat": sorted((round(fp.Length * 10, 2), round(fp.Width * 10, 2)), reverse=True),
                "outer": outer, "holes": holes, "bends": bends, "skipped": skipped}

    def check_sheet_metal_part(self, v_die: float = 0, min_flange: float = 0, min_hole_to_bend: float = 0,
                               sheet: str = "", brake_length: float = 0) -> dict:
        """Can the active sheet metal part be cut and bent? Rule, flat size against the
        sheet, flange lengths and holes against the V die. Unfolds the part if it has no flat
        pattern yet; the flat pattern is read inside Inventor when that is possible."""
        doc = self.active_document()
        comp = doc.ComponentDefinition
        if not hasattr(comp, "HasFlatPattern"):
            raise RuntimeError(f"{doc.DisplayName} is not a sheet metal part.")
        t = round(comp.Thickness.Value * 10, 3)
        rule = comp.ActiveSheetMetalStyle.Name
        try:
            self.flat_pattern()
        except Exception as e:
            return judge_sheet(doc.DisplayName, {"rule": rule, "thickness": t}, v_die, min_flange,
                               min_hole_to_bend, sheet, brake_length, flat_error=describe_com_error(e))
        rows = self._read_parts(flat=True)
        read = rows[0].get("sheet") if rows else None
        facts = sheet_facts(read) if read and "outer" in read else self._sheet_facts_com(comp)
        return judge_sheet(doc.DisplayName, facts, v_die, min_flange, min_hole_to_bend, sheet, brake_length)

    def check_clearances(self, clearance: float = 0.3, parts: list[str] | None = None,
                         max_pairs: int = 600) -> dict:
        """Parts of the active assembly that are closer than `clearance` mm without touching
        — no room for the powder coat. Touching parts (clamped, screwed) are fine; fasteners
        are left out. parts: name patterns to look at (default: every part)."""
        comp = self.active_assembly()
        if parts:
            occs = self._occ_set(parts)
        else:
            leaves = comp.Occurrences.AllLeafOccurrences
            occs = [leaves.Item(i) for i in range(1, leaves.Count + 1)]
        occs = [o for o in occs if not self._is_fastener(o)]
        pad = clearance / 10
        boxes = []
        for o in occs:
            rb = o.RangeBox
            boxes.append((rb.MinPoint.X - pad, rb.MinPoint.Y - pad, rb.MinPoint.Z - pad,
                          rb.MaxPoint.X + pad, rb.MaxPoint.Y + pad, rb.MaxPoint.Z + pad))
        pairs = [(i, j) for i in range(len(occs)) for j in range(i + 1, len(occs))
                 if all(boxes[i][k] <= boxes[j][k + 3] and boxes[j][k] <= boxes[i][k + 3] for k in range(3))]
        tight, touching, failed = [], 0, 0
        measure = self.app.MeasureTools
        for i, j in pairs[:max_pairs]:
            try:
                d = measure.GetMinimumDistance(occs[i], occs[j])
                d = (d[0] if isinstance(d, (tuple, list)) else d) * 10
            except Exception:
                failed += 1
                continue
            if d < 0.005:
                touching += 1
            elif d < clearance - 1e-6:
                tight.append({"a": self._path_of(occs[i]), "b": self._path_of(occs[j]), "gap_mm": round(d, 3)})
        tight.sort(key=lambda r: r["gap_mm"])
        notes = []
        if len(pairs) > max_pairs:
            notes.append(f"{len(pairs) - max_pairs} neighbouring pair(s) were not measured — "
                         "narrow it down with `parts`")
        if failed:
            notes.append(f"{failed} pair(s) could not be measured")
        return {"clearance_mm": clearance, "parts": len(occs), "pairs_measured": min(len(pairs), max_pairs),
                "touching": touching, "too_close": tight, "notes": notes}
