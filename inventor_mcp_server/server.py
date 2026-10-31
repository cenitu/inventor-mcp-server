"""MCP Server for Autodesk Inventor parametric modeling."""

import ast
import asyncio
import concurrent.futures
import ctypes
import functools
import inspect as _inspect  # `inspect` is a tool name below
import json
import linecache
import math
import os
import sys
import threading
import time
import traceback
from typing import Annotated

from pydantic import Field
try:
    # MCP Python SDK 2.x — FastMCP was renamed MCPServer
    from mcp.server import MCPServer as FastMCP
    from mcp.server.mcpserver import Image
except ImportError:  # SDK 1.x
    from mcp.server.fastmcp import FastMCP, Image
try:
    from . import inventor_api as _api
except ImportError:
    # Run as a script (`python inventor_mcp_server/server.py`), which is how most MCP clients
    # launch it — there is no parent package, so import from this directory.
    import os, sys
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import inventor_api as _api
if _api.WINDOWS:
    import pythoncom

mcp = FastMCP(
    "Autodesk Inventor",
    instructions=(
        "MCP server for parametric 3D modeling in Autodesk Inventor. "
        "All dimensions are in millimeters. "
        "Typical workflow: connect → create_document → create_sketch → draw geometry → extrude/revolve → save. "
        "Assemblies: save the parts first, then create_document → place_components (ground the first one) "
        "→ constrain by origin geometry / constrain(kind='insert') for bolt-in-hole. "
        "For anything not covered by a dedicated tool, use execute_python "
        "(persistent namespace, live inv/app/comp/body/tg objects). "
        "After editing inventor_api.py on disk, call reload_api — no server restart needed. "
        "Working in a project: connect(project=<.ipj path>) — the server reuses the Inventor that "
        "has it or starts a separate one, so several agents on several projects each get their own. "
        "When several Inventors are connected, pass the instance name connect returned to every "
        "call. list_instances shows them."
    ),
)

class _ThreadStdout:
    """sys.stdout that sends the Inventor thread's prints to execute_python's buffer.
    contextlib.redirect_stdout would swap stdout for the whole process — and a call can
    now keep running in the background while other threads print."""

    def __init__(self, default):
        self.default, self.local = default, threading.local()

    def _target(self):
        return getattr(self.local, "buf", None) or self.default

    def write(self, s):
        return self._target().write(s)

    def flush(self):
        return self._target().flush()

    def __getattr__(self, name):
        return getattr(self.default, name)


class _LiveOutput:
    """execute_python's output buffer. Each write lands at once — nothing is held back
    until the call ends — so manage_call shows the prints of code that is still running.
    No lock: list.append and "".join are atomic under the GIL, and a lock held by the
    Inventor thread could be left locked by manage_call(action='cancel')'s KeyboardInterrupt."""

    def __init__(self):
        self.parts = []

    def write(self, s):
        self.parts.append(s)
        return len(s)

    def flush(self):
        pass

    def isatty(self):
        return False

    def getvalue(self):
        return "".join(self.parts)


# ---------- Inventor instances and their call threads ----------
#
# One server can drive several Inventors at once — Claude Desktop runs a single server
# for all its chats, so two agents on two projects share it. Each connected Inventor is
# an _Instance with a name, its own call thread, execute_python namespace and last call:
# the agents don't wait for each other or overwrite each other's variables.
#
# Every call to an Inventor runs on that Inventor's thread (COM objects stay on the
# thread that made them). The tool waits CALL_TIMEOUT seconds at most, so a call stuck
# behind a modal dialog, or a long job, never freezes the server: the client gets an
# answer before its own timeout, and call_status picks up the result later.
#
# An Inventor the user opened by hand can't be told apart: Windows hands every client
# the first one that registered. So a second Inventor is one the server starts itself
# (connect(new_instance=True)) — it keeps the reference.

CALL_TIMEOUT = float(os.environ.get("INVENTOR_MCP_CALL_TIMEOUT", "50"))
STATUS_WAIT_MAX = min(45.0, CALL_TIMEOUT)  # call_status(wait_seconds) — below client timeouts
DEFAULT = "default"
_local = threading.local()  # .instance and .call on each Inventor thread


class _Call:
    def __init__(self, name):
        self.name, self.future = name, None
        self.started, self.finished = time.monotonic(), None
        self.timed_out = False      # outlived CALL_TIMEOUT: the client never saw the result
        self.collected = False      # call_status has returned the result
        self.cancel = threading.Event()  # cancel_call: cancelled() turns True
        self.cancelled_by = ""      # "cancel_call" or "timeout …"
        self.interrupted = 0        # KeyboardInterrupts raised in it by cancel_call / timeout
        self.progress = None        # (time, text) of execute_python's last progress()
        self.output = None          # execute_python's _LiveOutput

    def submit(self, inst, fn, args, kwargs):
        self.future = inst.thread.submit(_run_call, inst, self, fn, args, kwargs)
        self.future.add_done_callback(lambda _: setattr(self, "finished", time.monotonic()))
        return self


class _Instance:
    def __init__(self, name):
        self.name = name
        self.conn = _api.InventorConnection()
        self.ns: dict = {}              # execute_python's namespace (variables persist)
        self.ns_created = time.time()
        self.last_call: _Call | None = None
        self.running: _Call | None = None    # set by the Inventor thread while a call runs
        self.raising = False            # cancel_call is about to interrupt (see _interrupt)
        self.cancel_lock = threading.Lock()
        self.thread_id = None
        self.project = ""               # active .ipj when last seen
        self.libraries: list[str] = []  # load_library files, rerun in a new namespace
        self.ns_ready = False           # startup file and libraries loaded
        self.lib_status: dict = {}      # library path -> seconds, slowest lines, or failed
        self.lock = threading.Lock()    # two clients' calls arrive on two threads
        self.thread = concurrent.futures.ThreadPoolExecutor(
            max_workers=1, thread_name_prefix=f"inventor-{name}", initializer=self._init_thread)

    def _init_thread(self):
        if _api.WINDOWS:
            pythoncom.CoInitialize()
        _local.instance = self
        self.thread_id = threading.get_ident()

    @property
    def attached(self) -> bool:
        return self.conn._app is not None


_instances: dict[str, _Instance] = {DEFAULT: _Instance(DEFAULT)}


class _CurrentConnection:
    """`inv` in the tools: the connection of the Inventor whose thread runs the call."""

    def __getattr__(self, name):
        return getattr(_local.instance.conn, name)


inv = _CurrentConnection()


class _NoInstance(Exception):
    pass


def _names() -> str:
    return ", ".join(n for n, i in _instances.items() if i.attached) or "none"


def _pick(name: str) -> _Instance:
    if name:
        if name not in _instances:
            raise _NoInstance(f"No Inventor is connected as '{name}' (connected: {_names()}). "
                              "See list_instances, or connect(instance=...) first.")
        return _instances[name]
    attached = [i for i in _instances.values() if i.attached]
    if len(attached) > 1:
        raise _NoInstance(f"Several Inventors are connected ({_names()}) — pass instance=<name> "
                          "to say which one (list_instances shows their projects).")
    return attached[0] if attached else _instances[DEFAULT]


def _dialog_report(inst: _Instance) -> str:
    try:
        dialogs = _api.modal_dialogs(inst.conn.main_hwnd)
    except Exception:
        return ""
    if not dialogs:
        return ""
    lines = [f"Inventor '{inst.name}' is showing a modal dialog. It blocks every API call until "
             "someone answers it in Inventor — ask the user to read it and click the right button:"]
    lines += [f'  [{d["title"]}] {d["text"]}' for d in dialogs]
    return "\n".join(lines)


def _busy_report(inst: _Instance, call: _Call) -> str:
    msg = f"'{call.name}' has been running for {time.monotonic() - call.started:.0f} s."
    if call.cancel.is_set():
        msg += " Cancel was requested — it stops at its next Python line (not inside a COM call)."
    return msg + "\n" + (_dialog_report(inst) or
                         "No dialog is open — Inventor is still working on it (a long operation).")


def _tail(text: str, limit: int = 3000) -> str:
    """The last lines of text, at most limit characters, cut at a line start."""
    text = text.strip()
    if len(text) <= limit:
        return text
    cut = text[-limit:]
    return "…" + (cut[cut.find("\n") + 1:] if "\n" in cut else cut)


def _live_report(call: _Call) -> str:
    """progress() line and the latest output of a running execute_python call."""
    lines = []
    if call.progress:
        t, text = call.progress
        lines.append(f"Progress: {text} ({time.monotonic() - t:.0f} s ago)")
    out = call.output.getvalue().strip() if call.output is not None else ""
    if out:
        lines.append(f"Latest output ({out.count(chr(10)) + 1} lines so far):\n{_tail(out)}")
    return ("\n" + "\n".join(lines)) if lines else ""


def _silent_call(fn, args, kwargs):
    """Runs on the Inventor thread, with SilentOperation on: Inventor answers its own
    prompts (save dependents?, use the resolved file?) with the default instead of
    opening a dialog that waits for a click."""
    app, prev = _local.instance.conn._app, None
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


# ---------- Cancelling a call ----------
#
# cancel_call sets the call's cancel event (execute_python code polls cancelled()) and
# raises KeyboardInterrupt in the Inventor thread with PyThreadState_SetAsyncExc. Python
# delivers it at the thread's next bytecode — never inside a blocking COM call, which has
# to return first. It must never land after the call ended: in the executor's loop it
# would kill the thread and lose the next call. So it is only raised while inst.running
# is the call, and the Inventor thread, on leaving the call, clears inst.running, waits
# for a cancel that is raising right now (inst.raising — the two flags are set and read
# in opposite order, so one side always sees the other) and clears whatever is pending.
# The Inventor thread takes no lock for this: the exception could land while it holds it.


CANCEL_GRACE = 2.0  # s between cancelled() turning True and the KeyboardInterrupt


class _Cancelled(Exception):
    pass


