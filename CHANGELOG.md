# Changelog

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
