# Changelog

## Unreleased

### Added
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
