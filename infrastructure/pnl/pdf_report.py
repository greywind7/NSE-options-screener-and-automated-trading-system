#!/usr/bin/env python3
"""
infrastructure/pnl/pdf_report.py
==================================
Pure-Python Zero-Dependency PDF 1.4 Vector Report Generator:
Generates publication-quality, executive PDF reports for trading sessions:
1. Executive Header & KPI Metric Cards (Net P&L, Gross, Brokerage, Win Rate, Profit Factor).
2. Clean Vector Intraday Cumulative P&L Chart:
   - Dynamic Y-axis scaling with gridlines & Rs. currency callouts.
   - Zero-baseline reference.
   - Smooth line plot with filled profit/loss shaded regions under curve.
   - Trade execution markers and high-water mark annotations.
3. Execution Mode Breakdown Table (Live, Paper, Paper-Delayed).
4. Sub-Strategy Track Performance Table (Dynamic Trio, Combo, Trail SL, ATM, Runner, etc.).
5. Executed Trade Journal Table with green/red P&L color coding.
6. Zero external PyPI dependencies (pure Python standard library).
"""

from __future__ import annotations

import math
import os
import time
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple


def _sanitize_pdf_text(text: Any) -> str:
    """Sanitize and escape text for standard PDF Type 1 fonts (ASCII-safe)."""
    s = str(text) if text is not None else ""
    s = s.replace("₹", "Rs.")
    replacements = {
        "—": "-", "–": "-", "“": '"', "”": '"', "‘": "'", "’": "'",
        "•": "*", "↗": "^", "↘": "v", "→": ">", "⏳": "", "🚨": "",
        "💥": "", "🚀": "", "🩸": "", "⚡": "", "🚩": "", "🔥": "",
        "💤": "", "🎯": "", "📊": "", "📦": "", "✓": "[OK]", "✗": "[X]",
        "·": ".", "●": "*", "│": "|", "─": "-", "╭": "+", "╰": "+"
    }
    for k, v in replacements.items():
        s = s.replace(k, v)
    # Keep printable ASCII
    cleaned = "".join(c if 32 <= ord(c) <= 126 else " " for c in s)
    return cleaned.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


