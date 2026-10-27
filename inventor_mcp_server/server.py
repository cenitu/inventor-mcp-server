"""MCP Server for Autodesk Inventor parametric modeling."""

import concurrent.futures
import functools
import json
import os
import time

import pythoncom
try:
    # MCP Python SDK 2.x — FastMCP was renamed MCPServer
    from mcp.server import MCPServer as FastMCP
except ImportError:  # SDK 1.x
    from mcp.server.fastmcp import FastMCP
try:
    from . import inventor_api as _api
except ImportError:
    # Run as a script (`python inventor_mcp_server/server.py`), which is how most MCP clients
    # launch it — there is no parent package, so import from this directory.
    import os, sys
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import inventor_api as _api

mcp = FastMCP(
    "Autodesk Inventor",
    instructions=(
        "MCP server for parametric 3D modeling in Autodesk Inventor. "
        "All dimensions are in millimeters. "
        "Typical workflow: connect → create_part → create_sketch → draw geometry → extrude/revolve → save. "
        "Assemblies: save the parts first, then create_assembly → place_component (ground the first one) "
        "→ constrain by origin geometry / constrain_insert for bolt-in-hole. "
        "For anything not covered by a dedicated tool, use execute_python "
        "(persistent namespace, live inv/app/comp/body/tg objects). "
        "After editing inventor_api.py on disk, call reload_api — no server restart needed."
    ),
)

inv = _api.InventorConnection()

# Persistent namespace for execute_python (variables survive between calls)
_exec_ns: dict = {}
_exec_ns_created = time.time()
_exec_output = None  # stdout buffer of the execute_python call in progress


# ---------- Inventor call thread ----------
#
# Every Inventor call runs on this one thread (COM objects stay on the thread that made
# them). The tool waits CALL_TIMEOUT seconds at most, so a call stuck behind a modal
# dialog, or a long job, no longer freezes the whole server: the client gets an answer
# before its own timeout, and call_status picks up the result later.

CALL_TIMEOUT = float(os.environ.get("INVENTOR_MCP_CALL_TIMEOUT", "50"))
_com_thread = concurrent.futures.ThreadPoolExecutor(
    max_workers=1, thread_name_prefix="inventor-com", initializer=pythoncom.CoInitialize)


class _Call:
    def __init__(self, name, future):
        self.name, self.future = name, future
        self.started, self.finished = time.monotonic(), None
        self.timed_out = False      # outlived CALL_TIMEOUT: the client never saw the result
        self.collected = False      # call_status has returned the result
        future.add_done_callback(lambda _: setattr(self, "finished", time.monotonic()))


_last_call: _Call | None = None


def _dialog_report() -> str:
    try:
        dialogs = _api.modal_dialogs(inv.main_hwnd)
    except Exception:
        return ""
    if not dialogs:
        return ""
    lines = ["Inventor is showing a modal dialog. It blocks every API call until someone "
             "answers it in Inventor — ask the user to read it and click the right button:"]
    lines += [f'  [{d["title"]}] {d["text"]}' for d in dialogs]
    return "\n".join(lines)


def _busy_report(call: _Call) -> str:
    msg = f"'{call.name}' has been running for {time.monotonic() - call.started:.0f} s."
    return msg + "\n" + (_dialog_report() or
                         "No dialog is open — Inventor is still working on it (a long operation).")


def _silent_call(fn, args, kwargs):
    """Runs on the Inventor thread, with SilentOperation on: Inventor answers its own
    prompts (save dependents?, use the resolved file?) with the default instead of
    opening a dialog that waits for a click."""
    app, prev = inv._app, None
    if app is not None:
        try:
            prev = app.SilentOperation
            app.SilentOperation = True
        except Exception:
            app = None
    try:
        return fn(*args, **kwargs)
    finally:
        if app is not None:
            try:
                app.SilentOperation = prev
            except Exception:
                pass


def _on_com_thread(fn, args, kwargs):
    global _last_call
    if _last_call is not None and not _last_call.future.done():
        return (_busy_report(_last_call) + f"\nThis '{fn.__name__}' call was NOT started. "
                "Use call_status to follow the running call.")
    blocker = _dialog_report()
    if blocker:
        return blocker + f"\nThis '{fn.__name__}' call was NOT started. Retry once the dialog is closed."
    call = _Call(fn.__name__, _com_thread.submit(_silent_call, fn, args, kwargs))
    _last_call = call
    try:
        return call.future.result(timeout=CALL_TIMEOUT)
    except concurrent.futures.TimeoutError:
        call.timed_out = True
        return (_busy_report(call) + "\nIt keeps running in the background — "
                "call call_status to follow it and get its result.")


def tool(com: bool = True):
    """mcp.tool() that runs the function on the Inventor thread (see above).
    com=False for tools that must answer while that thread is busy."""
    def deco(fn):
        if not com:
            return mcp.tool()(fn)

        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            return _on_com_thread(fn, args, kwargs)
        return mcp.tool()(wrapper)
    return deco


