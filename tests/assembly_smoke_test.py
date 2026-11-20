"""Live smoke test for the assembly (IAM) tools.

Builds two scratch parts on disk, places them in a new assembly, constrains them
by origin geometry and by an Insert constraint, then reports PASS/FAIL per step.

    python tests/assembly_smoke_test.py
    python tests/assembly_smoke_test.py --keep    # leave the documents open

Scratch files go to %TEMP%\\inventor_mcp_smoke and are not cleaned up, because
an assembly keeps references to the parts it places.
"""
import os
import sys
import tempfile

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(_HERE))
sys.path.insert(0, _HERE)

from inventor_mcp_server import inventor_api as api  # noqa: E402
from smoke_test import Runner  # noqa: E402


def build_parts(inv, run, workdir):
    """A 40x40x10 plate with a Ø10 through hole, and a Ø10x30 pin."""
    plate = os.path.join(workdir, "smoke_plate.ipt")
    pin = os.path.join(workdir, "smoke_pin.ipt")

    run.step("part: plate", lambda: inv.create_part("smoke_plate"))
    run.step("plate sketch", lambda: inv.create_sketch("XY"))
    run.step("plate rectangle", lambda: inv.draw_rectangle(0, 0, 40, 40))
    run.step("plate extrude", lambda: inv.extrude(10))
    run.step("plate hole", lambda: inv.hole(0, 0, diameter=10))
    run.step("plate save", lambda: inv.save_document(plate, overwrite=True))

    run.step("part: pin", lambda: inv.create_part("smoke_pin"))
    run.step("pin sketch", lambda: inv.create_sketch("XY"))
    run.step("pin circle", lambda: inv.draw_circle(0, 0, 10))
    run.step("pin extrude", lambda: inv.extrude(30))
    run.step("pin save", lambda: inv.save_document(pin, overwrite=True))

    return plate, pin