def _set_async_exc(thread_id, exc) -> int:
    return ctypes.pythonapi.PyThreadState_SetAsyncExc(
        ctypes.c_ulong(thread_id), ctypes.py_object(exc) if exc is not None else None)


def _interrupt(inst: _Instance, call: _Call) -> bool:
    """Raise KeyboardInterrupt in the call, if it is still running. Any thread."""
    with inst.cancel_lock:
        inst.raising = True
        try:
            if inst.running is call and inst.thread_id:
                call.interrupted += 1
                return _set_async_exc(inst.thread_id, KeyboardInterrupt) == 1
            return False
        finally:
            inst.raising = False


def _disarm(inst: _Instance):
    """On the Inventor thread when a call ends: no KeyboardInterrupt may follow it."""
    inst.running = None
    while inst.raising:
        time.sleep(0.001)
    _set_async_exc(inst.thread_id, None)


def _cancel(inst: _Instance, call: _Call, by: str = "cancel_call") -> bool:
    call.cancelled_by = call.cancelled_by or by
    call.cancel.set()
    return _interrupt(inst, call)


def _interrupt_until_done(inst: _Instance, call: _Call, tries: int = 8, gap: float = 0.5) -> bool:
    """Raise KeyboardInterrupt again until the call ends. Once is not enough: code that
    catches everything — win32com's own dynamic.py has bare `except:` blocks, and a loop of
    COM calls spends most of its time in them — swallows it. Any thread; blocks."""
    for _ in range(tries):
        if call.future.done():
            return True
        _interrupt(inst, call)
        concurrent.futures.wait([call.future], gap)
    return call.future.done()


def _cancel_soon(inst: _Instance, call: _Call, by: str):
    """cancelled() turns True now; the KeyboardInterrupts follow after CANCEL_GRACE if the
    code hasn't stopped by itself (execute_python's timeout)."""
    call.cancelled_by = call.cancelled_by or by
    call.cancel.set()

    def later():
        if not call.future.done():
            _interrupt_until_done(inst, call)
    t = threading.Timer(CANCEL_GRACE, later)
    t.daemon = True
    t.start()


def _error_notes(e: BaseException) -> str:
    """Readable lines for the COM errors in e's chain (HRESULT names, the inner scode of
    DISP_E_EXCEPTION) and Inventor's own error message. On the Inventor thread."""
    lines, x = [], e
    for _ in range(5):
        if x is None:
            break
        if type(x).__name__ == "com_error":
            lines.append("→ " + _api.describe_com_error(x))
        x = x.__cause__ or x.__context__
    if lines:
        app = getattr(getattr(_local, "instance", None), "conn", None)
        said = _api.inventor_messages(app._app) if app is not None and app._app is not None else ""
        if said:
            lines.append("→ Inventor says: " + said)
    return "\n".join(lines)


def _run_call(inst: _Instance, call: _Call, fn, args, kwargs):
    """The call on the Inventor thread: silent, cancellable, COM errors made readable."""
    try:
        inst.running, _local.call = call, call
        try:
            return _silent_call(fn, args, kwargs)
        finally:
            _disarm(inst)
    except KeyboardInterrupt:
        raise _Cancelled(f"'{call.name}' was cancelled after {time.monotonic() - call.started:.0f} s.") from None
    except Exception as e:
        notes = _error_notes(e)
        if not notes:
            raise
        head = "" if type(e).__name__ == "com_error" else f"{type(e).__name__}: {e}\n"
        raise RuntimeError(head + notes.replace("→ ", "")) from None


def _on_com_thread(inst: _Instance, fn, args, kwargs, wait=None):
    if not _api.WINDOWS:
        return _api.NOT_WINDOWS
    with inst.lock:
        c = inst.last_call
        if c is not None and not c.future.done():
            return (_busy_report(inst, c) + f"\nThis '{fn.__name__}' call was NOT started. "
                    "Use manage_call to follow the running call (manage_call(action='cancel') stops it).")
        blocker = _dialog_report(inst)
        if blocker:
            return blocker + f"\nThis '{fn.__name__}' call was NOT started. Retry once the dialog is closed."
        call = _Call(fn.__name__).submit(inst, fn, args, kwargs)
        inst.last_call = call
    try:
        return call.future.result(timeout=CALL_TIMEOUT if wait is None else wait)
    except concurrent.futures.TimeoutError:
        call.timed_out = True
        return (_busy_report(inst, call) + _live_report(call) + "\nIt keeps running in the background — "
                "manage_call(wait_seconds=30) follows it and gets its result; manage_call(action='cancel') stops it.")


Instance = Annotated[str, Field(
    description="Which connected Inventor to use (list_instances). Leave empty when only one is connected.")]


def tool(com: bool = True):
    """mcp.tool() that runs the function on the chosen Inventor's thread (see above) and
    adds an `instance` parameter to pick it. com=False for tools that must answer while
    that thread is busy.
    The tool is async and waits in a worker thread: the MCP SDK runs sync tools on its
    event loop, so one agent's long call would hold up another agent's Inventor."""
    def deco(fn):
        if not com:
            return mcp.tool()(fn)

        @functools.wraps(fn)
        async def wrapper(*args, instance: str = "", **kwargs):
            try:
                inst = _pick(instance)
            except _NoInstance as e:
                return str(e)
            return await asyncio.to_thread(_on_com_thread, inst, fn, args, kwargs)

        sig = _inspect.signature(fn)
        wrapper.__signature__ = sig.replace(parameters=[
            *sig.parameters.values(),
            _inspect.Parameter("instance", _inspect.Parameter.KEYWORD_ONLY, default="", annotation=Instance)])
        return mcp.tool()(wrapper)
    return deco


def _call_status(inst: _Instance) -> str:
    c = inst.last_call
    if c is None:
        return "No Inventor call has been made yet."
    if not c.future.done():
        return _busy_report(inst, c) + _live_report(c)
    took = f"{c.finished - c.started:.0f} s"
    if not c.timed_out or c.collected:
        return f"Last call '{c.name}' finished ({took}); its result was already returned."
    c.collected = True
    try:
        result = c.future.result()
    except Exception as e:
        result = f"ERROR: {type(e).__name__}: {e}"
    return f"'{c.name}' finished after {took}:\n{result}"


async def call_status(wait_seconds: float = 0, instance: Instance = "") -> str:
    """Follow a call that outlived the wait (a long job, or one stuck behind an Inventor
    dialog): elapsed time, any open dialog's text, execute_python's progress() line and
    latest output — and the call's result once it has finished. Safe to call at any time.

    Args:
        wait_seconds: Wait up to this long (at most 45 s) for the running call to finish
            and return its result at once — instead of calling manage_call again and again.
            Without a name, waits until the first of the running calls finishes.
    """
    if instance or sum(i.attached for i in _instances.values()) <= 1:
        try:
            insts, single = [_pick(instance)], True
        except _NoInstance as e:
            return str(e)
    else:
        insts, single = [i for i in _instances.values() if i.attached], False
    wait = min(max(float(wait_seconds or 0), 0.0), STATUS_WAIT_MAX)
    running = [i.last_call.future for i in insts if i.last_call is not None and not i.last_call.future.done()]
    if wait and running:  # in a worker thread: the event loop keeps serving other agents
        await asyncio.to_thread(concurrent.futures.wait, running, wait,
                                concurrent.futures.FIRST_COMPLETED)
    if single:
        return _call_status(insts[0])
    return "\n\n".join(f"[{i.name}] {_call_status(i)}" for i in insts)


async def cancel_call(instance: Instance = "") -> str:
    """Stop the running call — a long execute_python loop, a Content Center scan, a job
    that turned out wrong. cancelled() in execute_python code turns True at once (poll it
    in long loops to stop cleanly); code that hasn't stopped by itself after CANCEL_GRACE
    seconds gets a KeyboardInterrupt in the Inventor thread. That takes effect at the next
    Python line: a call stuck INSIDE one COM call (a long Inventor operation, or a modal
    dialog) stops only once that call returns."""
    try:
        inst = _pick(instance)
    except _NoInstance as e:
        return str(e)
    c = inst.last_call
    if c is None or c.future.done():
        return f"Nothing is running on Inventor '{inst.name}'."
    c.cancelled_by = c.cancelled_by or "cancel_call"
    c.cancel.set()  # code polling cancelled() stops cleanly within the grace period
    await asyncio.to_thread(concurrent.futures.wait, [c.future], CANCEL_GRACE)
    if not c.future.done():
        await asyncio.to_thread(_interrupt_until_done, inst, c)
    if not c.future.done():
        return (_busy_report(inst, c) + "\nIt is inside a COM call (or behind a dialog) and stops once "
                "that returns — follow it with manage_call(wait_seconds=30).")
    if c.timed_out:
        return _call_status(inst)
    return f"'{c.name}' stopped after {c.finished - c.started:.0f} s; the waiting call got its result."


# ---------- Power tools (token savers) ----------

# Python run by execute_python / load_library. Each run gets its own file name, registered
# with linecache, so a traceback shows the agent's own lines — also for a helper defined in
# an earlier call that fails later.
STARTUP = os.environ.get("INVENTOR_MCP_STARTUP") or os.path.join(
    os.path.expanduser("~"), ".inventor-mcp", "startup.py")
_run_count = 0


def _capture(buf):
    """Send this thread's stdout AND stderr (traceback.print_exc) to buf."""
    for stream in ("stdout", "stderr"):
        if not isinstance(getattr(sys, stream), _ThreadStdout):
            setattr(sys, stream, _ThreadStdout(getattr(sys, stream)))
        getattr(sys, stream).local.buf = buf


def _release():
    sys.stdout.local.buf = sys.stderr.local.buf = None


def _run_source(me, source: str, filename: str | None = None):
    global _run_count
    if filename is None:
        _run_count += 1
        filename = f"<mcp-{_run_count}>"
    linecache.cache[filename] = (len(source), None, source.splitlines(True), filename)
    exec(compile(source, filename, "exec"), me.ns)


LIBRARIES = os.path.join(os.path.expanduser("~"), ".inventor-mcp", "libraries.json")


