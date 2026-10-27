"""Live smoke test against a real Inventor installation.

Exercises every geometry-creating code path once and reports PASS/FAIL per step,
so a new Inventor release can be qualified in a couple of minutes:

    python tests/smoke_test.py            # starts Inventor if not running
    python tests/smoke_test.py --keep     # leave the test documents open
    python tests/smoke_test.py --own      # start a separate Inventor, quit it at the end —
                                          # leaves an Inventor someone is working in alone

Nothing is saved to disk; the scratch documents it creates are closed without
saving unless --keep is given. Documents that were already open (your own work,
and anything they reference) are never touched. Exit code is the number of
failed steps.
"""
import sys
import os
import traceback

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from inventor_mcp_server import inventor_api as api  # noqa: E402


class Runner:
    def __init__(self):
        self.passed = 0
        self.failed = []

    def step(self, label, fn):
        try:
            result = fn()
        except Exception as e:
            self.failed.append((label, f"{type(e).__name__}: {e}"))
            print(f"  FAIL  {label}\n        {type(e).__name__}: {e}")
            if os.environ.get("SMOKE_TRACE"):
                traceback.print_exc()
            return None
        self.passed += 1
        print(f"  ok    {label}" + (f"  -> {result}" if isinstance(result, str) else ""))
        return result


def check_enums(inv, run):
    """Verify the hard-coded enum constants against the live type library."""
    import pythoncom
    import winreg

    expected = {
        "DocumentTypeEnum": {"kPartDocumentObject": api.kPartDocumentObject},
        "DimensionOrientationEnum": {
            "kHorizontalDim": api.kHorizontalDim,
            "kVerticalDim": api.kVerticalDim,
            "kAlignedDim": api.kAlignedDim,
        },
        "PartFeatureOperationEnum": {
            "kJoinOperation": api.kJoinOperation,
            "kCutOperation": api.kCutOperation,
            "kIntersectOperation": api.kIntersectOperation,
            "kSurfaceOperation": api.kSurfaceOperation,
            "kNewBodyOperation": api.kNewBodyOperation,
        },
        "PartFeatureExtentDirectionEnum": {
            "kPositiveExtentDirection": api.kPositiveExtentDirection,
            "kNegativeExtentDirection": api.kNegativeExtentDirection,
            "kSymmetricExtentDirection": api.kSymmetricExtentDirection,
        },
    }

    # Locate the Inventor type library without touching win32com's gencache
    # (EnsureDispatch would write %TEMP%\gen_py and break GetActiveObject).
    tlb = None
    root = winreg.OpenKey(winreg.HKEY_CLASSES_ROOT, "TypeLib")
    i = 0
    while tlb is None:
        try:
            guid = winreg.EnumKey(root, i)
        except OSError:
            break
        i += 1
        try:
            k = winreg.OpenKey(root, guid)
            ver = winreg.EnumKey(k, 0)
            name = winreg.QueryValue(winreg.OpenKey(k, ver), None)
        except OSError:
            continue
        if name and "Inventor Object Library" in name:
            major, _, minor = ver.partition(".")
            tlb = pythoncom.LoadRegTypeLib(guid, int(major, 16), int(minor or "0", 16), 0)

    if tlb is None:
        run.failed.append(("enums", "Inventor type library not found in registry"))
        print("  FAIL  enum verification: type library not found")
        return

    actual = {}
    for idx in range(tlb.GetTypeInfoCount()):
        if tlb.GetTypeInfoType(idx) != pythoncom.TKIND_ENUM:
            continue
        ename = tlb.GetDocumentation(idx)[0]
        if ename not in expected:
            continue
        ti = tlb.GetTypeInfo(idx)
        members = {}
        for v in range(ti.GetTypeAttr().cVars):
            vd = ti.GetVarDesc(v)
            members[ti.GetNames(vd[0])[0]] = vd[1]
        actual[ename] = members

    for ename, members in expected.items():
        for mname, mvalue in members.items():
            live = actual.get(ename, {}).get(mname)
            run.step(
                f"enum {ename}.{mname} == {mvalue}",
                lambda live=live, mvalue=mvalue, mname=mname: (
                    f"{mvalue}"
                    if live == mvalue
                    else _raise(f"type library says {mname} = {live}, code has {mvalue}")
                ),
            )


