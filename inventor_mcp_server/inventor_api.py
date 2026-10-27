"""Wrapper around Autodesk Inventor COM API."""

import os
import win32com.client
import win32com.client.gencache
import pythoncom
import win32process

# Never wrap COM objects in makepy (gen_py) classes, even when a cache exists. Something
# else on the machine — a script calling CastTo or gencache.EnsureDispatch — creates one
# for the Inventor type library, and from then on GetActiveObject fails with
# KeyError '_dispobj_' and a generic Document hides ComponentDefinition. Late-bound
# objects (with type info) expose every member of the real object.
win32com.client.gencache.GetClassForCLSID = lambda clsid: None


# Inventor 2026 enum constants (from type library)
# PartFeatureExtentDirectionEnum
kPositiveExtentDirection = 20993
kNegativeExtentDirection = 20994
kSymmetricExtentDirection = 20995

# PartFeatureOperationEnum
kJoinOperation = 20481
kCutOperation = 20482
kIntersectOperation = 20483
kSurfaceOperation = 20484
kNewBodyOperation = 20485

# DocumentTypeEnum
kPartDocumentObject = 12290
kAssemblyDocumentObject = 12291
kDrawingDocumentObject = 12292

# DimensionOrientationEnum (from Inventor 2026 type library — NOT 40706/40707/40708!)
kHorizontalDim = 19201
kVerticalDim   = 19202
kAlignedDim    = 19203

# CurveTypeEnum (Edge.GeometryType) — verified on 2026/2027
kLineSegmentCurve = 5123
kCircleCurve      = 5124

# SurfaceTypeEnum (Face.SurfaceType)
SURFACE_TYPES = {5889: "Unknown", 5890: "Plane", 5891: "Cylinder", 5892: "EllipticCylinder",
                 5893: "Cone", 5894: "EllipticCone", 5895: "Torus", 5896: "Sphere", 5897: "BSpline"}


# ---------- Readable errors ----------
#
# A com_error's own text is Windows' localized message for the HRESULT ('Özel durum
# oluştu.' = 'Exception occurred.') — it says nothing. The HRESULT's name, the inner
# scode of a DISP_E_EXCEPTION and a hint do.

HRESULTS = {
    0x80004005: ("E_FAIL", "Inventor refused the operation — check the inputs and the geometry"),
    0x80070057: ("E_INVALIDARG", "an argument is wrong: type, cm units, an object of another "
                                 "document, or a proxy needed in an assembly"),
    0x80004001: ("E_NOTIMPL", "not available for this object or document type"),
    0x80004002: ("E_NOINTERFACE", "the object is not of the type this call needs"),
    0x8000FFFF: ("E_UNEXPECTED", "Inventor is in a state that doesn't allow this call"),
    0x80070005: ("E_ACCESSDENIED", "read-only file or object"),
    0x80020009: ("DISP_E_EXCEPTION", "Inventor raised an error inside the call"),
    0x80020003: ("DISP_E_MEMBERNOTFOUND", "no such property or method on this object"),
    0x80020006: ("DISP_E_UNKNOWNNAME", "no such property or method on this object"),
    0x80020005: ("DISP_E_TYPEMISMATCH", "an argument has the wrong type"),
    0x8002000B: ("DISP_E_BADINDEX", "index out of range — COM collections start at 1"),
    0x8002000E: ("DISP_E_BADPARAMCOUNT", "wrong number of arguments"),
    0x80010108: ("RPC_E_DISCONNECTED", "the object was disconnected — re-resolve it from app/doc "
                                       "(it went stale: document closed/reopened, feature deleted)"),
    0x80010001: ("RPC_E_CALL_REJECTED", "Inventor is busy (a dialog or another operation) — retry"),
    0x8001010A: ("RPC_E_SERVERCALL_RETRYLATER", "Inventor is busy — retry"),
    0x800706BA: ("RPC_S_SERVER_UNAVAILABLE", "Inventor is gone (closed or crashed) — connect again"),
    0x800706BE: ("RPC_S_CALL_FAILED", "Inventor crashed or closed during the call — connect again"),
}


def hresult_name(code) -> str:
    """'E_INVALIDARG (0x80070057): <hint>' — or the bare hex for an unknown code."""
    if code is None:
        return ""
    code &= 0xFFFFFFFF
    name, hint = HRESULTS.get(code, ("", ""))
    return f"{name} (0x{code:08X})" + (f": {hint}" if hint else "") if name else f"0x{code:08X}"


def describe_com_error(e) -> str:
    """One readable line for a pywintypes.com_error; str(e) for anything else."""
    args = getattr(e, "args", ())
    if type(e).__name__ != "com_error" or not args or not isinstance(args[0], int):
        return str(e)
    hr = args[0]
    excepinfo = args[2] if len(args) > 2 else None
    text = "COM error " + hresult_name(hr)
    if excepinfo:
        _, source, description, _, _, scode = (tuple(excepinfo) + (None,) * 6)[:6]
        if scode and (scode & 0xFFFFFFFF) != (hr & 0xFFFFFFFF):
            text += " — inner " + hresult_name(scode)
        if description and description.strip():
            text += f" — {source + ': ' if source else ''}{description.strip()}"
    if len(args) > 3 and args[3] is not None:
        text += f" (argument {args[3]})"
    return text


def inventor_messages(app) -> str:
    """Inventor's own error/warning text (ErrorManager), best effort. It can hold messages
    of earlier operations too."""
    try:
        em = app.ErrorManager
        if not (em.HasErrors or em.HasWarnings):
            return ""
        msg = (em.AllMessages or em.LastMessage or "").strip()
    except Exception:
        return ""
    return msg if len(msg) <= 800 else "…" + msg[-800:]


# HealthStatusEnum — read from the type library at runtime (health_name); this is the
# fallback when that fails (values of the 2022–2025 API reference).
HEALTH_FALLBACK = {
    11777: "kUnknownHealth", 11778: "kUpToDateHealth", 11779: "kOutOfDateHealth",
    11780: "kDriverLostHealth", 11781: "kInErrorHealth", 11782: "kDeletedHealth",
    11783: "kCannotComputeHealth", 11784: "kSuppressedHealth", 11785: "kBeyondStopNodeHealth",
    11786: "kInconsistentHealth", 11787: "kRedundantHealth", 11788: "kNewlyAddedHealth",
    11789: "kInvalidLimitsHealth", 11790: "kJointDOFLockedHealth",
}
_typelib_enums: dict[str, dict[int, str]] = {}


def typelib_enum(app, name: str) -> dict[int, str]:
    """{value: member name} of an enum in Inventor's type library ({} if it can't be read).
    Read through the Application's type info — no gen_py cache involved."""
    if name in _typelib_enums:
        return _typelib_enums[name]
    found: dict[int, str] = {}
    try:
        tlib = app._oleobj_.GetTypeInfo().GetContainingTypeLib()[0]
        for i in range(tlib.GetTypeInfoCount()):
            if tlib.GetDocumentation(i)[0] != name:
                continue
            ti = tlib.GetTypeInfo(i)
            for j in range(ti.GetTypeAttr().cVars):
                vd = ti.GetVarDesc(j)
                found[int(vd.value)] = ti.GetNames(vd.memid)[0]
            break
    except Exception:
        found = {}
    if found:
        _typelib_enums[name] = found
    return found


def health_name(app, value) -> str:
    """'kInErrorHealth' for 11781 — the name of a HealthStatusEnum value."""
    try:
        value = int(value)
    except (TypeError, ValueError):
        return str(value)
    return (typelib_enum(app, "HealthStatusEnum") or HEALTH_FALLBACK).get(value) or str(value)


def unhealthy(app, obj) -> str:
    """The health of a feature / constraint when it is not fine (up to date or
    suppressed), else ''."""
    try:
        name = health_name(app, obj.HealthStatus)
    except Exception:
        return ""
    return "" if name in ("kUpToDateHealth", "kSuppressedHealth") else name


def modal_dialogs(main_hwnd) -> list[dict]:
    """Modal dialogs Inventor is showing, read from the window list — no COM call,
    so it works while an API call is stuck behind the dialog.

    A modal dialog disables Inventor's main window; the dialog is then a visible,
    enabled top-level window of the same process. Returns [{title, text}].
    """
    import win32gui
    import win32process

    if not main_hwnd or not win32gui.IsWindow(main_hwnd) or win32gui.IsWindowEnabled(main_hwnd):
        return []
    pid = win32process.GetWindowThreadProcessId(main_hwnd)[1]
    dialogs = []

    def child_text(hwnd, texts):
        t = win32gui.GetWindowText(hwnd).strip()
        if t and win32gui.IsWindowVisible(hwnd) and t not in texts:
            texts.append(t)
        return True

    def top_level(hwnd, _):
        if (hwnd != main_hwnd and win32gui.IsWindowVisible(hwnd) and win32gui.IsWindowEnabled(hwnd)
                and win32process.GetWindowThreadProcessId(hwnd)[1] == pid):
            texts = []
            try:
                win32gui.EnumChildWindows(hwnd, child_text, texts)
            except win32gui.error:
                pass
            dialogs.append({"title": win32gui.GetWindowText(hwnd), "text": " | ".join(texts)[:300]})
        return True

    win32gui.EnumWindows(top_level, None)
    return dialogs


try:
    from . import keeper as _keeper
except ImportError:  # run as a script: inventor_api is imported from its own folder
    import keeper as _keeper


def keeper(start: bool = True):
    """The keeper process's object (see keeper.py), started if needed. None if it can't be."""
    import subprocess, sys, time
    k = _keeper.find()
    if k is not None or not start:
        return k
    flags = 0x00000008 | 0x00000200  # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
    for extra in (0x01000000, 0):    # CREATE_BREAKAWAY_FROM_JOB — outlive the client's job if allowed
        try:
            subprocess.Popen([sys.executable, _keeper.__file__], creationflags=flags | extra,
                             stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL, close_fds=True)
            break
        except OSError:
            continue
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        k = _keeper.find()
        if k is not None:
            return k
        time.sleep(0.2)
    return None


def kept_inventors(timeout: float = 8) -> list[dict]:
    """Inventors the keeper holds: pid, project, open documents. Asked on a short-lived
    thread with a time limit — an Inventor busy with another agent's call may not answer."""
    import threading
    found: list[dict] = []

    def ask():
        pythoncom.CoInitialize()
        try:
            k = keeper(start=False)
            for a in (k.Apps() if k is not None else ()):
                app = win32com.client.Dispatch(a)
                info = {"pid": win32process.GetWindowThreadProcessId(int(app.MainFrameHWND))[1]}
                found.append(info)
                try:
                    p = app.DesignProjectManager.ActiveDesignProject
                    info.update(project=p.FullFileName, project_name=p.Name,
                                documents=app.Documents.VisibleDocuments.Count)
                except Exception:
                    info["busy"] = True
        except Exception:
            pass
        finally:
            pythoncom.CoUninitialize()

    t = threading.Thread(target=ask, daemon=True)
    t.start()
    t.join(timeout)
    return found


def inventor_pids() -> list[int]:
    """Process ids of every running Inventor."""
    import subprocess
    out = subprocess.run(["tasklist", "/FI", "IMAGENAME eq Inventor.exe", "/FO", "CSV", "/NH"],
                         capture_output=True, text=True, creationflags=subprocess.CREATE_NO_WINDOW).stdout
    return [int(row.split('","')[1]) for row in out.splitlines() if row.startswith('"Inventor.exe"')]


def profile_segments(entries) -> list[tuple]:
    """A closed profile as ("line", p0, p1) / ("arc" | "fillet", p0, through, p1) segments,
    in mm — "fillet" is a rounded corner, tangent to its two lines.

    entries: [x, y] a corner; [x, y, r] a corner rounded with radius r (a tangent arc
    between its two straight neighbours); {"through": [x, y]} makes the segment from the
    corner before it to the corner after it an arc through that point (after the last
    corner: the closing segment). Pure geometry — no Inventor needed.
    """
    import math
    corners, through = [], {}
    for e in entries:
        if isinstance(e, dict):
            if "through" not in e or not corners:
                raise ValueError(f"{e!r}: an arc entry is {{\"through\": [x, y]}}, after a corner")
            through[len(corners) - 1] = tuple(float(v) for v in e["through"][:2])
        else:
            if len(e) not in (2, 3):
                raise ValueError(f"{e!r}: a corner is [x, y] or [x, y, radius]")
            corners.append((float(e[0]), float(e[1]), float(e[2]) if len(e) == 3 else 0.0))
    n = len(corners)
    if n < 2 or (n == 2 and not through):
        raise ValueError("A closed profile needs at least 3 corners (or 2 joined by an arc)")

    def unit(a, b):
        d = math.hypot(b[0] - a[0], b[1] - a[1])
        if d < 1e-9:
            raise ValueError(f"Two corners at the same point {a}")
        return ((b[0] - a[0]) / d, (b[1] - a[1]) / d), d

    # rounded corners: where each one trims its two lines, and the arc's middle point
    trim_in, trim_out, mid = {}, {}, {}
    for i, (x, y, r) in enumerate(corners):
        if r <= 0:
            continue
        if (i - 1) % n in through or i in through:
            raise ValueError(f"Corner {i + 1} ({x}, {y}): a radius needs straight lines on both sides")
        p = (x, y)
        u1, _ = unit(p, corners[(i - 1) % n][:2])
        u2, _ = unit(p, corners[(i + 1) % n][:2])
        phi = math.acos(max(-1.0, min(1.0, u1[0] * u2[0] + u1[1] * u2[1])))
        if phi < 1e-6 or abs(phi - math.pi) < 1e-6:
            raise ValueError(f"Corner {i + 1} ({x}, {y}) is straight — nothing to round")
        t = r / math.tan(phi / 2)
        trim_in[i] = (p[0] + u1[0] * t, p[1] + u1[1] * t)
        trim_out[i] = (p[0] + u2[0] * t, p[1] + u2[1] * t)
        b, _ = unit((0.0, 0.0), (u1[0] + u2[0], u1[1] + u2[1]))
        c = r / math.sin(phi / 2)
        mid[i] = (p[0] + b[0] * (c - r), p[1] + b[1] * (c - r))

    segs = []
    for i in range(n):
        j = (i + 1) % n
        a = trim_out.get(i, corners[i][:2])
        b = trim_in.get(j, corners[j][:2])
        if i in through:
            segs.append(("arc", a, through[i], b))
        else:
            length = math.hypot(b[0] - a[0], b[1] - a[1])
            full = math.hypot(corners[j][0] - corners[i][0], corners[j][1] - corners[i][1])
            if (i in trim_out or j in trim_in) and (length < 1e-6 or
                    (b[0] - a[0]) * (corners[j][0] - corners[i][0]) + (b[1] - a[1]) * (corners[j][1] - corners[i][1]) <= 0):
                raise ValueError(f"The radii at corners {i + 1} and {j + 1} don't fit on a {full:g} mm side")
            if length > 1e-6:
                segs.append(("line", a, b))
        if j in mid:
            segs.append(("fillet", trim_in[j], mid[j], trim_out[j]))
    return segs