def _saved_libraries(project: str) -> list[str]:
    """load_library files remembered for a project, so a new server loads them too."""
    try:
        with open(LIBRARIES, encoding="utf-8") as f:
            return [p for p in json.load(f).get(project.lower(), []) if os.path.isfile(p)]
    except (OSError, ValueError):
        return []


def _remember_library(project: str, path: str):
    try:
        with open(LIBRARIES, encoding="utf-8") as f:
            known = json.load(f)
    except (OSError, ValueError):
        known = {}
    paths = known.setdefault(project.lower(), [])
    if path not in paths:
        paths.append(path)
        os.makedirs(os.path.dirname(LIBRARIES), exist_ok=True)
        with open(LIBRARIES, "w", encoding="utf-8") as f:
            json.dump(known, f, indent=1)


def _run_library(me, path: str) -> dict:
    """Run a library file statement by statement, timing each: where its load time goes."""
    with open(path, encoding="utf-8") as f:
        source = f.read()
    linecache.cache[path] = (len(source), None, source.splitlines(True), path)
    lines = source.splitlines()
    t0 = time.monotonic()
    times = []
    for node in ast.parse(source, path).body:
        t = time.monotonic()
        exec(compile(ast.Module([node], []), path, "exec"), me.ns)
        times.append((time.monotonic() - t, node.lineno))
    me.lib_status[path] = {"seconds": round(time.monotonic() - t0, 1),
                           "slowest": [f"line {n} ({s:.1f} s): {lines[n - 1].strip()[:60]}"
                                       for s, n in sorted(times, reverse=True)[:3] if s >= 0.5]}
    return me.lib_status[path]


def _user_traceback(e: BaseException) -> str:
    """The traceback from the agent's own code down — not the server's frames above it."""
    tb = e.__traceback__
    t = tb
    while t is not None and not _is_user_file(t.tb_frame.f_code.co_filename):
        t = t.tb_next
    notes = _error_notes(e)
    return "".join(traceback.format_exception(type(e), e, t or tb)) + (notes + "\n" if notes else "")


def _user_frames(e: BaseException):
    """The traceback entries of the agent's own code."""
    return [f for f in traceback.extract_tb(e.__traceback__) if _is_user_file(f.filename)]


def _is_user_file(name: str) -> bool:
    return name.startswith("<mcp") or name in _local.instance.libraries or name == STARTUP


def _load_libraries(me) -> str:
    """Run the startup file and load_library files into a fresh namespace."""
    if me.ns_ready:
        return ""
    me.ns_ready = True
    notes = []
    me.libraries += [p for p in _saved_libraries(me.project) if p not in me.libraries]
    for path in ([STARTUP] if os.path.isfile(STARTUP) else []) + me.libraries:
        try:
            done = _run_library(me, path)
            notes.append(f"(loaded {os.path.basename(path)} in {done['seconds']} s)")
        except Exception as e:
            me.lib_status[path] = {"failed": str(e)[:200]}
            notes.append(f"(loading {path} failed:\n{_user_traceback(e)})")
    return "\n".join(notes)


def _progress(msg) -> None:
    """execute_python: a short status line manage_call shows while the code runs."""
    call = getattr(_local, "call", None)
    if call is not None:
        call.progress = (time.monotonic(), str(msg)[:200])


def _cancelled() -> bool:
    """execute_python: True once manage_call(action='cancel') (or the timeout) asked this call to stop."""
    call = getattr(_local, "call", None)
    return call is not None and call.cancel.is_set()


def _bind(me) -> str | None:
    """Fresh inv/app/doc/comp/body/tg in the namespace. Returns an error to show, if any."""
    ctx = {"inv": me.conn, "math": math, "progress": _progress, "cancelled": _cancelled}
    try:
        # only the default reconnects by itself: a named Inventor that was closed must not
        # silently become whichever Inventor Windows hands out first
        if not inv.is_connected():
            if me.name != DEFAULT:
                return (f"Inventor '{me.name}' is closed. Start it again with "
                        f"connect(new_instance=True, instance='{me.name}', project=...).")
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
    me.ns.update(ctx)
    return None


@tool()
def execute_python(code: str, undo_on_error: bool = False, timeout: float = 0) -> str:
    """Execute Python code against the live Inventor connection.

    Pre-bound objects (re-resolved fresh on every call):
      inv  — InventorConnection wrapper (all high-level methods, mm units)
      app  — Inventor.Application COM object
      doc  — active document          comp — its ComponentDefinition
      body — comp.SurfaceBodies.Item(1) (or None)
      tg   — TransientGeometry        math — math module
      progress(msg) — status line manage_call shows while a long job runs
      cancelled()   — True once manage_call(action='cancel') asked to stop: poll it in long loops
    Variables you assign persist between calls until the server restarts or reload_api
    runs — get_status shows the namespace's age and names. Each connected Inventor has its
    own namespace. Helpers you need every time: put them in a file and load_library it
    (or in ~/.inventor-mcp/startup.py) — they come back by themselves in a new namespace.
    Internal COM units are CENTIMETERS — divide mm by 10.
    Objects are late-bound: a Document from Documents.Open exposes ComponentDefinition
    directly. Don't use CastTo / gencache.EnsureDispatch (they create a gen_py cache).
    Runs with SilentOperation on, so Inventor answers its own prompts instead of opening
    a dialog. Save with the save_document tool or inv.save_document(): a plain doc.SaveAs
    also writes every dirty dependent, protected and Content Center files included.
    A call longer than the wait limit keeps running; follow it with
    manage_call(wait_seconds=30), which shows the prints so far; manage_call(action='cancel') stops it.
    Returns captured stdout + stderr (print your results!), or the traceback from your
    own code down on error (COM errors named: E_INVALIDARG, RPC_E_DISCONNECTED …).

    Args:
        code: Python source.
        undo_on_error: Run inside one undo transaction on the active document and roll
            it back if the code raises — no half-built sketches or work planes left behind.
        timeout: Seconds; then cancelled() turns True, and the run is interrupted
            CANCEL_GRACE s later if it hasn't stopped — as manage_call(action='cancel') does (0 = none).
    """
    me, call = _local.instance, _local.call
    error = _bind(me)
    if error:
        return error
    buf = call.output = _LiveOutput()
    _capture(buf)
    txn = timer = None
    if timeout and timeout > 0:
        timer = threading.Timer(timeout, _cancel_soon, (me, call, f"timeout {timeout:g} s"))
        timer.daemon = True
        timer.start()
    try:
        notes = _load_libraries(me)
        if notes:
            print(notes)
        if undo_on_error and me.ns.get("doc") is not None:
            txn = inv.app.TransactionManager.StartTransaction(me.ns["doc"], "MCP execute_python")
        try:
            try:
                inv._silently(lambda: _run_source(me, code))
            finally:
                _disarm(me)  # a cancel arriving now must not hit the code below
        except (Exception, KeyboardInterrupt) as e:
            if txn is not None:
                txn.Abort()
                txn = None
                print("(undo_on_error: every change of this call was rolled back)", file=sys.stderr)
            partial = buf.getvalue().strip()
            if isinstance(e, KeyboardInterrupt):
                return (partial + "\n" if partial else "") + (
                    f"CANCELLED ({call.cancelled_by or 'interrupted'}) after {time.monotonic() - call.started:.0f} s at:\n"
                    + "".join(traceback.format_list(_user_frames(e))[-3:]))
            return (partial + "\n" if partial else "") + "ERROR:\n" + _user_traceback(e)
        if txn is not None:
            txn.End()
        out = buf.getvalue().strip()
        if len(out) > 6000:
            out = out[:6000] + "\n... (truncated at 6000 chars)"
        if call.cancel.is_set():
            out += f"\n(stopped early: {call.cancelled_by} asked it to, cancelled() was True)"
        return out.strip() if out.strip() else "(ok, no output)"
    finally:
        if timer is not None:
            timer.cancel()
        _release()


@tool()
def load_library(path: str) -> str:
    """Run a Python file into this Inventor's execute_python namespace and keep it there:
    after reload_api, a new namespace or a new server it is loaded again by itself (remembered
    per project). For helper functions you would otherwise redefine every time. Says how long
    it took and which lines were slowest — a COM call at module level is the usual culprit.
    A file at ~/.inventor-mcp/startup.py (or INVENTOR_MCP_STARTUP) is loaded for every Inventor."""
    me = _local.instance
    path = os.path.abspath(path)
    if not os.path.isfile(path):
        return f"File not found: {path}"
    error = _bind(me)
    if error:
        return error
    before = set(me.ns)
    buf = _local.call.output = _LiveOutput()
    _capture(buf)
    try:
        _load_libraries(me)
        if path not in me.libraries:
            me.libraries.append(path)
        try:
            done = inv._silently(lambda: _run_library(me, path))
        except Exception as e:
            me.libraries.remove(path)
            return buf.getvalue() + "ERROR:\n" + _user_traceback(e)
    finally:
        _release()
    _remember_library(me.project, path)
    new = sorted(k for k in set(me.ns) - before if not k.startswith("__"))
    out = buf.getvalue().strip()
    return ((out + "\n" if out else "") + f"Loaded {os.path.basename(path)} in {done['seconds']} s: "
            + (", ".join(new[:40]) or "no new names")
            + ("\nSlowest: " + "; ".join(done["slowest"]) if done["slowest"] else ""))


@tool()
def reload_api() -> str:
    """Hot-reload inventor_api.py from disk. Use after editing the wrapper —
    no Claude Code restart needed. Keeps the COM connections; clears this Inventor's
    execute_python namespace."""
    import importlib
    importlib.reload(_api)
    for i in _instances.values():
        old, i.conn = i.conn, _api.InventorConnection()
        i.conn._app, i.conn.main_hwnd = old._app, old.main_hwnd
    me = _local.instance
    me.ns.clear()
    me.ns_created = time.time()
    me.ns_ready = False  # startup file and load_library files run again
    return "inventor_api.py reloaded (connections preserved)"