@tool(com=False)
def call_status() -> str:
    """Follow a call that outlived the wait (a long job, or one stuck behind an Inventor
    dialog): elapsed time, any open dialog's text, execute_python output so far — and
    the call's result once it has finished. Safe to call at any time."""
    c = _last_call
    if c is None:
        return "No Inventor call has been made yet."
    if not c.future.done():
        out = _exec_output.getvalue().strip() if c.name == "execute_python" and _exec_output else ""
        return _busy_report(c) + (f"\nOutput so far:\n{out[-3000:]}" if out else "")
    took = f"{c.finished - c.started:.0f} s"
    if not c.timed_out or c.collected:
        return f"Last call '{c.name}' finished ({took}); its result was already returned."
    c.collected = True
    try:
        result = c.future.result()
    except Exception as e:
        result = f"ERROR: {type(e).__name__}: {e}"
    return f"'{c.name}' finished after {took}:\n{result}"


# ---------- Power tools (token savers) ----------

@tool()
def execute_python(code: str) -> str:
    """Execute Python code against the live Inventor connection.

    Pre-bound objects (re-resolved fresh on every call):
      inv  — InventorConnection wrapper (all high-level methods, mm units)
      app  — Inventor.Application COM object
      doc  — active document          comp — its ComponentDefinition
      body — comp.SurfaceBodies.Item(1) (or None)
      tg   — TransientGeometry        math — math module
    Variables you assign persist between calls (one shared namespace) until the server
    restarts or reload_api runs — status shows the namespace's age and names.
    Internal COM units are CENTIMETERS — divide mm by 10.
    Objects are late-bound: a Document from Documents.Open exposes ComponentDefinition
    directly. Don't use CastTo / gencache.EnsureDispatch (they create a gen_py cache).
    Runs with SilentOperation on, so Inventor answers its own prompts instead of opening
    a dialog. A call longer than the wait limit keeps running; follow it with call_status.
    Returns captured stdout (print your results!) or the traceback on error.
    """
    import io, contextlib, traceback, math as _math
    global _exec_output
    ctx = {"inv": inv, "math": _math}
    try:
        if not inv.is_connected():
            inv.connect()
        ctx["app"] = inv.app
        ctx["tg"] = inv.app.TransientGeometry
        ctx["doc"] = inv.app.ActiveDocument
        if ctx["doc"] is not None:
            ctx["comp"] = ctx["doc"].ComponentDefinition
            try:
                ctx["body"] = ctx["comp"].SurfaceBodies.Item(1)
            except Exception:
                ctx["body"] = None
    except Exception:
        pass
    _exec_ns.update(ctx)
    buf = _exec_output = io.StringIO()
    try:
        with contextlib.redirect_stdout(buf):
            # silent again in case this call connected first (the thread found no app)
            inv._silently(lambda: exec(compile(code, "<mcp>", "exec"), _exec_ns))
        out = buf.getvalue().strip()
        if len(out) > 6000:
            out = out[:6000] + "\n... (truncated at 6000 chars)"
        return out if out else "(ok, no output)"
    except Exception:
        partial = buf.getvalue().strip()
        tb = traceback.format_exc(limit=3)
        return (partial + "\n" if partial else "") + f"ERROR:\n{tb}"


@tool()
def reload_api() -> str:
    """Hot-reload inventor_api.py from disk. Use after editing the wrapper —
    no Claude Code restart needed. Keeps the existing COM connection."""
    import importlib
    global inv, _exec_ns_created
    importlib.reload(_api)
    old = inv
    inv = _api.InventorConnection()
    inv._app, inv.main_hwnd = old._app, old.main_hwnd
    _exec_ns.clear()
    _exec_ns_created = time.time()
    return "inventor_api.py reloaded (connection preserved)"


@tool()
def inspect() -> str:
    """Compact snapshot of the active part: volume, bbox, faces/edges count,
    feature tree with names, sketches, sheet metal thickness. One call instead
    of several diagnostic queries."""
    return json.dumps(inv.inspect(), indent=1, ensure_ascii=False)


@tool()
def list_edges(min_length: float = 0, max_count: int = 60) -> str:
    """Compact edge table of body 1: '[i] L=200.0 mid=(0.0,-225.0,0.8)' per line.
    Use to locate edges for flange/fillet/chamfer. min_length filters small edges (mm)."""
    return inv.list_edges(min_length=min_length, max_count=max_count)


@tool()
def list_faces(min_area: float = 0, max_count: int = 60) -> str:
    """Compact face table of body 1: '[i] Plane A=90000 c=(0.0,225.0,12.5)' per line.
    Use to locate faces for sketching/cuts. min_area filters small faces (mm²)."""
    return inv.list_faces(min_area=min_area, max_count=max_count)


