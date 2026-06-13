"""Operation model + executor.

Queue-based workflow: the user stacks up pending operations, then hits Apply.
Each operation is one primitive:

  * ShrinkOp  / ExtendOp  — safe, Windows-native (Resize-Partition).
  * MoveOp                — DANGEROUS, raw-sector relocation (rawmove.py) plus
                            a partition-table re-point (remove + create).

Partitions are resolved by their *current on-disk offset* at execution time
(via a fresh enumeration) rather than by partition number, because a MOVE
changes partition numbers. Offsets are stable except for the one partition an
op deliberately changes, so this keeps a multi-step plan consistent.
"""
from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from typing import Callable, List, Optional

from . import storage, rawmove
from .logging_util import get_logger
from .model import human_size

log = get_logger()

ProgressCb = Optional[Callable[[str, int, int], None]]  # (text, done, total)


class OperationError(Exception):
    pass


def _find_partition_by_offset(disk_number: int, offset: int) -> dict:
    """Re-enumerate and return the live partition segment at `offset`."""
    disks = storage.enumerate_disks()
    for d in disks:
        if d.number != disk_number:
            continue
        for s in d.segments:
            if s.kind == "partition" and s.offset == offset:
                return {
                    "partition_number": s.partition_number,
                    "size": s.size,
                    "gpt_type": s.gpt_type,
                    "drive_letter": s.drive_letter,
                    "is_boot": s.is_boot,
                    "is_system": s.is_system,
                    "file_system": s.file_system,
                }
    raise OperationError(
        f"No partition found at offset {offset:,} on disk {disk_number} "
        "(layout changed unexpectedly).")


@dataclass
class Operation:
    disk_number: int

    def describe(self) -> str:  # pragma: no cover - overridden
        raise NotImplementedError

    def execute(self, *, dry_run: bool, progress: ProgressCb = None,
                force: bool = False) -> None:
        raise NotImplementedError


@dataclass
class ExtendOp(Operation):
    """Grow the partition at `offset` to `new_size` (Resize-Partition)."""
    offset: int
    new_size: int
    label: str = ""

    def describe(self) -> str:
        return f"EXTEND {self.label} to {human_size(self.new_size)}"

    def execute(self, *, dry_run: bool, progress: ProgressCb = None,
                force: bool = False) -> None:
        part = _find_partition_by_offset(self.disk_number, self.offset)
        if progress:
            progress(self.describe(), 0, 1)
        if dry_run:
            log.info("[dry] %s (partition #%s)", self.describe(),
                     part["partition_number"])
            return

        # Clamp to the AUTHORITATIVE maximum. Get-PartitionSupportedSize.SizeMax
        # accounts for the GPT tail reserve and alignment, so a UI-computed
        # "current + free space" can be a few KB too big and Windows rejects the
        # whole call with "Not enough available capacity". Clamping fixes that.
        target = self.new_size
        try:
            sup = storage.get_supported_size(self.disk_number,
                                             part["partition_number"])
            if target > sup.size_max:
                log.warning("Extend requested %d B but SizeMax is %d B; "
                            "clamping to the supported maximum.",
                            target, sup.size_max)
                target = sup.size_max
        except storage.StorageError as e:
            log.warning("Could not query supported size (%s); "
                        "using requested size.", e)

        if target <= part["size"]:
            raise OperationError(
                f"No additional capacity is available to extend "
                f"partition #{part['partition_number']} "
                f"(already at the supported maximum of {human_size(part['size'])}).")

        storage.resize_partition(self.disk_number, part["partition_number"],
                                 target)
        if progress:
            progress(self.describe(), 1, 1)


