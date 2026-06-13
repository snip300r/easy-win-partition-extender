"""Data model for disks, partitions, volumes and unallocated regions.

These are plain dataclasses populated from the JSON emitted by the PowerShell
Storage cmdlets (see storage.py). All sizes/offsets are in bytes.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional


def human_size(num_bytes: Optional[int]) -> str:
    """Format a byte count as a human-readable string (binary units)."""
    if num_bytes is None:
        return "—"
    n = float(num_bytes)
    for unit in ("B", "KB", "MB", "GB", "TB", "PB"):
        if abs(n) < 1024.0 or unit == "PB":
            if unit == "B":
                return f"{int(n)} {unit}"
            return f"{n:.2f} {unit}"
        n /= 1024.0
    return f"{n:.2f} PB"


@dataclass
class Volume:
    """A mounted volume (file system) — maps to MSFT_Volume."""
    drive_letter: Optional[str]
    file_system: Optional[str]
    label: Optional[str]
    size: Optional[int]
    size_remaining: Optional[int]
    health: Optional[str]
    unique_id: Optional[str] = None

    @property
    def used(self) -> Optional[int]:
        if self.size is None or self.size_remaining is None:
            return None
        return self.size - self.size_remaining


# A segment in the on-disk layout: either a real partition or unallocated free
# space. Keeping both in one ordered list makes the disk-map rendering and the
# adjacency check trivial.
@dataclass
class Segment:
    """An ordered region on a disk: a partition or a free-space gap."""
    kind: str            # "partition" | "free"
    offset: int          # byte offset from start of disk
    size: int            # length in bytes

    # partition-only fields
    disk_number: Optional[int] = None
    partition_number: Optional[int] = None
    drive_letter: Optional[str] = None
    part_type: Optional[str] = None      # GPT/MBR type description
    is_boot: bool = False
    is_system: bool = False
    is_active: bool = False
    is_hidden: bool = False
    gpt_type: Optional[str] = None
    mbr_type: Optional[str] = None
    volume: Optional[Volume] = None

    @property
    def end(self) -> int:
        return self.offset + self.size

    @property
    def is_free(self) -> bool:
        return self.kind == "free"

    @property
    def file_system(self) -> Optional[str]:
        return self.volume.file_system if self.volume else None


@dataclass
class Disk:
    """A physical disk — maps to MSFT_Disk."""
    number: int
    friendly_name: str
    partition_style: str          # GPT / MBR / RAW
    size: int
    bus_type: Optional[str]
    operational_status: Optional[str]
    is_readonly: bool
    is_os_disk: bool = False       # contains the running OS / boot / system partition
    segments: List[Segment] = field(default_factory=list)

    @property
    def partitions(self) -> List[Segment]:
        return [s for s in self.segments if s.kind == "partition"]

    @property
    def free_segments(self) -> List[Segment]:
        return [s for s in self.segments if s.kind == "free"]

    def free_after(self, partition: Segment) -> Optional[Segment]:
        """Return the unallocated segment immediately following `partition`,
        if one exists (adjacency check used by Extend)."""
        idx = self.segments.index(partition)
        if idx + 1 < len(self.segments):
            nxt = self.segments[idx + 1]
            if nxt.is_free:
                return nxt
        return None