def assembly_tests(inv, run, plate, pin):
    run.step("create_assembly", lambda: inv.create_assembly("smoke_asm"))
    run.step("place plate (grounded)",
             lambda: inv.place_component(plate, 0, 0, 0, grounded=True))
    run.step("place pin", lambda: inv.place_component(pin, 60, 0, 0))
    run.step("list_occurrences", lambda: inv.list_occurrences())
    run.step("assembly_info", lambda: str(inv.assembly_info())[:160])

    def assembly_status():
        info = inv.get_document_info()
        if info["type_name"] != "assembly" or info["components_count"] != 2 or "sketches_count" in info:
            raise AssertionError(str(info))
        return f"{info['components_count']} components, {info['constraints_count']} constraints"
    run.step("status of an assembly", assembly_status)

    run.step("mate XY planes",
             lambda: inv.constrain(1, "XY", 2, "XY", "mate", offset=0))
    run.step("list_constraints", lambda: inv.list_constraints())
    run.step("delete_constraint", lambda: inv.delete_constraint(1))

    run.step("flush XZ planes",
             lambda: inv.constrain(1, "XZ", 2, "XZ", "flush", offset=5))
    run.step("angle Z axes",
             lambda: inv.constrain(1, "Z", 2, "Z", "angle", angle=0))
    def edit_offset():
        out = inv.edit_constraint(1, 7.5)
        if "→ 7.5 mm" not in out:
            _raise(out)
        return out
    run.step("edit_constraint offset", edit_offset)

    def edit_angle():
        out = inv.edit_constraint(2, 45)
        if "→ 45 deg" not in out:
            _raise(out)
        inv.edit_constraint(2, 90)
        return out
    run.step("edit_constraint angle", edit_angle)
    run.step("list_constraints shows the offset",
             lambda: inv.list_constraints() if "7" in inv.list_constraints() else _raise("no offset listed"))

    run.step("delete both constraints",
             lambda: (inv.delete_constraint(2), inv.delete_constraint(1))[1])

    edges = run.step("list_component_edges(plate)",
                     lambda: inv.list_component_edges(1))
    pin_edges = run.step("list_component_edges(pin)",
                         lambda: inv.list_component_edges(2))
    e1 = _first(edges)
    e2 = _first(pin_edges)
    if e1 and e2:
        run.step("insert constraint (pin into hole)",
                 lambda: inv.constrain_insert(1, e1, 2, e2, offset=0))
        run.step("pin moved into place", lambda: _assert_mated(inv))
    else:
        run.failed.append(("insert", "no circular edges found"))
        print("  FAIL  insert: no circular edges found")

    def assembly_cut():
        out = inv.assembly_cut("z", circles=[[15, 15, 0, 4]], participants=["smoke_plate:1"])
        if "smoke_plate:1 -126 mm³" not in out:
            _raise(out)
        return out
    run.step("assembly_cut through the plate only", assembly_cut)

    def cut_that_misses():
        comp = inv.active_assembly()
        n = comp.Sketches.Count, comp.Features.Count
        try:
            inv.assembly_cut("z", circles=[[5000, 0, 0, 4]])
        except RuntimeError as e:
            if "nothing was left" not in str(e):
                raise
        else:
            _raise("a cut that misses did not raise")
        if (comp.Sketches.Count, comp.Features.Count) != n:
            _raise("a failed cut left a sketch or feature behind")
        return "ok"
    run.step("assembly_cut that misses leaves nothing", cut_that_misses)

    def point_round_trip():
        top = inv.to_top(2, [0, 0, 30])
        back = inv.to_local(2, top)
        origin = inv.assembly_info()["components"][1]["position_mm"]
        if max(abs(a - b) for a, b in zip(back, [0, 0, 30])) > 1e-3:
            _raise(f"to_local(to_top(p)) = {back}")
        if max(abs(a - b) for a, b in zip(inv.to_top(2, [0, 0, 0]), origin)) > 1e-2:
            _raise(f"to_top of the origin is not the component's position {origin}")
        return f"pin tip at {top}"
    run.step("to_top / to_local", point_round_trip)

    def custom_view():
        paths = inv.view_images(["custom", "top"], 320, 240, look_from=[200, 150, 300],
                                target=[0, 0, 5], zoom=2, perspective=True)
        sizes = [os.path.getsize(p) for p in paths]
        for p in paths:
            os.remove(p)
        os.rmdir(os.path.dirname(paths[0]))
        if len(sizes) != 2 or min(sizes) < 500:
            _raise(f"pictures: {sizes}")
        try:
            inv.view_images(["custom"])
        except ValueError:
            return f"{sizes} bytes"
        _raise("custom without look_from did not raise")
    run.step("view from a point", custom_view)

    def replace():
        import shutil
        pin2 = os.path.join(os.path.dirname(pin), "smoke_pin_b.ipt")
        old = os.path.join(os.path.dirname(pin), "_old")
        shutil.rmtree(old, ignore_errors=True)
        shutil.copyfile(pin, pin2)
        n = inv.active_assembly().Constraints.Count
        out = inv.replace_occurrence(2, pin2, archive_old=True)
        now = inv.active_assembly().Occurrences.Item(2).Definition.Document.FullFileName
        if os.path.normcase(now) != os.path.normcase(pin2):
            _raise(f"occurrence 2 is still {now}")
        if inv.active_assembly().Constraints.Count != n or "gone" in out or "lost" in out:
            _raise(out)
        if not os.path.isfile(os.path.join(old, "smoke_pin.ipt")) or os.path.exists(pin):
            _raise("the old file was not archived: " + out)
        shutil.move(os.path.join(old, "smoke_pin.ipt"), pin)  # the next run builds it again
        return out
    run.step("replace_occurrence keeps the constraint, archives the old file", replace)

    def hole_alignment():
        # a second plate on top of the first: their Ø10 holes on each other, then 2 mm off
        inv.place_component(plate, 0, 0, 10)
        try:
            inv.save_document(os.path.join(os.path.dirname(pin), "smoke_asm.iam"), overwrite=True)
            out = inv.fastener_alignment(parts=["smoke_plate*"], refresh=True)
            if out["aligned"] < 1 or out["misaligned_count"] or "Ø10 on Ø10 (M10)" not in out["aligned_by_size"]:
                _raise(str(out))
            again = inv.fastener_alignment(parts=["smoke_plate*"])
            if again["read"]["from_cache"] < 1:
                _raise("nothing came from the cache: " + str(again["read"]))
            occ = inv._occ("smoke_plate:2")
            tg = inv.app.TransientGeometry
            move = tg.CreateMatrix()
            move.SetTranslation(tg.CreateVector(0.2, 0, 1.0))   # cm: 2 mm in X, still 10 mm up
            occ.Transformation = move
            off = inv.fastener_alignment(parts=["smoke_plate*"])
            rows = [r for r in off["misaligned"] if r["a_opening"] == "Ø10" and r["b_opening"] == "Ø10"]
            if not rows or abs(rows[0]["offset_mm"] - 2.0) > 0.01 or rows[0]["passes"] != "M8":
                _raise(str(off))
            if any(d.Dirty and d.DisplayName.startswith("smoke_plate") for d in inv.app.Documents):
                _raise("reading the geometry marked the part as changed")
            return f"aligned {out['aligned_by_size']}; 2 mm off: {rows[0]['passes']} still passes; {again['read']}"
        finally:
            inv.delete_occurrence("smoke_plate:2", force=True)
    run.step("fastener_alignment: aligned, cached, then 2 mm off", hole_alignment)

    def seeded_review():
        """An assembly with one of each mistake: each must come back exactly once, and nothing may change."""
        main = inv.app.ActiveDocument           # the smoke assembly: the steps after this one need it back
        folder = os.path.join(os.path.dirname(pin), "review")
        os.makedirs(folder, exist_ok=True)
        paths = {n: os.path.join(folder, n + ".ipt") for n in ("rv_plate", "rv_block", "rv_short")}
        inv.create_part("rv_plate")
        inv.create_sketch("XY")
        inv.draw_rectangle(50, 30, 100, 60)
        inv.extrude(5)
        inv.hole(20, 20, diameter=6)
        inv.save_document(paths["rv_plate"], overwrite=True)
        inv.create_part("rv_block")
        inv.create_sketch("XY")
        inv.draw_rectangle(0, 0, 10, 10)
        inv.extrude(5)
        inv.save_document(paths["rv_block"], overwrite=True)
        inv.create_part("rv_short", "sheet_metal")
        inv.set_sheet_metal_thickness(2)
        inv.sheet_profile("L", 60, 7, 100)
        inv.flat_pattern()                      # unfolded beforehand: the review itself unfolds nothing
        inv.save_document(paths["rv_short"], overwrite=True)
        inv.create_assembly("rv_asm")
        inv.place_component(paths["rv_plate"], 0, 0, 0, True)
        inv.place_component(paths["rv_plate"], 2, 0, 5)        # on top, its hole 2 mm off
        inv.place_component(paths["rv_plate"], 2, 0, 10.1)     # 0.1 mm above that one, holes in line
        inv.place_component(paths["rv_block"], 102, 30, 0)     # sticks 3 mm into the first plate's end
        inv.place_component(paths["rv_short"], 400, 0, 0)
        asm = os.path.join(folder, "rv_asm.iam")
        inv.save_document(asm, overwrite=True, save_dependents=True)
        before = sorted(d.DisplayName for d in inv.app.Documents if d.Dirty)
        steps = inv.app.TransactionManager.CommittedTransactions.Count
        out = inv.review_assembly()
        lines = lambda key, check: [r["text"] for r in out[key] if r["check"] == check]
        wrong = []
        if len(out["checked"]) != 6:
            wrong.append(f"checks that ran: {out['checked']}")
        if not (len(lines("problems", "holes")) == 1 and "2 mm off" in lines("problems", "holes")[0]):
            wrong.append(f"holes: {lines('problems', 'holes')}")
        if not (len(lines("problems", "clearance")) == 1 and "0.1 mm apart" in lines("problems", "clearance")[0]):
            wrong.append(f"clearance: {lines('problems', 'clearance')}")
        if not (len(lines("problems", "interference")) == 1 and "rv_block" in lines("problems", "interference")[0]):
            wrong.append(f"interference: {lines('problems', 'interference')}")
        generic = [t for t in lines("problems", "documents") if "Generic" in t]
        if not (len(generic) == 1 and "rv_block" in generic[0]):
            wrong.append(f"documents: {lines('problems', 'documents')}")
        short = lines("problems", "sheet_metal")
        if not (len(short) == 1 and short[0].startswith("rv_short") and "of sheet to the edge" in short[0]):
            wrong.append(f"sheet metal: {short}")
        after = sorted(d.DisplayName for d in inv.app.Documents if d.Dirty)
        if after != before or inv.app.TransactionManager.CommittedTransactions.Count != steps:
            wrong.append(f"the review changed something: dirty {before} -> {after}")
        for path in inv.view_images(["iso"], 200, 150, isolate="rv_short:1"):   # a picture is a look, too
            os.remove(path)
        pictured = sorted(d.DisplayName for d in inv.app.Documents if d.Dirty)
        if pictured != before:
            wrong.append(f"a picture left documents marked as changed: {pictured}")
        for d in [d for d in inv.app.Documents if d.DisplayName.startswith("rv_")]:
            try:
                d.Close(True)
            except Exception:
                pass                            # a part goes with the assembly that holds it
        main.Activate()
        if wrong:
            _raise("; ".join(wrong))
        return f"{len(out['problems'])} problems, {len(out['to_look_at'])} to look at, {len(out['not_checked'])} not checked, {out['seconds']} s"
    run.step("review_assembly: one of each mistake, each found once, nothing changed", seeded_review)

    def same_either_way():
        """What is read inside Inventor is what the COM code reads: rows, refs, order."""
        if not inv.reading_mode().startswith("inside"):
            _raise("not reading inside Inventor: " + inv.reading_mode())
        queries = {
            "faces of the plate (it has an assembly cut)": lambda: inv.find_faces("smoke_plate:1", max_count=10 ** 6),
            "edges of the plate": lambda: inv.find_edges("smoke_plate:1", max_count=10 ** 6),
            "the cut's cylinder": lambda: inv.find_faces("smoke_plate:1", surface="cylinder", diameter=4),
            "faces of the pin by index": lambda: inv.find_faces("2", max_count=10 ** 6),
            "the assembly snapshot": lambda: [inv.assembly_info()],
        }
        inside = {k: q() for k, q in queries.items()}
        inv._rules_off = "comparison"
        try:
            through_com = {k: q() for k, q in queries.items()}
        finally:
            inv._rules_off = ""
        different = [k for k in queries if inside[k] != through_com[k]]
        cut = inside["the cut's cylinder"]
        if different or len(cut) != 1:
            _raise(f"different: {different}; rows for the cut: {cut}")
        return f"{len(inside['faces of the plate (it has an assembly cut)'])} faces, "                f"{len(inside['edges of the plate'])} edges, the same both ways"
    run.step("read inside Inventor = read through COM", same_either_way)

    def deeper_info():
        info = inv.assembly_info()
        files = [c.get("file") for c in info["components"]]
        if files != ["smoke_plate.ipt", "smoke_pin_b.ipt"] or info.get("parts_total") != 2 \
                or info.get("parts_different") != 2:
            _raise(str(info))
        inv.place_component(plate, 0, 0, 100)
        try:
            info = inv.assembly_info()
            if info["parts_total"] != 3 or info["parts_different"] != 2 \
                    or info["most_used"] != [{"file": "smoke_plate.ipt", "count": 2}]:
                _raise(str(info))
        finally:
            inv.delete_occurrence("smoke_plate:2", force=True)
        if "type" in inv.get_document_info() or inv.get_document_info()["type_name"] != "assembly":
            _raise(str(inv.get_document_info()))
        return f"{info['parts_total']} parts, {info['parts_different']} different"
    run.step("assembly_info: files, totals, most used", deeper_info)

    def clearances():
        inv.place_component(plate, 0, 0, 10.1)   # 0.1 mm above the grounded plate
        try:
            out = inv.check_clearances()
            pairs = [(r["a"], r["b"], r["gap_mm"]) for r in out["too_close"]]
            if ("smoke_plate:1", "smoke_plate:2", 0.1) not in pairs:
                _raise(str(out))
            return out
        finally:
            inv.delete_occurrence("smoke_plate:2", force=True)
    run.step("check_clearances finds a 0.1 mm gap", clearances)

    def assembly_drawing():
        folder = os.path.dirname(pin)
        inv.save_document(os.path.join(folder, "smoke_asm.iam"), overwrite=True)
        out = inv.make_drawing(os.path.join(folder, "smoke_asm.pdf"), parts_list=True, balloons=True)
        if "parts list: 2 row(s)" not in out or "balloons: 2 on" not in out or "FAILED" in out or "COVERS" in out:
            _raise(out)
        return out
    run.step("make_drawing parts list and balloons", assembly_drawing)

    run.step("ground pin", lambda: inv.ground_occurrence(2, True))
    run.step("unground pin", lambda: inv.ground_occurrence(2, False))
    run.step("delete_occurrence", lambda: inv.delete_occurrence(2, force=True))


