"""Static checks that need no Inventor — runs in CI on a plain Windows runner.

    python tests/check_server.py

- the server imports both ways MCP clients launch it
  (`python -m inventor_mcp_server` package import, and
  `python inventor_mcp_server/server.py` script import)
- the deprecated `src/` shims still forward to the package (0.2.x configs)
- every tool has a description (it is what the AI client reads)
- the tool count advertised in README.md matches reality

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

flange = next(t for t in pkg_tools if t.name == "flange")
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
check("call_status reports the running call", "running for" in srv.call_status())
gate.set()
wait_done(A)
out = srv.call_status()
check("call_status returns the result once finished", "slow job done" in out, out.splitlines()[0])
check("…only once", "already returned" in srv.call_status())

print("Several Inventors (no Inventor needed):")
com_tools = [t for t in pkg_tools if t.name not in ("call_status", "list_instances")]
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
      "[default]" in srv.call_status() and "[second]" in srv.call_status())
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

print("Pictures (no Inventor needed):")
import base64, tempfile
PNG = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==")


def fake_views(self, views, width, height, white, focus, sketches, up):
    folder = tempfile.mkdtemp()
    paths = [os.path.join(folder, f"{v}.png") for v in views]
    for p in paths:
        open(p, "wb").write(PNG)
    return paths


real_views = srv._api.InventorConnection.view_images
srv._api.InventorConnection.view_images = fake_views
result = asyncio.run(pkg.mcp.call_tool("view_image", {"views": "iso,top"}))
srv._api.InventorConnection.view_images = real_views
blocks = result[0] if isinstance(result, tuple) else getattr(result, "content", result)
kinds = [getattr(b, "type", None) for b in blocks]
check("view_image reaches the client as images", kinds == ["image", "image"], str(kinds))

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
