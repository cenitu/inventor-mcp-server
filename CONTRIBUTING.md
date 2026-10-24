# Contributing to inventor-mcp-server

Thanks for helping! This project drives Autodesk Inventor through its COM API, and most of what it
knows was learned the hard way — by hitting an undocumented error first. Every contribution that
captures one more of those lessons makes the next person's life easier.

**Good places to start:** issues labelled
[`good first issue`](https://github.com/cenitu/inventor-mcp-server/labels/good%20first%20issue) or
[`help wanted`](https://github.com/cenitu/inventor-mcp-server/labels/help%20wanted).
Questions and ideas are welcome in [Discussions](https://github.com/cenitu/inventor-mcp-server/discussions).

## You can help even without writing code

- **Run the smoke test on your Inventor version** and post the output with the
  *Inventor version test report* issue template — especially Inventor 2024 / 2025, which nobody
  has verified yet. That alone is a valuable contribution.
- **Report a bug** with the exact COM error, your Inventor version and the tool call that failed.
- **Share what you built** in Discussions — real use cases show what's missing.

## Development setup

Requirements: Windows, Autodesk Inventor, Python 3.12+.

```
git clone https://github.com/cenitu/inventor-mcp-server.git
cd inventor-mcp-server
pip install "mcp[cli]" pywin32
```

Register the server in your MCP client by the **absolute path** to `src/server.py`
(see the README — don't use `"cwd"`).

> **Never call `win32com.client.gencache.EnsureDispatch`** against the same Python install.
> The generated `gen_py` cache breaks `GetActiveObject`. If it happens, delete
> `%LOCALAPPDATA%\Temp\gen_py`.

## Project layout

| Path | What lives there |
|---|---|
| `src/inventor_api.py` | `InventorConnection` — all COM / geometry logic, **millimetre** interface |
| `src/server.py` | MCP tool definitions — thin wrappers that parse arguments and call the API |
| `tests/check_server.py` | Checks that need no Inventor (runs in CI) |
| `tests/smoke_test.py` | Live test against a real Inventor — every geometry path once |
| `docs/inventor-api-notes.md` | Field notes on API quirks — **read this first** |
| `skills/` | Agent Skills that teach the AI the workflow and part recipes |

## Fast iteration

Edit `src/inventor_api.py`, then call the **`reload_api`** tool from your MCP client — the new code
is loaded without restarting the client, and the COM connection is kept. For experiments, the
**`execute_python`** tool runs code against the live connection (`inv`, `app`, `doc`, `comp`,
`body`, `tg` are pre-bound).

Remember: the COM API works in **centimetres** and mostly **radians**. Tools take millimetres and
degrees and convert.

## Adding or changing a tool

1. Put the logic in `InventorConnection` (`src/inventor_api.py`), taking mm / degrees.
2. Add a thin `@mcp.tool()` wrapper in `src/server.py`. The docstring is what the AI reads —
   say what the tool does, the units, and when to prefer it over a similar tool.
3. Return something the AI can verify. Feature operations should include the volume / topology
   delta (`self._fmt_delta(before)`), so a feature that silently did nothing is visible.
4. Add a step to `tests/smoke_test.py`. Keep it template-agnostic (stock Inventor templates only).
5. If you discovered an API quirk, add it to `docs/inventor-api-notes.md` — that file is the
   project's most valuable asset.
6. Update the README tool count (CI checks it) and add a line to `CHANGELOG.md` under *Unreleased*.

## Before opening a PR

```
python -X utf8 tests/check_server.py     # no Inventor needed — CI runs this too
python -X utf8 tests/smoke_test.py       # needs Inventor; paste the summary into the PR
```

The smoke test only closes the scratch documents it creates; your own open documents are left
untouched. CI cannot run Inventor, so **the smoke test result in your PR description is the real
test** — please include your Inventor version.

Style: match the surrounding code (naming, comment density, docstrings). One topic per PR.

## License

By contributing you agree that your contributions are licensed under the MIT License.