@dataclass
class ShrinkOp(Operation):
    """Shrink the partition at `offset` to `new_size` (Resize-Partition)."""
    offset: int
    new_size: int
    label: str = ""

    def describe(self) -> str:
        return f"SHRINK {self.label} to {human_size(self.new_size)}"

    def execute(self, *, dry_run: bool, progress: ProgressCb = None,
                force: bool = False) -> None:
        part = _find_partition_by_offset(self.disk_number, self.offset)
        if progress:
            progress(self.describe(), 0, 1)
        if dry_run:
            log.info("[dry] %s (partition #%s)", self.describe(),
                     part["partition_number"])
            return

        # Guard the AUTHORITATIVE minimum. Get-PartitionSupportedSize.SizeMin is
        # the smallest the volume can shrink to given UNMOVABLE files (page file,
        # hiberfil.sys, the $MFT/MFT zone, System Restore). Asking Windows to go
        # below it makes Resize fail wholesale with "StorageWMI 4 (size not
        # supported)", so reject it up front with an actionable message.
        try:
            sup = storage.get_supported_size(self.disk_number,
                                             part["partition_number"])
            if self.new_size < sup.size_min:
                raise OperationError(
                    f"Cannot shrink partition #{part['partition_number']} to "
                    f"{human_size(self.new_size)}: the smallest supported size "
                    f"is {human_size(sup.size_min)}, limited by unmovable files "
                    f"(page file, hibernation, $MFT, or System Restore). "
                    f"Free up or disable those, then retry.")
        except storage.StorageError as e:
            log.warning("Could not query supported size (%s); "
                        "using requested size.", e)

        storage.resize_partition(self.disk_number, part["partition_number"],
                                 self.new_size)
        if progress:
            progress(self.describe(), 1, 1)


@dataclass
class MoveOp(Operation):
    """Relocate the partition currently at `src_offset` to `dst_offset`.

    Steps: raw-copy the bytes (rawmove), remove the old table entry, create a
    new entry at the destination (same size/type/letter). The file system rides
    along because its bytes are physically at the new offset."""
    src_offset: int
    dst_offset: int
    size: int
    label: str = ""

    def describe(self) -> str:
        d = "right" if self.dst_offset > self.src_offset else "left"
        return (f"MOVE {self.label} {d} "
                f"({human_size(abs(self.dst_offset - self.src_offset))})")

    def execute(self, *, dry_run: bool, progress: ProgressCb = None,
                force: bool = False) -> None:
        part = _find_partition_by_offset(self.disk_number, self.src_offset)

        if part["is_boot"] or part["is_system"]:
            raise OperationError(
                "Refusing to MOVE a boot/system partition — too dangerous.")
        if part["gpt_type"] is None:
            raise OperationError(
                "MOVE is implemented for GPT disks only (no GptType found).")

        plan = rawmove.build_move_plan(
            self.disk_number, part["drive_letter"],
            self.src_offset, self.dst_offset, self.size)

        def _p(done, total):
            if progress:
                progress(self.describe(), done, total)

        # 1) Relocate the raw bytes.
        rawmove.execute_move(plan, dry_run=dry_run, verify=True, force=force,
                             progress=_p)

        if dry_run:
            log.info("[dry] would remove partition #%s and recreate at offset %d",
                     part["partition_number"], self.dst_offset)
            return

        # 2) Re-point the partition table: drop old entry, create at new offset.
        storage.remove_partition(self.disk_number, part["partition_number"])
        storage.create_partition(
            self.disk_number, self.dst_offset, self.size,
            gpt_type=part["gpt_type"], drive_letter=part["drive_letter"])
        log.warning("MOVE applied for %s", self.label)


@dataclass
class DeletePartitionOp(Operation):
    """DESTRUCTIVE: delete a donor partition's table entry to reclaim its space
    when Windows cannot shrink its file system online (RAW/unknown/exFAT/FAT).

    This ERASES access to all data on the donor. It exists only for the
    user-confirmed "borrow with format" path; the data-preserving ShrinkOp is
    always preferred when the donor is online-shrinkable."""
    offset: int
    size: int
    label: str = ""

    def describe(self) -> str:
        return f"ERASE {self.label} ({human_size(self.size)}) — all data lost"

    def execute(self, *, dry_run: bool, progress: ProgressCb = None,
                force: bool = False) -> None:
        part = _find_partition_by_offset(self.disk_number, self.offset)
        if part["is_boot"] or part["is_system"]:
            raise OperationError(
                "Refusing to delete a boot/system partition.")
        if progress:
            progress(self.describe(), 0, 1)
        if dry_run:
            log.info("[dry] would DELETE partition #%s at offset %d (%s)",
                     part["partition_number"], self.offset, self.label)
            return
        log.warning("ERASE partition #%s (%s) — removing table entry to free "
                    "%d bytes", part["partition_number"], self.label, self.size)
        storage.remove_partition(self.disk_number, part["partition_number"])
        if progress:
            progress(self.describe(), 1, 1)


