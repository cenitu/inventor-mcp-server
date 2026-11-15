"""Constants, readable errors and helpers shared by every part of the Inventor wrapper."""

import math
import os
import sys

# Inventor runs only on Windows. Elsewhere (a registry's Linux build check) the module still
# imports, so the server starts and lists its tools; every Inventor call then says why it can't run.
WINDOWS = sys.platform == "win32"
NOT_WINDOWS = ("This server drives Autodesk Inventor through COM, so it needs Windows with "
               "Inventor installed. The tools are listed here but can't run on this machine.")
if WINDOWS:
    import win32com.client
    import win32com.client.gencache
    import pythoncom
    import win32process

# Never wrap COM objects in makepy (gen_py) classes, even when a cache exists. Something
# else on the machine — a script calling CastTo or gencache.EnsureDispatch — creates one
# for the Inventor type library, and from then on GetActiveObject fails with
# KeyError '_dispobj_' and a generic Document hides ComponentDefinition. Late-bound
# objects (with type info) expose every member of the real object.
if WINDOWS:
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

# ShellDirectionEnum
kInsideShellDirection = 41217
kOutsideShellDirection = 41218
kBothSidesShellDirection = 41219

# SweepTypeEnum / SweepProfileOrientationEnum
kPathSweepType = 104449
kNormalToPath = 59649
kParallelToOriginalProfile = 59650

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


if WINDOWS:
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


# Everything here, private helpers included, is what `from api_common import *` gives the parts.
__all__ = [name for name in dir() if not name.startswith("__")]
