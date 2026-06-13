# easy-win-partition-extender

A **lightweight** Windows disk/partition viewer and **partition extender**,
written in pure Python with a Tkinter GUI. No heavy runtimes, no PyQt, no
Electron, and **zero pip dependencies** — it drives the built-in Windows
Storage PowerShell cmdlets under the hood.

> **Windows only (10/11).** The disk engine uses PowerShell Storage cmdlets and
> raw Win32 device I/O, so it does **not** run on Linux or macOS. (The internal
> Python package and log folder are still named `DiskFormat`.)

> A tiny, no-frills partition tool: view your disks and grow a partition into
> adjacent unallocated space, safely.

---

## ⚠️ DISCLAIMER — READ THIS FIRST

**Disk and partition operations are inherently dangerous and can cause
PERMANENT, IRRECOVERABLE DATA LOSS, including making Windows unbootable.**

- **BACK UP** all important data before using the Extend feature.
- This software is provided **"AS IS", without warranty of any kind.**
- **You use it entirely at your own risk.** The authors accept no liability
  for any data loss or damage.
- Do not run resize operations on a disk you cannot afford to lose, and never
  while other software is heavily writing to the target volume.

If you are not comfortable with the above, **do not use the Extend feature.**
The read-only viewer is safe to explore.

---

## Features

Queue-based workflow: **stack up operations, then Apply.**

- **Enumerate** all physical disks with partition style (GPT/MBR), size, bus
  type, and per-partition file system, drive letter, used/free space, flags,
  and unallocated space.
- **Visual disk maps** + a **partition table**; selection is synced between
  them.
- **Resize / Move** dialog with **draggable handles** — drag the right edge to
  extend/shrink, drag the bar to move the partition within adjacent free space.
- **Extend to size** — grow a partition to *any* size up to the total
  **unallocated** space on its right, **even when that free space is not
  adjacent**. A built-in **planner** decomposes this into the needed
  `MOVE`/`EXTEND` operations (it physically relocates the partitions in the
  way, then extends).
- **Allocate space (borrow)** — the "give space from D: to C:" feature:
  pick a **donor** partition to the right on the **same disk** and an amount;
  the planner builds `SHRINK donor → MOVE the partitions in between → EXTEND
  target`. This is how you enlarge a partition when the disk has **no existing
  unallocated space** (the common "C: is full" case).
  - **Data-safe by default:** the borrowable amount comes from
    `Get-PartitionSupportedSize`, so the donor is only ever shrunk online (NTFS)
    with **its data preserved**.
  - **Borrow from a non-shrinkable donor (erase mode):** if the donor's file
    system can't be shrunk in place (RAW/unknown/exFAT/FAT), the tool offers an
    explicit **erase** path — it `DELETE`s the donor and gives its space to the
    target. This **destroys all data on the donor** and requires a clearly
    labelled confirmation; it is never the silent default.
- **Shrink** a partition (frees space at its right edge).

> **A partition can only grow using free space on its OWN physical disk.** Free
> space on another disk (e.g. an external drive) can never extend it. If a
> partition can't grow, the tool now explains exactly why and what to do.
- **Pending-operations queue** with **Apply / Discard / Remove selected**.
- **Dry-run by default**: Apply *simulates* every operation and logs the exact
  sector-level move plan. You must explicitly **ARM raw writes** to execute.
- **Safety throughout** (see below).

### Engines used per operation

| Operation        | Engine                                   | Risk     |
|------------------|------------------------------------------|----------|
| Extend / Shrink  | `Resize-Partition` (Windows API)         | low      |
| Move (and the moves inside Extend-to-size) | **raw sector copy** (`DeviceIoControl` / raw `\\.\PhysicalDriveN`) + table re-point via `Remove-Partition`/`New-Partition` | **HIGH** |

> Windows has **no** native "move partition" operation, so moving is done by
> physically copying the partition's bytes to a new offset and then re-pointing
> the partition table at them. This is how partition-move tools work internally
> — and it is genuinely dangerous. See **MOVE risks** below.

---

## Requirements

