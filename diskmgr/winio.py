"""Low-level Windows raw-disk I/O via ctypes.

This module is the dangerous core: it opens the physical disk
(\\\\.\\PhysicalDriveN) and a volume (\\\\.\\X:) directly and reads/writes raw
sectors. It is used only by the partition-MOVE engine (rawmove.py).

Everything here requires administrator rights. Raw writes to a disk that holds
mounted volumes are blocked by Windows unless the affected volume is first
LOCKED and DISMOUNTED — we do exactly that around a move.

References (Win32):
  CreateFileW, ReadFile, WriteFile, SetFilePointerEx, CloseHandle
  DeviceIoControl with:
    IOCTL_DISK_GET_DRIVE_GEOMETRY_EX  - sector size / disk length
    FSCTL_LOCK_VOLUME / FSCTL_UNLOCK_VOLUME
    FSCTL_DISMOUNT_VOLUME
    IOCTL_DISK_UPDATE_PROPERTIES      - ask the OS to re-read the layout
"""
from __future__ import annotations

import ctypes
from ctypes import wintypes
from dataclasses import dataclass
from typing import Optional

from .logging_util import get_logger

log = get_logger()

kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

# -- constants --------------------------------------------------------------
GENERIC_READ = 0x80000000
GENERIC_WRITE = 0x40000000
FILE_SHARE_READ = 0x00000001
FILE_SHARE_WRITE = 0x00000002
OPEN_EXISTING = 3
FILE_FLAG_NO_BUFFERING = 0x20000000
FILE_FLAG_WRITE_THROUGH = 0x80000000
INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value
FILE_BEGIN = 0

# DeviceIoControl codes (precomputed CTL_CODE values).
IOCTL_DISK_GET_DRIVE_GEOMETRY_EX = 0x000700A0
IOCTL_DISK_UPDATE_PROPERTIES = 0x00070140
FSCTL_LOCK_VOLUME = 0x00090018
FSCTL_UNLOCK_VOLUME = 0x0009001C
FSCTL_DISMOUNT_VOLUME = 0x00090020


class DISK_GEOMETRY(ctypes.Structure):
    _fields_ = [
        ("Cylinders", ctypes.c_longlong),
        ("MediaType", wintypes.DWORD),
        ("TracksPerCylinder", wintypes.DWORD),
        ("SectorsPerTrack", wintypes.DWORD),
        ("BytesPerSector", wintypes.DWORD),
    ]


class DISK_GEOMETRY_EX(ctypes.Structure):
    _fields_ = [
        ("Geometry", DISK_GEOMETRY),
        ("DiskSize", ctypes.c_longlong),
        ("Data", ctypes.c_byte * 1),
    ]


class WinIoError(Exception):
    """Raised on a failed Win32 raw-I/O call (carries GetLastError)."""


def _check_handle(h, what: str):
    if not h or h == INVALID_HANDLE_VALUE:
        err = ctypes.get_last_error()
        raise WinIoError(f"{what} failed (CreateFile): Win32 error {err}")
    return h


@dataclass
class DiskGeometry:
    bytes_per_sector: int
    disk_size: int


def _open(path: str, access: int, flags: int = 0):
    kernel32.CreateFileW.restype = wintypes.HANDLE
    kernel32.CreateFileW.argtypes = [
        wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID,
        wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE,
    ]
    h = kernel32.CreateFileW(
        path, access, FILE_SHARE_READ | FILE_SHARE_WRITE, None,
        OPEN_EXISTING, flags, None,
    )
    return _check_handle(h, f"open {path}")


def _close(h):
    if h and h != INVALID_HANDLE_VALUE:
        kernel32.CloseHandle(h)


def _ioctl(h, code: int, in_buf=None, out_buf=None) -> int:
    bytes_returned = wintypes.DWORD(0)
    in_ptr = ctypes.byref(in_buf) if in_buf is not None else None
    in_len = ctypes.sizeof(in_buf) if in_buf is not None else 0
    out_ptr = ctypes.byref(out_buf) if out_buf is not None else None
    out_len = ctypes.sizeof(out_buf) if out_buf is not None else 0
    ok = kernel32.DeviceIoControl(
        h, code, in_ptr, in_len, out_ptr, out_len,
        ctypes.byref(bytes_returned), None,
    )
    if not ok:
        err = ctypes.get_last_error()
        raise WinIoError(f"DeviceIoControl(0x{code:08X}) failed: Win32 error {err}")
    return bytes_returned.value


def get_disk_geometry(disk_number: int) -> DiskGeometry:
    """Return sector size + total size for \\\\.\\PhysicalDriveN."""
    path = rf"\\.\PhysicalDrive{disk_number}"
    h = _open(path, GENERIC_READ)
    try:
        geo = DISK_GEOMETRY_EX()
        _ioctl(h, IOCTL_DISK_GET_DRIVE_GEOMETRY_EX, out_buf=geo)
        return DiskGeometry(
            bytes_per_sector=geo.Geometry.BytesPerSector,
            disk_size=geo.DiskSize,
        )
    finally:
        _close(h)


