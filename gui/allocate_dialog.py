"""Allocate-space dialog ("borrow" space from another partition).

Lets the user grow the selected partition by taking free space from a DONOR
partition that lies to its right on the SAME disk. The resulting plan is
SHRINK(donor) -> MOVE(intervening partitions) -> EXTEND(target), built by
planner.plan_allocate.
"""
from __future__ import annotations

import tkinter as tk
from tkinter import ttk
from typing import List, Optional, Tuple

from diskmgr.model import Disk, Segment, human_size

_GB = 1024 ** 3
_MB = 1024 ** 2
# Always leave a margin inside the donor so we never try to shrink onto used data.
_DONOR_MARGIN = 256 * _MB


class AllocateDialog(tk.Toplevel):
    """Returns `result` = (donor_segment, amount_bytes) or None."""

    def __init__(self, master, disk: Disk, target: Segment,
                 donors: List[Tuple[Segment, int, str]]):
        super().__init__(master)
        self.title("Allocate space from another partition")
        self.resizable(False, False)
        self.transient(master)
        self.grab_set()

        self.result: Optional[Tuple[Segment, int, str]] = None
        self._disk = disk
        self._target = target
        # donors arrive as (segment, max_bytes, mode). mode "shrink" preserves
        # the donor's data; mode "erase" reclaims space by DELETING the donor.
        self._donors = [d for d, _, _ in donors]
        self._max_borrow = {id(d): m for d, m, _ in donors}
        self._mode = {id(d): mode for d, _, mode in donors}

        frm = ttk.Frame(self, padding=14)
        frm.grid(sticky="nsew")
        pad = {"padx": 8, "pady": 4}

        tname = target.drive_letter + ":" if target.drive_letter else f"#{target.partition_number}"
        ttk.Label(frm, text=f"Extend {tname} ({human_size(target.size)})",
                  font=("Segoe UI", 11, "bold")).grid(
            row=0, column=0, columnspan=2, sticky="w", **pad)
        ttk.Label(frm, text="by taking free space from a partition to its right "
                            "on the same disk:",
                  foreground="#555").grid(row=1, column=0, columnspan=2,
                                          sticky="w", **pad)

        ttk.Label(frm, text="Donor partition:").grid(row=2, column=0, sticky="w", **pad)
        self._donor_var = tk.StringVar()
        self._labels = [self._donor_label(d) for d in self._donors]
        cb = ttk.Combobox(frm, textvariable=self._donor_var, values=self._labels,
                          state="readonly", width=44)
        cb.grid(row=2, column=1, sticky="w", **pad)
        cb.current(0)
        cb.bind("<<ComboboxSelected>>", lambda e: self._on_donor_change())

        self._info = ttk.Label(frm, text="", foreground="#444")
        self._info.grid(row=3, column=0, columnspan=2, sticky="w", **pad)

        ttk.Label(frm, text="Amount to transfer (GB):").grid(
            row=4, column=0, sticky="w", **pad)
        self._amt = tk.DoubleVar(value=0.0)
        ent = ttk.Entry(frm, textvariable=self._amt, width=14)
        ent.grid(row=4, column=1, sticky="w", **pad)

        self._scale = ttk.Scale(frm, from_=0, to=1, orient="horizontal",
                                length=360, command=self._on_scale)
        self._scale.grid(row=5, column=0, columnspan=2, sticky="ew", **pad)

        self._preview = ttk.Label(frm, text="", foreground="#2E5A88",
                                  wraplength=420, justify="left")
        self._preview.grid(row=6, column=0, columnspan=2, sticky="w", **pad)

        self._err = ttk.Label(frm, text="", foreground="#B00000", wraplength=420)
        self._err.grid(row=7, column=0, columnspan=2, sticky="w", **pad)

        btns = ttk.Frame(frm)
        btns.grid(row=8, column=0, columnspan=2, sticky="e", pady=(10, 0))
        ttk.Button(btns, text="Cancel", command=self._cancel).grid(row=0, column=0, padx=4)
        ttk.Button(btns, text="Queue plan", command=self._ok).grid(row=0, column=1, padx=4)

        self._amt.trace_add("write", lambda *_: self._on_amt())
        self.bind("<Escape>", lambda e: self._cancel())
        self._on_donor_change()
        self._center(master)

    # -- helpers ------------------------------------------------------------
    def _donor_label(self, d: Segment) -> str:
        name = d.drive_letter + ":" if d.drive_letter else f"#{d.partition_number}"
        suffix = "  ⚠ ERASES data" if self._mode.get(id(d)) == "erase" else ""
        return f"{name}  ({d.file_system or 'raw'}, {human_size(d.size)}){suffix}"

    def _current_donor(self) -> Segment:
        return self._donors[self._labels.index(self._donor_var.get())]

    def _mode_of(self, donor: Segment) -> str:
        return self._mode.get(id(donor), "shrink")

    def _max_amount(self, donor: Segment) -> int:
        if self._mode_of(donor) == "erase":
            # Erasing frees the whole donor; no in-use margin needed.
            usable = self._max_borrow.get(id(donor), 0)
        else:
            # Keep a safety margin below the shrink limit so we never ride right
            # up against unmovable data.
            usable = max(0, self._max_borrow.get(id(donor), 0) - _DONOR_MARGIN)
        return (usable // _MB) * _MB

    def _on_donor_change(self):
        donor = self._current_donor()
        mx = self._max_amount(donor)
        if self._mode_of(donor) == "erase":
            name = donor.drive_letter + ":" if donor.drive_letter else \
                f"#{donor.partition_number}"
            self._info.config(
                text=f"⚠ {name} ({donor.file_system or 'RAW/unknown'}) cannot be "
                     f"shrunk in place. Borrowing DELETES it — all its data is "
                     f"lost. Up to {human_size(mx)} can be reclaimed.",
                foreground="#B00000")
        else:
            self._info.config(
                text=f"{self._donor_label(donor)} can safely give up to "
                     f"{human_size(mx)} without data loss (256 MB safety margin "
                     f"kept; its existing data stays intact).",
                foreground="#444")
        self._scale.config(to=max(mx / _GB, 0.001))
        self._amt.set(round(mx / _GB, 1))
        self._refresh_preview()

    def _on_scale(self, value):
        gb = round(float(value), 2)
        if abs(gb - self._amt.get()) > 0.01:
            self._amt.set(gb)

    def _on_amt(self):
        try:
            gb = float(self._amt.get())
        except (tk.TclError, ValueError):
            return
        mx = self._max_amount(self._current_donor()) / _GB
        gb = max(0.0, min(gb, mx))
        if abs(self._scale.get() - gb) > 0.01:
            self._scale.set(gb)
        self._refresh_preview()

    def _refresh_preview(self):
        try:
            amount = int(float(self._amt.get()) * _GB)
        except (tk.TclError, ValueError):
            amount = 0
        amount = (amount // _MB) * _MB
        donor = self._current_donor()
        tname = self._target.drive_letter + ":" if self._target.drive_letter else f"#{self._target.partition_number}"
        dname = donor.drive_letter + ":" if donor.drive_letter else f"#{donor.partition_number}"
        if self._mode_of(donor) == "erase":
            leftover = donor.size - amount
            tail = (f"   ({human_size(leftover)} left as unallocated free space)"
                    if leftover >= _MB else "")
            self._preview.config(
                text=f"Result: {dname} {human_size(donor.size)} → DELETED,   "
                     f"{tname} {human_size(self._target.size)} → "
                     f"{human_size(self._target.size + amount)}.{tail}")
        else:
            self._preview.config(
                text=f"Result: {dname} {human_size(donor.size)} → "
                     f"{human_size(donor.size - amount)},   "
                     f"{tname} {human_size(self._target.size)} → "
                     f"{human_size(self._target.size + amount)}.")

    # -- finish -------------------------------------------------------------
    def _ok(self):
        try:
            amount = int(float(self._amt.get()) * _GB)
        except (tk.TclError, ValueError):
            self._err.config(text="Enter a valid amount.")
            return
        amount = (amount // _MB) * _MB
        if amount <= 0:
            self._err.config(text="Choose an amount greater than 0.")
            return
        donor = self._current_donor()
        if amount > self._max_amount(donor):
            self._err.config(text="Amount exceeds what the donor can give.")
            return
        self.result = (donor, amount, self._mode_of(donor))
        self.destroy()

    def _cancel(self):
        self.result = None
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
