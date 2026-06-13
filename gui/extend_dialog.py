"""Extend-partition dialog.

Presents the supported size bounds (from Get-PartitionSupportedSize), lets the
user pick a new total size via slider or numeric entry (in GB), enforces the
min/max, flags non-NTFS file systems, and returns the chosen size in bytes.
The caller performs the final typed confirmation + resize.
"""
from __future__ import annotations

import tkinter as tk
from tkinter import ttk
from typing import Optional

from diskmgr.model import Segment, human_size
from diskmgr.storage import SupportedSize

_GB = 1024 ** 3


class ExtendDialog(tk.Toplevel):
    """Modal dialog returning `result` (new size in bytes) or None if cancelled."""

    def __init__(self, master, partition: Segment, free_after: Segment,
                 supported: SupportedSize):
        super().__init__(master)
        self.title("Extend Partition")
        self.resizable(False, False)
        self.transient(master)
        self.grab_set()

        self.result: Optional[int] = None
        self._part = partition
        self._supported = supported

        # The slider works in GB for a friendly step; we clamp to byte bounds
        # on commit so we never exceed SizeMin/SizeMax.
        self._min_b = supported.size_min
        self._max_b = supported.size_max
        cur_b = partition.size

        pad = {"padx": 10, "pady": 4}
        frm = ttk.Frame(self, padding=12)
        frm.grid(sticky="nsew")

        label = (f"Partition #{partition.partition_number}"
                 + (f" ({partition.drive_letter}:)" if partition.drive_letter else ""))
        ttk.Label(frm, text=label, font=("Segoe UI", 11, "bold")).grid(
            row=0, column=0, columnspan=2, sticky="w", **pad)

        rows = [
            ("File system:", partition.file_system or "unknown"),
            ("Current size:", human_size(cur_b)),
            ("Available free space:", human_size(free_after.size)),
            ("Minimum size:", human_size(self._min_b)),
            ("Maximum size:", human_size(self._max_b)),
        ]
        for i, (k, v) in enumerate(rows, start=1):
            ttk.Label(frm, text=k).grid(row=i, column=0, sticky="w", **pad)
            ttk.Label(frm, text=v).grid(row=i, column=1, sticky="w", **pad)

        # NTFS = safe online grow. Anything else: warn loudly.
        fs = (partition.file_system or "").upper()
        if fs != "NTFS":
            warn = ttk.Label(
                frm,
                text=("⚠ Online resize is fully supported only for NTFS.\n"
                      f"This volume is {partition.file_system or 'unknown'} — "
                      "resize may fail or is unsupported."),
                foreground="#B00000", wraplength=360, justify="left")
            warn.grid(row=6, column=0, columnspan=2, sticky="w", **pad)

        # New-size controls.
        ttk.Separator(frm).grid(row=7, column=0, columnspan=2, sticky="ew", pady=8)
        ttk.Label(frm, text="New total size (GB):").grid(
            row=8, column=0, sticky="w", **pad)

        self._var_gb = tk.DoubleVar(value=round(self._max_b / _GB, 2))
        entry = ttk.Entry(frm, textvariable=self._var_gb, width=12)
        entry.grid(row=8, column=1, sticky="w", **pad)

        self._scale = ttk.Scale(
            frm, from_=self._min_b / _GB, to=self._max_b / _GB,
            orient="horizontal", length=340, command=self._on_scale)
        self._scale.set(self._max_b / _GB)
        self._scale.grid(row=9, column=0, columnspan=2, sticky="ew", **pad)

        ttk.Label(frm, text=f"Range: {human_size(self._min_b)} – "
                            f"{human_size(self._max_b)}",
                  foreground="#666666").grid(
            row=10, column=0, columnspan=2, sticky="w", **pad)

        self._err = ttk.Label(frm, text="", foreground="#B00000", wraplength=360)
        self._err.grid(row=11, column=0, columnspan=2, sticky="w", **pad)

        btns = ttk.Frame(frm)
        btns.grid(row=12, column=0, columnspan=2, sticky="e", pady=(10, 0))
        ttk.Button(btns, text="Cancel", command=self._cancel).grid(row=0, column=0, padx=4)
        self._ok = ttk.Button(btns, text="Extend…", command=self._confirm)
        self._ok.grid(row=0, column=1, padx=4)

        # Keep entry and slider in sync.
        self._var_gb.trace_add("write", lambda *_: self._on_entry())

        self.bind("<Return>", lambda e: self._confirm())
        self.bind("<Escape>", lambda e: self._cancel())
        self._center_on(master)

    # -- sync helpers -------------------------------------------------------
    def _on_scale(self, value: str) -> None:
        gb = round(float(value), 2)
        if abs(gb - self._var_gb.get()) > 0.01:
            self._var_gb.set(gb)

    def _on_entry(self) -> None:
        try:
            gb = float(self._var_gb.get())
        except (tk.TclError, ValueError):
            return
        lo, hi = self._min_b / _GB, self._max_b / _GB
        gb = max(lo, min(hi, gb))
        if abs(self._scale.get() - gb) > 0.01:
            self._scale.set(gb)

    def _chosen_bytes(self) -> Optional[int]:
        try:
            gb = float(self._var_gb.get())
        except (tk.TclError, ValueError):
            self._err.config(text="Enter a valid number.")
            return None
        # Snap slider-max to the exact byte max so we don't truncate usable space.
        if abs(gb - self._max_b / _GB) < 0.01:
            return self._max_b
        if abs(gb - self._min_b / _GB) < 0.01:
            return self._min_b
        b = int(round(gb * _GB))
        if b < self._min_b or b > self._max_b:
            self._err.config(
                text=f"Size must be between {human_size(self._min_b)} and "
                     f"{human_size(self._max_b)}.")
            return None
        return b

    # -- buttons ------------------------------------------------------------
    def _confirm(self) -> None:
        b = self._chosen_bytes()
        if b is None:
            return
        if b <= self._part.size:
            self._err.config(text="New size must be larger than the current size "
                                  "to extend.")
            return
        self.result = b
        self.destroy()

    def _cancel(self) -> None:
        self.result = None
        self.destroy()

    def _center_on(self, master) -> None:
        self.update_idletasks()
        try:
            mx, my = master.winfo_rootx(), master.winfo_rooty()
            mw, mh = master.winfo_width(), master.winfo_height()
            w, h = self.winfo_width(), self.winfo_height()
            self.geometry(f"+{mx + (mw - w)//2}+{my + (mh - h)//2}")
        except Exception:
            pass