@tool()
def transaction(action: str = "begin", name: str = "MCP batch") -> str:
    """Wrap multiple operations in one undo unit with rollback.

    Args:
        action: "begin" before a risky multi-step build, then "commit" on success
                or "abort" to roll back EVERYTHING since begin in one step.
        name: label shown in Inventor's undo list.
    """
    return inv.transaction(action, name)


# ---------- Connection ----------

@tool()
def connect(visible: bool = True) -> str:
    """Connect to Autodesk Inventor. Starts Inventor if not running.

    Args:
        visible: Show Inventor window (default True).
    """
    return inv.connect(visible)


@tool()
def status() -> str:
    """Check connection status, active document info, and the execute_python
    namespace (age, names you defined — gone after a server restart or reload_api).
    If Inventor is busy or showing a dialog, says so instead."""
    if not inv.is_connected():
        return "Not connected to Inventor. Call connect() first."
    try:
        info = inv.get_document_info()
    except RuntimeError:
        info = {"document": None}
    defined = sorted(k for k in _exec_ns
                     if not k.startswith("__") and k not in ("inv", "app", "doc", "comp", "body", "tg", "math"))
    info["python_namespace"] = {
        "created": time.strftime("%Y-%m-%d %H:%M", time.localtime(_exec_ns_created)),
        "names": defined,
    }
    return json.dumps(info, indent=2, ensure_ascii=False)


# ---------- Document management ----------

@tool()
def create_part(name: str = "", template: str = "metric") -> str:
    """Create a new empty part document.

    Args:
        name: Display name for the part (optional).
        template: Template type — "metric" (mm, default), "english" (inches), or full path to .ipt file.
    """
    return inv.create_part(name or None, template)


@tool()
def save_document(path: str = "") -> str:
    """Save the active document — never opens a dialog. Saving in place also saves
    dependents that changed (e.g. sub-assemblies), which would otherwise trigger
    Inventor's modal 'save dependents?' prompt and block until a user clicks OK.

    Args:
        path: Full file path to save as (e.g. "D:/parts/flange.ipt"). If empty, saves in place.
    """
    return inv.save_document(path or None)


@tool()
def list_documents() -> str:
    """List open documents (name, path, type, unsaved changes) and which one is active.
    Tools act on the ACTIVE document — check here when juggling several parts."""
    return json.dumps(inv.list_documents(), indent=1, ensure_ascii=False)


@tool()
def activate_document(name_or_path: str) -> str:
    """Make an open document the active one (by display name or full path)."""
    return inv.activate_document(name_or_path)


@tool()
def export_document(path: str, format: str = "STEP") -> str:
    """Export active document to another format.

    Args:
        path: Output file path (e.g. "D:/export/part.step").
        format: Export format — STEP, STL, SAT, or IGES.
    """
    return inv.export_document(path, format)


# ---------- Sketches ----------

@tool()
def create_sketch(plane: str = "XY", face_index: int = 0, offset: float = 0) -> str:
    """Create a new sketch on a work plane, a body face, or an offset work plane.

    Args:
        plane: Work plane — XY, XZ, or YZ. Ignored if face_index > 0.
        face_index: Index of body face to sketch on (1-based). 0 = use work plane. Use list_faces() to locate.
        offset: mm — sketch on a hidden work plane parallel to 'plane' at this distance
                (positive = along plane normal: XY→+Z, XZ→+Y, YZ→+X). For ribs/bosses at a height.
    """
    fi = face_index if face_index > 0 else None
    return inv.create_sketch(plane, fi, offset)


@tool()
def draw_rectangle(
    x: float = 0, y: float = 0,
    width: float = 100, height: float = 50,
    sketch_index: int = 0,
) -> str:
    """Draw a centered rectangle in a sketch. Dimensions in mm.

    Args:
        x: Center X coordinate in mm.
        y: Center Y coordinate in mm.
        width: Width in mm.
        height: Height in mm.
        sketch_index: Sketch number (0 = latest sketch).
    """
    idx = sketch_index if sketch_index > 0 else None
    return inv.draw_rectangle(x, y, width, height, idx)


@tool()
def draw_circle(
    x: float = 0, y: float = 0,
    diameter: float = 50,
    sketch_index: int = 0,
    construction: bool = False,
) -> str:
    """Draw a circle in a sketch with an automatic diameter dimension. Dimensions in mm.

    Args:
        x: Center X coordinate in mm.
        y: Center Y coordinate in mm.
        diameter: Diameter in mm. A diameter constraint is added automatically.
        sketch_index: Sketch number (0 = latest sketch).
        construction: If True, draws a reference (construction) circle — not part of profile.
                      Useful for showing bolt PCD (pitch circle diameter) without cutting.
    """
    idx = sketch_index if sketch_index > 0 else None
    return inv.draw_circle(x, y, diameter, idx, construction)