@tool()
def capture_view(views: str = "iso", width: int = 800, height: int = 600,
                 background: str = "inventor", fit_to: str = "", isolate: str = "",
                 sketches: bool = False, up: str = "y"):
    """Look at the model: pictures of the active document, returned as images you can see.
    Use it to check your work — a flange on the wrong side, a door opening the wrong way,
    a part floating off the frame — which the numbers alone don't show.

    Args:
        views: Comma-separated: iso, iso_left, iso_bottom, front, back, top, bottom, left,
               right, current (the user's view). E.g. "iso,front,top".
        width, height: Pixels per picture (keep them modest — each picture costs tokens).
        background: "inventor" (its gradient) or "white".
        fit_to: Assembly component paths, ","-separated — zoom to them, keeping each view's direction.
        isolate: Component paths, ","-separated — show only these (and what is inside them);
                 the others are hidden for the picture. For details buried in an assembly.
        sketches: Show sketches and work features too (hidden by default; restored after).
        up: "y" = Inventor's own view names (its default templates have Y up); "z" for a
            model built with Z up (frames, buildings): front looks along +Y, top down -Z.
    """
    names = [v.strip().lower() for v in views.split(",") if v.strip()]
    paths = inv.view_images(names, width, height, background.lower() == "white", fit_to, sketches, up, isolate)
    images = []
    for path in paths:
        with open(path, "rb") as f:
            images.append(Image(data=f.read(), format="png"))
        os.remove(path)
    os.rmdir(os.path.dirname(paths[0]))
    return images


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

@tool(com=False)
async def connect(visible: bool = True, project: str = "", new_instance: bool = False, pid: int = 0,
            instance: Annotated[str, Field(description=(
                "Name for this Inventor — pass the same name to later calls. Default: "
                "'default', or the project's name when the server starts one for it."))] = "") -> str:
    """Connect to Autodesk Inventor. Starts Inventor if none is running.

    Working on a project? Pass project=<full path of its .ipj>: the server uses the
    Inventor that already has it — also one a previous server started (after a restart of
    Claude Desktop) — or the running one if it is free to switch, or else starts a separate
    Inventor for it — so two agents on two projects each get their own.
    The answer names the instance; when several Inventors are connected, pass
    instance=<that name> to every later call.

    Args:
        visible: Show Inventor window (default True).
        project: Full path of an .ipj to work in.
        new_instance: Always start a separate Inventor.
        pid: Connect to this kept Inventor (list_instances shows them) — e.g. two in one project.
    """
    if not _api.WINDOWS:
        return _api.NOT_WINDOWS
    if project:
        project = os.path.abspath(project)
        if not os.path.isfile(project):
            return f"Project file not found: {project}"
    by_project = bool(project) and not new_instance and not instance and not pid
    attach_pid = pid
    if pid:
        if any(i.attached and i.conn.pid == pid for i in _instances.values()):
            return f"Inventor pid {pid} is already connected — see list_instances."
        name = instance or f"pid {pid}"
    elif by_project:
        hit = next((i for i in _instances.values() if i.attached and _same_file(i.project, project)), None)
        kept = None
        if hit is None:  # one a previous server started, still in this project?
            connected = {i.conn.pid for i in _instances.values() if i.attached}
            kept = next((k for k in await asyncio.to_thread(_api.kept_inventors)
                         if k["pid"] not in connected and _same_file(k.get("project", ""), project)), None)
        if hit is not None:
            name = hit.name
        elif kept is not None:
            name, attach_pid = _free_name(project), kept["pid"]
        elif not _instances[DEFAULT].attached:
            name = DEFAULT   # the running Inventor, if it can take the project
        else:
            name, new_instance = _free_name(project), True
    elif new_instance:
        name = instance or (_free_name(project) if project else f"inventor{len(_instances) + 1}")
        if name in _instances and _instances[name].attached:
            return f"An Inventor is already connected as '{name}' — pick another instance name."
    else:
        name = instance or DEFAULT
    inst = _instances.setdefault(name, _Instance(name))

    def connect_():  # on that Inventor's thread
        me = _local.instance
        conn = me.conn
        if attach_pid:
            msg = conn.attach(attach_pid)
        elif new_instance or not conn.is_connected():
            msg = conn.connect(visible, new_instance=new_instance)
            other = next((i.name for i in _instances.values()
                          if i is not inst and i.attached and i.conn.pid == conn.pid), None)
            if other:
                pid, conn._app, conn.main_hwnd = conn.pid, None, None
                return (f"That Inventor (pid {pid}) is already connected as '{other}' — use "
                        f"instance='{other}', or connect(new_instance=True) to start a separate one.")
        else:
            msg = "Already connected"
        if project:
            busy = conn.app.Documents.Count and not _same_file(conn.project_info()["path"], project)
            if by_project and busy:
                # someone works in another project here — leave it alone, start our own
                conn._app = conn.main_hwnd = None
                return _BUSY_WITH_OTHER_PROJECT
            msg += "\n" + conn.activate_project(project)
        me.project = conn.project_info()["path"]
        msg += f"\nInventor '{me.name}': pid {conn.pid}, project {conn.project_info()['name']}."
        if sum(i.attached for i in _instances.values()) > 1:
            msg += (f"\nSeveral Inventors are connected ({_names()}) — pass instance='{me.name}' "
                    "to every call for this one.")
        return msg

    connect_.__name__ = "connect"
    out = await asyncio.to_thread(_on_com_thread, inst, connect_, (), {})
    if out is _BUSY_WITH_OTHER_PROJECT:
        new_instance, by_project = True, False
        inst = _instances.setdefault(_free_name(project), _Instance(_free_name(project)))
        out = await asyncio.to_thread(_on_com_thread, inst, connect_, (), {})
    return out


_BUSY_WITH_OTHER_PROJECT = object()


def _same_file(a: str, b: str) -> bool:
    return bool(a and b) and os.path.normcase(os.path.abspath(a)) == os.path.normcase(os.path.abspath(b))


def _free_name(project: str) -> str:
    """An instance name for a project's own Inventor: the .ipj's name, numbered if taken."""
    base = os.path.splitext(os.path.basename(project))[0]
    name, n = base, 1
    while name in _instances and _instances[name].attached:
        n += 1
        name = f"{base} {n}"
    return name


def _describe():  # on an Inventor's thread
    me = _local.instance
    if not me.conn.is_connected():
        return "closed (its Inventor is gone — connect again)"
    app, proj = me.conn.app, me.conn.project_info()
    me.project = proj["path"]
    doc = app.ActiveDocument
    return (f"pid {me.conn.pid}, project {proj['name']} ({proj['path']}), "
            f"{app.Documents.VisibleDocuments.Count} open, active: {doc.DisplayName if doc else '-'}")


@tool(com=False)
async def list_instances() -> str:
    """The Inventors this server is connected to — name, project, open documents — and any
    other running Inventor it can't reach (opened by hand). Answers even while they are busy."""
    if not _api.WINDOWS:
        return _api.NOT_WINDOWS
    return await asyncio.to_thread(_list_instances)


def _list_instances() -> str:
    lines = []
    for i in _instances.values():
        if not i.attached:
            continue
        c = i.last_call
        if c is not None and not c.future.done():
            lines.append(f"{i.name}: busy — {_busy_report(i, c)}")
        elif _dialog_report(i):
            lines.append(f"{i.name}: {_dialog_report(i)}")
        else:
            try:
                lines.append(f"{i.name}: {i.thread.submit(_describe).result(timeout=5)}")
            except Exception as e:
                lines.append(f"{i.name}: no answer ({type(e).__name__})")
    reached = {i.conn.pid for i in _instances.values() if i.attached}
    for k in _api.kept_inventors():
        if k["pid"] not in reached:
            reached.add(k["pid"])
            what = (f"project {k['project_name']}, {k['documents']} open" if "project" in k
                    else "busy, didn't answer")
            lines.append(f"(pid {k['pid']}: {what} — started by an earlier server and kept; "
                         f"connect(project=...) or connect(pid={k['pid']}) picks it up.)")
    for pid in _api.inventor_pids():
        if pid not in reached:
            lines.append(f"(pid {pid}: not connected — opened by hand, or by a server that is gone. "
                         "connect() reaches it only if it is the first Inventor that started; "
                         "otherwise save its work and close it, and use connect(new_instance=True).)")
    return "\n".join(lines) or "No Inventor is running. Call connect()."


@tool()
def get_status() -> str:
    """Check connection status, active document info, and the execute_python
    namespace (age, names you defined — gone after a server restart or reload_api).
    If Inventor is busy or showing a dialog, says so instead."""
    me = _local.instance
    if not inv.is_connected():
        return f"Inventor '{me.name}' is not connected. Call connect() first."
    try:
        info = inv.get_document_info()
    except RuntimeError:
        info = {"document": None}
    info["inventor"] = {"instance": me.name, "pid": inv.pid, "project": inv.project_info()["name"]}
    defined = sorted(k for k in me.ns
                     if not k.startswith("__") and k not in ("inv", "app", "doc", "comp", "body", "tg", "math",
                                                             "progress", "cancelled"))
    info["python_namespace"] = {
        "created": time.strftime("%Y-%m-%d %H:%M", time.localtime(me.ns_created)),
        "names": defined,
        "libraries": {os.path.basename(p): me.lib_status.get(p, "not loaded yet — the next execute_python loads it")
                      for p in ([STARTUP] if os.path.isfile(STARTUP) else [])
                      + me.libraries + [q for q in _saved_libraries(me.project) if q not in me.libraries]},
    }
    unsaved = inv.unsaved_documents()
    if unsaved:
        info["never_saved_documents"] = unsaved + ["(manage_documents closes the ones nothing references)"]
    return json.dumps(info, indent=2, ensure_ascii=False)


# ---------- Finding geometry ----------

