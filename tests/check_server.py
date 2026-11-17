"""Static checks that need no Inventor — runs in CI on a plain Windows runner.

    python tests/check_server.py

- the server imports both ways MCP clients launch it
  (`python -m inventor_mcp_server` package import, and
  `python inventor_mcp_server/server.py` script import)
- the deprecated `src/` shims still forward to the package (0.2.x configs)
- every tool has a description (it is what the AI client reads)
- the tool count advertised in README.md matches reality
- execute_python's live output, call_status(wait_seconds), cancel_call, readable COM
  errors and save_document's library guard — against fake Inventor objects

Exit code is the number of failed checks. For geometry, run tests/smoke_test.py
against a real Inventor.
"""
import asyncio
import importlib
import importlib.util
import json
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

failed = []


def check(label, ok, detail=""):
    print(f"  {'ok  ' if ok else 'FAIL'}  {label}" + (f"  — {detail}" if detail else ""))
    if not ok:
        failed.append(label)


def tools_of(module):
    return asyncio.run(module.mcp.list_tools())


print("Server import:")
pkg = importlib.import_module("inventor_mcp_server.server")
pkg_tools = tools_of(pkg)
check("package import (python -m inventor_mcp_server)", len(pkg_tools) > 0, f"{len(pkg_tools)} tools")

spec = importlib.util.spec_from_file_location(
    "server_as_script", os.path.join(ROOT, "inventor_mcp_server", "server.py"))
script = importlib.util.module_from_spec(spec)
spec.loader.exec_module(script)
script_tools = tools_of(script)
check("script import (python inventor_mcp_server/server.py)", len(script_tools) == len(pkg_tools),
      f"{len(script_tools)} tools")

print("Deprecated src/ shims:")
spec = importlib.util.spec_from_file_location("legacy_server", os.path.join(ROOT, "src", "server.py"))
legacy = importlib.util.module_from_spec(spec)
spec.loader.exec_module(legacy)
check("src/server.py forwards to the package", legacy.main is pkg.main)
legacy_api = importlib.import_module("src.inventor_api")
check("src.inventor_api is the package module",
      legacy_api is importlib.import_module("inventor_mcp_server.inventor_api"))

print("Tools:")
names = [t.name for t in pkg_tools]
check("tool names are unique", len(names) == len(set(names)))
missing = [t.name for t in pkg_tools if not (t.description or "").strip()]
check("every tool has a description", not missing, ", ".join(missing))

flange = next(t for t in pkg_tools if t.name == "add_flange")
schema = getattr(flange, "input_schema", None) or getattr(flange, "inputSchema", {})  # mcp 2 renamed it
check("wrapped tools keep their parameters",
      {"edge_indices", "distance", "auto_miter"} <= set(schema.get("properties", {})))

read_only = sorted(t.name for t in pkg_tools   # by wire name: the attribute differs between SDK 1.x and 2.x
                   if t.annotations and t.annotations.model_dump(by_alias=True).get("readOnlyHint"))
check("the tools that only look are marked read-only, and no tool that changes something is",
      read_only == ["capture_view", "check_fastener_alignment", "check_interference", "check_sketch", "find_edges", "find_faces", "get_status",
                    "inspect_model", "list_instances", "measure_distance"], str(read_only))

print("Inventor thread (no Inventor needed):")
import threading, time
srv = pkg
srv.CALL_TIMEOUT = 0.5
import tempfile as _tempfile
srv.CALLS_DIR = _tempfile.mkdtemp(prefix="inventor_mcp_calls_")   # not the user's ~/.inventor-mcp
gate = threading.Event()
A = srv._instances[srv.DEFAULT]


def slow_job():
    gate.wait(5)
    return "slow job done"


def quick_job():
    return threading.current_thread().name


def wait_done(inst):
    for _ in range(50):
        if inst.last_call.future.done():
            return
        time.sleep(0.05)


out = srv._on_com_thread(A, quick_job, (), {})
check("calls run on the Inventor thread", out.startswith("inventor-default"), out)
out = srv._on_com_thread(A, slow_job, (), {})
check("a long call returns before the client times out", "keeps running" in out, out.splitlines()[0])
result_file = A.last_call.result_file
check("…and says where its result will be written",
      result_file.startswith(srv.CALLS_DIR) and result_file in out and not os.path.exists(result_file), result_file)
out = srv._on_com_thread(A, quick_job, (), {})
check("the next call is refused while one is running", "NOT started" in out)
status = lambda **kw: asyncio.run(srv.call_status(**kw))
check("call_status reports the running call", "running for" in status())
threading.Timer(0.3, gate.set).start()
t = time.monotonic()
out = status(wait_seconds=10)
took = time.monotonic() - t
check("call_status(wait_seconds) returns as soon as the call finishes",
      "slow job done" in out and took < 3, f"{took:.2f} s")
check("…only once", "already returned" in status())
for _ in range(40):          # the file is written by the call's done-callback
    if os.path.exists(result_file):
        break
    time.sleep(0.05)
record = json.load(open(result_file, encoding="utf-8")) if os.path.exists(result_file) else {}
check("the long call's result is also on disk, for when manage_call can't be reached",
      record.get("result") == "slow job done" and record.get("status") == "finished"
      and record.get("call") == "slow_job" and record.get("seconds", 0) > 0, str(record))