@tool()
def draw_line(
    x1: float = 0, y1: float = 0,
    x2: float = 100, y2: float = 0,
    sketch_index: int = 0,
    construction: bool = False,
) -> str:
    """Draw a line in a sketch. Coordinates in mm.

    Args:
        x1: Start X in mm.
        y1: Start Y in mm.
        x2: End X in mm.
        y2: End Y in mm.
        sketch_index: Sketch number (0 = latest sketch).
        construction: If True, line becomes a construction line (used as revolve axis, not part of profile).
    """
    idx = sketch_index if sketch_index > 0 else None
    return inv.draw_line(x1, y1, x2, y2, idx, construction)


@tool()
def draw_polygon(
    x: float = 0, y: float = 0,
    radius: float = 25,
    sides: int = 6,
    sketch_index: int = 0,
) -> str:
    """Draw a regular polygon in a sketch. Dimensions in mm.

    Args:
        x: Center X in mm.
        y: Center Y in mm.
        radius: Circumscribed radius in mm.
        sides: Number of sides (e.g. 6 for hexagon).
        sketch_index: Sketch number (0 = latest sketch).
    """
    idx = sketch_index if sketch_index > 0 else None
    return inv.draw_polygon(x, y, radius, sides, idx)


@tool()
def draw_closed_profile(
    points: str,
    sketch_index: int = 0,
) -> str:
    """Draw a closed profile (polygon) from connected points. All coordinates in mm.
    Lines share endpoints so Inventor recognizes the closed loop for extrude/revolve.

    Args:
        points: JSON array of [x,y] pairs in mm. Example: "[[0,0],[50,0],[25,40]]" for a triangle.
        sketch_index: Sketch number (0 = latest sketch).
    """
    import json as _json
    pts = _json.loads(points)
    idx = sketch_index if sketch_index > 0 else None
    return inv.draw_closed_profile(pts, idx)


# ---------- Features ----------

@tool()
def extrude(
    distance: float = 10,
    direction: str = "positive",
    operation: str = "join",
    sketch_index: int = 0,
    extent_type: str = "distance",
) -> str:
    """Extrude a sketch profile to create 3D geometry. Distance in mm.

    Args:
        distance: Extrusion distance in mm. Ignored when extent_type="all".
        direction: Direction — positive, negative, or symmetric.
        operation: Boolean operation — join, cut, intersect, surface, or new.
        sketch_index: Sketch number to extrude (0 = latest sketch).
        extent_type: Extent type — "distance" (fixed distance, default) or "all" (through entire body).
    """
    idx = sketch_index if sketch_index > 0 else None
    return inv.extrude(distance, direction, operation, idx, extent_type=extent_type)


@tool()
def revolve(
    angle: float = 360,
    axis: str = "Y",
    sketch_index: int = 0,
    operation: str = "join",
) -> str:
    """Revolve a sketch profile around a work axis to create or cut 3D geometry.

    PREFERRED workflow — no construction line needed in sketch:
      1. create_sketch("XY")
      2. draw_closed_profile([[0,0],[30,0],[30,8],[15,60],[0,60]])  ← profile only
      3. revolve(axis="Y", operation="join")

    For a revolve CUT (e.g. groove):
      1. create_sketch("XY")
      2. draw_closed_profile([[8,26],[15,26],[15,34],[8,34]])  ← groove cross-section
      3. revolve(axis="Y", operation="cut")

    Profile must be on one side of the axis (all X ≥ 0 when axis="Y").

    Args:
        angle: Revolution angle in degrees (360 = full revolution, default).
        axis: Rotation axis — "X", "Y", or "Z". Default "Y" for lathe parts on XY plane.
        sketch_index: Sketch number (0 = latest sketch).
        operation: Boolean operation — join (add material), cut (remove), intersect, or new.
    """
    idx = sketch_index if sketch_index > 0 else None
    return inv.revolve(angle, idx, axis, operation)


@tool()
def fillet(radius: float = 2, edge_indices: str = "1") -> str:
    """Add fillet (rounded edge) to edges of the solid body. Radius in mm.

    Args:
        radius: Fillet radius in mm.
        edge_indices: Comma-separated edge indices (e.g. "1,2,3"). Use status() to see edge count.
    """
    indices = [int(i.strip()) for i in edge_indices.split(",")]
    return inv.fillet(radius, indices)


@tool()
def chamfer(distance: float = 2, edge_indices: str = "1") -> str:
    """Add chamfer (angled edge) to edges of the solid body. Distance in mm.

    Args:
        distance: Chamfer distance in mm.
        edge_indices: Comma-separated edge indices (e.g. "1,2,3").
    """
    indices = [int(i.strip()) for i in edge_indices.split(",")]
    return inv.chamfer(distance, indices)


