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

print("Inventor thread (no Inventor needed):")
import threading, time
srv = pkg
srv.CALL_TIMEOUT = 0.5
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

print("Pictures (no Inventor needed):")
import base64, tempfile
PNG = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==")


def fake_views(self, views, width, height, white, focus, sketches, up, isolate):
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

print("Docs:")
readme = open(os.path.join(ROOT, "README.md"), encoding="utf-8").read()
m = re.search(r"\*\*(\d+) MCP tools\*\*", readme)
check("README tool count matches", bool(m) and int(m.group(1)) == len(names),
      f"README says {m.group(1) if m else '?'}, server has {len(names)}")

print(f"\n{len(failed)} failed")
sys.exit(len(failed))