@tool()
def find_faces(path: str = "", surface: str = "", normal: list[float] | None = None,
               at: dict | None = None, diameter: float = 0, min_area: float = 0,
               max_count: int = 30) -> str:
    """Find faces, filtered by type, direction, position or size. Without `path`: the active
    part (the `index` feeds create_sketch, sheet_metal_cut). With `path`: a component of the
    active assembly at any depth, in TOP assembly coordinates, with a `ref` ("Sub:1/Part:1#F12")
    for constrain; a sub-assembly path searches every part in it.

    Args:
        path: Component path (manage_occurrences list, depth=2). Empty = the active part.
        surface: "plane", "cylinder", "cone", … (empty = any).
        normal: Outward normal of a plane, e.g. [0, 0, 1] for faces looking up.
        at: Face center coordinates to match, e.g. {"Z": 182.5}.
        diameter: Cylinder diameter in mm.
        min_area: Skip faces smaller than this (mm²).
        max_count: Most results to return.
    """
    return json.dumps(inv.find_faces(path, surface, normal, at, diameter, min_area, max_count),
                      indent=1, ensure_ascii=False)


@tool()
def find_edges(path: str = "", at: dict | None = None, circular: bool = False, diameter: float = 0,
               min_length: float = 0, max_count: int = 40) -> str:
    """Find edges, filtered by position or type. Without `path`: the active part (the `index`
    feeds add_flange, fillet, chamfer). With `path`: a component at any depth, with a `ref`
    ("Sub:1/Bolt:1#E4") — circular ones feed constrain(kind="insert").

    Args:
        path: Component path (manage_occurrences list, depth=2). Empty = the active part.
        at: Edge midpoint coordinates to match (circle: its center), e.g. {"X": 50, "Z": 1.5}.
        circular: Only circular edges.
        diameter: Circle diameter in mm.
        min_length: Skip edges shorter than this (mm).
        max_count: Most results to return.
    """
    return json.dumps(inv.find_edges(path, at, circular, diameter, min_length, max_count),
                      indent=1, ensure_ascii=False)


@tool()
def inspect_model() -> str:
    """Snapshot of the active document in one call. Part: volume, bounding box, face and edge
    counts, feature tree, sketches, sheet metal thickness, warnings (faceted curves, surface
    bodies). Assembly: components with positions, constraint count, mass, volume."""
    return json.dumps(inv.inspect_model(), indent=1, ensure_ascii=False)


# ---------- Managing what is in a document ----------

@tool()
def manage_features(action: str = "list", indices: str = "") -> str:
    """List, delete or suppress the active part's features.

    Args:
        action: "list", "delete", "suppress" or "unsuppress".
        indices: Feature indices from list, comma-separated, e.g. "3,4,5".
    """
    a = action.lower()
    if a == "list":
        return json.dumps(inv.list_features(), indent=2, ensure_ascii=False)
    idx = [int(i) for i in indices.replace(" ", "").split(",") if i]
    if not idx:
        return f"{a} needs indices — see action='list'."
    if a == "delete":
        return inv.delete_feature(idx)
    if a in ("suppress", "unsuppress"):
        return inv.suppress_feature(idx, a == "suppress")
    return f"Unknown action '{action}': list, delete, suppress or unsuppress."


@tool()
def manage_parameters(action: str = "get", name: str = "", expression: str = "", units: str = "mm") -> str:
    """Read, change or add parameters of the active document.

    Args:
        action: "get" (all), "set" or "add".
        name: Parameter name, e.g. "width".
        expression: Value or expression, e.g. "50 mm", "width * 2".
        units: For "add": mm, cm, m, deg, …
    """
    a = action.lower()
    if a == "get":
        return json.dumps(inv.get_parameters(), indent=2, ensure_ascii=False)
    if a == "set":
        return inv.set_parameter(name, expression)
    if a == "add":
        return inv.add_parameter(name, expression, units)
    return f"Unknown action '{action}': get, set or add."


@tool()
def manage_sheet_metal_rules(action: str = "list", name: str = "", thickness: float = 0) -> str:
    """Sheet metal rules of the active part. Shops encode their tooling in rule names (e.g.
    'T15-V10' = 1.5 mm sheet, V10 die): prefer "set" — it sets thickness, bend radius and unfold
    method together; "thickness" changes only the thickness.

    Args:
        action: "list", "set" or "thickness".
        name: Rule name for "set" (case-insensitive).
        thickness: mm, for "thickness".
    """
    a = action.lower()
    if a == "list":
        return json.dumps(inv.list_sheet_metal_rules(), indent=1, ensure_ascii=False)
    if a == "set":
        return inv.set_sheet_metal_rule(name)
    if a == "thickness":
        return inv.set_sheet_metal_thickness(thickness)
    return f"Unknown action '{action}': list, set or thickness."


@tool()
def manage_materials(action: str = "report", material: str = "", search: str = "", parts: str = "*",
                     only_generic: bool = False, path: str = "", only_problems: bool = False) -> str:
    """Materials of many parts at once.

    "report": material and mass of every part of the active assembly (or part): counts per
    material, total mass, and what needs a look — parts left on Generic, vendor models made of
    surfaces (no volume, mass too low), parts with no mass.
    "list": material names in Inventor's libraries (`search` narrows them).
    "set": give `material` to every part whose name matches `parts`. Content Center and library
    parts are left alone. Save afterwards.

    Args:
        action: "report", "list" or "set".
        material: From the library, e.g. "Steel, Mild".
        search: For "list", e.g. "steel".
        parts: Name patterns, ","-separated, e.g. "*PLATE*,*BRACKET*".
        only_generic: "set" only parts still on Generic / Default.
        path: Limit "report" / "set" to a sub-assembly.
        only_problems: "report" without the per-part rows.
    """
    a = action.lower()
    if a == "report":
        return json.dumps(inv.material_report(path, only_problems), indent=1, ensure_ascii=False)
    if a == "list":
        names = inv.list_materials(search)
        return f"{len(names)} material(s): " + ", ".join(names[:150]) + (" …" if len(names) > 150 else "")
    if a == "set":
        return inv.set_material(material, parts, only_generic, path)
    return f"Unknown action '{action}': report, list or set."


@tool()
def manage_constraints(action: str = "list", index: int = 0, value: float | None = None,
                       undo_if_moved: bool = False) -> str:
    """List the active assembly's constraints with their numbers and offsets, flagging any in an
    error state; change one's offset or angle; or delete one. To add one use constrain. Editing
    reports every other component it dragged along. Deleting frees the components it held, and
    the numbers after it shift — list again before deleting another.

    Args:
        action: "list", "edit" or "delete".
        index: For "edit" and "delete": the constraint's number from list.
        value: For "edit": the new offset in mm, or angle in degrees. A formula it held is replaced.
        undo_if_moved: For "edit": put the old value back if any other component would move.
    """
    if action.lower() == "list":
        return inv.list_constraints()
    if action.lower() == "edit":
        if value is None:
            return "edit needs value: the new offset in mm, or angle in degrees."
        return inv.edit_constraint(index, value, undo_if_moved)
    if action.lower() == "delete":
        return inv.delete_constraint(index)
    return f"Unknown action '{action}': list, edit or delete."


@tool()
def manage_occurrences(action: str = "list", path: str = "", depth: int = 1,
                       definition: str = "", axis: list[float] | None = None,
                       near: list[float] | None = None, within: float = 0,
                       grounded: bool = True, cascade: bool = False, force: bool = False) -> str:
    """Components of the active assembly.

    "list": a table with positions; depth=2 also lists what is inside sub-assemblies, with paths.
    "find": components by what they are and where — definition name pattern, the direction of
    their Z axis, distance from a point. E.g. definition="*ISO 4017*M8*", axis=[0,0,1].
    "ground": fix a component in space (grounded=False releases it). Ground the first one.
    "delete": says first what depends on it (patterns, constraints) and stops; force=True
    deletes, cascade=True also deletes pattern members. Reports components that moved.

    Args:
        action: "list", "find", "ground" or "delete".
        path: Component name, path ("Sub:1/Part:1") or list index.
        depth: For "list".
        definition: For "find": name pattern on the part / file name.
        axis: For "find": direction of the component's Z axis, either way along it.
        near: For "find": a point [x, y, z] in mm.
        within: For "find": largest distance from `near`, mm.
        grounded: For "ground".
        cascade: For "delete".
        force: For "delete".
    """
    a = action.lower()
    if a == "list":
        return inv.list_occurrences(depth)
    if a == "find":
        return json.dumps(inv.find_occurrences(definition, axis, near, within), indent=1, ensure_ascii=False)
    if a == "ground":
        return inv.ground_occurrence(path, grounded)
    if a == "delete":
        return inv.delete_occurrence(path, cascade, force)
    return f"Unknown action '{action}': list, find, ground or delete."


@tool()
def manage_documents(action: str = "list", name_or_path: str = "", include_active: bool = False,
                     discard: bool = False, quit_inventor: bool = False) -> str:
    """Open documents of this Inventor.

    "list": name, type, dirty, active; protected (library / Content Center) ones marked.
    "open": a file by path. "activate": switch by name or path. "close": one, without saving
    unless it is yours and dirty (discard=True closes it anyway).
    "close_unsaved": documents never saved (Part12… left by failed attempts).
    "close_all": everything; refuses while your own files have unsaved changes, listing them
    (discard=True closes anyway; protected files never count). quit_inventor=True quits after.

    Args:
        action: "list", "open", "activate", "close", "close_unsaved" or "close_all".
        name_or_path: For open / activate / close.
        include_active: "close_unsaved" also closes the active document.
        discard: Close without saving your changes.
        quit_inventor: "close_all" then quits this Inventor.
    """
    a = action.lower()
    if a == "list":
        return json.dumps(inv.list_documents(), indent=1, ensure_ascii=False)
    if a == "open":
        return inv.open_document(name_or_path)
    if a == "activate":
        return inv.activate_document(name_or_path)
    if a == "close":
        return inv.close_document(name_or_path, discard)
    if a == "close_unsaved":
        return inv.close_unsaved(include_active)
    if a == "close_all":
        return inv.close_all(discard, quit_inventor)
    return f"Unknown action '{action}': list, open, activate, close, close_unsaved or close_all."


