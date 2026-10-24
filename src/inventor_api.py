"""Deprecated location — the package is now `inventor_mcp_server`.

Kept so scripts doing `from src.inventor_api import InventorConnection` keep
working; use `from inventor_mcp_server.inventor_api import ...` instead.
This shim will be removed in a future release.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from inventor_mcp_server import inventor_api as _real  # noqa: E402

# Make `src.inventor_api` the very same module object, so isinstance checks,
# module-level state and importlib.reload behave exactly as before.
sys.modules[__name__] = _real