def update_disk_properties(disk_number: int) -> None:
    """Ask Windows to re-read the partition table after we changed it."""
    path = rf"\\.\PhysicalDrive{disk_number}"
    h = _open(path, GENERIC_READ | GENERIC_WRITE)
    try:
        _ioctl(h, IOCTL_DISK_UPDATE_PROPERTIES)
    finally:
        _close(h)


class LockedVolume:
    """Context manager: open a volume, LOCK + DISMOUNT it so the disk sectors
    behind it can be written, and reliably unlock/close on exit.

    force=False (default): LOCK then DISMOUNT. If the lock fails (open handles)
    we raise — the safe behaviour.

    force=True (EXPERT, dangerous): DISMOUNT first — this forcibly unmounts the
    volume even with open handles, invalidating other programs' handles (any
    unsaved data in them is lost) — then re-LOCK to keep it from remounting
    during the raw copy. If the post-dismount lock still fails we proceed while
    holding the volume handle open. Used only when the user explicitly enables
    force-dismount.
    """

    def __init__(self, drive_letter: str, force: bool = False):
        self.drive_letter = drive_letter.rstrip(":")
        self.force = force
        self._locked = False
        self._h = None

    def __enter__(self):
        path = rf"\\.\{self.drive_letter}:"
        self._h = _open(path, GENERIC_READ | GENERIC_WRITE)

        if not self.force:
            log.info("Locking volume %s:", self.drive_letter)
            try:
                _ioctl(self._h, FSCTL_LOCK_VOLUME)
                self._locked = True
            except WinIoError as e:
                _close(self._h)
                self._h = None
                raise WinIoError(
                    f"Could not lock volume {self.drive_letter}: — it is in use. "
                    f"Windows refuses an exclusive lock while any program "
                    f"(including this tool, if run from {self.drive_letter}:) has "
                    f"the drive open. Close everything using {self.drive_letter}:, "
                    f"run DiskFormat from a different drive — or enable "
                    f"force-dismount (expert). [{e}]") from e
            log.info("Dismounting volume %s:", self.drive_letter)
            _ioctl(self._h, FSCTL_DISMOUNT_VOLUME)
            return self

        # --- FORCE path ---------------------------------------------------
        log.warning("FORCE-DISMOUNT volume %s: (open handles WILL be invalidated; "
                    "unsaved data in other apps on %s: is lost)",
                    self.drive_letter, self.drive_letter)
        _ioctl(self._h, FSCTL_DISMOUNT_VOLUME)
        # Now that handles are invalidated, try to lock to block any remount
        # while we copy. Best-effort.
        try:
            _ioctl(self._h, FSCTL_LOCK_VOLUME)
            self._locked = True
            log.warning("Locked %s: after force-dismount.", self.drive_letter)
        except WinIoError as e:
            log.warning("Could not lock %s: even after force-dismount (%s); "
                        "proceeding while holding the volume handle open.",
                        self.drive_letter, e)
        return self

    def __exit__(self, exc_type, exc, tb):
        try:
            if self._h and self._locked:
                try:
                    _ioctl(self._h, FSCTL_UNLOCK_VOLUME)
                except WinIoError:
                    pass
        finally:
            _close(self._h)
            self._h = None
        log.info("Released volume %s:", self.drive_letter)
        return False


class RawDisk:
    """Sector-aligned reader/writer over \\\\.\\PhysicalDriveN."""

    def __init__(self, disk_number: int, sector_size: int, writable: bool):
        self.disk_number = disk_number
        self.sector = sector_size
        access = GENERIC_READ | (GENERIC_WRITE if writable else 0)
        # WRITE_THROUGH so each write hits the medium (no lazy cache).
        flags = FILE_FLAG_WRITE_THROUGH if writable else 0
        self._h = _open(rf"\\.\PhysicalDrive{disk_number}", access, flags)

    def _seek(self, offset: int) -> None:
        if offset % self.sector != 0:
            raise WinIoError(f"offset {offset} not sector-aligned ({self.sector})")
        new_pos = ctypes.c_longlong(0)
        ok = kernel32.SetFilePointerEx(
            self._h, ctypes.c_longlong(offset), ctypes.byref(new_pos), FILE_BEGIN)
        if not ok:
            raise WinIoError(f"SetFilePointerEx({offset}) failed: "
                             f"{ctypes.get_last_error()}")

    def read(self, offset: int, length: int) -> bytes:
        if length % self.sector != 0:
            raise WinIoError(f"length {length} not sector-aligned")
        self._seek(offset)
        buf = ctypes.create_string_buffer(length)
        read = wintypes.DWORD(0)
        ok = kernel32.ReadFile(self._h, buf, length, ctypes.byref(read), None)
        if not ok:
            raise WinIoError(f"ReadFile @ {offset} failed: {ctypes.get_last_error()}")
        return buf.raw[:read.value]

    def write(self, offset: int, data: bytes) -> int:
        if len(data) % self.sector != 0:
            raise WinIoError("write length not sector-aligned")
        self._seek(offset)
        written = wintypes.DWORD(0)
        ok = kernel32.WriteFile(self._h, data, len(data),
                                ctypes.byref(written), None)
        if not ok:
            raise WinIoError(f"WriteFile @ {offset} failed: {ctypes.get_last_error()}")
        return written.value

    def close(self):
        _close(self._h)
        self._h = None

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()
        return False