def _raise(msg):
    raise AssertionError(msg)


def _expect_error(fn, fragment):
    try:
        fn()
    except Exception as e:
        if fragment in str(e):
            return f"raised: {str(e)[:80]}"
        raise AssertionError(f"wrong error: {e}")
    raise AssertionError("expected an error, got none")


def part_tests(inv, run):
    run.step("create_part(metric)", lambda: inv.create_part("smoke_part"))
    run.step("create_sketch(XY)", lambda: inv.create_sketch("XY"))
    run.step("draw_rectangle", lambda: inv.draw_rectangle(0, 0, 80, 60))
    run.step("extrude 10mm", lambda: inv.extrude(10, "positive", "join"))
    run.step("hole drilled through", lambda: inv.hole(25, 15, diameter=8))
    run.step("hole tapped M10", lambda: inv.hole(-25, 15, diameter=10, tapped=True))
    run.step("fillet r3", lambda: inv.fillet(3, None))
    run.step("list_edges", lambda: inv.list_edges()[:120])
    run.step("list_faces", lambda: inv.list_faces()[:120])
    run.step("find_face(top z=10)", lambda: inv.find_face(z=10))
    run.step("inspect", lambda: str(inv.inspect())[:120])
    run.step("list_features", lambda: str(inv.list_features())[:120])
    run.step("get_parameters", lambda: f"{len(inv.get_parameters())} parameters")
    run.step("add_parameter", lambda: inv.add_parameter("smoke_len", "42 mm"))
    run.step("set_parameter", lambda: inv.set_parameter("smoke_len", "43 mm"))
    run.step("transaction begin", lambda: inv.transaction("begin", "smoke"))
    run.step("transaction abort", lambda: inv.transaction("abort"))

    # chamfer + circular pattern on a fresh, simpler body
    run.step("create_part(metric) #2", lambda: inv.create_part("smoke_part2"))
    run.step("create_sketch(XY) #2", lambda: inv.create_sketch("XY"))
    run.step("draw_circle D40", lambda: inv.draw_circle(0, 0, 40))
    run.step("extrude 20mm", lambda: inv.extrude(20, "positive", "join"))
    run.step("chamfer 2mm", lambda: inv.chamfer(2, None))

    run.step("create_part(metric) #2b", lambda: inv.create_part("smoke_pattern"))
    run.step("create_sketch(XY) #2b", lambda: inv.create_sketch("XY"))
    run.step("draw_polygon hex", lambda: inv.draw_polygon(0, 0, 30, 6))
    run.step("extrude 12mm", lambda: inv.extrude(12, "positive", "join"))
    run.step("hole for pattern", lambda: inv.hole(10, 0, diameter=4))
    run.step("circular_pattern x6", lambda: inv.circular_pattern(2, 6, 360))
    run.step("suppress feature", lambda: inv.suppress_feature([3], True))
    run.step("unsuppress feature", lambda: inv.suppress_feature([3], False))
    run.step("delete feature", lambda: inv.delete_feature([3]))

    run.step("create_part(metric) #3", lambda: inv.create_part("smoke_revolve"))
    run.step("create_sketch(XY) #3", lambda: inv.create_sketch("XY"))
    run.step("draw_rectangle offset", lambda: inv.draw_rectangle(30, 10, 20, 20))
    run.step("revolve 360 about Y", lambda: inv.revolve(360, None, "Y", "join"))