n_files = len(os.listdir(srv.CALLS_DIR))
srv._on_com_thread(A, quick_job, (), {})
check("a call that answered in time writes no file", len(os.listdir(srv.CALLS_DIR)) == n_files)


def failing_job():
    gate2.wait(5)
    raise ValueError("no such face")


gate2 = threading.Event()
srv._on_com_thread(A, failing_job, (), {})
failed_file = A.last_call.result_file
gate2.set()
wait_done(A)
for _ in range(40):
    if os.path.exists(failed_file):
        break
    time.sleep(0.05)
record = json.load(open(failed_file, encoding="utf-8")) if os.path.exists(failed_file) else {}
check("a long call that fails leaves its error on disk",
      record.get("status") == "error" and "no such face" in str(record.get("result")), str(record))
status()   # collect it, so the next sections start clean

eta_call = srv._Call("job")
eta_call.started = time.monotonic() - 30
srv._local.call = eta_call
srv._progress(25, 100)
check("progress(done, total) gives the time left", eta_call.progress[1] == "25 of 100"
      and "about 9" in srv._eta(eta_call) and "s left" in srv._live_report(eta_call), srv._live_report(eta_call).strip())
srv._progress("placing bolts")
check("progress(text) still works", eta_call.progress[1] == "placing bolts")
srv._local.call = None

print("Several Inventors (no Inventor needed):")
com_tools = [t for t in pkg_tools if t.name not in ("manage_call", "list_instances")]
no_inst = [t.name for t in com_tools if "instance" not in (getattr(t, "input_schema", None) or t.inputSchema)["properties"]]
check("every Inventor tool takes an instance parameter", not no_inst, ", ".join(no_inst))
B = srv._instances.setdefault("second", srv._Instance("second"))
check("one connected Inventor needs no instance name", srv._pick("") is A)
A.conn._app = B.conn._app = object()  # pretend both are connected
try:
    srv._pick("")
    check("an unnamed call is refused when several are connected", False)
except srv._NoInstance as e:
    check("an unnamed call is refused when several are connected", "default, second" in str(e), str(e))
check("an unknown name is refused",
      "No Inventor is connected as 'x'" in asyncio.run(srv.tool()(quick_job)(instance="x")))
gate.clear()
srv._on_com_thread(A, slow_job, (), {})
out = srv._on_com_thread(B, quick_job, (), {})
check("a busy Inventor doesn't hold up the other one", out.startswith("inventor-second"), out)
check("call_status without a name reports every Inventor",
      "[default]" in status() and "[second]" in status())
gate.set()
wait_done(A)
srv._on_com_thread(A, lambda: A.ns.update(x=1), (), {})
check("each Inventor has its own execute_python namespace", "x" in A.ns and "x" not in B.ns)


async def two_clients():  # a slow call on A must not hold up B at the server level either
    slow, quick = srv.tool()(slow_job), srv.tool()(quick_job)
    gate.clear()
    t = time.monotonic()
    a = asyncio.create_task(slow(instance="default"))
    b = await quick(instance="second")
    took = time.monotonic() - t
    gate.set()
    await a
    return b, took
b, took = asyncio.run(two_clients())
check("the server answers one agent while another's call runs",
      b.startswith("inventor-second") and took < 0.4, f"{took:.2f} s")
A.conn._app = None
del srv._instances["second"]


async def loop_stays_free():  # call_status(wait_seconds) must wait off the event loop
    gate.clear()
    srv._on_com_thread(A, slow_job, (), {})
    waiting = asyncio.create_task(srv.call_status(wait_seconds=2))
    t = time.monotonic()
    await asyncio.sleep(0.05)
    took = time.monotonic() - t
    gate.set()
    await waiting
    return took
took = asyncio.run(loop_stays_free())
check("call_status(wait_seconds) doesn't hold up the event loop", took < 0.5, f"{took:.2f} s")

print("execute_python, cancel_call, errors (fake Inventor objects):")
import pywintypes


class Fake:
    def __init__(self, **kw):
        self.__dict__.update(kw)


A.conn._app = Fake(SoftwareVersion="fake", SilentOperation=False, ActiveDocument=None,
                   TransientGeometry=None, _oleobj_=None,
                   ErrorManager=Fake(HasErrors=True, HasWarnings=False,
                                     AllMessages="Sketch1 is not closed", LastMessage=""))
run_py = lambda code, **kw: asyncio.run(srv.execute_python(code=code, **kw))

out = run_py("print('first line')\nprogress('half way')\nwhile True: pass")
check("a long execute_python shows its prints and progress() at once",
      "first line" in out and "Progress: half way" in out, out.replace("\n", " | ")[:160])
check("call_status shows them while it runs", "first line" in status() and "half way" in status())
out = asyncio.run(srv.cancel_call())
check("cancel_call stops a while-True loop", "CANCELLED (cancel_call)" in out and "first line" in out,
      out.replace("\n", " | ")[:160])
out = srv._on_com_thread(A, quick_job, (), {})
check("the Inventor thread works after a cancel", out.startswith("inventor-default"), out)
out = run_py("print('polling')\nwhile not cancelled(): pass", timeout=0.2)
check("execute_python(timeout) cancels the run", "timeout 0.2 s" in out,
      out.replace("\n", " | ")[:160])
out = run_py("n = 0\nwhile not cancelled():\n    n += 1\nprint('stopped by itself')")
out = asyncio.run(srv.cancel_call())
check("a loop polling cancelled() stops cleanly before the interrupt",
      "stopped by itself" in out and "CANCELLED" not in out, out.replace("\n", " | ")[:160])
