import json
import os
import shutil
import signal
import tempfile
import unittest
from unittest.mock import patch

from core.candles.candle_manager import CandleManager
from core.candles.shm_layout import (
    SHM_SIGNAL_DETACH,
    SHM_SIGNAL_REATTACH,
    create_or_attach_shm,
    shm_exists,
    unlink_shm,
)
from strategies.momentum.opportunity_pool import OpportunityPool
from strategies.momentum.scoring_engine import ScoringEngine3
from web.health_service import HealthService


class TestShmLifecycleAndHealthConsistency(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp(prefix="test_shm_lifecycle_")
        self.pids_file = os.path.join(self.tmp_dir, "pids.json")
        self.logs_dir = os.path.join(self.tmp_dir, "logs")
        os.makedirs(self.logs_dir, exist_ok=True)
        self.shm_name = f"test_shm_lc_{os.getpid()}"
        unlink_shm(self.shm_name)

    def tearDown(self):
        unlink_shm(self.shm_name)
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def test_candle_manager_restart_signals_detach_recreates_and_signals_reattach(self):
        # 1. Pre-create a stale SHM segment as if left behind by a previous run
        stale = create_or_attach_shm(name=self.shm_name, create=True)
        stale.close()
        self.assertTrue(shm_exists(self.shm_name))

        # Attach a ScoringEngine3 consumer and register an external consumer PID in pids.json
        pool = OpportunityPool(enable_zmq=False, persist_alerts=False)
        engine = ScoringEngine3(shm_name=self.shm_name, pool=pool, enable_zmq=False, persist_alerts=False)
        self.assertFalse(engine._shm_detached)
        self.assertIsNotNone(engine.candles)

        fake_consumer_pid = 424242
        with open(self.pids_file, "w", encoding="utf-8") as f:
            json.dump({
                "scoring_engine": {"pid": fake_consumer_pid, "tier": "STRATEGY"},
                "exec_paper_fast_momentum_ATM": {"pid": 525252, "tier": "EXECUTION"},
            }, f)

        sent_signals = []

        def fake_kill(pid, sig):
            if sig == 0:
                return None  # process alive
            sent_signals.append((pid, sig))
            if sig == SHM_SIGNAL_DETACH:
                engine.request_shm_detach()
                engine._process_shm_signals()
            elif sig == SHM_SIGNAL_REATTACH:
                engine.request_shm_reattach()
                engine._process_shm_signals()

        try:
            with patch("core.candles.shm_layout.os.kill", side_effect=fake_kill):
                cm = CandleManager(
                    shm_name=self.shm_name,
                    enable_zmq=False,
                    pids_file=self.pids_file,
                )
                try:
                    # Must have sent DETACH before removing stale SHM, then REATTACH after fresh SHM creation
                    self.assertEqual(
                        sent_signals,
                        [
                            (fake_consumer_pid, SHM_SIGNAL_DETACH),
                            (fake_consumer_pid, SHM_SIGNAL_REATTACH),
                        ],
                    )
                    # Execution tracks must NEVER be sent SHM SIGUSR signals
                    self.assertFalse(any(pid == 525252 for pid, _ in sent_signals))
                    self.assertFalse(engine._shm_detached)
                    self.assertIsNotNone(engine.candles)
                finally:
                    cm.stop()

            # On CandleManager stop, consumers must receive DETACH and SHM must be unlinked
            self.assertEqual(sent_signals[-1], (fake_consumer_pid, SHM_SIGNAL_DETACH))
            self.assertTrue(engine._shm_detached)
            self.assertIsNone(engine.candles)
            self.assertFalse(shm_exists(self.shm_name))
            # Evaluating while detached returns empty safely
            self.assertEqual(engine.evaluate_universe(), [])
        finally:
            engine.stop()
            pool.stop()

    def test_health_service_marks_shm_inactive_and_clears_orphan_when_candle_manager_dead(self):
        # Create an orphan SHM file while candle_manager is NOT registered / NOT alive
        orphan = create_or_attach_shm(name=self.shm_name, create=True)
        orphan.close()
        self.assertTrue(shm_exists(self.shm_name))

        fake_scoring_pid = 616161
        with open(self.pids_file, "w", encoding="utf-8") as f:
            json.dump({
                "scoring_engine": {"pid": fake_scoring_pid, "tier": "STRATEGY"},
            }, f)

        sent_signals = []

        def fake_kill(pid, sig):
            if pid == fake_scoring_pid:
                if sig == 0:
                    return None
                sent_signals.append((pid, sig))
                return None
            raise OSError("dead")

        health = HealthService(
            logs_dir=self.logs_dir,
            pids_file=self.pids_file,
            shm_name=self.shm_name,
            skip_system_probes=False,
        )

        with patch("core.candles.shm_layout.os.kill", side_effect=fake_kill), \
             patch("web.health_service.is_pid_alive", side_effect=lambda p: p == fake_scoring_pid):
            st = health.get_status()

        self.assertFalse(st["shm"]["active"])
        self.assertEqual(st["shm"]["backend"], "stale_cleared")
        self.assertIn((fake_scoring_pid, SHM_SIGNAL_DETACH), sent_signals)
        self.assertFalse(shm_exists(self.shm_name))

        # When candle_manager IS alive and SHM exists, health reports SHM active
        live_shm = create_or_attach_shm(name=self.shm_name, create=True)
        try:
            fake_cm_pid = 717171
            with open(self.pids_file, "w", encoding="utf-8") as f:
                json.dump({"candle_manager": {"pid": fake_cm_pid, "tier": "CORE"}}, f)

            with patch("web.health_service.is_pid_alive", side_effect=lambda p: p == fake_cm_pid):
                st2 = health.get_status()

            self.assertTrue(st2["shm"]["active"])
            self.assertEqual(st2["shm"]["backend"], "mmap_tmp")
        finally:
            live_shm.close()
            unlink_shm(self.shm_name)
