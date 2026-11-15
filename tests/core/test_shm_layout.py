"""
Unit Tests for POSIX Shared Memory Layout & Seqlock.
"""
import time
import unittest
import numpy as np
from core.candles.shm_layout import (
    CANDLE_DTYPE,
    SEQLOCK_DTYPE,
    TOTAL_SHM_SIZE,
    NUM_SYMBOLS,
    NUM_BARS,
    create_or_attach_shm,
    get_shm_views,
    unlink_shm,
)

TEST_SHM_NAME = "test_shm_ohlc_221"


class TestSHMLayout(unittest.TestCase):
    def setUp(self):
        unlink_shm(TEST_SHM_NAME)

    def tearDown(self):
        unlink_shm(TEST_SHM_NAME)

    def test_itemsize_and_alignment(self):
        self.assertEqual(CANDLE_DTYPE.itemsize, 48)
        self.assertEqual(SEQLOCK_DTYPE.itemsize, 16)
        self.assertEqual(TOTAL_SHM_SIZE, (221 * 16) + (221 * 75 * 48))
        self.assertLess(TOTAL_SHM_SIZE, 800_000)  # Fits in 780 KB L2 cache

    def test_shm_creation_and_view_access(self):
        writer_shm = create_or_attach_shm(name=TEST_SHM_NAME, create=True)
        try:
            headers, candles = get_shm_views(writer_shm)
            self.assertEqual(headers.shape, (NUM_SYMBOLS,))
            self.assertEqual(candles.shape, (NUM_SYMBOLS, NUM_BARS))

            # Write test data for symbol 0 bar 0
            headers[0]["sequence_id"] = 1  # Updating
            now_ns = time.time_ns()
            headers[0]["last_update_ns"] = now_ns
            candles[0, 0]["timestamp"] = 1700000000
            candles[0, 0]["open"] = 100.5
            candles[0, 0]["high"] = 105.0
            candles[0, 0]["low"] = 100.0
            candles[0, 0]["close"] = 104.2
            candles[0, 0]["volume"] = 50000
            headers[0]["sequence_id"] = 2  # Stable

            # Attach as lock-free reader
            reader_shm = create_or_attach_shm(name=TEST_SHM_NAME, create=False)
            try:
                r_headers, r_candles = get_shm_views(reader_shm)
                # Verify reader reads identical data
                self.assertEqual(r_headers[0]["sequence_id"], 2)
                self.assertEqual(r_headers[0]["last_update_ns"], now_ns)
                self.assertEqual(r_candles[0, 0]["timestamp"], 1700000000)
                self.assertAlmostEqual(r_candles[0, 0]["open"], 100.5)
                self.assertAlmostEqual(r_candles[0, 0]["high"], 105.0)
                self.assertAlmostEqual(r_candles[0, 0]["low"], 100.0)
                self.assertAlmostEqual(r_candles[0, 0]["close"], 104.2)
                self.assertEqual(r_candles[0, 0]["volume"], 50000)
            finally:
                reader_shm.close()
        finally:
            writer_shm.close()
            writer_shm.unlink()


if __name__ == "__main__":
    unittest.main()