out = run_py("while True:\n    try:\n        sum(range(3000))\n    except BaseException:  # like win32com's bare excepts\n"
             "        pass\n    sum(range(3000))")
out = asyncio.run(srv.cancel_call())
check("cancel_call gets through code that swallows a KeyboardInterrupt", "CANCELLED" in out,
      out.replace("\n", " | ")[:160])
check("cancel_call with nothing running says so", "Nothing is running" in asyncio.run(srv.cancel_call()))

for i in range(60):  # a cancel that races the end of a call must not hit the next one
    # the timer reads A.last_call when it fires: wait for it, or a late one cancels a later
    # test's call (it did once: view_image "cancelled after 0 s")
    timer = threading.Timer(0.0004 * (i % 6), lambda: srv._cancel(A, A.last_call))
    timer.start()
    try:
        srv._on_com_thread(A, lambda: sum(range(30000)), (), {})
    except srv._Cancelled:
        pass
    timer.join()
    wait_done(A)
out = srv._on_com_thread(A, quick_job, (), {})
check("cancels racing the end of a call never leak into the next", out.startswith("inventor-default"), out)

out = run_py("import pywintypes\nraise pywintypes.com_error(-2147352567, 'Özel durum oluştu.', "
             "(0, None, None, None, 0, -2147024809), None)")
check("execute_python names the HRESULT and the inner scode",
      "DISP_E_EXCEPTION" in out and "inner E_INVALIDARG" in out, out.splitlines()[-2])
check("…and adds Inventor's own message", "Inventor says: Sketch1 is not closed" in out)


def disconnected():
    raise pywintypes.com_error(-2147417848, "Nesne bağlantısı kesildi", None, None)


try:
    srv._on_com_thread(A, disconnected, (), {})
    check("a tool's COM error reads as its name", False)
except RuntimeError as e:
    check("a tool's COM error reads as its name", "RPC_E_DISCONNECTED" in str(e) and "re-resolve" in str(e), str(e)[:120])
api = srv._api
check("health status numbers read as enum names (fallback)",
      api.health_name(A.conn._app, 11781) == "kInErrorHealth" and api.health_name(None, 11778) == "kUpToDateHealth")
check("only unhealthy features are flagged",
      api.unhealthy(None, Fake(HealthStatus=11786)) == "kInconsistentHealth"
      and api.unhealthy(None, Fake(HealthStatus=11778)) == "")
A.conn._app = None

print("save_document never writes library files (fake Inventor objects):")
import tempfile
root = tempfile.mkdtemp()
lib, cc = os.path.join(root, "Library"), os.path.join(root, "CC Files")
for d in (lib, cc):
    os.makedirs(d)


def fake_doc(name, folder, dirty=True, member=False, refs=()):
    d = Fake(DisplayName=name, FullFileName=os.path.join(folder, name) if folder else "", Dirty=dirty,
             DocumentType=12291 if name.endswith(".iam") else 12290, AllReferencedDocuments=list(refs),
             ComponentDefinition=Fake(IsContentMember=member), saves=[])
    d.Save2 = lambda deps=True, d=d: (d.saves.append(deps), setattr(d, "Dirty", False))
    d.SaveAs = lambda path, copy, d=d: d.saves.append(path)
    return d


def fake_app(active):
    paths = [Fake(Path="Library")]
    proj = Fake(FullFileName=os.path.join(root, "p.ipj"), ContentCenterPath="",
                LibraryPaths=Fake(Count=1, Item=lambda i: paths[i - 1]))
    return Fake(ActiveDocument=active, SilentOperation=False, Documents=[active],
                DesignProjectManager=Fake(ActiveDesignProject=proj),
                FileOptions=Fake(ContentCenterPath=cc))


mine, sub = fake_doc("plate.ipt", root), fake_doc("frame.iam", root)
std, std2 = fake_doc("ISO 4762 M8x20.ipt", cc), fake_doc("bracket.ipt", lib)
member, clean = fake_doc("U 100 L1500.ipt", root, member=True), fake_doc("pin.ipt", root, dirty=False)
asm = fake_doc("top.iam", root, refs=[mine, sub, std, std2, member, clean])
c = api.InventorConnection()
c._app = fake_app(asm)
out = c.save_document(save_dependents=True)
check("dirty project files are saved one by one, without dependents",
      mine.saves == [False] and sub.saves == [False] and asm.saves == [False] and clean.saves == [],
      out.splitlines()[1])
check("Content Center / library files and members are skipped",
      std.saves == std2.saves == member.saves == [] and "Not saved (3" in out, out.replace("\n", " | "))
mine.Dirty = True
asm2 = fake_doc("top2.iam", root, refs=[mine])
c._app = fake_app(asm2)
out = c.save_document(save_dependents=False)
check("save_dependents=False saves only the document", asm2.saves == [False] and mine.saves == [False]
      and "left unsaved" in out, out.replace("\n", " | "))
c._app = fake_app(std)
try:
    c.save_document()
    check("a Content Center file itself is never saved", False)
except RuntimeError as e:
    check("a Content Center file itself is never saved", std.saves == [] and "library file" in str(e), str(e)[:100])
