# Changelog

## 0.9.0 — 2026-10-27

### Added
- **`surface_to_solid`** — imported vendor models that came in as surfaces (no volume, no mass, no
  booleans) become solids: the part's visible surfaces are knitted; a shell with holes gets a boundary
  patch per loop of open edges and is knitted again; a shell that is already closed becomes a solid
  base feature. Reports each step, the solids' volumes, and the open edges it couldn't close.
  `KnitFeatures.Add` fails with E_FAIL on a single closed shell (nothing to stitch) — which is what an
  agent hit on every attempt.
- **Materials for many parts.** Parts were created on "Generic" and given materials one by one later.
  - `set_material(material, parts="*PLATE*,*BRACKET*", only_generic, path)` — every matching part of
    the assembly at once; Content Center and library parts are left alone;
  - `material_report` — material and mass per part, counts per material, total mass, and what needs a
    look: Generic parts, vendor models made of surfaces (no volume, mass too low), parts with no mass
    (e.g. a Content Center member placed without a length);
  - `list_materials(search)` — the exact library names; a misspelled name gets the closest ones;
  - `create_part(material=, part_number=, description=)`;
  - `properties(mass_kg=…)` overrides the mass (a 1.23 kg motor made of surfaces showed 0.23 kg);
  - `inspect` warns about surface bodies (WorkSurfaces and open shells).
- **Faces of nested components.** `assembly_faces(path, surface, normal, at, diameter)` finds faces of
  a component anywhere in the assembly ("Sub:1/Part:1"; a sub-assembly path searches every part in
  it), in top assembly coordinates, each with a ref ("Sub:1/Part:1#F12"). Agents had to walk
  occurrence → SubOccurrences → SurfaceBodies → Faces and filter by hand every time.
- **`constrain_faces(a, b, kind, offset, a_at)`** — mate / flush / angle between those faces or origin
  geometry ("path#XY") of components at any depth. `a_at={"Z": 182.5}` says where a's face must end
  up; a wrong offset sign is corrected. It reports every OTHER component the constraint moved —
  constraints that dragged parts out of place went unnoticed before.
- `list_occurrences(depth=2)` lists the components inside sub-assemblies with their paths.

### Fixed
- **The server starts and lists its tools on Linux** (Glama's build check runs it in a Debian
  container): the Windows-only imports load only on Windows; elsewhere Inventor calls answer that
  Windows with Inventor is required. Nothing changes on Windows.

## 0.8.0 — 2026-10-27

### Added
- **`sketch_pattern`** — many copies of a circle, slot or (rounded) rectangle in one sketch: a grid
  (rows × columns, pitch) or a circle of them (count, PCD, start angle; copies turn with it) — vent
  slots, perforations, bolt circles cut in one go. Sketch updates are deferred while drawing.
- **`check_sketch`** — why a sketch won't extrude: open ends, gaps (two ends a hair apart), points
  where 3+ curves meet, lines drawn twice, with sketch coordinates; whether Inventor builds a profile
  and how many loops. `extrude`, `revolve` and the sheet metal tools now say the same when the
  profile fails, instead of a bare E_INVALIDARG.
- **`offset_profile`** — offset every closed loop of a sketch inside or outside (gasket groove, wall,
  margin); lines, arcs and circles stay what they are.
- **Arcs in sketches.** Agents had no way to draw an arc, so round corners and holes came out as
  many short lines (faceted parts). Now:
  - `draw_closed_profile` takes rounded corners `[x, y, r]` (tangent arcs, lines trimmed, tangent
    constraints) and arcs through a point `{"through": [x, y]}`;
  - `draw_rectangle(corner_radius=…)`, `draw_slot`, `draw_arc` (through a point, or around a center);
  - `project_edges` — exact edges of a face / body in the sketch, for remodelling;
  - every arc goes through three points, so a sketch on a face whose axes are flipped can't mirror it
    (the counter-clockwise flag of `AddByCenterStartEndPoint` did);
  - `inspect` warns when a body has many shallow kinks between straight edges — a curve drawn as lines.
