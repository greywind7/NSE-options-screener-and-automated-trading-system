"""
Regression Tests for System Audit 2.0 Fixes (CRIT-01..05, HIGH-01..07, MED-*).
All tests run completely isolated using inproc:// ZMQ sockets and temporary directories.
"""
from datetime import datetime
import json
import os
import shutil
import tempfile
import time
import unittest
import zmq

from common.interfaces import OrderRequest, OrderResult
from common.tracks.adaptive_regime import AdaptiveRegimeTrack
from common.tracks.trend_pullback_v2 import TrendPullbackV2Track
from core.common.ipc_schemas import ActionableOpportunity
from core.common.time_utils import IST
from core.quotes.quotes_broker import QuotesBroker, QuotesBrokerClient
from execution.superstructures.paper import PaperSuperstructure
from web.candle_service import CandleService
from web.historical_service import HistoricalService
from web.live_service import LiveService
from web.simulation_service import SimulationService


class TestAudit20Fixes(unittest.TestCase):
    def setUp(self):
        self.test_dir = tempfile.mkdtemp(prefix="test_audit20_")
        self.ctx = zmq.Context()

    def tearDown(self):
        self.ctx.destroy(linger=0)
        if os.path.exists(self.test_dir):
            shutil.rmtree(self.test_dir)

    def test_quotes_broker_zero_fabrication_and_unsub_isolation(self):
        broker = QuotesBroker(
            feed_ipc="inproc://audit_feed",
            router_ipc="inproc://audit_router",
            stream_ipc="inproc://audit_stream",
            heartbeat_ipc="inproc://audit_hb",
            ctx=self.ctx,
        )
        from core.common.symbol_utils import normalize_instrument_token
        try:
            # Missing bid/ask must remain 0.0 (never fallback to ltp)
            norm_tok = normalize_instrument_token("NSE_FO|OPT1")
            broker.on_tick({"token": "NSE_FO|OPT1", "symbol": "OPT1", "ltp": 55.0})
            snap = broker._lvc[norm_tok]
            self.assertEqual(snap["bid"], 0.0)
            self.assertEqual(snap["ask"], 0.0)
            self.assertEqual(snap["ltp"], 55.0)

            # Unsubscribe with wrong client_id must NOT pop another client's subscription
            client = QuotesBrokerClient(router_ipc="inproc://audit_router", ctx=self.ctx)
            try:
                from core.common.ipc_schemas import pack_message, unpack_message

                def roundtrip(payload: dict):
                    client.socket.send_multipart([b"", pack_message(payload)])
                    broker._handle_request()
                    frames = client.socket.recv_multipart()
                    return unpack_message(frames[1])

                roundtrip({"action": "SUBSCRIBE", "tokens": [norm_tok], "client_id": "paper_1"})
                self.assertEqual(broker.get_ref_count(norm_tok), 1)

                roundtrip({"action": "UNSUBSCRIBE", "tokens": [norm_tok], "client_id": "unknown_client"})
                self.assertEqual(broker.get_ref_count(norm_tok), 1)

                roundtrip({"action": "UNSUBSCRIBE", "tokens": [norm_tok], "client_id": "paper_1"})
                self.assertEqual(broker.get_ref_count(norm_tok), 0)
            finally:
                client.close()
        finally:
            broker.stop()

    def test_tracks_do_not_mutate_instance_target_or_sl_across_opps(self):
        class DummyCtx:
            mode = "paper_delayed"
            def is_frozen(self): return False
            def subscribe_quotes(self, toks): pass
            def unsubscribe_quotes(self, toks): pass
            def submit_order(self, req): pass
            def record_trade_event(self, ev, data): pass

        track = AdaptiveRegimeTrack(context=DummyCtx())
        base_t, base_s = track.target_pct, track.sl_pct

        dt_am = datetime(2026, 11, 20, 9, 45, 0, tzinfo=IST)
        dt_mid = datetime(2026, 11, 20, 11, 30, 0, tzinfo=IST)
        opp_am = ActionableOpportunity(
            opp_id="O1", alert_id="A1", symbol="INFY", direction="BULL",
            archetype="FLAG", trigger_price=1500.0, timestamp=dt_am.timestamp(),
            strikes={
                "ATM": {"token": "NSE_FO|AM", "strike": 1500.0, "option_type": "CE", "ask": 30.0, "bid": 29.8, "lot_size": 100},
                "ITM1": {"token": "NSE_FO|MID", "strike": 1480.0, "option_type": "CE", "ask": 40.0, "bid": 39.8, "lot_size": 100},
            },
        )
        opp_mid = ActionableOpportunity(
            opp_id="O2", alert_id="A2", symbol="TCS", direction="BULL",
            archetype="FLAG", trigger_price=3500.0, timestamp=dt_mid.timestamp(),
            strikes={
                "ATM": {"token": "NSE_FO|AM2", "strike": 3500.0, "option_type": "CE", "ask": 30.0, "bid": 29.8, "lot_size": 100},
                "ITM1": {"token": "NSE_FO|MID2", "strike": 3450.0, "option_type": "CE", "ask": 40.0, "bid": 39.8, "lot_size": 100},
            },
        )

        tr_am = track.on_opportunity(opp_am)
        self.assertIsNotNone(tr_am)
        self.assertEqual(track.target_pct, base_t)
        self.assertEqual(track.sl_pct, base_s)
        self.assertAlmostEqual(tr_am.metadata["target_pct"], 0.18)

        tr_mid = track.on_opportunity(opp_mid)
        self.assertIsNotNone(tr_mid)
        self.assertAlmostEqual(tr_mid.metadata["target_pct"], 0.08)
        self.assertAlmostEqual(tr_mid.metadata["sl_pct"], 0.05)

        # Filling morning trade asynchronously after midday trade still uses morning target_pct (0.18)
        track.on_order_result(OrderResult(order_id=f"ORD_{tr_am.trade_id}", status="FILLED", fill_price=32.0, fill_quantity=100, fill_timestamp=dt_am.timestamp() + 1.0))
        self.assertAlmostEqual(tr_am.target_price, 32.0 * 1.18, places=4)
        self.assertAlmostEqual(tr_am.stop_loss, 32.0 * 0.93, places=4)

        tp2 = TrendPullbackV2Track(context=DummyCtx())
        orig_tp2_sl = tp2.sl_pct
        opp_wide = ActionableOpportunity(
            opp_id="O3", alert_id="A3", symbol="SBIN", direction="BULL",
            archetype="FLAG", trigger_price=800.0, timestamp=dt_am.timestamp(),
            strikes={"ITM1": {"token": "NSE_FO|SBIN", "strike": 790.0, "option_type": "CE", "ask": 50.0, "bid": 46.0, "lot_size": 100}},
        )
        tr_wide = tp2.on_opportunity(opp_wide)
        self.assertIsNotNone(tr_wide)
        self.assertEqual(tp2.sl_pct, orig_tp2_sl)
        self.assertGreater(tr_wide.metadata["sl_pct"], 0.10)

    def test_superstructure_freeze_flag_file_detection(self):
        flag_path = os.path.join("logs", ".freeze_paper")
        pre_existed = os.path.exists(flag_path)
        s = PaperSuperstructure(
            track_names=["fast_momentum_ATM"],
            rms_ipc="inproc://dummy_rms_f",
            feed_ipc="inproc://dummy_feed_f",
            hb_ipc="inproc://dummy_hb_f",
            quotes_router_ipc="inproc://dummy_qr_f",
            recordings_dir=self.test_dir,
            ctx=self.ctx,
        )
        try:
            self.assertFalse(s.is_frozen())
            os.makedirs("logs", exist_ok=True)
            with open(flag_path, "w", encoding="utf-8") as f:
                f.write("test\n")
            self.assertTrue(s.is_frozen())
        finally:
            if not pre_existed and os.path.exists(flag_path):
                os.remove(flag_path)
            s.stop()

    def test_sim_hash_includes_track_profile_and_web_totals_are_truthful(self):
        sim_srv = SimulationService(recordings_dir=self.test_dir)
        h_v1 = sim_srv.compute_param_hash("2026-11-19", {"track_profile": "v1"})
        h_v2 = sim_srv.compute_param_hash("2026-11-19", {"track_profile": "v2"})
        self.assertNotEqual(h_v1, h_v2)

        # Immutable entry_price/quantity & overall totals (not divided by n_active)
        from core.common.time_utils import get_market_date_str
        d_str = get_market_date_str()
        sdir = os.path.join(self.test_dir, d_str)
        os.makedirs(sdir, exist_ok=True)
        for tr_name in ("fast_momentum_ATM", "trend_pullback_ATM"):
            jpath = os.path.join(sdir, f"journal_paper_{tr_name}.jsonl")
            with open(jpath, "w", encoding="utf-8") as f:
                f.write(json.dumps({
                    "event": "TRADE_FILLED",
                    "mode": "paper",
                    "data": {
                        "trade_id": f"T_{tr_name}",
                        "track": tr_name,
                        "symbol": "INFY",
                        "token": "NSE_FO|1",
                        "quantity": 100,
                        "entry_price": 50.0,
                        "entry_time": time.time() - 60,
                        "status": "OPEN",
                    },
                }) + "\n")
                f.write(json.dumps({
                    "event": "TRADE_EXIT",
                    "mode": "paper",
                    "data": {
                        "trade_id": f"T_{tr_name}",
                        "track": tr_name,
                        "symbol": "INFY",
                        "token": "NSE_FO|1",
                        "quantity": 999,       # Attempted overwrite (must be ignored)
                        "entry_price": 999.0,  # Attempted overwrite (must be ignored)
                        "exit_price": 0.0,     # Genuine 0.0 exit price (must not become 50.0)
                        "exit_time": time.time() - 10,
                        "realized_pnl": -5000.0,
                        "status": "CLOSED",
                        "metadata": {"brokerage": 60.0},
                    },
                }) + "\n")

        live_srv = LiveService(recordings_dir=self.test_dir)
        overview = live_srv.scan_live_journals(force=True)
        self.assertAlmostEqual(overview["overall"]["net_pnl"], -10120.0, places=1)
        self.assertAlmostEqual(overview["overall"]["avg_track_net_pnl"], -5060.0, places=1)
        detail = live_srv.get_track_detail("paper", "fast_momentum_ATM")
        self.assertEqual(detail["trades"][0]["quantity"], 100)
        self.assertEqual(detail["trades"][0]["entry_price"], 50.0)
        self.assertEqual(detail["trades"][0]["exit_price"], 0.0)

        hist_srv = HistoricalService(recordings_dir=self.test_dir)
        h_over = hist_srv.get_historical_overview(d_str)
        self.assertAlmostEqual(h_over["overall"]["net_pnl"], -10120.0, places=1)
        self.assertAlmostEqual(h_over["overall"]["avg_track_net_pnl"], -5060.0, places=1)

    def test_advisory_lock_exception_safety_and_rejected_unsub(self):
        from core.feed.position_registry import _advisory_file_lock
        lock_target = os.path.join(self.test_dir, "active_positions.json")
        with self.assertRaises(ValueError):
            with _advisory_file_lock(lock_target):
                raise ValueError("caller failure must propagate once without double-yield")

        unsub_calls = []

        class DummyCtx:
            mode = "paper_delayed"
            def is_frozen(self): return False
            def subscribe_quotes(self, toks): pass
            def unsubscribe_quotes(self, toks): unsub_calls.append(list(toks))
            def submit_order(self, req): pass
            def record_trade_event(self, ev, data): pass

        from common.tracks.base_track import BaseTrack
        class DummyTrack(BaseTrack):
            def select_strike(self, opp): return opp.strikes.get("ATM")
            def evaluate_exit_rules(self, trade, ltp, bid, ask, ts): return None

        dt_am = datetime(2026, 11, 20, 9, 45, 0, tzinfo=IST)
        tr_inst = DummyTrack("dummy_ATM", target_pct=0.10, sl_pct=0.05, context=DummyCtx())
        opp = ActionableOpportunity(
            opp_id="O_REJ", alert_id="A_REJ", symbol="SBIN", direction="BULL",
            archetype="FLAG", trigger_price=800.0, timestamp=dt_am.timestamp(),
            strikes={"ATM": {"token": "NSE_FO|SBIN800CE", "strike": 800.0, "option_type": "CE", "ask": 20.0, "bid": 19.8, "lot_size": 100}},
        )
        t_state = tr_inst.on_opportunity(opp)
        self.assertIsNotNone(t_state)
        tr_inst.on_order_result(OrderResult(order_id=f"ORD_{t_state.trade_id}", status="REJECTED", fill_price=0.0, fill_quantity=0, fill_timestamp=dt_am.timestamp() + 1.0))
        self.assertEqual(unsub_calls[-1], ["NSE_FO|SBIN800CE", "SBIN", "NSE:SBIN"])

    def test_simulation_cache_preserves_options_df_and_candle_vol_guard(self):
        import polars as pl
        from core.candles.candle_manager import CandleManager
        from core.candles.shm_layout import unlink_shm
        from simulation.columnar_sim_engine import ColumnarSimEngine

        # 1. Verify SimulationService._ensure_cache keeps cached_options_df intact with >10 equity rows
        s_date = "2026-11-19"
        s_dir = os.path.join(self.test_dir, s_date)
        os.makedirs(s_dir, exist_ok=True)
        base_ts = 1795060800.0
        eq_rows = [{
            "token": "NSE:RELIANCE",
            "symbol": "RELIANCE",
            "ltp": 2500.0 + i,
            "volume": 1000 * (i + 1),
            "exchange_timestamp": base_ts + i,
        } for i in range(15)]
        opt_rows = [{
            "token": "NSE:RELIANCE26NOV2500CE",
            "symbol": "RELIANCE",
            "ltp": 50.0 + i,
            "bid": 49.5 + i,
            "ask": 50.5 + i,
            "bid_qty": 100,
            "ask_qty": 100,
            "volume": 500,
            "exchange_timestamp": base_ts + i * 2.0,
        } for i in range(10)]
        pl.DataFrame(eq_rows).write_parquet(os.path.join(s_dir, "equity_ticks.parquet"))
        pl.DataFrame(opt_rows).write_parquet(os.path.join(s_dir, "option_depth.parquet"))

        sim_srv = SimulationService(recordings_dir=self.test_dir)
        sim_srv._ensure_cache(s_date)
        self.assertIsNotNone(sim_srv.cached_equity_df)
        self.assertIsNotNone(sim_srv.cached_options_df)
        self.assertEqual(len(sim_srv.cached_options_df), 10)

        eng = ColumnarSimEngine(
            data_path=s_dir,
            modes=["paper"],
            cached_equity_df=sim_srv.cached_equity_df,
            cached_options_df=sim_srv.cached_options_df,
            shm_name="tmp_test_sim_cache_fix",
        )
        eng.load_data()
        self.assertIn("NSE:RELIANCE26NOV2500CE", eng.options_by_token)

        # 2. Verify CandleManager does not reset positive cumulative volume on a zero-vol tick
        shm_name = "tmp_test_cm_zerovol"
        unlink_shm(shm_name)
        cm = CandleManager(shm_name=shm_name, enable_zmq=False)
        try:
            sym = cm.index_to_symbol[0]
            cm.on_tick(sym, 100.0, 1000, base_ts)
            cm.on_tick(sym, 101.0, 0, base_ts + 5.0)       # spurious 0-volume tick
            cm.on_tick(sym, 102.0, 1200, base_ts + 10.0)   # should add delta 200, not 1200
            bar_idx = cm.current_bar_idx[0]
            self.assertEqual(int(cm.candles[0, bar_idx]["volume"]), 200)
        finally:
            cm.stop()
            unlink_shm(shm_name)

    def test_quotes_broker_heartbeat_single_thread_two_frames(self):
        import threading
        broker = QuotesBroker(
            feed_ipc="inproc://audit_hb_feed",
            router_ipc="inproc://audit_hb_router",
            stream_ipc="inproc://audit_hb_stream",
            heartbeat_ipc="inproc://audit_hb_pub",
            ctx=self.ctx,
        )
        hb_sub = self.ctx.socket(zmq.SUB)
        hb_sub.setsockopt(zmq.LINGER, 0)
        hb_sub.setsockopt(zmq.RCVTIMEO, 2000)
        hb_sub.setsockopt(zmq.SUBSCRIBE, b"HEARTBEAT")
        hb_sub.connect("inproc://audit_hb_stream")
        t = threading.Thread(target=broker.start, daemon=True)
        try:
            t.start()
            time.sleep(0.05)
            broker._emit_heartbeat(time.time())
            frames = hb_sub.recv_multipart()
            self.assertEqual(len(frames), 2)
            self.assertEqual(frames[0], b"HEARTBEAT")
        finally:
            hb_sub.close(linger=0)
            broker.stop()
            t.join(timeout=1.0)


if __name__ == "__main__":
    unittest.main()
