"""Wrapper around Autodesk Inventor COM API."""

import os
import win32com.client
import win32com.client.gencache
import pythoncom
import win32process

# Never wrap COM objects in makepy (gen_py) classes, even when a cache exists. Something
# else on the machine — a script calling CastTo or gencache.EnsureDispatch — creates one
# for the Inventor type library, and from then on GetActiveObject fails with
# KeyError '_dispobj_' and a generic Document hides ComponentDefinition. Late-bound
# objects (with type info) expose every member of the real object.
win32com.client.gencache.GetClassForCLSID = lambda clsid: None


# Inventor 2026 enum constants (from type library)
# PartFeatureExtentDirectionEnum
kPositiveExtentDirection = 20993
kNegativeExtentDirection = 20994
kSymmetricExtentDirection = 20995

# PartFeatureOperationEnum
kJoinOperation = 20481
kCutOperation = 20482
kIntersectOperation = 20483
kSurfaceOperation = 20484
kNewBodyOperation = 20485

# DocumentTypeEnum
kPartDocumentObject = 12290
kAssemblyDocumentObject = 12291

# DimensionOrientationEnum (from Inventor 2026 type library — NOT 40706/40707/40708!)
kHorizontalDim = 19201
kVerticalDim   = 19202
kAlignedDim    = 19203

# CurveTypeEnum (Edge.GeometryType) — verified on 2026/2027
kLineSegmentCurve = 5123
kCircleCurve      = 5124


def modal_dialogs(main_hwnd) -> list[dict]:
    """Modal dialogs Inventor is showing, read from the window list — no COM call,
    so it works while an API call is stuck behind the dialog.

    A modal dialog disables Inventor's main window; the dialog is then a visible,
    enabled top-level window of the same process. Returns [{title, text}].
    """
    import win32gui
    import win32process

    if not main_hwnd or not win32gui.IsWindow(main_hwnd) or win32gui.IsWindowEnabled(main_hwnd):
        return []
    pid = win32process.GetWindowThreadProcessId(main_hwnd)[1]
    dialogs = []

    def child_text(hwnd, texts):
        t = win32gui.GetWindowText(hwnd).strip()
        if t and win32gui.IsWindowVisible(hwnd) and t not in texts:
            texts.append(t)
        return True

    def top_level(hwnd, _):
        if (hwnd != main_hwnd and win32gui.IsWindowVisible(hwnd) and win32gui.IsWindowEnabled(hwnd)
                and win32process.GetWindowThreadProcessId(hwnd)[1] == pid):
            texts = []
            try:
                win32gui.EnumChildWindows(hwnd, child_text, texts)
            except win32gui.error:
                pass
            dialogs.append({"title": win32gui.GetWindowText(hwnd), "text": " | ".join(texts)[:300]})
        return True

    win32gui.EnumWindows(top_level, None)
    return dialogs


def inventor_pids() -> list[int]:
    """Process ids of every running Inventor."""
    import subprocess
    out = subprocess.run(["tasklist", "/FI", "IMAGENAME eq Inventor.exe", "/FO", "CSV", "/NH"],
                         capture_output=True, text=True, creationflags=subprocess.CREATE_NO_WINDOW).stdout
    return [int(row.split('","')[1]) for row in out.splitlines() if row.startswith('"Inventor.exe"')]