# ---------- Creating ----------

@tool()
def create_document(kind: str = "part", name: str = "", template: str = "metric", material: str = "",
                    part_number: str = "", description: str = "") -> str:
    """Create a new document — a part with its material and part number right away, so no part
    is left "Generic" for the BOM and the mass.

    Args:
        kind: "part", "sheet_metal" or "assembly".
        name: Display name (optional).
        template: "metric" (mm, default), "english" (inches), or a full path to a template file.
        material: Parts: from the library, e.g. "Steel, Mild".
        part_number, description: Parts: iProperties for the BOM.
    """
    k = kind.lower().replace("-", "_").replace(" ", "_")
    if k == "assembly":
        return inv.create_assembly(name or None, template)
    if k not in ("part", "sheet_metal"):
        return f"Unknown kind '{kind}': part, sheet_metal or assembly."
    return inv.create_part(name or None, "sheet_metal" if k == "sheet_metal" else template,
                           material, part_number, description)


@tool()
def add_hole(diameter: float = 10, depth: float = 0, tapped: bool = False, pitch: float = 0,
             x: float = 0, y: float = 0, point: list[float] | None = None,
             direction: list[float] | None = None, linear: dict | None = None,
             cbore_diameter: float = 0, cbore_depth: float = 0) -> str:
    """Add a native hole feature to the active part (drilled, tapped ISO metric 6H, counterbored).
    Fails — instead of leaving an empty feature — if no material was removed. Placed one of three ways:

    - `point` + `direction`: absolute model coordinates, e.g. point=[30,0,12], direction=[0,0,-1]
      (the way the drill travels). Safest: no sketch axes involved.
    - `linear`: dimensioned from two edges of a face, associative — preferred for fastener holes:
      {"face_axis": "Z", "face_value": 20, "ref1_axis": "X", "ref1_value": 0, "dist1": 10,
       "ref2_axis": "Y", "ref2_value": 0, "dist2": 15}.
    - `x`, `y`: on the XY plane, drilling into +Z.

    Args:
        diameter: mm (thread nominal if tapped, e.g. 5 for M5).
        depth: mm; 0 = through all.
        tapped: Thread it.
        pitch: mm; 0 = standard coarse.
        cbore_diameter, cbore_depth: Counterbore seat (with linear), e.g. 10 and 5.5 for M5.
    """
    p = pitch if pitch > 0 else None
    if point is not None:
        if direction is None:
            return "point needs a direction, e.g. direction=[0, 0, -1] (the way the drill travels)."
        return inv.hole_along(point, direction, diameter, depth, tapped, p)
    if linear:
        return inv.hole_linear(linear["face_axis"], linear["face_value"], linear["ref1_axis"],
                               linear["ref1_value"], linear["dist1"], linear["ref2_axis"],
                               linear["ref2_value"], linear["dist2"], diameter, depth, tapped, p,
                               cbore_diameter, cbore_depth)
    return inv.hole(x, y, diameter, depth, tapped, p)


@tool()
def sheet_metal_cut(sketch_index: int = 0, face_index: int = 0, circles: list | None = None,
                    rectangles: list | None = None) -> str:
    """Cut through a sheet metal panel — depth follows the thickness, so it stays parametric.
    Checks that material was removed (a cut sketched on a work plane silently removes nothing).

    From a sketch on the panel face: one sketch with several closed shapes = several holes.
    Or at WORLD coordinates, with no sketch axes to think about: face_index + circles / rectangles.

    Args:
        sketch_index: Sketch number (0 = latest).
        face_index: Face of the sheet (find_faces), for circles / rectangles.
        circles: [[x, y, z, diameter], ...], e.g. [[180, 267, 0, 4.9]].
        rectangles: Lists of 4 corners, [[[x,y,z], [x,y,z], [x,y,z], [x,y,z]], ...].
    """
    if face_index:
        return inv.cut_on_face(face_index, circles or [], rectangles or [])
    return inv.sheet_metal_cut(sketch_index if sketch_index > 0 else None)


@tool()
def add_assembly_cut(axis: str = "z", circles: list | None = None, rectangles: list | None = None,
                     at: float = 0, distance: float = 0, direction: str = "symmetric",
                     participants: str = "") -> str:
    """Cut through placed components at assembly level, for a hole drilled after assembly: the
    cut lives in this assembly and the part files stay as they are. To change the part itself
    for every use, cut it in the part (extrude, sheet_metal_cut). Returns the volume removed from
    each component; a cut that removes nothing is taken back and reported as an error.

    Args:
        axis: World direction of the cut: "x", "y" or "z".
        circles: [[x, y, z, diameter], ...] in world mm. The coordinate along axis is ignored.
        rectangles: Lists of 4 corners, [[[x,y,z], [x,y,z], [x,y,z], [x,y,z]], ...], in world mm.
        at: Where the sketch plane sits along axis, in mm. Matters when distance is set.
        distance: Depth in mm from that plane; 0 = through everything.
        direction: "symmetric" (both ways), "positive" (+axis) or "negative" (-axis).
        participants: Comma-separated names, paths or patterns of the only components to cut,
            e.g. "PANEL:1,*DOOR*"; empty = all that the shape meets.
    """
    names = [n.strip() for n in participants.split(",") if n.strip()]
    return inv.assembly_cut(axis, circles, rectangles, at, distance, direction, names or None)


@tool()
def place_components(components: list[dict], atomic: bool = True, skip_if_exists: bool = False) -> str:
    """Place parts / sub-assemblies into the active assembly — one or hundreds in one call. Files
    must exist on disk (save_document first); all are checked before anything is placed.

    Args:
        components: [{"path": "C:/.../beam.ipt", "x": 0, "y": 0, "z": 0, "x_axis": [1,0,0],
                      "y_axis": [0,1,0], "grounded": false, "name": "Beam_A1"}, ...]. mm.
                      "origin": [x,y,z] works too. Axes and name are optional (where the part's own
                      X/Y point; give both). Ground the first component of every assembly.
        atomic: One undo step: if any placement fails, none stay.
        skip_if_exists: Don't place one where the same file already sits at the same place —
                        safe to run a list again after an error.
    """
    return inv.place_many(components, atomic, skip_if_exists)


@tool()
def constrain(a: str, b: str, kind: str = "mate", offset: float = 0, angle: float = 0,
              a_at: dict | None = None, axes_opposed: bool = True) -> str:
    """Constrain two components, at any depth — by origin geometry, faces or circular edges. Prefer
    origin geometry: it survives part edits; face and edge numbers change with the topology.

    References: "Part:1#XY" / "#XZ" / "#YZ" / "#X" / "#Y" / "#Z" / "#ORIGIN" (origin geometry),
    "Sub:1/Part:1#F12" (face, from find_faces), "Sub:1/Bolt:1#E4" (edge, from find_edges).
    A list index works as the component: "2#XY".

    Says which OTHER components the constraint moved — one can drag parts you didn't mean to touch.

    Args:
        a, b: References; a is the one meant to move.
        kind: "mate" (faces against each other, axes in line), "flush" (faces the same way),
              "angle", or "insert" (two circular edges: a bolt into a hole).
        offset: mm for mate / flush / insert. Its sign is easy to get wrong — give a_at too.
        angle: Degrees for kind="angle".
        a_at: Where a's face must end up, e.g. {"Z": 182.5}. If it lands elsewhere, the offset is
              tried with the other sign.
        axes_opposed: For insert: True = the components face each other (normal bolt / hole case).
    """
    return inv.constrain_faces(a, b, kind, offset, angle, a_at, axes_opposed)


# ---------- The call in progress ----------

@tool(com=False)
async def manage_call(action: str = "status", wait_seconds: float = 0, instance: Instance = "") -> str:
    """Follow or stop a call that outlived the wait — a long execute_python, a Content Center scan,
    one stuck behind an Inventor dialog.

    "status": elapsed time, any open dialog's text, output so far, progress() line — and the
    result once it has finished. wait_seconds waits (at most 45 s) and returns as soon as it ends.
    "cancel": cancelled() in execute_python code turns True at once; code still running after 2 s
    gets a KeyboardInterrupt, repeated until it stops. That lands at the next Python line: a call
    stuck INSIDE one COM operation or behind a dialog stops only once that returns.

    Args:
        action: "status" or "cancel".
        wait_seconds: For "status".
    """
    if action.lower() == "cancel":
        return await cancel_call(instance)
    if action.lower() == "status":
        return await call_status(wait_seconds, instance)
    return f"Unknown action '{action}': status or cancel."


# ---------- Document management ----------

@tool()
def save_document(path: str = "", overwrite: bool = False, save_dependents: bool = False) -> str:
    """Save the active document — never opens a dialog, and never writes a library or
    Content Center file. Dependents that changed (e.g. sub-assemblies, edited parts) are
    saved one by one first, except files in the project's library paths or Content Center
    folders and Content Center members: Inventor marks standard parts dirty after opening
    an old assembly, and re-saving them would rewrite shared library files. Returns the
    files saved and the ones skipped.
    The path is checked first (characters, extension, an existing file) so a bad one
    gets a clear message instead of Inventor's E_INVALIDARG.

    Args:
        path: Full file path to save as (e.g. "D:/parts/flange.ipt"). If empty, saves in place.
        overwrite: Replace a file that already exists at path (not one open in Inventor).
        save_dependents: False = save only this document (Save2(False)); changed
            dependents stay unsaved and are listed.
    """
    return inv.save_document(path or None, overwrite, save_dependents)