def sheet_metal_tests(inv, run):
    run.step("create_part(sheet_metal)", lambda: inv.create_part("smoke_sm", "sheet_metal"))
    rules = run.step("list_sheet_metal_rules", lambda: inv.list_sheet_metal_rules())
    if rules:
        # Use whatever rule the template ships (stock: Default_mm; shops: their own),
        # in a different case to exercise the case-insensitive match.
        first = rules[0]["name"]
        run.step(f"set_sheet_metal_rule({first.upper()!r})",
                 lambda: inv.set_sheet_metal_rule(first.upper()))
        run.step("active rule switched",
                 lambda: first if inv.active_part().ActiveSheetMetalStyle.Name == first
                 else _raise("rule did not become active"))
    run.step("set_sheet_metal_rule(unknown) raises",
             lambda: _expect_error(lambda: inv.set_sheet_metal_rule("NO_SUCH_RULE"), "not found"))
    run.step("set_sheet_metal_thickness", lambda: inv.set_sheet_metal_thickness(2))
    run.step("create_sketch(XY) sm", lambda: inv.create_sketch("XY"))
    run.step("draw_rectangle sm", lambda: inv.draw_rectangle(0, 0, 100, 60))
    run.step("sheet_metal_face", lambda: inv.sheet_metal_face())
    idx = run.step("find_edge(x=50,y=0,z=0)", lambda: inv.find_edge(x=50, y=0, z=0))
    if isinstance(idx, int):
        run.step("flange 20mm", lambda: inv.flange([idx], 20, 90))
        run.step("create_sketch on top face", lambda: inv.create_sketch(face_index=inv.find_face(z=2)))
        run.step("draw_circle for cut", lambda: inv.draw_circle(0, 0, 20))
        run.step("sheet_metal_cut", lambda: inv.sheet_metal_cut())
    else:
        run.failed.append(("flange", "no edge found to flange"))
        print("  FAIL  flange: find_edge returned no usable index")


_TEMP_DIRS = []   # removed together with the scratch documents (unless --keep)


def document_tests(inv, run):
    """Active-document tracking and dialog-free saving (writes to a temp folder)."""
    import tempfile
    tmp = tempfile.mkdtemp(prefix="inventor_mcp_smoke_")
    _TEMP_DIRS.append(tmp)
    part_path = os.path.join(tmp, "doc_a.ipt")
    run.step("create_part doc_a", lambda: inv.create_part("doc_a"))
    run.step("create_part doc_b", lambda: inv.create_part("doc_b"))
    run.step("newest part is the active document",
             lambda: inv.active_document().DisplayName if inv.active_document().DisplayName == "doc_b"
             else _raise(f"active is {inv.active_document().DisplayName}"))
    run.step("activate_document(doc_a)", lambda: inv.activate_document("doc_a"))
    run.step("list_documents marks doc_a active",
             lambda: "ok" if [d for d in inv.list_documents() if d["active"]][0]["name"] == "doc_a"
             else _raise("wrong active document"))
    run.step("activate_document(unknown) raises",
             lambda: _expect_error(lambda: inv.activate_document("no_such_doc"), "No open document"))
    run.step("sketch + extrude in doc_a", lambda: (inv.create_sketch("XY"), inv.draw_rectangle(0, 0, 20, 20),
                                                   inv.extrude(5))[2])
    run.step("save_document(path) doc_a", lambda: inv.save_document(part_path))
    run.step("create_assembly", lambda: inv.create_assembly("doc_asm"))
    run.step("place doc_a", lambda: inv.place_component(part_path, 0, 0, 0, grounded=True))
    run.step("save assembly", lambda: inv.save_document(os.path.join(tmp, "doc_asm.iam")))

    def make_dependent_dirty():
        asm = inv.active_document()
        part = [d for d in asm.ReferencedDocuments if d.FullFileName.lower() == part_path.lower()][0]
        part.ComponentDefinition.Parameters.UserParameters.AddByExpression("smoke_dirty", "7 mm", "mm")
        part.Update(); asm.Update()
        return "dependent dirty" if part.Dirty else _raise("part did not become dirty")
    run.step("modify the referenced part (dirty dependent)", make_dependent_dirty)
    # Before the fix this opened Inventor's modal 'save dependents?' dialog and blocked.
    run.step("save_document saves dependents without a dialog", lambda: inv.save_document())
    run.step("dependent is saved",
             lambda: "ok" if not any(d.Dirty for d in inv.active_document().AllReferencedDocuments)
             else _raise("a dependent is still unsaved"))