def _first(text):
    """First index out of a '[i] circle ...' listing."""
    import re
    if not isinstance(text, str):
        return None
    m = re.search(r"^\[(\d+)\]", text, re.M)
    return int(m.group(1)) if m else None


def _assert_mated(inv):
    """The pin was placed 60mm off in X; the Insert constraint must pull it back."""
    info = inv.assembly_info()
    pin = info["components"][1]
    dx = abs(pin["position_mm"][0])
    if dx > 1.0:
        raise AssertionError(f"pin still {dx:.1f}mm off in X — constraint did not solve")
    return f"pin at x={pin['position_mm'][0]:.2f}mm"


def main():
    keep = "--keep" in sys.argv
    own = "--own" in sys.argv  # a separate Inventor, quit at the end
    workdir = os.path.join(tempfile.gettempdir(), "inventor_mcp_smoke")
    os.makedirs(workdir, exist_ok=True)

    inv = api.InventorConnection()
    run = Runner()

    print("Inventor MCP assembly smoke test")
    print("-" * 60)
    try:
        print(" ", inv.connect(new_instance=own))
    except Exception as e:
        print(f"Cannot connect to Inventor: {e}")
        return 1
    print("  version:", inv.app.SoftwareVersion.DisplayVersion)
    print("  workdir:", workdir)
    if own:
        inv.app.SilentOperation = True

    print("\nScratch parts:")
    plate, pin = build_parts(inv, run, workdir)
    print("\nAssembly:")
    assembly_tests(inv, run, plate, pin)

    if not keep:
        print("\nClosing scratch documents...")
        for doc in list(inv.app.Documents):
            try:
                doc.Close(True)  # SkipSave
            except Exception:
                pass

    if own and not keep:
        inv.app.Quit()
        inv._app = None
        import time
        time.sleep(10)  # leaving at once can leave a windowless Inventor process behind

    print("\n" + "-" * 60)
    print(f"{run.passed} passed, {len(run.failed)} failed")
    for label, err in run.failed:
        print(f"  FAILED  {label}: {err}")
    return len(run.failed)


if __name__ == "__main__":
    sys.exit(main())