- **Windows 10 or 11 (x64).**
- **Python 3.10+** (developed/tested on 3.14) with the standard Tkinter
  bundle (included in the official python.org installer).
- The Windows **Storage** PowerShell module (ships with Windows 10/11).

There is nothing to `pip install` — see [requirements.txt](requirements.txt).

---

## How to run

Disk writes require administrator rights. The app will request elevation
(UAC) automatically on launch.

```powershell
python main.py
```

- A **UAC prompt** appears; accept it so resize operations are possible.
- To explore the **read-only viewer without elevation** (no resize), run:

  ```powershell
  python main.py --no-elevate
  ```

When **not elevated** the toolbar shows "NOT elevated (read-only)" and ARM stays
disabled (you can still browse and build/simulate a plan).

---

## Workflow

1. Click a **partition** in a disk map or the partition table.
2. Pick an operation from the right sidebar:
   - **Resize / Move…** — drag the right edge to grow/shrink into *adjacent*
     free space, or drag the bar body to move the partition.
   - **Extend to size…** — type/slide to any size up to the **total
     unallocated space to the right**. If that free space isn't adjacent, the
     planner adds the `MOVE` operations needed to bring it next to the
     partition first. (If the disk has no unallocated space, it explains how to
     create some.)
   - **Allocate space (borrow)…** — take space from a **donor** partition to
     the right on the same disk. Builds `SHRINK → MOVE → EXTEND` for you. Use
     this for the "C: is full, take some from D:" case.
   - **Shrink…** — drag the right edge inward.
3. Each choice is added to the **Pending operations** list. Stack as many as
   you like; remove individual ones or **Discard all**.
4. **Apply**:
   - **Dry-run (default, ARM unchecked):** simulates everything and logs the
     full sector-level plan. **Nothing is written.** Review the log first.
   - **Armed (ARM checked):** requires a typed confirmation (`APPLY`, or
     `MOVE` if the plan relocates data) before writing.

---

## ⚠️ MOVE risks (read before arming a move)

A `MOVE` — including the moves generated by *Extend to size* across
non-adjacent free space — **physically relocates raw disk sectors** and then
re-points the partition table. This is the single most dangerous thing this
tool can do:

- A crash, **power loss, or bug mid-move can destroy the partition.** There is
  no undo. **Have a backup.**
- The current implementation copies bytes with the volume locked/dismounted,
  then re-points the table. For best safety, **move only on a disk that holds no
  in-use volumes** (close apps, no pagefile on the donor).
- **OS-disk moves are allowed but HIGH RISK** (with an extra confirmation):
  data partitions like D: can be moved to make room for C:, but **Boot/System
  partitions are always refused** and never moved.
- **You cannot move a volume the tool is running from.** Windows refuses an
  exclusive lock on a drive that has open handles — including DiskFormat's own
  folder/process. **Run DiskFormat from a *different* drive than the one being
  moved**, and close everything using the donor drive (no open files, no
  pagefile on it). A pre-flight check refuses such a plan *before* making any
  change, so a move can never be applied half-way.
- **Force-dismount (EXPERT):** if a volume can't be locked because other
  programs hold it open, the optional *Force-dismount busy volumes* switch will
  `FSCTL_DISMOUNT_VOLUME` it anyway (then re-lock to block a remount during the
  copy). This invalidates other apps' open handles — **any unsaved data on that
  drive is lost**, and there is a small remount race. It is **off by default**,
  requires confirmation, and only takes effect when ARMED and a move is
  involved. Close apps using the drive first; use only when you understand the
  risk.
- Implemented for **GPT** disks; MBR moves are refused.

When in doubt, leave ARM off and use the dry-run to inspect exactly what would
happen.

---

## Safety design

- **Dry-run by default** — the ARM switch must be explicitly enabled to write.
- **Elevation required** for writes — detected via `shell32.IsUserAnAdmin`,
  relaunched via `ShellExecuteW(..., "runas", ...)`.
- **Explicit selection only** — the tool never auto-picks a target.
- **OS-disk MOVE hard-block**; **boot/system MOVE refused**.
- **OS / boot / system guard** + **typed confirmation** before any write
  (`MOVE` keyword when a move is involved).
