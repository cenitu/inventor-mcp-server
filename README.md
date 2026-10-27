<p align="center">
  <img src="https://raw.githubusercontent.com/cenitu/inventor-mcp-server/main/assets/logo.png" alt="inventor-mcp-server logo" width="128">
</p>

# inventor-mcp-server

<!-- mcp-name: io.github.cenitu/inventor-mcp-server -->

[![PyPI](https://img.shields.io/pypi/v/inventor-mcp-server.svg)](https://pypi.org/project/inventor-mcp-server/)
[![CI](https://github.com/cenitu/inventor-mcp-server/actions/workflows/ci.yml/badge.svg)](https://github.com/cenitu/inventor-mcp-server/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![Contributions welcome](https://img.shields.io/badge/contributions-welcome-brightgreen.svg)](CONTRIBUTING.md)

> **Maintained continuation of [NeonGlay/inventor-mcp](https://github.com/NeonGlay/inventor-mcp).**
> The original repository has had no activity since its initial release (June 2026), and fresh
> installs there fail on MCP SDK 2.x. This repo carries the full original history plus the fixes
> and features that were submitted upstream as PRs and never reviewed. If the original author
> returns, everything here is available to be merged back. See [CHANGELOG.md](CHANGELOG.md).

**MCP server for parametric 3D modeling in Autodesk Inventor — drive Inventor with Claude (or any MCP client) in natural language.**

Build real parametric parts — flanges, shafts, nuts, brackets, sheet-metal enclosures — by talking to an AI assistant. The server wraps Inventor's COM API with high-level, millimeter-based tools and ships with battle-tested knowledge of Inventor 2026 API quirks that aren't documented anywhere else.

```
You:    "Build a DIN 934 M16 hex nut with proper conical chamfers"
Claude: creates sketch → hexagon → extrude → tapped M16×2 hole →
        revolve-cut chamfers → done. Fully parametric, dimensioned sketches.
```

## Features

- **73 MCP tools**: sketching, extrude/revolve, native hole features (drilled / tapped / counterbore), fillets, chamfers, circular patterns, sheet metal (named rules / Face / auto-mitered Flanges / Cut at world coordinates / Corner Round, with Flat Pattern support), parameters
- **Assemblies with vendor parts**: `import_step` (keeps the files in your project folder), placement with full orientation, `check_interference` / `measure_distance`, and `sweep_rotation` to check a door or lock moves freely through its whole range
- **Assemblies (IAM)**: place components, ground them, constrain by origin geometry (mate / flush / angle) or drop a bolt into a hole with a single Insert constraint
- **`execute_python` power tool** — run arbitrary Python against the live COM connection with a persistent namespace (escape hatch for anything not covered by dedicated tools)
- **Auto-reporting** — every feature operation returns a volume/topology delta (`Hole1 | V 23497 (-503 mm³) | F7 E14`), so the AI can self-verify each step
- **Never hangs on a dialog** — calls run with `SilentOperation` on a dedicated Inventor thread. A call stuck behind a modal dialog, or a long job, returns within 50 s with the dialog's title and text (read from the window list, no COM call); `call_status(wait_seconds=30)` follows it — with the prints and `progress()` line so far — and returns the result the moment it finishes; `cancel_call` stops it
- **Shop-floor outputs** — `flat_pattern` gives the flat size and writes the DXF/DWG for the laser (contour, holes and bend lines on their own layers); `make_drawing` makes a PDF drawing (base + projected + iso views, overall dimensions, or the flat pattern dimensioned); `bom` lists the assembly's parts with quantity, part number, description, material and mass (CSV for Excel / ERP); `properties` reads and sets part number, description, stock number, material and custom properties
- **Real arcs in sketches** — `draw_closed_profile` takes rounded corners (`[x, y, r]`) and arcs through a point, plus `draw_rectangle(corner_radius)`, `draw_slot`, `draw_arc` and `project_edges` (exact edges of a body, for remodelling). Arcs go through three points, so a sketch with flipped axes can't mirror them; `inspect` warns when a curve was drawn as many short lines
- **Sees its own work** — `view_image` returns pictures of the model (iso / front / top / … , several in one call, zoom to a component) as images the agent can look at, with sketches hidden and the user's camera put back. A flange on the wrong side or a part floating off the frame shows at a glance
- **Steel frames** — `frame_members` lays Content Center profiles along skeleton lines (start → end, roll, centered or at the profile origin, shortened or extended at either end) and returns a saw cut list — what Frame Generator does, which has no API. A 13-member frame takes ~35 s
- **Content Center and big assemblies** — `content_center` browses categories → families → sizes and makes members, custom length included (a 1500 mm DIN 1026-1 U 100 in one call); `place_many` places dozens of components with position, orientation, name and grounding in one call
- **Agent-friendly Python** — `execute_python` errors show the traceback from the agent's own lines (also in helpers defined in earlier calls), stderr is captured, and `undo_on_error=True` rolls back everything the call did. `load_library` (or `~/.inventor-mcp/startup.py`) keeps helper code loaded across namespace resets. COM errors come with their name and a hint (E_INVALIDARG, RPC_E_DISCONNECTED …) instead of a localized "Exception occurred". `save_document` checks the path first instead of failing with E_INVALIDARG and never rewrites library or Content Center files; `close_unsaved` clears the Part12s left by failed attempts
- **Several Inventors at once, across restarts** — e.g. two agents on two projects (Claude Desktop runs one server for all its chats). `connect(project="…ipj")` reuses the Inventor that has the project, or the running one if it is free, or starts a separate one; every tool takes `instance=` to pick one, and each has its own call thread and `execute_python` namespace, so one agent's long call doesn't hold up the other. `list_instances` shows them. An Inventor opened by hand can't be targeted — Windows always hands out the first one — so let the server start the second. A keeper process holds the Inventors the server started, so after a restart of the client `connect(project=…)` gets them back
- **Transactions** — wrap multi-step builds, roll back everything with one call
- **Hot reload** — edit the API wrapper and reload without restarting the MCP client
- **Topology helpers** — find edges/faces by coordinates instead of guessing indices
- **Parametric discipline** — projected origin points, symmetry constraints, dimensioned sketches that survive parameter changes

## Requirements

- Windows with **Autodesk Inventor** (developed on Inventor 2026, also verified on **Inventor 2027**; older versions may need enum adjustments — see [docs/inventor-api-notes.md](docs/inventor-api-notes.md))
- **Python 3.12+**
- `pip install "mcp[cli]" pywin32` — MCP Python SDK **1.x and 2.x** are both supported (2.x renamed `FastMCP` to `MCPServer`; the server detects which is installed)
- An MCP client — used daily with **Claude Code** and **Claude Desktop** (several agents at once, see below)

## Installation

1. Clone this repository:
   ```
   git clone https://github.com/cenitu/inventor-mcp-server.git
   ```

2. Install dependencies:
   ```
   pip install "mcp[cli]" pywin32
   ```

3. Register the server with your MCP client. For Claude Code, add to `.mcp.json` in your project root (see `.mcp.json.example`). Use the **absolute path** to `inventor_mcp_server/server.py`:
   ```json
   {
     "mcpServers": {
       "inventor": {
         "command": "python",
         "args": ["C:/path/to/inventor-mcp-server/inventor_mcp_server/server.py"]
       }
     }
   }
   ```

   > **Don't use `"cwd"` with `-m inventor_mcp_server`.** Claude Code ignores `cwd` and starts the server from
   > your project root, where the package can't be found — the process exits immediately and the client only
   > reports `Connection closed`. Pointing at the script by absolute path works from any directory.

   > Configs written for 0.2.x that point at `src/server.py` still work — that file now forwards to the
   > `inventor_mcp_server` package — but please update them; the shim will be removed in a later release.

   **Alternatively, install it from PyPI** — no clone needed, and the config gets a plain command:
   ```
   pip install inventor-mcp-server
   ```
   ```json
   { "mcpServers": { "inventor": { "command": "inventor-mcp-server" } } }
   ```
   (If your MCP client can't find the command, use the full path printed by `where inventor-mcp-server`.)

4. Restart the MCP client (MCP servers load at session start), start Inventor, then ask your AI assistant to build something.

> **Avoid `win32com.client.gencache.EnsureDispatch` and `CastTo`** in your own scripts against the same Python install — they generate a `gen_py` cache that breaks plain `GetActiveObject` (`KeyError: '_dispobj_'`). The server itself is immune since 0.5.0 (it never uses gen_py classes), but your scripts aren't: delete `%LOCALAPPDATA%\Temp\gen_py` if it happens. See the API notes for the full story.

## Claude Desktop

Claude Desktop reads the same `mcpServers` block from `claude_desktop_config.json` — use the full path
of the Python that has `mcp` and `pywin32` installed (a bare `python` may be a different one):

```json
{
  "mcpServers": {
    "inventor": {
      "command": "C:/Users/<you>/AppData/Local/Programs/Python/Python312/python.exe",
      "args": ["C:/path/to/inventor-mcp-server/inventor_mcp_server/server.py"]
    }
  }
}
```

Two things that cost us an afternoon:

- **Where the file is.** The Microsoft Store build of Claude Desktop does not use
  `%APPDATA%\Claude\claude_desktop_config.json`. It keeps its data in
  `%LOCALAPPDATA%\Claude-Data\claude_<id>\claude_desktop_config.json` (the `<id>` is the package id,
  e.g. `claude_pzs8sxrjxfjjc`). If an edit seems ignored, look in `%LOCALAPPDATA%\Claude\Logs\main.log`
  — its paths show which folder the app really uses.
- **Edit it only while Claude Desktop is fully closed.** The app writes its in-memory copy of the file
  back when it quits, so an edit made while it runs is silently thrown away. Quit from the tray icon
  (closing the window is not enough), edit, start it again. `Settings → Developer` should then list
  `inventor` as running; its log is `%LOCALAPPDATA%\Claude\Logs\mcp-server-inventor.log`.

## Several agents, several projects

Claude Desktop starts **one** server for all its chats, so several agents share it. Each agent gets its
own Inventor:

1. Tell each agent only its project: *"Work in C:\…\Kiosk.ipj."* It calls `connect(project=…)` — the
   server reuses the Inventor that already has that project, takes the running one if it is free, or
   starts a separate Inventor named after the project.
2. With several connected, every call carries `instance=<name>` (the agent learns the name from
   `connect`); a call without it is refused instead of guessing. `list_instances` shows them all.
3. Each Inventor has its own call thread, `execute_python` namespace and `call_status`, so one agent's
   long job doesn't hold up the others.

Things to know:

- **Let the server start the Inventors.** Open at most one by hand. Windows hands every client the first
  Inventor that started, so a second one opened by hand can't be reached (`list_instances` lists it as
  unreachable).
- **Restarting Claude Desktop (or the server) keeps the Inventors.** Every Inventor the server starts is
  handed to a small keeper process (`keeper.py`) that outlives the server; after the restart each agent's
  `connect(project=…)` gets the same Inventor back, open documents and all (`connect(pid=…)` picks one
  explicitly; `list_instances` shows them). The keeper lets go of an Inventor when its window closes
  and exits on its own when it holds none. Inventors opened by hand — and ones started by a server
  older than 0.7 — are not kept: save and close those before restarting.
- **Don't run tests in an agent's Inventor.** `python tests/smoke_test.py --own` starts its own Inventor
  and quits it. Exit your own scripts a few seconds after `app.Quit()`: a process that ends right away
  can leave a windowless `Inventor.exe` behind — which Windows may later hand to the next client that
  asks for a new Inventor. Tell an agent's Inventor from such a leftover by its open documents, not by
  its start time.

## Smoke test

`tests/smoke_test.py` exercises every geometry-creating code path once against a live
Inventor and reports PASS/FAIL per step — the quickest way to qualify a new Inventor
release or verify your install:

```
python tests/smoke_test.py            # starts Inventor if needed; closes only the docs it created
python tests/smoke_test.py --keep     # leave the test documents open for inspection
python tests/smoke_test.py --own      # in a separate Inventor it starts and quits — safe while agents work
```

It also checks every hard-coded enum constant against the live COM type library, so a
renumbered enum in a future release shows up as a named failure instead of a cryptic
COM error. Exit code = number of failed steps.

Current results: **118/118 pass on Inventor 2027.1** (Python 3.12, mcp 1.29 and 2.3).

`tests/assembly_smoke_test.py` does the same for the assembly tools — it builds a
plate and a pin on disk, places them, and exercises mate / flush / angle / insert
constraints (29/29 pass on Inventor 2027).

## Skills (optional, recommended)

The `skills/` directory contains two [Agent Skills](https://code.claude.com/docs/en/skills) that teach Claude the workflow and the Inventor 2026 API quirks:

- **inventor-modeling** — core patterns: units, sketch discipline, hole placement, sheet metal, diagnostic verification, common failure modes
- **inventor-din-parts** — parametric recipes for DIN/ISO standard parts (hex nuts DIN 934 / 439, bolts DIN 933, socket head cap screws DIN 912, countersunk screws DIN 7991, washers DIN 125, flanges DIN 2573) with dimension tables

Install by copying into your skills directory:
```
cp -r skills/inventor-modeling ~/.claude/skills/
cp -r skills/inventor-din-parts ~/.claude/skills/
```

## Why this exists

Inventor's COM API documentation is wrong or silent about many things in recent versions. This project encodes empirically verified knowledge:

- Correct Inventor 2026 enum values (`kPartDocumentObject = 12290`, dimension orientation `19201/19202/19203`, …)
- `ChamferFeatures.AddUsingDistance(EdgeCollection, d)` — the 2026 replacement for the removed `CreateChamferDefinition`
- Sheet-metal `FlangeDefinition`: the Distance argument is **silently ignored**; the real height is set via `feature.Definition.HeightExtent.Distance.Expression` (and the angle is in radians — pass `90` and you get 5156°)
- `CreateLinearPlacementDefinition` requires a `BiasPoint` argument that dynamic dispatch won't tell you about
- Edge indices renumber after every feature; fillets shift adjacent edges
- …and more in [docs/inventor-api-notes.md](docs/inventor-api-notes.md)

## Architecture

```
inventor_mcp_server/server.py        MCP server (FastMCP / MCPServer) — tool definitions (stdio transport)
inventor_mcp_server/inventor_api.py  InventorConnection — COM wrapper, all geometry logic (mm units)
skills/              Agent Skills for Claude
docs/                Hard-won API knowledge
```

## Contributing

Contributions welcome — see **[CONTRIBUTING.md](CONTRIBUTING.md)** for setup, the fast
edit → `reload_api` loop, and how to add a tool. Especially valuable:
- **Testing on Inventor 2024/2025** — run `tests/smoke_test.py` and file an
  *Inventor version test report* (no code needed)
- Drawing (IDW) support, and more modeling tools (shell, loft, sweep)
- More DIN/ISO part recipes
- Bug reports with the exact COM error and Inventor version

Start with a [`good first issue`](https://github.com/cenitu/inventor-mcp-server/labels/good%20first%20issue),
or ask and share what you built in [Discussions](https://github.com/cenitu/inventor-mcp-server/discussions).

## License

MIT — see [LICENSE](LICENSE).
