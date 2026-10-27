"""Keeps the Inventors the server started reachable after the server restarts.

Every Inventor registers itself in the Running Object Table under the same name, so a
client asking for "Inventor.Application" always gets the first one. The server reaches the
others only through the reference it got when it started them — and that reference dies
with the server process (Claude Desktop restarts it on every app restart).

This small process outlives the server: the server hands it each Inventor it starts, and
it registers one object in the ROT, "!InventorMCP:keeper", whose Apps() returns them all.
A new server finds the keeper there and gets its Inventors back. An Inventor object itself
can't be registered under a new name from another process (Register returns E_INVALIDARG
for a proxy), hence this go-between.

It lets go of an Inventor as soon as its main window is gone, so a closed Inventor can
exit, and quits itself after a while with nothing to keep.

    python keeper.py         (started by the server, detached; nothing to configure)
"""
import os
import sys
import time

import pythoncom
import win32com.client
import win32com.client.gencache
import win32gui
from win32com.server.util import wrap

win32com.client.gencache.GetClassForCLSID = lambda clsid: None  # late binding, as the server

NAME = "InventorMCP:keeper"
IDLE_EXIT = float(os.environ.get("INVENTOR_MCP_KEEPER_IDLE", "600"))  # s with nothing kept


class Keeper:
    _public_methods_ = ["Keep", "Apps", "Count"]

    def __init__(self):
        self.kept = {}  # main window handle -> (Inventor, was its window visible)

    def Keep(self, app):
        app = win32com.client.Dispatch(app)
        hwnd = int(app.MainFrameHWND)
        self.kept[hwnd] = (app, bool(win32gui.IsWindowVisible(hwnd)))
        return len(self.kept)

    def Apps(self):
        self.prune()
        return [a._oleobj_ for a, _ in self.kept.values()]

    def Count(self):
        self.prune()
        return len(self.kept)

    def prune(self):
        """Drop Inventors whose main window is gone, or hidden after it was shown (closed by the
        user while we held it) — window checks only: a closing Inventor may hang a COM call."""
        gone = [h for h, (_, shown) in self.kept.items()
                if not win32gui.IsWindow(h) or (shown and not win32gui.IsWindowVisible(h))]
        for hwnd in gone:
            del self.kept[hwnd]


def find(rot=None):
    """The running keeper (late-bound), or None."""
    rot = rot or pythoncom.GetRunningObjectTable()
    ctx = pythoncom.CreateBindCtx(0)
    for m in rot.EnumRunning():
        try:
            if m.GetDisplayName(ctx, None) == "!" + NAME:
                return win32com.client.Dispatch(rot.GetObject(m).QueryInterface(pythoncom.IID_IDispatch))
        except pythoncom.com_error:
            pass
    return None


def main():
    pythoncom.CoInitialize()
    rot = pythoncom.GetRunningObjectTable()
    if find(rot) is not None:
        return 0  # one keeper per user session is enough
    keeper = Keeper()
    cookie = rot.Register(1, wrap(keeper), pythoncom.CreateItemMoniker("!", NAME))  # keep alive
    empty_since = time.monotonic()
    try:
        while True:
            pythoncom.PumpWaitingMessages()
            keeper.prune()
            if keeper.kept:
                empty_since = time.monotonic()
            elif time.monotonic() - empty_since > IDLE_EXIT:
                return 0
            time.sleep(0.1)
    finally:
        rot.Revoke(cookie)


if __name__ == "__main__":
    sys.exit(main())
