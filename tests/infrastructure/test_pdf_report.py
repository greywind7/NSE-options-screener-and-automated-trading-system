#!/usr/bin/env python3
"""
tests/infrastructure/test_pdf_report.py
=========================================
Unit tests for Pure-Python Zero-Dependency PDF 1.4 Vector Report Generator.
"""

import os
import sys
import tempfile
import time
import unittest

# Ensure project root is on sys.path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "../..")))

from infrastructure.pnl.pdf_report import (
    PDFCanvas,
    PDFDocument,
    generate_daily_pdf_report,
    render_vector_chart,
)


class TestPDFReportGenerator(unittest.TestCase):
    """Test suite for PDF report generator and vector rendering primitives."""

    def test_pdf_canvas_primitives(self):
        """Verify vector canvas drawing operations."""
        canvas = PDFCanvas(595.28, 841.89)
        canvas.draw_rect(10, 10, 100, 50, fill_color=(0.1, 0.2, 0.3), stroke_color=(0.4, 0.5, 0.6))
        canvas.draw_line(0, 0, 100, 100, stroke_color=(0.1, 0.1, 0.1), dash=[2, 2])
        canvas.draw_polyline([(10, 10), (20, 30), (30, 25)], stroke_color=(0.0, 1.0, 0.0))
        canvas.draw_polygon([(10, 10), (20, 30), (30, 25)], fill_color=(0.9, 0.9, 0.9))
        canvas.draw_circle(50, 50, 15, fill_color=(1, 0, 0), stroke_color=(0, 0, 0))
        canvas.draw_text("Test Title (₹1,500) & Special 'Chars'", 50, 50, font="F2", size=12, align="center")

        data = canvas.get_stream_bytes()
        self.assertGreater(len(data), 50)
        # Verify text escaping replaced ₹ with Rs.
        self.assertIn(b"Rs.1,500", data)

    def test_pdf_document_assembly(self):
        """Verify PDF 1.4 document syntax and structure."""
        doc = PDFDocument(page_width=595.28, page_height=841.89)
        p1 = doc.new_page()
        p1.draw_text("Page 1 Content", 100, 700, font="F1", size=12)
        p2 = doc.new_page()
        p2.draw_text("Page 2 Content", 100, 700, font="F1", size=12)

        pdf_bytes = doc.build_pdf_bytes()
        self.assertTrue(pdf_bytes.startswith(b"%PDF-1.4\n"))
        self.assertIn(b"/Type /Catalog", pdf_bytes)
        self.assertIn(b"/Type /Pages", pdf_bytes)
        self.assertIn(b"/Count 2", pdf_bytes)
        self.assertIn(b"xref\n", pdf_bytes)
        self.assertIn(b"trailer\n", pdf_bytes)
        self.assertTrue(pdf_bytes.endswith(b"%%EOF\n"))

    def test_generate_daily_pdf_report(self):
        """Verify end-to-end PDF report generation with multi-track trades."""
        with tempfile.TemporaryDirectory() as tmpdir:
            pdf_path = os.path.join(tmpdir, "report_20261107.pdf")

            trades = [
                {
                    "trade_id": "T1",
                    "mode": "live",
                    "track": "DYNAMIC_TRIO",
                    "symbol": "RELIANCE",
                    "contract_symbol": "RELIANCE26NOV2600CE",
                    "direction": "BULL",
                    "quantity": 250,
                    "entry_price": 45.0,
                    "exit_price": 52.5,
                    "gross_pnl": 1875.0,
                    "brokerage": 60.0,
                    "net_pnl": 1815.0,
                    "entry_time": time.time() - 3600,
                    "exit_time": time.time() - 3000,
                    "exit_reason": "TARGET_HIT",
                    "status": "CLOSED",
                },
                {
                    "trade_id": "T2",
                    "mode": "paper",
                    "track": "TRAIL_SL_ATM",
                    "symbol": "TCS",
                    "contract_symbol": "TCS26NOV3500PE",
                    "direction": "BEAR",
                    "quantity": 175,
                    "entry_price": 60.0,
                    "exit_price": 55.0,
                    "gross_pnl": -875.0,
                    "brokerage": 60.0,
                    "net_pnl": -935.0,
                    "entry_time": time.time() - 2500,
                    "exit_time": time.time() - 1800,
                    "exit_reason": "STOP_LOSS",
                    "status": "CLOSED",
                },
                {
                    "trade_id": "T3",
                    "mode": "paper_delayed",
                    "track": "TRAIL_SL_ATM",
                    "symbol": "INFY",
                    "contract_symbol": "INFY26NOV1800CE",
                    "direction": "BULL",
                    "quantity": 400,
                    "entry_price": 22.0,
                    "exit_price": 27.5,
                    "gross_pnl": 2200.0,
                    "brokerage": 60.0,
                    "net_pnl": 2140.0,
                    "entry_time": time.time() - 1500,
                    "exit_time": time.time() - 500,
                    "exit_reason": "TRAILING_SL",
                    "status": "CLOSED",
                },
            ]

            out = generate_daily_pdf_report("20261107", trades, output_path=pdf_path)
            self.assertEqual(out, pdf_path)
            self.assertTrue(os.path.exists(pdf_path))
            self.assertGreater(os.path.getsize(pdf_path), 5000)

            with open(pdf_path, "rb") as f:
                content = f.read()
                self.assertTrue(content.startswith(b"%PDF-1.4\n"))
                self.assertIn(b"QUANTITATIVE MOMENTUM STRATEGY", content)
                self.assertIn(b"RELIANCE", content)
                self.assertIn(b"DYNAMIC_TRIO", content)

    def test_generate_daily_pdf_report_empty(self):
        """Verify PDF report handles sessions with 0 trades gracefully."""
        with tempfile.TemporaryDirectory() as tmpdir:
            pdf_path = os.path.join(tmpdir, "empty_report.pdf")
            out = generate_daily_pdf_report("20261107", [], output_path=pdf_path)
            self.assertTrue(os.path.exists(out))
            self.assertGreater(os.path.getsize(out), 3000)


if __name__ == "__main__":
    unittest.main()