class InventorConnection:
    def __init__(self):
        self._app = None
        self.main_hwnd = None  # Inventor's main window, for modal_dialogs()

    @property
    def app(self):
        if self._app is None:
            raise RuntimeError(
                "Not connected to Inventor. Call connect() first."
            )
        return self._app

    def connect(self, visible: bool = True, new_instance: bool = False) -> str:
        pythoncom.CoInitialize()
        try:
            if new_instance:
                raise LookupError  # DispatchEx always starts a separate Inventor
            self._app = win32com.client.GetActiveObject("Inventor.Application")
            msg = f"Connected to running Inventor {self._app.SoftwareVersion.DisplayVersion}"
        except Exception:
            try:
                running = set(inventor_pids())
                self._app = win32com.client.DispatchEx("Inventor.Application")
                self._app.Visible = visible
                import time
                deadline = time.monotonic() + 60
                while not self._app.Ready and time.monotonic() < deadline:
                    time.sleep(0.5)
                msg = f"Started Inventor {self._app.SoftwareVersion.DisplayVersion}"
                self._keep()
                pid = win32process.GetWindowThreadProcessId(int(self._app.MainFrameHWND))[1]
                if pid in running:
                    # an Inventor whose client went away without letting it finish quitting
                    # can still answer CreateObject — and hang on the next call
                    msg += (f" — but Windows handed back an Inventor that was already running "
                            f"(pid {pid}), not a new one. If calls hang, close that Inventor.")
            except Exception as e:
                raise RuntimeError(f"Cannot connect to Inventor: {e}")
        try:
            self.main_hwnd = int(self._app.MainFrameHWND)
        except Exception:
            self.main_hwnd = None
        return msg

    def _keep(self):
        """Hand an Inventor we started to the keeper, so a restarted server finds it again."""
        try:
            k = keeper()
            if k is not None:
                k.Keep(self._app)
        except Exception:
            pass  # best effort: without the keeper, only a restart loses this Inventor

    def attach(self, pid: int) -> str:
        """Connect to an Inventor the keeper holds (one a previous server started)."""
        pythoncom.CoInitialize()
        k = keeper(start=False)
        for a in (k.Apps() if k is not None else ()):
            app = win32com.client.Dispatch(a)
            hwnd = int(app.MainFrameHWND)
            if win32process.GetWindowThreadProcessId(hwnd)[1] == pid:
                self._app, self.main_hwnd = app, hwnd
                return f"Connected to kept Inventor {app.SoftwareVersion.DisplayVersion} (pid {pid})"
        raise RuntimeError(f"The keeper holds no Inventor with pid {pid} — see list_instances.")

    @property
    def pid(self) -> int | None:
        """Inventor's process id — from the main window, so no COM call (any thread)."""
        try:
            return win32process.GetWindowThreadProcessId(self.main_hwnd)[1] if self.main_hwnd else None
        except Exception:
            return None

    def project_info(self) -> dict:
        p = self.app.DesignProjectManager.ActiveDesignProject
        return {"name": p.Name, "path": p.FullFileName}

    def activate_project(self, path: str) -> str:
        """Make an .ipj the active project. Inventor refuses while documents are open."""
        path = os.path.abspath(path)
        if not os.path.isfile(path):
            raise RuntimeError(f"Project file not found: {path}")
        pm = self.app.DesignProjectManager
        same = lambda p: os.path.normcase(p.FullFileName) == os.path.normcase(path)
        if same(pm.ActiveDesignProject):
            return f"Project {pm.ActiveDesignProject.Name} is already active."
        if self.app.Documents.Count:
            raise RuntimeError("Inventor only switches projects with no documents open — close them "
                               "first, or connect(new_instance=True, project=...) for a separate Inventor.")
        projects = pm.DesignProjects
        proj = next((p for p in (projects.Item(i) for i in range(1, projects.Count + 1)) if same(p)), None)
        if proj is None:
            proj = projects.AddExisting(path)
        proj.Activate(False)  # False: this session only, the default project stays the user's
        return f"Project {proj.Name} is active."

    def is_connected(self) -> bool:
        if self._app is None:
            return False
        try:
            _ = self._app.SoftwareVersion
            return True
        except Exception:
            self._app = None
            return False

    # ---------- Document management ----------

    def create_part(self, name: str | None = None, template: str = "metric") -> str:
        import glob

        templates_path = self.app.FileLocations.TemplatesPath

        if template.lower() in ("sheet_metal", "sheetmetal", "sm"):
            # Sheet Metal template (metric mm)
            candidates_patterns = [
                os.path.join(templates_path, "Metric", "Sheet Metal (mm).ipt"),
                os.path.join(templates_path, "**", "Sheet Metal (mm).ipt"),
                os.path.join(templates_path, "Sheet Metal.ipt"),
            ]
            template_file = None
            for pattern in candidates_patterns:
                if "*" in pattern:
                    found = glob.glob(pattern, recursive=True)
                    if found:
                        template_file = found[0]
                        break
                elif os.path.exists(pattern):
                    template_file = pattern
                    break
            if template_file is None:
                raise RuntimeError(f"Cannot find Sheet Metal template in {templates_path}")
        elif template.lower() == "metric":
            # Search for metric template (mm)
            candidates_patterns = [
                os.path.join(templates_path, "Metric", "Standard (mm).ipt"),
                os.path.join(templates_path, "Metric", "Standard.ipt"),
                os.path.join(templates_path, "**", "Standard (mm).ipt"),
            ]
            template_file = None
            for pattern in candidates_patterns:
                if "*" in pattern:
                    found = glob.glob(pattern, recursive=True)
                    if found:
                        template_file = found[0]
                        break
                elif os.path.exists(pattern):
                    template_file = pattern
                    break
            if template_file is None:
                # Fallback: any .ipt template
                fallback = glob.glob(os.path.join(templates_path, "**", "*.ipt"), recursive=True)
                if fallback:
                    template_file = fallback[0]
                else:
                    raise RuntimeError(f"Cannot find metric template in {templates_path}")
        elif template.lower() == "english":
            candidates_patterns = [
                os.path.join(templates_path, "en-US", "Standard.ipt"),
                os.path.join(templates_path, "Standard.ipt"),
                os.path.join(templates_path, "**", "Standard.ipt"),
            ]
            template_file = None
            for pattern in candidates_patterns:
                if "*" in pattern:
                    found = glob.glob(pattern, recursive=True)
                    if found:
                        template_file = found[0]
                        break
                elif os.path.exists(pattern):
                    template_file = pattern
                    break
            if template_file is None:
                raise RuntimeError(f"Cannot find English template in {templates_path}")
        else:
            # template is a direct file path
            template_file = template
            if not os.path.exists(template_file):
                raise RuntimeError(f"Template not found: {template_file}")

        doc = self.app.Documents.Add(12290, template_file, True)
        if name:
            try:
                doc.DisplayName = name
            except Exception:
                pass  # DisplayName may be read-only before first save
        # Documents.Add(..., True) opens the window but does not always make it the
        # ActiveDocument — later calls would then edit the previous document.
        doc.Activate()
        return f"Part created: {doc.DisplayName} (unsaved — use save_document(path))"

    def active_document(self):
        doc = self.app.ActiveDocument
        if doc is None:
            raise RuntimeError("No active document open in Inventor.")
        return doc

    def active_part(self):
        doc = self.active_document()
        if doc.DocumentType != 12290:  # kPartDocumentObject
            raise RuntimeError(
                f"Active document is not a part (type={doc.DocumentType})."
            )
        return doc.ComponentDefinition

    def active_assembly(self):
        doc = self.active_document()
        if doc.DocumentType != kAssemblyDocumentObject:
            raise RuntimeError(
                f"Active document is not an assembly (type={doc.DocumentType}). "
                "Call create_assembly() or activate an .iam document."
            )
        return doc.ComponentDefinition

    def _silently(self, fn):
        """Run fn with Inventor's SilentOperation on: no modal dialogs (e.g. the
        'save dependents?' prompt that otherwise blocks until a user clicks OK)."""
        prev = self.app.SilentOperation
        self.app.SilentOperation = True
        try:
            return fn()
        finally:
            self.app.SilentOperation = prev

    _EXTENSIONS = {12290: (".ipt",), 12291: (".iam",), 12292: (".idw", ".dwg"), 12293: (".ipn",)}

    def _check_save_path(self, doc, path: str, overwrite: bool) -> bool:
        """Raise a clear error for a path Inventor's SaveAs would reject with E_INVALIDARG.
        Returns True when path is the document's own file (save in place instead)."""
        folder, name = os.path.split(path)
        stem, ext = os.path.splitext(name)
        bad = sorted(set('<>:"|?*') & set(name))
        if bad or not stem:
            raise RuntimeError(f"Invalid file name {name!r}" + (f" — remove {' '.join(bad)}" if bad else ""))
        allowed = self._EXTENSIONS.get(doc.DocumentType)
        if allowed and ext.lower() not in allowed:
            raise RuntimeError(f"{doc.DisplayName} must be saved as {' or '.join(allowed)}, not {ext or 'no extension'}")
        if not os.path.isdir(folder):
            raise RuntimeError(f"Folder does not exist: {folder} (a '/' or '\\' in the name starts a folder)")
        if doc.FullFileName and os.path.normcase(doc.FullFileName) == os.path.normcase(path):
            return True
        if os.path.exists(path):
            other = next((d.DisplayName for d in self.app.Documents
                          if d.FullFileName and os.path.normcase(d.FullFileName) == os.path.normcase(path)), None)
            if other:
                raise RuntimeError(f"{path} is open in Inventor ({other}) — pick another name or close it first")
            if not overwrite:
                raise RuntimeError(f"{path} already exists — pick another name, or overwrite=True to replace it")
            os.remove(path)
        return False

    def library_folders(self) -> list[str]:
        """Folders whose files are never saved: the active project's library paths and
        Content Center files folder, and the application's Content Center files folder.
        Relative project paths are relative to the .ipj's folder."""
        folders, base = [], ""

        def add(p):
            if p and p.strip():
                folders.append(os.path.normcase(os.path.abspath(
                    os.path.join(base, os.path.expandvars(p.strip())))))
        try:
            proj = self.app.DesignProjectManager.ActiveDesignProject
            base = os.path.dirname(proj.FullFileName)
        except Exception:
            proj = None
        if proj is not None:
            try:
                paths = proj.LibraryPaths
                for i in range(1, paths.Count + 1):
                    add(paths.Item(i).Path)
            except Exception:
                pass
            try:
                add(proj.ContentCenterPath)
            except Exception:
                pass
        try:
            add(self.app.FileOptions.ContentCenterPath)
        except Exception:
            pass
        return folders

    @staticmethod
    def _library_reason(doc, folders, members: bool = True) -> str:
        """Why doc must not be saved ('' if it may be): a file in a library / Content Center
        folder, or (members=True) any Content Center member — custom-length ones included,
        which only Content Center itself should rewrite."""
        if members:
            try:
                if doc.ComponentDefinition.IsContentMember:
                    return "Content Center member"
            except Exception:
                pass
        name = os.path.normcase(os.path.abspath(doc.FullFileName)) if doc.FullFileName else ""
        for f in folders:
            if name and (name == f or name.startswith(f.rstrip("\\/") + os.sep)):
                return "library / Content Center folder"
        return ""

    def save_document(self, path: str | None = None, overwrite: bool = False,
                      save_dependents: bool = True) -> str:
        """Save the active document without any dialog — and never a library or Content
        Center file.

        Plain Document.Save() opens a modal 'save changes to dependents?' dialog
        whenever a referenced file was updated (e.g. an assembly whose
        sub-assembly re-evaluated), and the API call waits until a user answers.
        Save2(True) would answer it by saving EVERY dirty dependent — including
        standard Content Center parts Inventor marked dirty on opening an old assembly.
        So each dirty dependent outside the library folders is saved on its own with
        Save2(False), then the document itself with Save2(False); library files stay
        unsaved (Inventor recomputes them when it opens them again). Save2(True, list)
        would do the same in one call, but one failing file would fail them all.
        """
        doc = self.active_document()
        folders = self.library_folders()
        reason = self._library_reason(doc, folders, members=False)  # a custom member may be saved on purpose
        if reason:
            raise RuntimeError(f"{doc.DisplayName} is a library file ({reason}: {doc.FullFileName}) — "
                               "it is not saved. Save a copy elsewhere with save_document(path) if you "
                               "really changed it.")
        target = os.path.abspath(path) if path else None
        if target:
            name = os.path.normcase(target)
            if any(name.startswith(f.rstrip("\\/") + os.sep) for f in folders):
                raise RuntimeError(f"{target} is in a library / Content Center folder — save it elsewhere")
            if self._check_save_path(doc, target, overwrite):
                target = None  # its own file: save in place
        saved, skipped, failed = [], [], []
        if save_dependents:
            for d in doc.AllReferencedDocuments:
                if not d.Dirty:
                    continue
                why = self._library_reason(d, folders) or ("" if d.FullFileName else "never saved")
                if why:
                    skipped.append(f"{d.DisplayName} ({why})")
                    continue
                try:
                    self._silently(lambda: d.Save2(False))
                    saved.append(d.FullFileName)
                except Exception as e:
                    failed.append(f"{d.DisplayName}: {describe_com_error(e)}")
        if target:
            self._silently(lambda: doc.SaveAs(target, False))
            saved.insert(0, target)
            msg = f"Saved as: {target}"
        else:
            self._silently(lambda: doc.Save2(False))
            saved.insert(0, doc.FullFileName)
            msg = f"Saved: {doc.FullFileName}"
        if len(saved) > 1:
            msg += f"\nDependents saved ({len(saved) - 1}): " + ", ".join(saved[1:])
        if skipped:
            msg += f"\nNot saved ({len(skipped)}, library or new): " + ", ".join(skipped)
        if failed:
            msg += f"\nFailed ({len(failed)}): " + "; ".join(failed)
        if not save_dependents:
            dirty = [d.DisplayName for d in doc.AllReferencedDocuments if d.Dirty]
            if dirty:
                msg += f"\nDependents left unsaved (save_dependents=False): {', '.join(dirty)}"
        return msg

    def unsaved_documents(self) -> list[str]:
        """Names of open documents that were never saved to disk."""
        return [d.DisplayName for d in self.app.Documents if not d.FullFileName]

    def close_unsaved(self, include_active: bool = False) -> str:
        """Close never-saved documents without saving — not ones another document references."""
        active = self.app.ActiveDocument
        closed, kept = [], []
        for d in list(self.app.Documents):
            if d.FullFileName:
                continue
            if active is not None and d.InternalName == active.InternalName and not include_active:
                kept.append(f"{d.DisplayName} (active)")
            elif d.ReferencingDocuments.Count:
                kept.append(f"{d.DisplayName} (used by {d.ReferencingDocuments.Item(1).DisplayName})")
            else:
                closed.append(d.DisplayName)
                d.Close(True)
        msg = f"Closed {len(closed)} never-saved document(s)" + (f": {', '.join(closed)}" if closed else "")
        return msg + (f". Kept: {', '.join(kept)}" if kept else "")

    def list_documents(self) -> list[dict]:
        """Open (visible) documents, the active one marked."""
        active = self.app.ActiveDocument
        out = []
        for d in self.app.Documents.VisibleDocuments:
            out.append({"name": d.DisplayName, "path": d.FullFileName or "(unsaved)",
                        "type": {12290: "part", 12291: "assembly", 12292: "drawing"}.get(d.DocumentType, str(d.DocumentType)),
                        "dirty": bool(d.Dirty), "active": active is not None and d.InternalName == active.InternalName})
        return out

    def activate_document(self, name_or_path: str) -> str:
        """Make an open document active, by display name or full path."""
        key = name_or_path.lower()
        for d in self.app.Documents.VisibleDocuments:
            if key in (d.DisplayName.lower(), (d.FullFileName or "").lower()):
                d.Activate()
                return f"Active: {d.DisplayName}"
        names = ", ".join(d.DisplayName for d in self.app.Documents.VisibleDocuments)
        raise ValueError(f"No open document '{name_or_path}'. Open: {names}")

    def export_document(self, path: str, format: str = "STEP") -> str:
        doc = self.active_document()
        path = os.path.abspath(path)

        # Translator add-ins by their fixed class IDs. The display names change
        # between releases ("Autodesk Inventor STEP Translator" vs 2027's
        # "Translator: STEP"), so matching names silently found nothing.
        translators = {
            "STEP": "{90AF7F40-0C01-11D5-8E83-0010B541CD80}",
            "IGES": "{90AF7F44-0C01-11D5-8E83-0010B541CD80}",
            "SAT":  "{89162634-02B6-11D5-8E80-0010B541CD80}",
            "STL":  "{533E9A98-FC3B-11D4-8E7E-0010B541CD80}",   # STL *export*
        }
        format_upper = format.upper()
        if format_upper not in translators:
            raise ValueError(f"Unsupported format: {format}. Use STEP, STL, SAT, or IGES.")
        try:
            addin = self.app.ApplicationAddIns.ItemById(translators[format_upper])
        except Exception:
            raise RuntimeError(f"{format_upper} translator add-in is not installed")
        if not addin.Activated:
            addin.Activate()

        context = self.app.TransientObjects.CreateTranslationContext()
        context.Type = 13059  # kFileBrowseIOMechanism
        options = self.app.TransientObjects.CreateNameValueMap()
        data_medium = self.app.TransientObjects.CreateDataMedium()
        data_medium.FileName = path
        if os.path.exists(path):
            os.remove(path)
        self._silently(lambda: addin.SaveCopyAs(doc, context, options, data_medium))
        if not os.path.exists(path):
            raise RuntimeError(f"{format_upper} export reported success but wrote no file: {path}")
        return f"Exported to: {path} ({format_upper}, {os.path.getsize(path) // 1024} KB)"

    # ---------- Sketches ----------

    def create_sketch(
        self,
        plane: str = "XY",
        face_index: int | None = None,
        offset: float = 0,
    ) -> str:
        """Create a sketch on a work plane, a body face, or an OFFSET work plane.

        offset (mm): creates a hidden work plane parallel to 'plane' at this
        distance and sketches on it. Offset direction: positive = along the
        plane's normal (XY→+Z, XZ→+Y, YZ→+X). Essential for features that
        don't start at the origin planes (ribs, bosses at a height).
        """
        comp = self.active_part()

        if face_index is not None:
            # Create sketch on a body face
            body = comp.SurfaceBodies.Item(1)
            if face_index < 1 or face_index > body.Faces.Count:
                raise ValueError(
                    f"face_index={face_index} out of range. Body has {body.Faces.Count} faces."
                )
            face = body.Faces.Item(face_index)
            sketch = comp.Sketches.Add(face)
            return f"Sketch created on face {face_index} (Sketch{comp.Sketches.Count})"

        planes = {
            "XY": comp.WorkPlanes.Item(3),  # XY plane
            "XZ": comp.WorkPlanes.Item(2),  # XZ plane
            "YZ": comp.WorkPlanes.Item(1),  # YZ plane
        }
        wp = planes.get(plane.upper())
        if wp is None:
            raise ValueError(f"Unknown plane: {plane}. Use XY, XZ, or YZ.")

        if offset:
            wp = comp.WorkPlanes.AddByPlaneAndOffset(wp, offset / 10.0)
            wp.Visible = False
            sketch = comp.Sketches.Add(wp)
            return (f"Sketch created on {plane.upper()}{offset:+g}mm offset plane "
                    f"(Sketch{comp.Sketches.Count})")

        sketch = comp.Sketches.Add(wp)
        return f"Sketch created on {plane.upper()} plane (Sketch{comp.Sketches.Count})"

    def _get_sketch(self, sketch_index: int | None = None):
        comp = self.active_part()
        if sketch_index is not None:
            return comp.Sketches.Item(sketch_index)
        return comp.Sketches.Item(comp.Sketches.Count)

    def _tg(self):
        return self.app.TransientGeometry

    def draw_rectangle(
        self,
        x: float = 0, y: float = 0,
        width: float = 100, height: float = 50,
        sketch_index: int | None = None,
        corner_radius: float = 0,
    ) -> str:
        if corner_radius > 0:
            if corner_radius * 2 > min(width, height) + 1e-9:
                raise ValueError(f"corner_radius {corner_radius} is more than half of {min(width, height)}")
            w, h, r = width / 2, height / 2, corner_radius
            corners = [[x - w, y - h, r], [x + w, y - h, r], [x + w, y + h, r], [x - w, y + h, r]]
            if abs(corner_radius * 2 - min(width, height)) < 1e-9:
                return self.draw_slot(x, y, max(width, height), min(width, height),
                                      0 if width >= height else 90, sketch_index)
            self.draw_closed_profile(corners, sketch_index)
            return f"Rectangle {width}x{height}mm R{corner_radius} at ({x},{y})"
        sketch = self._get_sketch(sketch_index)
        tg = self._tg()
        # All dimensions in cm (Inventor internal units)
        w = width / 10.0
        h = height / 10.0
        cx = x / 10.0
        cy = y / 10.0
        sketch.SketchLines.AddAsTwoPointRectangle(
            tg.CreatePoint2d(cx - w / 2, cy - h / 2),
            tg.CreatePoint2d(cx + w / 2, cy + h / 2),
        )
        return f"Rectangle {width}x{height}mm at ({x},{y})"

    def draw_circle(
        self,
        x: float = 0, y: float = 0,
        diameter: float = 50,
        sketch_index: int | None = None,
        construction: bool = False,
    ) -> str:
        """Draw a circle with an automatic diameter dimension constraint.
        construction=True draws a reference (non-profile) circle, e.g. bolt PCD.
        """
        sketch = self._get_sketch(sketch_index)
        tg = self._tg()
        r = (diameter / 10.0) / 2.0
        cx = x / 10.0
        cy = y / 10.0
        circle = sketch.SketchCircles.AddByCenterRadius(
            tg.CreatePoint2d(cx, cy), r
        )
        if construction:
            circle.Construction = True

        # Add diameter dimension — text placed just outside the circle
        text_x = cx + r * 0.8
        text_y = cy + r * 0.6
        try:
            sketch.DimensionConstraints.AddDiameter(
                circle, tg.CreatePoint2d(text_x, text_y)
            )
        except Exception:
            pass  # dimension already exists or sketch is consumed

        suffix = " [construction]" if construction else ""
        return f"Circle D={diameter}mm at ({x},{y}){suffix}"

    def draw_line(
        self,
        x1: float = 0, y1: float = 0,
        x2: float = 100, y2: float = 0,
        sketch_index: int | None = None,
        construction: bool = False,
    ) -> str:
        sketch = self._get_sketch(sketch_index)
        tg = self._tg()
        line = sketch.SketchLines.AddByTwoPoints(
            tg.CreatePoint2d(x1 / 10.0, y1 / 10.0),
            tg.CreatePoint2d(x2 / 10.0, y2 / 10.0),
        )
        if construction:
            line.Construction = True
        suffix = " [construction]" if construction else ""
        return f"Line from ({x1},{y1}) to ({x2},{y2})mm{suffix}"

    def draw_polygon(
        self,
        x: float = 0, y: float = 0,
        radius: float = 25,
        sides: int = 6,
        sketch_index: int | None = None,
    ) -> str:
        import math
        sketch = self._get_sketch(sketch_index)
        tg = self._tg()
        r = radius / 10.0
        cx = x / 10.0
        cy = y / 10.0
        points = []
        for i in range(sides):
            angle = 2 * math.pi * i / sides - math.pi / 2
            px = cx + r * math.cos(angle)
            py = cy + r * math.sin(angle)
            points.append(tg.CreatePoint2d(px, py))
        # Draw connected lines — share endpoints for proper profile
        first_line = sketch.SketchLines.AddByTwoPoints(points[0], points[1])
        prev_line = first_line
        for i in range(2, sides):
            prev_line = sketch.SketchLines.AddByTwoPoints(
                prev_line.EndSketchPoint, points[i]
            )
        # Close the polygon
        sketch.SketchLines.AddByTwoPoints(
            prev_line.EndSketchPoint, first_line.StartSketchPoint
        )
        return f"Polygon {sides} sides, R={radius}mm at ({x},{y})"

    def draw_closed_profile(
        self,
        points: list,
        sketch_index: int | None = None,
    ) -> str:
        """Draw a closed profile: corners [x, y], rounded corners [x, y, r], arcs
        {"through": [x, y]} between corners (see profile_segments). mm. Segments share
        their end points, so Inventor sees one closed loop for extrude/revolve. Arcs are
        drawn through three points — no clockwise/counter-clockwise flag to get wrong on a
        sketch whose axes are flipped."""
        segs = profile_segments(points)
        sketch = self._get_sketch(sketch_index)
        arcs = self._draw_segments(sketch, segs)
        lines = len(segs) - arcs
        return f"Closed profile: {lines} line(s), {arcs} arc(s)"

    def _draw_segments(self, sketch, segs) -> int:
        """Draw a closed chain of segments (mm), each starting on the previous one's end
        point and the last one ending on the first one's start. Returns the arc count."""
        tg = self._tg()
        pt = lambda p: tg.CreatePoint2d(p[0] / 10.0, p[1] / 10.0)

        def nearest(entity, p):  # an arc's start and end may come out swapped
            a, b = entity.StartSketchPoint, entity.EndSketchPoint
            da = (a.Geometry.X * 10 - p[0]) ** 2 + (a.Geometry.Y * 10 - p[1]) ** 2
            db = (b.Geometry.X * 10 - p[0]) ** 2 + (b.Geometry.Y * 10 - p[1]) ** 2
            return a if da <= db else b

        first = cur = None
        prev = None
        arcs = 0
        for k, seg in enumerate(segs):
            start = cur if cur is not None else pt(seg[1])
            end = first if k == len(segs) - 1 else pt(seg[-1])
            if seg[0] == "line":
                ent = sketch.SketchLines.AddByTwoPoints(start, end)
            else:  # arc or fillet
                ent = sketch.SketchArcs.AddByThreePoints(start, pt(seg[2]), end)
                arcs += 1
            if first is None:
                first = nearest(ent, seg[1])
            cur = nearest(ent, seg[-1])
            if prev is not None and "fillet" in (prev[0], seg[0]) and "line" in (prev[0], seg[0]):
                # rounded corners stay tangent when the sketch is edited later. Only those:
                # the solver would bend an arc "through" a point to make it tangent — a
                # D shape collapsed to a point that way
                try:
                    sketch.GeometricConstraints.AddTangent(prev[1], ent)
                except Exception:
                    pass
            prev = (seg[0], ent)
        return arcs

    def draw_arc(self, x1: float, y1: float, x2: float, y2: float,
                 xm: float | None = None, ym: float | None = None,
                 cx: float | None = None, cy: float | None = None,
                 sketch_index: int | None = None) -> str:
        """An open arc from (x1, y1) to (x2, y2), mm: through (xm, ym), or around the
        center (cx, cy) counter-clockwise in sketch coordinates (start and end must be
        equally far from it)."""
        import math
        if xm is None or ym is None:
            if cx is None or cy is None:
                raise ValueError("Give a point on the arc (xm, ym) or its center (cx, cy)")
            r1, r2 = math.hypot(x1 - cx, y1 - cy), math.hypot(x2 - cx, y2 - cy)
            if abs(r1 - r2) > 0.01:
                raise ValueError(f"Start and end are {r1:.3f} and {r2:.3f} mm from the center — not one circle")
            a1 = math.atan2(y1 - cy, x1 - cx)
            a2 = math.atan2(y2 - cy, x2 - cx)
            sweep = (a2 - a1) % (2 * math.pi) or 2 * math.pi
            am = a1 + sweep / 2
            xm, ym = cx + r1 * math.cos(am), cy + r1 * math.sin(am)
        sketch = self._get_sketch(sketch_index)
        tg = self._tg()
        arc = sketch.SketchArcs.AddByThreePoints(tg.CreatePoint2d(x1 / 10, y1 / 10),
                                                 tg.CreatePoint2d(xm / 10, ym / 10),
                                                 tg.CreatePoint2d(x2 / 10, y2 / 10))
        return (f"Arc R{arc.Radius * 10:.3f} from ({x1},{y1}) to ({x2},{y2}), "
                f"center ({arc.CenterSketchPoint.Geometry.X * 10:.3f}, {arc.CenterSketchPoint.Geometry.Y * 10:.3f})")

    def draw_slot(self, x: float, y: float, length: float, width: float, angle: float = 0,
                  sketch_index: int | None = None) -> str:
        """A slot (two straight sides, round ends) centered on (x, y): overall length and
        width in mm, turned by angle degrees in the sketch."""
        import math
        if length <= width:
            raise ValueError("A slot is longer than it is wide — use draw_circle for length == width")
        a = math.radians(angle)
        d = (math.cos(a), math.sin(a))          # along the slot
        n = (-d[1], d[0])                       # across it
        h, r = (length - width) / 2, width / 2
        p = lambda s, t: [x + d[0] * s + n[0] * t, y + d[1] * s + n[1] * t]
        self.draw_closed_profile([p(-h, r), p(h, r), {"through": p(h + r, 0)},
                                  p(h, -r), p(-h, -r), {"through": p(-h - r, 0)}], sketch_index)
        return f"Slot {length}x{width}mm at ({x},{y}), {angle}°"

    def project_edges(self, face_index: int = 0, edge_indices: str = "", body_index: int = 1,
                      sketch_index: int | None = None) -> str:
        """Project edges of a body onto the sketch — exact lines, arcs, circles and splines,
        linked to the model. face_index: all edges of that face; edge_indices: "3,7,8"."""
        sketch = self._get_sketch(sketch_index)
        body = self.active_part().SurfaceBodies.Item(body_index)
        if face_index:
            edges = body.Faces.Item(face_index).Edges
            edges = [edges.Item(i) for i in range(1, edges.Count + 1)]
        elif edge_indices:
            edges = [body.Edges.Item(int(s)) for s in edge_indices.replace(" ", "").split(",") if s]
        else:
            raise ValueError("Give face_index or edge_indices (list_faces / list_edges show them)")
        kinds = {}
        for e in edges:
            kind = self._curve_kind(sketch.AddByProjectingEntity(e))
            kinds[kind] = kinds.get(kind, 0) + 1
        return f"Projected {len(edges)} edge(s): " + ", ".join(f"{v} {k}" for k, v in sorted(kinds.items()))

    @staticmethod
    def _curve_kind(entity) -> str:
        """line / arc / circle / curve for a sketch entity, by the properties it has."""
        for prop, kind in (("SweepAngle", "arc"), ("Radius", "circle")):
            try:
                getattr(entity, prop)
                return kind
            except Exception:
                pass
        try:
            entity.Geometry.Direction
            return "line"
        except Exception:
            return "curve"

    def faceting(self, body) -> int:
        """How many vertices join two straight edges at a shallow angle (under 20°) — the
        trace of a curve drawn as many short lines. A designed part has almost none."""
        import math
        ends = {}
        for i in range(1, body.Edges.Count + 1):
            e = body.Edges.Item(i)
            if e.GeometryType != 5123:  # kLineSegmentCurve
                continue
            a, b = e.StartVertex.Point, e.StopVertex.Point
            pa, pb = (a.X, a.Y, a.Z), (b.X, b.Y, b.Z)
            for p, q in ((pa, pb), (pb, pa)):
                ends.setdefault(tuple(round(v, 5) for v in p), []).append((q[0] - p[0], q[1] - p[1], q[2] - p[2]))
        def turn(u, v):  # degrees the outline turns at the vertex: 0 = straight on
            nu, nv = math.sqrt(sum(c * c for c in u)), math.sqrt(sum(c * c for c in v))
            if nu < 1e-9 or nv < 1e-9:
                return 90.0
            return 180 - math.degrees(math.acos(max(-1, min(1, sum(a * b for a, b in zip(u, v)) / (nu * nv)))))

        # a vertex of a solid joins three edges (two of the outline, one across the thickness):
        # look at every pair
        return sum(1 for dirs in ends.values()
                   if any(0.5 < turn(dirs[i], dirs[j]) < 20
                          for i in range(len(dirs)) for j in range(i + 1, len(dirs))))


    # ---------- Sketch patterns, checks, offsets ----------

    _CURVES = ("SketchLines", "SketchArcs", "SketchCircles", "SketchEllipses",
               "SketchEllipticalArcs", "SketchSplines")

    def _sketch_curves(self, sketch) -> list[tuple[str, object]]:
        """(collection name, entity) of every curve that isn't construction geometry."""
        out = []
        for coll in self._CURVES:
            try:
                c = getattr(sketch, coll)
            except Exception:
                continue
            for i in range(1, c.Count + 1):
                e = c.Item(i)
                if not e.Construction:
                    out.append((coll, e))
        return out

    @staticmethod
    def _key(point) -> tuple:
        g = point.Geometry
        return (round(g.X * 10, 3), round(g.Y * 10, 3))

    def _solid_profile(self, sketch):
        """sketch.Profiles.AddForSolid(), with what is wrong with the sketch when it fails."""
        try:
            return sketch.Profiles.AddForSolid()
        except Exception as e:
            raise RuntimeError(f"No closed profile in {sketch.Name}: {self._sketch_problems(sketch)}") from e

    def _sketch_problems(self, sketch) -> str:
        info = self.check_sketch_of(sketch, try_profile=False)
        return "; ".join(info["problems"]) or "no open ends found — maybe nothing is drawn"

    def check_sketch(self, sketch_index: int | None = None) -> dict:
        return self.check_sketch_of(self._get_sketch(sketch_index))

    def check_sketch_of(self, sketch, try_profile: bool = True) -> dict:
        """What a profile needs, checked: every end point shared by exactly two curves.
        Open ends, near misses (two ends a hair apart), branches and doubled lines, with
        coordinates in sketch mm; then whether Inventor builds a profile, and how many loops."""
        import math
        curves = self._sketch_curves(sketch)
        ends, counts, lines = {}, {}, {}
        for coll, e in curves:
            counts[coll] = counts.get(coll, 0) + 1
            if coll in ("SketchCircles", "SketchEllipses"):
                continue  # closed by themselves
            try:
                a, b = self._key(e.StartSketchPoint), self._key(e.EndSketchPoint)
            except Exception:
                continue
            for k in (a, b):
                ends[k] = ends.get(k, 0) + 1
            if coll == "SketchLines":
                lines[tuple(sorted((a, b)))] = lines.get(tuple(sorted((a, b))), 0) + 1
        open_ends = [k for k, n in ends.items() if n == 1]
        problems = []
        near = set()
        for i, p in enumerate(open_ends):
            for q in open_ends[i + 1:]:
                d = math.dist(p, q)
                if d < 1.0:
                    problems.append(f"gap of {d:.3f} mm between {p} and {q} — not joined")
                    near.update((p, q))
        lone = [p for p in open_ends if p not in near]
        if lone:
            problems.append(f"{len(lone)} open end(s): " + ", ".join(map(str, lone[:10]))
                            + (" …" if len(lone) > 10 else ""))
        branches = [k for k, n in ends.items() if n > 2]
        if branches:
            problems.append(f"{len(branches)} point(s) where 3+ curves meet (overlap or a stray line): "
                            + ", ".join(map(str, branches[:10])))
        doubled = [k for k, n in lines.items() if n > 1]
        if doubled:
            problems.append(f"{len(doubled)} line(s) drawn twice: " + ", ".join(f"{a}-{b}" for a, b in doubled[:5]))
        info = {"sketch": sketch.Name,
                "curves": {k.replace("Sketch", "").lower(): v for k, v in counts.items()},
                "problems": problems}
        if try_profile:
            try:
                profile = sketch.Profiles.AddForSolid()
                info["profile"] = f"ok — {profile.Count} closed loop(s)"
                try:
                    profile.Delete()
                except Exception:
                    pass
            except Exception:
                info["profile"] = "Inventor can't build a profile from this sketch"
        return info

    def sketch_pattern(self, shape: str = "circle", diameter: float = 0, length: float = 0,
                       width: float = 0, corner_radius: float = 0, angle: float = 0,
                       x: float = 0, y: float = 0, rows: int = 1, columns: int = 1,
                       dx: float = 0, dy: float = 0, count: int = 0, pcd: float = 0,
                       start_angle: float = 0, sketch_index: int | None = None) -> str:
        """Many copies of one shape in one sketch, mm: a grid (first one at x, y; rows x
        columns, pitch dx / dy) or, with count, a circle of them (center x, y, diameter pcd,
        from start_angle; each copy turned with the circle)."""
        import math
        shape = shape.lower()
        if shape not in ("circle", "slot", "rectangle"):
            raise ValueError("shape is circle, slot or rectangle")
        if shape == "circle" and diameter <= 0:
            raise ValueError("circle needs diameter")
        if shape != "circle" and (length <= 0 or width <= 0):
            raise ValueError(f"{shape} needs length and width")
        if count:
            if pcd <= 0:
                raise ValueError("a circular pattern needs pcd (the diameter it runs on)")
            places = [(x + pcd / 2 * math.cos(math.radians(start_angle + 360 * k / count)),
                       y + pcd / 2 * math.sin(math.radians(start_angle + 360 * k / count)),
                       start_angle + 360 * k / count) for k in range(count)]
        else:
            if (rows > 1 and not dy) or (columns > 1 and not dx):
                raise ValueError("a grid needs dx (columns) and dy (rows)")
            places = [(x + c * dx, y + r * dy, 0.0) for r in range(rows) for c in range(columns)]
        if len(places) > 2000:
            raise ValueError(f"{len(places)} copies — more than 2000; use a feature pattern instead")
        sketch = self._get_sketch(sketch_index)
        tg = self._tg()
        sketch.DeferUpdates = True  # no solve after every curve: much faster for many copies
        try:
            self._pattern_copies(sketch, tg, places, shape, diameter, length, width,
                                 corner_radius, angle, sketch_index)
        finally:
            sketch.DeferUpdates = False
        return f"{len(places)} {shape}(s) in {sketch.Name}" + (f" on PCD {pcd}" if count else f" ({rows} x {columns})")

    def _pattern_copies(self, sketch, tg, places, shape, diameter, length, width,
                        corner_radius, angle, sketch_index):
        import math
        for px, py, turn in places:
            if shape == "circle":
                sketch.SketchCircles.AddByCenterRadius(tg.CreatePoint2d(px / 10, py / 10), diameter / 20)
            elif shape == "slot":
                self.draw_slot(px, py, length, width, angle + turn, sketch_index)
            else:
                a = math.radians(angle + turn)
                c, s = math.cos(a), math.sin(a)
                corner = lambda u, v: [px + u * c - v * s, py + u * s + v * c] + ([corner_radius] if corner_radius else [])
                self.draw_closed_profile([corner(-length / 2, -width / 2), corner(length / 2, -width / 2),
                                          corner(length / 2, width / 2), corner(-length / 2, width / 2)],
                                         sketch_index)

    def offset_profile(self, distance: float, side: str = "inside",
                       sketch_index: int | None = None) -> str:
        """Offset every closed loop of the sketch by distance mm, inside or outside."""
        side = side.lower()
        if side not in ("inside", "outside"):
            raise ValueError("side is inside or outside")
        sketch = self._get_sketch(sketch_index)
        loops = self._loops(sketch)
        if not loops:
            raise RuntimeError(f"No closed loop in {sketch.Name} to offset")
        done = []
        for loop in loops:
            before = self._span(loop)
            for natural in (True, False):
                # one curve of the loop + IncludeConnectedEntities: Inventor refuses the whole
                # loop handed over curve by curve ("Failed to create offset")
                coll = self.app.TransientObjects.CreateObjectCollection()
                coll.Add(loop[0])
                made = list(sketch.OffsetSketchEntitiesUsingDistance(coll, distance / 10, natural, True, False))
                after = self._span(made)
                if (after < before) == (side == "inside"):
                    done.append(len(made))
                    break
                for e in made:
                    e.Delete()
            else:
                raise RuntimeError("Inventor couldn't offset a loop to the requested side")
        return f"Offset {len(done)} loop(s) {distance} mm {side} ({sum(done)} curve(s))"

    def _loops(self, sketch) -> list[list]:
        """Closed loops of the sketch's curves, by shared end points (circles alone)."""
        curves = [e for coll, e in self._sketch_curves(sketch)]
        loops, rest = [], []
        for e in curves:
            try:
                e.StartSketchPoint
                rest.append(e)
            except Exception:
                loops.append([e])  # circle / ellipse
        while rest:
            loop = [rest.pop()]
            keys = {self._key(loop[0].StartSketchPoint), self._key(loop[0].EndSketchPoint)}
            grew = True
            while grew:
                grew = False
                for e in list(rest):
                    ks = {self._key(e.StartSketchPoint), self._key(e.EndSketchPoint)}
                    if ks & keys:
                        loop.append(e)
                        rest.remove(e)
                        keys |= ks
                        grew = True
            loops.append(loop)
        return loops

    def _span(self, entities) -> float:
        """Width + height of the entities' points (circles: center ± radius), mm."""
        xs, ys = [], []
        for e in entities:
            try:
                for p in (e.StartSketchPoint, e.EndSketchPoint):
                    xs.append(p.Geometry.X); ys.append(p.Geometry.Y)
            except Exception:
                pass
            try:
                c, r = e.CenterSketchPoint.Geometry, e.Radius
                xs += [c.X - r, c.X + r]; ys += [c.Y - r, c.Y + r]
            except Exception:
                pass
        return (max(xs) - min(xs) + max(ys) - min(ys)) * 10 if xs else 0.0

    # ---------- Features ----------

    def extrude(
        self,
        distance: float = 10,
        direction: str = "positive",
        operation: str = "join",
        sketch_index: int | None = None,
        profile_index: int = 1,
        extent_type: str = "distance",
    ) -> str:
        comp = self.active_part()
        sketch = self._get_sketch(sketch_index)
        profile = self._solid_profile(sketch)

        dir_map = {
            "positive": kPositiveExtentDirection,
            "negative": kNegativeExtentDirection,
            "symmetric": kSymmetricExtentDirection,
        }
        op_map = {
            "join": kJoinOperation,
            "cut": kCutOperation,
            "intersect": kIntersectOperation,
            "surface": kSurfaceOperation,
            "new": kNewBodyOperation,
        }

        direction_enum = dir_map.get(direction.lower(), kPositiveExtentDirection)
        operation_enum = op_map.get(operation.lower(), kJoinOperation)
        before = self._vol_state()

        if extent_type.lower() == "all":
            feature = comp.Features.ExtrudeFeatures.AddByThroughAllExtent(
                profile, direction_enum, operation_enum
            )
            return f"Extrude through-all {direction} ({operation}): {feature.Name}{self._fmt_delta(before)}"
        else:
            dist_cm = distance / 10.0
            feature = comp.Features.ExtrudeFeatures.AddByDistanceExtent(
                profile, dist_cm, direction_enum, operation_enum
            )
            return f"Extrude {distance}mm {direction} ({operation}): {feature.Name}{self._fmt_delta(before)}"

    def revolve(
        self,
        angle: float = 360,
        sketch_index: int | None = None,
        axis: str = "Y",
        operation: str = "join",
        axis_line_index: int | None = None,
    ) -> str:
        """Revolve a sketch profile around an axis.

        axis: "X" / "Y" / "Z"  — use a named WorkAxis (preferred, no construction line needed)
        axis_line_index: sketch line index (legacy); overrides axis when provided.
        """
        import math
        comp = self.active_part()
        sketch = self._get_sketch(sketch_index)
        profile = self._solid_profile(sketch)

        # Resolve axis entity: named WorkAxis (preferred) or sketch line (legacy)
        _work_axis_map = {"X": 1, "Y": 2, "Z": 3}
        if axis_line_index is not None:
            # Explicit keyword: legacy sketch-line mode
            axis_entity = sketch.SketchLines.Item(axis_line_index)
            axis_label = f"sketch line {axis_line_index}"
        elif isinstance(axis, str) and axis.upper() in _work_axis_map:
            # Named work axis — preferred, no construction line needed
            axis_entity = comp.WorkAxes.Item(_work_axis_map[axis.upper()])
            axis_label = f"{axis.upper()} work axis"
        elif isinstance(axis, int):
            # Backward compat: integer passed positionally from old server.py
            axis_entity = sketch.SketchLines.Item(axis)
            axis_label = f"sketch line {axis}"
        else:
            axis_entity = sketch.SketchLines.Item(1)
            axis_label = "sketch line 1"

        op_map = {
            "join": kJoinOperation,
            "cut": kCutOperation,
            "intersect": kIntersectOperation,
            "new": kNewBodyOperation,
        }
        operation_enum = op_map.get(operation.lower(), kJoinOperation)

        revolve_features = comp.Features.RevolveFeatures
        before = self._vol_state()

        if angle >= 360:
            revolve_features.AddFull(profile, axis_entity, operation_enum)
        else:
            angle_rad = math.radians(angle)
            revolve_features.AddByAngle(
                profile, axis_entity, angle_rad,
                kPositiveExtentDirection, operation_enum
            )

        return f"Revolve {angle}° around {axis_label} ({operation}){self._fmt_delta(before)}"

    def fillet(self, radius: float = 2, edge_indices: list[int] | None = None) -> str:
        comp = self.active_part()
        r_cm = radius / 10.0

        edges_collection = self.app.TransientObjects.CreateEdgeCollection()
        body = comp.SurfaceBodies.Item(1)

        if edge_indices:
            for idx in edge_indices:
                edges_collection.Add(body.Edges.Item(idx))
        else:
            edges_collection.Add(body.Edges.Item(1))

        before = self._vol_state()
        fillet_def = comp.Features.FilletFeatures.CreateFilletDefinition()
        fillet_def.AddConstantRadiusEdgeSet(edges_collection, r_cm)
        comp.Features.FilletFeatures.Add(fillet_def)
        return f"Fillet R={radius}mm on {edges_collection.Count} edge(s){self._fmt_delta(before)}"

    def chamfer(self, distance: float = 2, edge_indices: list[int] | None = None) -> str:
        """Add equal-distance chamfer to edges.
        Inventor 2026 API: ChamferFeatures.AddUsingDistance(EdgeCollection, dist_cm)
        NOT CreateChamferDefinition (removed in 2026).
        Requires EdgeCollection (not ObjectCollection).
        """
        comp = self.active_part()
        d_cm = distance / 10.0

        ec = self.app.TransientObjects.CreateEdgeCollection()
        body = comp.SurfaceBodies.Item(1)

        if edge_indices:
            for idx in edge_indices:
                ec.Add(body.Edges.Item(idx))
        else:
            ec.Add(body.Edges.Item(1))

        before = self._vol_state()
        comp.Features.ChamferFeatures.AddUsingDistance(ec, d_cm)
        return f"Chamfer {distance}mm on {ec.Count} edge(s){self._fmt_delta(before)}"

    # Standard ISO metric coarse thread pitches (mm)
    _METRIC_COARSE_PITCH = {
        1: 0.25, 1.2: 0.25, 1.4: 0.3, 1.6: 0.35, 2: 0.4, 2.5: 0.45,
        3: 0.5, 3.5: 0.6, 4: 0.7, 5: 0.8, 6: 1.0, 8: 1.25, 10: 1.5,
        12: 1.75, 14: 2.0, 16: 2.0, 18: 2.5, 20: 2.5, 22: 2.5, 24: 3.0,
        27: 3.0, 30: 3.5, 33: 3.5, 36: 4.0, 39: 4.0, 42: 4.5, 45: 4.5,
        48: 5.0, 52: 5.0, 56: 5.5, 60: 5.5, 64: 6.0,
    }

    def hole(
        self,
        x: float = 0, y: float = 0,
        diameter: float = 10,
        depth: float = 0,
        tapped: bool = False,
        pitch: float | None = None,
    ) -> str:
        """Create a proper Inventor HoleFeature using native HoleFeatures API.
        depth=0 means through-all (default).
        tapped=True creates a tapped (threaded) hole with ISO Metric profile.
        pitch: thread pitch in mm (auto-selected from standard coarse series if None).
        Uses CreateSketchPlacementDefinition(ObjectCollection_of_SketchPoints) API.
        """
        comp = self.active_part()
        tg = self._tg()
        hf = comp.Features.HoleFeatures
        before = self._vol_state()

        # Create sketch on XY work plane for hole center point
        wp = comp.WorkPlanes.Item(3)  # XY plane
        sketch = comp.Sketches.Add(wp)
        pt = sketch.SketchPoints.Add(tg.CreatePoint2d(x / 10.0, y / 10.0))

        # Create ObjectCollection with the SketchPoint (required by API)
        col = self.app.TransientObjects.CreateObjectCollection()
        col.Add(pt)

        # Create sketch-based placement definition
        placement = hf.CreateSketchPlacementDefinition(col)

        diameter_cm = diameter / 10.0

        # Build second argument: TapInfo for tapped holes, float diameter for drilled holes
        if tapped:
            # Determine pitch (standard coarse if not given)
            if pitch is None:
                dia_int = int(round(diameter))
                pitch = self._METRIC_COARSE_PITCH.get(dia_int)
                if pitch is None:
                    raise ValueError(
                        f"No standard coarse pitch for M{dia_int}. "
                        "Pass pitch= explicitly."
                    )
            # Format designation: "M16x2" (not "M16x2.0")
            dia_int = int(round(diameter))
            pitch_str = str(int(pitch)) if pitch == int(pitch) else str(pitch)
            designation = f"M{dia_int}x{pitch_str}"
            tap_info = hf.CreateTapInfo(
                True,                   # RightHanded
                "ISO Metric profile",   # ThreadType (exact string from thread.xlsx)
                designation,            # ThreadDesignation e.g. "M16x2"
                "6H",                   # Class (standard internal metric)
                True,                   # FullTapDepth
            )
            hole_arg = tap_info
            type_str = f"M{dia_int} tapped (pitch {pitch}mm, 6H)"
        else:
            hole_arg = diameter_cm
            type_str = f"drilled D={diameter}mm"

        def add(arg, dist):
            if depth <= 0:
                return hf.AddDrilledByThroughAllExtent(placement, arg, kNegativeExtentDirection)
            return hf.AddDrilledByDistanceExtent(placement, arg, dist, kNegativeExtentDirection)

        extent_str = "through-all" if depth <= 0 else f"{depth}mm deep"
        try:
            feature = add(hole_arg, depth / 10.0)
        except Exception:
            # some parts (Content Center members) reject plain numbers — values with units work
            if tapped:
                raise
            feature = add(f"{diameter} mm", f"{depth} mm")

        self.active_document().Update()
        after = self._vol_state()
        if before and after and after[0] >= before[0] - 1e-3:
            # the feature exists but cut nothing (e.g. the point is off the body, or the
            # feature failed) — don't leave it and its sketch behind
            feature.Delete()
            try:
                sketch.Delete()
            except Exception:
                pass
            raise RuntimeError(f"Hole at ({x},{y}) removed no material — the point is sketched on "
                               "the XY plane and drills into +Z: is it inside the body's outline, "
                               "with the body on the +Z side? Feature deleted. hole_linear places "
                               "by a face instead.")

        # --- Add position dimensions in a separate sketch (after hole creation) ---
        # HoleFeature placement sketch must have ONLY SketchPoints — construction lines
        # cannot be in the same sketch. So we create a dedicated dimension sketch.
        self._add_hole_position_dims(comp, x, y)

        return f"Hole {type_str} {extent_str} at ({x},{y}): {feature.Name}{self._fmt_delta(before)}"

    def _add_hole_position_dims(self, comp, x: float, y: float) -> None:
        """Add a dimension sketch showing the hole center position from origin.
        Uses a separate sketch with construction line(s) to allow AddTwoPointDistance.
        Only adds dimensions if hole is not at origin.
        """
        tg = self._tg()
        x_cm = x / 10.0
        y_cm = y / 10.0

        if abs(x) < 0.01 and abs(y) < 0.01:
            return  # hole at origin — nothing to dimension

        wp = comp.WorkPlanes.Item(3)  # XY plane
        dim_sketch = comp.Sketches.Add(wp)
        dc = dim_sketch.DimensionConstraints

        if abs(y) < 0.01:
            # Hole on X axis — one horizontal construction line
            line = dim_sketch.SketchLines.AddByTwoPoints(
                tg.CreatePoint2d(0.0, 0.0),
                tg.CreatePoint2d(x_cm, 0.0)
            )
            line.Construction = True
            text_pt = tg.CreatePoint2d(x_cm / 2, -1.5)
            dc.AddTwoPointDistance(line.StartSketchPoint, line.EndSketchPoint,
                                   kHorizontalDim, text_pt)
        elif abs(x) < 0.01:
            # Hole on Y axis — one vertical construction line
            line = dim_sketch.SketchLines.AddByTwoPoints(
                tg.CreatePoint2d(0.0, 0.0),
                tg.CreatePoint2d(0.0, y_cm)
            )
            line.Construction = True
            text_pt = tg.CreatePoint2d(-1.5, y_cm / 2)
            dc.AddTwoPointDistance(line.StartSketchPoint, line.EndSketchPoint,
                                   kVerticalDim, text_pt)
        else:
            # Hole at (x, y) — two construction lines: H and V legs
            h_line = dim_sketch.SketchLines.AddByTwoPoints(
                tg.CreatePoint2d(0.0, 0.0),
                tg.CreatePoint2d(x_cm, 0.0)
            )
            h_line.Construction = True
            v_line = dim_sketch.SketchLines.AddByTwoPoints(
                tg.CreatePoint2d(x_cm, 0.0),
                tg.CreatePoint2d(x_cm, y_cm)
            )
            v_line.Construction = True
            # Horizontal dim (X distance from center)
            dc.AddTwoPointDistance(h_line.StartSketchPoint, h_line.EndSketchPoint,
                                   kHorizontalDim, tg.CreatePoint2d(x_cm / 2, -1.5))
            # Vertical dim (Y distance)
            dc.AddTwoPointDistance(v_line.StartSketchPoint, v_line.EndSketchPoint,
                                   kVerticalDim, tg.CreatePoint2d(x_cm + 1.5, y_cm / 2))

    # ---------- Diagnostics / token-saving helpers ----------

    @staticmethod
    def _fresh_mass(doc, comp):
        """MassProperties recomputed now. An assembly otherwise returns the value cached in
        the file — stale after its sub-assemblies changed (3607 kg vs 3343 kg seen)."""
        try:
            doc.Update()
        except Exception:
            pass
        mp = comp.MassProperties
        for prop, value in (("CacheResultsOnCompute", False), ("Accuracy", 25603)):  # kHigh
            try:
                setattr(mp, prop, value)
            except Exception:
                pass
        return mp

    def _vol_state(self):
        """(volume_mm3, faces, edges) of body 1, or None if no body."""
        try:
            comp = self.active_part()
            mp = comp.MassProperties
            body = comp.SurfaceBodies.Item(1)
            return (mp.Volume * 1000, body.Faces.Count, body.Edges.Count)
        except Exception:
            return None

    def _fmt_delta(self, before):
        """Compact ' | V 35271 (−7854) | F7 E16' suffix comparing to 'before' state."""
        after = self._vol_state()
        if not after:
            return ""
        if not before:
            # first feature — no 'before' body existed
            return f" | V {after[0]:.0f} mm³ | F{after[1]} E{after[2]}"
        dv = after[0] - before[0]
        return f" | V {after[0]:.0f} ({dv:+.0f} mm³) | F{after[1]} E{after[2]}"

    def inspect(self) -> dict:
        """Compact one-call snapshot of the active part."""
        doc = self.active_document()
        comp = doc.ComponentDefinition
        info: dict = {"document": doc.DisplayName}
        try:
            body = comp.SurfaceBodies.Item(1)
            rb = body.RangeBox
            mp = comp.MassProperties
            info["volume_mm3"] = round(mp.Volume * 1000)
            info["bbox_mm"] = {
                "x": [round(rb.MinPoint.X * 10, 2), round(rb.MaxPoint.X * 10, 2)],
                "y": [round(rb.MinPoint.Y * 10, 2), round(rb.MaxPoint.Y * 10, 2)],
                "z": [round(rb.MinPoint.Z * 10, 2), round(rb.MaxPoint.Z * 10, 2)],
            }
            info["faces"] = body.Faces.Count
            info["edges"] = body.Edges.Count
            shallow = self.faceting(body)
            if shallow >= 8:
                info["warning"] = (f"{shallow} shallow kinks between straight edges — curves look drawn as "
                                   "many short lines. Use arcs: draw_closed_profile radii / through, "
                                   "draw_arc, draw_slot, draw_rectangle(corner_radius), project_edges.")
        except Exception:
            info["body"] = "none"
        feats = []
        for i in range(1, comp.Features.Count + 1):
            f = comp.Features.Item(i)
            nm = f.Name
            if getattr(f, "Suppressed", False):
                nm += " [suppressed]"
            health = unhealthy(self.app, f)
            if health:
                nm += f" [{health}]"
            feats.append(f"[{i}] {nm}")
        info["features"] = feats
        info["sketches"] = [comp.Sketches.Item(i).Name for i in range(1, comp.Sketches.Count + 1)]
        try:
            info["sheet_metal_thickness"] = str(comp.ActiveSheetMetalStyle.Thickness)
        except Exception:
            pass
        return info

    def list_edges(self, body_index: int = 1, min_length: float = 0, max_count: int = 60) -> str:
        """Compact edge table: '[i] L=200.0 mid=(0.0,-225.0,0.8)' one per line.
        min_length filters out tiny edges (mm)."""
        body = self.active_part().SurfaceBodies.Item(body_index)
        lines = []
        for i in range(1, body.Edges.Count + 1):
            try:
                e = body.Edges.Item(i)
                sv, ev = e.StartVertex.Point, e.StopVertex.Point
                L = ((ev.X - sv.X) ** 2 + (ev.Y - sv.Y) ** 2 + (ev.Z - sv.Z) ** 2) ** 0.5 * 10
                if L < min_length:
                    continue
                mx, my, mz = (sv.X + ev.X) / 2 * 10, (sv.Y + ev.Y) / 2 * 10, (sv.Z + ev.Z) / 2 * 10
                lines.append(f"[{i}] L={L:.1f} mid=({mx:.1f},{my:.1f},{mz:.1f})")
            except Exception:
                lines.append(f"[{i}] (closed curve)")
            if len(lines) >= max_count:
                lines.append(f"... truncated at {max_count}")
                break
        return "\n".join(lines) if lines else "(no edges match)"

    def list_faces(self, body_index: int = 1, min_area: float = 0, max_count: int = 60) -> str:
        """Compact face table: '[i] Plane A=90000 c=(0.0,225.0,12.5)' one per line.
        min_area in mm²."""
        type_names = {5890: "Plane", 5891: "Cyl", 5892: "EllCyl", 5893: "Cone",
                      5894: "EllCone", 5895: "Torus", 5896: "Sphere", 5897: "BSpline"}
        body = self.active_part().SurfaceBodies.Item(body_index)
        lines = []
        for i in range(1, body.Faces.Count + 1):
            try:
                f = body.Faces.Item(i)
                a = f.Evaluator.Area * 100
                if a < min_area:
                    continue
                xs, ys, zs = [], [], []
                for v in range(1, f.Vertices.Count + 1):
                    p = f.Vertices.Item(v).Point
                    xs.append(p.X * 10); ys.append(p.Y * 10); zs.append(p.Z * 10)
                t = type_names.get(f.SurfaceType, str(f.SurfaceType))
                if xs:
                    c = f"c=({sum(xs)/len(xs):.1f},{sum(ys)/len(ys):.1f},{sum(zs)/len(zs):.1f})"
                else:
                    c = "c=(closed)"
                lines.append(f"[{i}] {t} A={a:.0f} {c}")
            except Exception:
                lines.append(f"[{i}] ?")
            if len(lines) >= max_count:
                lines.append(f"... truncated at {max_count}")
                break
        return "\n".join(lines) if lines else "(no faces match)"

    # ---------- Transactions (rollback support) ----------

    def transaction(self, action: str = "begin", name: str = "MCP batch") -> str:
        """begin: start an undo-transaction wrapping subsequent operations.
        commit: finalize. abort: roll back EVERYTHING since begin (one undo unit)."""
        action = action.lower()
        if action == "begin":
            if getattr(self, "_txn", None) is not None:
                return "Transaction already open — commit or abort it first"
            self._txn = self.app.TransactionManager.StartTransaction(
                self.app.ActiveDocument, name)
            return f"Transaction '{name}' started"
        if action == "commit":
            if getattr(self, "_txn", None) is None:
                return "No open transaction"
            self._txn.End()
            self._txn = None
            return "Transaction committed"
        if action == "abort":
            if getattr(self, "_txn", None) is None:
                return "No open transaction"
            self._txn.Abort()
            self._txn = None
            return "Transaction aborted — all changes since 'begin' rolled back"
        return f"Unknown action '{action}'. Use begin / commit / abort."

    def _plane_face(self, axis: str, value: float, body_index: int = 1, tol: float = 0.1):
        """Largest planar face whose vertices ALL lie at <axis>==value (mm).
        Re-resolve after every feature — face objects/indices go stale."""
        body = self.active_part().SurfaceBodies.Item(body_index)
        best_f, best_a = None, 0
        for i in range(1, body.Faces.Count + 1):
            f = body.Faces.Item(i)
            if f.SurfaceType != 5890:  # plane
                continue
            ok = True
            for v in range(1, f.Vertices.Count + 1):
                p = f.Vertices.Item(v).Point
                c = {"x": p.X, "y": p.Y, "z": p.Z}[axis.lower()] * 10
                if abs(c - value) > tol:
                    ok = False
                    break
            if ok:
                a = f.Evaluator.Area
                if a > best_a:
                    best_a, best_f = a, f
        if best_f is None:
            raise ValueError(f"No planar face at {axis}={value}mm")
        return best_f

    def _face_edge(self, face, axis: str, value: float, tol: float = 0.1):
        """Edge of a face whose BOTH vertices lie at <axis>==value (mm)."""
        for i in range(1, face.Edges.Count + 1):
            e = face.Edges.Item(i)
            sv, ev = e.StartVertex.Point, e.StopVertex.Point
            cs = {"x": sv.X, "y": sv.Y, "z": sv.Z}[axis.lower()] * 10
            ce = {"x": ev.X, "y": ev.Y, "z": ev.Z}[axis.lower()] * 10
            if abs(cs - value) < tol and abs(ce - value) < tol:
                return e
        raise ValueError(f"No edge of face at {axis}={value}mm")

    def hole_linear(
        self,
        face_axis: str, face_value: float,
        ref1_axis: str, ref1_value: float, dist1: float,
        ref2_axis: str, ref2_value: float, dist2: float,
        diameter: float = 5.5,
        depth: float = 0,
        tapped: bool = False,
        pitch: float | None = None,
        cbore_diameter: float = 0,
        cbore_depth: float = 0,
    ) -> str:
        """Hole placed parametrically from two face edges (Linear placement).

        The hole stays attached to the edges — change the part length and the
        hole keeps its 8mm-from-edge distance. This is the preferred way to
        place fastener holes (vs sketch-point placement with absolute coords).

        face_axis/face_value: the planar face to drill into, e.g. ("z", 25) = top.
        ref1/ref2: two edges OF THAT FACE by their constant coordinate,
                   e.g. ("x", -34.5, 8) = 8mm from the left edge.
        depth: 0 = through-all, otherwise mm.
        tapped: ISO metric thread (uses standard coarse pitch unless pitch given).
        cbore_diameter/cbore_depth: >0 adds a counterbore (e.g. 10 and 5.5 for
                   an M5 ISO 4762 socket head cap screw seat).

        Direction is resolved automatically: try positive, check that material
        was actually removed (volume), flip to negative if not.
        """
        comp = self.active_part()
        tg = self._tg()
        hf = comp.Features.HoleFeatures
        doc = self.active_document()

        # Build the hole argument (diameter or TapInfo)
        if tapped:
            if pitch is None:
                dia_int = int(round(diameter))
                pitch = self._METRIC_COARSE_PITCH.get(dia_int)
                if pitch is None:
                    raise ValueError(f"No standard coarse pitch for M{dia_int}")
            dia_int = int(round(diameter))
            pitch_str = str(int(pitch)) if pitch == int(pitch) else str(pitch)
            hole_arg = hf.CreateTapInfo(
                True, "ISO Metric profile", f"M{dia_int}x{pitch_str}", "6H", True)
            type_str = f"M{dia_int} tapped"
        else:
            hole_arg = diameter / 10.0
            type_str = f"D={diameter}mm"

        v0 = comp.MassProperties.Volume * 1000

        for direction in (kPositiveExtentDirection, kNegativeExtentDirection):
            # Re-resolve topology fresh each attempt
            face = self._plane_face(face_axis, face_value)
            e1 = self._face_edge(face, ref1_axis, ref1_value)
            e2 = self._face_edge(face, ref2_axis, ref2_value)

            # BiasPoint: 3D point near the intended hole location. Compute by
            # stepping from each reference edge toward the face centroid.
            xs, ys, zs = [], [], []
            for v in range(1, face.Vertices.Count + 1):
                p = face.Vertices.Item(v).Point
                xs.append(p.X * 10); ys.append(p.Y * 10); zs.append(p.Z * 10)
            centroid = {"x": sum(xs)/len(xs), "y": sum(ys)/len(ys), "z": sum(zs)/len(zs)}
            coords = dict(centroid)
            coords[face_axis.lower()] = face_value
            sgn1 = 1 if centroid[ref1_axis.lower()] >= ref1_value else -1
            coords[ref1_axis.lower()] = ref1_value + sgn1 * dist1
            sgn2 = 1 if centroid[ref2_axis.lower()] >= ref2_value else -1
            coords[ref2_axis.lower()] = ref2_value + sgn2 * dist2
            bias = tg.CreatePoint(coords["x"] / 10, coords["y"] / 10, coords["z"] / 10)

            placement = hf.CreateLinearPlacementDefinition(
                face, e1, dist1 / 10.0, e2, dist2 / 10.0, bias)

            if cbore_diameter > 0:
                d_cm = depth / 10.0 if depth > 0 else 10.0  # cbore needs finite depth
                feature = hf.AddCBoreByDistanceExtent(
                    placement, hole_arg, d_cm, direction,
                    cbore_diameter / 10.0, cbore_depth / 10.0)
                extent_str = f"{depth}mm deep, cbore Ø{cbore_diameter}x{cbore_depth}"
            elif depth > 0:
                feature = hf.AddDrilledByDistanceExtent(
                    placement, hole_arg, depth / 10.0, direction)
                extent_str = f"{depth}mm deep"
            else:
                feature = hf.AddDrilledByThroughAllExtent(
                    placement, hole_arg, direction)
                extent_str = "through-all"

            doc.Update()
            v1 = comp.MassProperties.Volume * 1000
            if v1 < v0 - 1:  # material removed — direction correct
                return (f"Hole {type_str} {extent_str} on {face_axis}={face_value} "
                        f"({dist1}mm from {ref1_axis}={ref1_value}, "
                        f"{dist2}mm from {ref2_axis}={ref2_value}): {feature.Name}"
                        f" | V {v1:.0f} ({v1-v0:+.0f} mm³)")
            feature.Delete()
            doc.Update()

        raise RuntimeError("Hole removed no material in either direction — check placement")

    def circular_pattern(
        self,
        feature_index: int,
        count: int = 8,
        angle: float = 360,
        axis: str = "Z",
    ) -> str:
        """Create a circular pattern of a feature around a work axis.
        Distributes 'count' instances evenly within 'angle' degrees.
        """
        import math
        comp = self.active_part()
        cpf = comp.Features.CircularPatternFeatures

        # Get the feature to pattern
        feature = comp.Features.Item(feature_index)

        feat_col = self.app.TransientObjects.CreateObjectCollection()
        feat_col.Add(feature)

        # Work axis selection (X=1, Y=2, Z=3)
        axis_map = {"X": 1, "Y": 2, "Z": 3}
        axis_index = axis_map.get(axis.upper(), 3)
        axis_obj = comp.WorkAxes.Item(axis_index)

        angle_rad = math.radians(angle)

        pattern = cpf.Add(
            feat_col,    # ParentFeatures — ObjectCollection
            axis_obj,    # AxisEntity — WorkAxis
            True,        # NaturalAxisDirection
            count,       # Count
            angle_rad,   # Angle (radians, total span)
            True,        # FitWithinAngle — distribute evenly within angle
            47363,       # ComputeType = kOptimizedCompute (default)
        )

        return (
            f"Circular pattern: {count}x '{feature.Name}' "
            f"around {axis.upper()}-axis, {angle} deg: {pattern.Name}"
        )

    # ---------- Parameters ----------

    def get_parameters(self) -> list[dict]:
        doc = self.active_document()
        params = doc.ComponentDefinition.Parameters
        result = []
        for i in range(1, params.Count + 1):
            p = params.Item(i)
            try:
                result.append({
                    "name": p.Name,
                    "value": p.Value,
                    "expression": p.Expression,
                    "unit": p.Units if hasattr(p, "Units") else "",
                })
            except Exception:
                pass
        return result

    def set_parameter(self, name: str, expression: str) -> str:
        doc = self.active_document()
        params = doc.ComponentDefinition.Parameters
        for i in range(1, params.Count + 1):
            p = params.Item(i)
            if p.Name.lower() == name.lower():
                p.Expression = expression
                doc.Update()
                return f"Parameter '{name}' set to '{expression}' (value={p.Value})"
        raise ValueError(f"Parameter '{name}' not found.")

    def add_parameter(self, name: str, expression: str, units: str = "mm") -> str:
        doc = self.active_document()
        params = doc.ComponentDefinition.Parameters.UserParameters
        params.AddByExpression(name, expression, units)
        doc.Update()
        return f"User parameter '{name}' = '{expression}' ({units}) added"

    # ---------- Query ----------

    def get_document_info(self) -> dict:
        doc = self.active_document()
        comp = doc.ComponentDefinition
        info = {
            "name": doc.DisplayName,
            "full_path": doc.FullFileName,
            "type": str(doc.DocumentType),
            "sketches_count": comp.Sketches.Count,
            "features_count": comp.Features.Count if hasattr(comp.Features, "Count") else 0,
            "bodies_count": comp.SurfaceBodies.Count,
        }
        if comp.SurfaceBodies.Count > 0:
            body = comp.SurfaceBodies.Item(1)
            info["edges_count"] = body.Edges.Count
            info["faces_count"] = body.Faces.Count
        return info

    def list_features(self) -> list[dict]:
        comp = self.active_part()
        result = []
        for i in range(1, comp.Features.Count + 1):
            f = comp.Features.Item(i)
            result.append({
                "index": i,
                "name": f.Name,
                "type": str(f.Type),
                "suppressed": f.Suppressed if hasattr(f, "Suppressed") else False,
            })
            health = unhealthy(self.app, f)
            if health:
                result[-1]["health"] = health
        return result

    def delete_feature(self, indices: list[int]) -> str:
        """Delete features by index. Always deletes in reverse order to avoid index shifting."""
        comp = self.active_part()
        total = comp.Features.Count
        for idx in sorted(indices, reverse=True):
            if idx < 1 or idx > total:
                raise ValueError(f"Feature index {idx} out of range (1–{total})")
            name = comp.Features.Item(idx).Name
            comp.Features.Item(idx).Delete()
            total -= 1
        deleted = sorted(indices)
        return f"Deleted {len(indices)} feature(s): indices {deleted}"

    def suppress_feature(self, indices: list[int], suppressed: bool = True) -> str:
        """Suppress or unsuppress features by index."""
        comp = self.active_part()
        names = []
        for idx in indices:
            f = comp.Features.Item(idx)
            f.Suppressed = suppressed
            names.append(f.Name)
        state = "Suppressed" if suppressed else "Unsuppressed"
        return f"{state}: {', '.join(names)}"

    # ---------- Sheet Metal Features ----------

    def set_sheet_metal_thickness(self, thickness_mm: float) -> str:
        """Set the thickness of the active sheet metal style. Inventor 2026.
        Uses local decimal (comma) — required by Inventor for European locales.
        """
        comp = self.active_part()
        style = comp.ActiveSheetMetalStyle
        # Try comma decimal first (European), fall back to dot
        try:
            style.Thickness = f"{thickness_mm:.3f}".replace(".", ",") + " mm"
        except Exception:
            style.Thickness = f"{thickness_mm:.3f} mm"
        return f"Sheet metal thickness set to {thickness_mm}mm (style: {style.Name})"

    def list_sheet_metal_rules(self) -> list[dict]:
        """Sheet metal rules (styles) available in the active part.

        Rules come from the template / style library, so a shop's own rules
        (e.g. 'T15-V10' = 1.5 mm with a V10 die) show up here. Thickness and
        bend radius are returned as Inventor expressions, exactly as stored.
        """
        comp = self.active_part()
        if not hasattr(comp, "SheetMetalStyles"):
            raise RuntimeError("Active part is not a sheet metal part. "
                               "Use create_part(template='sheet_metal').")
        active = comp.ActiveSheetMetalStyle.Name
        rules = []
        styles = comp.SheetMetalStyles
        for i in range(1, styles.Count + 1):
            s = styles.Item(i)
            try:
                radius = s.BendRadius
            except Exception:
                radius = None
            rules.append({"name": s.Name, "thickness": s.Thickness,
                          "bend_radius": radius, "active": s.Name == active})
        return rules

    def set_sheet_metal_rule(self, name: str) -> str:
        """Activate a sheet metal rule by name (case-insensitive).

        Prefer this over set_sheet_metal_thickness when the shop has its own
        rules: a rule carries thickness, bend radius AND unfold method together,
        so flat patterns match the real press brake.

        NOTE: ComponentDefinition.ActiveSheetMetalStyle is READ-ONLY —
        assigning to it raises. The style activates itself via .Activate().
        """
        comp = self.active_part()
        if not hasattr(comp, "SheetMetalStyles"):
            raise RuntimeError("Active part is not a sheet metal part. "
                               "Use create_part(template='sheet_metal').")
        styles = comp.SheetMetalStyles
        names = [styles.Item(i).Name for i in range(1, styles.Count + 1)]
        match = next((i for i, n in enumerate(names, 1) if n.lower() == name.lower()), None)
        if match is None:
            raise ValueError(f"Sheet metal rule '{name}' not found. Available: {', '.join(names)}")
        styles.Item(match).Activate()
        s = comp.ActiveSheetMetalStyle
        try:
            thickness_mm = comp.Thickness.Value * 10   # resolved Parameter, cm -> mm
            t = f"{thickness_mm:g} mm"
        except Exception:
            t = s.Thickness
        radius = s.BendRadius
        try:
            # Expression may reference other parameters ('Thickness', '10 / 6');
            # resolve it now that this rule is the active one. Result is in cm.
            r_mm = self.active_document().UnitsOfMeasure.GetValueFromExpression(radius, "mm") * 10
            radius = f"{radius} = {r_mm:.3f} mm"
        except Exception:
            pass
        return f"Sheet metal rule '{s.Name}' active: thickness {t}, bend radius {radius}"

    def sheet_metal_face(self, sketch_index: int | None = None) -> str:
        """Create a base sheet-metal Face from a closed sketch profile.
        Equivalent of the 'Face' command in Inventor Sheet Metal UI.
        Uses the active sheet metal style's thickness automatically.
        """
        comp = self.active_part()
        sketch = self._get_sketch(sketch_index)
        profile = self._solid_profile(sketch)
        ff = comp.Features.FaceFeatures
        before = self._vol_state()
        fdef = ff.CreateFaceFeatureDefinition(profile)
        feat = ff.Add(fdef)
        return f"Sheet metal face: {feat.Name}{self._fmt_delta(before)}"

    # HeightDatumTypeEnum — how the flange height is measured
    _HEIGHT_DATUM = {"outer": 75521, "inner": 75522, "tangent": 75523,
                     "outer_ortho": 75524, "inner_ortho": 75525}

    def flange(
        self,
        edge_indices: list[int],
        distance: float = 25,
        angle: float = 90,
        body_index: int = 1,
        auto_miter: bool = True,
        miter_gap: str = "GapSize",
        height_datum: str = "outer",
    ) -> str:
        """Add a Sheet Metal Flange to one or more edges.

        distance: flange height in mm (default 25mm = Inventor's own default)
        angle: bend angle in degrees, default 90° (perpendicular)
        edge_indices: 1-based indices into the body's Edges collection. Give all
            edges of a loop in ONE call so Inventor auto-miters the corners —
            separate flanges need corner seams to close the gaps.
        auto_miter: miter the corners between the edges (UI: Auto-Mitering).
        miter_gap: gap expression; "GapSize" takes it from the sheet metal rule.
        height_datum: "outer" (from outer intersection, UI default), "inner",
            "tangent", "outer_ortho", "inner_ortho".

        Quirks (Inventor 2026/2027):
          - The Distance argument of CreateFlangeDefinition is ignored; the
            height must be set with SetDistanceHeightExtent before Add.
          - The API default MiterGap is the sheet THICKNESS, not the rule's
            GapSize that the UI uses — corners come out visibly open unless
            MiterGap is set explicitly.
          - Angle must be in RADIANS. Passing 90 → 5156°.
          - The flange grows from the face the selected edge belongs to: pick the
            edge on the side the flange should bend toward (e.g. the inner-face
            edge of a wall for an inward flange).
        With "outer" the distance equals the height from the outer mold line, so
        distance=200 on a base panel at Z=0..t gives walls reaching Z=200.
        """
        import math
        datum = self._HEIGHT_DATUM.get(height_datum.lower())
        if datum is None:
            raise ValueError(f"height_datum must be one of {', '.join(self._HEIGHT_DATUM)}")
        comp = self.active_part()
        body = comp.SurfaceBodies.Item(body_index)
        ec = self.app.TransientObjects.CreateEdgeCollection()
        for idx in edge_indices:
            ec.Add(body.Edges.Item(idx))

        flf = comp.Features.FlangeFeatures
        before = self._vol_state()
        fdef = flf.CreateFlangeDefinition(ec, math.radians(angle), distance / 10.0)
        fdef.SetDistanceHeightExtent(distance / 10.0, kPositiveExtentDirection, datum)
        fdef.ApplyAutoMitering = auto_miter
        if auto_miter and miter_gap:
            fdef.MiterGap = miter_gap
        feat = flf.Add(fdef)
        # Update so subsequent feature/edge operations see correct geometry
        self.active_document().Update()
        gap = ""
        if auto_miter:
            try:
                g = feat.Definition.MiterGap
                gap = f", miter gap {g.Expression} = {g.Value * 10:.2f} mm"
            except Exception:
                pass
        return (f"Flange {distance}mm @ {angle}° on {ec.Count} edge(s){gap}: "
                f"{feat.Name}{self._fmt_delta(before)}")

    def sheet_metal_cut(
        self,
        sketch_index: int | None = None,
        cut_across_bends: bool = False,
    ) -> str:
        """Cut through the sheet using a sketch profile. Sheet Metal Cut.
        Default: cut perpendicular to the face (through thickness).
        cut_across_bends: if True, cut wraps around bends (unfolds first).
        """
        comp = self.active_part()
        sketch = self._get_sketch(sketch_index)
        profile = self._solid_profile(sketch)

        cf = comp.Features.CutFeatures
        before = self._vol_state()
        cdef = cf.CreateCutDefinition(profile)
        # Default extent = through thickness (kThroughAllExtent style)
        feat = cf.Add(cdef)
        self._require_removed(feat, before, "Sheet metal cut")
        return f"Sheet metal cut: {feat.Name}{self._fmt_delta(before)}"

    def _require_removed(self, feat, before, label):
        """A sheet metal Cut sketched on a work plane (not on a face of the sheet)
        is created without error but removes nothing. Delete it and raise."""
        after = self._vol_state()
        if before and after and before[0] - after[0] < 1e-3:
            name = feat.Name
            feat.Delete(False, False, False)
            raise RuntimeError(
                f"{label} '{name}' removed no material and was deleted — the sketch must "
                "lie on a face of the sheet (create_sketch(face_index=...) or cut_on_face), "
                "not on a work plane, and the profile must overlap the sheet.")

    def cut_on_face(
        self,
        face_index: int,
        circles: list | None = None,
        rectangles: list | None = None,
        body_index: int = 1,
    ) -> str:
        """Cut holes through the sheet at WORLD coordinates (mm), sketched on a face.

        Face sketches have their own origin and axes (often flipped), so placing
        geometry with sketch coordinates is error-prone. Here every point is given
        in model space and mapped with PlanarSketch.ModelToSketchSpace.

        circles:    [[x, y, z, diameter], ...]
        rectangles: [[[x, y, z], [x, y, z], [x, y, z], [x, y, z]], ...] — 4 corners in order
        The removed volume is verified; a cut that removes nothing raises.
        """
        comp = self.active_part()
        body = comp.SurfaceBodies.Item(body_index)
        face = body.Faces.Item(face_index)
        sketch = comp.Sketches.Add(face)
        tg = self._tg()

        def p2(x, y, z):
            return sketch.ModelToSketchSpace(tg.CreatePoint(x / 10.0, y / 10.0, z / 10.0))

        n = 0
        for x, y, z, d in circles or []:
            sketch.SketchCircles.AddByCenterRadius(p2(x, y, z), d / 20.0)
            n += 1
        for corners in rectangles or []:
            pts = [p2(*c) for c in corners]
            first = sketch.SketchLines.AddByTwoPoints(pts[0], pts[1])
            last = first
            for pt in pts[2:]:
                last = sketch.SketchLines.AddByTwoPoints(last.EndSketchPoint, pt)
            sketch.SketchLines.AddByTwoPoints(last.EndSketchPoint, first.StartSketchPoint)
            n += 1
        if n == 0:
            sketch.Delete()
            raise ValueError("Give at least one circle or rectangle.")
        cf = comp.Features.CutFeatures
        before = self._vol_state()
        feat = cf.Add(cf.CreateCutDefinition(self._solid_profile(sketch)))
        self.active_document().Update()
        self._require_removed(feat, before, "cut_on_face")
        return f"Cut {n} shape(s) on face {face_index}: {feat.Name}{self._fmt_delta(before)}"

    def corner_round(self, edge_indices: list[int], radius: float = 5, body_index: int = 1) -> str:
        """Round sheet metal corners (Corner Round feature — also rounds the flat
        pattern, so the laser cuts it). Edges are the short edges running through
        the thickness at the corners; locate them with find_edge / list_edges."""
        comp = self.active_part()
        body = comp.SurfaceBodies.Item(body_index)
        ec = self.app.TransientObjects.CreateEdgeCollection()
        for idx in edge_indices:
            ec.Add(body.Edges.Item(idx))
        before = self._vol_state()
        crf = comp.Features.CornerRoundFeatures
        feat = crf.Add(crf.CreateCornerRoundDefinition(ec, f"{radius} mm"))
        self.active_document().Update()
        return f"Corner round R{radius} on {ec.Count} corner(s): {feat.Name}{self._fmt_delta(before)}"

    def find_edge(
        self,
        body_index: int = 1,
        x: float | None = None,
        y: float | None = None,
        z: float | None = None,
        tolerance: float = 0.1,
    ) -> int:
        """Locate a body edge by its midpoint coordinates (mm). Returns 1-based index.
        Pass any subset of x/y/z — None values are not constrained.
        Useful for finding the edge to flange (no need to print all 20 edges).
        """
        comp = self.active_part()
        body = comp.SurfaceBodies.Item(body_index)
        for i in range(1, body.Edges.Count + 1):
            e = body.Edges.Item(i)
            sv, ev = e.StartVertex.Point, e.StopVertex.Point
            mx = (sv.X + ev.X) / 2 * 10
            my = (sv.Y + ev.Y) / 2 * 10
            mz = (sv.Z + ev.Z) / 2 * 10
            if x is not None and abs(mx - x) > tolerance: continue
            if y is not None and abs(my - y) > tolerance: continue
            if z is not None and abs(mz - z) > tolerance: continue
            return i
        criteria = ", ".join(
            f"{k}={v}" for k, v in [("x", x), ("y", y), ("z", z)] if v is not None
        )
        raise ValueError(f"No edge found matching midpoint ({criteria}) tol={tolerance}mm")

    def find_face(
        self,
        body_index: int = 1,
        x: float | None = None,
        y: float | None = None,
        z: float | None = None,
        min_area: float = 100,
        surface_type: int = 5890,
        tolerance: float = 0.5,
    ) -> int:
        """Locate a body face by its centroid coordinates (mm). Returns 1-based index.

        Useful for: finding a panel face to sketch on for sheet metal Cut.
        Centroid is computed as the average of the face's vertices.

        surface_type: 5890=Plane (default), 5891=Cylinder, 5893=Cone, 5895=Torus, 5896=Sphere
                      (SURFACE_TYPES has them all)
        min_area: filter out small auxiliary faces (mm²)
        tolerance: how close the centroid must be (mm)
        """
        comp = self.active_part()
        body = comp.SurfaceBodies.Item(body_index)
        for i in range(1, body.Faces.Count + 1):
            f = body.Faces.Item(i)
            if surface_type is not None and f.SurfaceType != surface_type: continue
            try:
                xs, ys, zs = [], [], []
                for vi in range(1, f.Vertices.Count + 1):
                    v = f.Vertices.Item(vi)
                    xs.append(v.Point.X * 10)
                    ys.append(v.Point.Y * 10)
                    zs.append(v.Point.Z * 10)
                if not xs: continue
                cx = sum(xs) / len(xs)
                cy = sum(ys) / len(ys)
                cz = sum(zs) / len(zs)
                area_mm2 = f.Evaluator.Area * 100
                if area_mm2 < min_area: continue
                if x is not None and abs(cx - x) > tolerance: continue
                if y is not None and abs(cy - y) > tolerance: continue
                if z is not None and abs(cz - z) > tolerance: continue
                return i
            except Exception:
                continue
        criteria = ", ".join(
            f"{k}={v}" for k, v in [("x", x), ("y", y), ("z", z)] if v is not None
        )
        raise ValueError(f"No face matching centroid ({criteria}) area≥{min_area} tol={tolerance}mm")

    # ---------- Assembly (IAM) ----------

    # Origin geometry of a component, addressable by name. Work planes/axes are
    # indexed 1..3 in YZ/XZ/XY and X/Y/Z order respectively (same convention as
    # parts, see create_sketch).
    _ASM_GEOM = {
        "YZ": ("WorkPlanes", 1), "XZ": ("WorkPlanes", 2), "XY": ("WorkPlanes", 3),
        "X": ("WorkAxes", 1), "Y": ("WorkAxes", 2), "Z": ("WorkAxes", 3),
        "ORIGIN": ("WorkPoints", 1),
    }

    def create_assembly(self, name: str | None = None, template: str = "metric") -> str:
        """Create a new empty assembly document (.iam).

        template: "metric" (Standard (mm).iam), "english", or a full .iam path.
        """
        import glob

        templates_path = self.app.FileLocations.TemplatesPath
        if template.lower() == "metric":
            patterns = [
                os.path.join(templates_path, "Metric", "Standard (mm).iam"),
                os.path.join(templates_path, "**", "Standard (mm).iam"),
            ]
        elif template.lower() == "english":
            patterns = [
                os.path.join(templates_path, "en-US", "Standard.iam"),
                os.path.join(templates_path, "**", "Standard.iam"),
            ]
        else:
            if not os.path.exists(template):
                raise RuntimeError(f"Template not found: {template}")
            patterns = [template]

        template_file = None
        for pattern in patterns:
            if "*" in pattern:
                found = glob.glob(pattern, recursive=True)
                if found:
                    template_file = found[0]
                    break
            elif os.path.exists(pattern):
                template_file = pattern
                break
        if template_file is None:
            fallback = glob.glob(os.path.join(templates_path, "**", "*.iam"), recursive=True)
            if not fallback:
                raise RuntimeError(f"Cannot find an assembly template in {templates_path}")
            template_file = fallback[0]

        doc = self.app.Documents.Add(kAssemblyDocumentObject, template_file, True)
        if name:
            try:
                doc.DisplayName = name
            except Exception:
                pass  # read-only until first save
        doc.Activate()
        return f"Assembly created: {doc.DisplayName} (unsaved — use save_document(path))"

    def _placement(self, x=0.0, y=0.0, z=0.0, x_axis=None, y_axis=None):
        """Matrix putting a component's origin at (x,y,z) mm with its X/Y axes along
        x_axis/y_axis (Z = X × Y). No axes: no rotation."""
        tg = self._tg()
        matrix = tg.CreateMatrix()
        if x_axis is not None or y_axis is not None:
            if x_axis is None or y_axis is None:
                raise ValueError("Give both x_axis and y_axis (or neither).")
            ax, ay = [float(v) for v in x_axis], [float(v) for v in y_axis]
            if abs(sum(a * b for a, b in zip(ax, ay))) > 1e-6:
                raise ValueError(f"x_axis {ax} and y_axis {ay} must be perpendicular.")
            az = (ax[1]*ay[2] - ax[2]*ay[1], ax[2]*ay[0] - ax[0]*ay[2], ax[0]*ay[1] - ax[1]*ay[0])
            matrix.SetCoordinateSystem(tg.CreatePoint(x / 10.0, y / 10.0, z / 10.0),
                                       tg.CreateVector(*ax), tg.CreateVector(*ay), tg.CreateVector(*az))
        elif x or y or z:
            matrix.SetTranslation(tg.CreateVector(x / 10.0, y / 10.0, z / 10.0))
        return matrix

    def place_many(self, items: list[dict]) -> str:
        """Place many components in one call: [{path, x, y, z, x_axis, y_axis, grounded, name}].
        x/y/z (or origin=[x,y,z]) in mm. Every entry is checked before anything is placed;
        a placement that fails is reported and the rest go on."""
        comp = self.active_assembly()
        missing = sorted({os.path.abspath(it["path"]) for it in items
                          if not os.path.exists(os.path.abspath(it.get("path", "")))})
        if missing:
            raise RuntimeError("Files not found (nothing placed): " + ", ".join(missing))
        placed, failed = [], []
        for n, it in enumerate(items, 1):
            try:
                x, y, z = it.get("origin") or (it.get("x", 0), it.get("y", 0), it.get("z", 0))
                occ = comp.Occurrences.Add(os.path.abspath(it["path"]),
                                           self._placement(float(x), float(y), float(z),
                                                           it.get("x_axis"), it.get("y_axis")))
                if it.get("grounded"):
                    occ.Grounded = True
                if it.get("name"):
                    try:
                        occ.Name = it["name"]
                    except Exception:
                        failed.append(f"#{n} placed as {occ.Name}, name {it['name']!r} taken")
                placed.append(occ.Name)
            except Exception as e:
                failed.append(f"#{n} {os.path.basename(it['path'])}: {e}")
        msg = f"Placed {len(placed)} of {len(items)} ({comp.Occurrences.Count} occurrences now)"
        if len(placed) <= 12:
            msg += f": {', '.join(placed)}"
        return msg + ("\nProblems:\n  " + "\n  ".join(failed) if failed else "")

    def place_component(
        self,
        path: str,
        x: float = 0, y: float = 0, z: float = 0,
        grounded: bool = False,
        x_axis: tuple | None = None,
        y_axis: tuple | None = None,
    ) -> str:
        """Place a part or sub-assembly into the active assembly at (x,y,z) mm.

        x_axis / y_axis: where the component's own X and Y axes point in the
        assembly, e.g. x_axis=(0,0,1), y_axis=(0,1,0). Z follows (X × Y).
        Omit both for no rotation. Lets vendor STEP models (whose axes rarely
        match yours) be placed exactly without constraints.

        The file must exist on disk — save parts before placing them.
        Returns the occurrence index (1-based) for use in constraints.
        """
        comp = self.active_assembly()
        path = os.path.abspath(path)
        if not os.path.exists(path):
            raise RuntimeError(f"Component file not found: {path}")
        occ = comp.Occurrences.Add(path, self._placement(x, y, z, x_axis, y_axis))
        if grounded:
            occ.Grounded = True

        idx = comp.Occurrences.Count
        state = ", grounded" if grounded else ""
        return (f"Placed '{occ.Name}' at ({x},{y},{z})mm{state} "
                f"[occurrence {idx} of {comp.Occurrences.Count}]")

    # ---------- Pictures of the model ----------

    VIEWS = {"iso": 10759, "iso_left": 10760, "iso_bottom": 10761, "front": 10764, "back": 10756,
             "top": 10754, "bottom": 10757, "left": 10758, "right": 10755, "current": 10765}

    _CLUTTER = ("Sketches", "Sketches3D", "UserWorkPlanes", "UserWorkAxes", "UserWorkPoints",
                "OriginWorkPlanes", "OriginWorkAxes", "OriginWorkPoints")

    # Z-up views: (direction from the model to the eye, up vector)
    _ZUP = {"iso": ((1, -1, 1), (0, 0, 1)), "iso_left": ((-1, -1, 1), (0, 0, 1)),
            "iso_bottom": ((1, -1, -1), (0, 0, 1)), "front": ((0, -1, 0), (0, 0, 1)),
            "back": ((0, 1, 0), (0, 0, 1)), "top": ((0, 0, 1), (0, 1, 0)),
            "bottom": ((0, 0, -1), (0, -1, 0)), "left": ((-1, 0, 0), (0, 0, 1)),
            "right": ((1, 0, 0), (0, 0, 1))}

    def _z_up_view(self, cam, name):
        """Point the camera for a model built with Z up (Inventor's named views assume Y up)."""
        rb = self.active_document().ComponentDefinition.RangeBox
        lo, hi = rb.MinPoint, rb.MaxPoint
        center = [(lo.X + hi.X) / 2, (lo.Y + hi.Y) / 2, (lo.Z + hi.Z) / 2]
        span = max(hi.X - lo.X, hi.Y - lo.Y, hi.Z - lo.Z, 1.0)
        (dx, dy, dz), up = self._ZUP[name]
        tg = self._tg()
        cam.Target = tg.CreatePoint(*center)
        cam.Eye = tg.CreatePoint(center[0] + dx * span * 3, center[1] + dy * span * 3, center[2] + dz * span * 3)
        cam.UpVector = tg.CreateUnitVector(*up)

    def view_images(self, views=("iso",), width: int = 800, height: int = 600,
                    white: bool = False, focus: str = "", sketches: bool = False,
                    up: str = "y") -> list[str]:
        """Render the active document from each named view into PNG files and return their
        paths. The camera is put back afterwards, so the user's view doesn't jump.
        focus: an occurrence name — zoom to that component instead of the whole model."""
        import tempfile
        unknown = [v for v in views if v not in self.VIEWS]
        if unknown:
            raise ValueError(f"Unknown view {', '.join(unknown)} — use {', '.join(self.VIEWS)}")
        self.active_document()
        view = self.app.ActiveView
        cam = view.Camera
        saved = (cam.Eye, cam.Target, cam.UpVector, cam.Perspective)
        box = self._occurrence_box(focus) if focus else None
        colors = ()
        if white:
            colors = (self.app.TransientObjects.CreateColor(255, 255, 255),) * 2  # top, bottom
        folder = tempfile.mkdtemp(prefix="inventor-view-")
        paths = []
        shown = {}
        if not sketches:  # sketches and work features hide the shape in a picture
            vis = self.app.ActiveDocument.ObjectVisibility
            for prop in self._CLUTTER:
                try:
                    shown[prop] = getattr(vis, prop)
                    setattr(vis, prop, False)
                except Exception:
                    pass
        try:
            for name in views:
                if name != "current":
                    if up.lower() == "z":
                        self._z_up_view(cam, name)
                    else:
                        cam.ViewOrientationType = self.VIEWS[name]
                    cam.Perspective = False
                    cam.Fit()
                    cam.Apply()
                if box is not None:
                    self._zoom_to(cam, box)
                path = os.path.join(folder, f"{name}.png")
                cam.SaveAsBitmap(path, int(width), int(height), *colors)
                paths.append(path)
        finally:
            cam.Eye, cam.Target, cam.UpVector = saved[0], saved[1], saved[2]
            cam.Perspective = saved[3]
            cam.Apply()
            for prop, value in shown.items():
                try:
                    setattr(self.app.ActiveDocument.ObjectVisibility, prop, value)
                except Exception:
                    pass
        return paths

    def _occurrence_box(self, name: str):
        comp = self.active_assembly()
        occ = next((comp.Occurrences.Item(i) for i in range(1, comp.Occurrences.Count + 1)
                    if comp.Occurrences.Item(i).Name.lower() == name.lower()), None)
        if occ is None:
            raise RuntimeError(f"No component named {name!r} (see list_occurrences)")
        return occ.RangeBox

    @staticmethod
    def _zoom_to(cam, box):
        """Keep the view direction, center on the box and frame it with some margin."""
        lo, hi = box.MinPoint, box.MaxPoint
        center = [(a + b) / 2 for a, b in ((lo.X, hi.X), (lo.Y, hi.Y), (lo.Z, hi.Z))]
        size = max(hi.X - lo.X, hi.Y - lo.Y, hi.Z - lo.Z) * 1.3
        eye, target = cam.Eye, cam.Target
        d = [eye.X - target.X, eye.Y - target.Y, eye.Z - target.Z]
        t = cam.Target
        t.X, t.Y, t.Z = center
        e = cam.Eye
        e.X, e.Y, e.Z = (c + v for c, v in zip(center, d))
        cam.Target, cam.Eye = t, e
        cam.SetExtents(size, size)
        cam.Apply()

    # ---------- Manufacturing outputs: flat pattern, BOM, properties, drawings ----------

    # DXF/DWG layers Inventor writes for a flat pattern. The cut contour always goes out;
    # bend lines are optional (press-brake marks); everything else is hidden.
    _FLAT_HIDDEN = ("IV_TANGENT", "IV_ARC_CENTERS", "IV_TOOL_CENTER", "IV_TOOL_CENTER_DOWN",
                    "IV_FEATURE_PROFILES", "IV_FEATURE_PROFILES_DOWN", "IV_ALTREP_FRONT",
                    "IV_ALTREP_BACK", "IV_UNCONSUMED_SKETCHES", "IV_ROLL_TANGENT", "IV_ROLL")
    _BEND_LAYERS = ("IV_BEND", "IV_BEND_DOWN", "IV_BEND_UP")

    def flat_pattern(self, path: str = "", format: str = "DXF", bend_lines: bool = True,
                     acad_version: str = "2018") -> dict:
        """Flat pattern of the active sheet metal part: size, bends, and optionally a
        DXF/DWG for the laser. Creates the flat pattern if the part has none."""
        doc = self.active_document()
        comp = doc.ComponentDefinition
        if not hasattr(comp, "HasFlatPattern"):
            raise RuntimeError(f"{doc.DisplayName} is not a sheet metal part.")
        if not comp.HasFlatPattern:
            self._silently(comp.Unfold)
            try:
                comp.FlatPattern.ExitEdit()
            except Exception:
                pass
        fp = comp.FlatPattern
        info = {"part": doc.DisplayName,
                "flat_mm": [round(fp.Length * 10, 2), round(fp.Width * 10, 2)],
                "thickness_mm": round(comp.Thickness.Value * 10, 3)}
        try:
            info["bends"] = fp.FlatBendResults.Count
        except Exception:
            pass
        if path:
            fmt = format.upper()
            if fmt not in ("DXF", "DWG"):
                raise ValueError("format is DXF or DWG")
            path = os.path.abspath(path)
            if not path.lower().endswith("." + fmt.lower()):
                raise ValueError(f"path must end in .{fmt.lower()}")
            if not os.path.isdir(os.path.dirname(path)):
                raise RuntimeError(f"Folder does not exist: {os.path.dirname(path)}")
            hidden = self._FLAT_HIDDEN + (() if bend_lines else self._BEND_LAYERS)
            spec = (f"FLAT PATTERN {fmt}?AcadVersion={acad_version}"
                    f"&OuterProfileLayer=OUTER&InteriorProfilesLayer=INNER"
                    f"&InvisibleLayers={';'.join(hidden)}")
            if os.path.exists(path):
                os.remove(path)
            self._silently(lambda: comp.DataIO.WriteDataToFile(spec, path))
            if not os.path.exists(path):
                raise RuntimeError(f"Inventor wrote no file: {path}")
            info["file"] = path
            info["layers"] = "OUTER (contour), INNER (holes/cut-outs)" + (", IV_BEND* (bend lines)" if bend_lines else "")
        return info

    _DTP = "Design Tracking Properties"
    _PROPS = {"part_number": (_DTP, "Part Number"), "description": (_DTP, "Description"),
              "stock_number": (_DTP, "Stock Number"), "vendor": (_DTP, "Vendor"),
              "revision": ("Inventor Summary Information", "Revision Number"),
              "title": ("Inventor Summary Information", "Title"),
              "material": (_DTP, "Material"), "cost_center": (_DTP, "Cost Center")}

    @classmethod
    def _prop(cls, doc, key):
        try:
            group, name = cls._PROPS[key]
            return doc.PropertySets.Item(group).Item(name).Value
        except Exception:
            return None

    def _mass_kg(self, doc):
        try:
            return round(doc.ComponentDefinition.MassProperties.Mass, 3)
        except Exception:
            return None

    def bom(self, view: str = "parts_only", path: str = "") -> str:
        """Bill of materials of the active assembly — parts_only (flat, totals per part)
        or structured (with sub-assemblies, indented). path: also write a CSV (UTF-8, ;)."""
        doc = self.active_document()
        if doc.DocumentType != kAssemblyDocumentObject:
            raise RuntimeError("The active document is not an assembly.")
        b = doc.ComponentDefinition.BOM
        structured = view.lower().startswith("struct")
        if structured:
            b.StructuredViewEnabled = True
            b.StructuredViewFirstLevelOnly = False
        else:
            b.PartsOnlyViewEnabled = True
        wanted = 62466 if structured else 62467  # kStructured / kPartsOnly BOMViewType
        bv = next(b.BOMViews.Item(i) for i in range(1, b.BOMViews.Count + 1)
                  if b.BOMViews.Item(i).ViewType == wanted)
        rows = []

        def walk(collection, depth):
            for i in range(1, collection.Count + 1):
                r = collection.Item(i)
                d = r.ComponentDefinitions.Item(1).Document
                rows.append({"item": r.ItemNumber, "level": depth, "qty": r.ItemQuantity,
                             "part_number": self._prop(d, "part_number") or "",
                             "description": self._prop(d, "description") or "",
                             "material": self._prop(d, "material") or "",
                             "stock_number": self._prop(d, "stock_number") or "",
                             "mass_kg": self._mass_kg(d),
                             "file": os.path.basename(d.FullFileName)})
                if structured and r.ChildRows is not None:
                    walk(r.ChildRows, depth + 1)

        walk(bv.BOMRows, 0)
        cols = ["item", "qty", "part_number", "description", "material", "stock_number", "mass_kg", "file"]
        if path:
            import csv
            path = os.path.abspath(path)
            with open(path, "w", newline="", encoding="utf-8-sig") as f:
                w = csv.writer(f, delimiter=";")
                w.writerow(cols)
                for r in rows:
                    w.writerow([r[c] if r[c] is not None else "" for c in cols])
        lines = [f"BOM {doc.DisplayName} ({'structured' if structured else 'parts only'}, {len(rows)} rows)"]
        for r in rows:
            mass = f"{r['mass_kg']} kg" if r["mass_kg"] is not None else ""
            lines.append(f"{'  ' * r['level']}{r['item']:>4}  {r['qty']:>4} x  {r['part_number']}"
                         f"  | {r['description']} | {r['material']} | {mass} | {r['file']}")
        if path:
            lines.append(f"CSV: {path}")
        return "\n".join(lines)

    def properties(self, values: dict | None = None, material: str = "") -> dict:
        """Read the active document's iProperties; set the given ones first. values keys:
        part_number, description, stock_number, vendor, revision, title, cost_center, or
        any other name (a custom property). material: a material name from the library."""
        doc = self.active_document()
        changed = []
        for key, value in (values or {}).items():
            if key in self._PROPS and key != "material":
                group, name = self._PROPS[key]
                doc.PropertySets.Item(group).Item(name).Value = str(value)
            else:
                custom = doc.PropertySets.Item("Inventor User Defined Properties")
                try:
                    custom.Item(key).Value = value
                except Exception:
                    custom.Add(value, key)
            changed.append(key)
        if material:
            self._set_material(doc, material)
            changed.append("material")
        out = {k: self._prop(doc, k) for k in self._PROPS}
        custom = doc.PropertySets.Item("Inventor User Defined Properties")
        out["custom"] = {custom.Item(i).Name: custom.Item(i).Value for i in range(1, custom.Count + 1)}
        out["mass_kg"] = self._mass_kg(doc)
        if changed:
            out["changed"] = changed
        return out

    def _set_material(self, doc, name: str):
        if doc.DocumentType != kPartDocumentObject:
            raise RuntimeError("Material is set on parts.")
        key = name.lower()
        for i in range(1, doc.MaterialAssets.Count + 1):  # already in the document
            a = doc.MaterialAssets.Item(i)
            if a.DisplayName.lower() == key:
                doc.ActiveMaterial = a
                return
        libs = self.app.AssetLibraries
        for i in range(1, libs.Count + 1):
            assets = libs.Item(i).MaterialAssets
            for j in range(1, assets.Count + 1):
                a = assets.Item(j)
                if a.DisplayName.lower() == key:
                    doc.ActiveMaterial = a.CopyTo(doc)
                    return
        raise RuntimeError(f"No material {name!r} in the document or the material libraries.")

    def _drawing_template(self, template: str) -> str:
        import glob
        if os.path.isfile(template):
            return template
        root = self.app.FileLocations.TemplatesPath
        for pattern in (os.path.join(root, "Metric", f"{template}.idw"),
                        os.path.join(root, "**", "Metric", f"{template}.idw"),
                        os.path.join(root, "**", f"{template}.idw")):
            found = glob.glob(pattern, recursive=True)
            if found:
                return found[0]
        raise RuntimeError(f"No drawing template {template!r} under {root}")

    _SCALES = (10, 5, 2, 1, 1 / 2, 1 / 5, 1 / 10, 1 / 20, 1 / 50, 1 / 100)

    # Where views go, as fractions of the sheet — all above the title block (bottom right).
    # Projected views take their direction from where they sit relative to the base view:
    # "top" below it and "right" beside it is ISO (first angle) — the template's standard
    # decides what Inventor calls them.
    _SPOTS = {"top": (0.25, 0.30), "right": (0.58, 0.66), "iso": (0.85, 0.66)}

    def make_drawing(self, pdf_path: str, views: str = "front,top,right,iso",
                     template: str = "ISO", scale: float = 0, flat: bool = False,
                     drawing_path: str = "") -> str:
        """2D drawing of the active (saved) part or assembly: a base view, projected views,
        overall dimensions on the base view and the model dimensions Inventor can retrieve,
        exported to PDF. flat=True draws a sheet metal part's flat pattern (one view)."""
        model = self.active_document()
        if not model.FullFileName:
            raise RuntimeError("Save the model first — a drawing references the file.")
        pdf_path = os.path.abspath(pdf_path)
        if not pdf_path.lower().endswith(".pdf"):
            raise ValueError("pdf_path must end in .pdf")
        names = [v.strip().lower() for v in views.split(",") if v.strip()]
        base_name = "front" if flat else names[0]
        orient = {"front": 10764, "top": 10754, "right": 10755, "left": 10758,
                  "back": 10756, "bottom": 10757}.get(base_name)
        if orient is None:
            raise ValueError("The first view must be front, top, right, left, back or bottom")
        extra = [] if flat else names[1:]
        unknown = [n for n in extra if n not in self._SPOTS]
        if unknown:
            raise ValueError(f"Projected views can be {', '.join(self._SPOTS)} — not {', '.join(unknown)}")
        comp = model.ComponentDefinition
        if flat:
            if not getattr(comp, "HasFlatPattern", False):
                self.flat_pattern()  # creates it
            size = (comp.FlatPattern.Length, comp.FlatPattern.Width)
        else:
            rb = comp.RangeBox
            dx, dy, dz = (rb.MaxPoint.X - rb.MinPoint.X, rb.MaxPoint.Y - rb.MinPoint.Y,
                          rb.MaxPoint.Z - rb.MinPoint.Z)
            size = (max(dx, dz), max(dy, dz))
        drawing = self.app.Documents.Add(kDrawingDocumentObject, self._drawing_template(template), True)
        try:
            sheet = drawing.ActiveSheet
            cell = (0.6, 0.6) if flat else (0.28, 0.30)  # share of the sheet for the base view
            if not scale:
                fit = min(sheet.Width * cell[0] / max(size[0], 1e-6), sheet.Height * cell[1] / max(size[1], 1e-6))
                scale = next((s for s in self._SCALES if s <= fit), self._SCALES[-1])
            tg = self._tg()
            pt = lambda fx, fy: tg.CreatePoint2d(sheet.Width * fx, sheet.Height * fy)
            opts = self.app.TransientObjects.CreateNameValueMap()
            if flat:
                opts.Add("SheetMetalFoldedModel", False)
            base = sheet.DrawingViews.AddBaseView(model, pt(0.4, 0.55) if flat else pt(0.25, 0.66),
                                                  scale, orient, 32258, "", "", opts)  # hidden lines removed
            placed = [base_name if not flat else "flat pattern"]
            for name in extra:
                if name == "iso":  # shaded, at half scale so it stays in its corner
                    sheet.DrawingViews.AddProjectedView(base, pt(*self._SPOTS[name]), 32259, scale / 2)
                else:
                    sheet.DrawingViews.AddProjectedView(base, pt(*self._SPOTS[name]), 32258)
                placed.append(name)
            overall = self._overall_dimensions(sheet, base)
            retrieved = 0
            try:
                gd = sheet.DrawingDimensions.GeneralDimensions
                for i in range(1, sheet.DrawingViews.Count + 1):
                    v = sheet.DrawingViews.Item(i)
                    got = gd.GetRetrievableDimensions(v)
                    if got is not None and got.Count:
                        gd.Retrieve(v, got)
                        retrieved += got.Count
            except Exception:
                pass
            if drawing_path:
                drawing_path = os.path.abspath(drawing_path)
                self._silently(lambda: drawing.SaveAs(drawing_path, False))
            self._export_pdf(drawing, pdf_path)
            ratio = f"{scale:g}:1" if scale >= 1 else f"1:{1 / scale:g}"
            return (f"Drawing of {model.DisplayName}: {', '.join(placed)}, scale {ratio}, "
                    f"overall {' x '.join(overall) or '-'} mm, {retrieved} retrieved dimension(s), "
                    f"sheet {sheet.Width * 10:.0f}x{sheet.Height * 10:.0f} mm"
                    f"\nPDF: {pdf_path} ({os.path.getsize(pdf_path) // 1024} KB)"
                    + (f"\nDrawing: {drawing_path}" if drawing_path else ""))
        finally:
            if not drawing_path:
                drawing.Close(True)

    def _overall_dimensions(self, sheet, view) -> list[str]:
        """Overall width and height on a view: between its outermost vertical lines and its
        outermost horizontal lines. Returns the values (mm) it placed."""
        tg = self._tg()
        lines = []
        for i in range(1, view.DrawingCurves.Count + 1):
            c = view.DrawingCurves.Item(i)
            try:
                if c.CurveType == 5123:  # kLineSegmentCurve
                    lines.append((c, c.StartPoint, c.EndPoint))
            except Exception:
                pass
        vertical = [l for l in lines if abs(l[1].X - l[2].X) < 1e-4]
        horizontal = [l for l in lines if abs(l[1].Y - l[2].Y) < 1e-4]
        gd = sheet.DrawingDimensions.GeneralDimensions
        out = []
        cx, cy = view.Center.X, view.Center.Y
        for group, key, pos, kind in (
                (vertical, lambda l: l[1].X, tg.CreatePoint2d(cx, view.Top + 1.2), 60162),          # horizontal dim
                (horizontal, lambda l: l[1].Y, tg.CreatePoint2d(view.Left - 1.2, cy), 60163)):      # vertical dim
            if len(group) < 2:
                continue
            a, b = min(group, key=key), max(group, key=key)
            try:
                dim = gd.AddLinear(pos, sheet.CreateGeometryIntent(a[0]), sheet.CreateGeometryIntent(b[0]), kind)
                out.append(f"{dim.ModelValue * 10:g}")
            except Exception:
                pass
        return out

    @staticmethod
    def _nvm_set(nvm, key, value):
        """NameValueMap[key] = value. Value is a property with a parameter, which late-bound
        pywin32 can only read — so put it through IDispatch directly."""
        try:
            nvm.Add(key, value)
        except Exception:
            dispid = nvm._oleobj_.GetIDsOfNames("Value")
            nvm._oleobj_.Invoke(dispid, 0, pythoncom.DISPATCH_PROPERTYPUT, 0, key, value)

    def _export_pdf(self, drawing, path: str):
        addin = self.app.ApplicationAddIns.ItemById("{0AC6FD96-2F4D-42CE-8BE0-8AEA580399E4}")
        if not addin.Activated:
            addin.Activate()
        context = self.app.TransientObjects.CreateTranslationContext()
        context.Type = 13059  # kFileBrowseIOMechanism
        options = self.app.TransientObjects.CreateNameValueMap()
        if addin.HasSaveCopyAsOptions(drawing, context, options):
            for key, value in (("All_Color_AS_Black", 0), ("Vector_Resolution", 400),
                               ("Sheet_Range", 14082)):  # kPrintAllSheets
                self._nvm_set(options, key, value)
        medium = self.app.TransientObjects.CreateDataMedium()
        medium.FileName = path
        if os.path.exists(path):
            os.remove(path)
        self._silently(lambda: addin.SaveCopyAs(drawing, context, options, medium))
        if not os.path.exists(path):
            raise RuntimeError(f"PDF export wrote no file: {path}")

    # ---------- Content Center ----------
    #
    # Walking the whole Content Center tree takes minutes, so it is navigated by a category
    # path ("Structural Shapes/Channels") and every node / family found is cached on the
    # connection — the next call for the same family is instant.

    def _cc_node(self, category: str):
        cache = self.__dict__.setdefault("_cc_nodes", {})
        key = "/".join(s.strip() for s in category.split("/") if s.strip())
        if key in cache:
            return cache[key]
        node, walked = self.app.ContentCenter.TreeViewTopNode, []
        segs, i = (key.split("/") if key else []), 0
        while i < len(segs):
            kids = [node.ChildNodes.Item(k) for k in range(1, node.ChildNodes.Count + 1)]
            names = {k.DisplayName.lower(): k for k in kids}
            # longest match first: a name like "Square/Rectangular Tubes" spans two segments
            j = next((j for j in range(len(segs), i, -1) if "/".join(segs[i:j]).lower() in names), None)
            if j is None:
                raise RuntimeError(f"No category {segs[i]!r} under {'/'.join(walked) or 'Content Center'}. "
                                   f"There: {', '.join(k.DisplayName for k in kids)}")
            node = names["/".join(segs[i:j]).lower()]
            walked.append(node.DisplayName)
            i = j
        cache[key] = node
        return node

    def _cc_family(self, category: str, family: str):
        cache = self.__dict__.setdefault("_cc_families", {})
        key = (category.strip("/ ").lower(), family.lower())
        if key not in cache:
            node = self._cc_node(category)
            fams = [node.Families.Item(i) for i in range(1, node.Families.Count + 1)]
            match = next((f for f in fams if f.DisplayName.lower() == family.lower()), None)
            if match is None:
                raise RuntimeError(f"No family {family!r} in {category!r}. "
                                   f"There: {', '.join(f.DisplayName for f in fams)}")
            cache[key] = match
        return cache[key]

    @staticmethod
    def _cc_columns(fam) -> dict:
        return {fam.TableColumns.Item(i).InternalName: fam.TableColumns.Item(i)
                for i in range(1, fam.TableColumns.Count + 1)}

    def content_center(self, category: str = "", family: str = "", size: str = "",
                       length: float = 0, file_name: str = "") -> str:
        """Browse the Content Center, or make a member's part file to place.

        No family: lists the sub-categories and families of `category`.
        Family, no size: lists its sizes. Family + size: creates the part and returns its
        path. length (mm) makes a custom-length member (structural shapes) saved as
        file_name (default: <project workspace>/Content Center Custom/<family> <size> L<length>.ipt).
        """
        if not family:
            node = self._cc_node(category)
            subs = [node.ChildNodes.Item(i).DisplayName for i in range(1, node.ChildNodes.Count + 1)]
            fams = [node.Families.Item(i).DisplayName for i in range(1, node.Families.Count + 1)]
            out = [f"{category or 'Content Center'}:"]
            if subs:
                out.append("categories: " + ", ".join(subs))
            if fams:
                out.append("families: " + ", ".join(fams))
            return "\n".join(out)
        fam = self._cc_family(category, family)
        cols = self._cc_columns(fam)
        names = [c for c in ("DESIGNATION", "G_D", "SIZE") if c in cols]
        rows = [fam.TableRows.Item(i) for i in range(1, fam.TableRows.Count + 1)]
        label = lambda r: str(r.GetCellValue(cols[names[0]])) if names else str(r.Index)
        if not size:
            return f"{fam.DisplayName} — {len(rows)} sizes: " + ", ".join(label(r) for r in rows)
        norm = lambda s: str(s).replace(" ", "").lower()
        row = next((r for r in rows if any(norm(r.GetCellValue(cols[c])) == norm(size) for c in names)), None)
        if row is None:
            raise RuntimeError(f"No size {size!r} in {fam.DisplayName}. Sizes: {', '.join(label(r) for r in rows)}")
        custom = length > 0
        if custom and "B_L" not in cols:
            raise RuntimeError(f"{fam.DisplayName} has no length parameter (B_L) — omit length.")
        values = self.app.TransientObjects.CreateNameValueMap()
        if custom:
            values.Add("B_L", float(length))  # mm
            if not file_name:
                ws = self.app.DesignProjectManager.ActiveDesignProject.WorkspacePath
                safe = "".join(ch for ch in f"{fam.DisplayName} {label(row)} L{length:g}" if ch not in '<>:"/\\|?*')
                file_name = os.path.join(ws, "Content Center Custom", safe + ".ipt")
            file_name = os.path.abspath(file_name)
            os.makedirs(os.path.dirname(file_name), exist_ok=True)
        if custom and os.path.exists(file_name):
            path = file_name  # made before (frames reuse one file per profile and length)
        else:
            result = self._silently(lambda: fam.CreateMember(
                row, 0, "", 51969, custom, file_name if custom else "", values))  # kUseDefaultRefreshSetting
            path = next((r for r in (result if isinstance(result, tuple) else (result,))
                         if isinstance(r, str) and r.lower().endswith(".ipt")), None)
            if not path or not os.path.exists(path):
                raise RuntimeError(f"Content Center did not create the part: {result}")
        return f"{fam.DisplayName} {label(row)}" + (f" L={length:g} mm" if custom else "") + f": {path}"

    # ---------- Frames: profiles along skeleton lines (Frame Generator has no API) ----------

    def _member_geometry(self, path: str, length: float) -> tuple[int, list, list]:
        """Length axis (0/1/2 = X/Y/Z) of a member part and its range box in mm, read once."""
        cache = self.__dict__.setdefault("_member_geom", {})
        if path not in cache:
            doc = self.app.Documents.Open(path, False)
            rb = doc.ComponentDefinition.RangeBox
            lo = [rb.MinPoint.X * 10, rb.MinPoint.Y * 10, rb.MinPoint.Z * 10]
            hi = [rb.MaxPoint.X * 10, rb.MaxPoint.Y * 10, rb.MaxPoint.Z * 10]
            axis = min(range(3), key=lambda i: abs((hi[i] - lo[i]) - length))
            cache[path] = (axis, lo, hi)
            if not doc.ReferencingDocuments.Count:
                doc.Close(True)
        return cache[path]

    @staticmethod
    def _frame_axes(direction, roll_deg: float):
        """World directions for a member's section axes (u, v) and length axis w. u is level
        (perpendicular to world Z) unless the member is vertical; roll turns the section."""
        import math as m
        n = m.sqrt(sum(c * c for c in direction))
        w = [c / n for c in direction]
        ref = [0.0, 0.0, 1.0] if abs(w[2]) < 0.999 else [1.0, 0.0, 0.0]
        u = [ref[1] * w[2] - ref[2] * w[1], ref[2] * w[0] - ref[0] * w[2], ref[0] * w[1] - ref[1] * w[0]]
        nu = m.sqrt(sum(c * c for c in u))
        u = [c / nu for c in u]
        v = [w[1] * u[2] - w[2] * u[1], w[2] * u[0] - w[0] * u[2], w[0] * u[1] - w[1] * u[0]]
        if roll_deg:
            c, s = m.cos(m.radians(roll_deg)), m.sin(m.radians(roll_deg))
            u, v = [c * a + s * b for a, b in zip(u, v)], [-s * a + c * b for a, b in zip(u, v)]
        return u, v, w

    def frame_members(self, members: list[dict], folder: str = "", category: str = "",
                      family: str = "", size: str = "") -> str:
        """Place structural members along skeleton lines in the active assembly.

        members: [{"start": [x,y,z], "end": [x,y,z], "family": ..., "size": ...,
                   "category": ..., "roll": 0, "anchor": "center"|"origin",
                   "extend": [0, 0], "name": ...}] in mm. family/size/category default to
        the call's own arguments. Each member is a Content Center custom-length part, one file
        per profile and length (reused). anchor "center" puts the section's middle on the
        line, "origin" the profile's own origin. Returns the placements and a cut list.
        """
        import math as m
        comp = self.active_assembly()
        if folder:
            folder = os.path.abspath(folder)
            os.makedirs(folder, exist_ok=True)
        placed, problems, cuts = [], [], {}
        for n, mb in enumerate(members, 1):
            try:
                fam_cat = mb.get("category") or category
                fam = mb.get("family") or family
                sz = mb.get("size") or size
                if not (fam_cat and fam and sz):
                    raise ValueError("category, family and size are needed (per member or for the call)")
                start = [float(c) for c in mb["start"]]
                end = [float(c) for c in mb["end"]]
                d = [b - a for a, b in zip(start, end)]
                line = m.sqrt(sum(c * c for c in d))
                if line < 1:
                    raise ValueError("start and end are the same point")
                ext0, ext1 = (list(mb.get("extend") or [0, 0]) + [0, 0])[:2]
                length = round(line + ext0 + ext1, 1)
                file_name = ""
                if folder:
                    safe = "".join(ch for ch in f"{fam} {sz} L{length:g}" if ch not in '<>:"/\\|?*')
                    file_name = os.path.join(folder, safe + ".ipt")
                out = self.content_center(fam_cat, fam, sz, length, file_name)
                path = out.rsplit(": ", 1)[1]
                axis, lo, hi = self._member_geometry(path, length)
                u, v, w = self._frame_axes(d, float(mb.get("roll", 0)))
                # local axes: the length axis goes along w, the next two (cyclic) along u, v
                world = [None, None, None]
                world[axis], world[(axis + 1) % 3], world[(axis + 2) % 3] = w, u, v
                # the local point that must sit on the start of the line
                local = [0.0, 0.0, 0.0]
                local[axis] = lo[axis]
                if mb.get("anchor", "center") == "center":
                    for i in ((axis + 1) % 3, (axis + 2) % 3):
                        local[i] = (lo[i] + hi[i]) / 2
                begin = [s - ext0 * c / line for s, c in zip(start, d)]
                origin = [b - sum(local[i] * world[i][k] for i in range(3)) for k, b in enumerate(begin)]
                occ = comp.Occurrences.Add(path, self._placement(*origin, world[0], world[1]))
                if mb.get("name"):
                    try:
                        occ.Name = mb["name"]
                    except Exception:
                        problems.append(f"#{n}: name {mb['name']!r} taken, kept {occ.Name}")
                placed.append(f"{occ.Name}: {fam} {sz} L={length:g}")
                key = (fam, sz, length)
                cuts[key] = cuts.get(key, 0) + 1
            except Exception as e:
                problems.append(f"#{n}: {e}")
        lines = [f"Placed {len(placed)} of {len(members)} members"] + [f"  {p}" for p in placed]
        if cuts:
            lines.append("Cut list:")
            for (fam, sz, length), qty in sorted(cuts.items()):
                lines.append(f"  {qty} x {fam} {sz}  L={length:g} mm")
        if problems:
            lines.append("Problems:")
            lines += [f"  {p}" for p in problems]
        return "\n".join(lines)

    # ---------- Vendor parts, clearance checks, motion ----------

    def import_step(self, step_path: str, dest_folder: str, name: str | None = None) -> dict:
        """Import a vendor STEP file as Inventor files inside dest_folder.

        Inventor writes the parts of an imported STEP assembly into a folder NEXT
        TO THE .step FILE (e.g. Downloads\\x\\1.ipt ... 7.ipt) — which may be a
        synced or shared folder. So the STEP is first copied into dest_folder and
        imported from there; nothing is written beside the original.
        Returns a summary (path, type, size, components with bbox/volume).
        """
        import shutil
        step_path = os.path.abspath(step_path)
        dest_folder = os.path.abspath(dest_folder)
        if not os.path.exists(step_path):
            raise RuntimeError(f"STEP file not found: {step_path}")
        os.makedirs(dest_folder, exist_ok=True)
        local = os.path.join(dest_folder, os.path.basename(step_path))
        if os.path.normcase(local) != os.path.normcase(step_path):
            shutil.copy2(step_path, local)
        doc = self.app.Documents.Open(local, True)
        doc.Activate()
        base = name or os.path.splitext(os.path.basename(step_path))[0]
        is_asm = doc.DocumentType == kAssemblyDocumentObject
        target = os.path.join(dest_folder, base + (".iam" if is_asm else ".ipt"))
        stray = []
        for ref in list(doc.AllReferencedDocuments):
            if not os.path.normcase(ref.FullFileName or "").startswith(os.path.normcase(dest_folder)):
                stray.append(ref.FullFileName)
        if stray:
            raise RuntimeError(f"Import wrote files outside {dest_folder}: {stray}")
        self._silently(lambda: doc.SaveAs(target, False))
        cd = doc.ComponentDefinition

        def bb(r):
            return [round(v * 10, 2) for v in (r.MinPoint.X, r.MaxPoint.X, r.MinPoint.Y,
                                                r.MaxPoint.Y, r.MinPoint.Z, r.MaxPoint.Z)]
        out = {"path": target, "type": "assembly" if is_asm else "part", "bbox_mm": bb(cd.RangeBox)}
        if is_asm:
            comps = []
            for o in cd.Occurrences:
                try:
                    vol = round(o.MassProperties.Volume * 1000)
                except Exception:
                    vol = None
                comps.append({"name": o.Name, "bbox_mm": bb(o.RangeBox), "volume_mm3": vol})
            out["components"] = comps
        return out

    def _occ(self, name: str):
        """Occurrence by name in the active assembly; 'Sub:1/2' reaches a
        sub-occurrence (e.g. one part of a vendor hinge assembly)."""
        comp = self.active_assembly()
        parts = name.split("/")
        coll = comp.Occurrences
        occ = None
        for part in parts:
            matches = [o for o in (coll if occ is None else occ.SubOccurrences) if o.Name == part]
            if not matches:
                have = ", ".join(o.Name for o in (coll if occ is None else occ.SubOccurrences))
                raise ValueError(f"No occurrence '{part}'. Available: {have}")
            occ = matches[0]
        return occ

    def check_interference(self, set_a: list[str], set_b: list[str] | None = None) -> list[dict]:
        """Interference between two sets of occurrences (names; 'Asm:1/2' for a
        sub-part). set_b empty = everything else. Returns overlap volume and
        bounding box per pair — touching faces are not interference."""
        comp = self.active_assembly()
        a = [self._occ(n) for n in set_a]
        a_names = {o.Name for o in a}
        b = [self._occ(n) for n in set_b] if set_b else [o for o in comp.Occurrences if o.Name not in a_names]
        ca = self.app.TransientObjects.CreateObjectCollection()
        for o in a:
            ca.Add(o)
        cb = self.app.TransientObjects.CreateObjectCollection()
        for o in b:
            cb.Add(o)
        res = comp.AnalyzeInterference(ca, cb)
        out = []
        for i in range(1, res.Count + 1):
            r = res.Item(i)
            rb = r.InterferenceBody.RangeBox
            out.append({"a": r.OccurrenceOne.Name, "b": r.OccurrenceTwo.Name,
                        "volume_mm3": round(r.Volume * 1000, 3),
                        "bbox_mm": [round(v * 10, 2) for v in (rb.MinPoint.X, rb.MaxPoint.X, rb.MinPoint.Y,
                                                                rb.MaxPoint.Y, rb.MinPoint.Z, rb.MaxPoint.Z)]})
        return out

    def measure_distance(self, a: str, b: str) -> float:
        """Minimum distance in mm between two occurrences (0 = touching or overlapping)."""
        self.active_assembly()
        return round(self.app.MeasureTools.GetMinimumDistance(self._occ(a), self._occ(b))[0] * 10, 3)

    def sweep_rotation(
        self,
        moving: list[str],
        axis_point: tuple, axis_dir: tuple,
        start: float = 0, stop: float = 110, step: float = 10,
        against: list[str] | None = None,
        measure_to: str | None = None,
    ) -> list[dict]:
        """Rotate occurrences about an axis in steps and check for collisions —
        e.g. a door on its hinge axis, or a lock tongue on its shaft.

        moving: occurrences that rotate together (door, lock, door-mounted studs).
        axis_point (mm) / axis_dir: the rotation axis. Angles in degrees.
        against: occurrences to test (default: everything else).
        measure_to: optional occurrence to report the minimum distance to.
        The original positions are always restored.
        """
        import math
        comp = self.active_assembly()
        tg = self._tg()
        movers = [self._occ(n) for n in moving]
        base = [o.Transformation for o in movers]
        pt = tg.CreatePoint(*(v / 10.0 for v in axis_point))
        vec = tg.CreateVector(*axis_dir)
        target = self._occ(measure_to) if measure_to else None
        rows = []
        try:
            ang = start
            while ang <= stop + 1e-9:
                rot = tg.CreateMatrix()
                rot.SetToRotation(math.radians(ang), vec, pt)
                for o, m0 in zip(movers, base):
                    m = m0.Copy()
                    m.TransformBy(rot)
                    o.Transformation = m
                hits = self.check_interference(moving, against)
                row = {"angle": round(ang, 3), "collisions": len(hits),
                       "volume_mm3": round(sum(h["volume_mm3"] for h in hits), 3),
                       "with": sorted({h["b"] for h in hits})}
                if target is not None:
                    row["min_distance_mm"] = round(min(
                        self.app.MeasureTools.GetMinimumDistance(o, target)[0] for o in movers) * 10, 2)
                rows.append(row)
                ang += step
        finally:
            for o, m0 in zip(movers, base):
                o.Transformation = m0
            self.active_document().Update()
        return rows

    def list_occurrences(self) -> str:
        """Compact table of the assembly's components:
        '[i] block:1  pos=(0.0,0.0,0.0) grounded'
        """
        comp = self.active_assembly()
        lines = []
        for i in range(1, comp.Occurrences.Count + 1):
            occ = comp.Occurrences.Item(i)
            t = occ.Transformation.Translation
            flags = []
            if occ.Grounded:
                flags.append("grounded")
            if occ.Suppressed:
                flags.append("suppressed")
            lines.append(
                f"[{i}] {occ.Name}  pos=({t.X * 10:.1f},{t.Y * 10:.1f},{t.Z * 10:.1f})"
                + ("  " + " ".join(flags) if flags else "")
            )
        return "\n".join(lines) if lines else "(no components placed)"

    def ground_occurrence(self, index: int, grounded: bool = True) -> str:
        """Ground (fix in space) or unground a component. Ground the first
        component of every assembly — otherwise constraints move it instead."""
        comp = self.active_assembly()
        occ = comp.Occurrences.Item(index)
        occ.Grounded = grounded
        return f"{'Grounded' if grounded else 'Ungrounded'}: {occ.Name}"

    def delete_occurrence(self, index: int) -> str:
        comp = self.active_assembly()
        occ = comp.Occurrences.Item(index)
        name = occ.Name
        occ.Delete()
        return f"Deleted occurrence: {name} ({comp.Occurrences.Count} left)"

    def _origin_proxy(self, occ, geometry: str):
        """Resolve 'XY' / 'X' / 'ORIGIN' on an occurrence to an assembly-context
        geometry proxy. Constraints need the PROXY, not the part-level entity —
        passing the raw work plane silently constrains the wrong context."""
        key = geometry.upper()
        if key not in self._ASM_GEOM:
            raise ValueError(
                f"Unknown geometry '{geometry}'. Use one of: "
                + ", ".join(self._ASM_GEOM)
            )
        collection, idx = self._ASM_GEOM[key]
        entity = getattr(occ.Definition, collection).Item(idx)
        return occ.CreateGeometryProxy(entity)

    def constrain(
        self,
        occurrence1: int, geometry1: str,
        occurrence2: int, geometry2: str,
        kind: str = "mate",
        offset: float = 0,
        angle: float = 0,
    ) -> str:
        """Constrain two components by their origin planes/axes/point.

        kind:
          "mate"   — planes face each other / axes are collinear (offset in mm)
          "flush"  — planes point the same way (offset in mm)
          "angle"  — angle between two planes or axes (angle in degrees)
        geometry1/geometry2: "XY", "XZ", "YZ", "X", "Y", "Z", "ORIGIN"

        Origin-geometry constraints are the parametric way to assemble: they
        survive part edits, unlike face picks that break when topology renumbers.
        """
        import math
        comp = self.active_assembly()
        occ1 = comp.Occurrences.Item(occurrence1)
        occ2 = comp.Occurrences.Item(occurrence2)
        e1 = self._origin_proxy(occ1, geometry1)
        e2 = self._origin_proxy(occ2, geometry2)

        k = kind.lower()
        if k == "mate":
            c = comp.Constraints.AddMateConstraint(e1, e2, offset / 10.0)
            detail = f"offset {offset}mm"
        elif k == "flush":
            c = comp.Constraints.AddFlushConstraint(e1, e2, offset / 10.0)
            detail = f"offset {offset}mm"
        elif k == "angle":
            c = comp.Constraints.AddAngleConstraint(e1, e2, math.radians(angle))
            detail = f"{angle}°"
        else:
            raise ValueError(f"Unknown constraint kind '{kind}'. Use mate, flush or angle.")

        self.active_document().Update()
        return (f"{k.capitalize()} constraint '{c.Name}': "
                f"{occ1.Name}.{geometry1.upper()} ↔ {occ2.Name}.{geometry2.upper()} ({detail})")

    def constrain_insert(
        self,
        occurrence1: int, edge1: int,
        occurrence2: int, edge2: int,
        offset: float = 0,
        axes_opposed: bool = True,
    ) -> str:
        """Insert constraint between two circular edges (bolt into a hole).

        edge1/edge2 are 1-based indices into each component's body-1 edge list;
        use list_component_edges() to find the circular ones.
        """
        comp = self.active_assembly()
        occ1 = comp.Occurrences.Item(occurrence1)
        occ2 = comp.Occurrences.Item(occurrence2)
        e1 = occ1.CreateGeometryProxy(
            occ1.Definition.SurfaceBodies.Item(1).Edges.Item(edge1))
        e2 = occ2.CreateGeometryProxy(
            occ2.Definition.SurfaceBodies.Item(1).Edges.Item(edge2))
        c = comp.Constraints.AddInsertConstraint(e1, e2, axes_opposed, offset / 10.0)
        self.active_document().Update()
        return (f"Insert constraint '{c.Name}': {occ1.Name} edge {edge1} ↔ "
                f"{occ2.Name} edge {edge2} (offset {offset}mm)")

    def list_component_edges(
        self, occurrence: int, circular_only: bool = True, max_count: int = 40
    ) -> str:
        """Edge table of one placed component, in its own part coordinates.
        circular_only=True lists just the circular edges — the ones an Insert
        constraint can use."""
        comp = self.active_assembly()
        occ = comp.Occurrences.Item(occurrence)
        body = occ.Definition.SurfaceBodies.Item(1)
        lines = []
        for i in range(1, body.Edges.Count + 1):
            e = body.Edges.Item(i)
            is_circular = e.GeometryType == kCircleCurve
            if circular_only and not is_circular:
                continue
            if is_circular:
                g = e.Geometry
                c = g.Center
                lines.append(
                    f"[{i}] circle D={g.Radius * 20:.1f} "
                    f"c=({c.X * 10:.1f},{c.Y * 10:.1f},{c.Z * 10:.1f})"
                )
            else:
                sv, ev = e.StartVertex.Point, e.StopVertex.Point
                lines.append(
                    f"[{i}] edge mid=({(sv.X + ev.X) / 2 * 10:.1f},"
                    f"{(sv.Y + ev.Y) / 2 * 10:.1f},{(sv.Z + ev.Z) / 2 * 10:.1f})"
                )
            if len(lines) >= max_count:
                break
        return "\n".join(lines) if lines else "(no matching edges)"

    def list_constraints(self) -> str:
        comp = self.active_assembly()
        lines = []
        for i in range(1, comp.Constraints.Count + 1):
            c = comp.Constraints.Item(i)
            health = unhealthy(self.app, c)
            lines.append(f"[{i}] {c.Name}" + (" (suppressed)" if c.Suppressed else "")
                         + (f" [{health}]" if health else ""))
        return "\n".join(lines) if lines else "(no constraints)"

    def delete_constraint(self, index: int) -> str:
        comp = self.active_assembly()
        c = comp.Constraints.Item(index)
        name = c.Name
        c.Delete()
        self.active_document().Update()
        return f"Deleted constraint: {name} ({comp.Constraints.Count} left)"

    def assembly_info(self) -> dict:
        """Snapshot of the active assembly: components, constraints, mass, bbox."""
        doc = self.active_document()
        if doc.DocumentType != kAssemblyDocumentObject:
            raise RuntimeError("Active document is not an assembly.")
        comp = doc.ComponentDefinition
        info: dict = {
            "document": doc.DisplayName,
            "occurrences": comp.Occurrences.Count,
            "constraints": comp.Constraints.Count,
            "components": [],
        }
        for i in range(1, comp.Occurrences.Count + 1):
            occ = comp.Occurrences.Item(i)
            t = occ.Transformation.Translation
            info["components"].append({
                "index": i,
                "name": occ.Name,
                "position_mm": [round(t.X * 10, 3), round(t.Y * 10, 3), round(t.Z * 10, 3)],
                "grounded": bool(occ.Grounded),
            })
        try:
            mp = self._fresh_mass(doc, comp)
            info["mass_kg"] = round(mp.Mass, 4)
            info["volume_mm3"] = round(mp.Volume * 1000, 1)
        except Exception:
            pass
        return info
