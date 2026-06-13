"""Proportional disk-map widget.

Renders a disk's segments (partitions + unallocated regions) as proportional
horizontal bars on a Tk Canvas. Clicking a segment selects it and fires a
callback. Free space is drawn hatched/grey; the selected segment is outlined.

Labels are measured against each bar's pixel width and elided with "…" so text
never spills into neighbouring bars. Segments too narrow to label legibly show
their full details in a hover tooltip instead.
"""
from __future__ import annotations

import tkinter as tk
import tkinter.font as tkfont
from typing import Callable, List, Optional

from diskmgr.model import Disk, Segment, human_size

# Colour palette — distinct, readable on a light background.
_PART_COLORS = ["#4F81BD", "#9BBB59", "#C0504D", "#8064A2", "#4BACC6", "#F79646"]
_FREE_COLOR = "#D9D9D9"
_OS_PART_COLOR = "#2E5A88"
_SELECT_OUTLINE = "#FF0000"
_MIN_SEG_PX = 28        # minimum drawn width so tiny partitions stay clickable
_TEXT_PAD = 6           # horizontal breathing room kept inside each bar
_TWO_LINE_MIN_PX = 48   # below this, show a single line (title only)


class DiskMap(tk.Canvas):
    """Canvas that draws one disk's layout as proportional bars."""

    def __init__(self, master, on_select: Callable[[Optional[Segment]], None], **kw):
        super().__init__(master, height=84, bg="white", highlightthickness=1,
                         highlightbackground="#AAAAAA", **kw)
        self._on_select = on_select
        self._disk: Optional[Disk] = None
        self._selected: Optional[Segment] = None
        self._rects: List[tuple] = []  # (rect_id, segment)

        # Fonts created once so we can measure text width for elision.
        self._font_title = tkfont.Font(family="Segoe UI", size=8, weight="bold")
        self._font_sub = tkfont.Font(family="Segoe UI", size=7)

        # Lightweight hover tooltip for narrow segments.
        self._tip: Optional[tk.Toplevel] = None
        self._tip_label: Optional[tk.Label] = None
        self._hover_seg: Optional[Segment] = None

        self.bind("<Configure>", lambda e: self.redraw())
        self.bind("<Button-1>", self._on_click)
        self.bind("<Motion>", self._on_motion)
        self.bind("<Leave>", lambda e: self._hide_tip())

    def set_disk(self, disk: Optional[Disk]) -> None:
        self._disk = disk
        self._selected = None
        self._hide_tip()
        self.redraw()

    @property
    def selected(self) -> Optional[Segment]:
        return self._selected

    def select_segment(self, seg: Optional[Segment]) -> None:
        self._selected = seg
        self.redraw()
        self._on_select(seg)

    def redraw(self) -> None:
        self.delete("all")
        self._rects.clear()
        if not self._disk or self._disk.size <= 0 or not self._disk.segments:
            self.create_text(10, 40, anchor="w", fill="#888888",
                             text="No layout to display")
            return

        w = max(self.winfo_width(), 100)
        h = self.winfo_height()
        pad = 6
        usable_w = w - 2 * pad
        total = float(self._disk.size)

        # First pass: compute proportional widths, enforcing a clickable minimum
        # so very small partitions remain visible/selectable.
        raw = [max(usable_w * (s.size / total), _MIN_SEG_PX)
               for s in self._disk.segments]
        scale = usable_w / sum(raw) if sum(raw) > usable_w else 1.0
        widths = [r * scale for r in raw]

        x = pad
        color_idx = 0
        for seg, sw in zip(self._disk.segments, widths):
            x0, x1 = x, x + sw
            if seg.is_free:
                fill = _FREE_COLOR
            elif seg.is_boot or seg.is_system:
                fill = _OS_PART_COLOR
            else:
                fill = _PART_COLORS[color_idx % len(_PART_COLORS)]
                color_idx += 1

            outline = _SELECT_OUTLINE if seg is self._selected else "#555555"
            width = 3 if seg is self._selected else 1
            rect = self.create_rectangle(x0, pad, x1, h - pad, fill=fill,
                                         outline=outline, width=width)
            self._rects.append((rect, seg))

            # Labels (clipped/elided to the bar so they never overflow).
            self._draw_label(seg, x0, x1, h)
            x = x1

    # -- labels -------------------------------------------------------------
    def _seg_title(self, seg: Segment) -> str:
        if seg.is_free:
            return "Unallocated"
        if seg.drive_letter:
            return f"{seg.drive_letter}:"
        if seg.is_system:
            return "System"
        if seg.is_boot:
            return "Boot"
        return f"#{seg.partition_number}"

    def _elide(self, text: str, font: tkfont.Font, max_px: float) -> str:
        """Return `text` truncated with an ellipsis so it fits within max_px,
        or "" if not even a single character fits."""
        if max_px <= 0 or not text:
            return ""
        if font.measure(text) <= max_px:
            return text
        ell = "…"
        for i in range(len(text) - 1, 0, -1):
            cand = text[:i] + ell
            if font.measure(cand) <= max_px:
                return cand
        return ell if font.measure(ell) <= max_px else ""

    def _draw_label(self, seg: Segment, x0: float, x1: float, h: int) -> None:
        bw = x1 - x0
        avail = bw - _TEXT_PAD
        if avail <= 2:
            return  # too narrow for any legible text; tooltip covers it
        cx = (x0 + x1) / 2
        fg = "#333333" if seg.is_free else "white"

        title = self._elide(self._seg_title(seg), self._font_title, avail)
        if not title:
            return
        sub = self._elide(human_size(seg.size), self._font_sub, avail)

        if sub and bw >= _TWO_LINE_MIN_PX:
            self.create_text(cx, h / 2 - 8, text=title, fill=fg,
                             font=self._font_title)
            self.create_text(cx, h / 2 + 8, text=sub, fill=fg,
                             font=self._font_sub)
        else:
            # Not enough width for two lines — show the identifier only.
            self.create_text(cx, h / 2, text=title, fill=fg,
                             font=self._font_title)

    # -- interaction --------------------------------------------------------
    def _seg_at(self, px: float, py: float) -> Optional[Segment]:
        for rect, seg in self._rects:
            x0, y0, x1, y1 = self.coords(rect)
            if x0 <= px <= x1 and y0 <= py <= y1:
                return seg
        return None

    def _on_click(self, event) -> None:
        self.select_segment(self._seg_at(event.x, event.y))

    # -- tooltip ------------------------------------------------------------
    def _tip_text(self, seg: Segment) -> str:
        parts = [self._seg_title(seg), human_size(seg.size)]
        if not seg.is_free:
            fs = seg.file_system or "RAW/unknown"
            parts.append(fs)
            if seg.volume and seg.volume.size_remaining is not None:
                parts.append(f"{human_size(seg.volume.size_remaining)} free")
        return "  •  ".join(parts)

    def _on_motion(self, event) -> None:
        seg = self._seg_at(event.x, event.y)
        if seg is self._hover_seg:
            return
        self._hover_seg = seg
        if seg is None:
            self._hide_tip()
        else:
            self._show_tip(self._tip_text(seg), event.x_root, event.y_root)

    def _show_tip(self, text: str, x_root: int, y_root: int) -> None:
        if self._tip is None:
            self._tip = tk.Toplevel(self)
            self._tip.wm_overrideredirect(True)
            self._tip.attributes("-topmost", True)
            self._tip_label = tk.Label(
                self._tip, text="", justify="left", background="#FFFFE1",
                relief="solid", borderwidth=1, font=("Segoe UI", 8),
                padx=6, pady=3)
            self._tip_label.pack()
        self._tip_label.config(text=text)
        self._tip.wm_geometry(f"+{x_root + 12}+{y_root + 18}")
        self._tip.deiconify()

    def _hide_tip(self) -> None:
        self._hover_seg = None
        if self._tip is not None:
            self._tip.withdraw()
