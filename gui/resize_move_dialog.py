"""Interactive Resize/Move dialog (drag-handle style).

Shows the selected partition flanked by its immediately-adjacent unallocated
space. The user can:
  * drag the RIGHT handle  -> extend (into free-after) or shrink the partition
  * drag the BODY          -> move the partition within the surrounding free
The numeric fields (size / free-before / free-after) stay in sync with the bar.

On OK it returns a list of Operations (ExtendOp / ShrinkOp / MoveOp) to be
added to the pending-operations queue. It deliberately works only with
ADJACENT free space — the headline "extend into ANY free space" (which may move
other partitions) is the separate Extend-to-size dialog driven by the planner.
"""
from __future__ import annotations

import tkinter as tk
from tkinter import ttk
from typing import List, Optional

from diskmgr.model import Disk, Segment, human_size
from diskmgr.operations import Operation, ExtendOp, ShrinkOp, MoveOp

ALIGN = 1024 * 1024
_MB = 1024 * 1024
_HANDLE_W = 7
_BAR_H = 60


class ResizeMoveDialog(tk.Toplevel):
    def __init__(self, master, disk: Disk, target: Segment,
                 free_before: Optional[Segment], free_after: Optional[Segment]):
        super().__init__(master)
        self.title("Resize / Move Partition")
        self.resizable(False, False)
        self.transient(master)
        self.grab_set()

        self.result: List[Operation] = []
        self._disk = disk
        self._target = target

        # Region geometry (bytes).
        self._region_start = free_before.offset if free_before else target.offset
        region_end = free_after.end if free_after else target.end
        self._region_size = region_end - self._region_start

        # Floor for shrinking: never below the used bytes of the file system.
        used = target.volume.used if (target.volume and target.volume.used) else 0
        self._min_size = max(_align(used + 16 * _MB), 16 * _MB)
        self._max_size = self._region_size  # can grow to fill the whole region

        # Mutable proposed geometry (bytes), start aligned.
        self._cur_offset = target.offset
        self._cur_size = target.size

        self._build_ui(free_before, free_after)
        self._drag = None
        self.bind("<Escape>", lambda e: self._cancel())
        self._center(master)
        self._sync_fields()

    # -- UI -----------------------------------------------------------------
    def _build_ui(self, fb, fa):
        frm = ttk.Frame(self, padding=12)
        frm.grid(sticky="nsew")

        title = f"Partition {self._label()}  •  {self._target.file_system or 'raw'}"
        ttk.Label(frm, text=title, font=("Segoe UI", 11, "bold")).grid(
            row=0, column=0, columnspan=4, sticky="w", pady=(0, 8))

        self._canvas = tk.Canvas(frm, width=560, height=_BAR_H + 24, bg="white",
                                 highlightthickness=1, highlightbackground="#999")
        self._canvas.grid(row=1, column=0, columnspan=4, pady=4)
        self._canvas.bind("<Button-1>", self._on_press)
        self._canvas.bind("<B1-Motion>", self._on_drag)
        self._canvas.bind("<ButtonRelease-1>", lambda e: setattr(self, "_drag", None))

        hint = ("Drag the right edge to resize • drag the bar body to move • "
                "or type sizes below.")
        ttk.Label(frm, text=hint, foreground="#666").grid(
            row=2, column=0, columnspan=4, sticky="w", pady=(2, 8))

        # Numeric fields (MB).
        self._v_before = tk.DoubleVar()
        self._v_size = tk.DoubleVar()
        self._v_after = tk.DoubleVar()
        for col, (lbl, var, cb) in enumerate([
                ("Unallocated before (MB)", self._v_before, self._edit_before),
                ("Partition size (MB)", self._v_size, self._edit_size),
                ("Unallocated after (MB)", self._v_after, self._edit_after)]):
            box = ttk.Frame(frm)
            box.grid(row=3, column=col, padx=6, sticky="w")
            ttk.Label(box, text=lbl).pack(anchor="w")
            e = ttk.Entry(box, textvariable=var, width=14)
            e.pack(anchor="w")
            e.bind("<Return>", lambda ev, f=cb: f())
            e.bind("<FocusOut>", lambda ev, f=cb: f())

        self._err = ttk.Label(frm, text="", foreground="#B00000", wraplength=540)
        self._err.grid(row=4, column=0, columnspan=4, sticky="w", pady=(8, 0))

        btns = ttk.Frame(frm)
        btns.grid(row=5, column=0, columnspan=4, sticky="e", pady=(10, 0))
        ttk.Button(btns, text="Cancel", command=self._cancel).grid(row=0, column=0, padx=4)
        ttk.Button(btns, text="OK", command=self._ok).grid(row=0, column=1, padx=4)

    def _label(self) -> str:
        if self._target.drive_letter:
            return f"{self._target.drive_letter}:"
        return f"#{self._target.partition_number}"

    # -- byte<->pixel mapping ----------------------------------------------
    def _x(self, offset: int) -> float:
        w = 560 - 4
        return 2 + (offset - self._region_start) / self._region_size * w

    def _off(self, x: float) -> int:
        w = 560 - 4
        return int(self._region_start + (x - 2) / w * self._region_size)

    # -- drawing ------------------------------------------------------------
    def _redraw(self):
        c = self._canvas
        c.delete("all")
        top, bot = 10, 10 + _BAR_H
        # Whole region background = unallocated (grey).
        c.create_rectangle(self._x(self._region_start), top,
                           self._x(self._region_start + self._region_size), bot,
                           fill="#E6E6E6", outline="#BBB")
        c.create_text((self._x(self._region_start) +
                       self._x(self._region_start + self._region_size)) / 2,
                      bot + 12, text="Unallocated region", fill="#888",
                      font=("Segoe UI", 7))
        # Partition (blue) at current offset/size.
        x0, x1 = self._x(self._cur_offset), self._x(self._cur_offset + self._cur_size)
        c.create_rectangle(x0, top, x1, bot, fill="#4F81BD", outline="#2E5A88",
                           width=2, tags="part")
        if x1 - x0 > 60:
            c.create_text((x0 + x1) / 2, (top + bot) / 2 - 6, text=self._label(),
                          fill="white", font=("Segoe UI", 9, "bold"))
            c.create_text((x0 + x1) / 2, (top + bot) / 2 + 8,
                          text=human_size(self._cur_size), fill="white",
                          font=("Segoe UI", 7))
        # Right resize handle.
        c.create_rectangle(x1 - _HANDLE_W, top, x1, bot, fill="#FFD24D",
                           outline="#B8860B", tags="rhandle")

    # -- interaction --------------------------------------------------------
    def _on_press(self, e):
        x0 = self._x(self._cur_offset)
        x1 = self._x(self._cur_offset + self._cur_size)
        if abs(e.x - x1) <= _HANDLE_W + 2:
            self._drag = ("resize", e.x, self._cur_size)
        elif x0 <= e.x <= x1:
            self._drag = ("move", e.x, self._cur_offset)
        else:
            self._drag = None

    def _on_drag(self, e):
        if not self._drag:
            return
        mode, start_x, start_val = self._drag
        delta_bytes = self._off(e.x) - self._off(start_x)
        if mode == "resize":
            new_size = _align(start_val + delta_bytes)
            new_size = max(self._min_size,
                           min(new_size, self._region_size -
                               (self._cur_offset - self._region_start)))
            self._cur_size = new_size
        else:  # move
            new_off = _align(start_val + delta_bytes)
            new_off = max(self._region_start,
                          min(new_off, self._region_start + self._region_size
                              - self._cur_size))
            self._cur_offset = new_off
        self._redraw()
        self._sync_fields()

    # -- field editing ------------------------------------------------------
    def _edit_size(self):
        try:
            new_size = _align(int(self._v_size.get() * _MB))
        except (tk.TclError, ValueError):
            return
        new_size = max(self._min_size,
                       min(new_size, self._region_size -
                           (self._cur_offset - self._region_start)))
        self._cur_size = new_size
        self._redraw(); self._sync_fields()

    def _edit_before(self):
        try:
            before = _align(int(self._v_before.get() * _MB))
        except (tk.TclError, ValueError):
            return
        before = max(0, min(before, self._region_size - self._cur_size))
        self._cur_offset = self._region_start + before
        self._redraw(); self._sync_fields()

    def _edit_after(self):
        try:
            after = _align(int(self._v_after.get() * _MB))
        except (tk.TclError, ValueError):
            return
        after = max(0, min(after, self._region_size - self._cur_size))
        self._cur_offset = self._region_start + (self._region_size - after - self._cur_size)
        self._redraw(); self._sync_fields()

    def _sync_fields(self):
        before = self._cur_offset - self._region_start
        after = self._region_size - before - self._cur_size
        self._v_before.set(round(before / _MB, 1))
        self._v_size.set(round(self._cur_size / _MB, 1))
        self._v_after.set(round(after / _MB, 1))
        self._redraw()

    # -- finish -------------------------------------------------------------
    def _ok(self):
        ops: List[Operation] = []
        off_changed = self._cur_offset != self._target.offset
        size_changed = self._cur_size != self._target.size

        if not off_changed and not size_changed:
            self._err.config(text="Nothing changed.")
            return

        # Moving a boot/system partition is blocked downstream; warn early.
        if off_changed and (self._target.is_boot or self._target.is_system):
            self._err.config(text="Cannot move a boot/system partition.")
            return

        # Sequence so the disk is never asked to overlap:
        #   shrinking first frees space, then move; growing needs the move first.
        if size_changed and self._cur_size < self._target.size:
            ops.append(ShrinkOp(self._disk.number, self._target.offset,
                                self._cur_size, self._label()))
            if off_changed:
                ops.append(MoveOp(self._disk.number, self._target.offset,
                                  self._cur_offset, self._cur_size, self._label()))
        else:
            if off_changed:
                ops.append(MoveOp(self._disk.number, self._target.offset,
                                  self._cur_offset, self._target.size, self._label()))
            if size_changed:
                ops.append(ExtendOp(self._disk.number, self._cur_offset,
                                    self._cur_size, self._label()))
        self.result = ops
        self.destroy()

    def _cancel(self):
        self.result = []
        self.destroy()

    def _center(self, master):
        self.update_idletasks()
        try:
            mx, my = master.winfo_rootx(), master.winfo_rooty()
            mw, mh = master.winfo_width(), master.winfo_height()
            w, h = self.winfo_width(), self.winfo_height()
            self.geometry(f"+{mx + (mw - w)//2}+{my + (mh - h)//2}")
        except Exception:
            pass


def _align(v: int) -> int:
    return (int(v) // ALIGN) * ALIGN