c._app = fake_app(fake_doc("new.ipt", ""))
try:
    c.save_document(os.path.join(cc, "new.ipt"))
    check("nothing is saved into a Content Center folder", False)
except RuntimeError as e:
    check("nothing is saved into a Content Center folder", "Content Center folder" in str(e), str(e)[:100])

print("Flat pattern checks (no Inventor needed):")
import math
_api_mod = importlib.import_module("inventor_mcp_server.inventor_api")
_box = [[(0, 0), (100, 0)], [(100, 0), (100, 60)], [(100, 60), (0, 60)], [(0, 60), (0, 0)]]
_ring = lambda cx, cy, r: [(cx + r * math.cos(k * math.pi / 16), cy + r * math.sin(k * math.pi / 16)) for k in range(33)]
found = _api_mod.flat_pattern_problems(_box, [], [{"a": (0, 12), "b": (100, 12), "radius": 1.67}], 1.5, 10)
check("a 12 mm flange on a V10 die is fine, both legs measured",
      found["problems"] == [] and found["bends"][0]["legs_mm"] == [48.0, 12.0], str(found))
found = _api_mod.flat_pattern_problems(_box, [], [{"a": (0, 4), "b": (100, 4), "radius": 1.67}], 1.5, 10)
check("a 4 mm flange does not reach across a V10 die",
      len(found["problems"]) == 1 and "needs 6.5 mm" in found["problems"][0], str(found["problems"]))
found = _api_mod.flat_pattern_problems(
    _box, [{"points": _ring(30, 15, 2.5), "diameter": 5}, {"points": _ring(70, 40, 0.5), "diameter": 1},
           {"points": _ring(50, 12, 3), "diameter": 6}, {"points": _ring(50, 40, 4), "diameter": 8}],
    [{"a": (0, 12), "b": (100, 12), "radius": 1.67}], 1.5, 10)
texts = " | ".join(found["problems"])
check("holes: near the bend, across it, smaller than the sheet, and a good one left alone",
      len(found["problems"]) == 3 and "0.5 mm from the bend line" in texts and "across the bend line" in texts
      and "smaller than the 1.5 mm sheet" in texts, texts)
found = _api_mod.flat_pattern_problems(
    _box, [], [{"a": (0, 20), "b": (100, 20), "radius": 1.67}, {"a": (0, 24), "b": (100, 24), "radius": 1.67}], 1.5, 10)
check("two bends 4 mm apart are named as too close to each other",
      sum("next bend" in p for p in found["problems"]) == 2, str(found["problems"]))
check("TXX-VYY rule names give thickness and die",
      _api_mod.rule_from_name("T15-V10") == (1.5, 10.0) and _api_mod.rule_from_name("t20-v12") == (2.0, 12.0)
      and _api_mod.rule_from_name("Default_mm") is None)

print("Pictures (no Inventor needed):")
import base64, tempfile
PNG = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==")


def fake_views(self, views, *args):
    folder = tempfile.mkdtemp()
    paths = [os.path.join(folder, f"{v}.png") for v in views]
    for p in paths:
        open(p, "wb").write(PNG)
    return paths


real_views = srv._api.InventorConnection.view_images
srv._api.InventorConnection.view_images = fake_views
result = asyncio.run(pkg.mcp.call_tool("capture_view", {"views": "iso,top"}))
srv._api.InventorConnection.view_images = real_views
blocks = result[0] if isinstance(result, tuple) else getattr(result, "content", result)
kinds = [getattr(b, "type", None) for b in blocks]
check("capture_view reaches the client as images", kinds == ["image", "image"], str(kinds))

seen = []
srv._api.InventorConnection.view_images = lambda self, views, *a: seen.append(list(views)) or fake_views(self, views)
asyncio.run(pkg.mcp.call_tool("capture_view", {"look_from": [100, 100, 100]}))
asyncio.run(pkg.mcp.call_tool("capture_view", {"views": "top", "look_from": [100, 100, 100]}))
srv._api.InventorConnection.view_images = real_views
check("capture_view(look_from) takes the custom view, alone or beside the named ones",
      seen == [["custom"], ["top", "custom"]], str(seen))

os.environ["INVENTOR_MCP_PROTECTED"] = r"C:\vendor\*;D:\orig"
reason = api_paths = importlib.import_module("inventor_mcp_server.inventor_api").InventorConnection._path_reason
check("protected paths: a glob and a folder", reason(r"C:\vendor\a.ipt", []) == reason(r"D:\orig\b.iam", []) == "protected path"
      and reason(r"C:\work\a.ipt", []) == "", str(reason(r"D:\orig\b.iam", [])))
del os.environ["INVENTOR_MCP_PROTECTED"]

print("Profiles with arcs (no Inventor needed):")
import math
api_mod = importlib.import_module("inventor_mcp_server.inventor_api")
segs = api_mod.profile_segments([[0, 0, 10], [200, 0, 10], [200, 100, 10], [0, 100, 10]])
lines = [s for s in segs if s[0] == "line"]
arcs = [s for s in segs if s[0] == "fillet"]
check("rounded rectangle: 4 trimmed lines and 4 arcs",
      len(lines) == 4 and len(arcs) == 4 and all(abs(a - b) < 1e-9 for a, b in zip(
          lines[0][1] + lines[0][2], (10, 0, 190, 0))), str(lines[0]))
