"""Windows Storage operations via PowerShell Storage cmdlets.

Why PowerShell instead of pip packages:
  * The Storage module (root\\Microsoft\\Windows\\Storage WMI namespace) ships
    with Windows 10/11 — zero extra download, no `pip install wmi/pywin32`.
  * The cmdlets (Get-Disk, Get-Partition, Get-Volume,
    Get-PartitionSupportedSize, Resize-Partition) are battle-tested and map
    1:1 to the MSFT_Disk / MSFT_Partition / MSFT_Volume CIM classes and their
    methods (Resize-Partition == MSFT_Partition.Resize,
    Get-PartitionSupportedSize == MSFT_Partition.GetSupportedSize).

We invoke PowerShell with -NoProfile/-NonInteractive and ask for JSON
(ConvertTo-Json) so parsing is robust. All public functions raise
StorageError with a clear message on failure.
"""
from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from typing import List, Optional, Tuple

from .model import Disk, Segment, Volume
from .logging_util import get_logger

log = get_logger()

# GPT reserves space at the tail of the disk for the backup partition table.
# We ignore unallocated gaps smaller than this so we don't render slivers of
# table/alignment padding as "free space".
MIN_FREE_BYTES = 1 * 1024 * 1024  # 1 MiB


class StorageError(Exception):
    """Raised when a PowerShell storage call fails."""


def _run_powershell(script: str, timeout: int = 60) -> str:
    """Run a PowerShell script block and return stdout. Raises StorageError
    with stderr/exit-code detail on failure."""
    cmd = [
        "powershell.exe",
        "-NoProfile",
        "-NonInteractive",
        "-ExecutionPolicy", "Bypass",
        "-Command", script,
    ]
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except FileNotFoundError as e:
        raise StorageError("powershell.exe not found on PATH") from e
    except subprocess.TimeoutExpired as e:
        raise StorageError(f"PowerShell call timed out after {timeout}s") from e

    if proc.returncode != 0:
        err = (proc.stderr or "").strip() or (proc.stdout or "").strip()
        raise StorageError(
            f"PowerShell exited with code {proc.returncode}: {err or '(no output)'}"
        )
    return proc.stdout


def _as_list(obj) -> list:
    """ConvertTo-Json collapses single-element arrays to a bare object and
    empty arrays to null. Normalise everything back to a list."""
    if obj is None:
        return []
    if isinstance(obj, list):
        return obj
    return [obj]


# ---------------------------------------------------------------------------
# Enumeration
# ---------------------------------------------------------------------------

# One round-trip pulls disks, partitions and volumes together. Enum-typed
# properties are cast to [string] so JSON gives us readable labels rather than
# integers. DriveLetter is a [char]; empty becomes \0 which we map to $null.
_ENUM_SCRIPT = r"""
$ErrorActionPreference = 'Stop'
$disks = Get-Disk | Select-Object `
    Number,
    FriendlyName,
    @{N='PartitionStyle';E={[string]$_.PartitionStyle}},
    @{N='Size';E={[int64]$_.Size}},
    @{N='BusType';E={[string]$_.BusType}},
    @{N='OperationalStatus';E={[string]$_.OperationalStatus}},
    @{N='IsReadOnly';E={[bool]$_.IsReadOnly}}

$parts = Get-Partition | Select-Object `
    DiskNumber,
    PartitionNumber,
    @{N='DriveLetter';E={if($_.DriveLetter -and $_.DriveLetter -ne [char]0){[string]$_.DriveLetter}else{$null}}},
    @{N='Offset';E={[int64]$_.Offset}},
    @{N='Size';E={[int64]$_.Size}},
    @{N='Type';E={[string]$_.Type}},
    @{N='IsBoot';E={[bool]$_.IsBoot}},
    @{N='IsSystem';E={[bool]$_.IsSystem}},
    @{N='IsActive';E={[bool]$_.IsActive}},
    @{N='IsHidden';E={[bool]$_.IsHidden}},
    @{N='GptType';E={[string]$_.GptType}},
    @{N='MbrType';E={[string]$_.MbrType}},
    @{N='AccessPaths';E={@($_.AccessPaths)}}

$vols = Get-Volume | Select-Object `
    @{N='DriveLetter';E={if($_.DriveLetter -and $_.DriveLetter -ne [char]0){[string]$_.DriveLetter}else{$null}}},
    @{N='FileSystem';E={[string]$_.FileSystemType}},
    @{N='Label';E={[string]$_.FileSystemLabel}},
    @{N='Size';E={[int64]$_.Size}},
    @{N='SizeRemaining';E={[int64]$_.SizeRemaining}},
    @{N='Health';E={[string]$_.HealthStatus}},
    @{N='Path';E={[string]$_.Path}}

[pscustomobject]@{
    SystemDrive = $env:SystemDrive
    Disks       = @($disks)
    Partitions  = @($parts)
    Volumes     = @($vols)
} | ConvertTo-Json -Depth 6 -Compress
"""