- **`call_status(wait_seconds=…)`** — waits up to 45 s (never past the call timeout) for the running
  call and returns its result as soon as it finishes, instead of agents polling it 20+ times per long
  job. The wait runs in a worker thread, so the server keeps answering other agents meanwhile.
- **`cancel_call`** — stops the running call: `cancelled()` in `execute_python` code turns True at
  once, so a loop that polls it stops cleanly; code still running 2 s later gets a `KeyboardInterrupt`
  in the Inventor thread (`PyThreadState_SetAsyncExc`), repeated every 0.5 s until the call ends —
  once is not enough, win32com's own `dynamic.py` has bare `except:` blocks that swallow it, and a loop
  of COM calls spends most of its time there. It lands at the next Python line, not inside a blocking
  COM call or behind a modal dialog. It can never hit the next call: it is only raised while the call
  runs, and the thread clears a pending one when the call ends.
- **`execute_python(timeout=…)`** — cancels the run the same way after that many seconds (grace period included).
- **Live output** — prints of a running `execute_python` show in `call_status` (and in the first "still
  running" answer) as they happen, last lines first; `progress(msg)` in the namespace sets a status
  line `call_status` shows.
- **`save_document(save_dependents=…)`** — `False` saves only the document (`Save2(False)`). The answer
  lists the files saved and the ones skipped.

### Fixed
- **Saving never writes library or Content Center files.** `Save2(True)` saved every dirty dependent —
  including standard Content Center parts Inventor marks dirty when it opens an old assembly. Now each
  dirty dependent is saved on its own, except files under the project's library paths, its Content
  Center folder, the application's Content Center files folder, and Content Center members; a library
  file as the active document, or a save-as into such a folder, is refused.
- **Readable COM errors.** Tracebacks and tool errors name the HRESULT (E_FAIL, E_INVALIDARG,
  RPC_E_DISCONNECTED "re-resolve it from app/doc", RPC_E_CALL_REJECTED, …) and the inner code of a
  DISP_E_EXCEPTION, with a hint, instead of Windows' localized "Exception occurred."; Inventor's own
  message (`ErrorManager`) is added when it has one.
- **Health status by name** — `list_features`, `inspect` and `list_constraints` flag features and
  constraints that are not healthy with the HealthStatusEnum name (`kInErrorHealth`,
  `kInconsistentHealth`, …), read from the type library at runtime.
- `list_faces` and the SurfaceTypeEnum notes had 5892–5896 wrong (5892 is an elliptic cylinder, 5893 a
  cone, 5896 a sphere).

### Docs
- API notes: stale objects and lagging collections, stale assembly MassProperties, tuple-returning
  calls, RangeBox slack on curved faces, ungrounded assembly work features, directed angle
  constraints, invisible documents dropped, and features that can't be knit, flipped or redefined.

## 0.7.0 — 2026-10-27

### Added
- **`frame_members`** — a steel frame from skeleton lines: each member (start, end, profile, roll,
  `anchor` center/origin, `extend` at either end) becomes a Content Center custom-length part turned
  along its line; one file per profile and length, reused. Returns the placements and a cut list.
  Frame Generator itself exposes no API (its add-in has no Automation object and its commands need
  interactive selection), so this does its core job with the Content Center.
- **`view_image(up="z")`** — named views for models built with Z up (Inventor's own assume Y up):
  front looks along +Y, top down -Z.

- **Inventors survive a server restart.** Every Inventor the server starts is handed to a keeper
  process (`keeper.py`, started detached, outliving the client's job when allowed) that registers one
  object in the Running Object Table; a restarted server gets the Inventors back from it:
  `connect(project=...)` reuses a kept Inventor in that project, `connect(pid=...)` picks one, and
  `list_instances` lists them. Inventor's own object can't be registered under a new name from
  another process (E_INVALIDARG for a proxy), hence the go-between. The keeper drops an Inventor
  when its main window is gone or hidden (window checks only, no COM call) and exits after 10 min
  holding none (`INVENTOR_MCP_KEEPER_IDLE`).

### Fixed
- **`content_center` with a "/" in a category name** ("Structural Shapes/Square/Rectangular Tubes")
  — the path is matched longest-name-first.
- `content_center` reuses a custom member file that already exists instead of asking Content
  Center to make it again.

## 0.6.0 — 2026-10-27

### Added
- **`flat_pattern`** — flat size, thickness and bend count of a sheet metal part, and with a path a
  DXF/DWG for the laser: OUTER contour, INNER holes, bend lines on IV_BEND* (optional); tangent
  lines, sketches and the like left out. Creates the flat pattern if missing.
- **`make_drawing`** — PDF drawing of the active saved part/assembly from a template (ISO, DIN, …):
  base view, projected views (top below, right beside), a shaded iso at half scale, overall
  dimensions on the base view, retrieved model dimensions; `flat=True` draws the flat pattern with
  its overall size. Largest standard scale that fits. Optionally keeps the .idw.
- **`bom`** — parts-only or structured bill of materials: item, quantity, part number, description,
  material, stock number, mass, file; optionally a CSV (UTF-8, `;`) for Excel / ERP.
- **`properties`** — read and set iProperties (part number, description, stock number, vendor,
  revision, title, cost center, custom properties) and the material from the library.
- `connect(new_instance=True)` says so when Windows hands back an Inventor that was already running
  instead of starting one (it can hang).
- **`view_image`** — pictures of the active document returned as MCP images, so the agent can see
  its work: named views (iso, front, top, …; several per call), size, white background, `focus` on
  an assembly component. Sketches and work features are hidden while capturing; the camera and
  visibility are restored, so the user's view doesn't jump.

## 0.5.1 — 2026-10-27

### Added
- **`content_center`** — browse the Content Center step by step (category path → families → sizes)
  and make a member's part file; `length` makes a custom-length structural member. Nodes and families
  are cached per Inventor, since walking the whole tree takes minutes.
- **`place_many`** — place many components in one call (position or origin, `x_axis`/`y_axis`,
  grounded, name). All files are checked before anything is placed.
- **`load_library`** — run a Python file into the `execute_python` namespace and keep it: it is
  loaded again by itself after `reload_api` or a server restart. `~/.inventor-mcp/startup.py`
  (or `INVENTOR_MCP_STARTUP`) is loaded the same way for every Inventor. `status` lists them.
- **`execute_python(undo_on_error=True)`** — the call runs in one undo transaction and is rolled back
  if it raises: no half-built sketches or work planes left behind.
- **`close_unsaved`** — close never-saved documents left by failed attempts (not ones another
  document references). `status` lists them.
- **`save_document(overwrite=...)`**.
- **`connect(project=...)` finds or starts the right Inventor.** It reuses a connected Inventor that
  already has the project, else the running Inventor if it can switch (no documents open, or already
  in that project), else starts a separate Inventor named after the project. An agent only needs to
  know its project — not whether another agent is already working. An Inventor with documents open
  in another project is left alone.

### Fixed
- **`execute_python` tracebacks show the agent's own line.** Only the server's three outermost frames
  were returned. Now the traceback starts at the agent's code, with source lines — also for a helper
  defined in an earlier call. stderr (`traceback.print_exc()`) is captured too.
- **`save_document` explains a bad path** instead of Inventor's `E_INVALIDARG`: invalid characters,
  wrong extension, missing folder (a `/` in the name), a file that exists or is open in Inventor.
  Saving again to the document's own path saves in place.
- **Assembly mass was stale** after sub-assemblies changed (3607 kg reported, 3343 kg real):
  `assembly_info` updates the document and recomputes instead of returning the cached value.
- **`hole` that cut nothing was left behind.** If no material is removed (point off the body), the
  feature and its sketch are deleted and the call fails with a hint. Parts that reject plain numbers
  for the diameter/depth (Content Center members) get values with units.
- **A new Inventor wasn't ready.** `connect(new_instance=True)` waits until Inventor reports
  `Ready` — Content Center calls failed for the first seconds.

## 0.5.0 — 2026-10-27

### Fixed
- **A modal dialog no longer freezes the server.** A call that made Inventor open a dialog (e.g.
  `Documents.Open` → "resolved to a different file — use it?", or `Save` → "save dependents?") never
  returned, and because tools ran on the server's event loop every later call — read-only ones
  included — timed out too, until someone clicked the dialog. Now:
  - every call runs with `SilentOperation` on, so Inventor answers its own prompts with the default
    (this includes `execute_python`, which did not use it before);
  - calls run on a dedicated Inventor thread and the tool waits at most 50 s
    (`INVENTOR_MCP_CALL_TIMEOUT`). A call still running then returns with the open dialog's title and
    text — read from the window list, no COM call — or "still working" for a long job;
  - a call is refused, with the same report, while a dialog is open or another call is running.
- **Immune to a `gen_py` cache.** If anything on the machine ran `CastTo` / `gencache.EnsureDispatch`
  for the Inventor type library, `connect` failed with `KeyError: '_dispobj_'` and a generic
  `Document` (e.g. from `Documents.Open`) hid `ComponentDefinition`. The server now never uses gen_py
  classes; every object is late-bound with type info.

### Added
- **Several Inventors at once.** `connect(new_instance=True, project=..., instance=...)` starts a
  separate Inventor and makes the project active; every tool takes an `instance` parameter, and each
  Inventor has its own call thread, `execute_python` namespace and `call_status`, so two agents on two
  projects don't wait for each other or overwrite each other's variables. With one Inventor nothing
  changes; with several, a call without `instance` is refused instead of guessing. New
  **`list_instances`** shows them — and any Inventor it can't reach, e.g. one opened by hand (Windows
  hands every client the first Inventor that started, so a second one must be started by the
  server). `status` shows the instance, pid and project. Tools are async now, so the MCP SDK's event
  loop isn't held by one agent's long call.
- **`call_status`** — follow a call that outlived the wait: elapsed time, open dialog, `execute_python`
  output so far, and its result once finished.
- **`status`** shows the `execute_python` namespace's age and the names defined in it.

## 0.4.0 — 2026-10-26

### Added
- **`import_step`** — import a vendor STEP (hinge, lock, gland) into the project folder. The STEP is
  copied there first, because Inventor writes the parts of an imported assembly next to the .step
  file (e.g. into a synced Downloads/OneDrive folder). Returns per-component bounding boxes and volumes.
- **`place_component(x_axis, y_axis)`** — full orientation, so vendor models can be placed exactly.
- **`check_interference`**, **`measure_distance`** — overlap volume/bbox and minimum distance between
  occurrences (`Asm:1/2` addresses a part inside a sub-assembly).
- **`sweep_rotation`** — rotate occurrences about an axis in steps and report collisions per angle
  (door on its hinge axis, lock tongue on its shaft); positions are restored.
- **`cut_on_face`** — holes and rectangular cut-outs placed by WORLD coordinates on a sheet metal
  face (mapped with `ModelToSketchSpace`), so the face sketch's own origin and flipped axes don't
  matter. Verifies material was removed.
- **`corner_round`** — Corner Round on sheet metal corners (also in the flat pattern).
- **`list_documents` / `activate_document`** — see which document is active and switch by name or path.
- **`flange`**: `auto_miter`, `miter_gap` (default `GapSize` from the rule) and `height_datum`
  options; the height is now set with `SetDistanceHeightExtent`.

### Fixed
- **`export_document` never worked on Inventor 2027** (and failed for STEP, IGES, SAT and STL alike):
  it searched translator add-ins by display name ("Autodesk Inventor STEP Translator"), which is
  "Translator: STEP" in 2027. Translators are now found by their fixed class IDs; the misleading
  fallback that saved a native file under the export extension is gone, and the written file is verified.
- **Saving no longer opens a blocking dialog.** `save_document` used `Document.Save()`, which opens
  Inventor's modal "save changes to dependents?" prompt whenever a referenced file changed — the
  call then waits until a user clicks OK. It now uses `Save2(True)` under `SilentOperation`.
- **New parts/assemblies are made active.** `Documents.Add(..., True)` does not always make the new
  document the `ActiveDocument`, so following calls could edit the previous document.
- **Flange corners came out open.** The API's default miter gap is the sheet thickness, not the
  rule's `GapSize` the UI uses; `flange` now sets `GapSize` explicitly.
- **`sheet_metal_cut` silently removed nothing** when the sketch was on a work plane. It now deletes
  the empty feature and raises with a hint (#6).

## 0.3.0 — 2026-10-24

### Changed
- **Package renamed `src` → `inventor_mcp_server`**, so the project can be published to PyPI
  (installing it used to put a top-level module called `src` into site-packages). New entry points:
  `inventor_mcp_server/server.py`, `python -m inventor_mcp_server`, and the `inventor-mcp-server`
  command after `pip install`. Full packaging metadata (license, classifiers, Windows-only `pywin32`).
- **Deprecated, still working:** `src/server.py` and `src.inventor_api` now forward to the new package,
  so 0.2.x MCP configs and scripts keep running. They will be removed in a later release.

### Fixed
- **MCP client config in the docs.** README, `.mcp.json.example` and `llms-install.md` recommended
  `-m src.server` with `"cwd"`; Claude Code ignores `cwd`, so the server failed with
  `No module named 'src'` and the client only showed `Connection closed`. The docs now register the
  server by the absolute path to `src/server.py`, and *Common issues* explains the failure
  (related: NeonGlay/inventor-mcp#9).
- **Smoke test no longer closes your documents.** It used to close *every* open document without
  saving when it finished, discarding unsaved work. It now records what was open before it ran and
  closes only the scratch documents it created (documents referenced by open assemblies are
  protected too).

### Verified
- Inventor **2027.1**: smoke test 61/61.

### Added
- **Published on PyPI and the official MCP Registry** (`io.github.cenitu/inventor-mcp-server`):
  `pip install inventor-mcp-server`. Tagging `v*` runs the new Publish workflow (version checks,
  PyPI trusted publishing, MCP Registry via GitHub OIDC).
- **Contributor setup** — `CONTRIBUTING.md`, issue templates (bug report, feature request,
  Inventor version test report), PR template, and CI on Windows that checks the server imports
  both ways clients launch it on MCP SDK 1.x and 2.x, every tool has a description and the README
  tool count is current (`tests/check_server.py`, no Inventor needed).
- **`set_sheet_metal_rule` / `list_sheet_metal_rules`** — activate a sheet metal rule by name
  (case-insensitive), so thickness, bend radius and unfold method come from the shop's own rules
  instead of overriding the thickness alone. Reports the resolved values
  (`T15-V10 active: thickness 1.5 mm, bend radius 10 / 6 = 1.667 mm`); unknown names list what exists.

## 0.2.0 — first maintained release

Continuation of [NeonGlay/inventor-mcp](https://github.com/NeonGlay/inventor-mcp) 0.1.0.
Each item below was first submitted upstream; links point to the original PRs/issues.

### Fixed
- **MCP Python SDK 2.x support** — `FastMCP` was renamed `MCPServer`; fresh installs failed at import
  (NeonGlay/inventor-mcp#1, fixes NeonGlay/inventor-mcp#5).
- **`python src/server.py` works** — the relative import no longer breaks when an MCP client launches
  the server as a script (fixes NeonGlay/inventor-mcp#6).

### Added
- **Inventor 2027 verified** — `tests/smoke_test.py` runs every geometry path against a live Inventor
  and checks the hard-coded enums against the COM type library (NeonGlay/inventor-mcp#2).
- **Assembly (IAM) support** — create assemblies, place and ground components, mate / flush / angle /
  insert constraints on origin geometry, assembly mass summary (NeonGlay/inventor-mcp#3).
- **DIN parts skill** — DIN 912, DIN 7991 and DIN 439 recipes (NeonGlay/inventor-mcp#4).
