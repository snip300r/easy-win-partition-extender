"""Main application window — partition-manager-style layout.

Top:    safety banner + toolbar.
Centre: scrollable per-disk layout bars (click a partition / free region) and a
        partition table listing every partition.
Right:  Operations sidebar acting on the current selection.
Bottom: the Pending-Operations queue + Apply / Discard, the raw-write ARM
        switch (dry-run by default), a progress bar and status line.

The user stacks operations, reviews them, then Applies — exactly like the
commercial tools. Applying in dry-run mode simulates everything (and logs the
sector-level plan) without writing a single byte.
"""
from __future__ import annotations

import queue
import threading
import tkinter as tk
from tkinter import ttk, messagebox
from typing import Dict, List, Optional, Tuple

from diskmgr import storage, planner, operations
from diskmgr.admin import is_admin
from diskmgr.logging_util import get_logger, log_path
from diskmgr.model import Disk, Segment, human_size
from diskmgr.storage import SupportedSize
from .diskmap import DiskMap
from .resize_move_dialog import ResizeMoveDialog
from .extend_dialog import ExtendDialog
from .allocate_dialog import AllocateDialog

log = get_logger()


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("DiskFormat — Partition Manager")
        self.geometry("1180x780")
        self.minsize(1000, 680)

        self._disks: List[Disk] = []
        self._maps: List[DiskMap] = []
        self._tree_index: Dict[str, Tuple[Disk, Segment]] = {}
        self._sel: Optional[Tuple[Disk, Segment]] = None
        self._pending: List[operations.Operation] = []

        self._armed = tk.BooleanVar(value=False)  # raw writes disarmed by default
        self._force = tk.BooleanVar(value=False)  # force-dismount disabled by default

        self._ui_q: "queue.Queue" = queue.Queue()

        self._build_banner()
        self._build_toolbar()
        self._build_body()
        self._build_pending_pane()
        self._build_statusbar()

        self.after(100, self.refresh)
        self.after(120, self._pump_ui_queue)

    # ===================================================================
    # Layout construction
    # ===================================================================
    def _build_banner(self):
        banner = tk.Frame(self, bg="#FFF3CD", bd=1, relief="solid")
        banner.pack(fill="x", side="top")
        tk.Label(banner, bg="#FFF3CD", fg="#856404", pady=6,
                 font=("Segoe UI", 9, "bold"),
                 text=("⚠  Disk operations can cause PERMANENT DATA LOSS — "
                       "BACK UP first. MOVE physically relocates data and is "
                       "high-risk. Use at your own risk.")
                 ).pack(side="left", padx=10)

    def _build_toolbar(self):
        bar = ttk.Frame(self, padding=(8, 6))
        bar.pack(fill="x", side="top")
        ttk.Button(bar, text="⟳ Refresh", command=self.refresh).pack(side="left")
        ttk.Separator(bar, orient="vertical").pack(side="left", fill="y", padx=8)
        ttk.Label(bar, text="Log:").pack(side="left")
        ttk.Label(bar, text=log_path(), foreground="#555").pack(side="left", padx=(2, 0))

        admin = is_admin()
        self._admin_lbl = tk.Label(
            bar, fg="#1B7A1B" if admin else "#B00000", font=("Segoe UI", 9, "bold"),
            text="● Administrator" if admin else "● NOT elevated (read-only)")
        self._admin_lbl.pack(side="right")

    def _build_body(self):
        outer = ttk.Frame(self)
        outer.pack(fill="both", expand=True, padx=8, pady=(0, 4))

        paned = ttk.PanedWindow(outer, orient="horizontal")
        paned.pack(fill="both", expand=True)

        # --- left: disk maps (top) + partition table (bottom) ---
        left = ttk.Frame(paned)
        paned.add(left, weight=4)

        maps_lf = ttk.LabelFrame(left, text="Disks", padding=4)
        maps_lf.pack(fill="both", expand=True)
        self._canvas = tk.Canvas(maps_lf, highlightthickness=0, height=260)
        vsb = ttk.Scrollbar(maps_lf, orient="vertical", command=self._canvas.yview)
        self._inner = ttk.Frame(self._canvas)
        self._inner.bind("<Configure>", lambda e: self._canvas.configure(
            scrollregion=self._canvas.bbox("all")))
        self._win = self._canvas.create_window((0, 0), window=self._inner, anchor="nw")
        self._canvas.bind("<Configure>",
                          lambda e: self._canvas.itemconfigure(self._win, width=e.width))
        self._canvas.configure(yscrollcommand=vsb.set)
        self._canvas.pack(side="left", fill="both", expand=True)
        vsb.pack(side="right", fill="y")

        table_lf = ttk.LabelFrame(left, text="Partitions", padding=4)
        table_lf.pack(fill="both", expand=True, pady=(6, 0))
        cols = ("disk", "part", "drive", "fs", "capacity", "used", "free", "status")
        self._tree = ttk.Treeview(table_lf, columns=cols, show="headings", height=8)
        headings = {
            "disk": ("Disk", 50), "part": ("Partition", 90), "drive": ("Drive", 50),
            "fs": ("File system", 90), "capacity": ("Capacity", 90),
            "used": ("Used", 90), "free": ("Free", 90), "status": ("Status", 150),
        }
        for c, (txt, w) in headings.items():
            self._tree.heading(c, text=txt)
            self._tree.column(c, width=w, anchor="w")
        tvsb = ttk.Scrollbar(table_lf, orient="vertical", command=self._tree.yview)
        self._tree.configure(yscrollcommand=tvsb.set)
        self._tree.pack(side="left", fill="both", expand=True)
        tvsb.pack(side="right", fill="y")
        self._tree.bind("<<TreeviewSelect>>", self._on_tree_select)

        # --- right: operations sidebar ---
        side = ttk.LabelFrame(paned, text="Operations", padding=10)
        paned.add(side, weight=1)
        self._ops_buttons: List[ttk.Button] = []

        def add_btn(text, cmd):
            b = ttk.Button(side, text=text, command=cmd, width=24)
            b.pack(fill="x", pady=3)
            self._ops_buttons.append(b)
            return b

        add_btn("Resize / Move…", self._op_resize_move)
        add_btn("Extend to size…", self._op_extend_to_size)
        add_btn("Allocate space (borrow)…", self._op_allocate)
        add_btn("Shrink…", self._op_shrink)

        ttk.Separator(side).pack(fill="x", pady=8)
        ttk.Label(side, text="Selection", font=("Segoe UI", 9, "bold")).pack(anchor="w")
        self._details = tk.Text(side, width=30, height=18, state="disabled",
                                wrap="word", font=("Consolas", 9),
                                background="#FAFAFA", relief="flat")
        self._details.pack(fill="both", expand=True, pady=(4, 0))

    def _build_pending_pane(self):
        lf = ttk.LabelFrame(self, text="Pending operations", padding=6)
        lf.pack(fill="x", side="top", padx=8, pady=(0, 4))

        top = ttk.Frame(lf)
        top.pack(fill="x")
        self._pending_list = tk.Listbox(top, height=4, font=("Consolas", 9))
        self._pending_list.pack(side="left", fill="both", expand=True)
        plsb = ttk.Scrollbar(top, orient="vertical", command=self._pending_list.yview)
        self._pending_list.configure(yscrollcommand=plsb.set)
        plsb.pack(side="right", fill="y")

        ctl = ttk.Frame(lf)
        ctl.pack(fill="x", pady=(6, 0))

        arm = ttk.Checkbutton(
            ctl, variable=self._armed, command=self._on_arm_toggle,
            text="ARM raw writes (uncheck = dry-run simulation, writes nothing)")
        arm.pack(side="left")
        self._arm_lbl = tk.Label(ctl, text="DRY-RUN", fg="#1B7A1B",
                                 font=("Segoe UI", 9, "bold"))
        self._arm_lbl.pack(side="left", padx=10)

        force_cb = ttk.Checkbutton(
            ctl, variable=self._force, command=self._on_force_toggle,
            text="Force-dismount busy volumes (EXPERT — can lose open data)")
        force_cb.pack(side="left", padx=(12, 0))

        ttk.Button(ctl, text="Discard all", command=self._discard).pack(side="right")
        self._apply_btn = ttk.Button(ctl, text="Apply", command=self._apply)
        self._apply_btn.pack(side="right", padx=6)
        ttk.Button(ctl, text="Remove selected",
                   command=self._remove_selected_pending).pack(side="right", padx=6)

        self._progress = ttk.Progressbar(lf, mode="determinate")
        self._progress.pack(fill="x", pady=(6, 0))

    def _build_statusbar(self):
        self._status = tk.StringVar(value="Ready.")
        bar = ttk.Frame(self, relief="sunken", padding=(8, 3))
        bar.pack(fill="x", side="bottom")
        ttk.Label(bar, textvariable=self._status).pack(side="left")

    # ===================================================================
    # Data / rendering
    # ===================================================================
    def refresh(self):
        if self._pending:
            if not messagebox.askyesno(
                    "Discard pending?",
                    "Refreshing will clear the pending-operations queue. Continue?"):
                return
        self._set_status("Enumerating disks…")
        try:
            self._disks = storage.enumerate_disks()
        except storage.StorageError as e:
            messagebox.showerror("Enumeration failed", str(e))
            self._set_status("Enumeration failed.")
            return
        self._pending.clear()
        self._refresh_pending_view()
        self._render_disks()
        self._render_table()
        self._clear_selection()
        self._set_status(f"Found {len(self._disks)} disk(s).")

    def _render_disks(self):
        for w in self._inner.winfo_children():
            w.destroy()
        self._maps.clear()
        for disk in self._disks:
            frame = ttk.Frame(self._inner, padding=(4, 6))
            frame.pack(fill="x", expand=True)
            header = (f"Disk {disk.number}  •  {disk.friendly_name}  •  "
                      f"{disk.partition_style}  •  {human_size(disk.size)}"
                      f"  •  {disk.bus_type or ''}")
            if disk.is_os_disk:
                header += "   [OS DISK — high risk]"
            ttk.Label(frame, text=header, font=("Segoe UI", 10, "bold"),
                      foreground="#2E5A88" if disk.is_os_disk else "#000").pack(anchor="w")
            dm = DiskMap(frame, on_select=lambda seg, d=disk: self._select(d, seg))
            dm.pack(fill="x", pady=3)
            dm.set_disk(disk)
            self._maps.append(dm)
            ttk.Separator(frame).pack(fill="x", pady=(4, 0))

    def _render_table(self):
        self._tree.delete(*self._tree.get_children())
        self._tree_index.clear()
        for disk in self._disks:
            for s in disk.segments:
                if s.kind == "free":
                    iid = f"d{disk.number}o{s.offset}"
                    self._tree.insert("", "end", iid=iid, values=(
                        disk.number, "—", "—", "Unallocated",
                        human_size(s.size), "—", human_size(s.size), "Free space"))
                else:
                    flags = []
                    if s.is_system: flags.append("System")
                    if s.is_boot: flags.append("Boot")
                    if s.is_active: flags.append("Active")
                    used = human_size(s.volume.used) if (s.volume and s.volume.used is not None) else "—"
                    free = human_size(s.volume.size_remaining) if (s.volume and s.volume.size_remaining is not None) else "—"
                    iid = f"d{disk.number}o{s.offset}"
                    self._tree.insert("", "end", iid=iid, values=(
                        disk.number, f"#{s.partition_number}",
                        f"{s.drive_letter}:" if s.drive_letter else "—",
                        s.file_system or "raw", human_size(s.size), used, free,
                        ", ".join(flags) or "—"))
                self._tree_index[iid] = (disk, s)

    # ===================================================================
    # Selection
    # ===================================================================
    def _select(self, disk: Disk, seg: Optional[Segment]):
        # Clear other maps' selection.
        for dm in self._maps:
            if dm._disk is not disk and dm._selected is not None:
                dm._selected = None
                dm.redraw()
        self._sel = (disk, seg) if seg else None
        # Sync the table highlight.
        if seg:
            iid = f"d{disk.number}o{seg.offset}"
            if self._tree.exists(iid):
                self._tree.selection_set(iid)
                self._tree.see(iid)
        self._update_details()
        self._update_actions()

    def _on_tree_select(self, _evt):
        sel = self._tree.selection()
        if not sel:
            return
        pair = self._tree_index.get(sel[0])
        if not pair:
            return
        disk, seg = pair
        # Reflect into the disk map.
        for dm in self._maps:
            if dm._disk is disk:
                dm._selected = seg
                dm.redraw()
            elif dm._selected is not None:
                dm._selected = None
                dm.redraw()
        self._sel = (disk, seg)
        self._update_details()
        self._update_actions()

    def _clear_selection(self):
        self._sel = None
        self._update_details()
        self._update_actions()

    def _update_actions(self):
        is_part = bool(self._sel and self._sel[1].kind == "partition")
        state = "normal" if is_part else "disabled"
        for b in self._ops_buttons:
            b.config(state=state)

    def _update_details(self):
        self._details.config(state="normal")
        self._details.delete("1.0", "end")
        if not self._sel:
            self._details.insert("end", "Select a partition or free region.")
            self._details.config(state="disabled")
            return
        d, s = self._sel
        lines = [f"Disk {d.number} ({d.partition_style})",
                 d.friendly_name, ""]
        if s.is_free:
            lines += ["UNALLOCATED",
                      f"Offset: {human_size(s.offset)}",
                      f"Size:   {human_size(s.size)}"]
        else:
            right_free = sum(x.size for x in d.segments[d.segments.index(s) + 1:]
                             if x.is_free)
            lines += [
                f"Partition #{s.partition_number}",
                f"Drive:  {s.drive_letter + ':' if s.drive_letter else '(none)'}",
                f"FS:     {s.file_system or 'raw'}",
                f"Size:   {human_size(s.size)}",
            ]
            if s.volume and s.volume.size_remaining is not None:
                lines += [f"Used:   {human_size(s.volume.used)}",
                          f"Free:   {human_size(s.volume.size_remaining)}"]
            lines += [
                f"Offset: {human_size(s.offset)}",
                "",
                f"Boot:{s.is_boot} System:{s.is_system} Active:{s.is_active}",
                "",
                f"Free space to the right (usable by Extend-to-size): "
                f"{human_size(right_free)}",
            ]
            if d.is_os_disk:
                lines += ["", "⚠ OS disk: extend/shrink/allocate allowed",
                          "  but MOVES here are HIGH RISK.",
                          "  Boot/System partitions never move."]
        self._details.insert("end", "\n".join(lines))
        self._details.config(state="disabled")

    # ===================================================================
    # Operation builders (add to pending queue)
    # ===================================================================
    def _require_partition(self) -> Optional[Tuple[Disk, Segment]]:
        if not self._sel or self._sel[1].kind != "partition":
            messagebox.showinfo("Select a partition",
                                "Please select a partition first.")
            return None
        return self._sel

    def _op_resize_move(self):
        sel = self._require_partition()
        if not sel:
            return
        disk, seg = sel
        idx = disk.segments.index(seg)
        before = disk.segments[idx - 1] if idx > 0 and disk.segments[idx - 1].is_free else None
        after = disk.segments[idx + 1] if idx + 1 < len(disk.segments) and disk.segments[idx + 1].is_free else None
        if not before and not after:
            messagebox.showinfo(
                "No adjacent free space",
                "This partition has no unallocated space directly beside it.\n\n"
                "Use “Extend to size…” to pull in free space from elsewhere on "
                "the disk (this may move other partitions).")
            return
        dlg = ResizeMoveDialog(self, disk, seg, before, after)
        self.wait_window(dlg)
        if dlg.result:
            self._add_ops(dlg.result)

    def _op_extend_to_size(self):
        sel = self._require_partition()
        if not sel:
            return
        disk, seg = sel
        idx = disk.segments.index(seg)
        right_free = sum(x.size for x in disk.segments[idx + 1:] if x.is_free)

        # A partition can only grow into UNALLOCATED space on the SAME physical
        # disk. If there's no meaningful free space to the right, explain why and
        # point at the realistic ways to create some — don't open a dead slider.
        USABLE_MIN = 16 * 1024 * 1024  # ignore sub-16 MiB alignment slivers
        if right_free < USABLE_MIN:
            self._explain_no_space(disk, seg, idx)
            return

        # Reuse the extend dialog: max = current + all right-side free; the
        # planner will move intervening partitions as needed.
        supported = SupportedSize(size_min=seg.size, size_max=seg.size + right_free)
        synthetic_free = Segment(kind="free", offset=seg.end, size=right_free)
        dlg = ExtendDialog(self, seg, synthetic_free, supported)
        self.wait_window(dlg)
        if dlg.result is None:
            return
        try:
            ops = planner.plan_extend(disk, seg, dlg.result)
        except planner.PlanError as e:
            messagebox.showerror("Cannot plan extend", str(e))
            return
        self._add_ops(ops)

    # Smallest worthwhile borrow — below this a donor isn't offered.
    _MIN_BORROW = 64 * 1024 * 1024  # 64 MB

    def _safe_borrow_max(self, donor: Segment) -> int:
        """Bytes that can be shaved off `donor` WITHOUT data loss or a reformat.

        Authoritative source: Get-PartitionSupportedSize, which reports how far
        Windows will shrink the volume online — it already accounts for used
        data AND unmovable files. (donor.size - SizeMin) is therefore the exact
        amount that can be borrowed safely. Falls back to the volume free-space
        figure only when the supported size can't be queried (e.g. the app isn't
        elevated). Returns 0 for volumes Windows can't shrink in place (RAW/
        unknown/exFAT/FAT) — borrowing from those would require a reformat."""
        try:
            sup = storage.get_supported_size(donor.disk_number,
                                             donor.partition_number)
            return max(0, donor.size - sup.size_min)
        except storage.StorageError:
            free_in = (donor.volume.size_remaining
                       if (donor.volume and donor.volume.size_remaining) else 0)
            return max(0, free_in)

    def _eligible_donors(self, disk: Disk, seg: Segment,
                         idx: int) -> List[Tuple[Segment, int, str]]:
        """Partitions to the RIGHT of `seg` on the same disk that can give up
        space, each as (segment, max_bytes, mode):

          * mode "shrink" — Windows can shrink it online; data is PRESERVED.
          * mode "erase"  — not online-shrinkable; space can only be reclaimed
                            by DELETING the donor (its data is lost). Offered so
                            the user can borrow from a RAW/unknown volume when
                            they have accepted the format.
        """
        out: List[Tuple[Segment, int, str]] = []
        for x in disk.segments[idx + 1:]:
            if x.kind != "partition" or x.is_boot or x.is_system:
                continue
            shrink_max = self._safe_borrow_max(x)
            if shrink_max >= self._MIN_BORROW:
                out.append((x, shrink_max, "shrink"))
            else:
                erase_max = (x.size // (1024 * 1024)) * (1024 * 1024)
                if erase_max >= self._MIN_BORROW:
                    out.append((x, erase_max, "erase"))
        return out

    def _op_allocate(self):
        sel = self._require_partition()
        if not sel:
            return
        disk, seg = sel
        idx = disk.segments.index(seg)
        donors = self._eligible_donors(disk, seg, idx)
        if not donors:
            messagebox.showinfo(
                "No donor available",
                self._no_donor_reason(disk, seg, idx))
            return
        dlg = AllocateDialog(self, disk, seg, donors)
        self.wait_window(dlg)
        if not dlg.result:
            return
        donor, amount, mode = dlg.result
        dname = donor.drive_letter + ":" if donor.drive_letter else \
            f"#{donor.partition_number}"
        if mode == "erase":
            if not messagebox.askyesno(
                "Erase donor to borrow — DATA LOSS",
                f"{dname} ({donor.file_system or 'RAW/unknown'}, "
                f"{human_size(donor.size)}) cannot be shrunk in place by "
                f"Windows, so borrowing from it requires DELETING it.\n\n"
                f"ALL DATA ON {dname} WILL BE PERMANENTLY LOST.\n\n"
                f"The plan will erase {dname} and add up to "
                f"{human_size(amount)} to {seg.drive_letter or '#'+str(seg.partition_number)}:"
                f". Back up {dname} first.\n\nContinue?",
                icon="warning", default="no"):
                return
            try:
                ops = planner.plan_allocate_erase(disk, seg, donor, amount)
            except planner.PlanError as e:
                messagebox.showerror("Cannot plan", str(e))
                return
        else:
            try:
                ops = planner.plan_allocate(disk, seg, donor, amount)
            except planner.PlanError as e:
                messagebox.showerror("Cannot plan", str(e))
                return
        if disk.is_os_disk:
            if not messagebox.askyesno(
                "OS disk — high risk",
                "This plan MOVES partitions on the disk Windows is running "
                "from. If a donor volume is in use (open files, pagefile) the "
                "move can fail, and a crash/power loss mid-move can lose data.\n\n"
                "Close apps using the donor drive and BACK UP first.\n\n"
                "Queue this plan anyway?", icon="warning", default="no"):
                return
        self._add_ops(ops)

    def _no_donor_reason(self, disk: Disk, seg: Segment, idx: int) -> str:
        """Build a specific reason no donor is offered. The key distinction the
        user needs: is there simply no partition to the right, or is there one
        (e.g. D:) that Windows can't shrink in place — which is why borrowing
        from it would otherwise force a data-erasing reformat."""
        right_parts = [x for x in disk.segments[idx + 1:]
                       if x.kind == "partition" and not x.is_boot
                       and not x.is_system]
        if not right_parts:
            return (
                "There is no partition to the right of this one (on the same "
                "disk) to borrow from.\n\n"
                "Space can only come from another partition on the SAME "
                "physical disk, located to the right of this one.")

        # There ARE candidates, but none is shrinkable enough. Say which, and
        # why — almost always a non-NTFS / RAW file system.
        lines = ["These partitions are to the right but cannot safely give up "
                 "space right now:\n"]
        for x in right_parts:
            nm = x.drive_letter + ":" if x.drive_letter else f"#{x.partition_number}"
            fs = x.file_system or "RAW/unknown"
            mx = self._safe_borrow_max(x)
            if mx < self._MIN_BORROW:
                if fs.upper() == "NTFS":
                    why = "it is too full to shrink (no free space inside)"
                else:
                    why = (f"its file system ({fs}) cannot be shrunk in place by "
                           "Windows — reclaiming its space would require "
                           "reformatting it and ERASING its data")
                lines.append(f"  • {nm}: {why}.")
        lines.append(
            "\nTo borrow from a non-NTFS volume safely, back it up, convert it "
            "to NTFS (or move its data off and re-create it as NTFS), then try "
            "again. DiskFormat will not reformat it for you.")
        return "\n".join(lines)

    def _explain_no_space(self, disk: Disk, seg: Segment, idx: int):
        """Tell the user *why* a partition can't be extended and what to do."""
        name = seg.drive_letter + ":" if seg.drive_letter else f"#{seg.partition_number}"

        # Shrinkable partitions to the right on THIS disk (potential donors).
        donors = []
        for x in disk.segments[idx + 1:]:
            if x.kind == "partition" and x.volume and x.volume.size_remaining:
                donors.append((x, x.volume.size_remaining))

        # Free space on OTHER disks (cannot help — different physical disk).
        other_free = 0
        for d in self._disks:
            if d.number == disk.number:
                continue
            other_free += sum(s.size for s in d.segments if s.is_free)

        lines = [
            f"{name} cannot be extended right now.",
            "",
            f"Disk {disk.number} ({disk.friendly_name}) has no usable "
            f"unallocated space after {name} — only "
            f"{human_size(sum(x.size for x in disk.segments[idx+1:] if x.is_free))} "
            "of tiny alignment gaps.",
            "",
            "IMPORTANT: a partition can only grow into free space on its OWN "
            "physical disk.",
        ]
        if other_free >= 16 * 1024 * 1024:
            lines += [
                f"You have {human_size(other_free)} free on OTHER disk(s), but "
                "that space cannot be added to this partition.",
            ]
        if donors:
            d_desc = ", ".join(
                f"{(p.drive_letter+':' if p.drive_letter else '#'+str(p.partition_number))}"
                f" (~{human_size(free)} free inside)"
                for p, free in donors)
            lines += [
                "",
                f"To grow {name}, use the “Allocate space (borrow)…” button to "
                f"take space from a partition to its right: {d_desc}.",
                "That builds a Shrink + Move + Extend plan automatically.",
            ]
            if disk.is_os_disk:
                lines += [
                    "",
                    "⚠ NOTE: this is the OS disk, so the plan MOVES partitions "
                    "on the live Windows disk — high risk. Back up first and run "
                    "the dry-run before arming.",
                ]
        else:
            lines += [
                "",
                "There are no shrinkable partitions to the right to reclaim "
                "space from either.",
            ]
        messagebox.showinfo("Nothing to extend into", "\n".join(lines))

    def _op_shrink(self):
        sel = self._require_partition()
        if not sel:
            return
        disk, seg = sel
        used = seg.volume.used if (seg.volume and seg.volume.used) else 0
        min_size = max(used + 16 * 1024 * 1024, 16 * 1024 * 1024)
        # Simple prompt dialog reusing ExtendDialog isn't suitable; ask via the
        # resize/move dialog which supports shrink by dragging the right edge.
        idx = disk.segments.index(seg)
        after = disk.segments[idx + 1] if idx + 1 < len(disk.segments) and disk.segments[idx + 1].is_free else None
        before = disk.segments[idx - 1] if idx > 0 and disk.segments[idx - 1].is_free else None
        dlg = ResizeMoveDialog(self, disk, seg, before, after)
        self.wait_window(dlg)
        if dlg.result:
            self._add_ops(dlg.result)

    def _add_ops(self, ops: List[operations.Operation]):
        self._pending.extend(ops)
        self._refresh_pending_view()
        self._set_status(f"{len(ops)} operation(s) queued "
                         f"({len(self._pending)} pending).")

    def _refresh_pending_view(self):
        self._pending_list.delete(0, "end")
        for i, op in enumerate(self._pending, 1):
            self._pending_list.insert("end", f"{i}.  {op.describe()}")
        self._apply_btn.config(state="normal" if self._pending else "disabled")

    def _remove_selected_pending(self):
        sel = list(self._pending_list.curselection())
        for i in reversed(sel):
            del self._pending[i]
        self._refresh_pending_view()

    def _discard(self):
        self._pending.clear()
        self._refresh_pending_view()
        self._set_status("Pending operations discarded.")

    def _on_arm_toggle(self):
        if self._armed.get():
            if not is_admin():
                messagebox.showwarning(
                    "Not elevated",
                    "Raw writes require administrator rights. Restart the app "
                    "elevated (it will prompt for UAC).")
                self._armed.set(False)
                return
            self._arm_lbl.config(text="ARMED — WILL WRITE", fg="#B00000")
        else:
            self._arm_lbl.config(text="DRY-RUN", fg="#1B7A1B")

    def _on_force_toggle(self):
        if self._force.get():
            if not messagebox.askyesno(
                "Force-dismount — EXPERT / DANGEROUS",
                "Force-dismount forcibly unmounts a busy volume so it can be "
                "moved, even when other programs (or Windows) have files open "
                "on it.\n\n"
                "• Any UNSAVED data in apps using that drive will be LOST.\n"
                "• Close apps that use the drive first anyway.\n"
                "• Never use this on a drive with active important writes.\n\n"
                "Enable force-dismount?", icon="warning", default="no"):
                self._force.set(False)

    # ===================================================================
    # Apply
    # ===================================================================
    def _apply(self):
        if not self._pending:
            return
        armed = self._armed.get()
        dry = not armed

        # Guards for real writes. (Boot/system partitions can never be moved —
        # that is enforced in MoveOp.execute. Moves of *data* partitions on the
        # OS disk are allowed but flagged as high-risk below.)
        if armed:
            if not is_admin():
                messagebox.showerror("Not elevated",
                                     "Administrator rights are required to apply.")
                return
            # All-or-nothing pre-flight: refuse plans that can't possibly apply
            # (e.g. moving the volume this tool runs from) BEFORE any change.
            problems = operations.preflight(self._pending)
            if problems:
                messagebox.showerror(
                    "Cannot apply this plan",
                    "The plan was NOT started (nothing was changed):\n\n• "
                    + "\n\n• ".join(problems))
                return

        has_move = any(isinstance(o, operations.MoveOp) for o in self._pending)
        force = bool(self._force.get()) and armed and has_move

        summary = "\n".join(f"  {i}. {op.describe()}"
                            for i, op in enumerate(self._pending, 1))
        if force:
            summary += ("\n\n⚠ FORCE-DISMOUNT is ON: busy volumes will be "
                        "forcibly unmounted; unsaved data in apps using them "
                        "will be lost.")

        if dry:
            if not messagebox.askyesno(
                    "Simulate (dry-run)",
                    f"Simulate these {len(self._pending)} operation(s)? "
                    "Nothing will be written; the sector-level plan is logged.\n\n"
                    f"{summary}"):
                return
        else:
            if not self._typed_confirm(summary, has_move):
                return

        self._run_apply(dry, force)

    def _typed_confirm(self, summary: str, has_move: bool) -> bool:
        win = tk.Toplevel(self)
        win.title("Confirm Apply")
        win.transient(self); win.grab_set(); win.resizable(False, False)
        frm = ttk.Frame(win, padding=14); frm.grid()
        word = "MOVE" if has_move else "APPLY"
        head = ("You are about to WRITE TO DISK." +
                ("\n\nThis plan includes a partition MOVE — raw data will be "
                 "physically relocated. A failure or power loss mid-move can "
                 "destroy data." if has_move else ""))
        ttk.Label(frm, text=head, foreground="#B00000", wraplength=460,
                  justify="left", font=("Segoe UI", 10, "bold")
                  ).grid(row=0, column=0, sticky="w")
        ttk.Label(frm, text=summary, justify="left", font=("Consolas", 9)
                  ).grid(row=1, column=0, sticky="w", pady=8)
        ttk.Label(frm, text=f"Type {word} to proceed:").grid(row=2, column=0, sticky="w")
        var = tk.StringVar()
        ent = ttk.Entry(frm, textvariable=var, width=20)
        ent.grid(row=3, column=0, sticky="w", pady=6); ent.focus_set()
        out = {"ok": False}

        def ok():
            if var.get().strip().upper() == word:
                out["ok"] = True; win.destroy()
            else:
                messagebox.showwarning("Confirm", f"Type {word} exactly.", parent=win)

        btns = ttk.Frame(frm); btns.grid(row=4, column=0, sticky="e", pady=(8, 0))
        ttk.Button(btns, text="Cancel", command=win.destroy).grid(row=0, column=0, padx=4)
        ttk.Button(btns, text="Proceed", command=ok).grid(row=0, column=1, padx=4)
        win.bind("<Return>", lambda e: ok()); win.bind("<Escape>", lambda e: win.destroy())
        self.wait_window(win)
        return out["ok"]

    def _run_apply(self, dry: bool, force: bool = False):
        ops = list(self._pending)
        self._apply_btn.config(state="disabled")
        self._progress.config(value=0, maximum=100)
        self._set_status("Simulating…" if dry else "Applying — DO NOT power off…")

        def progress(text, done, total):
            pct = (done / total * 100) if total else 0
            self._ui_q.put(("progress", text, pct))

        def worker():
            try:
                operations.execute_plan(ops, dry_run=dry, progress=progress,
                                        force=force)
                self._ui_q.put(("done", dry, None))
            except Exception as e:  # noqa: BLE001
                self._ui_q.put(("error", dry, str(e)))

        threading.Thread(target=worker, daemon=True).start()

    def _pump_ui_queue(self):
        try:
            while True:
                msg = self._ui_q.get_nowait()
                kind = msg[0]
                if kind == "progress":
                    _, text, pct = msg
                    self._progress.config(value=pct)
                    self._set_status(text)
                elif kind == "done":
                    dry = msg[1]
                    if dry:
                        messagebox.showinfo(
                            "Dry-run complete",
                            "Simulation finished — nothing was written.\n\n"
                            f"The detailed plan was logged to:\n{log_path()}\n\n"
                            "To execute for real, tick “ARM raw writes” and Apply "
                            "again.")
                        self._set_status("Dry-run complete (no changes).")
                        self._apply_btn.config(state="normal")
                    else:
                        messagebox.showinfo("Done", "All operations applied.")
                        self._set_status("Apply complete.")
                        self._pending.clear()
                        self.refresh()
                    self._progress.config(value=0)
                elif kind == "error":
                    _, dry, err = msg
                    messagebox.showerror(
                        "Operation failed",
                        f"{'Simulation' if dry else 'Apply'} stopped on an error:\n\n{err}")
                    self._set_status("Failed — see log.")
                    self._progress.config(value=0)
                    self._apply_btn.config(state="normal")
                    if not dry:
                        self.refresh()
        except queue.Empty:
            pass
        self.after(120, self._pump_ui_queue)

    # ===================================================================
    def _set_status(self, text):
        self._status.set(text)


def run():
    App().mainloop()