mid = arcs[0][2]  # corner (200, 0): center (190, 10), the middle 10 mm from it towards the corner
check("…each arc's middle lies on the radius",
      abs(math.hypot(mid[0] - 190, mid[1] - 10) - 10) < 1e-9 and mid[0] > 190 and mid[1] < 10, str(mid))
segs = api_mod.profile_segments([[0, -50], [0, 50], {"through": [50, 0]}])
check("a D shape: a line and the closing arc", [s[0] for s in segs] == ["line", "arc"]
      and segs[1][1:] == ((0.0, 50.0), (50.0, 0.0), (0.0, -50.0)), str(segs))
for bad, why in (([[0, 0, 60], [100, 0, 60], [100, 100], [0, 100]], "don't fit"),
                 ([[0, 0], [10, 0, 2], {"through": [15, 5]}, [10, 10]], "straight lines on both sides")):
    try:
        api_mod.profile_segments(bad)
        check(f"refused: {why}", False)
    except ValueError as e:
        check(f"refused: {why}", why in str(e), str(e))

print("Keeper (no Inventor needed):")
keeper_mod = importlib.import_module("inventor_mcp_server.keeper")
k = keeper_mod.Keeper()
k.kept = {0x7FFFFFF0: (object(), True)}  # a window handle that doesn't exist
check("the keeper lets go of an Inventor whose window is gone", k.Count() == 0)

import io
default, captured = io.StringIO(), io.StringIO()
proxy = srv._ThreadStdout(default)
worker = threading.Thread(target=lambda: (setattr(proxy.local, "buf", captured), proxy.write("inventor")))
worker.start(); worker.join()
proxy.write("other thread")
check("execute_python captures only the Inventor thread's prints",
      captured.getvalue() == "inventor" and default.getvalue() == "other thread")

print("Assemblies, views and drawings (fake Inventor objects):")


def raises(fn, fragment):
    try:
        fn()
    except Exception as e:
        return fragment in str(e), str(e)[:140]
    return False, "no error"


class FakeMatrix:
    """Rows of a 4x4 transform; lengths in cm like Inventor."""
    def __init__(self, rows):
        self.rows = rows

    def Cell(self, r, c):
        return self.rows[r - 1][c - 1]


def fake_box(lo, hi):
    return Fake(MinPoint=Fake(X=lo[0], Y=lo[1], Z=lo[2]), MaxPoint=Fake(X=hi[0], Y=hi[1], Z=hi[2]))


def fake_occ(name, lo, hi, member=False):
    return Fake(Name=name, RangeBox=fake_box(lo, hi), Definition=Fake(IsContentMember=member))


conn = api.InventorConnection()
# a component turned 90° about Z and moved to (100, 20, 5) mm
turned = Fake(Transformation=FakeMatrix([[0, -1, 0, 10.0], [1, 0, 0, 2.0], [0, 0, 1, 0.5], [0, 0, 0, 1]]))
conn._occ = lambda path: turned
top = conn.to_top("P:1", [10, 0, 3])
check("to_top turns and moves a component point into assembly mm", top == [100.0, 30.0, 8.0], str(top))
check("to_local is its inverse", conn.to_local("P:1", top) == [10.0, 0.0, 3.0], str(conn.to_local("P:1", top)))

# plates stacked along Z (cm): a|b touch, b|c are 0.1 mm apart, d is far away, a bolt sits in the gap
occs = [fake_occ("a:1", (0, 0, 0), (8, 6, 1.2)), fake_occ("b:1", (0, 0, 1.2), (8, 6, 2.4)),
        fake_occ("c:1", (0, 0, 2.41), (8, 6, 3.61)), fake_occ("d:1", (0, 0, 8), (8, 6, 9.2)),
        fake_occ("ISO 4762 M6:1", (1, 1, 2.4), (1.6, 1.6, 2.41), member=True)]
measured = []


def fake_distance(x, y):
    measured.append((x.Name, y.Name))
    gap = max(x.RangeBox.MinPoint.Z - y.RangeBox.MaxPoint.Z, y.RangeBox.MinPoint.Z - x.RangeBox.MaxPoint.Z, 0)
    return (gap, None)   # a tuple under pywin32: distance in cm first


leaves = Fake(Count=len(occs), Item=lambda i: occs[i - 1])
conn._app = Fake(ActiveDocument=Fake(DocumentType=12291, ComponentDefinition=Fake(Occurrences=Fake(AllLeafOccurrences=leaves))),
                 MeasureTools=Fake(GetMinimumDistance=fake_distance))
out = conn.check_clearances(0.3)
check("clearances: only neighbours are measured, fasteners left out",
      sorted(measured) == [("a:1", "b:1"), ("b:1", "c:1")] and out["parts"] == 4, str(measured))
check("clearances: touching is fine, a 0.1 mm gap is reported",
      out["touching"] == 1 and out["too_close"] == [{"a": "b:1", "b": "c:1", "gap_mm": 0.1}], str(out))
out = conn.check_clearances(0.3, max_pairs=1)
check("clearances: pairs left unmeasured are named", any("not measured" in n for n in out["notes"]), str(out["notes"]))

# view arguments are checked before Inventor is asked for anything
ok, why = raises(lambda: conn.view_images(["sideways"]), "Unknown view")
check("capture_view: an unknown view is refused", ok, why)
ok, why = raises(lambda: conn.view_images(["custom"]), "needs look_from")
check("capture_view: the custom view needs look_from", ok, why)
ok, why = raises(lambda: conn.view_images(["iso"], zoom=0), "zoom")
check("capture_view: zoom must be above 0", ok, why)

