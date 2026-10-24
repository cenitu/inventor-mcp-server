# Installation guide for AI agents

Step-by-step setup of the inventor-mcp server. Follow in order.

## Prerequisites check

1. **Windows only** — this server drives Autodesk Inventor through COM; it cannot work on macOS/Linux.
2. **Autodesk Inventor** must be installed (verified on 2026, 2027 and 2027.1; 2024/2025 may work with enum differences). Verify:
   ```powershell
   Test-Path "C:\Program Files\Autodesk"
   ```
3. **Python 3.12+** on PATH:
   ```powershell
   python --version
   ```

## Setup steps

1. Clone the repository to a stable location (avoid paths that may be deleted):
   ```powershell
   git clone https://github.com/cenitu/inventor-mcp-server.git
   cd inventor-mcp-server
   ```

2. Install Python dependencies:
   ```powershell
   pip install "mcp[cli]" pywin32
   ```

3. Register the MCP server in the client's MCP configuration. The server uses
   **stdio** transport. Point `args` at the **absolute path** of `inventor_mcp_server/server.py`
   in the clone (do NOT use `"cwd"` + `-m inventor_mcp_server` — Claude Code ignores
   `cwd`, the module isn't found and the client only reports `Connection closed`):
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
   - Claude Code: add to `.mcp.json` in the project root
   - Cline: add to `cline_mcp_settings.json`. **Cline 4.x reads
     `%USERPROFILE%\.cline\data\settings\cline_mcp_settings.json`**; older
     versions used `%APPDATA%\Code\User\globalStorage\saoudrizwan.claude-dev\settings\cline_mcp_settings.json`.
     Writing to the wrong one fails silently (the server just never appears).
     If unsure, the file Cline actually uses is the one opened by
     *MCP Servers → Edit Configuration* — or write to the `.cline` path first.
     If that file already has other servers, add `inventor` next to them.
   - Claude Desktop: add to `claude_desktop_config.json`

4. Verify: restart the MCP client (in VS Code: *Developer: Reload Window*),
   then start Autodesk Inventor (the server
   connects to a **running** Inventor instance). Call the `connect` tool —
   expected response: `Connected to running Inventor <version>`.

## Smoke test

Ask the agent to run:
1. `connect` → should report the Inventor version
2. `create_part` with `template="metric"` → creates an empty part
3. `create_sketch` with `plane="XY"` → `draw_circle` (diameter=50) → `extrude` (distance=10)
4. `inspect` → should show one extrusion, volume ≈ 19 635 mm³

## Common issues

- **`Connection closed` / server failed to connect** — the client couldn't start
  the server process. Run the exact `command` + `args` from the config in a
  terminal from a *different* directory; it should wait silently for input.
  `No module named 'inventor_mcp_server'` (or `'src'` in older setups) means the config uses `-m ...` with `cwd`
  (unsupported by Claude Code) — switch to the absolute script path.

- **`Cannot connect to Inventor`** — Inventor isn't running. Start it first (the
  server attaches to the active instance; it can also launch one, but attaching
  is more reliable).
- **`KeyError: '_dispobj_'`** — a stale `gen_py` COM cache. Fix:
  ```powershell
  Remove-Item -Recurse -Force "$env:LOCALAPPDATA\Temp\gen_py"
  ```
- **Unicode errors in console scripts** — run Python with `-X utf8` on Windows.
- All tool dimensions are **millimeters**; Inventor internally uses centimeters —
  the server converts automatically.