@tool()
def hole(
    x: float = 0, y: float = 0,
    diameter: float = 10,
    depth: float = 0,
    tapped: bool = False,
    pitch: float = 0,
) -> str:
    """Create a native Inventor HoleFeature. Dimensions in mm.
    Creates a proper Hole feature (not extrude cut) — works with circular_pattern.
    Supports both plain drilled holes and tapped (threaded) metric holes.

    Args:
        x: Hole center X in mm.
        y: Hole center Y in mm.
        diameter: Hole diameter in mm. For tapped holes this is the nominal thread diameter (e.g. 16 for M16).
        depth: Hole depth in mm. 0 = through-all (default).
        tapped: True = create tapped (threaded) hole with ISO Metric profile (6H). False = plain drilled hole.
        pitch: Thread pitch in mm (e.g. 2.0 for M16x2). 0 = auto-select standard coarse pitch.
    """
    p = pitch if pitch > 0 else None
    return inv.hole(x, y, diameter, depth, tapped, p)


@tool()
def hole_linear(
    face_axis: str, face_value: float,
    ref1_axis: str, ref1_value: float, dist1: float,
    ref2_axis: str, ref2_value: float, dist2: float,
    diameter: float = 5.5,
    depth: float = 0,
    tapped: bool = False,
    pitch: float = 0,
    cbore_diameter: float = 0,
    cbore_depth: float = 0,
) -> str:
    """Hole placed parametrically from two edges of a face (Linear placement).
    Survives part resizing — hole keeps its distance from the edges.
    Drill direction resolved automatically (volume check).

    Args:
        face_axis/face_value: planar face to drill into, e.g. ("z", 25) = top face at Z=25mm.
        ref1_axis/ref1_value/dist1: first edge of that face + distance from it, e.g. ("x", -34.5, 8).
        ref2_axis/ref2_value/dist2: second edge + distance.
        diameter: hole diameter mm (or thread nominal if tapped, e.g. 5 for M5).
        depth: 0 = through-all, otherwise depth in mm.
        tapped: True = ISO metric thread 6H (standard coarse pitch unless pitch given).
        pitch: thread pitch mm (0 = auto standard coarse).
        cbore_diameter/cbore_depth: >0 adds counterbore seat (M5 ISO 4762: 10 and 5.5).
    """
    p = pitch if pitch > 0 else None
    return inv.hole_linear(face_axis, face_value, ref1_axis, ref1_value, dist1,
                           ref2_axis, ref2_value, dist2, diameter, depth,
                           tapped, p, cbore_diameter, cbore_depth)


@tool()
def circular_pattern(
    feature_index: int,
    count: int = 8,
    angle: float = 360,
    axis: str = "Z",
) -> str:
    """Create a circular (rotational) pattern of a feature around a work axis.
    Ideal for bolt holes around a flange — one hole + circular_pattern instead of 8 holes.

    Args:
        feature_index: Index of the feature to pattern (from list_features()).
        count: Number of instances (total including the original).
        angle: Total span angle in degrees (360 = full circle).
        axis: Rotation axis — X, Y, or Z (default Z for flanges).
    """
    return inv.circular_pattern(feature_index, count, angle, axis)


# ---------- Parameters ----------

@tool()
def get_parameters() -> str:
    """Get all parameters of the active document. Returns JSON list."""
    params = inv.get_parameters()
    return json.dumps(params, indent=2, ensure_ascii=False)


@tool()
def set_parameter(name: str, expression: str) -> str:
    """Set a parameter value by name.

    Args:
        name: Parameter name (e.g. "d0", "width").
        expression: New value or expression (e.g. "50 mm", "d0 * 2").
    """
    return inv.set_parameter(name, expression)


@tool()
def add_parameter(name: str, expression: str, units: str = "mm") -> str:
    """Add a new user parameter.

    Args:
        name: Parameter name (e.g. "flange_diameter").
        expression: Value or expression (e.g. "100", "width * 2").
        units: Units — mm, cm, m, deg, etc.
    """
    return inv.add_parameter(name, expression, units)


# ---------- Sheet Metal ----------

@tool()
def set_sheet_metal_thickness(thickness: float = 0.8) -> str:
    """Override the active sheet metal style's thickness. Use after create_part(template="sheet_metal").
    If the shop has named rules (see list_sheet_metal_rules), use set_sheet_metal_rule instead —
    changing only the thickness leaves the rule's bend radius and unfold method unmatched."""
    return inv.set_sheet_metal_thickness(thickness)


@tool()
def list_sheet_metal_rules() -> str:
    """List the sheet metal rules available in the active sheet metal part
    (name, thickness, bend radius, which one is active). Shops often encode
    their tooling in rule names, e.g. 'T15-V10' = 1.5 mm sheet, V10 die."""
    return json.dumps(inv.list_sheet_metal_rules(), indent=1, ensure_ascii=False)


