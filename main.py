"""DiskFormat — lightweight Windows partition viewer & extender.

Entry point. On startup it ensures the process is elevated (disk writes need
administrator rights); if not, it relaunches itself through UAC. Then it starts
the Tkinter GUI.

Run:
    python main.py

A UAC prompt will appear. Enumeration works read-only even without elevation,
but resize operations require administrator rights.
"""
from __future__ import annotations

import sys

from diskmgr.admin import is_admin, relaunch_as_admin
from diskmgr.logging_util import get_logger

log = get_logger()


def main() -> int:
    # Allow `--no-elevate` to skip the UAC relaunch (read-only enumeration,
    # handy for testing the UI without admin).
    skip_elevate = "--no-elevate" in sys.argv

    if not is_admin() and not skip_elevate:
        log.info("Not elevated — attempting UAC relaunch.")
        if relaunch_as_admin():
            # A new elevated process is starting; this one exits.
            log.info("Elevated instance launched; exiting non-elevated process.")
            return 0
        # User declined UAC or relaunch failed — continue read-only with a note.
        log.warning("Elevation declined/failed; continuing read-only.")

    log.info("Starting GUI (admin=%s).", is_admin())
    from gui.app import run
    run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