- **Bounds enforced** — extend/shrink sizes are clamped; the planner refuses if
  there isn't enough right-side free space (and tells you how much there is).
- **Overlap-safe copy** — move direction is chosen so source bytes are never
  overwritten before they're read; each chunk is **read-back verified**.
- **Error surfacing** — every PowerShell and Win32 call checks its result;
  failures show the exact code/message and stop the plan.
- **Audit log** — every operation is timestamped to
  `%LOCALAPPDATA%\DiskFormat\diskformat.log`.
- **Backup banner** — a persistent reminder to back up first.

---

## Project layout

```
diskformat/
├── main.py                 # entry point: UAC elevation + launch GUI
├── requirements.txt        # (intentionally empty — stdlib only)
├── README.md
├── diskmgr/                # core (no GUI)
│   ├── model.py            # Disk / Segment / Volume dataclasses + sizing
│   ├── storage.py          # PowerShell Storage wrappers (enum/size/resize/create)
│   ├── winio.py            # ctypes raw-disk I/O (CreateFile/Read/Write, lock+dismount)
│   ├── rawmove.py          # raw partition-MOVE engine (overlap-safe, dry-run)
│   ├── operations.py       # Extend/Shrink/Move ops + plan executor
│   ├── planner.py          # decompose "extend to any size" into move+extend
│   ├── admin.py            # UAC elevation via ctypes
│   └── logging_util.py     # timestamped audit logging
└── gui/                    # Tkinter front-end
    ├── app.py              # main window: maps + table + ops + pending queue
    ├── diskmap.py          # proportional disk-map canvas widget
    ├── resize_move_dialog.py  # draggable-handle resize/move dialog
    ├── allocate_dialog.py  # borrow-space-from-donor dialog
    └── extend_dialog.py    # size picker (used by Extend-to-size)
```

---

## How it talks to Windows

Rather than `pip install wmi`/`pywin32`, DiskFormat shells out to the Storage
cmdlets and parses their JSON (`... | ConvertTo-Json`). These map 1:1 to the
CIM classes in `root\Microsoft\Windows\Storage`:

| Concept            | Cmdlet                        | CIM equivalent                       |
|--------------------|-------------------------------|--------------------------------------|
| List disks         | `Get-Disk`                    | `MSFT_Disk`                          |
| List partitions    | `Get-Partition`               | `MSFT_Partition`                     |
| List volumes       | `Get-Volume`                  | `MSFT_Volume`                        |
| Resize bounds      | `Get-PartitionSupportedSize`  | `MSFT_Partition.GetSupportedSize()`  |
| Resize             | `Resize-Partition`            | `MSFT_Partition.Resize()`           |
| Re-point table     | `Remove-Partition` + `New-Partition` | `MSFT_Partition.Delete()` / `MSFT_Disk.CreatePartition()` |

The **move** step that has no cmdlet equivalent (relocating the bytes) is done
directly via Win32 `DeviceIoControl` + raw reads/writes on `\\.\PhysicalDriveN`
in [`winio.py`](diskmgr/winio.py) / [`rawmove.py`](diskmgr/rawmove.py). This
keeps the footprint tiny and leans on battle-tested OS components for the table
edits while only hand-rolling the unavoidable raw copy.

---

## Known limitations

- **Extend to size** consolidates free space that lies to the **right** of the
  target (it moves the partitions in between). Free space entirely to the
  **left** would require moving the target itself — not done in v1; the UI tells
  you when this is the case and how much left-side free exists.
- **MOVE is GPT-only** and **refused for boot/system partitions and the OS
  disk**.
- Online shrink/extend reliability depends on the file system; **NTFS** is the
  well-supported case. **FAT32/exFAT** and **BitLocker-locked** volumes may
  refuse online resize and are flagged.
- The MOVE engine has been built and exercised in **dry-run/simulation** and
  via geometry/enumeration checks; an armed move on real data should be tested
  on a **throwaway disk** first. It is intentionally disarmed by default.