def assembly_tool_tests(inv, run):
    """import_step (nothing written beside the original), oriented placement,
    interference / distance, and a rotation sweep that must hit an obstacle."""
    import tempfile
    tmp = tempfile.mkdtemp(prefix="inventor_mcp_smoke_")
    _TEMP_DIRS.append(tmp)
    src, imp = os.path.join(tmp, "download"), os.path.join(tmp, "project")
    os.makedirs(src)
    blk, bar = os.path.join(tmp, "blk.ipt"), os.path.join(tmp, "bar.ipt")

    run.step("cube 20 (z 0..20)", lambda: (inv.create_part("blk"), inv.create_sketch("XY"),
                                           inv.draw_rectangle(0, 0, 20, 20), inv.extrude(20),
                                           inv.save_document(blk))[-1])
    run.step("export cube as STEP", lambda: inv.export_document(os.path.join(src, "blk.step"), "STEP"))
    for fmt, ext in (("IGES", "igs"), ("SAT", "sat"), ("STL", "stl")):
        run.step(f"export cube as {fmt}", lambda fmt=fmt, ext=ext: inv.export_document(os.path.join(tmp, f"blk.{ext}"), fmt))

    def bar_part():
        inv.create_part("bar"); inv.create_sketch("XY")
        inv.draw_closed_profile([[0, -5], [40, -5], [40, 5], [0, 5]])
        inv.extrude(10)
        return inv.save_document(bar)
    run.step("bar 40x10x10 along +X from origin", bar_part)
    run.step("assembly of 2 cubes → STEP", lambda: (inv.create_assembly("stepasm"),
                                                    inv.place_component(blk, 0, 0, 0, grounded=True),
                                                    inv.place_component(blk, 30, 0, 0, grounded=True),
                                                    inv.save_document(os.path.join(tmp, "stepasm.iam")),
                                                    inv.export_document(os.path.join(src, "asm.step"), "STEP"))[-1])
    before = set(os.listdir(src))
    run.step("import_step part", lambda: inv.import_step(os.path.join(src, "blk.step"), imp)["type"])
    res = run.step("import_step assembly", lambda: inv.import_step(os.path.join(src, "asm.step"), imp))
    run.step("imported assembly has 2 components",
             lambda: "ok" if res and len(res.get("components", [])) == 2 else _raise(str(res)))
    run.step("nothing written beside the original STEP files",
             lambda: "ok" if set(os.listdir(src)) == before
             else _raise(f"new files in download folder: {set(os.listdir(src)) - before}"))

    run.step("create_assembly clearance", lambda: inv.create_assembly("clr"))
    run.step("place cube A (grounded)", lambda: inv.place_component(blk, 0, 0, 0, grounded=True))
    run.step("place cube B overlapping by 10", lambda: inv.place_component(blk, 10, 0, 0, grounded=True))
    run.step("place cube C 20 mm away", lambda: inv.place_component(blk, 40, 0, 0, grounded=True))

    def overlap_4000():
        hits = inv.check_interference(["blk:1"], ["blk:2"])
        v = hits[0]["volume_mm3"] if hits else 0
        return f"{v} mm3" if abs(v - 4000) < 0.5 else _raise(f"overlap {v} mm3, expected 4000")
    run.step("check_interference A↔B = 10x20x20", overlap_4000)
    run.step("check_interference A↔C none",
             lambda: "ok" if not inv.check_interference(["blk:1"], ["blk:3"]) else _raise("unexpected overlap"))
    run.step("measure_distance A↔C = 20",
             lambda: f"{inv.measure_distance('blk:1', 'blk:3')} mm" if abs(inv.measure_distance("blk:1", "blk:3") - 20) < 1e-3
             else _raise(f"distance {inv.measure_distance('blk:1', 'blk:3')}"))

    def rotated_bar():
        inv.place_component(bar, 0, 0, 30, grounded=True, x_axis=(0, 1, 0), y_axis=(-1, 0, 0))
        occ = inv._occ("bar:1"); r = occ.RangeBox
        dy, dx = (r.MaxPoint.Y - r.MinPoint.Y) * 10, (r.MaxPoint.X - r.MinPoint.X) * 10
        if not (abs(dy - 40) < 1e-3 and abs(dx - 10) < 1e-3):
            _raise(f"bar spans X {dx:.1f} Y {dy:.1f}, expected 10 / 40")
        occ.Transformation = inv._tg().CreateMatrix()          # back to +X, at the origin
        m = occ.Transformation; m.SetTranslation(inv._tg().CreateVector(0, 0, 3.0)); occ.Transformation = m
        return "oriented along +Y"
    run.step("place_component with x_axis/y_axis", rotated_bar)
    run.step("obstacle cube at y=30, z=30..50", lambda: inv.place_component(blk, 0, 30, 30, grounded=True))

    def sweep():
        occ = inv._occ("bar:1"); r0 = occ.RangeBox; x0 = r0.MaxPoint.X
        rows = inv.sweep_rotation(["bar:1"], (0, 0, 30), (0, 0, 1), 0, 90, 30, measure_to="blk:4")
        hit = {r["angle"]: r["collisions"] for r in rows}
        if hit.get(0) or hit.get(30) or not hit.get(90):
            _raise(f"collisions by angle {hit}, expected none at 0/30 and a hit at 90")
        if abs(occ.RangeBox.MaxPoint.X - x0) > 1e-6:
            _raise("bar not restored to its start position")
        return f"collisions {hit}, restored"
    run.step("sweep_rotation hits the obstacle at 90° and restores", sweep)