class PDFCanvas:
    """Vector graphics canvas for constructing a single PDF page."""

    def __init__(self, width: float = 595.28, height: float = 841.89):
        self.width = width
        self.height = height
        self.ops: List[str] = []

    def set_fill_color(self, r: float, g: float, b: float) -> None:
        self.ops.append(f"{r:.3f} {g:.3f} {b:.3f} rg")

    def set_stroke_color(self, r: float, g: float, b: float) -> None:
        self.ops.append(f"{r:.3f} {g:.3f} {b:.3f} RG")

    def set_line_width(self, width: float) -> None:
        self.ops.append(f"{width:.2f} w")

    def set_dash(self, dash_array: List[float] = [], phase: float = 0.0) -> None:
        if dash_array:
            d_str = " ".join(f"{x:.1f}" for x in dash_array)
            self.ops.append(f"[{d_str}] {phase:.1f} d")
        else:
            self.ops.append("[] 0 d")

    def draw_rect(
        self,
        x: float,
        y: float,
        w: float,
        h: float,
        fill_color: Optional[Tuple[float, float, float]] = None,
        stroke_color: Optional[Tuple[float, float, float]] = None,
        stroke_width: float = 1.0,
    ) -> None:
        """Draw rectangle with optional fill and stroke."""
        self.ops.append("q")
        if fill_color is not None:
            self.set_fill_color(*fill_color)
        if stroke_color is not None:
            self.set_stroke_color(*stroke_color)
            self.set_line_width(stroke_width)

        self.ops.append(f"{x:.2f} {y:.2f} {w:.2f} {h:.2f} re")
        if fill_color is not None and stroke_color is not None:
            self.ops.append("B")
        elif fill_color is not None:
            self.ops.append("f")
        elif stroke_color is not None:
            self.ops.append("S")
        self.ops.append("Q")

    def draw_line(
        self,
        x1: float,
        y1: float,
        x2: float,
        y2: float,
        stroke_color: Tuple[float, float, float] = (0.0, 0.0, 0.0),
        stroke_width: float = 1.0,
        dash: Optional[List[float]] = None,
    ) -> None:
        """Draw line segment."""
        self.ops.append("q")
        self.set_stroke_color(*stroke_color)
        self.set_line_width(stroke_width)
        if dash:
            self.set_dash(dash)
        self.ops.append(f"{x1:.2f} {y1:.2f} m {x2:.2f} {y2:.2f} l S")
        self.ops.append("Q")

    def draw_polyline(
        self,
        points: List[Tuple[float, float]],
        stroke_color: Tuple[float, float, float] = (0.0, 0.0, 0.0),
        stroke_width: float = 1.5,
    ) -> None:
        """Draw connected line segments."""
        if len(points) < 2:
            return
        self.ops.append("q")
        self.set_stroke_color(*stroke_color)
        self.set_line_width(stroke_width)
        start_x, start_y = points[0]
        self.ops.append(f"{start_x:.2f} {start_y:.2f} m")
        for px, py in points[1:]:
            self.ops.append(f"{px:.2f} {py:.2f} l")
        self.ops.append("S")
        self.ops.append("Q")

    def draw_polygon(
        self,
        points: List[Tuple[float, float]],
        fill_color: Tuple[float, float, float],
    ) -> None:
        """Draw filled polygon."""
        if len(points) < 3:
            return
        self.ops.append("q")
        self.set_fill_color(*fill_color)
        start_x, start_y = points[0]
        self.ops.append(f"{start_x:.2f} {start_y:.2f} m")
        for px, py in points[1:]:
            self.ops.append(f"{px:.2f} {py:.2f} l")
        self.ops.append("f")
        self.ops.append("Q")

    def draw_circle(
        self,
        cx: float,
        cy: float,
        r: float,
        fill_color: Optional[Tuple[float, float, float]] = None,
        stroke_color: Optional[Tuple[float, float, float]] = None,
        stroke_width: float = 1.0,
    ) -> None:
        """Approximate circle using 4 Bezier curves."""
        self.ops.append("q")
        if fill_color:
            self.set_fill_color(*fill_color)
        if stroke_color:
            self.set_stroke_color(*stroke_color)
            self.set_line_width(stroke_width)

        k = 0.5522847498 * r
        self.ops.append(f"{cx + r:.2f} {cy:.2f} m")
        self.ops.append(f"{cx + r:.2f} {cy + k:.2f} {cx + k:.2f} {cy + r:.2f} {cx:.2f} {cy + r:.2f} c")
        self.ops.append(f"{cx - k:.2f} {cy + r:.2f} {cx - r:.2f} {cy + k:.2f} {cx - r:.2f} {cy:.2f} c")
        self.ops.append(f"{cx - r:.2f} {cy - k:.2f} {cx - k:.2f} {cy - r:.2f} {cx:.2f} {cy - r:.2f} c")
        self.ops.append(f"{cx + k:.2f} {cy - r:.2f} {cx + r:.2f} {cy - k:.2f} {cx + r:.2f} {cy:.2f} c")

        if fill_color and stroke_color:
            self.ops.append("B")
        elif fill_color:
            self.ops.append("f")
        else:
            self.ops.append("S")
        self.ops.append("Q")

    def draw_text(
        self,
        text: str,
        x: float,
        y: float,
        font: str = "F1",  # F1: Helvetica, F2: Helvetica-Bold, F3: Helvetica-Oblique
        size: float = 10.0,
        color: Tuple[float, float, float] = (0.1, 0.1, 0.1),
        align: str = "left",  # "left", "center", "right"
    ) -> None:
        """Render single line of text with alignment."""
        escaped = _sanitize_pdf_text(text)
        # Approximate character width based on Helvetica metrics
        char_w = size * 0.52
        if "Bold" in font or font == "F2":
            char_w = size * 0.56
        approx_width = len(escaped) * char_w

        x_pos = x
        if align == "center":
            x_pos = x - (approx_width / 2.0)
        elif align == "right":
            x_pos = x - approx_width

        self.ops.append("BT")
        self.ops.append(f"/{font} {size:.2f} Tf")
        self.set_fill_color(*color)
        self.ops.append(f"1 0 0 1 {x_pos:.2f} {y:.2f} Tm")
        self.ops.append(f"({escaped}) Tj")
        self.ops.append("ET")

    def get_stream_bytes(self) -> bytes:
        """Compile page operations into PDF stream bytes."""
        stream_content = "\n".join(self.ops) + "\n"
        return stream_content.encode("latin1")


