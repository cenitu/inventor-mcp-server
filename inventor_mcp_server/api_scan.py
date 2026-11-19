"""Reading geometry in bulk: a rule run inside Inventor, its results kept per file on disk."""
import hashlib
import json
import re
import time

if __package__:
    from .api_common import *  # noqa: F401,F403
    from . import alignment
    from .flat_checks import judge_sheet, sheet_facts
else:  # server.py run as a script: the modules sit on sys.path
    from api_common import *  # noqa: F401,F403
    import alignment
    from flat_checks import judge_sheet, sheet_facts

# Why a rule: read through COM from Python, a face costs about 6 ms — one millisecond for
# the calls themselves, the rest for pywin32 asking Inventor to describe every object it is
# handed. A 174-face part took 19.8 s that way and 0.1 s as a rule inside Inventor (measured
# on Inventor 2027.1). The iLogic add-in ships with Inventor, so nothing has to be installed.
ILOGIC_ADDIN = "{3BDD8D79-2179-4B11-8A5A-257B1C0263AC}"
RULES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "rules")
SCAN_CACHE = os.environ.get("INVENTOR_MCP_SCAN_CACHE") or os.path.join(
    os.path.expanduser("~"), ".inventor-mcp", "scan")
IDENTITY = [1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1, 0]


def _rule_version(rule_path: str) -> str:
    """Changes whenever the rule's text does, so a cache written by another rule is not used."""
    with open(rule_path, "rb") as f:
        return hashlib.sha1(f.read()).hexdigest()[:12]


def _cache_file(path: str) -> str:
    return os.path.join(SCAN_CACHE, hashlib.sha1(os.path.normcase(path).encode("utf-8")).hexdigest() + ".json")


def _file_of(name: str) -> str:
    """The file behind a document name: "C:\\x\\part.ipt<Flat>" is a model state of part.ipt."""
    return name.split("<")[0]


def cache_get(path: str, version: str):
    """The stored scan of a document, if its file on disk is still the one that was scanned."""
    try:
        st = os.stat(_file_of(path))
        with open(_cache_file(path), encoding="utf-8") as f:
            entry = json.load(f)
        if entry["rule"] == version and entry["mtime_ns"] == st.st_mtime_ns and entry["size"] == st.st_size:
            return entry["part"]
    except (OSError, ValueError, KeyError):
        pass
    return None


def cache_put(path: str, version: str, part: dict) -> None:
    try:
        st = os.stat(_file_of(path))
        os.makedirs(SCAN_CACHE, exist_ok=True)
        target = _cache_file(path)
        with open(target + ".tmp", "w", encoding="utf-8") as f:
            json.dump({"file": path, "rule": version, "mtime_ns": st.st_mtime_ns, "size": st.st_size,
                       "part": part}, f)
        os.replace(target + ".tmp", target)
    except OSError:
        pass   # the scan is still good for this call


SURFACES = {5890: "plane", 5891: "cylinder", 5893: "cone", 5895: "torus", 5896: "sphere", 5897: "bspline"}


def face_info(row: list, surfaces: dict = SURFACES) -> dict:
    """A face as find_faces describes it, from the numbers read_brep.iLogicVb hands back
    (cm, unrounded) — the same arithmetic as InventorConnection._face_info, so the answer
    does not depend on how the face was read."""
    st = int(row[0])
    lo, hi = row[2:5], row[5:8]
    info = {"type": surfaces.get(st, str(st)), "area_mm2": round(row[1] * 100, 1),
            "center": [round((a + b) / 2 * 10, 3) for a, b in zip(lo, hi)]}
    if st == 5890:
        s = -1 if row[11] else 1
        info["normal"] = [round(s * c, 4) + 0.0 for c in row[8:11]]
    elif st in (5891, 5893):
        info["axis"] = [round(c, 4) + 0.0 for c in row[8:11]]
        info["axis_point"] = [round(c * 10, 3) for c in row[11:14]]
        if st == 5891:
            info["diameter"] = round(row[14] * 20, 3)
    return info