@tool()
def set_sheet_metal_rule(name: str) -> str:
    """Activate a sheet metal rule by name (case-insensitive). Sets thickness,
    bend radius and unfold method together — prefer this over
    set_sheet_metal_thickness whenever a matching rule exists.
    Unknown names return the list of available rules."""
    return inv.set_sheet_metal_rule(name)


@tool()
def sheet_metal_face(sketch_index: int = 0) -> str:
    """Create a base Sheet Metal Face from a closed sketch profile.
    The first flat panel in a sheet metal part. Uses style's Thickness automatically."""
    idx = sketch_index if sketch_index > 0 else None
    return inv.sheet_metal_face(idx)


@tool()
def flange(edge_indices: str, distance: float = 25, angle: float = 90,
           auto_miter: bool = True, miter_gap: str = "GapSize",
           height_datum: str = "outer") -> str:
    """Add a Sheet Metal Flange (bent wall) to one or more edges.

    Real sheet-metal feature (vs Extrude) — supports Flat Pattern unfolding,
    has proper bend radius, depth ties to Thickness.
    Put ALL edges of a loop (e.g. the 4 edges of a base panel) in ONE call so the
    corners are auto-mitered; separate calls leave open corners.
    The flange bends toward the side of the face the edge belongs to: for an
    inward flange on a wall, pick the wall's inner-face edge.

    Args:
        edge_indices: Comma-separated edge indices (use find_edge to locate).
        distance: Flange height in mm (default 25 = Inventor default).
        angle: Bend angle in degrees (default 90 = perpendicular).
        auto_miter: Miter the corners between the edges (default True).
        miter_gap: Corner gap expression; "GapSize" = from the sheet metal rule (default).
        height_datum: "outer" (from outer intersection, default), "inner", "tangent",
            "outer_ortho" or "inner_ortho".
    """
    idx_list = [int(i.strip()) for i in edge_indices.split(",")]
    return inv.flange(idx_list, distance, angle, auto_miter=auto_miter,
                      miter_gap=miter_gap, height_datum=height_datum)


@tool()
def cut_on_face(face_index: int, circles: str = "", rectangles: str = "") -> str:
    """Cut holes through the sheet, placed in WORLD coordinates (mm) on a face.
    Avoids the face-sketch axis confusion — no need to know the sketch's origin.
    Verifies material was removed (a cut on a work plane silently removes nothing).

    Args:
        face_index: Face of the sheet to sketch on (use find_face / list_faces).
        circles: JSON list of [x, y, z, diameter], e.g. "[[180, 267, 0, 4.9]]".
        rectangles: JSON list of 4-corner lists, e.g. "[[[-150,-300,40],[150,-300,40],[150,-300,160],[-150,-300,160]]]".
    """
    c = json.loads(circles) if circles.strip() else []
    r = json.loads(rectangles) if rectangles.strip() else []
    return inv.cut_on_face(face_index, c, r)


@tool()
def corner_round(edge_indices: str, radius: float = 5) -> str:
    """Round sharp sheet metal corners (Corner Round — carried into the flat pattern).
    Use on exposed corners people handle, e.g. mounting plate tabs.

    Args:
        edge_indices: Comma-separated indices of the short edges through the thickness at the corners.
        radius: Corner radius in mm.
    """
    idx_list = [int(i.strip()) for i in edge_indices.split(",")]
    return inv.corner_round(idx_list, radius)


@tool()
def sheet_metal_cut(sketch_index: int = 0) -> str:
    """Cut through the panel — depth = Thickness automatically (parametric!).
    Sketch must be on the panel face (not a work plane): a cut that removes no
    material is deleted and reported as an error. For holes at known world
    coordinates, cut_on_face is easier.
    One sketch with multiple closed shapes = one Cut feature with multiple holes.
    """
    idx = sketch_index if sketch_index > 0 else None
    return inv.sheet_metal_cut(idx)


@tool()
def find_edge(x: float = 0, y: float = 0, z: float = 0,
              use_x: bool = False, use_y: bool = False, use_z: bool = False,
              tolerance: float = 0.1) -> str:
    """Find an edge by its midpoint coordinates (mm). Returns edge index (string).
    Useful for flange — locate the edge to bend without printing all edges.

    Args:
        x/y/z: Target midpoint coordinates (mm).
        use_x/y/z: Set True for each coord that should be matched (False = ignored).
        tolerance: How close midpoint must be (mm). Default 0.1.
    """
    kwargs = {}
    if use_x: kwargs['x'] = x
    if use_y: kwargs['y'] = y
    if use_z: kwargs['z'] = z
    idx = inv.find_edge(tolerance=tolerance, **kwargs)
    return f"Edge {idx}"