cam = Fake(fit=0, extents=None)
cam.Fit = lambda: setattr(cam, "fit", cam.fit + 1)
cam.SetExtents = lambda w, h: setattr(cam, "extents", (round(w, 3), round(h, 3)))
conn._tg = lambda: Fake(CreatePoint=lambda *xyz: tuple(round(v, 6) for v in xyz),
                        CreateUnitVector=lambda *xyz: tuple(xyz))
model = fake_box((0, 0, 0), (8, 6, 1.2))
conn._custom_view(cam, [300, 0, 5], None, "z", model)
check("a camera from a point: mm become cm, the model's middle is the target, the model is fitted",
      cam.Eye == (30.0, 0.0, 0.5) and cam.Target == (4.0, 3.0, 0.6) and cam.UpVector == (0.0, 0.0, 1.0)
      and cam.fit == 1, str((cam.Eye, cam.Target, cam.UpVector, cam.fit)))
conn._custom_view(cam, [40, 30, 500], [40, 30, 0], "z", model)
check("looking straight along `up` takes another up vector; a target frames the picture instead of fitting",
      cam.UpVector == (0.0, 1.0, 0.0) and cam.fit == 1 and cam.extents == (10.4, 10.4),
      str((cam.UpVector, cam.fit, cam.extents)))
ok, why = raises(lambda: conn._custom_view(cam, [1, 2, 3], [1, 2, 3], "z", model), "same point")
check("a camera at its own target is refused", ok, why)

# drawing arguments, also before any COM call
part = Fake(DocumentType=12290, FullFileName=r"C:\work\plate.ipt", DisplayName="plate.ipt", ComponentDefinition=Fake())
conn._app = Fake(ActiveDocument=part)
for label, kwargs, fragment in (
        ("a parts list needs an assembly", dict(parts_list=True), "for an assembly"),
        ("balloons need an assembly", dict(balloons=True), "for an assembly"),
        ("a vertical section and the right view can't share a place", dict(section="vertical"), "leave that view out"),
        ("a horizontal section and the top view can't either", dict(section="horizontal:5", views="front,top"), "leave that view out"),
        ("an unknown section direction is refused", dict(section="diagonal", views="front"), "vertical"),
        ("a detail needs a point and a radius", dict(detail=[1, 2, 3], views="front"), "[x, y, z, radius]"),
        ("section and detail are not for a flat pattern", dict(flat=True, section="vertical"), "folded model"),
        ("the base view must be an orthographic one", dict(views="iso"), "first view")):
    ok, why = raises(lambda: conn.make_drawing(r"C:\work\out.pdf", **kwargs), fragment)
    check("create_drawing: " + label, ok, why)
conn._app = Fake(ActiveDocument=Fake(DocumentType=12290, FullFileName="", DisplayName="Part1", ComponentDefinition=Fake()))
ok, why = raises(lambda: conn.make_drawing(r"C:\work\out.pdf"), "Save the model first")
check("create_drawing: an unsaved model is refused", ok, why)

# features by index or name, patterns and mirrors
feats = [Fake(Name="Extrusion1"), Fake(Name="Hole1")]


def feature_item(key):
    if isinstance(key, int) and 1 <= key <= len(feats):
        return feats[key - 1]
    return next(f for f in feats if f.Name == key)


picked = []
shop = Fake(Features=Fake(Count=2, Item=feature_item), SurfaceBodies=Fake(Count=1),
            WorkAxes=Fake(Item=lambda i: Fake(Name="XYZ"[i - 1] + " Axis")),
            WorkPlanes=Fake(Count=3, Item=lambda k: (_ for _ in ()).throw(KeyError(k)) if isinstance(k, str) else Fake(Name="YZ Plane")))
conn._app = Fake(ActiveDocument=Fake(DocumentType=12290, ComponentDefinition=shop),
                 TransientObjects=Fake(CreateObjectCollection=lambda: Fake(Add=picked.append)))
col, found_names = conn._features(shop, "2, Extrusion1")
check("features are taken by index or by name", found_names == ["Hole1", "Extrusion1"] and len(picked) == 2, str(found_names))
ok, why = raises(lambda: conn._features(shop, "Hole9"), "[2] Hole1")
check("an unknown feature gets the list of features", ok, why)
for label, call, fragment in (
        ("one instance is not a pattern", lambda: conn.rectangular_pattern("2", 1, 10), "at least 2"),
        ("a second direction needs its spacing", lambda: conn.rectangular_pattern("2", 2, 10, "X", 3, 0), "spacing"),
        ("the two directions must differ", lambda: conn.rectangular_pattern("2", 2, 10, "X", 2, 10, "x"), "must differ"),
        ("an unknown mirror plane gets the choices", lambda: conn.mirror("2", plane="Middle"), "Use XY, XZ, YZ"),
        ("remove_original is for a mirrored body", lambda: conn.mirror("2", remove_original=True), "body=True")):
    ok, why = raises(call, fragment)
    check("patterns and mirrors: " + label, ok, why)

print("Holes of parts that lie on each other (no Inventor needed):")
align = importlib.import_module("inventor_mcp_server.alignment")
scan_mod = importlib.import_module("inventor_mcp_server.api_scan")


