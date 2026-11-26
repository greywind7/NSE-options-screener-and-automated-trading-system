"""
Unit Tests for Spec 2.0 Multi-Regime Execution Tracks (8 New Tracks)
and Track Profile Configuration Presets (v1, v2, default).
"""
from datetime import datetime
import unittest

from common.interfaces import ExecutionContext
from common.tracks.factory import (
    create_tracks,
    get_enabled_track_names,
    load_track_profile,
)
from common.tracks.structural_convexity import StructuralConvexityTrack
from common.tracks.opening_thrust_convex import OpeningThrustConvexityTrack
from common.tracks.delta_scalp import DeltaScalpTrack
from common.tracks.micro_chandelier import MicroChandelierTrack
from common.tracks.gamma_runner import GammaRunnerTrack
from common.tracks.volatility_expansion import VolatilityExpansionTrack
from common.tracks.adaptive_regime import AdaptiveRegimeTrack
from common.tracks.trend_pullback_v2 import TrendPullbackV2Track
from core.common.ipc_schemas import ActionableOpportunity, OrderRequest
from core.common.time_utils import IST


class DummyExecutionContext(ExecutionContext):
    def __init__(self, mode: str = "paper"):
        self._mode = mode
        self.submitted_orders = []
        self.journal_events = []
        self.subscribed_quotes = []
        self._frozen = False

    @property
    def mode(self) -> str:
        return self._mode

    def is_frozen(self) -> bool:
        return self._frozen

    def submit_order(self, request: OrderRequest) -> None:
        self.submitted_orders.append(request)

    def cancel_order(self, order_id: str) -> None:
        pass

    def record_trade_event(self, event_type: str, data: dict) -> None:
        self.journal_events.append((event_type, data))

    def subscribe_quotes(self, tokens: list) -> None:
        self.subscribed_quotes.extend(tokens)


def _make_dummy_opp(
    symbol="INFY",
    direction="BULL",
    trigger_price=1500.0,
    timestamp=1000.0,
    flag_low=1490.0,
    flag_high=1510.0,
):
    return ActionableOpportunity(
        opp_id="OPP_TEST_1",
        alert_id="ALT_TEST_1",
        symbol=symbol,
        direction=direction,
        archetype="FLAG_BULL_BREAKOUT",
        trigger_price=trigger_price,
        timestamp=timestamp,
        flag_high=flag_high,
        flag_low=flag_low,
        pole_high=1510.0,
        pole_low=1485.0,
        strikes={
            "ATM": {"token": "INFY_ATM", "strike": 1500.0, "ask": 20.0, "bid": 19.8, "lot_size": 300},
            "ITM1": {"token": "INFY_ITM1", "strike": 1480.0, "ask": 35.0, "bid": 34.6, "lot_size": 300},
            "ITM2": {"token": "INFY_ITM2", "strike": 1460.0, "ask": 50.0, "bid": 49.5, "lot_size": 300},
            "OTM1": {"token": "INFY_OTM1", "strike": 1520.0, "ask": 12.0, "bid": 11.8, "lot_size": 300},
            "OTM2": {"token": "INFY_OTM2", "strike": 1540.0, "ask": 6.0, "bid": 5.8, "lot_size": 300},
        },
    )


