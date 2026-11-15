"""Connecting, documents, saving and protected files."""
if __package__:
    from .api_common import *  # noqa: F401,F403
    from .flat_checks import flat_pattern_problems, rule_from_name  # noqa: F401
else:  # server.py run as a script: the modules sit on sys.path
    from api_common import *  # noqa: F401,F403
    from flat_checks import flat_pattern_problems, rule_from_name  # noqa: F401


class DocumentsMixin:
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

    def create_part(self, name: str | None = None, template: str = "metric", material: str = "",
                    part_number: str = "", description: str = "") -> str:
        msg = self._create_part(name, template)
        values = {k: v for k, v in (("part_number", part_number), ("description", description)) if v}
        if values or material:
            self.properties(values, material)
            msg += "".join(f", {k} {v}" for k, v in (("material", material), *values.items()) if v)
        return msg

    def _create_part(self, name: str | None = None, template: str = "metric") -> str:
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
    def protected_patterns() -> list[str]:
        """Extra protected paths (globs): INVENTOR_MCP_PROTECTED, ";"-separated, and
        ~/.inventor-mcp/protected.txt, one per line — vendor originals, other projects."""
        pats = [p for p in os.environ.get("INVENTOR_MCP_PROTECTED", "").split(";") if p.strip()]
        try:
            with open(os.path.join(os.path.expanduser("~"), ".inventor-mcp", "protected.txt"), encoding="utf-8") as f:
                pats += [ln.strip() for ln in f if ln.strip() and not ln.startswith("#")]
        except OSError:
            pass
        return [os.path.normcase(os.path.expandvars(p.strip())) for p in pats]

    @classmethod
    def _library_reason(cls, doc, folders, members: bool = True) -> str:
        """Why doc must not be saved ('' if it may be): a file in a library / Content Center
        folder, or (members=True) any Content Center member — custom-length ones included,
        which only Content Center itself should rewrite."""
        if members:
            try:
                if doc.ComponentDefinition.IsContentMember:
                    return "Content Center member"
            except Exception:
                pass
        return cls._path_reason(doc.FullFileName, folders) if doc.FullFileName else ""

    @classmethod
    def _path_reason(cls, path: str, folders) -> str:
        """Why nothing may be written to path ('' if it may be)."""
        import fnmatch
        name = os.path.normcase(os.path.abspath(path))
        for f in folders:
            if name == f or name.startswith(f.rstrip("\\/") + os.sep):
                return "library / Content Center folder"
        if any(fnmatch.fnmatch(name, p) or fnmatch.fnmatch(name, p.rstrip("\\/") + os.sep + "*")
               for p in cls.protected_patterns()):
            return "protected path"
        return ""

    def save_document(self, path: str | None = None, overwrite: bool = False,
                      save_dependents: bool = False) -> str:
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
            why = self._path_reason(target, folders)
            if why:
                raise RuntimeError(f"{target} is in a {why} — save it elsewhere")
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
        # SaveAs under SilentOperation writes every dirty dependent too — protected files
        # included. So what must not be written is marked clean for the moment of the save.
        held = [d for d in doc.AllReferencedDocuments if d.Dirty]
        for d in held:
            d.Dirty = False
        try:
            if target:
                self._silently(lambda: doc.SaveAs(target, False))
                saved.insert(0, target)
                msg = f"Saved as: {target}"
            else:
                self._silently(lambda: doc.Save2(False))
                saved.insert(0, doc.FullFileName)
                msg = f"Saved: {doc.FullFileName}"
        finally:
            for d in held:
                d.Dirty = True
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
        folders = self.library_folders()
        out = []
        for d in self.app.Documents.VisibleDocuments:
            row = {"name": d.DisplayName, "path": d.FullFileName or "(unsaved)",
                   "type": {12290: "part", 12291: "assembly", 12292: "drawing"}.get(d.DocumentType, str(d.DocumentType)),
                   "dirty": bool(d.Dirty), "active": active is not None and d.InternalName == active.InternalName}
            why = self._library_reason(d, folders)
            if why:
                row["protected"] = why
            out.append(row)
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