def _build_volume(v: dict) -> Volume:
    return Volume(
        drive_letter=v.get("DriveLetter"),
        file_system=v.get("FileSystem") or None,
        label=v.get("Label") or None,
        size=v.get("Size"),
        size_remaining=v.get("SizeRemaining"),
        health=v.get("Health") or None,
        unique_id=v.get("Path") or None,
    )


def _match_volume(part: dict, volumes: List[dict]) -> Optional[dict]:
    r"""Associate a partition with its volume, first by drive letter, then by
    matching the partition's AccessPaths against the volume's \\?\Volume{..} path."""
    dl = part.get("DriveLetter")
    if dl:
        for v in volumes:
            if v.get("DriveLetter") == dl:
                return v
    access = part.get("AccessPaths") or []
    for v in volumes:
        vp = v.get("Path")
        if vp and vp in access:
            return v
    return None


def _compute_segments(disk_size: int, parts: List[dict],
                      volumes: List[dict]) -> List[Segment]:
    """Turn the partition list into an ordered list of partition + free-space
    segments. Free space is any gap >= MIN_FREE_BYTES between consecutive
    partitions, before the first, or after the last."""
    parts = sorted(parts, key=lambda p: p["Offset"])
    segments: List[Segment] = []
    cursor = 0

    for p in parts:
        offset = p["Offset"]
        gap = offset - cursor
        if gap >= MIN_FREE_BYTES:
            segments.append(Segment(kind="free", offset=cursor, size=gap))
        vol_dict = _match_volume(p, volumes)
        segments.append(Segment(
            kind="partition",
            offset=offset,
            size=p["Size"],
            disk_number=p["DiskNumber"],
            partition_number=p["PartitionNumber"],
            drive_letter=p.get("DriveLetter"),
            part_type=p.get("Type"),
            is_boot=bool(p.get("IsBoot")),
            is_system=bool(p.get("IsSystem")),
            is_active=bool(p.get("IsActive")),
            is_hidden=bool(p.get("IsHidden")),
            gpt_type=p.get("GptType") or None,
            mbr_type=p.get("MbrType") or None,
            volume=_build_volume(vol_dict) if vol_dict else None,
        ))
        cursor = max(cursor, offset + p["Size"])

    # Trailing free space up to the end of the disk.
    tail = disk_size - cursor
    if tail >= MIN_FREE_BYTES:
        segments.append(Segment(kind="free", offset=cursor, size=tail))

    return segments


def enumerate_disks() -> List[Disk]:
    """Enumerate all physical disks with their partition/free-space layout."""
    raw = _run_powershell(_ENUM_SCRIPT)
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        raise StorageError(f"Could not parse PowerShell output: {e}\n{raw[:500]}") from e

    system_drive = (data.get("SystemDrive") or "").rstrip(":").upper() or None
    disks_raw = _as_list(data.get("Disks"))
    parts_raw = _as_list(data.get("Partitions"))
    vols_raw = _as_list(data.get("Volumes"))

    disks: List[Disk] = []
    for d in disks_raw:
        num = d["Number"]
        my_parts = [p for p in parts_raw if p.get("DiskNumber") == num]
        segments = _compute_segments(d.get("Size") or 0, my_parts, vols_raw)

        # An "OS disk" hosts the running system: a boot/system partition, or a
        # partition carrying the system drive letter. We hard-guard it later.
        is_os = any(s.is_boot or s.is_system for s in segments if s.kind == "partition")
        if system_drive:
            is_os = is_os or any(
                (s.drive_letter or "").upper() == system_drive
                for s in segments if s.kind == "partition"
            )

        disks.append(Disk(
            number=num,
            friendly_name=d.get("FriendlyName") or f"Disk {num}",
            partition_style=d.get("PartitionStyle") or "RAW",
            size=d.get("Size") or 0,
            bus_type=d.get("BusType") or None,
            operational_status=d.get("OperationalStatus") or None,
            is_readonly=bool(d.get("IsReadOnly")),
            is_os_disk=is_os,
            segments=segments,
        ))

    disks.sort(key=lambda x: x.number)
    log.info("Enumerated %d disk(s); system drive=%s", len(disks), system_drive)
    return disks


# ---------------------------------------------------------------------------
# Supported size (bounds for resize)
# ---------------------------------------------------------------------------

@dataclass
class SupportedSize:
    size_min: int
    size_max: int