@tool()
def export_document(path: str, format: str = "STEP") -> str:
    """Write the active document to a neutral CAD file, for a customer, a vendor or a printer.
    An existing file at path is replaced; the file is checked afterwards and its size returned.
    The Inventor document itself is unchanged — save_document saves that. For the laser use
    export_flat_pattern, for a parts list export_bom, for a PDF create_drawing.

    Args:
        path: Output file, e.g. "D:/export/part.step".
        format: STEP, IGES, SAT or STL.
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
    corner_radius: float = 0,
) -> str:
    """Draw a centered rectangle in a sketch. Dimensions in mm.

    Args:
        x: Center X coordinate in mm.
        y: Center Y coordinate in mm.
        width: Width in mm.
        height: Height in mm.
        sketch_index: Sketch number (0 = latest sketch).
        corner_radius: Round all four corners with real arcs (half the short side = a slot).
    """
    idx = sketch_index if sketch_index > 0 else None
    return inv.draw_rectangle(x, y, width, height, idx, corner_radius)


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
    """Draw one straight line in a sketch, in the sketch's coordinates — an open line, or a
    construction line for a revolve axis. Lines drawn one by one don't join into a profile:
    for a closed shape use draw_closed_profile or draw_rectangle.

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
    points: str | list,
    sketch_index: int = 0,
) -> str:
    """Draw a closed profile of lines AND arcs. All coordinates in mm. Segments share their
    end points, so Inventor sees one closed loop for extrude/revolve. Never approximate a
    curve with many short lines — round corners and arcs are built in:

      [x, y]              a corner
      [x, y, r]           a corner rounded with radius r (tangent arc, lines trimmed)
      {"through": [x, y]} the segment from the corner before to the corner after is an
                          arc through this point (after the last corner: the closing one)

    Examples: plate with R10 corners "[[0,0,10],[200,0,10],[200,100,10],[0,100,10]]";
    a D shape "[[0,-50],[0,50],{"through":[50,0]}]". Arcs go through three points, so the
    sketch's axis directions can't flip them.

    Args:
        points: JSON array (or list) of the entries above.
        sketch_index: Sketch number (0 = latest sketch).
    """
    import json as _json
    pts = _json.loads(points) if isinstance(points, str) else points
    idx = sketch_index if sketch_index > 0 else None
    return inv.draw_closed_profile(pts, idx)


@tool()
def draw_arc(x1: float, y1: float, x2: float, y2: float,
             xm: float | None = None, ym: float | None = None,
             cx: float | None = None, cy: float | None = None,
             sketch_index: int = 0) -> str:
    """Draw an open arc from (x1, y1) to (x2, y2) in mm — e.g. to close a profile drawn with
    draw_line. Either through a point on it (xm, ym), or around its center (cx, cy) running
    counter-clockwise in sketch coordinates. For closed shapes prefer draw_closed_profile.

    Args:
        x1, y1, x2, y2: Start and end.
        xm, ym: A point on the arc between them.
        cx, cy: The center (start and end must be equally far from it).
        sketch_index: Sketch number (0 = latest sketch).
    """
    return inv.draw_arc(x1, y1, x2, y2, xm, ym, cx, cy, sketch_index if sketch_index > 0 else None)


@tool()
def draw_slot(x: float, y: float, length: float, width: float, angle: float = 0,
              sketch_index: int = 0) -> str:
    """Draw a slot (oblong hole / rounded-end profile) centered on (x, y), mm.

    Args:
        x, y: Center.
        length: Overall length, end to end.
        width: Width (= the end radius x 2).
        angle: Direction of the length in degrees, in sketch coordinates.
        sketch_index: Sketch number (0 = latest sketch).
    """
    return inv.draw_slot(x, y, length, width, angle, sketch_index if sketch_index > 0 else None)


@tool()
def draw_pattern(shape: str = "circle", diameter: float = 0, length: float = 0,
                   width: float = 0, corner_radius: float = 0, angle: float = 0,
                   x: float = 0, y: float = 0, rows: int = 1, columns: int = 1,
                   dx: float = 0, dy: float = 0, count: int = 0, pcd: float = 0,
                   start_angle: float = 0, sketch_index: int = 0) -> str:
    """Many copies of one shape in one sketch — vent slots, hole grids, bolt circles — then
    one cut makes them all. mm.

    Grid: the first copy at (x, y), `rows` x `columns`, pitch `dx` (between columns) and `dy`
    (between rows). Circle: `count` copies on a circle of diameter `pcd` around (x, y), the
    first at `start_angle` degrees; slots and rectangles turn with the circle.

    Args:
        shape: circle (diameter), slot (length, width — overall), rectangle (length, width,
               corner_radius).
        angle: Turn every slot / rectangle by this many degrees (length along X at 0).
        sketch_index: Sketch number (0 = latest sketch).
    """
    return inv.sketch_pattern(shape, diameter, length, width, corner_radius, angle, x, y, rows,
                              columns, dx, dy, count, pcd, start_angle,
                              sketch_index if sketch_index > 0 else None)


@tool()
def check_sketch(sketch_index: int = 0) -> str:
    """Why won't it extrude? Checks a sketch for what a closed profile needs: open ends, gaps
    (two ends a hair apart, not joined), points where 3+ curves meet (overlaps, stray
    lines), lines drawn twice — with sketch coordinates in mm — and whether Inventor builds a
    profile, with how many closed loops. extrude / revolve report the same when they fail.

    Args:
        sketch_index: Sketch number (0 = latest sketch).
    """
    return json.dumps(inv.check_sketch(sketch_index if sketch_index > 0 else None), indent=1, ensure_ascii=False)


@tool()
def offset_profile(distance: float, side: str = "inside", sketch_index: int = 0) -> str:
    """Offset every closed loop of a sketch by `distance` mm — a gasket groove, a wall, a
    lip, an inner cut-out with a margin. Lines, arcs and circles stay lines, arcs and circles.

    Args:
        distance: Offset in mm.
        side: "inside" or "outside" of each loop.
        sketch_index: Sketch number (0 = latest sketch).
    """
    return inv.offset_profile(distance, side, sketch_index if sketch_index > 0 else None)


@tool()
def project_edges(face_index: int = 0, edge_indices: str = "", body_index: int = 1,
                  sketch_index: int = 0) -> str:
    """Project a body's edges onto the sketch — exact lines, arcs, circles and splines,
    linked to the model. Use it to build on existing or reference geometry (e.g. remodelling
    an imported part) instead of re-tracing curves point by point.

    Args:
        face_index: Project every edge of this face of the body (find_faces).
        edge_indices: Or these edges, "3,7,8" (find_edges).
        body_index: Body in a multi-body part (1 = first).
        sketch_index: Sketch number (0 = latest sketch).
    """
    return inv.project_edges(face_index, edge_indices, body_index, sketch_index if sketch_index > 0 else None)


# ---------- Features ----------

