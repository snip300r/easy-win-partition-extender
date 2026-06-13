"""Operation logging.

Every disk operation (and notable read/enumeration events) is appended to a
timestamped log file so there is an audit trail of anything destructive.
"""
from __future__ import annotations

import logging
import os
from logging.handlers import RotatingFileHandler

_LOGGER_NAME = "diskmgr"
_configured = False


def log_path() -> str:
    """Location of the log file: %LOCALAPPDATA%\\DiskFormat\\diskformat.log,
    falling back to a file next to the script if LOCALAPPDATA is unavailable."""
    base = os.environ.get("LOCALAPPDATA")
    if base:
        d = os.path.join(base, "DiskFormat")
    else:
        d = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "logs")
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, "diskformat.log")


def get_logger() -> logging.Logger:
    """Return the shared logger, configuring file + console handlers once."""
    global _configured
    logger = logging.getLogger(_LOGGER_NAME)
    if _configured:
        return logger

    logger.setLevel(logging.DEBUG)
    fmt = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S"
    )

    try:
        fh = RotatingFileHandler(
            log_path(), maxBytes=1_000_000, backupCount=3, encoding="utf-8"
        )
        fh.setLevel(logging.INFO)
        fh.setFormatter(fmt)
        logger.addHandler(fh)
    except Exception:
        # If the log file can't be opened we still want the app to run.
        pass

    ch = logging.StreamHandler()
    ch.setLevel(logging.DEBUG)
    ch.setFormatter(fmt)
    logger.addHandler(ch)

    _configured = True
    logger.info("=== diskmgr logger initialised (log: %s) ===", log_path())
    return logger
