"""MCP Server for Autodesk Inventor parametric modeling."""

import asyncio
import concurrent.futures
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

import pythoncom
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
DEFAULT = "default"
_local = threading.local()  # .instance on each Inventor thread


class _Call:
    def __init__(self, name, future):
        self.name, self.future = name, future
        self.started, self.finished = time.monotonic(), None
        self.timed_out = False      # outlived CALL_TIMEOUT: the client never saw the result
        self.collected = False      # call_status has returned the result
        future.add_done_callback(lambda _: setattr(self, "finished", time.monotonic()))


class _Instance:
    def __init__(self, name):
        self.name = name
        self.conn = _api.InventorConnection()
        self.ns: dict = {}              # execute_python's namespace (variables persist)
        self.ns_created = time.time()
        self.output = None              # stdout buffer of the execute_python call in progress
        self.last_call: _Call | None = None
        self.project = ""               # active .ipj when last seen
        self.libraries: list[str] = []  # load_library files, rerun in a new namespace
        self.ns_ready = False           # startup file and libraries loaded
        self.lock = threading.Lock()    # two clients' calls arrive on two threads
        self.thread = concurrent.futures.ThreadPoolExecutor(
            max_workers=1, thread_name_prefix=f"inventor-{name}", initializer=self._init_thread)

    def _init_thread(self):
        pythoncom.CoInitialize()
        _local.instance = self

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
    return msg + "\n" + (_dialog_report(inst) or
                         "No dialog is open — Inventor is still working on it (a long operation).")


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


def _on_com_thread(inst: _Instance, fn, args, kwargs, wait=None):
    with inst.lock:
        c = inst.last_call
        if c is not None and not c.future.done():
            return (_busy_report(inst, c) + f"\nThis '{fn.__name__}' call was NOT started. "
                    "Use call_status to follow the running call.")
        blocker = _dialog_report(inst)
        if blocker:
            return blocker + f"\nThis '{fn.__name__}' call was NOT started. Retry once the dialog is closed."
        call = _Call(fn.__name__, inst.thread.submit(_silent_call, fn, args, kwargs))
        inst.last_call = call
    try:
        return call.future.result(timeout=CALL_TIMEOUT if wait is None else wait)
    except concurrent.futures.TimeoutError:
        call.timed_out = True
        return (_busy_report(inst, call) + "\nIt keeps running in the background — "
                "call call_status to follow it and get its result.")


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
        out = inst.output.getvalue().strip() if c.name == "execute_python" and inst.output else ""
        return _busy_report(inst, c) + (f"\nOutput so far:\n{out[-3000:]}" if out else "")
    took = f"{c.finished - c.started:.0f} s"
    if not c.timed_out or c.collected:
        return f"Last call '{c.name}' finished ({took}); its result was already returned."
    c.collected = True
    try:
        result = c.future.result()
    except Exception as e:
        result = f"ERROR: {type(e).__name__}: {e}"
    return f"'{c.name}' finished after {took}:\n{result}"


@tool(com=False)
def call_status(instance: Instance = "") -> str:
    """Follow a call that outlived the wait (a long job, or one stuck behind an Inventor
    dialog): elapsed time, any open dialog's text, execute_python output so far — and
    the call's result once it has finished. Safe to call at any time."""
    if instance or sum(i.attached for i in _instances.values()) <= 1:
        try:
            return _call_status(_pick(instance))
        except _NoInstance as e:
            return str(e)
    return "\n\n".join(f"[{i.name}] {_call_status(i)}" for i in _instances.values() if i.attached)


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


def _user_traceback(e: BaseException) -> str:
    """The traceback from the agent's own code down — not the server's frames above it."""
    tb = e.__traceback__
    t = tb
    while t is not None and not _is_user_file(t.tb_frame.f_code.co_filename):
        t = t.tb_next
    return "".join(traceback.format_exception(type(e), e, t or tb))


def _is_user_file(name: str) -> bool:
    return name.startswith("<mcp") or name in _local.instance.libraries or name == STARTUP


def _load_libraries(me) -> str:
    """Run the startup file and load_library files into a fresh namespace."""
    if me.ns_ready:
        return ""
    me.ns_ready = True
    notes = []
    for path in ([STARTUP] if os.path.isfile(STARTUP) else []) + me.libraries:
        try:
            with open(path, encoding="utf-8") as f:
                _run_source(me, f.read(), path)
            notes.append(f"(loaded {os.path.basename(path)})")
        except Exception as e:
            notes.append(f"(loading {path} failed:\n{_user_traceback(e)})")
    return "\n".join(notes)