@tool()
def find_face(x: float = 0, y: float = 0, z: float = 0,
              use_x: bool = False, use_y: bool = False, use_z: bool = False,
              min_area: float = 100, tolerance: float = 0.5) -> str:
    """Find a planar face by its centroid coordinates (mm). Returns face index (string).
    Useful for sheet metal Cut — locate the outer panel face to sketch on.

    Args:
        x/y/z: Target centroid coordinates (mm).
        use_x/y/z: Set True for each coord that should be matched.
        min_area: Minimum area in mm² (filters tiny edge faces). Default 100.
        tolerance: Centroid match tolerance (mm). Default 0.5.
    """
    kwargs = {}
    if use_x: kwargs['x'] = x
    if use_y: kwargs['y'] = y
    if use_z: kwargs['z'] = z
    idx = inv.find_face(min_area=min_area, tolerance=tolerance, **kwargs)
    return f"Face {idx}"


# ---------- Query ----------

@tool()
def list_features() -> str:
    """List all features in the active part. Returns JSON list."""
    features = inv.list_features()
    return json.dumps(features, indent=2, ensure_ascii=False)


@tool()
def delete_feature(indices: str) -> str:
    """Delete one or more features by index. Use list_features() to find indices.

    Args:
        indices: Comma-separated feature indices to delete (e.g. "3,4,5"). Deletes in reverse order automatically.
    """
    idx_list = [int(i.strip()) for i in indices.split(",")]
    return inv.delete_feature(idx_list)


@tool()
def suppress_feature(indices: str, suppressed: bool = True) -> str:
    """Suppress or unsuppress features (hide without deleting). Use list_features() to find indices.

    Args:
        indices: Comma-separated feature indices (e.g. "3,4,5").
        suppressed: True to suppress (hide), False to unsuppress (show).
    """
    idx_list = [int(i.strip()) for i in indices.split(",")]
    return inv.suppress_feature(idx_list, suppressed)


# ---------- Assembly (IAM) ----------

@tool()
def create_assembly(name: str = "", template: str = "metric") -> str:
    """Create a new empty assembly document (.iam).

    Args:
        name: Display name for the assembly (optional).
        template: "metric" (mm, default), "english" (inches), or full path to an .iam file.
    """
    return inv.create_assembly(name or None, template)


@tool()
def place_component(
    path: str,
    x: float = 0, y: float = 0, z: float = 0,
    grounded: bool = False,
    x_axis: str = "", y_axis: str = "",
) -> str:
    """Place a part or sub-assembly into the active assembly. The file must exist
    on disk — save_document() the part first.

    Args:
        path: Full path to the .ipt / .iam file.
        x: X position in mm (where the component's origin goes).
        y: Y position in mm.
        z: Z position in mm.
        grounded: Fix the component in space. Ground the FIRST component of every
                  assembly, otherwise constraints move it instead of the others.
        x_axis: Optional orientation — where the component's X axis points, e.g. "0,0,1".
        y_axis: Where its Y axis points, e.g. "0,1,0" (must be perpendicular; Z = X × Y).
    """
    xa = tuple(float(v) for v in x_axis.split(",")) if x_axis.strip() else None
    ya = tuple(float(v) for v in y_axis.split(",")) if y_axis.strip() else None
    return inv.place_component(path, x, y, z, grounded, x_axis=xa, y_axis=ya)


@tool()
def import_step(step_path: str, dest_folder: str, name: str = "") -> str:
    """Import a vendor STEP file (hinge, lock, gland...) as Inventor files in dest_folder.
    The STEP is copied into dest_folder first, because Inventor writes the parts of
    an imported assembly next to the .step file (e.g. into Downloads or a synced folder).
    Returns path, type, overall bbox and per-component bbox/volume — use them to
    find mounting faces and axes before placing it with place_component.

    Args:
        step_path: The downloaded .step / .stp file.
        dest_folder: Project folder to hold the Inventor files.
        name: Base name for the .iam/.ipt (default: the STEP file name).
    """
    return json.dumps(inv.import_step(step_path, dest_folder, name or None), indent=1, ensure_ascii=False)


@tool()
def check_interference(set_a: str, set_b: str = "") -> str:
    """Check overlap between occurrences of the active assembly. Touching faces
    are not reported. Intended overlaps (gasket compression) show up too — read
    the volume and bbox.

    Args:
        set_a: Comma-separated occurrence names, e.g. "PANO_KAPAK:1". Use "Asm:1/2"
            for one part inside a sub-assembly (e.g. a vendor hinge's body).
        set_b: Names to test against; empty = everything else.
    """
    a = [s.strip() for s in set_a.split(",") if s.strip()]
    b = [s.strip() for s in set_b.split(",") if s.strip()] or None
    hits = inv.check_interference(a, b)
    return json.dumps(hits, indent=1, ensure_ascii=False) if hits else "No interference"