def sheet_metal_box_tests(inv, run):
    """A small enclosure body: 4 flanges in ONE call (auto-miter), an inward
    front flange, holes at world coordinates, a work-plane cut that must fail,
    and corner rounds on a flat plate."""
    T = 1.5
    run.step("create_part(sheet_metal) box", lambda: inv.create_part("smoke_box", "sheet_metal"))
    run.step("set_sheet_metal_thickness 1.5", lambda: inv.set_sheet_metal_thickness(T))
    run.step("create_sketch(XY) box", lambda: inv.create_sketch("XY"))
    run.step("draw_rectangle 100x60", lambda: inv.draw_rectangle(0, 0, 100, 60))
    run.step("sheet_metal_face box", lambda: inv.sheet_metal_face())
    walls = [inv.find_edge(x=50, y=0, z=T), inv.find_edge(x=-50, y=0, z=T),
             inv.find_edge(x=0, y=30, z=T), inv.find_edge(x=0, y=-30, z=T)]
    run.step("flange 4 edges in one call (auto-miter)", lambda: inv.flange(walls, 40))

    def miter_gap_is_rule_gap():
        comp = inv.active_part()
        fl = [f for f in comp.Features if f.Name.startswith("Flange")][-1]
        mg = fl.Definition.MiterGap
        # Check the expression, not just the value: in the stock Default_mm rule
        # GapSize equals the thickness, which is also the API's (wrong) default.
        if "gapsize" not in mg.Expression.replace(" ", "").lower():
            _raise(f"miter gap expression is {mg.Expression!r}, expected 'GapSize'")
        return f"miter gap = {mg.Expression} = {mg.Value*10:.2f} mm"
    run.step("miter gap taken from the rule (not thickness)", miter_gap_is_rule_gap)

    def walls_reach_40():
        rb = inv.active_part().SurfaceBodies.Item(1).RangeBox
        if abs(rb.MaxPoint.Z * 10 - 40) > 0.05 or abs(rb.MaxPoint.X * 10 - 50) > 0.05:
            _raise(f"bbox X {rb.MaxPoint.X*10:.2f} Z {rb.MaxPoint.Z*10:.2f}, expected 50 / 40")
        return "outer 100 x 60 x 40"
    run.step("box outer size from outer intersection", walls_reach_40)

    inner = [inv.find_edge(x=50 - T, y=0, z=40), inv.find_edge(x=-(50 - T), y=0, z=40),
             inv.find_edge(x=0, y=30 - T, z=40), inv.find_edge(x=0, y=-(30 - T), z=40)]
    run.step("inward front flange 8mm (inner-face edges)", lambda: inv.flange(inner, 8))

    def front_flange_is_inward():
        rb = inv.active_part().SurfaceBodies.Item(1).RangeBox
        if rb.MaxPoint.X * 10 > 50.05:
            _raise(f"flange went outward: X max {rb.MaxPoint.X*10:.2f}")
        return "outer size unchanged"
    run.step("front flange bent inward", front_flange_is_inward)

    side = inv.find_face(x=-50, min_area=500, tolerance=0.1)
    run.step("cut_on_face: 2 holes at world coordinates",
             lambda: inv.cut_on_face(side, circles=[[-50, -10, 20, 5.5], [-50, 10, 20, 5.5]]))
    run.step("create_sketch on work plane (XY offset 20)", lambda: inv.create_sketch("XY", offset=20))
    run.step("draw_circle on work plane", lambda: inv.draw_circle(0, 0, 10))
    n_feat = inv.active_part().Features.Count
    run.step("sheet_metal_cut on work plane raises",
             lambda: _expect_error(lambda: inv.sheet_metal_cut(), "removed no material"))
    run.step("failed cut left no feature behind",
             lambda: "ok" if inv.active_part().Features.Count == n_feat
             else _raise("an empty Cut feature was left in the tree"))

    run.step("create_part(sheet_metal) plate", lambda: inv.create_part("smoke_plate", "sheet_metal"))
    run.step("set_sheet_metal_thickness 2", lambda: inv.set_sheet_metal_thickness(2))
    run.step("create_sketch(XY) plate", lambda: inv.create_sketch("XY"))
    run.step("draw_rectangle 80x50", lambda: inv.draw_rectangle(0, 0, 80, 50))
    run.step("sheet_metal_face plate", lambda: inv.sheet_metal_face())

    def corners():
        b = inv.active_part().SurfaceBodies.Item(1)
        out = []
        for i in range(1, b.Edges.Count + 1):
            e = b.Edges.Item(i)
            p, q = e.StartVertex.Point, e.StopVertex.Point
            if abs(abs(p.Z - q.Z) * 10 - 2) < 1e-3 and abs(p.X - q.X) < 1e-6 and abs(p.Y - q.Y) < 1e-6:
                out.append(i)
        return out
    run.step("corner_round R5 on 4 corners", lambda: inv.corner_round(corners(), 5))
    run.step("flat pattern still unfolds", lambda: (inv.active_part().Unfold(),
                                                    inv.active_part().FlatPattern.ExitEdit(), "ok")[2])


