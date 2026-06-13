"""Planner — turn a high-level intent into an ordered list of operations.

The headline feature ("extend a partition to any size") is implemented here. Windows can only extend into free space that sits
immediately AFTER a partition, so when the free space the user wants is
separated from the target by other partitions, we generate MOVE operations
that push those partitions toward the end of the disk, coalescing the free
space directly after the target, and finish with a single EXTEND.

v1 scope: consolidate free space that lies to the RIGHT of the target. This
covers the common cases:
    [ C: ][ *free* ][ D: ]   -> extend C: into the gap (move D: right)
    [ C: ][ D: ][ *free* ]   -> extend C: (move D: right, frees space after C:)
Free space entirely to the LEFT of the target would require moving the target
itself; that is flagged as unsupported in v1 (noted in the UI).
"""
from __future__ import annotations

from dataclasses import replace
from typing import List

from .model import Disk, Segment, human_size
from .operations import (Operation, MoveOp, ExtendOp, ShrinkOp,
                         DeletePartitionOp)

# Keep moves/extends aligned to 1 MiB — matches typical partition alignment and
# the sector-alignment the move engine requires.
ALIGN = 1024 * 1024


class PlanError(Exception):
    pass


def _align_up(v: int) -> int:
    return ((v + ALIGN - 1) // ALIGN) * ALIGN


def _seg_label(s: Segment) -> str:
    if s.drive_letter:
        return f"{s.drive_letter}:"
    return f"partition #{s.partition_number}"


def plan_extend(disk: Disk, target: Segment, new_size: int) -> List[Operation]:
    """Build the op list to extend `target` to `new_size` bytes.

    Raises PlanError with an explanation if it can't be done within the v1
    (right-side consolidation) scope.
    """
    if target.kind != "partition":
        raise PlanError("Target is not a partition.")
    extra = new_size - target.size
    if extra <= 0:
        raise PlanError("New size must be larger than the current size.")

    idx = disk.segments.index(target)
    right = disk.segments[idx + 1:]
    right_free = sum(s.size for s in right if s.is_free)

    # Free space directly after the target (no move needed for this part).
    adjacent_free = 0
    if right and right[0].is_free:
        adjacent_free = right[0].size

    if extra > right_free:
        left_free = sum(s.size for s in disk.segments[:idx] if s.is_free)
        hint = ""
        if left_free > 0:
            hint = (f" There is {human_size(left_free)} of free space to the "
                    "LEFT of this partition, but extending into left-side space "
                    "requires moving this partition (not supported in v1).")
        raise PlanError(
            f"Not enough free space to the right. You asked for "
            f"+{human_size(extra)} but only {human_size(right_free)} is "
            f"available to the right of this partition.{hint}")

    ops: List[Operation] = []

    # If the needed space isn't already adjacent, shift the in-between
    # partitions to the right by just enough to open `extra` bytes of free
    # space immediately after the target. Target layout of the right region:
    #     [ extra free ][ right partitions, packed ][ leftover free ]
    # Each partition moves right (or stays); we process RIGHTMOST FIRST so every
    # destination lands in space that is already free.
    if extra > adjacent_free:
        right_parts = [s for s in right if s.kind == "partition"]
        boundary = right[-1].end  # right edge of the right-hand region
        # Final start offsets, left-to-right, beginning `extra` after target.
        # Align partition offsets UP so the freed gap is never smaller than
        # requested and partitions never overlap downward.
        pos = _align_up(target.end + extra)
        final_offsets = []
        for p in right_parts:
            new_offset = _align_up(pos)
            final_offsets.append(new_offset)
            pos = new_offset + p.size
        # Validate the packed layout fits and never moves a partition LEFT
        # (this planner only shifts right; a left shift would signal a bug).
        if pos > boundary:
            raise PlanError(
                "Alignment overhead leaves too little room to consolidate the "
                "free space. Try a slightly smaller target size.")
        for p, new_offset in zip(right_parts, final_offsets):
            if new_offset < p.offset:
                raise PlanError("Internal: planner attempted a leftward move.")
        # Emit moves rightmost-first (overlap-safe for rightward shifts).
        for p, new_offset in reversed(list(zip(right_parts, final_offsets))):
            if new_offset != p.offset:
                ops.append(MoveOp(
                    disk_number=disk.number,
                    src_offset=p.offset,
                    dst_offset=new_offset,
                    size=p.size,
                    label=_seg_label(p),
                ))

    ops.append(ExtendOp(
        disk_number=disk.number,
        offset=target.offset,
        new_size=new_size,
        label=_seg_label(target),
    ))
    return ops


def _post_shrink_disk(disk: Disk, donor: Segment, amount: int) -> Disk:
    """Return a hypothetical copy of `disk` as it would look AFTER shrinking
    `donor` by `amount` — the donor keeps its offset, shrinks, and a new free
    segment appears immediately after it. Used to plan the follow-up extend
    against the layout that will actually exist when the extend runs."""
    new_segments: List[Segment] = []
    for s in disk.segments:
        if s is donor:
            shrunk = replace(s, size=s.size - amount)
            new_segments.append(shrunk)
            new_segments.append(Segment(kind="free", offset=shrunk.end,
                                        size=amount))
        else:
            new_segments.append(s)
    return replace(disk, segments=new_segments)


def plan_allocate(disk: Disk, target: Segment, donor: Segment,
                  amount: int) -> List[Operation]:
    """Allocate space from D: to C: — give `amount` bytes from `donor`
    to `target`.

    Produces: SHRINK donor, then the MOVE/EXTEND sequence that consolidates the
    freed space next to the target. `donor` must lie to the RIGHT of `target`
    (v1 only consolidates rightward).
    """
    if target.kind != "partition" or donor.kind != "partition":
        raise PlanError("Both target and donor must be partitions.")
    if donor.offset <= target.offset:
        raise PlanError("The donor partition must be to the RIGHT of the "
                        "partition you are extending.")
    amount = (amount // ALIGN) * ALIGN
    if amount <= 0:
        raise PlanError("Choose an amount of at least 1 MB to transfer.")
    if amount >= donor.size:
        raise PlanError("Cannot take the donor's entire size.")

    shrink = ShrinkOp(disk_number=disk.number, offset=donor.offset,
                      new_size=donor.size - amount, label=_seg_label(donor))

    # Plan the extend against the layout that exists after the shrink.
    hypo = _post_shrink_disk(disk, donor, amount)
    htarget = next(s for s in hypo.segments
                   if s.kind == "partition" and s.offset == target.offset)
    return [shrink] + plan_extend(hypo, htarget, target.size + amount)


def _post_delete_disk(disk: Disk, donor: Segment) -> Disk:
    """Return a hypothetical copy of `disk` with `donor` replaced by an equal
    free-space segment at the same offset — the layout that exists right after
    the donor partition is deleted. Used to plan the follow-up extend."""
    new_segments: List[Segment] = []
    for s in disk.segments:
        if s is donor:
            new_segments.append(Segment(kind="free", offset=s.offset,
                                        size=s.size))
        else:
            new_segments.append(s)
    return replace(disk, segments=new_segments)


def plan_allocate_erase(disk: Disk, target: Segment, donor: Segment,
                        amount: int) -> List[Operation]:
    """Borrow by ERASING the donor: delete the donor partition outright, then
    consolidate + extend `target` by `amount` into the freed space. Used when
    the donor's file system cannot be shrunk online and the user has accepted
    the data loss. Any space beyond `amount` is left as unallocated free space.

    Produces: DELETE donor, then the MOVE/EXTEND sequence. `donor` must lie to
    the RIGHT of `target` (v1 only consolidates rightward)."""
    if target.kind != "partition" or donor.kind != "partition":
        raise PlanError("Both target and donor must be partitions.")
    if donor.offset <= target.offset:
        raise PlanError("The donor partition must be to the RIGHT of the "
                        "partition you are extending.")
    amount = (amount // ALIGN) * ALIGN
    if amount <= 0:
        raise PlanError("Choose an amount of at least 1 MB to transfer.")
    if amount > donor.size:
        raise PlanError("Cannot take more space than the donor's total size.")

    delete = DeletePartitionOp(disk_number=disk.number, offset=donor.offset,
                               size=donor.size, label=_seg_label(donor))
    # Plan the extend against the layout that exists after the delete.
    hypo = _post_delete_disk(disk, donor)
    htarget = next(s for s in hypo.segments
                   if s.kind == "partition" and s.offset == target.offset)
    return [delete] + plan_extend(hypo, htarget, target.size + amount)


def plan_shrink(disk: Disk, target: Segment, new_size: int) -> List[Operation]:
    """Shrink `target` to `new_size` (frees space at its right edge)."""
    if target.kind != "partition":
        raise PlanError("Target is not a partition.")
    if new_size >= target.size:
        raise PlanError("New size must be smaller than the current size.")
    return [ShrinkOp(
        disk_number=disk.number,
        offset=target.offset,
        new_size=new_size,
        label=_seg_label(target),
    )]
