"""Deprecated location — the package is now `inventor_mcp_server`.

Kept so MCP client configs that point at `<clone>/src/server.py` keep working.
New configs: `<clone>/inventor_mcp_server/server.py`, or the `inventor-mcp-server`
command after `pip install`. This shim will be removed in a future release.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from inventor_mcp_server.server import main  # noqa: E402

if __name__ == "__main__":
    main()