def _bind(me) -> str | None:
    """Fresh inv/app/doc/comp/body/tg in the namespace. Returns an error to show, if any."""
    ctx = {"inv": me.conn, "math": math}
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
def execute_python(code: str, undo_on_error: bool = False) -> str:
    """Execute Python code against the live Inventor connection.

    Pre-bound objects (re-resolved fresh on every call):
      inv  — InventorConnection wrapper (all high-level methods, mm units)
      app  — Inventor.Application COM object
      doc  — active document          comp — its ComponentDefinition
      body — comp.SurfaceBodies.Item(1) (or None)
      tg   — TransientGeometry        math — math module
    Variables you assign persist between calls until the server restarts or reload_api
    runs — status shows the namespace's age and names. Each connected Inventor has its
    own namespace. Helpers you need every time: put them in a file and load_library it
    (or in ~/.inventor-mcp/startup.py) — they come back by themselves in a new namespace.
    Internal COM units are CENTIMETERS — divide mm by 10.
    Objects are late-bound: a Document from Documents.Open exposes ComponentDefinition
    directly. Don't use CastTo / gencache.EnsureDispatch (they create a gen_py cache).
    Runs with SilentOperation on, so Inventor answers its own prompts instead of opening
    a dialog. A call longer than the wait limit keeps running; follow it with call_status.
    Returns captured stdout + stderr (print your results!), or the traceback from your
    own code down on error.

    Args:
        code: Python source.
        undo_on_error: Run inside one undo transaction on the active document and roll
            it back if the code raises — no half-built sketches or work planes left behind.
    """
    import io
    me = _local.instance
    error = _bind(me)
    if error:
        return error
    buf = me.output = io.StringIO()
    _capture(buf)
    txn = None
    try:
        notes = _load_libraries(me)
        if notes:
            print(notes)
        if undo_on_error and me.ns.get("doc") is not None:
            txn = inv.app.TransactionManager.StartTransaction(me.ns["doc"], "MCP execute_python")
        try:
            inv._silently(lambda: _run_source(me, code))
        except Exception as e:
            if txn is not None:
                txn.Abort()
                txn = None
                print("(undo_on_error: every change of this call was rolled back)", file=sys.stderr)
            partial = buf.getvalue().strip()
            return (partial + "\n" if partial else "") + "ERROR:\n" + _user_traceback(e)
        if txn is not None:
            txn.End()
        out = buf.getvalue().strip()
        if len(out) > 6000:
            out = out[:6000] + "\n... (truncated at 6000 chars)"
        return out if out else "(ok, no output)"
    finally:
        _release()


@tool()
def load_library(path: str) -> str:
    """Run a Python file into this Inventor's execute_python namespace and keep it there:
    after reload_api or a new namespace it is loaded again by itself. For helper functions
    and cached objects (Content Center families…) you would otherwise redefine every time.
    A file at ~/.inventor-mcp/startup.py (or INVENTOR_MCP_STARTUP) is loaded the same way
    for every Inventor."""
    import io
    me = _local.instance
    path = os.path.abspath(path)
    if not os.path.isfile(path):
        return f"File not found: {path}"
    error = _bind(me)
    if error:
        return error
    before = set(me.ns)
    buf = me.output = io.StringIO()
    _capture(buf)
    try:
        _load_libraries(me)
        if path not in me.libraries:
            me.libraries.append(path)
        try:
            with open(path, encoding="utf-8") as f:
                source = f.read()
            inv._silently(lambda: _run_source(me, source, path))
        except Exception as e:
            me.libraries.remove(path)
            return buf.getvalue() + "ERROR:\n" + _user_traceback(e)
    finally:
        _release()
    new = sorted(k for k in set(me.ns) - before if not k.startswith("__"))
    out = buf.getvalue().strip()
    return (out + "\n" if out else "") + f"Loaded {os.path.basename(path)}: {', '.join(new) or 'no new names'}"


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
def view_image(views: str = "iso", width: int = 800, height: int = 600,
               background: str = "inventor", focus: str = "", sketches: bool = False):
    """Look at the model: pictures of the active document, returned as images you can see.
    Use it to check your work — a flange on the wrong side, a door opening the wrong way,
    a part floating off the frame — which the numbers alone don't show.

    Args:
        views: Comma-separated: iso, iso_left, iso_bottom, front, back, top, bottom, left,
               right, current (the user's view). E.g. "iso,front,top".
        width, height: Pixels per picture (keep them modest — each picture costs tokens).
        background: "inventor" (its gradient) or "white".
        focus: Assembly component name — zoom to it, keeping each view's direction.
        sketches: Show sketches and work features too (hidden by default; restored after).
    """
    names = [v.strip().lower() for v in views.split(",") if v.strip()]
    paths = inv.view_images(names, width, height, background.lower() == "white", focus, sketches)
    images = []
    for path in paths:
        with open(path, "rb") as f:
            images.append(Image(data=f.read(), format="png"))
        os.remove(path)
    os.rmdir(os.path.dirname(paths[0]))
    return images


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

