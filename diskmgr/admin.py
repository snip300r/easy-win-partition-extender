"""Administrator-elevation helpers (Windows only).

Disk write operations require an elevated process. We detect elevation via
shell32.IsUserAnAdmin and, if missing, relaunch the same script through
ShellExecuteW with the "runas" verb, which triggers the UAC prompt.
"""
from __future__ import annotations

import ctypes
import sys
import os


def is_admin() -> bool:
    """Return True if the current process has administrator rights."""
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def relaunch_as_admin() -> bool:
    """Relaunch the current Python script elevated via UAC.

    Returns True if a relaunch was triggered (caller should exit), False if we
    are already elevated and execution should continue.
    """
    if is_admin():
        return False

    # Re-run: python.exe  <script> <original args...>
    script = os.path.abspath(sys.argv[0])
    params = " ".join(f'"{a}"' for a in [script] + sys.argv[1:])

    # ShellExecuteW(hwnd, verb, file, params, dir, show)
    # SW_SHOWNORMAL == 1. A return value > 32 means success.
    rc = ctypes.windll.shell32.ShellExecuteW(
        None, "runas", sys.executable, params, None, 1
    )
    if rc <= 32:
        # User declined the UAC prompt or the call failed.
        return False
    return True