def app_volume_drives() -> set:
    """Drive letters this process is running from (executable / cwd / script).
    A MOVE can never lock a volume that the running tool itself uses."""
    drives = set()
    candidates = [sys.executable, os.getcwd()]
    if sys.argv and sys.argv[0]:
        candidates.append(os.path.abspath(sys.argv[0]))
    for c in candidates:
        drv = os.path.splitdrive(c)[0]  # e.g. 'D:'
        if drv:
            drives.add(drv.rstrip(":").upper())
    return drives


def preflight(ops: List[Operation]) -> List[str]:
    """Validate an armed plan BEFORE running any operation, so we never apply a
    plan partially. Returns a list of human-readable problems ([] = OK)."""
    problems: List[str] = []
    app_drives = app_volume_drives()
    for op in ops:
        if isinstance(op, ShrinkOp):
            # A shrink is the data-preserving way to free space ("borrow"), but
            # ONLY if Windows can shrink this file system online (NTFS). For a
            # RAW/unknown/exFAT/FAT donor, Get-PartitionSupportedSize fails and
            # the only way to reclaim the space is a reformat. Catch that HERE
            # so the whole plan is rejected before any write — never silently
            # format the donor.
            try:
                part = _find_partition_by_offset(op.disk_number, op.offset)
            except OperationError as e:
                problems.append(str(e))
                continue
            fs = (part.get("file_system") or "").upper()
            try:
                sup = storage.get_supported_size(
                    op.disk_number, part["partition_number"])
            except storage.StorageError:
                problems.append(
                    f"Cannot shrink {op.label} without data loss: its file "
                    f"system ({part.get('file_system') or 'RAW/unknown'}) cannot "
                    f"be resized in place by Windows. Freeing its space would "
                    f"require reformatting {op.label} and ERASING its data. "
                    f"Back up {op.label}, then convert it to NTFS (or use a tool "
                    f"that resizes this file system) before retrying.")
                continue
            if op.new_size < sup.size_min:
                problems.append(
                    f"Cannot shrink {op.label} that far: the smallest size it "
                    f"supports is {human_size(sup.size_min)} (its data and "
                    f"unmovable files occupy the rest). Choose a target of at "
                    f"least {human_size(sup.size_min)}.")
        if isinstance(op, MoveOp):
            try:
                part = _find_partition_by_offset(op.disk_number, op.src_offset)
            except OperationError as e:
                problems.append(str(e))
                continue
            dl = (part.get("drive_letter") or "").upper()
            if dl and dl in app_drives:
                problems.append(
                    f"Cannot move {op.label}: DiskFormat is running FROM drive "
                    f"{dl}: (its folder or the Python process is there). Windows "
                    f"cannot lock a volume the running program uses. Copy this "
                    f"tool to another drive (e.g. C:) and launch it from there, "
                    f"and make sure {dl}: has no open files.")
            if part.get("is_boot") or part.get("is_system"):
                problems.append(f"Cannot move {op.label}: it is a boot/system "
                                "partition.")
    return problems


def execute_plan(ops: List[Operation], *, dry_run: bool,
                 progress: ProgressCb = None, force: bool = False) -> None:
    """Run a list of operations in order. Stops at the first failure."""
    log.warning("Executing plan: %d operation(s), dry_run=%s, force=%s",
                len(ops), dry_run, force)
    for idx, op in enumerate(ops, 1):
        log.warning("  [%d/%d] %s", idx, len(ops), op.describe())
        op.execute(dry_run=dry_run, progress=progress, force=force)
    log.warning("Plan complete (dry_run=%s).", dry_run)