def _rect(x0, y0, x1, y1, z):
    c = [(x0, y0, z), (x1, y0, z), (x1, y1, z), (x0, y1, z)]
    return [{"k": "l", "pts": [list(c[i]), list(c[(i + 1) % 4])]} for i in range(4)]


def _hole(x, y, d):
    return lambda z: {"outer": False, "edges": [{"k": "c", "c": [x, y, z], "r": d / 2}]}


def _slot(x, y, d, length):
    r, h = d / 2, (length - d) / 2

    def make(z):
        arc = lambda cx, a0: [[cx + r * math.cos(a0 + math.pi * k / 8), y + r * math.sin(a0 + math.pi * k / 8), z]
                              for k in range(9)]
        return {"outer": False, "edges": [
            {"k": "a", "c": [x + h, y, z], "r": r, "pts": arc(x + h, -math.pi / 2)},
            {"k": "l", "pts": [[x + h, y + r, z], [x - h, y + r, z]]},
            {"k": "a", "c": [x - h, y, z], "r": r, "pts": arc(x - h, math.pi / 2)},
            {"k": "l", "pts": [[x - h, y - r, z], [x + h, y - r, z]]}]}
    return make


def _plate(w, h, t, openings):
    face = lambda z, n: {"p": [1, 1, z], "n": n, "area": w * h,
                         "loops": [{"outer": True, "edges": _rect(0, 0, w, h, z)}] + [mk(z) for mk in openings]}
    return {"faces": [face(t, [0, 0, 1]), face(0, [0, 0, -1])]}


# a 200 x 100 base; a 160 x 60 bracket lies on it (its underside at z = 2)
base = _plate(200, 100, 2, [_hole(20, 20, 6.5), _hole(60, 20, 6.5), _hole(100, 20, 6.5), _hole(180, 80, 6.5),
                            _slot(140, 20, 6.5, 16), _hole(120, 45, 30)])
bracket = _plate(160, 60, 3, [_hole(20, 20, 6.5), _hole(61.5, 20, 6.5), _hole(143, 20, 6.5), _hole(30, 45, 9),
                              _hole(139.5, 45, 10)])
on_top = [1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1, 2]
placed = [{"path": "base:1", "file": "base.ipt", "m": scan_mod.IDENTITY},
          {"path": "bracket:1", "file": "bracket.ipt", "m": on_top}]
found = align.check(placed, {"base.ipt": base, "bracket.ipt": bracket})
rows = found["aligned_rows"]
check("holes on each other: one aligned pair, and a hole inside a slot's travel",
      found["aligned"] == 2 and sorted(r["b_opening"] + "|" + r["a_opening"] for r in rows)
      == ["Ø6.5|slot 6.5x16", "Ø6.5|Ø6.5"], str(rows))
slot_row = next(r for r in rows if "slot" in r["a_opening"])
check("a slot row points at the round hole, where the bolt goes",
      slot_row["at"] == [143, 20, 2] and slot_row["passes"] == "M6", str(slot_row))
check("a hole 1.5 mm off is misaligned, with the bolt that still passes",
      len(found["misaligned"]) == 1 and found["misaligned"][0]["offset_mm"] == 1.5
      and found["misaligned"][0]["largest_bolt_mm"] == 5.0 and found["misaligned"][0]["passes"] == "M5",
      str(found["misaligned"]))
blocked = sorted((b["part"], b["opening"], b["behind"]) for b in found["blocked"])
check("holes with solid sheet behind them are blocked — also two that only graze each other",
      blocked == [("base:1", "Ø30", "bracket:1"), ("base:1", "Ø6.5", "bracket:1"),
                  ("bracket:1", "Ø10", "base:1"), ("bracket:1", "Ø9", "base:1")], str(blocked))
check("a hole the other part does not reach is open",
      [(o["part"], o["at"]) for o in found["open"]] == [("base:1", [180, 80, 2])], str(found["open"]))
apart = [placed[0], {"path": "bracket:1", "file": "bracket.ipt", "m": on_top[:11] + [2.4]}]
check("faces 0.4 mm apart still lie on each other; 0.6 mm apart they don't",
      align.check(apart, {"base.ipt": base, "bracket.ipt": bracket})["aligned"] == 2
      and align.check([placed[0], {"path": "bracket:1", "file": "bracket.ipt", "m": on_top[:11] + [2.6]}],
                      {"base.ipt": base, "bracket.ipt": bracket})["pairs_of_parts_in_contact"] == 0)
turned = [0, -1, 0, 60, 1, 0, 0, 0, 0, 0, 1, 2]   # the bracket turned 90° about Z: its hole at (20, 20) lands on (40, 20)
moved = align.check([placed[0], {"path": "bracket:1", "file": "bracket.ipt", "m": turned}],
                    {"base.ipt": _plate(200, 200, 2, [_hole(40, 20, 6.5)]), "bracket.ipt": _plate(160, 60, 3, [_hole(20, 20, 6.5)])})
check("a turned component is compared where it really is", moved["aligned"] == 1, str(moved["aligned"]))
stud = _plate(160, 60, 3, [_hole(20, 20, 6.5)])
for face in stud["faces"]:
    face["loops"][1]["hole"] = False          # the foot of a stud standing on the face, not a hole