class InventorConnection:
    def __init__(self):
        self._app = None
        self.main_hwnd = None  # Inventor's main window, for modal_dialogs()

    @property
    def app(self):
        if self._app is None:
            raise RuntimeError(
                "Not connected to Inventor. Call connect() first."
            )
        return self._app

    def connect(self, visible: bool = True, new_instance: bool = False) -> str:
        pythoncom.CoInitialize()
        try:
            if new_instance:
                raise LookupError  # DispatchEx always starts a separate Inventor
            self._app = win32com.client.GetActiveObject("Inventor.Application")
            msg = f"Connected to running Inventor {self._app.SoftwareVersion.DisplayVersion}"
        except Exception:
            try:
                self._app = win32com.client.DispatchEx("Inventor.Application")
                self._app.Visible = visible
                msg = f"Started Inventor {self._app.SoftwareVersion.DisplayVersion}"
            except Exception as e:
                raise RuntimeError(f"Cannot connect to Inventor: {e}")
        try:
            self.main_hwnd = int(self._app.MainFrameHWND)
        except Exception:
            self.main_hwnd = None
        return msg

    @property
    def pid(self) -> int | None:
        """Inventor's process id — from the main window, so no COM call (any thread)."""
        try:
            return win32process.GetWindowThreadProcessId(self.main_hwnd)[1] if self.main_hwnd else None
        except Exception:
            return None

    def project_info(self) -> dict:
        p = self.app.DesignProjectManager.ActiveDesignProject
        return {"name": p.Name, "path": p.FullFileName}

    def activate_project(self, path: str) -> str:
        """Make an .ipj the active project. Inventor refuses while documents are open."""
        path = os.path.abspath(path)
        if not os.path.isfile(path):
            raise RuntimeError(f"Project file not found: {path}")
        pm = self.app.DesignProjectManager
        same = lambda p: os.path.normcase(p.FullFileName) == os.path.normcase(path)
        if same(pm.ActiveDesignProject):
            return f"Project {pm.ActiveDesignProject.Name} is already active."
        if self.app.Documents.Count:
            raise RuntimeError("Inventor only switches projects with no documents open — close them "
                               "first, or connect(new_instance=True, project=...) for a separate Inventor.")
        projects = pm.DesignProjects
        proj = next((p for p in (projects.Item(i) for i in range(1, projects.Count + 1)) if same(p)), None)
        if proj is None:
            proj = projects.AddExisting(path)
        proj.Activate(False)  # False: this session only, the default project stays the user's
        return f"Project {proj.Name} is active."

    def is_connected(self) -> bool:
        if self._app is None:
            return False
        try:
            _ = self._app.SoftwareVersion
            return True
        except Exception:
            self._app = None
            return False

    # ---------- Document management ----------

    def create_part(self, name: str | None = None, template: str = "metric") -> str:
        import glob

        templates_path = self.app.FileLocations.TemplatesPath

        if template.lower() in ("sheet_metal", "sheetmetal", "sm"):
            # Sheet Metal template (metric mm)
            candidates_patterns = [
                os.path.join(templates_path, "Metric", "Sheet Metal (mm).ipt"),
                os.path.join(templates_path, "**", "Sheet Metal (mm).ipt"),
                os.path.join(templates_path, "Sheet Metal.ipt"),
            ]
            template_file = None
            for pattern in candidates_patterns:
                if "*" in pattern:
                    found = glob.glob(pattern, recursive=True)
                    if found:
                        template_file = found[0]
                        break
                elif os.path.exists(pattern):
                    template_file = pattern
                    break
            if template_file is None:
                raise RuntimeError(f"Cannot find Sheet Metal template in {templates_path}")
        elif template.lower() == "metric":
            # Search for metric template (mm)
            candidates_patterns = [
                os.path.join(templates_path, "Metric", "Standard (mm).ipt"),
                os.path.join(templates_path, "Metric", "Standard.ipt"),
                os.path.join(templates_path, "**", "Standard (mm).ipt"),
            ]
            template_file = None
            for pattern in candidates_patterns:
                if "*" in pattern:
                    found = glob.glob(pattern, recursive=True)
                    if found:
                        template_file = found[0]
                        break
                elif os.path.exists(pattern):
                    template_file = pattern
                    break
            if template_file is None:
                # Fallback: any .ipt template
                fallback = glob.glob(os.path.join(templates_path, "**", "*.ipt"), recursive=True)
                if fallback:
                    template_file = fallback[0]
                else:
                    raise RuntimeError(f"Cannot find metric template in {templates_path}")
        elif template.lower() == "english":
            candidates_patterns = [
                os.path.join(templates_path, "en-US", "Standard.ipt"),
                os.path.join(templates_path, "Standard.ipt"),
                os.path.join(templates_path, "**", "Standard.ipt"),
            ]
            template_file = None
            for pattern in candidates_patterns:
                if "*" in pattern:
                    found = glob.glob(pattern, recursive=True)
                    if found:
                        template_file = found[0]
                        break
                elif os.path.exists(pattern):
                    template_file = pattern
                    break
            if template_file is None:
                raise RuntimeError(f"Cannot find English template in {templates_path}")
        else:
            # template is a direct file path
            template_file = template
            if not os.path.exists(template_file):
                raise RuntimeError(f"Template not found: {template_file}")

        doc = self.app.Documents.Add(12290, template_file, True)
        if name:
            try:
                doc.DisplayName = name
            except Exception:
                pass  # DisplayName may be read-only before first save
        # Documents.Add(..., True) opens the window but does not always make it the
        # ActiveDocument — later calls would then edit the previous document.
        doc.Activate()
        return f"Part created: {doc.DisplayName} (unsaved — use save_document(path))"

    def active_document(self):
        doc = self.app.ActiveDocument
        if doc is None:
            raise RuntimeError("No active document open in Inventor.")
        return doc

    def active_part(self):
        doc = self.active_document()
        if doc.DocumentType != 12290:  # kPartDocumentObject
            raise RuntimeError(
                f"Active document is not a part (type={doc.DocumentType})."
            )
        return doc.ComponentDefinition

    def active_assembly(self):
        doc = self.active_document()
        if doc.DocumentType != kAssemblyDocumentObject:
            raise RuntimeError(
                f"Active document is not an assembly (type={doc.DocumentType}). "
                "Call create_assembly() or activate an .iam document."
            )
        return doc.ComponentDefinition

    def _silently(self, fn):
        """Run fn with Inventor's SilentOperation on: no modal dialogs (e.g. the
        'save dependents?' prompt that otherwise blocks until a user clicks OK)."""
        prev = self.app.SilentOperation
        self.app.SilentOperation = True
        try:
            return fn()
        finally:
            self.app.SilentOperation = prev

    def save_document(self, path: str | None = None) -> str:
        """Save the active document without any dialog.

        Plain Document.Save() opens a modal 'save changes to dependents?' dialog
        whenever a referenced file was updated (e.g. an assembly whose
        sub-assembly re-evaluated), and the API call waits until a user answers.
        Save2(True) saves the dirty dependents too, under SilentOperation.
        """
        doc = self.active_document()
        if path:
            path = os.path.abspath(path)
            self._silently(lambda: doc.SaveAs(path, False))
            return f"Saved as: {path}"
        dirty = [d.DisplayName for d in doc.AllReferencedDocuments if d.Dirty]
        self._silently(lambda: doc.Save2(True))
        extra = f" (+ {len(dirty)} dependent(s): {', '.join(dirty)})" if dirty else ""
        return f"Saved: {doc.FullFileName}{extra}"

    def list_documents(self) -> list[dict]:
        """Open (visible) documents, the active one marked."""
        active = self.app.ActiveDocument
        out = []
        for d in self.app.Documents.VisibleDocuments:
            out.append({"name": d.DisplayName, "path": d.FullFileName or "(unsaved)",
                        "type": {12290: "part", 12291: "assembly", 12292: "drawing"}.get(d.DocumentType, str(d.DocumentType)),
                        "dirty": bool(d.Dirty), "active": active is not None and d.InternalName == active.InternalName})
        return out

    def activate_document(self, name_or_path: str) -> str:
        """Make an open document active, by display name or full path."""
        key = name_or_path.lower()
        for d in self.app.Documents.VisibleDocuments:
            if key in (d.DisplayName.lower(), (d.FullFileName or "").lower()):
                d.Activate()
                return f"Active: {d.DisplayName}"
        names = ", ".join(d.DisplayName for d in self.app.Documents.VisibleDocuments)
        raise ValueError(f"No open document '{name_or_path}'. Open: {names}")

    def export_document(self, path: str, format: str = "STEP") -> str:
        doc = self.active_document()
        path = os.path.abspath(path)

        # Translator add-ins by their fixed class IDs. The display names change
        # between releases ("Autodesk Inventor STEP Translator" vs 2027's
        # "Translator: STEP"), so matching names silently found nothing.
        translators = {
            "STEP": "{90AF7F40-0C01-11D5-8E83-0010B541CD80}",
            "IGES": "{90AF7F44-0C01-11D5-8E83-0010B541CD80}",
            "SAT":  "{89162634-02B6-11D5-8E80-0010B541CD80}",
            "STL":  "{533E9A98-FC3B-11D4-8E7E-0010B541CD80}",   # STL *export*
        }
        format_upper = format.upper()
        if format_upper not in translators:
            raise ValueError(f"Unsupported format: {format}. Use STEP, STL, SAT, or IGES.")
        try:
            addin = self.app.ApplicationAddIns.ItemById(translators[format_upper])
        except Exception:
            raise RuntimeError(f"{format_upper} translator add-in is not installed")
        if not addin.Activated:
            addin.Activate()

        context = self.app.TransientObjects.CreateTranslationContext()
        context.Type = 13059  # kFileBrowseIOMechanism
        options = self.app.TransientObjects.CreateNameValueMap()
        data_medium = self.app.TransientObjects.CreateDataMedium()
        data_medium.FileName = path
        if os.path.exists(path):
            os.remove(path)
        self._silently(lambda: addin.SaveCopyAs(doc, context, options, data_medium))
        if not os.path.exists(path):
            raise RuntimeError(f"{format_upper} export reported success but wrote no file: {path}")
        return f"Exported to: {path} ({format_upper}, {os.path.getsize(path) // 1024} KB)"

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
    ) -> str:
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
        points: list[list[float]],
        sketch_index: int | None = None,
    ) -> str:
        """Draw a closed profile from a list of [x,y] points (mm).
        Lines share endpoints for proper profile recognition by Inventor."""
        sketch = self._get_sketch(sketch_index)
        tg = self._tg()

        if len(points) < 3:
            raise ValueError("Need at least 3 points for a closed profile")

        # First line
        pt1 = tg.CreatePoint2d(points[0][0] / 10.0, points[0][1] / 10.0)
        pt2 = tg.CreatePoint2d(points[1][0] / 10.0, points[1][1] / 10.0)
        first_line = sketch.SketchLines.AddByTwoPoints(pt1, pt2)

        # Subsequent lines — connect to previous endpoint
        prev_line = first_line
        for i in range(2, len(points)):
            next_pt = tg.CreatePoint2d(points[i][0] / 10.0, points[i][1] / 10.0)
            prev_line = sketch.SketchLines.AddByTwoPoints(
                prev_line.EndSketchPoint, next_pt
            )

        # Close the loop — connect last endpoint to first startpoint
        sketch.SketchLines.AddByTwoPoints(
            prev_line.EndSketchPoint, first_line.StartSketchPoint
        )

        return f"Closed profile with {len(points)} points ({len(points)} lines)"

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
        profile = sketch.Profiles.AddForSolid()

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
        profile = sketch.Profiles.AddForSolid()

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

        if depth <= 0:
            feature = hf.AddDrilledByThroughAllExtent(
                placement, hole_arg, kNegativeExtentDirection
            )
            extent_str = "through-all"
        else:
            depth_cm = depth / 10.0
            feature = hf.AddDrilledByDistanceExtent(
                placement, hole_arg, depth_cm, kNegativeExtentDirection
            )
            extent_str = f"{depth}mm deep"

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
        except Exception:
            info["body"] = "none"
        feats = []
        for i in range(1, comp.Features.Count + 1):
            f = comp.Features.Item(i)
            nm = f.Name
            if getattr(f, "Suppressed", False):
                nm += " [suppressed]"
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
        type_names = {5890: "Plane", 5891: "Cyl", 5892: "Cone", 5893: "Cone",
                      5894: "Sphere", 5895: "Torus"}
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
        comp = doc.ComponentDefinition
        info = {
            "name": doc.DisplayName,
            "full_path": doc.FullFileName,
            "type": str(doc.DocumentType),
            "sketches_count": comp.Sketches.Count,
            "features_count": comp.Features.Count if hasattr(comp.Features, "Count") else 0,
            "bodies_count": comp.SurfaceBodies.Count,
        }
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
        profile = sketch.Profiles.AddForSolid()
        ff = comp.Features.FaceFeatures
        before = self._vol_state()
        fdef = ff.CreateFaceFeatureDefinition(profile)
        feat = ff.Add(fdef)
        return f"Sheet metal face: {feat.Name}{self._fmt_delta(before)}"

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
        cut_across_bends: bool = False,
    ) -> str:
        """Cut through the sheet using a sketch profile. Sheet Metal Cut.
        Default: cut perpendicular to the face (through thickness).
        cut_across_bends: if True, cut wraps around bends (unfolds first).
        """
        comp = self.active_part()
        sketch = self._get_sketch(sketch_index)
        profile = sketch.Profiles.AddForSolid()

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
        feat = cf.Add(cf.CreateCutDefinition(sketch.Profiles.AddForSolid()))
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

        surface_type: 5890=Plane (default), 5891=Cylinder, 5892=Cone, 5893=Sphere, 5894=Torus
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
        occ = comp.Occurrences.Add(path, matrix)
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

    def check_interference(self, set_a: list[str], set_b: list[str] | None = None) -> list[dict]:
        """Interference between two sets of occurrences (names; 'Asm:1/2' for a
        sub-part). set_b empty = everything else. Returns overlap volume and
        bounding box per pair — touching faces are not interference."""
        comp = self.active_assembly()
        a = [self._occ(n) for n in set_a]
        a_names = {o.Name for o in a}
        b = [self._occ(n) for n in set_b] if set_b else [o for o in comp.Occurrences if o.Name not in a_names]
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
            out.append({"a": r.OccurrenceOne.Name, "b": r.OccurrenceTwo.Name,
                        "volume_mm3": round(r.Volume * 1000, 3),
                        "bbox_mm": [round(v * 10, 2) for v in (rb.MinPoint.X, rb.MaxPoint.X, rb.MinPoint.Y,
                                                                rb.MaxPoint.Y, rb.MinPoint.Z, rb.MaxPoint.Z)]})
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

    def list_occurrences(self) -> str:
        """Compact table of the assembly's components:
        '[i] block:1  pos=(0.0,0.0,0.0) grounded'
        """
        comp = self.active_assembly()
        lines = []
        for i in range(1, comp.Occurrences.Count + 1):
            occ = comp.Occurrences.Item(i)
            t = occ.Transformation.Translation
            flags = []
            if occ.Grounded:
                flags.append("grounded")
            if occ.Suppressed:
                flags.append("suppressed")
            lines.append(
                f"[{i}] {occ.Name}  pos=({t.X * 10:.1f},{t.Y * 10:.1f},{t.Z * 10:.1f})"
                + ("  " + " ".join(flags) if flags else "")
            )
        return "\n".join(lines) if lines else "(no components placed)"

    def ground_occurrence(self, index: int, grounded: bool = True) -> str:
        """Ground (fix in space) or unground a component. Ground the first
        component of every assembly — otherwise constraints move it instead."""
        comp = self.active_assembly()
        occ = comp.Occurrences.Item(index)
        occ.Grounded = grounded
        return f"{'Grounded' if grounded else 'Ungrounded'}: {occ.Name}"

    def delete_occurrence(self, index: int) -> str:
        comp = self.active_assembly()
        occ = comp.Occurrences.Item(index)
        name = occ.Name
        occ.Delete()
        return f"Deleted occurrence: {name} ({comp.Occurrences.Count} left)"

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
            lines.append(f"[{i}] {c.Name}" + (" (suppressed)" if c.Suppressed else ""))
        return "\n".join(lines) if lines else "(no constraints)"

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
            mp = comp.MassProperties
            info["mass_kg"] = round(mp.Mass, 4)
            info["volume_mm3"] = round(mp.Volume * 1000, 1)
        except Exception:
            pass
        return info
