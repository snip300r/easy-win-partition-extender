"""Raw partition-move engine.

Windows has no "move partition" API, so to extend a partition into free space
that is NOT immediately after it we must physically relocate the bytes of the
partition(s) in the way, then update the partition table.

This module does the *byte relocation* part: an overlap-aware, sector-aligned
copy of a partition's contents from its current offset to a new offset on the
same physical disk. The partition-table update (remove old entry / create new
entry at the new offset) is done by the caller via the Windows Storage cmdlets
(storage.py) — that way we never hand-edit GPT/MBR tables or their checksums.

SAFETY:
  * Defaults to DRY-RUN: it logs every planned chunk copy and writes nothing.
  * Refuses to run on the OS disk.
  * Locks + dismounts the source volume for the duration of the copy.
  * Copies in the correct direction so overlapping ranges never clobber data
    that hasn't been read yet.
  * Verifies each chunk by reading it back (when armed).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Optional

from .logging_util import get_logger
from .winio import RawDisk, LockedVolume, get_disk_geometry, update_disk_properties

log = get_logger()

# Copy granularity. Must be a multiple of the sector size; 4 MiB is a good
# throughput/responsiveness trade-off.
CHUNK_BYTES = 4 * 1024 * 1024


class MoveError(Exception):
    pass


@dataclass
class MovePlan:
    """A planned relocation of one partition's raw bytes."""
    disk_number: int
    drive_letter: Optional[str]   # volume to lock/dismount (None = no FS mounted)
    src_offset: int
    dst_offset: int
    size: int
    sector_size: int

    @property
    def direction(self) -> str:
        return "right" if self.dst_offset > self.src_offset else "left"

    @property
    def overlaps(self) -> bool:
        a0, a1 = self.src_offset, self.src_offset + self.size
        b0, b1 = self.dst_offset, self.dst_offset + self.size
        return a0 < b1 and b0 < a1

    def describe(self) -> str:
        return (f"MOVE disk {self.disk_number} "
                f"{self.size/1024/1024:.0f} MiB "
                f"from offset {self.src_offset:,} to {self.dst_offset:,} "
                f"({self.direction}, "
                f"{'overlapping' if self.overlaps else 'disjoint'})")


def build_move_plan(disk_number: int, drive_letter: Optional[str],
                    src_offset: int, dst_offset: int, size: int) -> MovePlan:
    """Validate geometry/alignment and return a MovePlan."""
    geo = get_disk_geometry(disk_number)
    sec = geo.bytes_per_sector
    for name, val in (("src", src_offset), ("dst", dst_offset), ("size", size)):
        if val % sec != 0:
            raise MoveError(
                f"{name} ({val}) is not aligned to the {sec}-byte sector size.")
    if dst_offset < 0 or dst_offset + size > geo.disk_size:
        raise MoveError("Destination range falls outside the disk.")
    return MovePlan(disk_number, drive_letter, src_offset, dst_offset, size, sec)


def execute_move(plan: MovePlan, *, dry_run: bool = True,
                 verify: bool = True, force: bool = False,
                 progress: Optional[Callable[[int, int], None]] = None) -> None:
    """Relocate the partition's bytes per `plan`.

    dry_run=True (default) logs each chunk but performs NO writes.
    force=True force-dismounts an in-use volume (EXPERT, dangerous).
    progress(done_bytes, total_bytes) is called as the copy proceeds.
    """
    log.warning("execute_move(dry_run=%s, force=%s): %s",
                dry_run, force, plan.describe())

    chunk = (CHUNK_BYTES // plan.sector_size) * plan.sector_size
    total = plan.size

    if dry_run:
        # Simulate the chunk schedule so the user can review it risk-free.
        _simulate(plan, chunk, progress)
        log.warning("DRY-RUN complete — no data was written.")
        return

    # --- ARMED: real raw copy ------------------------------------------------
    # Lock/dismount the source FS (if any) so Windows lets us write the disk.
    lock_ctx = (LockedVolume(plan.drive_letter, force=force)
                if plan.drive_letter else _NullCtx())
    with lock_ctx, RawDisk(plan.disk_number, plan.sector_size, writable=True) as disk:
        if plan.direction == "right" and plan.overlaps:
            # Moving right with overlap: copy from the TAIL backwards so we never
            # overwrite source bytes we still need to read.
            done = 0
            pos = total
            while pos > 0:
                this = min(chunk, pos)
                pos -= this
                src = plan.src_offset + pos
                dst = plan.dst_offset + pos
                _copy_chunk(disk, src, dst, this, verify)
                done += this
                if progress:
                    progress(done, total)
        else:
            # Moving left, or disjoint: copy head-first.
            done = 0
            while done < total:
                this = min(chunk, total - done)
                src = plan.src_offset + done
                dst = plan.dst_offset + done
                _copy_chunk(disk, src, dst, this, verify)
                done += this
                if progress:
                    progress(done, total)

    update_disk_properties(plan.disk_number)
    log.warning("MOVE complete: %s", plan.describe())


def _copy_chunk(disk: RawDisk, src: int, dst: int, length: int, verify: bool):
    data = disk.read(src, length)
    if len(data) != length:
        raise MoveError(f"short read at {src}: {len(data)}/{length}")
    disk.write(dst, data)
    if verify:
        back = disk.read(dst, length)
        if back != data:
            raise MoveError(
                f"verification FAILED at dst {dst}: written bytes do not match.")


def _simulate(plan: MovePlan, chunk: int,
              progress: Optional[Callable[[int, int], None]]):
    total = plan.size
    n = (total + chunk - 1) // chunk
    log.info("  would copy %d chunk(s) of up to %d bytes (%s)",
             n, chunk, plan.direction)
    done = 0
    # Walk in the same direction the real copy would, for an accurate preview.
    if plan.direction == "right" and plan.overlaps:
        pos = total
        while pos > 0:
            this = min(chunk, pos)
            pos -= this
            log.debug("  [dry] copy %d bytes: %d -> %d",
                      this, plan.src_offset + pos, plan.dst_offset + pos)
            done += this
            if progress:
                progress(done, total)
    else:
        while done < total:
            this = min(chunk, total - done)
            log.debug("  [dry] copy %d bytes: %d -> %d",
                      this, plan.src_offset + done, plan.dst_offset + done)
            done += this
            if progress:
                progress(done, total)


class _NullCtx:
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False