def _doc_ids(inv):
    """InternalName of every open document — includes the invisible ones
    referenced by open assemblies, so those are protected too."""
    ids = set()
    for doc in list(inv.app.Documents):
        try:
            ids.add(doc.InternalName)
        except Exception:
            pass
    return ids


def main():
    keep = "--keep" in sys.argv
    own = "--own" in sys.argv
    inv = api.InventorConnection()
    run = Runner()

    print("Inventor MCP smoke test")
    print("-" * 60)
    try:
        print(" ", inv.connect(new_instance=own))
    except Exception as e:
        print(f"Cannot connect to Inventor: {e}")
        return 1
    print("  version:", inv.app.SoftwareVersion.DisplayVersion)
    try:
        return run_all(inv, run, keep)
    finally:
        if own and not keep:
            inv.app.Quit()
            inv._app = None
            import time
            time.sleep(10)  # leaving at once can leave a windowless Inventor process behind


def run_all(inv, run, keep):
    preexisting = _doc_ids(inv)
    if preexisting:
        print(f"  {len(preexisting)} document(s) already open — will be left untouched")
    print()

    print("Enum constants vs. live type library:")
    check_enums(inv, run)
    print("\nPart modeling:")
    part_tests(inv, run)
    print("\nSheet metal:")
    sheet_metal_tests(inv, run)
    print("\nSheet metal box (auto-miter, cut_on_face, corner_round):")
    sheet_metal_box_tests(inv, run)
    print("\nDocuments (active document, dialog-free save):")
    document_tests(inv, run)
    print("\nAssembly tools (import_step, placement, interference, sweep):")
    assembly_tool_tests(inv, run)

    if not keep:
        print("\nClosing scratch documents...")
        closed = 0
        for doc in list(inv.app.Documents):
            try:
                if doc.InternalName in preexisting:
                    continue  # the user's own document, or one it references
                doc.Close(True)  # SkipSave
                closed += 1
            except Exception:
                pass
        print(f"  closed {closed} scratch document(s)")
        import shutil
        for d in _TEMP_DIRS:
            shutil.rmtree(d, ignore_errors=True)
        if _TEMP_DIRS:
            print(f"  removed {len(_TEMP_DIRS)} temp folder(s)")

    print("\n" + "-" * 60)
    print(f"{run.passed} passed, {len(run.failed)} failed")
    for label, err in run.failed:
        print(f"  FAILED  {label}: {err}")
    return len(run.failed)


if __name__ == "__main__":
    sys.exit(main())