def get_supported_size(disk_number: int, partition_number: int) -> SupportedSize:
    """Wrap Get-PartitionSupportedSize == MSFT_Partition.GetSupportedSize.

    SizeMax already accounts for adjacent free space and the file system, so it
    is the authoritative upper bound for an Extend; SizeMin is the smallest the
    FS can shrink to."""
    script = (
        f"$ErrorActionPreference='Stop';"
        f"$s = Get-PartitionSupportedSize -DiskNumber {int(disk_number)} "
        f"-PartitionNumber {int(partition_number)};"
        f"[pscustomobject]@{{SizeMin=[int64]$s.SizeMin;SizeMax=[int64]$s.SizeMax}}"
        f" | ConvertTo-Json -Compress"
    )
    raw = _run_powershell(script)
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        raise StorageError(f"Could not parse supported size: {raw[:300]}") from e
    return SupportedSize(size_min=int(data["SizeMin"]), size_max=int(data["SizeMax"]))


# ---------------------------------------------------------------------------
# Resize (the destructive operation)
# ---------------------------------------------------------------------------

def resize_partition(disk_number: int, partition_number: int,
                     new_size_bytes: int) -> None:
    """Wrap Resize-Partition == MSFT_Partition.Resize.

    Resizes the partition to exactly new_size_bytes. For an Extend this grows
    the partition into adjacent unallocated space and (for NTFS) grows the
    file system online. Raises StorageError with the underlying message on
    any failure; the caller is responsible for confirmation/guards.
    """
    log.info(
        "RESIZE requested: disk=%d partition=%d -> %d bytes",
        disk_number, partition_number, new_size_bytes,
    )
    script = (
        f"$ErrorActionPreference='Stop';"
        f"Resize-Partition -DiskNumber {int(disk_number)} "
        f"-PartitionNumber {int(partition_number)} "
        f"-Size {int(new_size_bytes)}"
    )
    try:
        _run_powershell(script, timeout=300)
    except StorageError:
        log.exception("RESIZE FAILED: disk=%d partition=%d",
                      disk_number, partition_number)
        raise
    log.info("RESIZE OK: disk=%d partition=%d now %d bytes",
             disk_number, partition_number, new_size_bytes)


# ---------------------------------------------------------------------------
# Partition-table primitives used by the MOVE engine.
#
# A "move" is: relocate the raw bytes (rawmove.py) then re-point the partition
# table at the new offset. Rather than hand-edit GPT/MBR tables we let Windows
# do it: Remove-Partition drops the old table entry and New-Partition writes a
# fresh one at the new offset. Because the file-system bytes already physically
# live at the new offset, the new partition mounts the existing data intact.
# ---------------------------------------------------------------------------

def remove_partition(disk_number: int, partition_number: int) -> None:
    """Remove a partition's TABLE ENTRY (Remove-Partition). Does not wipe data;
    used by MOVE after the bytes have been relocated."""
    log.warning("REMOVE partition entry: disk=%d partition=%d",
                disk_number, partition_number)
    script = (
        f"$ErrorActionPreference='Stop';"
        f"Remove-Partition -DiskNumber {int(disk_number)} "
        f"-PartitionNumber {int(partition_number)} -Confirm:$false"
    )
    _run_powershell(script)


def create_partition(disk_number: int, offset_bytes: int, size_bytes: int,
                     gpt_type: Optional[str] = None,
                     drive_letter: Optional[str] = None) -> int:
    """Create a partition TABLE ENTRY at an exact offset/size (New-Partition).
    Returns the new partition number. No format is performed — the bytes at
    `offset_bytes` are expected to already hold the moved file system."""
    log.warning("CREATE partition entry: disk=%d offset=%d size=%d type=%s letter=%s",
                disk_number, offset_bytes, size_bytes, gpt_type, drive_letter)
    parts = [
        f"New-Partition -DiskNumber {int(disk_number)} "
        f"-Offset {int(offset_bytes)} -Size {int(size_bytes)}"
    ]
    if gpt_type:
        parts.append(f"-GptType '{gpt_type}'")
    if drive_letter:
        parts.append(f"-DriveLetter {drive_letter.rstrip(':')}")
    script = (
        f"$ErrorActionPreference='Stop';"
        f"$p = {' '.join(parts)};"
        f"[int]$p.PartitionNumber"
    )
    out = _run_powershell(script).strip()
    try:
        return int(out.splitlines()[-1])
    except (ValueError, IndexError) as e:
        raise StorageError(f"Could not read new partition number: {out!r}") from e


def assign_drive_letter(disk_number: int, partition_number: int,
                        drive_letter: str) -> None:
    """Attach a drive letter to an existing partition."""
    dl = drive_letter.rstrip(":")
    script = (
        f"$ErrorActionPreference='Stop';"
        f"Get-Partition -DiskNumber {int(disk_number)} "
        f"-PartitionNumber {int(partition_number)} | "
        f"Set-Partition -NewDriveLetter {dl}"
    )
    _run_powershell(script)