with_stud = align.check(placed, {"base.ipt": base, "bracket.ipt": stud})
pinned = [(r["part"], r["opening"], r["stud"], r["clearance_mm"]) for r in with_stud["stud_does_not_fit"]]
check("an inner loop around something that stands on the face is a stud, not an opening: "
      "a Ø6.5 stud in a Ø6.5 hole is engaged",
      with_stud["aligned"] == 0 and len(with_stud["engaged_rows"]) == 1
      and with_stud["engaged_rows"][0]["stud_of"] == "bracket:1" and with_stud["engaged_rows"][0]["clearance_mm"] == 0
      and not pinned, str(with_stud["engaged_rows"]))
for face in stud["faces"]:
    face["loops"][1]["edges"][0]["c"][0] = 21.0     # the stud 1 mm off
off_stud = align.check(placed, {"base.ipt": base, "bracket.ipt": stud})
check("a stud that is 1 mm off in a hole of its own size does not fit",
      [(r["part"], r["clearance_mm"]) for r in off_stud["stud_does_not_fit"]] == [("base:1", -1.0)],
      str(off_stud["stud_does_not_fit"]))
u_shape = [(0, 0, 0), (30, 0, 0), (30, 20, 0), (20, 20, 0), (20, 5, 0), (10, 5, 0), (10, 20, 0), (0, 20, 0)]
u_segments = [(u_shape[i], u_shape[(i + 1) % 8]) for i in range(8)]
inner = align._interior_point(u_segments)
check("a U-shaped cut-out is represented by a point inside it, not by its centroid",
      align._inside((inner[0], inner[1]), [((a[0], a[1]), (b[0], b[1])) for a, b in u_segments]), str(inner))

scan_mod.SCAN_CACHE = _tempfile.mkdtemp(prefix="inventor_mcp_scan_")
part_file = os.path.join(scan_mod.SCAN_CACHE, "plate.ipt")
open(part_file, "w").write("v1")
scan_mod.cache_put(part_file, "rule-a", {"faces": [1]})
check("a scan is kept per file", scan_mod.cache_get(part_file, "rule-a") == {"faces": [1]})
check("…and not used for another version of the rule", scan_mod.cache_get(part_file, "rule-b") is None)
open(part_file, "w").write("version two")
check("…nor once the file has changed", scan_mod.cache_get(part_file, "rule-a") is None)
rule_text = open(os.path.join(ROOT, "inventor_mcp_server", "rules", "scan_planar.iLogicVb"), encoding="utf-8").read()
code = "\n".join(l for l in rule_text.split("\n") if not l.lstrip().startswith("'"))
check("the rule touches no file and starts nothing (iLogic's Security Alert would hold the call)",
      not any(word in code for word in ("System.IO", "Process", "Shell", "CreateObject", "Registry", "WebClient")))

print("Resources:")
listed = asyncio.run(pkg.mcp.list_resources())
uris = sorted(str(r.uri) for r in listed)
check("the API notes and the modelling skill are offered as resources",
      uris == ["inventor://notes/api", "inventor://skills/modeling"], str(uris))
got = asyncio.run(pkg.mcp.read_resource("inventor://notes/api"))
text = "".join(getattr(c, "content", "") or "" for c in (got if isinstance(got, (list, tuple)) else [got]))
check("inventor://notes/api returns the notes", "Tuple-returning calls" in text and len(text) > 5000, str(len(text)))
import tomllib
data = tomllib.load(open(os.path.join(ROOT, "pyproject.toml"), "rb"))["tool"]["setuptools"]["package-data"]
check("the notes are shipped in the package",
      "notes/*.md" in data.get("inventor_mcp_server", [])
      and os.path.isfile(os.path.join(ROOT, "inventor_mcp_server", "notes", "inventor-api-notes.md")))

print("Docs:")
readme = open(os.path.join(ROOT, "README.md"), encoding="utf-8").read()
m = re.search(r"\*\*(\d+) MCP tools\*\*", readme)
check("README tool count matches", bool(m) and int(m.group(1)) == len(names),
      f"README says {m.group(1) if m else '?'}, server has {len(names)}")

print("The wrapper's parts (last: it reloads them):")
import ast
wrapper = importlib.import_module("inventor_mcp_server.inventor_api")
defined = {}
for part in wrapper._PARTS:
    tree = ast.parse(open(os.path.join(ROOT, "inventor_mcp_server", part + ".py"), encoding="utf-8").read())
    for cls in (n for n in tree.body if isinstance(n, ast.ClassDef)):
        for fn in (n for n in cls.body if isinstance(n, ast.FunctionDef)):
            defined.setdefault(fn.name, []).append(part)
twice = {name: parts for name, parts in defined.items() if len(parts) > 1}
check("no method is defined in two parts", not twice, str(twice))
check("InventorConnection has every method of every part",
      all(hasattr(wrapper.InventorConnection, name) for name in defined), str(len(defined)))
before = wrapper.InventorConnection
part_modules = [sys.modules["inventor_mcp_server." + part] for part in wrapper._PARTS]
importlib.reload(wrapper)
check("reloading the wrapper reloads every part and rebuilds the class",
      wrapper.InventorConnection is not before
      and all(getattr(wrapper.InventorConnection, name).__module__ != "inventor_mcp_server.inventor_api"
              for name in ("make_drawing", "mirror", "view_images", "connect"))
      and all(sys.modules["inventor_mcp_server." + p] is m for p, m in zip(wrapper._PARTS, part_modules)))

print(f"\n{len(failed)} failed")
sys.exit(len(failed))