class PDFDocument:
    """PDF 1.4 Document builder."""

    def __init__(self, page_width: float = 595.28, page_height: float = 841.89):
        self.page_width = page_width
        self.page_height = page_height
        self.pages: List[PDFCanvas] = []

    def new_page(self) -> PDFCanvas:
        canvas = PDFCanvas(self.page_width, self.page_height)
        self.pages.append(canvas)
        return canvas

    def build_pdf_bytes(self) -> bytes:
        """Compile document into standard PDF 1.4 binary format."""
        objects: List[bytes] = []
        obj_offsets: List[int] = []

        # Object 1: Catalog
        # Object 2: Pages tree
        # Object 3..: Font objects
        # Following: Page objects and content streams

        num_pages = len(self.pages)
        # Page object IDs: 6 + 2*i
        # Content object IDs: 7 + 2*i

        font1 = b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"
        font2 = b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica-Bold >>"
        font3 = b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica-Oblique >>"

        kids = " ".join(f"{6 + 2*i} 0 R" for i in range(num_pages))
        pages_obj = f"<< /Type /Pages /Kids [{kids}] /Count {num_pages} >>".encode("latin1")
        catalog_obj = b"<< /Type /Catalog /Pages 2 0 R >>"

        # Temporary list of all objects (1-indexed)
        all_objs: List[bytes] = [
            b"", # placeholder for index 0
            catalog_obj, # 1
            pages_obj,   # 2
            font1,       # 3
            font2,       # 4
            font3,       # 5
        ]

        # Add page and content objects
        for i, canvas in enumerate(self.pages):
            content_id = 7 + 2 * i
            page_dict = (
                f"<< /Type /Page /Parent 2 0 R "
                f"/MediaBox [0 0 {self.page_width:.2f} {self.page_height:.2f}] "
                f"/Contents {content_id} 0 R "
                f"/Resources << /Font << /F1 3 0 R /F2 4 0 R /F3 5 0 R >> >> >>"
            ).encode("latin1")
            all_objs.append(page_dict)

            stream_data = canvas.get_stream_bytes()
            content_dict = (
                f"<< /Length {len(stream_data)} >>\nstream\n".encode("latin1")
                + stream_data
                + b"endstream"
            )
            all_objs.append(content_dict)

        # Assemble PDF file
        out = bytearray()
        out.extend(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")

        # Object 0 is dummy
        obj_offsets = [0] * len(all_objs)
        for obj_idx in range(1, len(all_objs)):
            obj_offsets[obj_idx] = len(out)
            out.extend(f"{obj_idx} 0 obj\n".encode("latin1"))
            out.extend(all_objs[obj_idx])
            out.extend(b"\nendobj\n")

        # Cross reference table
        xref_offset = len(out)
        out.extend(f"xref\n0 {len(all_objs)}\n".encode("latin1"))
        out.extend(b"0000000000 65535 f \n")
        for obj_idx in range(1, len(all_objs)):
            out.extend(f"{obj_offsets[obj_idx]:010d} 00000 n \n".encode("latin1"))

        # Trailer
        out.extend(
            f"trailer\n<< /Size {len(all_objs)} /Root 1 0 R >>\n"
            f"startxref\n{xref_offset}\n%%EOF\n".encode("latin1")
        )
        return bytes(out)

    def write_to_file(self, filepath: str) -> None:
        """Write PDF bytes to filesystem."""
        os.makedirs(os.path.dirname(os.path.abspath(filepath)), exist_ok=True)
        data = self.build_pdf_bytes()
        with open(filepath, "wb") as f:
            f.write(data)


def render_vector_chart(
    canvas: PDFCanvas,
    box_x: float,
    box_y: float,
    box_w: float,
    box_h: float,
    trades: List[Dict[str, Any]],
    title: str = "INTRADAY CUMULATIVE NET P&L TRAJECTORY",
) -> None:
    """
    Render clean vector line chart of cumulative net P&L with shaded profit/drawdown fill.
    """
    # Card container
    canvas.draw_rect(
        box_x, box_y, box_w, box_h,
        fill_color=(0.98, 0.985, 0.995),
        stroke_color=(0.82, 0.86, 0.90),
        stroke_width=0.75,
    )

    # Chart header
    canvas.draw_text(title, box_x + 14, box_y + box_h - 18, font="F2", size=9.5, color=(0.15, 0.20, 0.28))

    # Inner plotting region
    pad_left = 60.0
    pad_right = 20.0
    pad_top = 28.0
    pad_bottom = 26.0

    plot_x = box_x + pad_left
    plot_y = box_y + pad_bottom
    plot_w = box_w - pad_left - pad_right
    plot_h = box_h - pad_top - pad_bottom

    # Chart background
    canvas.draw_rect(
        plot_x, plot_y, plot_w, plot_h,
        fill_color=(1.0, 1.0, 1.0),
        stroke_color=(0.88, 0.90, 0.93),
        stroke_width=0.5,
    )

    # Build time-series data
    sorted_trades = sorted(trades, key=lambda t: float(t.get("exit_time") or t.get("timestamp") or 0.0))
    if not sorted_trades:
        canvas.draw_text("No closed trade records available for graph.", plot_x + (plot_w / 2.0), plot_y + (plot_h / 2.0), font="F1", size=10, color=(0.5, 0.5, 0.5), align="center")
        return

    pts: List[Tuple[float, float]] = []
    # Start at session start (e.g., 0 net P&L)
    first_ts = float(sorted_trades[0].get("entry_time") or sorted_trades[0].get("exit_time") or time.time()) - 300.0
    cum_net = 0.0
    pts.append((first_ts, 0.0))

    for t in sorted_trades:
        ts = float(t.get("exit_time") or t.get("timestamp") or time.time())
        cum_net += float(t.get("net_pnl", 0.0))
        pts.append((ts, cum_net))

    t_min = pts[0][0]
    t_max = pts[-1][0]
    if t_max <= t_min:
        t_max = t_min + 3600.0

    y_vals = [p[1] for p in pts]
    min_y = min(y_vals)
    max_y = max(y_vals)

    # Include 0 in range
    min_y = min(min_y, 0.0)
    max_y = max(max_y, 0.0)

    # Add 10% breathing room
    y_span = max_y - min_y
    if y_span < 100.0:
        y_span = 500.0
        max_y += 250.0
        min_y -= 250.0
    else:
        pad = y_span * 0.12
        max_y += pad
        min_y -= pad
        y_span = max_y - min_y

    def to_screen(t_val: float, y_val: float) -> Tuple[float, float]:
        norm_x = (t_val - t_min) / (t_max - t_min)
        norm_y = (y_val - min_y) / y_span
        sx = plot_x + (norm_x * plot_w)
        sy = plot_y + (norm_y * plot_h)
        return sx, sy

    # Draw horizontal grid lines & Y-axis labels
    grid_steps = 4
    for i in range(grid_steps + 1):
        gy_val = min_y + (i / grid_steps) * y_span
        _, sy = to_screen(t_min, gy_val)
        is_zero = abs(gy_val) < (y_span * 0.03)

        if is_zero:
            # Prominent dashed zero line
            canvas.draw_line(plot_x, sy, plot_x + plot_w, sy, stroke_color=(0.55, 0.60, 0.68), stroke_width=0.8, dash=[3, 3])
        else:
            canvas.draw_line(plot_x, sy, plot_x + plot_w, sy, stroke_color=(0.92, 0.93, 0.95), stroke_width=0.5)

        # Label text
        label_str = f"Rs.{gy_val:+,.0f}" if abs(gy_val) >= 1.0 else "Rs.0"
        canvas.draw_text(label_str, plot_x - 6, sy - 3, font="F1", size=7.0, color=(0.45, 0.50, 0.56), align="right")

    # Time X-axis labels
    time_steps = 4
    for i in range(time_steps + 1):
        gt_val = t_min + (i / time_steps) * (t_max - t_min)
        sx, _ = to_screen(gt_val, min_y)
        canvas.draw_line(sx, plot_y, sx, plot_y + plot_h, stroke_color=(0.94, 0.95, 0.96), stroke_width=0.5)
        dt_str = datetime.fromtimestamp(gt_val).strftime("%H:%M")
        canvas.draw_text(dt_str, sx, plot_y - 12, font="F1", size=7.0, color=(0.45, 0.50, 0.56), align="center")

    # Screen coordinates for data points
    screen_pts = [to_screen(t_val, y_val) for t_val, y_val in pts]
    _, zero_sy = to_screen(t_min, 0.0)

    # Shaded Area Fill under Curve down to zero
    fill_poly = [(screen_pts[0][0], zero_sy)] + screen_pts + [(screen_pts[-1][0], zero_sy)]
    area_color = (0.90, 0.96, 0.92) if cum_net >= 0 else (0.97, 0.91, 0.91)
    canvas.draw_polygon(fill_poly, fill_color=area_color)

    # Draw cumulative trend curve
    line_color = (0.10, 0.62, 0.35) if cum_net >= 0 else (0.85, 0.22, 0.22)
    canvas.draw_polyline(screen_pts, stroke_color=line_color, stroke_width=1.6)

    # Draw trade dots
    for sx, sy in screen_pts[1:]:
        canvas.draw_circle(sx, sy, 2.2, fill_color=(1.0, 1.0, 1.0), stroke_color=line_color, stroke_width=1.2)

    # Final P&L badge at end of curve
    last_sx, last_sy = screen_pts[-1]
    badge_txt = f"Rs.{cum_net:+,.1f}"
    badge_bg = (0.10, 0.62, 0.35) if cum_net >= 0 else (0.85, 0.22, 0.22)
    badge_w = 48.0
    badge_h = 13.0
    bx = min(plot_x + plot_w - badge_w, max(plot_x, last_sx - (badge_w / 2.0)))
    by = min(plot_y + plot_h - badge_h, max(plot_y, last_sy + 5.0))
    canvas.draw_rect(bx, by, badge_w, badge_h, fill_color=badge_bg)
    canvas.draw_text(badge_txt, bx + (badge_w / 2.0), by + 3.2, font="F2", size=7.5, color=(1.0, 1.0, 1.0), align="center")


def generate_daily_pdf_report(
    session_date: str,
    trades: List[Dict[str, Any]],
    output_path: Optional[str] = None,
) -> str:
    """
    Generate professional vector PDF report from session trades.
    Saves to recordings/{session_date}/report_{session_date}.pdf by default.
    """
    if not output_path:
        output_path = f"recordings/{session_date}/report_{session_date}.pdf"

    closed_trades = [t for t in trades if t.get("status") == "CLOSED" or t.get("event") == "EXIT"]

    # Compute key metrics
    tot_gross = sum(float(t.get("gross_pnl", 0.0)) for t in closed_trades)
    tot_brkr = sum(float(t.get("brokerage", 0.0)) for t in closed_trades)
    tot_net = sum(float(t.get("net_pnl", 0.0)) for t in closed_trades)
    wins = sum(1 for t in closed_trades if float(t.get("net_pnl", 0.0)) > 0)
    losses = len(closed_trades) - wins
    win_rate = (wins / len(closed_trades) * 100.0) if closed_trades else 0.0

    pos_gains = sum(float(t.get("net_pnl", 0.0)) for t in closed_trades if float(t.get("net_pnl", 0.0)) > 0)
    neg_losses = sum(abs(float(t.get("net_pnl", 0.0))) for t in closed_trades if float(t.get("net_pnl", 0.0)) < 0)
    profit_factor = (pos_gains / neg_losses) if neg_losses > 0 else (99.9 if pos_gains > 0 else 0.0)
    avg_trade = (tot_net / len(closed_trades)) if closed_trades else 0.0

    doc = PDFDocument(page_width=595.28, page_height=841.89)  # A4
    page1 = doc.new_page()

    # Layout constants
    m_left = 36.0
    m_top = 805.0
    content_w = doc.page_width - (m_left * 2.0)  # 523.28 pt

    # =========================================================================
    # 1. HEADER BANNER
    # =========================================================================
    banner_h = 58.0
    banner_y = m_top - banner_h
    page1.draw_rect(
        m_left, banner_y, content_w, banner_h,
        fill_color=(0.07, 0.12, 0.22),
        stroke_color=(0.15, 0.22, 0.35),
        stroke_width=1.0,
    )
    # Title
    page1.draw_text("QUANTITATIVE MOMENTUM STRATEGY", m_left + 16, banner_y + 35, font="F2", size=13.0, color=(1.0, 1.0, 1.0))
    page1.draw_text("Automated Multi-Track Execution & P&L Performance Report", m_left + 16, banner_y + 22, font="F1", size=8.5, color=(0.75, 0.82, 0.90))
    page1.draw_text(f"Session: {session_date} | Date: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}", m_left + 16, banner_y + 10, font="F3", size=7.5, color=(0.58, 0.65, 0.75))

    # Header Right Badge
    badge_bg = (0.13, 0.65, 0.35) if tot_net >= 0 else (0.85, 0.20, 0.20)
    page1.draw_rect(m_left + content_w - 110, banner_y + 14, 96, 28, fill_color=badge_bg)
    status_label = "PROFITABLE" if tot_net >= 0 else "DRAWDOWN"
    page1.draw_text(status_label, m_left + content_w - 62, banner_y + 29, font="F2", size=8.0, color=(1.0, 1.0, 1.0), align="center")
    page1.draw_text(f"Rs.{tot_net:+,.0f}", m_left + content_w - 62, banner_y + 18, font="F2", size=8.5, color=(1.0, 1.0, 1.0), align="center")

    # =========================================================================
    # 2. EXECUTIVE KPI CARDS (4-Columns)
    # =========================================================================
    cards_y = banner_y - 62.0
    cards_h = 52.0
    gap = 8.0
    col_w = (content_w - (gap * 3.0)) / 4.0

    cards_data = [
        {
            "title": "NET REALIZED P&L",
            "val": f"Rs.{tot_net:+,.2f}",
            "sub": f"Gross: Rs.{tot_gross:+,.0f}",
            "color": (0.10, 0.62, 0.35) if tot_net >= 0 else (0.85, 0.22, 0.22),
        },
        {
            "title": "TOTAL BROKERAGE",
            "val": f"Rs.{tot_brkr:,.2f}",
            "sub": f"Friction: {abs(tot_brkr / (tot_gross if tot_gross != 0 else 1.0)) * 100.0:.1f}%",
            "color": (0.20, 0.25, 0.32),
        },
        {
            "title": "WIN RATE (W / L)",
            "val": f"{win_rate:.1f}%",
            "sub": f"{wins} Wins / {losses} Losses",
            "color": (0.10, 0.50, 0.85),
        },
        {
            "title": "PROFIT FACTOR",
            "val": f"{profit_factor:.2f}",
            "sub": f"Avg Trade: Rs.{avg_trade:+,.0f}",
            "color": (0.45, 0.25, 0.70),
        },
    ]

    for i, c in enumerate(cards_data):
        cx = m_left + (i * (col_w + gap))
        page1.draw_rect(
            cx, cards_y, col_w, cards_h,
            fill_color=(0.97, 0.98, 0.99),
            stroke_color=(0.85, 0.88, 0.92),
            stroke_width=0.75,
        )
        page1.draw_text(c["title"], cx + 10, cards_y + cards_h - 14, font="F2", size=7.0, color=(0.45, 0.50, 0.58))
        page1.draw_text(c["val"], cx + 10, cards_y + cards_h - 30, font="F2", size=11.5, color=c["color"])
        page1.draw_text(c["sub"], cx + 10, cards_y + cards_h - 43, font="F1", size=7.0, color=(0.40, 0.45, 0.50))

    # =========================================================================
    # 3. VECTOR INTRADAY P&L LINE CHART
    # =========================================================================
    chart_y = cards_y - 156.0
    chart_h = 146.0
    render_vector_chart(page1, m_left, chart_y, content_w, chart_h, closed_trades)

    # =========================================================================
    # 4. EXECUTION MODE BREAKDOWN TABLE
    # =========================================================================
    mode_tbl_y = chart_y - 100.0
    page1.draw_text("1. PERFORMANCE BY EXECUTION MODE", m_left, mode_tbl_y + 88, font="F2", size=9.5, color=(0.10, 0.15, 0.25))

    # Header
    m_hdrs = [("MODE", 75.0), ("TRADES (W/L)", 95.0), ("WIN RATE", 70.0), ("GROSS P&L", 85.0), ("BROKERAGE", 70.0), ("NET P&L", 80.0), ("PF", 48.0)]
    th_y = mode_tbl_y + 68
    page1.draw_rect(m_left, th_y, content_w, 16.0, fill_color=(0.15, 0.22, 0.32))
    cur_x = m_left + 8
    for hname, hw in m_hdrs:
        page1.draw_text(hname, cur_x, th_y + 4.5, font="F2", size=7.0, color=(1.0, 1.0, 1.0))
        cur_x += hw

    # Group by mode
    by_mode: Dict[str, List[Dict[str, Any]]] = {}
    for t in closed_trades:
        m = str(t.get("mode", "paper")).upper()
        by_mode.setdefault(m, []).append(t)

    row_y = th_y - 14.0
    for idx, (m_name, m_list) in enumerate(sorted(by_mode.items())):
        bg = (0.98, 0.985, 0.995) if idx % 2 == 0 else (1.0, 1.0, 1.0)
        page1.draw_rect(m_left, row_y, content_w, 14.0, fill_color=bg, stroke_color=(0.90, 0.92, 0.94), stroke_width=0.4)

        m_w = sum(1 for t in m_list if float(t.get("net_pnl", 0.0)) > 0)
        m_l = len(m_list) - m_w
        m_wr = (m_w / len(m_list) * 100.0) if m_list else 0.0
        m_gross = sum(float(t.get("gross_pnl", 0.0)) for t in m_list)
        m_brkr = sum(float(t.get("brokerage", 0.0)) for t in m_list)
        m_net = sum(float(t.get("net_pnl", 0.0)) for t in m_list)
        m_pos = sum(float(t.get("net_pnl", 0.0)) for t in m_list if float(t.get("net_pnl", 0.0)) > 0)
        m_neg = sum(abs(float(t.get("net_pnl", 0.0))) for t in m_list if float(t.get("net_pnl", 0.0)) < 0)
        m_pf = (m_pos / m_neg) if m_neg > 0 else (99.9 if m_pos > 0 else 0.0)

        cur_x = m_left + 8
        page1.draw_text(m_name, cur_x, row_y + 3.5, font="F2", size=7.0, color=(0.1, 0.1, 0.1))
        cur_x += 75.0
        page1.draw_text(f"{len(m_list)} ({m_w}W / {m_l}L)", cur_x, row_y + 3.5, font="F1", size=7.0, color=(0.2, 0.2, 0.2))
        cur_x += 95.0
        page1.draw_text(f"{m_wr:.1f}%", cur_x, row_y + 3.5, font="F1", size=7.0, color=(0.2, 0.2, 0.2))
        cur_x += 70.0
        page1.draw_text(f"Rs.{m_gross:+,.2f}", cur_x, row_y + 3.5, font="F1", size=7.0, color=(0.2, 0.2, 0.2))
        cur_x += 85.0
        page1.draw_text(f"Rs.{m_brkr:,.0f}", cur_x, row_y + 3.5, font="F1", size=7.0, color=(0.2, 0.2, 0.2))
        cur_x += 70.0
        net_col = (0.10, 0.62, 0.35) if m_net >= 0 else (0.85, 0.22, 0.22)
        page1.draw_text(f"Rs.{m_net:+,.2f}", cur_x, row_y + 3.5, font="F2", size=7.0, color=net_col)
        cur_x += 80.0
        page1.draw_text(f"{m_pf:.2f}", cur_x, row_y + 3.5, font="F1", size=7.0, color=(0.2, 0.2, 0.2))

        row_y -= 14.0

    # =========================================================================
    # 5. SUB-STRATEGY TRACK BREAKDOWN TABLE
    # =========================================================================
    trk_tbl_y = row_y - 20.0
    page1.draw_text("2. PERFORMANCE BY SUB-STRATEGY TRACK", m_left, trk_tbl_y + 10, font="F2", size=9.5, color=(0.10, 0.15, 0.25))

    th2_y = trk_tbl_y - 8.0
    page1.draw_rect(m_left, th2_y, content_w, 16.0, fill_color=(0.15, 0.22, 0.32))
    t_hdrs = [("TRACK", 100.0), ("TRADES (W/L)", 80.0), ("WIN RATE", 65.0), ("GROSS P&L", 80.0), ("BROKERAGE", 65.0), ("NET P&L", 85.0), ("PF", 48.0)]
    cur_x = m_left + 8
    for hname, hw in t_hdrs:
        page1.draw_text(hname, cur_x, th2_y + 4.5, font="F2", size=7.0, color=(1.0, 1.0, 1.0))
        cur_x += hw

    by_track: Dict[str, List[Dict[str, Any]]] = {}
    for t in closed_trades:
        trk = str(t.get("track", "ATM"))
        by_track.setdefault(trk, []).append(t)

    sorted_tracks = sorted(
        by_track.items(),
        key=lambda x: sum(float(t.get("net_pnl", 0.0)) for t in x[1]),
        reverse=True,
    )

    row2_y = th2_y - 14.0
    # Show up to 6 tracks on Page 1
    for idx, (t_name, t_list) in enumerate(sorted_tracks[:6]):
        bg = (0.98, 0.985, 0.995) if idx % 2 == 0 else (1.0, 1.0, 1.0)
        page1.draw_rect(m_left, row2_y, content_w, 14.0, fill_color=bg, stroke_color=(0.90, 0.92, 0.94), stroke_width=0.4)

        t_w = sum(1 for t in t_list if float(t.get("net_pnl", 0.0)) > 0)
        t_l = len(t_list) - t_w
        t_wr = (t_w / len(t_list) * 100.0) if t_list else 0.0
        t_gross = sum(float(t.get("gross_pnl", 0.0)) for t in t_list)
        t_brkr = sum(float(t.get("brokerage", 0.0)) for t in t_list)
        t_net = sum(float(t.get("net_pnl", 0.0)) for t in t_list)
        t_pos = sum(float(t.get("net_pnl", 0.0)) for t in t_list if float(t.get("net_pnl", 0.0)) > 0)
        t_neg = sum(abs(float(t.get("net_pnl", 0.0))) for t in t_list if float(t.get("net_pnl", 0.0)) < 0)
        t_pf = (t_pos / t_neg) if t_neg > 0 else (99.9 if t_pos > 0 else 0.0)

        cur_x = m_left + 8
        page1.draw_text(t_name, cur_x, row2_y + 3.5, font="F2", size=7.0, color=(0.1, 0.1, 0.1))
        cur_x += 100.0
        page1.draw_text(f"{len(t_list)} ({t_w}W / {t_l}L)", cur_x, row2_y + 3.5, font="F1", size=7.0, color=(0.2, 0.2, 0.2))
        cur_x += 80.0
        page1.draw_text(f"{t_wr:.1f}%", cur_x, row2_y + 3.5, font="F1", size=7.0, color=(0.2, 0.2, 0.2))
        cur_x += 65.0
        page1.draw_text(f"Rs.{t_gross:+,.2f}", cur_x, row2_y + 3.5, font="F1", size=7.0, color=(0.2, 0.2, 0.2))
        cur_x += 80.0
        page1.draw_text(f"Rs.{t_brkr:,.0f}", cur_x, row2_y + 3.5, font="F1", size=7.0, color=(0.2, 0.2, 0.2))
        cur_x += 65.0
        net_col = (0.10, 0.62, 0.35) if t_net >= 0 else (0.85, 0.22, 0.22)
        page1.draw_text(f"Rs.{t_net:+,.2f}", cur_x, row2_y + 3.5, font="F2", size=7.0, color=net_col)
        cur_x += 85.0
        page1.draw_text(f"{t_pf:.2f}", cur_x, row2_y + 3.5, font="F1", size=7.0, color=(0.2, 0.2, 0.2))

        row2_y -= 14.0

    # Page 1 Footer
    page1.draw_line(m_left, 32.0, m_left + content_w, 32.0, stroke_color=(0.85, 0.88, 0.90), stroke_width=0.5)
    page1.draw_text("CONFIDENTIAL - AUTOMATED TRADING SYSTEM PERFORMANCE AUDIT", m_left, 20.0, font="F1", size=6.5, color=(0.5, 0.55, 0.6))
    page1.draw_text("Page 1 of 2", m_left + content_w, 20.0, font="F1", size=6.5, color=(0.5, 0.55, 0.6), align="right")

    # =========================================================================
    # PAGE 2: RECENT CLOSED TRADES AUDIT LOG
    # =========================================================================
    page2 = doc.new_page()

    # Page 2 Header
    p2_hdr_y = m_top - 36.0
    page2.draw_rect(m_left, p2_hdr_y, content_w, 36.0, fill_color=(0.07, 0.12, 0.22))
    page2.draw_text("SESSION TRADE EXECUTION JOURNAL", m_left + 14, p2_hdr_y + 20, font="F2", size=10.5, color=(1.0, 1.0, 1.0))
    page2.draw_text(f"Session {session_date} | Showing last {min(45, len(closed_trades))} executed orders", m_left + 14, p2_hdr_y + 8, font="F1", size=7.5, color=(0.7, 0.78, 0.86))

    # Table Header
    j_hdrs = [
        ("TIME", 42.0),
        ("TRACK", 68.0),
        ("SYMBOL", 55.0),
        ("CONTRACT", 80.0),
        ("DIR", 32.0),
        ("QTY", 30.0),
        ("ENTRY", 45.0),
        ("EXIT", 45.0),
        ("NET P&L", 65.0),
        ("EXIT REASON", 60.0),
    ]

    j_th_y = p2_hdr_y - 20.0
    page2.draw_rect(m_left, j_th_y, content_w, 16.0, fill_color=(0.15, 0.22, 0.32))
    cur_x = m_left + 6
    for hname, hw in j_hdrs:
        page2.draw_text(hname, cur_x, j_th_y + 4.5, font="F2", size=6.5, color=(1.0, 1.0, 1.0))
        cur_x += hw

    # Populate rows (up to 45 most recent trades)
    j_row_y = j_th_y - 13.0
    sorted_recent = sorted(closed_trades, key=lambda t: float(t.get("exit_time") or t.get("timestamp") or 0.0), reverse=True)

    for idx, t in enumerate(sorted_recent[:42]):
        bg = (0.98, 0.985, 0.995) if idx % 2 == 0 else (1.0, 1.0, 1.0)
        page2.draw_rect(m_left, j_row_y, content_w, 13.0, fill_color=bg, stroke_color=(0.92, 0.93, 0.95), stroke_width=0.3)

        ts = float(t.get("exit_time") or t.get("timestamp") or time.time())
        t_time = datetime.fromtimestamp(ts).strftime("%H:%M:%S")
        trk = str(t.get("track", "ATM"))[:11]
        sym = str(t.get("symbol", ""))[:8]
        cntr = str(t.get("contract_symbol", t.get("contract_key", "")))[-12:]
        direction = str(t.get("direction", "BULL"))[:4]
        qty = int(t.get("quantity") or 1)
        ep = float(t.get("entry_price") or 0.0)
        xp = float(t.get("exit_price") or 0.0)
        net_p = float(t.get("net_pnl", 0.0))
        reason = str(t.get("exit_reason", ""))[:12]

        cur_x = m_left + 6
        page2.draw_text(t_time, cur_x, j_row_y + 3.0, font="F1", size=6.2, color=(0.3, 0.3, 0.3))
        cur_x += 42.0
        page2.draw_text(trk, cur_x, j_row_y + 3.0, font="F2", size=6.2, color=(0.1, 0.1, 0.1))
        cur_x += 68.0
        page2.draw_text(sym, cur_x, j_row_y + 3.0, font="F2", size=6.2, color=(0.1, 0.1, 0.1))
        cur_x += 55.0
        page2.draw_text(cntr, cur_x, j_row_y + 3.0, font="F1", size=6.0, color=(0.25, 0.25, 0.25))
        cur_x += 80.0
        d_col = (0.1, 0.6, 0.3) if direction == "BULL" else (0.8, 0.2, 0.2)
        page2.draw_text(direction, cur_x, j_row_y + 3.0, font="F2", size=6.2, color=d_col)
        cur_x += 32.0
        page2.draw_text(str(qty), cur_x, j_row_y + 3.0, font="F1", size=6.2, color=(0.2, 0.2, 0.2))
        cur_x += 30.0
        page2.draw_text(f"{ep:.2f}", cur_x, j_row_y + 3.0, font="F1", size=6.2, color=(0.2, 0.2, 0.2))
        cur_x += 45.0
        page2.draw_text(f"{xp:.2f}", cur_x, j_row_y + 3.0, font="F1", size=6.2, color=(0.2, 0.2, 0.2))
        cur_x += 45.0
        p_col = (0.10, 0.62, 0.35) if net_p >= 0 else (0.85, 0.22, 0.22)
        page2.draw_text(f"Rs.{net_p:+,.1f}", cur_x, j_row_y + 3.0, font="F2", size=6.2, color=p_col)
        cur_x += 65.0
        page2.draw_text(reason, cur_x, j_row_y + 3.0, font="F1", size=6.0, color=(0.35, 0.35, 0.35))

        j_row_y -= 13.0

    # Page 2 Footer
    page2.draw_line(m_left, 32.0, m_left + content_w, 32.0, stroke_color=(0.85, 0.88, 0.90), stroke_width=0.5)
    page2.draw_text("CONFIDENTIAL - AUTOMATED TRADING SYSTEM PERFORMANCE AUDIT", m_left, 20.0, font="F1", size=6.5, color=(0.5, 0.55, 0.6))
    page2.draw_text("Page 2 of 2", m_left + content_w, 20.0, font="F1", size=6.5, color=(0.5, 0.55, 0.6), align="right")

    doc.write_to_file(output_path)
    return output_path

