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
check("wrapped tools keep their parameters",
      {"edge_indices", "distance", "auto_miter"} <= set(flange.inputSchema.get("properties", {})))

print("Inventor thread (no Inventor needed):")
import threading, time
srv = pkg
srv.CALL_TIMEOUT = 0.5
gate = threading.Event()


def slow_job():
    gate.wait(5)
    return "slow job done"


def quick_job():
    return threading.current_thread().name


out = srv._on_com_thread(quick_job, (), {})
check("calls run on the Inventor thread", out.startswith("inventor-com"), out)
out = srv._on_com_thread(slow_job, (), {})
check("a long call returns before the client times out", "keeps running" in out, out.splitlines()[0])
out = srv._on_com_thread(quick_job, (), {})
check("the next call is refused while one is running", "NOT started" in out)
check("call_status reports the running call", "running for" in srv.call_status())
gate.set()
for _ in range(50):
    if srv._last_call.future.done():
        break
    time.sleep(0.05)
out = srv.call_status()
check("call_status returns the result once finished", "slow job done" in out, out.splitlines()[0])
check("…only once", "already returned" in srv.call_status())

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
