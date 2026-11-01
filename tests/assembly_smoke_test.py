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