def edge_info(row: list) -> dict:
    """An edge as find_edges describes it — the arithmetic of InventorConnection._edge_info."""
    kind = int(row[0])
    if kind == 5124:
        return {"type": "circle", "diameter": round(row[1] * 20, 3), "at": [round(c * 10, 3) for c in row[2:5]]}
    a, b = row[1:4], row[4:7]
    return {"type": {5123: "line", 5125: "arc"}.get(kind, "curve"),
            "length": round(sum((p - q) ** 2 for p, q in zip(a, b)) ** 0.5 * 10, 3),
            "at": [round((p + q) * 5, 3) for p, q in zip(a, b)]}


class ScanMixin:
    def reading_mode(self) -> str:
        """How faces and edges are read on this connection."""
        if os.environ.get("INVENTOR_MCP_RULES", "1").strip() in ("0", "off", "false"):
            return "through COM (INVENTOR_MCP_RULES=0)"
        off = getattr(self, "_rules_off", "")
        if off:
            return f"through COM — slower; rules inside Inventor are off: {off}"
        broken = getattr(self, "_rules_broken", {})
        return "inside Inventor (iLogic rules)" + (
            f"; not {', '.join(sorted(broken))}: it did not run here, that reading goes through COM" if broken else "")

    def _inside(self, rule: str, **arguments):
        """The rule's answer, or None when the caller should read through COM instead:
        rules switched off, iLogic not there or refusing (remembered for this connection),
        or the rule could not do this one request (say, a component name that does not exist
        — the COM code then gives its own, fuller error)."""
        if not self.reading_mode().startswith("inside") or rule in getattr(self, "_rules_broken", {}):
            return None
        try:
            return self._run_rule(self.active_document(), rule, **arguments)
        except RuntimeError as e:
            text = str(e)
            if "gave no result" in text:                 # this rule does not run on this Inventor
                self._rules_broken = {**getattr(self, "_rules_broken", {}), rule: text[:200]}
            elif "failed inside Inventor" not in text:   # iLogic itself is the problem, not this request
                self._rules_off = text[:160]
            return None
        except Exception as e:                           # anything COM: don't let a faster path break a tool
            self._rules_off = describe_com_error(e)[:160]
            return None

    def _read_brep(self, what: str, path="") -> list | None:
        """Faces or edges ("what") of the active part or of a component, read inside Inventor:
        [{"path", "bodies": [[row, …], …]}], in the order the COM code walks them."""
        got = self._inside("read_brep.iLogicVb", what=what, path=str(path))
        return None if got is None else got["leaves"]

    def _read_parts(self, flat: bool = False) -> list | None:
        """Every different part of the active assembly (or the active part), read inside
        Inventor: material, mass, properties, and for sheet metal the rule, thickness and —
        with flat — the flat pattern. None: read through COM instead."""
        got = self._inside("read_parts.iLogicVb", **({"flat": "1"} if flat else {}))
        return None if got is None else got["parts"]

    def _walk(self) -> dict | None:
        """The active assembly's first-level components ("top") and the file of every part
        occurrence at any depth ("files"), read inside Inventor."""
        return self._inside("read_brep.iLogicVb", what="walk")

    def _run_rule(self, doc, rule: str, **arguments) -> dict:
        """Run rules/<rule> on doc inside Inventor and return the JSON it hands back in the
        argument "result". (Not through a file: a rule that writes one gets iLogic's Security
        Alert, a dialog that holds the call under SilentOperation.)"""
        rule_path = os.path.join(RULES, rule)
        try:
            addin = self.app.ApplicationAddIns.ItemById(ILOGIC_ADDIN)
            if not addin.Activated:
                addin.Activate()
            automation = addin.Automation
        except Exception as e:
            raise RuntimeError("This needs Inventor's iLogic add-in (Tools > Add-Ins > iLogic), and it could "
                               f"not be reached: {describe_com_error(e)}") from None
        # iLogic shows its own dialogs for a rule that does not compile or fails ("Rule Compile
        # Errors …", "Error on line …") — Inventor's SilentOperation does not stop those, and the
        # call waits behind them. Its own SilentOperation does: the call then comes straight
        # back with an error code. Without that switch a rule is not run at all.
        try:
            was_silent = bool(automation.SilentOperation)
            automation.SilentOperation = True
        except Exception:
            raise RuntimeError("This Inventor's iLogic can't be kept from showing error dialogs "
                               "(no Automation.SilentOperation), so rules are not run on it") from None
        args = self.app.TransientObjects.CreateNameValueMap()
        for key, value in arguments.items():
            args.Add(key, value)
        try:
            code = self._silently(lambda: automation.RunExternalRuleWithArguments(doc, rule_path, args))
        finally:
            try:
                automation.SilentOperation = was_silent
            except Exception:
                pass
        try:
            text = args.Item("result")
        except Exception:
            raise RuntimeError(f"The rule {rule} gave no result (iLogic answered {code!r}) — it did not run "
                               "to its end: it does not compile on this Inventor, or iLogic refused it "
                               "(Tools > Options > iLogic Configuration > security).") from None
        result = json.loads(text)
        if "error" in result:
            raise RuntimeError(f"The rule {rule} failed inside Inventor: {result['error'][:600]}")
        return result

    def scan_planar(self, refresh: bool = False) -> dict:
        """Planar faces and their openings for every part of the active document, and for an
        assembly every part occurrence with its transform. A file is read once and then
        taken from disk until it changes; a part with unsaved changes is always read anew."""
        doc = self.active_document()
        if doc.DocumentType not in (kPartDocumentObject, kAssemblyDocumentObject):
            raise RuntimeError("The active document is neither a part nor an assembly.")
        rule = "scan_planar.iLogicVb"
        if not self.reading_mode().startswith("inside"):
            raise RuntimeError("This reading runs inside Inventor (iLogic rules) and has no other way: "
                               + self.reading_mode())
        version = _rule_version(os.path.join(RULES, rule))
        started = time.perf_counter()
        walk = self._run_rule(doc, rule, files="", walk="1")
        documents = walk["documents"]
        parts, fresh = {}, []
        for path, state in documents.items():
            cached = None if (refresh or state["dirty"] or path.startswith("unsaved:")) \
                else cache_get(path, version)
            if cached is None:
                fresh.append(path)
            else:
                parts[path] = cached
        if fresh:
            scanned = self._run_rule(doc, rule, files="|".join(p for p in fresh if p))["parts"]
            for path, part in scanned.items():
                parts[path] = part
                if path and not path.startswith("unsaved:") and not documents[path]["dirty"]:
                    cache_put(path, version, part)
        if doc.DocumentType == kPartDocumentObject:
            occurrences = [{"path": doc.DisplayName, "file": next(iter(documents), ""), "visible": True,
                            "m": IDENTITY}]
        else:
            occurrences = walk.get("occurrences", [])
        return {"occurrences": occurrences, "parts": parts, "documents": documents,
                "scanned_now": len(fresh), "from_cache": len(documents) - len(fresh),
                "seconds": round(time.perf_counter() - started, 2)}

    def fastener_alignment(self, parts: list[str] | None = None, max_gap: float = 0.5,
                           tolerance: float = 0.1, refresh: bool = False,
                           include_fasteners: bool = False, limit: int = 40) -> dict:
        """Do the holes and slots of parts that lie on each other line up? See alignment.check."""
        import fnmatch
        self.active_assembly()
        scan = self.scan_planar(refresh)
        occs = [o for o in scan["occurrences"] if o.get("visible", True)]
        if not include_fasteners:
            occs = [o for o in occs if not scan["documents"].get(o["file"], {}).get("content")]
        if parts:
            pats = [p.lower() for p in parts]
            occs = [o for o in occs if any(fnmatch.fnmatch(o["path"].lower(), p)
                                           or fnmatch.fnmatch(os.path.basename(o["file"]).lower(), p) for p in pats)]
            if not occs:
                raise ValueError(f"No part matches {', '.join(parts)}")
        started = time.perf_counter()
        found = alignment.check(occs, scan["parts"], max_gap, tolerance)
        sizes = {}
        for row in found.pop("aligned_rows"):
            key = f"{row['a_opening']} on {row['b_opening']}" + (f" ({row['passes']})" if "passes" in row else "")
            sizes[key] = sizes.get(key, 0) + 1
        studs = {}
        for row in found.pop("engaged_rows"):
            key = f"{row['opening']} hole on {row['stud']} stud"
            studs[key] = studs.get(key, 0) + 1
        out = {"parts": found["parts"], "pairs_of_parts_in_contact": found["pairs_of_parts_in_contact"],
               "aligned": found["aligned"],
               "aligned_by_size": dict(sorted(sizes.items(), key=lambda kv: -kv[1])[:15]),
               "engaged": sum(studs.values()),
               "engaged_by_size": dict(sorted(studs.items(), key=lambda kv: -kv[1])[:15])}
        for key in ("misaligned", "stud_does_not_fit", "blocked", "open"):
            rows = found[key]
            out[key + "_count"] = len(rows)
            out[key] = rows[:limit]
        skipped = sum(p.get("skipped", 0) for p in scan["parts"].values())
        notes = []
        if skipped:
            notes.append(f"{skipped} planar face(s) could not be read and were left out")
        unread = sorted({o["path"] for o in occs if o["file"] not in scan["parts"]})
        if unread:
            notes.append(f"{len(unread)} component(s) could not be read and were left out: "
                         + ", ".join(unread[:8]))
        for key in ("misaligned", "stud_does_not_fit", "blocked", "open"):
            if out[key + "_count"] > limit:
                notes.append(f"{key}: the first {limit} of {out[key + '_count']} — narrow it down with `parts`")
        out["notes"] = notes
        out["read"] = {"files": len(scan["documents"]), "scanned_now": scan["scanned_now"],
                       "from_cache": scan["from_cache"], "scan_seconds": scan["seconds"],
                       "compare_seconds": round(time.perf_counter() - started, 2)}
        return out

    # ---------- One look at an assembly before it goes to the shop ----------

    REVIEW_CHECKS = ("documents", "sheet_metal", "holes", "clearance", "interference", "constraints")
    _GENERIC_MATERIALS = ("generic", "default")

    @staticmethod
    def _grouped(rows: list, key, text, limit: int) -> list:
        """Rows that say the same thing become one line with a count and one example."""
        groups = {}
        for r in rows:
            groups.setdefault(key(r), []).append(r)
        out = [{"text": text(k, g), "count": len(g), "example": g[0]} for k, g in groups.items()]
        out.sort(key=lambda r: -r["count"])
        return out[:limit] + ([{"text": f"… and {len(out) - limit} more kinds", "count": sum(r["count"] for r in out[limit:])}]
                              if len(out) > limit else [])

    def review_assembly(self, checks: list[str] | None = None, clearance: float = 0.3, min_overlap: float = 1.0,
                        v_die: float = 0, sheet: str = "", brake_length: float = 0, limit: int = 12) -> dict:
        """Everything that can be checked about the active assembly without changing it, in
        one answer: problems, things to look at, and — so that silence is never read as
        "fine" — what was not checked and why."""
        doc = self.active_document()
        if doc.DocumentType != kAssemblyDocumentObject:
            raise RuntimeError("review_assembly looks at an assembly — for one sheet metal part use "
                               "check_manufacturability.")
        wanted = [c.strip().lower() for c in (checks or self.REVIEW_CHECKS)]
        unknown = [c for c in wanted if c not in self.REVIEW_CHECKS]
        if unknown:
            raise ValueError(f"Unknown check {', '.join(unknown)} — the checks are {', '.join(self.REVIEW_CHECKS)}")
        started = time.perf_counter()
        problems, look, unchecked, ran = [], [], [], {}
        base = lambda path: os.path.basename(_file_of(path)) if path else "?"

        def add(to, check, text, **more):
            to.append({"check": check, "text": text, **more})

        def section(name, fn):
            """One check: its time, and whatever goes wrong inside it is a line under not_checked."""
            if name not in wanted:
                add(unchecked, name, "not asked for (checks)")
                return
            t = time.perf_counter()
            try:
                summary = fn()
            except Exception as e:
                summary = None
                add(unchecked, name, f"failed: {describe_com_error(e) if type(e).__name__ == 'com_error' else e}")
            if summary is not None:
                ran[name] = f"{summary} ({time.perf_counter() - t:.1f} s)"

        parts_read = self._read_parts(flat="sheet_metal" in wanted) if {"documents", "sheet_metal"} & set(wanted) else None
        scan = None
        if {"holes", "clearance"} & set(wanted):
            try:
                scan = self.scan_planar()
            except Exception as e:
                scan = str(e)
        slow = "this reading runs inside Inventor (iLogic rules), and they are off here: " + self.reading_mode()

        def documents():
            if parts_read is None:
                add(unchecked, "documents", slow + " — manage_materials(action='report') reads the same through COM")
                return None
            own = [r for r in parts_read if not r["content"]]
            generic = [r["name"] for r in own if r["material"].lower() in self._GENERIC_MATERIALS]
            if generic:
                add(problems, "documents", f"{len(generic)} part(s) have no material (Generic) — mass and BOM are wrong: "
                    + ", ".join(generic[:limit]) + (" …" if len(generic) > limit else ""), count=len(generic))
            massless = [r["name"] for r in own if not r["mass"] and not r["surfaces"]]
            if massless:
                add(problems, "documents", f"{len(massless)} part(s) have no mass: " + ", ".join(massless[:limit]),
                    count=len(massless))
            surfaces = [r["name"] for r in own if r["surfaces"] and not r["overridden"]]
            if surfaces:
                add(problems, "documents", f"{len(surfaces)} part(s) are made of surfaces — no volume, mass too low "
                    "(convert_surfaces_to_solid, or set the mass): " + ", ".join(surfaces[:limit]), count=len(surfaces))
            for label, field in (("part number", "part_number"), ("description", "description")):
                missing = [r["name"] for r in own if not r[field].strip()]
                if missing:
                    add(problems if field == "part_number" else look, "documents",
                        f"{len(missing)} part(s) have no {label}: " + ", ".join(missing[:limit])
                        + (" …" if len(missing) > limit else ""), count=len(missing))
            unsaved = [r["name"] for r in parts_read if r["dirty"] or not r["file"]]
            if unsaved:
                add(look, "documents", f"{len(unsaved)} part(s) have unsaved changes — what is on disk is not what "
                    "was checked: " + ", ".join(unsaved[:limit]), count=len(unsaved))
            return f"{len(parts_read)} different parts, {len(parts_read) - len(own)} of them Content Center"

        def sheet_metal():
            if parts_read is None:
                add(unchecked, "sheet_metal", slow + " — check_manufacturability checks one part at a time")
                return None
            sheets = [r for r in parts_read if r.get("sheet")]
            judged = 0
            for r in sheets:
                read = r["sheet"]
                if "error" in read:
                    add(unchecked, "sheet_metal", f"{r['name']}: could not be read ({read['error']})")
                elif not read["has_flat"]:
                    add(unchecked, "sheet_metal", f"{r['name']}: no flat pattern yet — nothing is unfolded here; "
                        "check_manufacturability on the part unfolds and checks it")
                else:
                    verdict = judge_sheet(r["name"], sheet_facts(read), v_die, 0, 0, sheet, brake_length)
                    judged += 1
                    same = {}   # "Bend 2: cut-out 7 is 3.6 mm from …" for cut-outs 1 to 12 is one line
                    for text in verdict["problems"]:
                        same.setdefault(re.sub(r"(hole|cut-out) \d+ ", r"\1 ", text), []).append(text)
                    for text, group in same.items():
                        if len(group) == 1:
                            text = group[0]
                        else:
                            text = re.sub(r"(Ø[\d.]+ hole|cut-out) (is|lies)",
                                          lambda m: f"{len(group)} {m.group(1)}s {'are' if m.group(2) == 'is' else 'lie'}",
                                          text).replace(" it is pulled", " they are pulled").replace("move it or cut it", "move them or cut them")
                        add(problems, "sheet_metal", f"{r['name']}: {text}", count=len(group))
                    for text in verdict["notes"]:
                        add(look, "sheet_metal", f"{r['name']}: {text}")
            return f"{judged} of {len(sheets)} sheet metal part(s) checked on their flat pattern"

        def visible_parts():
            return [o for o in scan["occurrences"] if o.get("visible", True)
                    and not scan["documents"].get(o["file"], {}).get("content")]

        def holes():
            if not isinstance(scan, dict):
                add(unchecked, "holes", f"could not be read: {scan}")
                return None
            occs = visible_parts()
            file_of = {o["path"]: base(o["file"]) for o in occs}
            found = alignment.check(occs, scan["parts"])
            for g in self._grouped(found["misaligned"],
                                   lambda r: (file_of[r["a"]], file_of[r["b"]], r["a_opening"], r["b_opening"], r["offset_mm"]),
                                   lambda k, g: f"{k[2]} in {k[0]} and {k[3]} in {k[1]} are {k[4]:g} mm off"
                                                + (f" — {g[0]['passes']} still passes" if "passes" in g[0] else ""), limit):
                add(problems, "holes", g.pop("text"), **g)
            for g in self._grouped(found["stud_does_not_fit"],
                                   lambda r: (file_of[r["part"]], file_of[r["stud_of"]], r["opening"], r["stud"]),
                                   lambda k, g: f"the {k[3]} stud of {k[1]} does not fit the {k[2]} hole of {k[0]} "
                                                f"({-g[0]['clearance_mm']:g} mm too tight)", limit):
                add(problems, "holes", g.pop("text"), **g)
            for kind, rows, other in (("has solid material behind it", found["blocked"], "behind"),
                                      ("has nothing behind it", found["open"], "touching")):
                for g in self._grouped(rows, lambda r: (file_of[r["part"]], r["opening"], file_of[r[other]]),
                                       lambda k, g: f"{k[1]} in {k[0]} {kind}"
                                                    + (f": {k[2]}" if other == "behind" else f" (it lies on {k[2]})"), limit):
                    add(look, "holes", g.pop("text"), **g)
            unread = sorted({o["path"] for o in occs if o["file"] not in scan["parts"]})
            if unread:
                add(unchecked, "holes", f"{len(unread)} component(s) could not be read: " + ", ".join(unread[:6]))
            add(unchecked, "holes", "only flat faces are compared: a hole with a curved part behind it reads as having "
                "nothing behind it, and what sits inside a hole as its own part is not looked at")
            return (f"{found['aligned']} aligned, {len(found['engaged_rows'])} with a stud in them, "
                    f"{len(found['misaligned'])} off, {len(found['blocked'])} with solid behind, {len(found['open'])} open")

        def clearances():
            if not isinstance(scan, dict):
                add(unchecked, "clearance", f"could not be read: {scan}")
                return None
            occs = visible_parts()
            file_of = {o["path"]: base(o["file"]) for o in occs}
            rows = alignment.close_faces(occs, scan["parts"], clearance)
            for g in self._grouped(rows, lambda r: (file_of[r["a"]], file_of[r["b"]], r["gap_mm"]),
                                   lambda k, g: f"{k[0]} and {k[1]} are {k[2]:g} mm apart — less than {clearance:g} mm, "
                                                "no room for the coat", limit):
                add(problems, "clearance", g.pop("text"), **g)
            add(unchecked, "clearance", "only flat faces that face each other are measured — "
                "check_manufacturability on the assembly measures every pair of parts (slow)")
            return f"{len(rows)} pair(s) of parts closer than {clearance:g} mm without touching"

        def interference():
            got = self._inside("read_brep.iLogicVb", what="interference")
            if got is None:
                add(unchecked, "interference", slow + " — check_interference does it through COM")
                return None
            rows = [{"a": r[0], "b": r[1], "volume_mm3": round(r[2] * 1000, 3),
                     "at": [round((r[3 + i] + r[6 + i]) * 5, 2) for i in range(3)]} for r in got["rows"]]
            name = lambda step: step.rsplit(":", 1)[0]
            pair = lambda r: tuple(sorted((name(r["a"].split("/")[-1]), name(r["b"].split("/")[-1]))))

            def home(r):
                """The sub-assembly both parts sit in ('' when they don't share one), without instance numbers."""
                a, b = r["a"].split("/")[:-1], r["b"].split("/")[:-1]
                shared = []
                for x, y in zip(a, b):
                    if x != y:
                        break
                    shared.append(name(x))
                return "/".join(shared)
            real = [r for r in rows if r["volume_mm3"] >= min_overlap]
            inside = [r for r in real if home(r)]
            for g in self._grouped(inside, home,
                                   lambda k, g: f"inside {k}: {len({pair(x) for x in g})} pair(s) of its parts overlap — "
                                                f"up to {max(x['volume_mm3'] for x in g):g} mm³ (a bought-in model "
                                                "often does; otherwise fix it there)", limit):
                add(look, "interference", g.pop("text"), **g)
            for g in self._grouped([r for r in real if not home(r)], pair,
                                   lambda k, g: f"{k[0]} and {k[1]} overlap — up to "
                                                f"{max(x['volume_mm3'] for x in g):g} mm³", limit):
                add(problems, "interference", g.pop("text"), **g)
            small = len(rows) - len(real)
            if small:
                add(look, "interference", f"{small} overlap(s) under {min_overlap:g} mm³ (a touching edge, a thread)",
                    count=small)
            return f"{got['parts']} parts, {len(rows)} overlap(s), {len(real)} of them {min_overlap:g} mm³ or more"

        def constraints():
            comp = doc.ComponentDefinition
            sick = self._sick_constraints(comp)
            if sick:
                add(problems, "constraints", f"{len(sick)} constraint(s) Inventor can't solve: " + ", ".join(sick[:limit]),
                    count=len(sick))
            walk = self._walk()
            if walk is None:
                add(unchecked, "constraints", "whether a component is grounded — " + slow)
            elif walk["top"] and not any(row[4] for row in walk["top"]):
                add(look, "constraints", "no component is grounded: the whole assembly can be dragged out of place")
            return f"{comp.Constraints.Count} constraint(s), {len(sick)} unsolved"

        for name, fn in (("documents", documents), ("sheet_metal", sheet_metal), ("holes", holes),
                         ("clearance", clearances), ("interference", interference), ("constraints", constraints)):
            section(name, fn)
        if isinstance(scan, dict):
            hidden = sum(1 for o in scan["occurrences"] if not o.get("visible", True))
            if hidden:
                add(unchecked, "holes", f"{hidden} hidden component(s) are left out of the hole, clearance and "
                    "interference checks")
        return {"assembly": doc.DisplayName, "checked": ran, "problems": problems, "to_look_at": look,
                "not_checked": unchecked, "seconds": round(time.perf_counter() - started, 1)}