@tool(com=False)
async def connect(visible: bool = True, project: str = "", new_instance: bool = False,
            instance: Annotated[str, Field(description=(
                "Name for this Inventor — pass the same name to later calls. Default: "
                "'default', or the project's name when the server starts one for it."))] = "") -> str:
    """Connect to Autodesk Inventor. Starts Inventor if none is running.

    Working on a project? Pass project=<full path of its .ipj>: the server uses the
    Inventor that already has it, or the running one if it is free to switch, or else
    starts a separate Inventor for it — so two agents on two projects each get their own.
    The answer names the instance; when several Inventors are connected, pass
    instance=<that name> to every later call.

    Args:
        visible: Show Inventor window (default True).
        project: Full path of an .ipj to work in.
        new_instance: Always start a separate Inventor.
    """
    if project:
        project = os.path.abspath(project)
        if not os.path.isfile(project):
            return f"Project file not found: {project}"
    by_project = bool(project) and not new_instance and not instance
    if by_project:
        hit = next((i for i in _instances.values() if i.attached and _same_file(i.project, project)), None)
        if hit is not None:
            name = hit.name
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
        if new_instance or not conn.is_connected():
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
    for pid in _api.inventor_pids():
        if pid not in reached:
            lines.append(f"(pid {pid}: not connected — opened by hand, or by a server that is gone. "
                         "connect() reaches it only if it is the first Inventor that started; "
                         "otherwise save its work and close it, and use connect(new_instance=True).)")
    return "\n".join(lines) or "No Inventor is running. Call connect()."


@tool()
def status() -> str:
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
                     if not k.startswith("__") and k not in ("inv", "app", "doc", "comp", "body", "tg", "math"))
    info["python_namespace"] = {
        "created": time.strftime("%Y-%m-%d %H:%M", time.localtime(me.ns_created)),
        "names": defined,
        "libraries": [os.path.basename(p) for p in
                      ([STARTUP] if os.path.isfile(STARTUP) else []) + me.libraries],
    }
    unsaved = inv.unsaved_documents()
    if unsaved:
        info["never_saved_documents"] = unsaved + ["(close_unsaved closes the ones nothing references)"]
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
def save_document(path: str = "", overwrite: bool = False) -> str:
    """Save the active document — never opens a dialog. Saving in place also saves
    dependents that changed (e.g. sub-assemblies), which would otherwise trigger
    Inventor's modal 'save dependents?' prompt and block until a user clicks OK.
    The path is checked first (characters, extension, an existing file) so a bad one
    gets a clear message instead of Inventor's E_INVALIDARG.

    Args:
        path: Full file path to save as (e.g. "D:/parts/flange.ipt"). If empty, saves in place.
        overwrite: Replace a file that already exists at path (not one open in Inventor).
    """
    return inv.save_document(path or None, overwrite)


@tool()
def close_unsaved(include_active: bool = False) -> str:
    """Close documents that were never saved (Part12, Assembly3… left by failed attempts)
    without saving. Ones another open document references are kept.

    Args:
        include_active: Close the active document too if it was never saved.
    """
    return inv.close_unsaved(include_active)


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
def content_center(category: str = "", family: str = "", size: str = "",
                   length: float = 0, file_name: str = "") -> str:
    """Browse the Content Center and make member parts to place (place_component / place_many).

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
def place_many(components: list[dict]) -> str:
    """Place many parts / sub-assemblies into the active assembly in one call — for frames
    of dozens or hundreds of members, where place_component one by one is slow.

    Args:
        components: [{"path": "C:/.../beam.ipt", "x": 0, "y": 0, "z": 0,
                      "x_axis": [1,0,0], "y_axis": [0,1,0], "grounded": false,
                      "name": "Beam_A1"}, ...]. mm. "origin": [x,y,z] works too. Axes and
                      name are optional (axes: where the part's own X/Y point; give both).
                      Files must exist — all are checked before anything is placed.
    """
    return inv.place_many(components)


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