class TestNewExecutionTracks(unittest.TestCase):
    def setUp(self):
        self.context = DummyExecutionContext()

    def test_structural_convexity_exit_rules(self):
        track = StructuralConvexityTrack(context=self.context)
        opp = _make_dummy_opp(direction="BULL", trigger_price=1500.0, flag_low=1490.0)
        trade = track.on_opportunity(opp)
        self.assertIsNotNone(trade)
        self.assertEqual(trade.entry_price, 20.0)

        # 1. Structural Invalidation: Underlying LTP drops to or below flag_low (1490.0)
        trade.metadata["underlying_ltp"] = 1489.5
        reason = track.evaluate_exit_rules(trade, ltp=18.5, bid=18.4, ask=18.5, ts=1010.0)
        self.assertEqual(reason, "STRUCTURAL_INVALIDATION")

        # Reset underlying LTP to safe value above flag_low
        trade.metadata["underlying_ltp"] = 1505.0

        # 2. Stage 1: Breakeven Free-Roll at +8% gain (bid = 21.60)
        track.on_tick("INFY_ATM", ltp=21.6, bid=21.6, ask=21.7, ts=1020.0)
        self.assertEqual(trade.ratchet_level, 1)
        self.assertAlmostEqual(trade.stop_loss, 20.10, places=2)  # Entry + 0.5% buffer

        # 3. Stage 2: 50% Profit Trail at +20% gain (bid = 24.0, peak profit 20%)
        track.on_tick("INFY_ATM", ltp=24.0, bid=24.0, ask=24.1, ts=1030.0)
        self.assertEqual(trade.ratchet_level, 2)
        # 50% of 20% = +10% lock -> SL should be 20.0 * 1.10 = 22.0
        self.assertAlmostEqual(trade.stop_loss, 22.0, places=2)

        # 4. Hard emergency stop loss (-12% = 17.60)
        reason_sl = track.evaluate_exit_rules(trade, ltp=15.0, bid=17.5, ask=17.6, ts=1040.0)
        self.assertEqual(reason_sl, "EMERGENCY_SL_HIT")

    def test_opening_thrust_convex_window_and_cutoff(self):
        track = OpeningThrustConvexityTrack(context=self.context)

        # Morning timestamp: 09:30 IST on a normal date
        dt_valid = datetime(2026, 11, 20, 9, 30, 0, tzinfo=IST)
        opp_valid = _make_dummy_opp(timestamp=dt_valid.timestamp())
        trade = track.on_opportunity(opp_valid)
        self.assertIsNotNone(trade)

        # Afternoon timestamp: 11:30 IST (must be rejected)
        dt_invalid = datetime(2026, 11, 20, 11, 30, 0, tzinfo=IST)
        opp_invalid = _make_dummy_opp(timestamp=dt_invalid.timestamp())
        rejected = track.on_opportunity(opp_invalid)
        self.assertIsNone(rejected)

        # Cutoff exit at 10:45 IST
        dt_cutoff = datetime(2026, 11, 20, 10, 46, 0, tzinfo=IST)
        reason = track.evaluate_exit_rules(trade, ltp=20.0, bid=20.0, ask=20.1, ts=dt_cutoff.timestamp())
        self.assertEqual(reason, "MORNING_SESSION_CUTOFF")

    def test_delta_scalp_itm2_and_timeout(self):
        track = DeltaScalpTrack(context=self.context)
        opp = _make_dummy_opp()
        trade = track.on_opportunity(opp)
        self.assertIsNotNone(trade)
        # Deep ITM2 contract selected (ask = 50.0)
        self.assertEqual(trade.token, "INFY_ITM2")
        self.assertEqual(trade.entry_price, 50.0)
        # Target: +7% = 53.50, SL: -4% = 48.00
        self.assertAlmostEqual(trade.target_price, 53.50, places=2)
        self.assertAlmostEqual(trade.stop_loss, 48.00, places=2)

        # Velocity timeout after 180s if profit < +2%
        reason_timeout = track.evaluate_exit_rules(trade, ltp=50.2, bid=50.2, ask=50.3, ts=trade.entry_time + 185.0)
        self.assertEqual(reason_timeout, "VELOCITY_TIMEOUT")

    def test_micro_chandelier_ratchets(self):
        track = MicroChandelierTrack(context=self.context)
        opp = _make_dummy_opp()
        trade = track.on_opportunity(opp)
        self.assertIsNotNone(trade)
        # ITM1 selected (ask = 35.0)
        self.assertEqual(trade.token, "INFY_ITM1")
        self.assertEqual(trade.entry_price, 35.0)

        # +3% profit -> Ratchet 1 (Breakeven 35.0)
        track.on_tick("INFY_ITM1", ltp=36.1, bid=36.1, ask=36.2, ts=trade.entry_time + 10.0)
        self.assertEqual(trade.ratchet_level, 1)
        self.assertEqual(trade.stop_loss, 35.0)

        # +5% profit -> Ratchet 2 (+3% lock = 36.05)
        track.on_tick("INFY_ITM1", ltp=36.8, bid=36.8, ask=36.9, ts=trade.entry_time + 20.0)
        self.assertEqual(trade.ratchet_level, 2)
        self.assertAlmostEqual(trade.stop_loss, 36.05, places=2)

    def test_gamma_runner_otm2_ratchets(self):
        track = GammaRunnerTrack(context=self.context)
        opp = _make_dummy_opp()
        trade = track.on_opportunity(opp)
        self.assertIsNotNone(trade)
        # OTM2 selected (ask = 6.0)
        self.assertEqual(trade.token, "INFY_OTM2")
        self.assertEqual(trade.entry_price, 6.0)
        self.assertAlmostEqual(trade.target_price, 10.50, places=2)  # +75%
        self.assertAlmostEqual(trade.stop_loss, 5.10, places=2)      # -15%

        # +25% profit (bid = 7.50) -> Level 1 Breakeven
        track.on_tick("INFY_OTM2", ltp=7.55, bid=7.55, ask=7.60, ts=trade.entry_time + 50.0)
        self.assertEqual(trade.ratchet_level, 1)
        self.assertAlmostEqual(trade.stop_loss, 6.06, places=2)

        # +50% profit (bid = 9.0) -> Level 2 (+25% lock = 7.50)
        track.on_tick("INFY_OTM2", ltp=9.05, bid=9.05, ask=9.10, ts=trade.entry_time + 100.0)
        self.assertEqual(trade.ratchet_level, 2)
        self.assertAlmostEqual(trade.stop_loss, 7.50, places=2)

    def test_volatility_expansion_deceleration(self):
        track = VolatilityExpansionTrack(context=self.context)
        opp = _make_dummy_opp()
        trade = track.on_opportunity(opp)
        self.assertIsNotNone(trade)

        # 5-minute timeout with < 3% gain
        reason = track.evaluate_exit_rules(trade, ltp=20.2, bid=20.2, ask=20.3, ts=trade.entry_time + 305.0)
        self.assertEqual(reason, "DECELERATION_TIMEOUT")

    def test_adaptive_regime_phases(self):
        track = AdaptiveRegimeTrack(context=self.context)

        # Morning phase (09:45 IST) -> ATM selected
        dt_am = datetime(2026, 11, 20, 9, 45, 0, tzinfo=IST)
        opp_am = _make_dummy_opp(timestamp=dt_am.timestamp())
        trade_am = track.on_opportunity(opp_am)
        self.assertIsNotNone(trade_am)
        self.assertEqual(trade_am.token, "INFY_ATM")
        self.assertEqual(trade_am.metadata.get("regime_phase"), "MORNING")

        # Midday phase (11:30 IST) -> ITM1 selected for chop defense
        dt_mid = datetime(2026, 11, 20, 11, 30, 0, tzinfo=IST)
        opp_mid = _make_dummy_opp(timestamp=dt_mid.timestamp())
        trade_mid = track.on_opportunity(opp_mid)
        self.assertIsNotNone(trade_mid)
        self.assertEqual(trade_mid.token, "INFY_ITM1")
        self.assertEqual(trade_mid.metadata.get("regime_phase"), "MIDDAY")

    def test_trend_pullback_v2_spread_calibration(self):
        track = TrendPullbackV2Track(context=self.context)
        opp = _make_dummy_opp()
        trade = track.on_opportunity(opp)
        self.assertIsNotNone(trade)
        # Initial SL dynamically calibrated against spread (ask 35.0, bid 34.6 -> spread ~1.14%)
        # SL pct >= 0.06
        self.assertGreaterEqual(track.sl_pct, 0.06)

    def test_track_profile_presets_and_factory(self):
        # 1. v1 profile
        v1_names = get_enabled_track_names(track_profile="v1")
        self.assertIn("fast_momentum_ATM", v1_names)
        self.assertIn("trend_pullback_ATM", v1_names)
        self.assertIn("fixed_target_ATM", v1_names)
        self.assertNotIn("structural_convexity_ATM", v1_names)

        # 2. v2 profile (gamma_runner_OTM2 retired)
        v2_names = get_enabled_track_names(track_profile="v2")
        self.assertIn("structural_convexity_ATM", v2_names)
        self.assertIn("opening_thrust_convex_ATM", v2_names)
        self.assertIn("delta_scalp_ITM2", v2_names)
        self.assertIn("micro_chandelier_ITM1", v2_names)
        self.assertNotIn("gamma_runner_OTM2", v2_names)
        self.assertIn("volatility_expansion_ATM", v2_names)
        self.assertIn("adaptive_regime_ATM", v2_names)
        self.assertIn("trend_pullback_v2_ITM1", v2_names)
        self.assertNotIn("fast_momentum_ATM", v2_names)

        # 3. default profile (both v1 and active v2, without gamma_runner_OTM2)
        def_names = get_enabled_track_names(track_profile="default")
        self.assertIn("fast_momentum_ATM", def_names)
        self.assertIn("structural_convexity_ATM", def_names)
        self.assertNotIn("gamma_runner_OTM2", def_names)

        # 4. Instantiate v2 tracks via create_tracks
        tracks_v2 = create_tracks(context=self.context, track_profile="v2")
        self.assertEqual(len(tracks_v2), 7)
        self.assertIsInstance(tracks_v2["structural_convexity_ATM"], StructuralConvexityTrack)
        self.assertIsInstance(tracks_v2["delta_scalp_ITM2"], DeltaScalpTrack)
        self.assertTrue(tracks_v2["structural_convexity_ATM"].option_scaling_cfg.get("enabled"))

    def test_option_convexity_scaling_and_spread_sl_floor(self):
        # Expensive ITM2 option (ask = 50 on 1500 spot -> prem_pct = 3.33%, scale = 0.015 / 0.03333 = 0.45)
        tracks_v1 = create_tracks(context=self.context, track_filter=["fixed_target_ATM"])
        tr_obj = tracks_v1["fixed_target_ATM"]
        # High-premium quote (ask = 45.0 -> 3.0% on 1500 spot -> scale = 0.50)
        opp_hi = _make_dummy_opp(trigger_price=1500.0)
        opp_hi.strikes["ATM"] = {"token": "INFY_ATM", "strike": 1500.0, "ask": 45.0, "bid": 44.5, "lot_size": 300}
        trade = tr_obj.on_opportunity(opp_hi)
        self.assertIsNotNone(trade)
        # target scaled by 0.50: 0.15 * 0.50 = 0.075
        self.assertAlmostEqual(trade.metadata["target_pct"], 0.075, places=4)
        self.assertAlmostEqual(trade.target_price, 45.0 * 1.075, places=4)
        # Wide-spread quote triggers 2.5x spread SL floor (>= 6.25% for 2.5% spread)
        opp_sp = _make_dummy_opp(trigger_price=1500.0)
        opp_sp.strikes["ATM"] = {"token": "INFY_ATM", "strike": 1500.0, "ask": 20.0, "bid": 19.5, "lot_size": 300}
        trade_sp = tr_obj.on_opportunity(opp_sp)
        self.assertIsNotNone(trade_sp)
        self.assertAlmostEqual(trade_sp.metadata["sl_pct"], 0.0625, places=4)


if __name__ == "__main__":
    unittest.main()