@tool()
def extrude(
    distance: float = 10,
    direction: str = "positive",
    operation: str = "join",
    sketch_index: int = 0,
    extent_type: str = "distance",
) -> str:
    """Turn a sketch's closed profile into 3D: add material, cut it, or start a new body.
    For a turned shape use revolve; for sheet metal, add_sheet_metal_face and sheet_metal_cut.

    Returns the feature name and the change, e.g. "V 23497 (-503 mm³) | F7 E14": read it — a cut
    showing no volume change went the wrong way or missed the body; try the other direction.
    Without a closed profile it fails and says where the gap is (as check_sketch does).

    Args:
        distance: mm. Ignored when extent_type="all".
        direction: "positive" (along the sketch normal: +Z on the XY plane), "negative" or "symmetric".
        operation: "join" (add), "cut", "intersect", "surface" or "new" (a separate body).
        sketch_index: Sketch number (0 = latest sketch).
        extent_type: "distance", or "all" to go through the entire body.
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
    """Round edges of the active part's first body. For an angled edge use chamfer; to round
    a profile's corners before extruding, use draw_closed_profile radii.

    Edge numbers change after every feature: run find_edges again before the next fillet, and
    give all edges of one radius in one call. Tangent edges are followed automatically. Fails
    if the radius doesn't fit the geometry — fillet the big radii first. Returns the feature name.

    Args:
        radius: Fillet radius in mm, e.g. 2.
        edge_indices: Comma-separated edge numbers from find_edges, e.g. "1,2,3".
    """
    indices = [int(i.strip()) for i in edge_indices.split(",")]
    return inv.fillet(radius, indices)


@tool()
def chamfer(distance: float = 2, edge_indices: str = "1") -> str:
    """Add an equal-distance chamfer to edges of the active part's first body. For rounded
    edges use fillet; on sheet metal corners, add_corner_round.

    Give ALL edges in one call: edge numbers change after every feature, so a second call with
    old numbers hits the wrong edges. Returns the feature name.

    Args:
        distance: Chamfer distance in mm, e.g. 2.
        edge_indices: Comma-separated edge numbers from find_edges, e.g. "1,2,3".
    """
    indices = [int(i.strip()) for i in edge_indices.split(",")]
    return inv.chamfer(distance, indices)


@tool()
def add_circular_pattern(
    feature_index: int,
    count: int = 8,
    angle: float = 360,
    axis: str = "Z",
) -> str:
    """Create a circular (rotational) pattern of a feature around a work axis.
    Ideal for bolt holes around a flange — one hole + add_circular_pattern instead of 8 holes.

    Args:
        feature_index: Index of the feature to pattern (from list_features()).
        count: Number of instances (total including the original).
        angle: Total span angle in degrees (360 = full circle).
        axis: Rotation axis — X, Y, or Z (default Z for flanges).
    """
    return inv.circular_pattern(feature_index, count, angle, axis)


# ---------- Parameters ----------

# ---------- Sheet Metal ----------

@tool()
def add_sheet_metal_face(sketch_index: int = 0) -> str:
    """Create the first flat panel of a sheet metal part from a closed sketch profile; add_flange
    bends walls from its edges, sheet_metal_cut opens holes in it. Needs a part made with
    create_document(kind="sheet_metal"). Set the rule first (manage_sheet_metal_rules): the panel
    takes its thickness from the active rule, so it has none of its own to pass here.

    Args:
        sketch_index: Sketch number (0 = latest sketch).
    """
    idx = sketch_index if sketch_index > 0 else None
    return inv.sheet_metal_face(idx)


@tool()
def add_sheet_profile(kind: str, width: float, height: float, length: float, flange: float = 0,
                      bend_radius: float = 0) -> str:
    """Make a whole bent profile — angle, channel, Z or hat — as one contour flange in an empty
    sheet metal part, from outer sizes: section in the XY plane from 0,0, length along +Z. It
    unfolds like any sheet metal part. Set the rule first (manage_sheet_metal_rules): thickness
    comes from it. For a panel with walls use add_sheet_metal_face + add_flange.

    Args:
        kind: "L", "U", "C" (U with lips turned in), "Z" or "hat".
        width: L: bottom leg. U, C: overall width. Z: top flange, measured over the web.
            Hat: width of the crown. All mm.
        height: Overall height in mm.
        length: Length of the profile in mm.
        flange: C: lip. Z: bottom flange, measured over the web. Hat: each brim. Ignored for L and U.
        bend_radius: Inside radius in mm; 0 = from the rule.
    """
    return inv.sheet_profile(kind, width, height, length, flange, bend_radius)


@tool()
def add_flange(edge_indices: str, distance: float = 25, angle: float = 90,
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
        edge_indices: Comma-separated edge indices (use find_edges to locate).
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
def add_corner_round(edge_indices: str, radius: float = 5) -> str:
    """Round sharp sheet metal corners (Corner Round — carried into the flat pattern).
    Use on exposed corners people handle, e.g. mounting plate tabs.

    Args:
        edge_indices: Comma-separated indices of the short edges through the thickness at the corners.
        radius: Corner radius in mm.
    """
    idx_list = [int(i.strip()) for i in edge_indices.split(",")]
    return inv.corner_round(idx_list, radius)


# ---------- Query ----------

# ---------- Assembly (IAM) ----------

# ---------- Manufacturing outputs ----------

@tool()
def export_flat_pattern(path: str = "", format: str = "DXF", bend_lines: bool = True) -> str:
    """Flat pattern of the active sheet metal part: flat size, thickness, bend count — and with
    a path, a DXF/DWG for the laser / punch (layers OUTER = contour, INNER = holes and
    cut-outs, IV_BEND* = bend lines; tangent lines, sketches etc. left out). Creates the
    flat pattern if the part has none.

    Args:
        path: Output file (.dxf or .dwg). Empty: just report the flat size.
        format: DXF or DWG.
        bend_lines: Include the bend lines (on their own layers).
    """
    return json.dumps(inv.flat_pattern(path, format, bend_lines), indent=1, ensure_ascii=False)


@tool()
def export_bom(view: str = "parts_only", path: str = "") -> str:
    """Bill of materials of the active assembly: item, quantity, part number, description,
    material, stock number, mass, file. For purchasing / ERP — set part numbers and
    descriptions first with properties().

    Args:
        view: "parts_only" (every part once with its total quantity) or "structured"
              (sub-assemblies with their parts indented).
        path: Also write a CSV (UTF-8, ';'-separated) — opens in Excel.
    """
    return inv.bom(view, path)


@tool()
def edit_properties(values: dict | None = None, material: str = "", mass_kg: float = 0) -> str:
    """Read — and optionally set — the active document's iProperties: part number,
    description, stock number, vendor, revision, title, cost center, material, mass and
    custom properties. These feed the BOM and the drawing's title block.

    Args:
        values: e.g. {"part_number": "BRS-101", "description": "Side panel 2 mm",
                "stock_number": "DKP-2.0"}. Unknown keys become custom properties.
        material: Material name as in Inventor's library (e.g. "Steel, Mild"; manage_materials(action='list')).
        mass_kg: Override the mass — for vendor models made of surfaces, which have no volume
                 (a 1.23 kg motor showed 0.23 kg). manage_materials lists them.
    """
    return json.dumps(inv.properties(values, material, mass_kg), indent=1, ensure_ascii=False, default=str)


@tool()
def convert_surfaces_to_solid(work_surfaces: str = "all", tolerance: float = 0.01, patch_holes: bool = True) -> str:
    """Turn surfaces into solids — imported vendor models (x_t, IGES, STEP) that came in as
    faces with no volume, so no mass and no booleans. Stitches the part's visible surfaces,
    patches the holes left in the shell, and closes it into a solid; a shell that is already
    closed becomes a solid directly. Reports each step, the solids' volumes, and the open
    edges it couldn't close (coordinates in mm). Run it in the part that holds the surfaces.

    Args:
        work_surfaces: "all", or WorkSurface numbers "1,2,5" to convert one group at a time
                       (e.g. one component of a multi-part import).
        tolerance: Largest gap to stitch over, mm.
        patch_holes: Fill loops of open edges with a boundary patch.
    """
    return json.dumps(inv.surface_to_solid(work_surfaces, tolerance, patch_holes), indent=1, ensure_ascii=False)


@tool()
def create_drawing(pdf_path: str, views: str = "front,top,right,iso", template: str = "ISO",
                 scale: float = 0, flat: bool = False, drawing_path: str = "") -> str:
    """2D drawing of the active part or assembly (saved to disk) → PDF for the shop floor.
    A base view, projected views around it, overall dimensions on the base view (between
    its outermost straight edges — check them on assemblies), the model dimensions
    Inventor can retrieve, and the template's title block (filled from properties()).
    Read the PDF back to check the layout.

    Args:
        pdf_path: Output .pdf.
        views: First is the base view (front/top/right/left/back/bottom); the rest are
               projected from it: top (placed below), right (beside), iso (shaded, half scale).
        template: Drawing template name (ISO, DIN, ANSI (mm) …) or a full .idw path.
        scale: 0 = largest standard scale that fits; or e.g. 0.2 for 1:5.
        flat: Sheet metal part: draw the flat pattern (one view).
        drawing_path: Also keep the drawing as .idw / .dwg (otherwise it is closed).
    """
    return inv.make_drawing(pdf_path, views, template, scale, flat, drawing_path)


@tool()
def use_content_center(category: str = "", family: str = "", size: str = "",
                   length: float = 0, file_name: str = "") -> str:
    """Browse the Content Center and make member parts to place (place_components / place_components).

    Step by step — each call is quick (the whole tree takes minutes to walk, so give a path):
      content_center()                                         → top categories
      content_center("Structural Shapes")                      → sub-categories
      content_center("Structural Shapes/Channels")             → families
      content_center("Structural Shapes/Channels", "DIN 1026-1")          → sizes
      content_center("Structural Shapes/Channels", "DIN 1026-1", "U 100", length=1500)
                                                               → part file path

    Args:
        category: Category path, "/"-separated, as listed.
        family: Family name, as listed.
        size: Size designation as listed (spaces and case ignored).
        length: mm — a custom-length member (structural shapes). 0 = standard member.
        file_name: Where a custom member is saved (default: project workspace /
                   Content Center Custom/<family> <size> L<length>.ipt).
    """
    return inv.content_center(category, family, size, length, file_name)


@tool()
def add_frame_members(members: list[dict], category: str = "", family: str = "", size: str = "",
                  folder: str = "") -> str:
    """Build a steel frame: structural profiles laid along skeleton lines in the active
    assembly — what Frame Generator does (it has no API). Each member is a Content Center
    custom-length part (one file per profile and length, reused), turned so its length runs
    from start to end. Returns the placements and a saw cut list. Check with capture_view.

    Args:
        members: [{"start": [0,0,0], "end": [0,0,2000], "name": "Post_1"}, ...] in mm. Per
            member, optional: "family", "size", "category" (override the call's), "roll"
            (degrees, turns the section about its line), "anchor" ("center" = the section's
            middle on the line, default; "origin" = the profile's own origin), "extend"
            ([at start, at end] mm, negative to shorten — e.g. to stop at a crossing member).
            Horizontal members keep their section level; roll 90 turns a U to face sideways.
        category, family, size: Default profile, as use_content_center lists them, e.g.
            "Structural Shapes/Channels", "DIN 1026-1", "U 100".
        folder: Where the member files go (default: project workspace /Content Center Custom).
    """
    return inv.frame_members(members, folder, category, family, size)


@tool()
def import_step(step_path: str, dest_folder: str, name: str = "") -> str:
    """Import a vendor STEP file (hinge, lock, gland...) as Inventor files in dest_folder.
    The STEP is copied into dest_folder first, because Inventor writes the parts of
    an imported assembly next to the .step file (e.g. into Downloads or a synced folder).
    Returns path, type, overall bbox and per-component bbox/volume — use them to
    find mounting faces and axes before placing it with place_components.

    Args:
        step_path: The downloaded .step / .stp file.
        dest_folder: Project folder to hold the Inventor files.
        name: Base name for the .iam/.ipt (default: the STEP file name).
    """
    return json.dumps(inv.import_step(step_path, dest_folder, name or None), indent=1, ensure_ascii=False)


@tool()
def check_interference(set_a: str, set_b: str = "", ignore_fasteners: bool = False) -> str:
    """Check overlap between occurrences of the active assembly: full paths of both, volume and
    bounding box of each. Touching faces are not reported. Intended overlaps (gasket compression,
    thread in thread) show up too — read the volume and bbox.

    Args:
        set_a: Comma-separated names, paths or patterns: "DOOR:1", "Sub:1/Part:2", "*BRACKET*".
        set_b: The same, to test against; empty = everything else.
        ignore_fasteners: Set aside overlaps with small Content Center members (bolts, nuts,
                          washers, under 150 mm) — threads overlap by design. They are counted.
    """
    a = [s.strip() for s in set_a.split(",") if s.strip()]
    b = [s.strip() for s in set_b.split(",") if s.strip()] or None
    hits = inv.check_interference(a, b, ignore_fasteners)
    shown = [h for h in hits if not h.get("fastener")]
    note = f"\n({len(hits) - len(shown)} overlap(s) with fasteners set aside)" if len(hits) != len(shown) else ""
    return (json.dumps(shown, indent=1, ensure_ascii=False) if shown else "No interference") + note


@tool()
def measure_distance(a: str, b: str) -> str:
    """Minimum distance in mm between two occurrences (0 = touching/overlapping).
    Names as in manage_occurrences; "Asm:1/2" reaches a part inside a sub-assembly."""
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


def main():
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
