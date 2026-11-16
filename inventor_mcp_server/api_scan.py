"""Reading geometry in bulk: a rule run inside Inventor, its results kept per file on disk."""
import hashlib
import json
import time

if __package__:
    from .api_common import *  # noqa: F401,F403
    from . import alignment
else:  # server.py run as a script: the modules sit on sys.path
    from api_common import *  # noqa: F401,F403
    import alignment

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


def cache_get(path: str, version: str):
    """The stored scan of a file, if the file on disk is still the one that was scanned."""
    try:
        st = os.stat(path)
        with open(_cache_file(path), encoding="utf-8") as f:
            entry = json.load(f)
        if entry["rule"] == version and entry["mtime_ns"] == st.st_mtime_ns and entry["size"] == st.st_size:
            return entry["part"]
    except (OSError, ValueError, KeyError):
        pass
    return None


def cache_put(path: str, version: str, part: dict) -> None:
    try:
        st = os.stat(path)
        os.makedirs(SCAN_CACHE, exist_ok=True)
        target = _cache_file(path)
        with open(target + ".tmp", "w", encoding="utf-8") as f:
            json.dump({"file": path, "rule": version, "mtime_ns": st.st_mtime_ns, "size": st.st_size,
                       "part": part}, f)
        os.replace(target + ".tmp", target)
    except OSError:
        pass   # the scan is still good for this call


class ScanMixin:
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
        args = self.app.TransientObjects.CreateNameValueMap()
        for key, value in arguments.items():
            args.Add(key, value)
        self._silently(lambda: automation.RunExternalRuleWithArguments(doc, rule_path, args))
        try:
            text = args.Item("result")
        except Exception:
            raise RuntimeError(f"The rule {rule} gave no result — it did not run to its end. iLogic may have "
                               "refused it (Tools > Options > iLogic Configuration > security), or it does "
                               "not compile on this Inventor.") from None
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
        version = _rule_version(os.path.join(RULES, rule))
        started = time.perf_counter()
        walk = self._run_rule(doc, rule, files="", walk="1")
        documents = walk["documents"]
        parts, fresh = {}, []
        for path, state in documents.items():
            cached = None if (refresh or state["dirty"] or not path) else cache_get(path, version)
            if cached is None:
                fresh.append(path)
            else:
                parts[path] = cached
        if fresh:
            scanned = self._run_rule(doc, rule, files="|".join(p for p in fresh if p))["parts"]
            for path, part in scanned.items():
                parts[path] = part
                if path and not documents[path]["dirty"]:
                    cache_put(path, version, part)
        if doc.DocumentType == kPartDocumentObject:
            occurrences = [{"path": doc.DisplayName, "file": doc.FullFileName, "visible": True, "m": IDENTITY}]
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
        if any(not path for path in scan["documents"]):
            notes.append("Parts that were never saved are not told apart — save them first")
        for key in ("misaligned", "stud_does_not_fit", "blocked", "open"):
            if out[key + "_count"] > limit:
                notes.append(f"{key}: the first {limit} of {out[key + '_count']} — narrow it down with `parts`")
        out["notes"] = notes
        out["read"] = {"files": len(scan["documents"]), "scanned_now": scan["scanned_now"],
                       "from_cache": scan["from_cache"], "scan_seconds": scan["seconds"],
                       "compare_seconds": round(time.perf_counter() - started, 2)}
        return out
