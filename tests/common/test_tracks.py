"""
Unit Tests for Execution Tracks & Invariant #2 (Strict Ask-based pricing).
Verifies isolated track-specific exit rules and ratchets.
"""
import time
import unittest

from common.interfaces import ExecutionContext, TradeState
from common.tracks.fast_momentum import FastMomentumTrack
from common.tracks.trend_pullback import TrendPullbackTrack
from common.tracks.fixed_target import FixedTargetTrack
from common.tracks.composite_track import CompositeTrack
from core.common.ipc_schemas import ActionableOpportunity, OrderRequest


class DummyExecutionContext(ExecutionContext):
    def __init__(self, mode: str = "paper"):
        self._mode = mode
        self.submitted_orders = []
        self.journal_events = []
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


class TestTracks(unittest.TestCase):
    def setUp(self):
        self.context = DummyExecutionContext()

    def test_invariant_2_strict_ask_pricing(self):
        track = FastMomentumTrack(context=self.context, target_pct=0.12, sl_pct=0.08)

        # Stale/different LTP = 15.0, but executable depth Ask = 20.0, Bid = 19.8
        opp = ActionableOpportunity(
            opp_id="OPP_1",
            alert_id="ALT_1",
            symbol="RELIANCE",
            direction="BULL",
            archetype="FLAG_BULL_BREAKOUT",
            trigger_price=2500.0,
            strikes={
                "ATM": {
                    "token": "NSE:RELIANCE26NOV2500CE",
                    "strike": 2500.0,
                    "option_type": "CE",
                    "ltp": 15.0,   # Stale LTP
                    "bid": 19.8,
                    "ask": 20.0,   # Executable Ask
                    "lot_size": 250,
                }
            },
            timestamp=1000.0,
        )

        trade = track.on_opportunity(opp)
        self.assertIsNotNone(trade)

        # INVARIANT #2 VERIFICATION:
        # Entry price must be calculated strictly on Ask (20.0), NOT on LTP (15.0)!
        self.assertEqual(trade.entry_ask, 20.0)
        self.assertEqual(trade.entry_price, 20.0)
        # Target = 20.0 * 1.12 = 22.40
        self.assertAlmostEqual(trade.target_price, 22.40, places=2)
        # SL = 20.0 * (1 - 0.08) = 18.40
        self.assertAlmostEqual(trade.stop_loss, 18.40, places=2)

    def test_fast_momentum_ratchets_and_decay(self):
        track = FastMomentumTrack(context=self.context, target_pct=0.12, sl_pct=0.08)
        opp = ActionableOpportunity(
            opp_id="OPP_FM",
            alert_id="ALT_FM",
            symbol="TCS",
            direction="BULL",
            archetype="PURE_BULL_BREAKOUT",
            trigger_price=3500.0,
            strikes={
                "ATM": {
                    "token": "NSE:TCS26NOV3500CE",
                    "strike": 3500.0,
                    "option_type": "CE",
                    "ltp": 100.0,
                    "bid": 99.5,
                    "ask": 100.0,
                    "lot_size": 175,
                }
            },
            timestamp=1000.0,
        )
        trade = track.on_opportunity(opp)
        trade.entry_time = 1000.0
        # Entry Ask = 100.0, Target = 112.0, SL = 92.0

        # Tick 1: at +6% profit (Bid = 106.0), SL ratchets to breakeven (100.0)
        track.on_tick("NSE:TCS26NOV3500CE", ltp=106.0, bid=106.0, ask=106.2, ts=1060.0)
        self.assertEqual(trade.ratchet_level, 1)
        self.assertEqual(trade.stop_loss, 100.0)
        self.assertEqual(trade.status, "OPEN")

        # Tick 2: at +9% profit (Bid = 109.0), SL ratchets to +4% (104.0)
        track.on_tick("NSE:TCS26NOV3500CE", ltp=109.0, bid=109.0, ask=109.2, ts=1100.0)
        self.assertEqual(trade.ratchet_level, 2)
        self.assertEqual(trade.stop_loss, 104.0)
        self.assertEqual(trade.status, "OPEN")

        # Tick 3: Target hit (Bid = 112.5) -> Exits with TARGET_HIT
        track.on_tick("NSE:TCS26NOV3500CE", ltp=112.5, bid=112.5, ask=112.8, ts=1120.0)
        self.assertEqual(trade.status, "CLOSED")
        self.assertEqual(trade.exit_reason, "TARGET_HIT")
        self.assertEqual(trade.exit_price, 112.5)

    def test_trend_pullback_stepped_ratchet(self):
        track = TrendPullbackTrack(context=self.context, target_pct=0.25, sl_pct=0.12)
        opp = ActionableOpportunity(
            opp_id="OPP_TP",
            alert_id="ALT_TP",
            symbol="INFY",
            direction="BULL",
            archetype="FLAG_BULL_BREAKOUT",
            trigger_price=1800.0,
            strikes={
                "ITM1": {
                    "token": "NSE:INFY26NOV1750CE",
                    "strike": 1750.0,
                    "option_type": "CE",
                    "ltp": 50.0,
                    "bid": 49.8,
                    "ask": 50.0,
                    "lot_size": 400,
                }
            },
            timestamp=1000.0,
        )
        trade = track.on_opportunity(opp)
        trade.entry_time = 1000.0
        # Entry = 50.0, Target = 62.50, SL = 44.0

        # Step 1: At +15% profit (Bid = 57.50), SL moves to +5% (52.50)
        track.on_tick("NSE:INFY26NOV1750CE", ltp=57.5, bid=57.5, ask=57.8, ts=1300.0)
        self.assertEqual(trade.ratchet_level, 1)
        self.assertEqual(trade.stop_loss, 52.50)

        # Step 2: At +20% profit (Bid = 60.0), SL moves to +12% (56.00)
        track.on_tick("NSE:INFY26NOV1750CE", ltp=60.0, bid=60.0, ask=60.3, ts=1500.0)
        self.assertEqual(trade.ratchet_level, 2)
        self.assertAlmostEqual(trade.stop_loss, 56.00, places=2)

    def test_fixed_target_inr_gate(self):
        track = FixedTargetTrack(
            context=self.context,
            target_pct=0.15,
            sl_pct=0.10,
            fixed_target_inr=2000.0,
        )
        opp = ActionableOpportunity(
            opp_id="OPP_FT",
            alert_id="ALT_FT",
            symbol="SBIN",
            direction="BULL",
            archetype="PURE_BULL_BREAKOUT",
            trigger_price=800.0,
            strikes={
                "ATM": {
                    "token": "NSE:SBIN26NOV800CE",
                    "strike": 800.0,
                    "option_type": "CE",
                    "ltp": 20.0,
                    "bid": 19.9,
                    "ask": 20.0,
                    "lot_size": 750,
                }
            },
            timestamp=1000.0,
        )
        trade = track.on_opportunity(opp)
        trade.entry_time = 1000.0
        # Entry = 20.0, Qty = 750.
        # If Bid reaches 22.70: PnL = (22.70 - 20.0) * 750 = 2.70 * 750 = 2025 >= 2000 INR target!
        track.on_tick("NSE:SBIN26NOV800CE", ltp=22.7, bid=22.7, ask=22.8, ts=1100.0)
        self.assertEqual(trade.status, "CLOSED")
        self.assertEqual(trade.exit_reason, "INR_TARGET_HIT")

    def test_preferred_mode_strike_selection(self):
        opp = ActionableOpportunity(
            opp_id="OPP_MODES",
            alert_id="ALT_MODES",
            symbol="INFY",
            direction="BULL",
            archetype="FLAG_BULL_BREAKOUT",
            trigger_price=1500.0,
            strikes={
                "ATM": {
                    "token": "NSE:INFY26NOV1500CE",
                    "strike": 1500.0,
                    "option_type": "CE",
                    "ltp": 30.0,
                    "bid": 29.8,
                    "ask": 30.0,
                    "lot_size": 400,
                },
                "ITM1": {
                    "token": "NSE:INFY26NOV1480CE",
                    "strike": 1480.0,
                    "option_type": "CE",
                    "ltp": 45.0,
                    "bid": 44.8,
                    "ask": 45.0,
                    "lot_size": 400,
                },
                "OTM1": {
                    "token": "NSE:INFY26NOV1520CE",
                    "strike": 1520.0,
                    "option_type": "CE",
                    "ltp": 18.0,
                    "bid": 17.8,
                    "ask": 18.0,
                    "lot_size": 400,
                },
            },
            timestamp=1000.0,
        )

        # Track 1: FastMomentum ATM
        t_atm = FastMomentumTrack(context=self.context, track_name="fast_momentum_ATM", preferred_mode="ATM")
        tr_atm = t_atm.on_opportunity(opp)
        self.assertIsNotNone(tr_atm)
        self.assertEqual(tr_atm.token, "NSE:INFY26NOV1500CE")
        self.assertEqual(tr_atm.entry_ask, 30.0)

        # Track 2: FastMomentum ITM
        t_itm = FastMomentumTrack(context=self.context, track_name="fast_momentum_ITM", preferred_mode="ITM")
        tr_itm = t_itm.on_opportunity(opp)
        self.assertIsNotNone(tr_itm)
        self.assertEqual(tr_itm.token, "NSE:INFY26NOV1480CE")
        self.assertEqual(tr_itm.entry_ask, 45.0)

        # Track 3: TrendPullback OTM
        t_otm = TrendPullbackTrack(context=self.context, track_name="trend_pullback_OTM", preferred_mode="OTM")
        tr_otm = t_otm.on_opportunity(opp)
        self.assertIsNotNone(tr_otm)
        self.assertEqual(tr_otm.token, "NSE:INFY26NOV1520CE")
        self.assertEqual(tr_otm.entry_ask, 18.0)

    def test_composite_track_shared_pool(self):
        st1 = FixedTargetTrack(context=self.context, track_name="fixed_target_ITM", preferred_mode="ITM")
        st2 = FixedTargetTrack(context=self.context, track_name="fixed_target_ATM", preferred_mode="ATM")
        st3 = TrendPullbackTrack(context=self.context, track_name="trend_pullback_OTM", preferred_mode="OTM")

        comp = CompositeTrack(
            context=self.context,
            track_name="combination",
            sub_tracks=[st1, st2, st3],
            shared_pool=True,
            total_capital=6000.0,
        )

        def make_opp(sym: str):
            return ActionableOpportunity(
                opp_id=f"OPP_{sym}",
                alert_id=f"ALT_{sym}",
                symbol=sym,
                direction="BULL",
                archetype="FLAG_BULL_BREAKOUT",
                trigger_price=1000.0,
                strikes={
                    "ATM": {"token": f"NSE:{sym}ATM", "strike": 1000.0, "ask": 20.0, "bid": 19.8, "lot_size": 100},
                    "ITM1": {"token": f"NSE:{sym}ITM", "strike": 980.0, "ask": 35.0, "bid": 34.8, "lot_size": 100},
                    "OTM1": {"token": f"NSE:{sym}OTM", "strike": 1020.0, "ask": 12.0, "bid": 11.8, "lot_size": 100},
                },
                timestamp=1000.0,
            )

        # Trade 1 from opp1: st1 selects ITM1 (ask 35.0 * 100 = ₹3,500 <= ₹6,000 available)
        tr1 = comp.on_opportunity(make_opp("SYM1"))
        self.assertIsNotNone(tr1)
        self.assertEqual(len(comp.active_trades), 1)
        self.assertEqual(tr1.track, "combination")  # Logged under composite track name
        self.assertEqual(comp.available_capital, 2500.0)

        # Trade 2 from opp2: st1 selects ITM1 (requires ₹3,500 > ₹2,500 remaining), so st2 (ATM, ask 20.0 * 100 = ₹2,000 <= ₹2,500) executes!
        tr2 = comp.on_opportunity(make_opp("SYM2"))
        self.assertIsNotNone(tr2)
        self.assertEqual(len(comp.active_trades), 2)
        self.assertEqual(comp.available_capital, 500.0)

        # Trade 3 from opp3: st3 selects OTM1 (requires 12.0 * 100 = ₹1,200 > ₹500 remaining). Must be blocked by capital availability!
        tr3 = comp.on_opportunity(make_opp("SYM3"))
        self.assertIsNone(tr3)
        self.assertEqual(len(comp.active_trades), 2)

    def test_composite_track_equal_share_pool(self):
        # 2 sub-tracks, shared_pool=False, total_capital=10,000 -> each sub-track gets partitioned share of ₹5,000
        st1 = FixedTargetTrack(context=self.context, track_name="fixed_target_ITM", preferred_mode="ITM")
        st2 = FixedTargetTrack(context=self.context, track_name="fixed_target_ATM", preferred_mode="ATM")

        comp = CompositeTrack(
            context=self.context,
            track_name="combination",
            sub_tracks=[st1, st2],
            shared_pool=False,
            total_capital=10000.0,
        )
        self.assertEqual(st1.allocated_capital, 5000.0)
        self.assertEqual(st2.allocated_capital, 5000.0)

        # st1 requires 4,000 -> accepted (remaining 1,000)
        self.assertTrue(st1.can_allocate(4000.0))
        st1.commit_capital(4000.0)
        self.assertEqual(st1.available_capital, 1000.0)

        # st1 requires 2,000 > 1,000 -> rejected!
        self.assertFalse(st1.can_allocate(2000.0))

        # Meanwhile st2 has its independent partition of 5,000!
        self.assertEqual(st2.available_capital, 5000.0)
        self.assertTrue(st2.can_allocate(4000.0))

    def test_factory_create_tracks_from_config(self):
        from common.tracks.factory import get_enabled_track_names, create_tracks

        sample_cfg = {
            "strategies": {
                "momentum": {
                    "enabled_tracks": [
                        {"type": "base", "name": "fast_momentum", "modes": ["ATM", "ITM"]},
                        {
                            "type": "composite",
                            "name": "combination",
                            "shared_pool": True,
                            "composition": [
                                {"fixed_target": ["ITM", "ATM"]},
                                {"trend_pullback": ["OTM"]},
                            ],
                        },
                    ],
                    "tracks": {
                        "fast_momentum": {"target_pct": 0.12, "sl_pct": 0.08},
                        "fixed_target": {"target_pct": 0.15, "sl_pct": 0.10, "fixed_target_inr": 800.0},
                        "trend_pullback": {"target_pct": 0.25, "sl_pct": 0.12},
                    },
                }
            }
        }

        names = get_enabled_track_names(sample_cfg)
        self.assertEqual(names, ["fast_momentum_ATM", "fast_momentum_ITM", "combination"])

        tracks = create_tracks(context=self.context, strategy_cfg=sample_cfg)
        self.assertIn("fast_momentum_ATM", tracks)
        self.assertIn("fast_momentum_ITM", tracks)
        self.assertIn("combination", tracks)

        self.assertEqual(tracks["fast_momentum_ATM"].preferred_mode, "ATM")
        self.assertEqual(tracks["fast_momentum_ITM"].preferred_mode, "ITM")

        comb = tracks["combination"]
        self.assertIsInstance(comb, CompositeTrack)
        self.assertEqual(len(comb.sub_tracks), 3)  # fixed_target_ITM, fixed_target_ATM, trend_pullback_OTM
        self.assertTrue(comb.shared_pool)


if __name__ == "__main__":
    unittest.main()