@tool()
def measure_distance(a: str, b: str) -> str:
    """Minimum distance in mm between two occurrences (0 = touching/overlapping).
    Names as in list_occurrences; "Asm:1/2" reaches a part inside a sub-assembly."""
    return f"{inv.measure_distance(a, b)} mm"


@tool()
def sweep_rotation(moving: str, axis_point: str, axis_dir: str,
                   start: float = 0, stop: float = 110, step: float = 10,
                   against: str = "", measure_to: str = "") -> str:
    """Rotate occurrences about an axis in steps and report collisions at each
    angle — e.g. check a door opens 0–110° on its hinge axis without hitting the
    body, or a lock tongue turns freely. Positions are restored afterwards.

    Args:
        moving: Comma-separated occurrences that rotate together (door + lock + door studs).
        axis_point: A point on the axis, "x,y,z" in mm (e.g. the hinge pin).
        axis_dir: Axis direction, e.g. "0,1,0".
        start / stop / step: Angles in degrees.
        against: Occurrences to test; empty = everything else.
        measure_to: Optional occurrence to report the minimum distance to at each angle.
    """
    mv = [s.strip() for s in moving.split(",") if s.strip()]
    ag = [s.strip() for s in against.split(",") if s.strip()] or None
    pt = tuple(float(v) for v in axis_point.split(","))
    ax = tuple(float(v) for v in axis_dir.split(","))
    rows = inv.sweep_rotation(mv, pt, ax, start, stop, step, ag, measure_to or None)
    return json.dumps(rows, indent=1, ensure_ascii=False)


@tool()
def list_occurrences() -> str:
    """Compact table of the assembly's components:
    '[1] block:1  pos=(0.0,0.0,0.0) grounded'. Indices feed the constrain tools."""
    return inv.list_occurrences()


@tool()
def assembly_info() -> str:
    """Snapshot of the active assembly: components with positions, constraint
    count, mass and volume. One call instead of several queries."""
    return json.dumps(inv.assembly_info(), indent=1, ensure_ascii=False)


@tool()
def constrain(
    occurrence1: int, geometry1: str,
    occurrence2: int, geometry2: str,
    kind: str = "mate",
    offset: float = 0,
    angle: float = 0,
) -> str:
    """Constrain two components by their ORIGIN geometry — the parametric way to
    assemble. Origin constraints survive part edits; face picks break when
    topology renumbers.

    Args:
        occurrence1: Index of the first component (see list_occurrences).
        geometry1: "XY", "XZ", "YZ", "X", "Y", "Z" or "ORIGIN".
        occurrence2: Index of the second component.
        geometry2: Same options as geometry1.
        kind: "mate" (planes face each other / axes collinear), "flush" (planes
              point the same way) or "angle".
        offset: Offset in mm for mate/flush.
        angle: Angle in degrees for kind="angle".
    """
    return inv.constrain(occurrence1, geometry1, occurrence2, geometry2, kind, offset, angle)


@tool()
def constrain_insert(
    occurrence1: int, edge1: int,
    occurrence2: int, edge2: int,
    offset: float = 0,
    axes_opposed: bool = True,
) -> str:
    """Insert constraint between two circular edges — a bolt into a hole in one
    call (aligns the axes AND seats the faces).

    Args:
        occurrence1: Index of the first component.
        edge1: Circular edge index on that component (see list_component_edges).
        occurrence2: Index of the second component.
        edge2: Circular edge index on that component.
        offset: Distance between the two circle planes in mm.
        axes_opposed: True = the components face each other (normal bolt/hole case).
    """
    return inv.constrain_insert(occurrence1, edge1, occurrence2, edge2, offset, axes_opposed)


@tool()
def list_component_edges(occurrence: int, circular_only: bool = True, max_count: int = 40) -> str:
    """Edge table of one placed component in its own part coordinates:
    '[1] circle D=10.0 c=(0.0,0.0,10.0)'. Use to pick edges for constrain_insert.

    Args:
        occurrence: Component index (see list_occurrences).
        circular_only: Only list circular edges (the ones Insert can use).
        max_count: Maximum rows to return.
    """
    return inv.list_component_edges(occurrence, circular_only, max_count)


@tool()
def list_constraints() -> str:
    """List the assembly constraints with their indices."""
    return inv.list_constraints()


@tool()
def delete_constraint(index: int) -> str:
    """Delete an assembly constraint by index (see list_constraints)."""
    return inv.delete_constraint(index)


@tool()
def ground_occurrence(index: int, grounded: bool = True) -> str:
    """Ground (fix in space) or unground a component.

    Args:
        index: Component index (see list_occurrences).
        grounded: True to ground, False to release.
    """
    return inv.ground_occurrence(index, grounded)


@tool()
def delete_occurrence(index: int) -> str:
    """Remove a component from the assembly (see list_occurrences for the index)."""
    return inv.delete_occurrence(index)


def main():
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
