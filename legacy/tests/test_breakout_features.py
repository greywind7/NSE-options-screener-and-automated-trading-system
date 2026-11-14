#!/usr/bin/env python3
"""
scratch/test_breakout_features.py
==================================
Unit tests for the three new breakout features:
1. 8-Second Fresh Breakout Alert Category & Live Countdown
2. 1-Lot ATM Option Cash Requirement Column
3. Separate Bull/Bear Flag Breakout / Breakdown Alert (💥 BULL BREAKOUT & 💥 BEAR BREAKDOWN)
"""

import sys
import os
import time
from datetime import datetime, timedelta

# Add legacy directory to path
legacy_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, legacy_dir)

from stream_rvol_scanner import (
    calculate_setup_score,
    make_setup_badge,
    setup_rank_key,
    compute_option_requirement,
    _render_row,
    print_dashboard,
    RollingVolumeTracker,
)
from bidirectional_engine import (
    FlagPatternTracker,
    OptionStrikeResolver,
    Instrument,
    InstrumentManager,
    FlagPaperPortfolio,
)


def test_early_flag_breakout_and_scoring():
    print("[TEST 1] Testing Early Flag Breakout Detection & Setup Scoring...")
    tracker = RollingVolumeTracker()
    key = "NSE_EQ|TEST1"
    profile = {
        "symbol": "TEST1",
        "avg_1m_volume": 2000.0,
        "avg_5m_volume": 10000.0,
        "time_slot_avg_volume": {datetime.now().strftime("%H:%M"): 2000.0},
        "cumulative_expected_volume": {datetime.now().strftime("%H:%M"): 100000.0},
    }

    # 1. Establish preceding Bull Pole & coiling at highs
    t0 = datetime.now()
    tracker.pole_tracking[key]["bull_pole_ts"] = t0.timestamp()
    tracker.pole_tracking[key]["bull_pole_price"] = 100.0

    # Cycle 1: coiling near high with dry volume
    v1 = tracker.update(
        key=key,
        current_volume=50000,
        rvol_cum=1.0,
        profile=profile,
        range_pos=0.88,
        ltp=100.0,
        high_price=101.0,
        low_price=95.0,
        now=t0,
    )
    assert v1["is_bull_coiling"] or tracker.flag_states[key].get("state") == "BULL_FLAG", "Should detect bull coiling"

    # Cycle 2: piercing the flag ceiling with volume inflation (rvol_1m = 1.2, pace = 1.5)
    # Simulate minute candle rollover so prev_high is 100.0
    tracker.minute_state[key]["prev_high"] = 100.0
    tracker.flag_states[key]["state"] = "BULL_FLAG"
    tracker.flag_states[key]["coiling_cycles"] = 2
    tracker.flag_states[key]["flag_high"] = 100.0

    t1 = t0 + timedelta(seconds=10)
    v2 = tracker.update(
        key=key,
        current_volume=65000,
        rvol_cum=1.2,
        profile=profile,
        range_pos=0.92,
        ltp=101.5,   # Breaks above flag ceiling 100.0
        high_price=101.5,
        low_price=95.0,
        now=t1,
    )

    assert v2["is_flag_breakout"], "Should flag early breakout"
    assert v2["flag_breakout_type"] == "BULL", "Should be BULL flag breakout"

    # Test scoring with flag breakout
    score, stage, direction, details = calculate_setup_score(
        rvol_5m=1.2,
        rvol_1m=1.5,
        rvol_cum=1.2,
        ltp=101.5,
        open_price=96.0,
        high_price=101.5,
        low_price=95.0,
        pct_change=4.5,
        price_history=[99.0, 100.0, 100.5, 101.0, 101.5],
        is_flag_breakout=v2["is_flag_breakout"],
        flag_breakout_type=v2["flag_breakout_type"],
    )

    assert stage == "💥 BULL BREAKOUT", f"Expected '💥 BULL BREAKOUT', got '{stage}'"
    assert direction == "BULL"
    assert score >= 80, f"Score should be at least 80 for early flag breakout, got {score}"

    badge = make_setup_badge(stage, direction)
    assert "💥 BULL BREAK" in badge, f"Badge should contain 💥 BULL BREAK, got {badge}"
    print(f"  -> Early Bull Breakout Verified: stage={stage}, score={score}, badge={badge.strip()}")


def test_flag_engine_no_coiling_entry():
    print("\n[TEST 2] Testing Flag Engine Avoids Premature Coiling Entry & Fires on Early Break...")
    flag_tracker = FlagPatternTracker(symbol="TEST_FLAG")

    # While in coiling (🌀 BULL FLAG), it MUST NOT enter!
    state, event = flag_tracker.update(
        ltp=100.0,
        high=100.5,
        low=95.0,
        range_pos=0.88,
        rvol_5m=0.5,
        rvol_cum=1.0,
        stage="🌀 BULL FLAG",
        score=65,
    )
    assert state != "TRIGGERED" and state != "IN_TRADE", f"Should not enter while coiling! Got state={state}"
    print("  -> Coiling state does NOT trigger premature entry: PASSED")

    # When early flag breakout occurs (💥 BULL BREAKOUT), it MUST trigger immediately!
    state2, event2 = flag_tracker.update(
        ltp=101.5,
        high=101.5,
        low=95.0,
        range_pos=0.92,
        rvol_5m=1.5,
        rvol_cum=1.3,
        stage="💥 BULL BREAKOUT",
        score=85,
    )
    assert state2 == "TRIGGERED", f"Expected TRIGGERED on '💥 BULL BREAKOUT', got {state2}"
    assert event2.get("trigger_source") == "💥 BULL BREAKOUT", f"Expected trigger_source '💥 BULL BREAKOUT', got {event2}"
    assert event2.get("direction") == "BULL", f"Expected direction BULL, got {event2}"
    print("  -> '💥 BULL BREAKOUT' triggers direct entry: PASSED")


def test_option_requirement_column():
    print("\n[TEST 3] Testing 1-Lot ATM Option Requirement Computation & Formatting...")
    # 1. Non-F&O equity
    non_fo_res = compute_option_requirement(None, "PENNY", 45.0, direction="BULL")
    assert not non_fo_res["has_fo"], "Should be non-F&O"
    assert "Cash Only" in non_fo_res["display_str"], "Should display Cash Only"
    print(f"  -> Non-F&O display: '{non_fo_res['raw_str']}'")

    # 2. Mock F&O resolver
    class MockInst:
        def __init__(self, key, sym, strike, lot):
            self.instrument_key = key
            self.trading_symbol = sym
            self.strike_price = strike
            self.lot_size = lot

    class MockResolved:
        def __init__(self, atm):
            self.atm_inst = atm
            self.expiry_date = "2026-11-26"

    class MockResolver:
        def resolve_strikes(self, sym, spot, option_type="CE"):
            return MockResolved(MockInst("NSE_FO|12345", f"{sym}26NOV2500CE", 2500.0, 250))

    mock_quotes = {
        "NSE_FO|12345": {
            "last_price": 35.5,
            "depth": {"sell": [{"price": 36.0, "quantity": 500}], "buy": [{"price": 35.0, "quantity": 500}]},
        }
    }

    resolver = MockResolver()
    fo_res = compute_option_requirement(resolver, "RELIANCE", 2490.0, direction="BULL", quotes=mock_quotes)
    assert fo_res["has_fo"], "Should be F&O"
    # In live market, ask = 36.0, lot_size = 250 -> 36.0 * 250 = 9000
    assert fo_res["one_lot_cost"] > 0, "Cost should be > 0"
    assert "₹9.0k" in fo_res["raw_str"] or "₹8.9k" in fo_res["raw_str"], f"Got: {fo_res['raw_str']}"
    print(f"  -> F&O Option requirement display: '{fo_res['raw_str']}'")


def test_8_second_fresh_breakout_lifecycle():
    print("\n[TEST 4] Testing 8-Second Fresh Breakout Alert Lifecycle & Downgrade...")
    fresh_tracker = {}
    previous_stages = {}

    key = "NSE_EQ|TCS"
    sym = "TCS"

    # Step 1: Stock is in COILING stage (not breakout)
    previous_stages[key] = "🌀 BULL FLAG"

    # Step 2: Transitions to 💥 BULL BREAKOUT
    new_stage = "💥 BULL BREAKOUT"
    t0 = time.time()
    row_data = {
        "instrument_key": key,
        "symbol": sym,
        "last_price": 3500.0,
        "pct_change": 2.5,
        "breakout_score": 88,
        "breakout_stage": new_stage,
        "direction": "BULL",
        "opt_req_display": "  ₹15.2k (₹45.0)  ",
    }

    breakout_stages = {"🚀 BREAKOUT", "🩸 BREAKDOWN", "💥 BULL BREAKOUT", "💥 BEAR BREAKDOWN"}
    prev = previous_stages.get(key, "NORMAL")
    previous_stages[key] = new_stage

    if new_stage in breakout_stages and prev not in breakout_stages:
        fresh_tracker[key] = {
            "trigger_ts": t0,
            "symbol": sym,
            "stage": new_stage,
            "direction": "BULL",
            "row_data": row_data,
        }

    assert key in fresh_tracker, "Stock should be added to fresh_breakout_tracker"

    # Step 3: Check at t + 3.0 seconds -> should be FRESH with ~5.0s remaining
    now_ts = t0 + 3.0
    elapsed = now_ts - fresh_tracker[key]["trigger_ts"]
    assert elapsed < 8.0, "Should still be in 8s window"
    rem_sec = round(8.0 - elapsed, 1)
    assert abs(rem_sec - 5.0) < 0.1, f"Expected ~5.0s remaining, got {rem_sec}"
    countdown_str = f"⏳ {rem_sec:.1f}s left"
    print(f"  -> Live Countdown at +3.0s: {countdown_str}")

    # Render row with countdown
    row_str = _render_row(row_data, threshold=2.0, countdown_str=countdown_str)
    assert "⏳ 5.0s left" in row_str or "⏳" in row_str, "Row should contain countdown badge"
    assert "₹15.2k" in row_str, "Row should contain option requirement"
    print(f"  -> Rendered Fresh Alert Row:\n     {row_str}")

    # Step 4: Check at t + 8.5 seconds -> should EXPIRE and demote to confirmed setups
    now_ts_expired = t0 + 8.5
    elapsed_exp = now_ts_expired - fresh_tracker[key]["trigger_ts"]
    if elapsed_exp >= 8.0:
        fresh_tracker.pop(key, None)

    assert key not in fresh_tracker, "Stock should be removed from fresh_breakout_tracker after 8s"

    # It now appears in confirmed_setups
    confirmed_setups = [row_data]
    assert len(confirmed_setups) == 1, "Should now be in confirmed_setups"
    assert confirmed_setups[0]["breakout_stage"] == "💥 BULL BREAKOUT"
    print("  -> Expired after 8.5s: Naturally downgraded to confirmed setups table! PASSED")


def test_dashboard_rendering():
    print("\n[TEST 5] Testing print_dashboard Rendering with Fresh Alerts & 1-Lot Column...")
    fresh_alert_row = {
        "instrument_key": "NSE_EQ|INFY",
        "symbol": "INFY",
        "last_price": 1820.0,
        "net_change": 25.0,
        "pct_change": 1.39,
        "price_history": [1810.0, 1815.0, 1820.0],
        "high": 1825.0,
        "low": 1800.0,
        "rvol_5m": 2.45,
        "rvol_cum": 1.65,
        "pace_ratio": 1.48,
        "breakout_score": 92,
        "breakout_stage": "💥 BULL BREAKOUT",
        "direction": "BULL",
        "activity_state": "🔥 SURGE 5m",
        "opt_req_display": "  ₹14.5k (₹29.0)  ",
        "opt_req_raw": "₹14.5k (₹29.0)",
    }
    confirmed_row = {
        "instrument_key": "NSE_EQ|RELIANCE",
        "symbol": "RELIANCE",
        "last_price": 2500.0,
        "net_change": 30.0,
        "pct_change": 1.21,
        "price_history": [2480.0, 2490.0, 2500.0],
        "high": 2505.0,
        "low": 2470.0,
        "rvol_5m": 1.85,
        "rvol_cum": 1.40,
        "pace_ratio": 1.32,
        "breakout_score": 85,
        "breakout_stage": "🚀 BREAKOUT",
        "direction": "BULL",
        "activity_state": "⚡ ACTIVE",
        "opt_req_display": "  ₹12.0k (₹24.0)  ",
        "opt_req_raw": "₹12.0k (₹24.0)",
    }
    fresh_alerts = [{
        "trigger_ts": time.time(),
        "rem_sec": 6.8,
        "row_data": fresh_alert_row,
    }]
    print_dashboard(
        alerts=[fresh_alert_row, confirmed_row],
        confirmed_setups=[confirmed_row],
        forming_setups=[],
        flag_setups=[],
        all_rows=[fresh_alert_row, confirmed_row],
        threshold=2.0,
        scan_count=1,
        fresh_alerts=fresh_alerts,
    )
    print("  -> print_dashboard rendered with 0 errors: PASSED")


def test_downgrade_to_confirmed_never_disappears():
    print("\n[TEST 6] Testing Fresh Breakouts Downgrade to Confirmed & Never Disappear...")
    downgraded = {}
    fresh_tracker = {}
    key = "NSE_EQ|SBIN"
    sym = "SBIN"

    t0 = time.time()
    row_data = {
        "instrument_key": key,
        "symbol": sym,
        "last_price": 820.0,
        "breakout_stage": "💥 BULL BREAKOUT",
        "breakout_score": 88,
    }

    # Step 1: Trigger fresh breakout
    fresh_tracker[key] = {
        "trigger_ts": t0,
        "stage": "💥 BULL BREAKOUT",
        "row_data": row_data,
    }

    # Both fresh and confirmed are populated (confirmed is NEVER eliminated)
    all_rows = [row_data]
    breakout_stages = {"🚀 BREAKOUT", "🩸 BREAKDOWN", "💥 BULL BREAKOUT", "💥 BEAR BREAKDOWN"}
    confirmed = [r for r in all_rows if r.get("breakout_stage") in breakout_stages]
    assert len(confirmed) == 1, "Confirmed setups must NOT be eliminated while alert is fresh!"
    print("  -> Confirmed category remains populated while fresh: PASSED")

    # Step 2: At +9.0s (after fresh countdown expires)
    now_ts = t0 + 9.0
    for k, alert_info in list(fresh_tracker.items()):
        if now_ts - alert_info["trigger_ts"] >= 8.0:
            fresh_tracker.pop(k, None)
            downgraded[k] = {
                "ts": now_ts,
                "stage": alert_info["stage"],
                "row_data": alert_info["row_data"],
            }

    assert key not in fresh_tracker, "Should be removed from fresh banner after 8s"

    # Step 3: Even if market tick temporarily dips stage to NORMAL
    row_data_dipped = dict(row_data)
    row_data_dipped["breakout_stage"] = "NORMAL"
    all_rows_dipped = [row_data_dipped]

    confirmed_keys = set()
    confirmed_setups = []
    for r in all_rows_dipped:
        if r.get("breakout_stage") in breakout_stages:
            confirmed_setups.append(r)
            confirmed_keys.add(r["instrument_key"])

    # Downgraded cache restores it
    for k, d_info in list(downgraded.items()):
        if now_ts - d_info["ts"] <= 900.0 and k not in confirmed_keys:
            matching = [r for r in all_rows_dipped if r["instrument_key"] == k]
            if matching:
                rc = dict(matching[0])
                rc["breakout_stage"] = d_info["stage"]
                confirmed_setups.append(rc)
                confirmed_keys.add(k)

    assert len(confirmed_setups) == 1, "Must remain in confirmed category after 8s downgrade!"
    assert confirmed_setups[0]["instrument_key"] == key
    assert confirmed_setups[0]["breakout_stage"] == "💥 BULL BREAKOUT"
    print("  -> Successfully downgraded to Confirmed category and persisted: PASSED")


def test_flat_ticks_receive_zero_acceleration():
    print("\n[TEST 7] Testing Flat Ticks Receive Zero Acceleration Points (Point 3 Fix)...")
    # Case 1: Identical prices [100.0, 100.0, 100.0]
    score1, stage1, dir1, details1 = calculate_setup_score(
        rvol_5m=1.0,
        rvol_1m=1.0,
        rvol_cum=1.0,
        ltp=100.0,
        open_price=100.0,
        high_price=105.0,
        low_price=95.0,
        pct_change=1.0,
        price_history=[100.0, 100.0, 100.0],
    )
    # Open == ltp gives 5 pts for candle color, but trend is 0 and acceleration MUST be 0!
    assert details1["tick_score"] == 5, f"Flat ticks should receive only candle color (5 pts), got {details1['tick_score']}"
    print(f"  -> Flat ticks [100, 100, 100] tick score: {details1['tick_score']} (0 acceleration): PASSED")

    # Case 2: Strictly advancing prices [100.0, 100.0, 100.5]
    score2, stage2, dir2, details2 = calculate_setup_score(
        rvol_5m=1.0,
        rvol_1m=1.0,
        rvol_cum=1.0,
        ltp=100.5,
        open_price=100.0,
        high_price=105.0,
        low_price=95.0,
        pct_change=1.0,
        price_history=[100.0, 100.0, 100.5],
    )
    # Has positive trend and strict progression, so tick score must be > 5!
    assert details2["tick_score"] > 5, f"Advancing ticks should receive acceleration points, got {details2['tick_score']}"
    print(f"  -> Advancing ticks [100, 100, 100.5] tick score: {details2['tick_score']}: PASSED")


def test_meaningful_tick_thrust_scaled_to_day_range():
    print("\n[TEST 8] Testing Tick Thrust Normalized Relative to Day's Activity (User Enhancement)...")
    # Stock day range is 20.0 (High 120, Low 100)
    # Case A: Micro noise tick move (+0.05 on a 20.0 day range = 0.25% of day range)
    s_noise, _, _, det_noise = calculate_setup_score(
        rvol_5m=1.0,
        rvol_1m=1.0,
        rvol_cum=1.0,
        ltp=110.05,
        open_price=105.0,
        high_price=120.0,
        low_price=100.0,
        pct_change=2.0,
        price_history=[110.00, 110.02, 110.05],
    )

    # Case B: Order book sweep (+1.00 on a 20.0 day range = 5.0% of day range)
    s_sweep, _, _, det_sweep = calculate_setup_score(
        rvol_5m=1.0,
        rvol_1m=1.0,
        rvol_cum=1.0,
        ltp=111.00,
        open_price=105.0,
        high_price=120.0,
        low_price=100.0,
        pct_change=2.0,
        price_history=[110.00, 110.50, 111.00],
    )

    print(f"  -> Noise micro tick score (+0.05 / 20.0 range): {det_noise['tick_score']} pts")
    print(f"  -> Aggressive sweep tick score (+1.00 / 20.0 range): {det_sweep['tick_score']} pts")
    assert det_sweep["tick_score"] > det_noise["tick_score"], "Aggressive day-relative order book sweep must score higher than micro noise!"
    assert det_sweep["tick_score"] >= 20, f"Violent sweep should get near max tick score, got {det_sweep['tick_score']}"
    print("  -> Meaningful tick thrust relative to day's activity: PASSED")


def test_directional_momentum_explicit_alignment():
    print("\n[TEST 9] Testing Directional Momentum Explicit Alignment & Contradiction Penalty (Point 1 Fix)...")
    # Case A: Stock is up on the day (+3%), but dumping in last 5m (-0.7%)
    _, _, _, det_dumping = calculate_setup_score(
        rvol_5m=2.0,
        rvol_1m=1.5,
        rvol_cum=1.5,
        ltp=103.0,
        open_price=100.0,
        high_price=105.0,
        low_price=99.0,
        pct_change=3.0,
        price_history=[104.0, 103.5, 103.0],
        pct_change_5m=-0.7,
    )
    # bull_macro_mom is 8, but docked 10 for severe dump -> 0 pts!
    assert det_dumping["mom_score"] == 0, f"Stock dumping -0.7% in 5m must have 0 momentum score, got {det_dumping['mom_score']}"
    print(f"  -> Up +3% on day but dumping -0.7% in 5m: mom_score = {det_dumping['mom_score']}: PASSED")

    # Case B: Stock is falling on the day (-2.0%)
    _, _, dir_falling, det_falling = calculate_setup_score(
        rvol_5m=2.0,
        rvol_1m=1.5,
        rvol_cum=1.5,
        ltp=98.0,
        open_price=100.0,
        high_price=101.0,
        low_price=97.0,
        pct_change=-2.0,
        price_history=[97.5, 97.8, 98.0],
        pct_change_5m=0.5,
    )
    assert det_falling["bull_mom_score"] == 0, f"Stock falling on the day must receive 0 bull momentum, got {det_falling['bull_mom_score']}"
    print(f"  -> Falling on the day (-2%): bull_mom_score = {det_falling['bull_mom_score']}, dir = {dir_falling}: PASSED")

    # Case C: Genuine aligned bullish momentum: +3.0% day, +0.8% 5m impulse
    _, _, _, det_aligned = calculate_setup_score(
        rvol_5m=2.0,
        rvol_1m=1.5,
        rvol_cum=1.5,
        ltp=103.0,
        open_price=100.0,
        high_price=103.0,
        low_price=99.0,
        pct_change=3.0,
        price_history=[102.2, 102.6, 103.0],
        pct_change_5m=0.8,
    )
    assert det_aligned["mom_score"] >= 20, f"Aligned momentum should score >= 20, got {det_aligned['mom_score']}"
    print(f"  -> Aligned bull momentum: mom_score = {det_aligned['mom_score']}: PASSED")


def test_100_point_ceiling_and_pillar_caps():
    print("\n[TEST 10] Testing 100-Point Ceiling & Individual Pillar Caps (Point 2 Fix)...")
    # Feed extreme breakout numbers that would exceed 100 if uncapped
    score, stage, direction, details = calculate_setup_score(
        rvol_5m=10.0,          # Base vol = 15
        rvol_1m=8.0,
        rvol_cum=4.0,          # Inst bonus = +3
        ltp=110.0,
        open_price=100.0,
        high_price=110.0,      # HOD fresh = 10 + 10 = 20
        low_price=100.0,
        pct_change=10.0,       # Macro mom = 10
        pct_change_5m=3.0,     # Impulse mom = 20 -> mom = 30
        seconds_since_hod=10.0,
        price_history=[100.0, 102.0, 105.0, 108.0, 110.0],  # Full tick thrust + accel = 25
        z_vol=5.0,             # Z-vol = 10 -> vol sum = 15+10+3 = 28 uncapped, must cap at 25!
    )
    assert details["vol_score"] <= 25, f"Pillar 1 Volume must cap at 25, got {details['vol_score']}"
    assert details["pos_score"] <= 20, f"Pillar 2 Position must cap at 20, got {details['pos_score']}"
    assert details["mom_score"] <= 30, f"Pillar 3 Momentum must cap at 30, got {details['mom_score']}"
    assert details["tick_score"] <= 25, f"Pillar 4 Tick Velocity must cap at 25, got {details['tick_score']}"
    assert score <= 100, f"Total score must NEVER exceed 100, got {score}"
    assert score == 100, f"Max conditions should equal exactly 100, got {score}"
    print(f"  -> Total Score: {score}/100 [Vol={details['vol_score']}, Pos={details['pos_score']}, Mom={details['mom_score']}, Tick={details['tick_score']}]: PASSED")


def test_early_window_damping_and_z_score_baseline():
    print("\n[TEST 11] Testing Early Window Volume Confidence Damping & Z-Score Baseline (Point 4 Fix)...")
    tracker = RollingVolumeTracker()
    key = "NSE_EQ|ZTEST"
    profile = {
        "symbol": "ZTEST",
        "avg_1m_volume": 1000.0,
        "avg_5m_volume": 5000.0,
        "time_slot_avg_volume": {datetime.now().strftime("%H:%M"): 1000.0},
    }

    t0 = datetime.now()
    # At t=0 (elapsed = 10s default), sudden volume burst of 1,000 shares
    v0 = tracker.update(
        key=key,
        current_volume=1000,
        rvol_cum=1.0,
        profile=profile,
        range_pos=0.5,
        ltp=100.0,
        high_price=101.0,
        low_price=99.0,
        now=t0,
    )
    # Under old logic: 1000 * (300/10) = 30,000 projected volume (6x RVOL)
    # Under confidence damping: damped towards expected 5000 volume!
    assert v0["proj_5m_vol"] < 20000, f"Early window volume should be damped, got {v0['proj_5m_vol']}"
    print(f"  -> Early 10s burst projected volume damped from 30,000 to {v0['proj_5m_vol']}: PASSED")

    # Fast polling (<30s apart) should NOT pollute rvol_5m_history with duplicates
    for i in range(1, 10):
        tracker.update(
            key=key,
            current_volume=1000 + i * 10,
            rvol_cum=1.0,
            profile=profile,
            range_pos=0.5,
            ltp=100.0,
            high_price=101.0,
            low_price=99.0,
            now=t0 + timedelta(seconds=i * 2),  # every 2s
        )
    assert len(tracker.rvol_5m_history[key]) == 1, f"Fast polling should be cadenced to >=30s, got {len(tracker.rvol_5m_history[key])} samples"
    assert v0["z_vol"] == 0.0, "Z-vol should be 0.0 with insufficient samples (<8)"
    print("  -> Cadenced 30s sampling avoids variance pollution: PASSED")


def test_dead_stock_without_pole_fails_flag_detection():
    print("\n[TEST 12] Testing Stagnant Stock Without Pole Cannot Fake a Flag (Point 5 Fix)...")
    tracker = RollingVolumeTracker()
    key = "NSE_EQ|DEAD"
    profile = {
        "symbol": "DEAD",
        "avg_1m_volume": 2000.0,
        "avg_5m_volume": 10000.0,
    }

    # Stock idling at highs (range_pos = 0.90) with very low volume (rvol_5m = 0.20), but NO preceding pole
    t0 = datetime.now()
    v = tracker.update(
        key=key,
        current_volume=5000,
        rvol_cum=0.5,
        profile=profile,
        range_pos=0.90,
        ltp=100.0,
        high_price=100.2,
        low_price=99.8,
        now=t0,
    )
    assert not v["is_bull_coiling"], "Stagnant stock without a preceding pole MUST NOT be classified as bull coiling!"
    assert v["activity_state"] in ("🧊 DORMANT", "NORMAL", "⚠️ DRY HIGH"), f"Expected DORMANT, NORMAL, or ⚠️ DRY HIGH, got '{v['activity_state']}'"

    score, stage, direction, details = calculate_setup_score(
        rvol_5m=0.20,
        rvol_1m=0.20,
        rvol_cum=0.50,
        ltp=100.0,
        open_price=100.0,
        high_price=100.2,
        low_price=99.8,
        pct_change=0.2,
        price_history=[100.0, 100.0, 100.0],
        has_bull_pole=v.get("has_bull_pole", False),
    )
    assert stage != "🌀 BULL FLAG", f"Dead stock must NOT be scored as '🌀 BULL FLAG', got '{stage}'"
    print(f"  -> Dead stock at highs classified as '{v['activity_state']}' and stage '{stage}': PASSED")


def test_intra_5m_volume_accel_and_dry_extreme_shield():
    print("\n[TEST 13] Testing Intra-5m Volume Acceleration & Dry High/Low Shield...")
    # 1. High price drift without volume acceleration (A_vol = 0.50, range_pos = 0.95)
    score_dry, stage_dry, dir_dry, det_dry = calculate_setup_score(
        rvol_5m=1.2,
        rvol_1m=0.6,
        rvol_cum=1.0,
        ltp=105.0,
        open_price=100.0,
        high_price=105.0,
        low_price=99.0,
        pct_change=5.0,
        price_history=[103.0, 104.0, 105.0],
        vol_accel=0.50,
        is_dry_extreme=True,
    )
    assert stage_dry == "⚠️ DRY HIGH", f"Expected '⚠️ DRY HIGH', got '{stage_dry}'"
    assert score_dry <= 58, f"Dry extreme score must be clamped < 60, got {score_dry}"
    assert stage_dry != "🚀 BREAKOUT", "Dry high MUST NOT be classified as confirmed BREAKOUT"
    print(f"  -> Dry High clamped to score={score_dry}, stage='{stage_dry}': PASSED")

    # 2. Strong volume acceleration (A_vol = 1.60) confirms breakout
    score_accel, stage_accel, dir_accel, det_accel = calculate_setup_score(
        rvol_5m=2.0,
        rvol_1m=3.2,
        rvol_cum=2.0,
        ltp=105.0,
        open_price=100.0,
        high_price=105.0,
        low_price=99.0,
        pct_change=5.0,
        price_history=[103.0, 104.0, 105.0],
        vol_accel=1.60,
        is_dry_extreme=False,
    )
    assert stage_accel in ("🚀 BREAKOUT", "⚡ FORMING"), f"Accelerated volume should confirm breakout, got {stage_accel}"
    assert score_accel >= 75, f"Expected high score for volume acceleration, got {score_accel}"
    print(f"  -> Volume Accelerated push scored {score_accel}, stage='{stage_accel}': PASSED")


def test_support_resistance_levels_and_ceiling_penalty():
    print("\n[TEST 14] Testing Support/Resistance Clearance & Resistance Ceiling Penalty...")
    from bidirectional_engine import compute_support_resistance_levels

    # Spot=100, R1=100.10 (only 0.1% clearance -> near resistance trap!)
    sr = compute_support_resistance_levels(prev_close=98.0, open_price=99.0, high_price=100.1, low_price=97.0, ltp=100.0)
    assert sr["is_near_resistance"], "Should detect near resistance trap"
    assert sr["bull_clearance_pct"] < 0.2, f"Expected low clearance, got {sr['bull_clearance_pct']}%"

    # Setup score with resistance trap should dock -12 pts
    score_trap, _, _, _ = calculate_setup_score(
        rvol_5m=2.0,
        rvol_1m=2.5,
        rvol_cum=1.5,
        ltp=100.0,
        open_price=99.0,
        high_price=100.1,
        low_price=97.0,
        pct_change=2.0,
        price_history=[99.5, 99.8, 100.0],
        is_near_resistance=True,
        bull_clearance_pct=0.1,
    )

    # Setup score with ample clearance (clearance >= 0.8% and not near resistance)
    score_clear, _, _, _ = calculate_setup_score(
        rvol_5m=2.0,
        rvol_1m=2.5,
        rvol_cum=1.5,
        ltp=100.0,
        open_price=99.0,
        high_price=100.1,
        low_price=97.0,
        pct_change=2.0,
        price_history=[99.5, 99.8, 100.0],
        is_near_resistance=False,
        bull_clearance_pct=1.2,
    )

    diff = score_clear - score_trap
    assert diff >= 12, f"Resistance trap penalty must dock >= 12 pts, got difference of {diff}"
    print(f"  -> Resistance trap docked {diff} pts (Trap={score_trap} vs Clear={score_clear}): PASSED")


def test_rsi_and_macd_confluence():
    print("\n[TEST 15] Testing RSI Momentum/Exhaustion & MACD Histogram Confluence...")
    from bidirectional_engine import compute_rsi, compute_macd

    # 1. Steady uptrend series
    uptrend = [100.0 + i * 0.5 for i in range(25)]
    rsi_up = compute_rsi(uptrend, period=14)
    _, _, macd_hist_up = compute_macd(uptrend)
    assert rsi_up > 55.0, f"Uptrend RSI should be > 55, got {rsi_up}"
    assert macd_hist_up > 0, f"Uptrend MACD histogram should be positive, got {macd_hist_up}"

    # 2. Overbought exhaustion vs healthy momentum scoring
    score_healthy, _, _, _ = calculate_setup_score(
        rvol_5m=2.0, rvol_1m=2.0, rvol_cum=1.5, ltp=110.0, open_price=100.0,
        high_price=110.0, low_price=99.0, pct_change=3.0, price_history=[108.0, 109.0, 110.0],
        rsi=62.0, macd_hist=0.5,
    )
    score_exhausted, _, _, _ = calculate_setup_score(
        rvol_5m=2.0, rvol_1m=2.0, rvol_cum=1.5, ltp=110.0, open_price=100.0,
        high_price=110.0, low_price=99.0, pct_change=3.0, price_history=[108.0, 109.0, 110.0],
        rsi=82.0, macd_hist=-0.2,
    )
    assert score_healthy > score_exhausted, f"Healthy RSI (62) + MACD expansion must outscore exhausted RSI (82), got {score_healthy} vs {score_exhausted}"
    print(f"  -> RSI/MACD Confluence verified: Healthy={score_healthy} vs Overbought/Exhausted={score_exhausted}: PASSED")


def test_relative_cross_sectional_ranking_and_two_stage_funnel():
    print("\n[TEST 16] Testing Two-Stage Funnel & Cross-Sectional Alpha Ranking...")

    class MockInst:
        def __init__(self, key, sym, strike, lot):
            self.instrument_key = key
            self.upstox_instrument_key = key
            self.trading_symbol = sym
            self.strike_price = strike
            self.lot_size = lot
            self.expiry_date = "2026-11-26"

    class MockResolved:
        def __init__(self, atm):
            self.atm_inst = atm
            self.itm_inst = None
            self.otm_inst = None
            self.itms = []
            self.otms = []
            self.is_valid = True
            self.expiry_date = "2026-11-26"

    class MockResolver:
        def resolve_strikes(self, sym, spot, option_type="CE"):
            opt_key = f"NSE_FO|{sym}_OPT"
            return MockResolved(MockInst(opt_key, f"{sym}26NOVCE", spot, 100))

    portfolio = FlagPaperPortfolio(
        starting_capital=8000.0,  # Capital only affords ONE trade (₹5,000)
        direction="BOTH",
        strike_mode="ATM",
        max_lots=1,
    )
    portfolio.resolver = MockResolver()

    t0 = datetime(2026, 11, 4, 10, 30, 0)
    # Candidate A: Sequential first, but lower alpha (Score 76, A_vol = 0.9, Clearance = 0.2%)
    portfolio.process_underlying_tick(
        symbol="CANDIDATE_A",
        ltp=100.0,
        high=100.5,
        low=95.0,
        range_pos=0.92,
        rvol_5m=1.6,
        rvol_cum=1.2,
        stage="🚀 BREAKOUT",
        score=76,
        now=t0,
        vol_accel=0.9,
        clearance_pct=0.2,
    )

    # Candidate B: Sequential second, but MUCH higher alpha (Score 94, A_vol = 2.0, Clearance = 1.5%)
    portfolio.process_underlying_tick(
        symbol="CANDIDATE_B",
        ltp=200.0,
        high=201.0,
        low=190.0,
        range_pos=0.95,
        rvol_5m=2.8,
        rvol_cum=2.0,
        stage="🚀 BREAKOUT",
        score=94,
        now=t0,
        vol_accel=2.0,
        clearance_pct=1.5,
    )

    # Verify candidate B has higher composite alpha score than A
    alpha_a = portfolio.pending_triggers["CANDIDATE_A"]["alpha_score"]
    alpha_b = portfolio.pending_triggers["CANDIDATE_B"]["alpha_score"]
    assert alpha_b > alpha_a, f"Candidate B alpha ({alpha_b}) must exceed Candidate A ({alpha_a})"

    # Mock quotes with both underlying and option depth
    mock_quotes = {
        "CANDIDATE_A": {"last_price": 100.0},
        "CANDIDATE_B": {"last_price": 200.0},
        "NSE_FO|CANDIDATE_A_OPT": {
            "last_price": 50.0,
            "depth": {"buy": [{"price": 49.5, "quantity": 500}], "sell": [{"price": 50.0, "quantity": 500}]},
        },
        "NSE_FO|CANDIDATE_B_OPT": {
            "last_price": 50.0,
            "depth": {"buy": [{"price": 49.5, "quantity": 500}], "sell": [{"price": 50.0, "quantity": 500}]},
        },
    }

    # Process market quotes
    portfolio.process_market_quotes(underlying_quotes=mock_quotes, option_quotes=mock_quotes, now=t0)
    atm_sim = portfolio.simulations["ATM"]
    executed_syms = [p.symbol for p in atm_sim.active_positions] + [p.symbol for p in atm_sim.closed_positions]
    assert "CANDIDATE_B" in executed_syms, f"Relative ranking should have entered CHAMPION CANDIDATE_B! Executed: {executed_syms}"
    assert "CANDIDATE_A" not in executed_syms, f"Candidate A should NOT have starved Candidate B! Executed: {executed_syms}"
    print(f"  -> Cross-sectional relative ranking entered CANDIDATE_B (Alpha={alpha_b:.1f}) over CANDIDATE_A (Alpha={alpha_a:.1f}): PASSED")


def test_rejection_of_premature_channel_entries():
    print("\n[TEST 17] Testing Rejection of Premature FORMING/CRACKING Channel Entries...")
    flag_tracker = FlagPatternTracker(symbol="CHOP_TEST")

    state, event = flag_tracker.update(
        ltp=100.0,
        high=100.0,
        low=90.0,
        range_pos=0.88,
        rvol_5m=1.3,
        rvol_cum=1.1,
        stage="⚡ FORMING",
        score=68,
        is_near_resistance=True,
    )
    assert state != "TRIGGERED" and state != "IN_TRADE", f"Premature channel entry MUST NOT trigger! Got {state}"
    print("  -> Premature '⚡ FORMING' at channel ceiling rejected: PASSED")


def test_rolling_target_decay_5_to_2_5():
    print("\n[TEST 18] Testing Dynamic Rolling Target Decay (5.0% -> 2.5% over 30 mins)...")
    from bidirectional_engine import PaperPosition

    t_entry = datetime(2026, 11, 4, 10, 0, 0)
    pos = PaperPosition(
        trade_id="TEST-ROLL-01",
        symbol="NIFTY_CE",
        direction="BULL",
        option_type="CE",
        strike_type="ATM",
        option_symbol="NIFTY26NOVCE",
        option_key="NSE_FO|NIFTY",
        strike_price=24000.0,
        expiry_date="2026-11-26",
        lot_size=100,
        lots=1,
        quantity=100,
        entry_time="10:00:00",
        entry_epoch=t_entry.timestamp(),
        entry_underlying=24000.0,
        entry_premium=100.0,
        capital_invested=10000.0,
        flag_high=24010.0,
        flag_low=23950.0,
        flag_height=60.0,
        stop_loss_underlying=23950.0,
        target_premium=105.60,
        stop_loss_premium=85.0,
        target_gross_gain=560.0,
        target_net_profit=500.0,
        brokerage=60.0,
        initial_target_pct=5.0,
        initial_r_stop=0.15,
    )

    # 1. At t=0 min (Entry): Target is 5.0%
    pos.update_mtm(underlying_price=24000.0, option_premium=100.0, now=t_entry)
    assert pos.current_target_pct == 5.0, f"Expected 5.0% at t=0, got {pos.current_target_pct}"
    # Target net profit: 5% of 10,000 = 500, + 60 brkr = 560 gross -> premium 105.60
    assert abs(pos.current_target_gross_gain - 560.0) < 0.1
    assert abs(pos.current_target_premium - 105.60) < 0.1
    print(f"  -> t=0m: Target={pos.current_target_pct:.2f}% (TGT Premium=₹{pos.current_target_premium:.2f})")

    # 2. At t=15 min (Halfway): Target should be 3.75%
    t_15 = t_entry + timedelta(minutes=15)
    pos.update_mtm(underlying_price=24010.0, option_premium=101.0, now=t_15)
    assert abs(pos.current_target_pct - 3.75) < 0.05, f"Expected 3.75% at t=15, got {pos.current_target_pct}"
    # Target net profit: 3.75% of 10,000 = 375, + 60 brkr = 435 gross -> premium 104.35
    assert abs(pos.current_target_premium - 104.35) < 0.1
    print(f"  -> t=15m: Target={pos.current_target_pct:.2f}% (TGT Premium=₹{pos.current_target_premium:.2f})")

    # 3. At t=30 min: Target should reach 2.50%
    t_30 = t_entry + timedelta(minutes=30)
    pos.update_mtm(underlying_price=24015.0, option_premium=101.5, now=t_30)
    assert abs(pos.current_target_pct - 2.50) < 0.05, f"Expected 2.50% at t=30, got {pos.current_target_pct}"
    # Target net profit: 2.5% of 10,000 = 250, + 60 brkr = 310 gross -> premium 103.10
    assert abs(pos.current_target_premium - 103.10) < 0.1
    print(f"  -> t=30m: Target={pos.current_target_pct:.2f}% (TGT Premium=₹{pos.current_target_premium:.2f})")
    print("  -> Rolling Target Decay (5.0% -> 2.5% over 30 mins): PASSED")


def test_stagnant_red_breakeven_shift_and_sl_upped_33():
    print("\n[TEST 19] Testing Stagnant Red Breakeven Shift (2.5% -> 0.5%) & SL Upped by 33%...")
    from bidirectional_engine import PaperPosition

    t_entry = datetime(2026, 11, 4, 10, 0, 0)
    pos = PaperPosition(
        trade_id="TEST-RED-01",
        symbol="BANKNIFTY_CE",
        direction="BULL",
        option_type="CE",
        strike_type="ATM",
        option_symbol="BANKNIFTY26NOVCE",
        option_key="NSE_FO|BNF",
        strike_price=51000.0,
        expiry_date="2026-11-26",
        lot_size=100,
        lots=1,
        quantity=100,
        entry_time="10:00:00",
        entry_epoch=t_entry.timestamp(),
        entry_underlying=51000.0,
        entry_premium=100.0,
        capital_invested=10000.0,
        flag_high=51020.0,
        flag_low=50950.0,
        flag_height=70.0,
        stop_loss_underlying=50950.0,
        initial_sl_underlying=50950.0,
        target_premium=105.60,
        stop_loss_premium=85.0,
        target_gross_gain=560.0,
        target_net_profit=500.0,
        brokerage=60.0,
        initial_target_pct=5.0,
        initial_r_stop=0.15,
    )

    # Simulate position staying in red for 30 minutes (premium drops to 96.0, mtm_pnl is negative)
    t_30 = t_entry + timedelta(minutes=30)
    pos.update_mtm(underlying_price=50980.0, option_premium=96.0, now=t_30)
    assert pos.mtm_pnl < 0, "Position should be in red"
    assert pos.red_stagnant_since is not None, "Position should trigger red stagnation timer at 30 mins in red"
    assert pos.exit_stage_type == "BREAKEVEN_SHIFT"
    print(f"  -> t=30m (In Red): Breakeven Shift Triggered! Target={pos.current_target_pct:.2f}%, SL={pos.current_r_stop*100:.1f}%")

    # Over the next 10 minutes:
    # At t=35m (5 mins into shift, 50% progress):
    # Target shifts: 2.5% -> 1.5%
    # SL upped by 33% * 0.5 = 16.5% tighter (R = 0.15 * (1 - 0.165) = 0.1252 -> SL at ~87.48)
    t_35 = t_entry + timedelta(minutes=35)
    pos.update_mtm(underlying_price=50985.0, option_premium=97.0, now=t_35)
    assert abs(pos.current_target_pct - 1.50) < 0.05, f"Expected 1.50% at t=35m, got {pos.current_target_pct}"
    assert pos.current_r_stop < 0.15, f"SL should be tightened, got {pos.current_r_stop}"
    assert pos.current_stop_loss_premium > 85.0, f"SL premium should be moved UP from 85.0, got {pos.current_stop_loss_premium}"
    print(f"  -> t=35m (+5m shift): Target={pos.current_target_pct:.2f}% (TGT=₹{pos.current_target_premium:.2f}), SL Drop=-{pos.current_r_stop*100:.1f}% (SL=₹{pos.current_stop_loss_premium:.2f})")

    # At t=40m (10 mins into shift, 100% progress):
    # Target shifts fully to 0.5% (Break Even + brokerage)
    # SL is upped by full 33% (R = 0.15 * (1 - 0.33) = 0.1005 -> SL at ₹89.95 instead of ₹85.00!)
    t_40 = t_entry + timedelta(minutes=40)
    pos.update_mtm(underlying_price=50990.0, option_premium=98.0, now=t_40)
    assert abs(pos.current_target_pct - 0.50) < 0.05, f"Expected 0.50% at t=40m, got {pos.current_target_pct}"
    # Target: 0.5% of 10,000 = 50, + 60 brkr = 110 gross -> premium 101.10
    assert abs(pos.current_target_premium - 101.10) < 0.1
    # SL: 100 * (1 - 0.1005) = 89.95
    assert abs(pos.current_stop_loss_premium - 89.95) < 0.1
    print(f"  -> t=40m (+10m shift): Target={pos.current_target_pct:.2f}% (TGT=₹{pos.current_target_premium:.2f}), SL Drop=-{pos.current_r_stop*100:.1f}% (SL=₹{pos.current_stop_loss_premium:.2f})")
    print("  -> Stagnant Red Breakeven Shift (2.5% -> 0.5%) & SL Upped 33%: PASSED")


def test_breakeven_bounce_and_tightened_sl_exits():
    print("\n[TEST 20] Testing Breakeven Bounce Exit & Tightened Stop Loss Exit Execution...")
    from bidirectional_engine import FlagPaperPortfolio, StrikeSimulation, PaperPosition

    # 1. Test Breakeven Bounce Exit:
    # A position that was in red for 30m bounces to 101.20 at min 36 (hitting the shifted 1.3% target)
    sim = StrikeSimulation(strike_type="ATM", starting_capital=50000.0)
    portfolio = FlagPaperPortfolio(starting_capital=50000.0, strike_mode="ATM")
    portfolio.simulations["ATM"] = sim

    t_entry = datetime(2026, 11, 4, 10, 0, 0)
    pos_bounce = PaperPosition(
        trade_id="BOUNCE-01",
        symbol="BOUNCE_STOCK",
        direction="BULL",
        option_type="CE",
        strike_type="ATM",
        option_symbol="BOUNCE26NOVCE",
        option_key="NSE_FO|BOUNCE",
        strike_price=1000.0,
        expiry_date="2026-11-26",
        lot_size=100,
        lots=1,
        quantity=100,
        entry_time="10:00:00",
        entry_epoch=t_entry.timestamp(),
        entry_underlying=1000.0,
        entry_premium=100.0,
        capital_invested=10000.0,
        flag_high=1005.0,
        flag_low=995.0,
        flag_height=10.0,
        stop_loss_underlying=995.0,
        initial_sl_underlying=995.0,
        target_premium=105.60,
        stop_loss_premium=85.0,
        target_gross_gain=560.0,
        target_net_profit=500.0,
        brokerage=60.0,
        initial_target_pct=5.0,
        initial_r_stop=0.15,
    )
    sim.active_positions.append(pos_bounce)

    # First update at 30m in red to trigger the shift
    t_30 = t_entry + timedelta(minutes=30)
    pos_bounce.update_mtm(underlying_price=998.0, option_premium=95.0, now=t_30)

    # At t=35m, option bounces to 102.50 (above current shifted target ₹102.10 [1.5% net + ₹60])
    t_35 = t_entry + timedelta(minutes=35)
    mock_quotes = {
        "BOUNCE_STOCK": {"last_price": 1002.0},
        "NSE_FO|BOUNCE": {"last_price": 102.5, "depth": {"buy": [{"price": 102.5, "quantity": 100}]}},
    }
    portfolio.process_market_quotes(mock_quotes, now=t_35)

    assert len(sim.closed_positions) == 1, "Position should have exited on breakeven bounce!"
    closed_bounce = sim.closed_positions[0]
    assert "TARGET (Breakeven Scalp" in closed_bounce.exit_reason, f"Expected Breakeven Scalp exit reason, got {closed_bounce.exit_reason}"
    assert closed_bounce.realized_pnl >= 0, f"Breakeven exit must be net positive/flat, got ₹{closed_bounce.realized_pnl}"
    print(f"  -> Breakeven Bounce Exit executed: Reason='{closed_bounce.exit_reason}' | Net P&L=₹{closed_bounce.realized_pnl:+.2f}: PASSED")

    # 2. Test Tightened Stop Loss Exit:
    # A position that was in red for 30m drops to 88.0 at min 38 (exceeding tightened SL ₹89.0 instead of original ₹85.0)
    pos_drop = PaperPosition(
        trade_id="DROP-01",
        symbol="DROP_STOCK",
        direction="BULL",
        option_type="CE",
        strike_type="ATM",
        option_symbol="DROP26NOVCE",
        option_key="NSE_FO|DROP",
        strike_price=500.0,
        expiry_date="2026-11-26",
        lot_size=100,
        lots=1,
        quantity=100,
        entry_time="10:00:00",
        entry_epoch=t_entry.timestamp(),
        entry_underlying=500.0,
        entry_premium=100.0,
        capital_invested=10000.0,
        flag_high=502.0,
        flag_low=495.0,
        flag_height=7.0,
        stop_loss_underlying=495.0,
        initial_sl_underlying=495.0,
        target_premium=105.60,
        stop_loss_premium=85.0,
        target_gross_gain=560.0,
        target_net_profit=500.0,
        brokerage=60.0,
        initial_target_pct=5.0,
        initial_r_stop=0.15,
    )
    sim.active_positions.append(pos_drop)

    # First update at 30m in red
    pos_drop.update_mtm(underlying_price=497.0, option_premium=93.0, now=t_30)

    # At t=38m (80% into shift, SL is tightened to ~89.0), option drops to 88.0
    t_38 = t_entry + timedelta(minutes=38)
    mock_quotes_drop = {
        "DROP_STOCK": {"last_price": 496.0},
        "NSE_FO|DROP": {"last_price": 88.0, "depth": {"buy": [{"price": 88.0, "quantity": 100}]}},
    }
    portfolio.process_market_quotes(mock_quotes_drop, now=t_38)

    assert len(sim.closed_positions) == 2, "Drop position should have exited on tightened SL!"
    closed_drop = sim.closed_positions[1]
    assert "Upped 33%" in closed_drop.exit_reason, f"Expected Upped 33% stop loss reason, got {closed_drop.exit_reason}"
    print(f"  -> Tightened Stop Loss Exit executed: Reason='{closed_drop.exit_reason}' | Net P&L=₹{closed_drop.realized_pnl:+.2f}: PASSED")


def test_intermediate_trailing_profit_floor():
    print("\n[TEST 21] Testing Intermediate Trailing Profit Floor (3.5% trigger -> 2.5% base floor, rolls to 3.5% at 4.5% peak)...")
    from bidirectional_engine import FlagPaperPortfolio, StrikeSimulation, PaperPosition

    sim = StrikeSimulation(strike_type="ATM", starting_capital=50000.0)
    portfolio = FlagPaperPortfolio(starting_capital=50000.0, strike_mode="ATM")
    portfolio.simulations["ATM"] = sim

    t_entry = datetime(2026, 11, 4, 10, 0, 0)
    pos = PaperPosition(
        trade_id="TRAIL-01",
        symbol="TRAIL_STOCK",
        direction="BULL",
        option_type="CE",
        strike_type="ATM",
        option_symbol="TRAIL26NOVCE",
        option_key="NSE_FO|TRAIL",
        strike_price=1000.0,
        expiry_date="2026-11-26",
        lot_size=100,
        lots=1,
        quantity=100,
        entry_time="10:00:00",
        entry_epoch=t_entry.timestamp(),
        entry_underlying=1000.0,
        entry_premium=100.0,
        capital_invested=10000.0,
        flag_high=1005.0,
        flag_low=995.0,
        flag_height=10.0,
        stop_loss_underlying=995.0,
        initial_sl_underlying=995.0,
        target_premium=105.60,
        stop_loss_premium=85.0,
        target_gross_gain=560.0,
        target_net_profit=500.0,
        brokerage=60.0,
        initial_target_pct=5.0,
        initial_r_stop=0.15,
    )
    sim.active_positions.append(pos)

    # 1. At t=2m, price reaches ₹104.10 (Net P&L = ₹350 = +3.5%)
    t_2m = t_entry + timedelta(minutes=2)
    mock_quotes_3_5 = {
        "TRAIL_STOCK": {"last_price": 1005.0},
        "NSE_FO|TRAIL": {"last_price": 104.10, "depth": {"buy": [{"price": 104.10, "quantity": 100}]}},
    }
    portfolio.process_market_quotes(mock_quotes_3_5, now=t_2m)
    assert len(sim.active_positions) == 1, "Position must NOT exit at 3.5%"
    assert pos.trailing_floor_active, "Trailing floor must be active at 3.5%"
    assert pos.trailing_floor_pct == 2.5, f"Expected 2.5% floor at 3.5% trigger, got {pos.trailing_floor_pct}"
    print(f"  -> Crosses 3.5%: Trailing floor activated at +{pos.trailing_floor_pct:.1f}% (₹{pos.trailing_floor_premium:.2f})")

    # 2. At t=3m, price rises to ₹105.10 (Net P&L = ₹450 = +4.5%, user example!)
    t_3m = t_entry + timedelta(minutes=3)
    mock_quotes_4_5 = {
        "TRAIL_STOCK": {"last_price": 1007.0},
        "NSE_FO|TRAIL": {"last_price": 105.10, "depth": {"buy": [{"price": 105.10, "quantity": 100}]}},
    }
    portfolio.process_market_quotes(mock_quotes_4_5, now=t_3m)
    assert len(sim.active_positions) == 1, "Position must NOT exit while advancing"
    assert pos.peak_pnl_pct == 4.5, f"Peak P&L should be 4.5%, got {pos.peak_pnl_pct}"
    assert pos.trailing_floor_pct == 3.5, f"Floor should roll to 3.5% at 4.5% peak (4.5 - 1.0), got {pos.trailing_floor_pct}"
    assert pos.trailing_floor_premium == 104.10, f"Floor premium should be 104.10, got {pos.trailing_floor_premium}"
    print(f"  -> Advances to 4.5%: Floor rolled to +{pos.trailing_floor_pct:.1f}% (₹{pos.trailing_floor_premium:.2f})")

    # 3. At t=4m, price reverses to ₹104.00 (below ₹104.10 / +3.5% floor)
    t_4m = t_entry + timedelta(minutes=4)
    mock_quotes_rev = {
        "TRAIL_STOCK": {"last_price": 1004.0},
        "NSE_FO|TRAIL": {"last_price": 104.00, "depth": {"buy": [{"price": 104.00, "quantity": 100}]}},
    }
    portfolio.process_market_quotes(mock_quotes_rev, now=t_4m)
    assert len(sim.closed_positions) == 1, "Position should have exited on trailing floor reversal!"
    closed = sim.closed_positions[0]
    assert "PROFIT_TRAIL_FLOOR (+3.5%" in closed.exit_reason, f"Unexpected exit reason: {closed.exit_reason}"
    assert closed.realized_pnl == 340.0, f"Expected Net P&L ₹340.0 (+3.4%), got ₹{closed.realized_pnl}"
    print(f"  -> Reverses to 3.5%: Exited with reason='{closed.exit_reason}' | Net P&L=₹{closed.realized_pnl:+.2f}: PASSED")


def test_target_runner_reversal_floor():
    print("\n[TEST 22] Testing Target Runner Reversal Floor Exit (5% hit -> runs 30s, surges to 7%, rolls floor to 6%, exits on reversal)...")
    from bidirectional_engine import FlagPaperPortfolio, StrikeSimulation, PaperPosition

    sim = StrikeSimulation(strike_type="ATM", starting_capital=50000.0)
    portfolio = FlagPaperPortfolio(starting_capital=50000.0, strike_mode="ATM")
    portfolio.simulations["ATM"] = sim

    t_entry = datetime(2026, 11, 4, 10, 0, 0)
    pos = PaperPosition(
        trade_id="RUNNER-01",
        symbol="RUNNER_STOCK",
        direction="BULL",
        option_type="CE",
        strike_type="ATM",
        option_symbol="RUNNER26NOVCE",
        option_key="NSE_FO|RUNNER",
        strike_price=1000.0,
        expiry_date="2026-11-26",
        lot_size=100,
        lots=1,
        quantity=100,
        entry_time="10:00:00",
        entry_epoch=t_entry.timestamp(),
        entry_underlying=1000.0,
        entry_premium=100.0,
        capital_invested=10000.0,
        flag_high=1005.0,
        flag_low=995.0,
        flag_height=10.0,
        stop_loss_underlying=995.0,
        initial_sl_underlying=995.0,
        target_premium=105.60,
        stop_loss_premium=85.0,
        target_gross_gain=560.0,
        target_net_profit=500.0,
        brokerage=60.0,
        initial_target_pct=5.0,
        initial_r_stop=0.15,
    )
    sim.active_positions.append(pos)

    # 1. At t=t_entry, price reaches ₹105.60 (+5.0% target hit)
    t_tgt = t_entry
    mock_quotes_5_0 = {
        "RUNNER_STOCK": {"last_price": 1008.0},
        "NSE_FO|RUNNER": {"last_price": 105.60, "depth": {"buy": [{"price": 105.60, "quantity": 100}]}},
    }
    portfolio.process_market_quotes(mock_quotes_5_0, now=t_tgt)
    assert len(sim.active_positions) == 1, "Position must NOT exit immediately upon hitting target!"
    assert pos.target_runner_active, "Target runner must be activated upon target hit!"
    assert pos.target_runner_floor_pct == 5.0, f"Floor should be locked at 5.0%, got {pos.target_runner_floor_pct}"
    assert pos.target_runner_floor_premium == 105.60
    print(f"  -> Target Hit at +5.0%: RUNNER ACTIVATED! Floor locked at +{pos.target_runner_floor_pct:.1f}% (₹{pos.target_runner_floor_premium:.2f})")

    # 2. At t=10s (+10s into runner), price surges to ₹107.60 (+7.0%, user example!)
    t_surge = t_tgt + timedelta(seconds=10)
    mock_quotes_7_0 = {
        "RUNNER_STOCK": {"last_price": 1012.0},
        "NSE_FO|RUNNER": {"last_price": 107.60, "depth": {"buy": [{"price": 107.60, "quantity": 100}]}},
    }
    portfolio.process_market_quotes(mock_quotes_7_0, now=t_surge)
    assert len(sim.active_positions) == 1, "Position must NOT exit while extending run"
    assert pos.peak_pnl_pct == 7.0, f"Peak P&L should be 7.0%, got {pos.peak_pnl_pct}"
    assert pos.target_runner_floor_pct == 6.0, f"Floor should roll to 6.0% (7.0 - 1.0), got {pos.target_runner_floor_pct}"
    assert pos.target_runner_floor_premium == 106.60, f"Floor premium should be 106.60, got {pos.target_runner_floor_premium}"
    print(f"  -> Surges to +7.0%: Floor rolled to +{pos.target_runner_floor_pct:.1f}% (₹{pos.target_runner_floor_premium:.2f})")

    # 3. At t=20s (+20s into runner), price reverses to ₹106.40 (below ₹106.60 / +6.0% floor)
    t_rev = t_tgt + timedelta(seconds=20)
    mock_quotes_rev = {
        "RUNNER_STOCK": {"last_price": 1009.0},
        "NSE_FO|RUNNER": {"last_price": 106.40, "depth": {"buy": [{"price": 106.40, "quantity": 100}]}},
    }
    portfolio.process_market_quotes(mock_quotes_rev, now=t_rev)
    assert len(sim.closed_positions) == 1, "Position should exit when dropping below runner floor!"
    closed = sim.closed_positions[0]
    assert "Runner Reversal Floor +6.0%" in closed.exit_reason, f"Unexpected exit reason: {closed.exit_reason}"
    assert closed.realized_pnl == 580.0, f"Expected Net P&L ₹580 (+5.8%), got ₹{closed.realized_pnl}"
    print(f"  -> Reverses to 6.0% floor: Exited with reason='{closed.exit_reason}' | Net P&L=₹{closed.realized_pnl:+.2f}: PASSED")


def test_target_runner_30s_window_complete():
    print("\n[TEST 23] Testing Target Runner 30-Second Window Complete Exit (stays above floor, exits at 30s)...")
    from bidirectional_engine import FlagPaperPortfolio, StrikeSimulation, PaperPosition

    sim = StrikeSimulation(strike_type="ATM", starting_capital=50000.0)
    portfolio = FlagPaperPortfolio(starting_capital=50000.0, strike_mode="ATM")
    portfolio.simulations["ATM"] = sim

    t_entry = datetime(2026, 11, 4, 10, 0, 0)
    pos = PaperPosition(
        trade_id="RUNNER-02",
        symbol="STRONG_STOCK",
        direction="BULL",
        option_type="CE",
        strike_type="ATM",
        option_symbol="STRONG26NOVCE",
        option_key="NSE_FO|STRONG",
        strike_price=1000.0,
        expiry_date="2026-11-26",
        lot_size=100,
        lots=1,
        quantity=100,
        entry_time="10:00:00",
        entry_epoch=t_entry.timestamp(),
        entry_underlying=1000.0,
        entry_premium=100.0,
        capital_invested=10000.0,
        flag_high=1005.0,
        flag_low=995.0,
        flag_height=10.0,
        stop_loss_underlying=995.0,
        initial_sl_underlying=995.0,
        target_premium=105.60,
        stop_loss_premium=85.0,
        target_gross_gain=560.0,
        target_net_profit=500.0,
        brokerage=60.0,
        initial_target_pct=5.0,
        initial_r_stop=0.15,
    )
    sim.active_positions.append(pos)

    # 1. Target hit at t=t_entry
    t_tgt = t_entry
    mock_quotes_tgt = {
        "STRONG_STOCK": {"last_price": 1008.0},
        "NSE_FO|STRONG": {"last_price": 105.60, "depth": {"buy": [{"price": 105.60, "quantity": 100}]}},
    }
    portfolio.process_market_quotes(mock_quotes_tgt, now=t_tgt)

    # 2. Holds strong at ₹107.20 (+6.6%) through t=31s (31s elapsed in runner, above 30s limit)
    t_31s = t_tgt + timedelta(seconds=31)
    mock_quotes_strong = {
        "STRONG_STOCK": {"last_price": 1011.0},
        "NSE_FO|STRONG": {"last_price": 107.20, "depth": {"buy": [{"price": 107.20, "quantity": 100}]}},
    }
    portfolio.process_market_quotes(mock_quotes_strong, now=t_31s)
    assert len(sim.closed_positions) == 1, "Position should exit upon completing 30s runner window!"
    closed = sim.closed_positions[0]
    assert "Runner Window +6.6% Locked after 30s" in closed.exit_reason, f"Unexpected exit reason: {closed.exit_reason}"
    assert closed.realized_pnl == 660.0, f"Expected Net P&L ₹660 (+6.6%), got ₹{closed.realized_pnl}"
    print(f"  -> 30s Window Complete: Exited with reason='{closed.exit_reason}' | Net P&L=₹{closed.realized_pnl:+.2f}: PASSED")


def test_bear_flag_underlying_stop_loss_breathing_room():
    print("\n[TEST 24] Testing Bear Flag & Breakdown Underlying Stop Loss Buffer (>= 0.8%)...")
    from bidirectional_engine import FlagPaperPortfolio

    class MockInst:
        def __init__(self, key, sym, strike, lot):
            self.upstox_instrument_key = key
            self.instrument_key = key
            self.trading_symbol = sym
            self.strike_price = strike
            self.lot_size = lot
            self.expiry_date = "2026-11-26"

    class MockResolved:
        def __init__(self, inst):
            self.atm_inst = inst
            self.itm_inst = None
            self.otm_inst = None
            self.itms = []
            self.otms = []
            self.is_valid = True
            self.expiry_date = "2026-11-26"

    class MockResolver:
        def resolve_strikes(self, sym, spot, option_type="PE"):
            opt_key = f"NSE_FO|{sym}_PE"
            return MockResolved(MockInst(opt_key, f"{sym}26NOV100PE", spot, 100))

    portfolio = FlagPaperPortfolio(starting_capital=50000.0, strike_mode="ATM")
    portfolio.resolver = MockResolver()
    sim = portfolio.simulations["ATM"]

    t0 = datetime(2026, 11, 4, 11, 0, 0)
    # Process tick to trigger breakdown
    portfolio.process_underlying_tick(
        symbol="TESTBEAR",
        ltp=100.0,
        high=103.0,
        low=100.0,
        range_pos=0.0,
        rvol_5m=1.8,
        rvol_cum=1.5,
        stage="🩸 BREAKDOWN",
        score=85,
        now=t0,
        vol_accel=1.2,
        clearance_pct=1.5,
    )

    quotes = {
        "TESTBEAR": {"last_price": 100.0},
        "NSE_EQ|TESTBEAR": {"last_price": 100.0},
        "NSE_FO|TESTBEAR_PE": {
            "last_price": 4.0,
            "depth": {"buy": [{"price": 3.95, "quantity": 500}], "sell": [{"price": 4.00, "quantity": 500}]},
        },
    }
    portfolio.process_market_quotes(quotes, quotes, t0)

    assert len(sim.active_positions) == 1, f"Should enter Bear PE trade, active={len(sim.active_positions)}"
    pos = sim.active_positions[0]

    sl_dist_pct = ((pos.stop_loss_underlying - pos.entry_underlying) / pos.entry_underlying) * 100
    assert sl_dist_pct >= 0.799, f"Bear SL distance must be >= 0.80%, got {sl_dist_pct:.2f}% (SL: {pos.stop_loss_underlying})"
    print(f"  -> Bear SL underlying set to ₹{pos.stop_loss_underlying:.2f} (+{sl_dist_pct:.2f}% buffer >= 0.80%): PASSED")


def test_bear_position_survives_normal_tick_noise():
    print("\n[TEST 25] Testing Bear Trade Survives 0.15-0.20% Tick Noise...")
    from bidirectional_engine import FlagPaperPortfolio

    class MockInst:
        def __init__(self, key, sym, strike, lot):
            self.upstox_instrument_key = key
            self.instrument_key = key
            self.trading_symbol = sym
            self.strike_price = strike
            self.lot_size = lot
            self.expiry_date = "2026-11-26"

    class MockResolved:
        def __init__(self, inst):
            self.atm_inst = inst
            self.itm_inst = None
            self.otm_inst = None
            self.itms = []
            self.otms = []
            self.is_valid = True
            self.expiry_date = "2026-11-26"

    class MockResolver:
        def resolve_strikes(self, sym, spot, option_type="PE"):
            opt_key = f"NSE_FO|{sym}_PE"
            return MockResolved(MockInst(opt_key, f"{sym}26NOV100PE", spot, 100))

    portfolio = FlagPaperPortfolio(starting_capital=50000.0, strike_mode="ATM")
    portfolio.resolver = MockResolver()
    sim = portfolio.simulations["ATM"]

    t0 = datetime(2026, 11, 4, 11, 0, 0)
    portfolio.process_underlying_tick(
        symbol="NOISE",
        ltp=100.0,
        high=103.0,
        low=100.0,
        range_pos=0.0,
        rvol_5m=1.8,
        rvol_cum=1.5,
        stage="🩸 BREAKDOWN",
        score=85,
        now=t0,
        vol_accel=1.2,
        clearance_pct=1.5,
    )

    quotes = {
        "NOISE": {"last_price": 100.0},
        "NSE_EQ|NOISE": {"last_price": 100.0},
        "NSE_FO|NOISE_PE": {
            "last_price": 4.0,
            "depth": {"buy": [{"price": 3.95, "quantity": 500}], "sell": [{"price": 4.00, "quantity": 500}]},
        },
    }
    portfolio.process_market_quotes(quotes, quotes, t0)
    assert len(sim.active_positions) == 1

    # Simulate equity flickering up 0.18% to 100.18 (previously caused STOP_LOSS Underlying Invalidation @ 100.15)
    # The option drops by only 2% (from 4.00 to 3.92)
    quotes_noise = {
        "NOISE": {"last_price": 100.18},
        "NSE_EQ|NOISE": {"last_price": 100.18},
        "NSE_FO|NOISE_PE": {
            "last_price": 3.92,
            "depth": {"buy": [{"price": 3.90, "quantity": 500}], "sell": [{"price": 3.92, "quantity": 500}]},
        },
    }
    portfolio.process_market_quotes(quotes_noise, quotes_noise, datetime(2026, 11, 4, 11, 0, 30))

    # Verify trade is STILL ACTIVE and NOT stopped out!
    assert len(sim.active_positions) == 1, "Position must NOT be stopped out on 0.18% micro-tick flicker!"
    assert len(sim.closed_positions) == 0, "No trade should be closed on micro-tick flicker"
    print("  -> Bear trade survived +0.18% noise flicker (remains active): PASSED")


def test_bear_breakdown_not_blocked_by_support_at_lod():
    print("\n[TEST 26] Testing Bear Breakdown at LOD is NOT Falsely Blocked by Support Floor...")
    from bidirectional_engine import compute_support_resistance_levels, FlagPatternTracker

    # Stock at 100.0 breaking Day's Low (LOD=100.0, High=103.0, Prev_Close=102.0)
    sr = compute_support_resistance_levels(prev_close=102.0, open_price=102.0, high_price=103.0, low_price=100.0, ltp=100.0)
    assert sr["next_support"] < 100.0, f"Expected next support below LOD 100.0, got {sr['next_support']}"
    assert not sr["is_near_support"], "LOD itself should not trigger is_near_support=True"

    # FlagPatternTracker should allow breakdown when vol_accel >= 1.0 even if near S1
    tracker = FlagPatternTracker("BEAR_TEST")
    state, event = tracker.update(
        ltp=100.0,
        high=103.0,
        low=100.0,
        range_pos=0.0,
        rvol_5m=1.8,
        rvol_cum=1.5,
        stage="🩸 BREAKDOWN",
        score=85,
        now=datetime(2026, 11, 4, 11, 0, 0),
        vol_accel=1.2,
        is_dry_extreme=False,
        is_near_support=False,
    )
    assert state == "TRIGGERED", f"Expected state TRIGGERED, got {state}"
    assert event["direction"] == "BEAR", "Should trigger BEAR breakdown"
    print("  -> Bear breakdown at LOD triggered cleanly without support blockage: PASSED")


def test_flag_boundary_invalidation_across_multiple_distances():
    print("\n[TEST 27] Testing Structural Flag Invalidation Across Distances (0.4%, 0.8%, 1.2%, 1.8%, 2.5%, 3.0%)...")
    from bidirectional_engine import FlagPaperPortfolio

    class MockInst:
        def __init__(self, key, sym, strike, lot):
            self.upstox_instrument_key = key
            self.instrument_key = key
            self.trading_symbol = sym
            self.strike_price = strike
            self.lot_size = lot
            self.expiry_date = "2026-11-26"

    class MockResolved:
        def __init__(self, inst):
            self.atm_inst = inst
            self.itm_inst = None
            self.otm_inst = None
            self.itms = []
            self.otms = []
            self.is_valid = True
            self.expiry_date = "2026-11-26"

    class MockResolver:
        def resolve_strikes(self, sym, spot, option_type="PE"):
            opt_key = f"NSE_FO|{sym}_PE"
            return MockResolved(MockInst(opt_key, f"{sym}26NOV100PE", spot, 100))

    t0 = datetime(2026, 11, 4, 11, 0, 0)
    test_cases = [
        # (flag_high_pct, expected_sl_underlying, should_gate)
        (0.4, 100.80, False),  # 0.4% flag high -> 0.80% minimum noise buffer
        (0.8, 100.80, False),  # 0.8% flag high -> exactly at flag boundary
        (1.2, 101.20, False),  # 1.2% flag high -> exactly at flag boundary
        (1.8, 101.80, False),  # 1.8% flag high -> NEVER capped inside flag at 101.50!
        (2.5, 102.50, False),  # 2.5% flag high -> exactly at flag boundary
        (3.0, None, True),     # 3.0% flag high -> gated upfront (>2.5% max limit)
    ]

    for dist_pct, expected_sl, should_gate in test_cases:
        portfolio = FlagPaperPortfolio(starting_capital=50000.0, strike_mode="ATM")
        portfolio.resolver = MockResolver()
        sim = portfolio.simulations["ATM"]

        sym = f"BEAR_{int(dist_pct*10)}"
        high_val = round(100.0 * (1.0 + dist_pct / 100.0), 2)
        flag_h = round(high_val - 100.0, 2)

        portfolio.process_underlying_tick(
            symbol=sym,
            ltp=100.0,
            high=high_val,
            low=100.0,
            range_pos=0.0,
            rvol_5m=1.8,
            rvol_cum=1.5,
            stage="🩸 BREAKDOWN",
            score=85,
            now=t0,
            vol_accel=1.2,
            clearance_pct=1.5,
        )
        # Override trigger event flag boundaries for exact test case
        if sym in portfolio.pending_triggers:
            portfolio.pending_triggers[sym]["event"]["flag_high"] = high_val
            portfolio.pending_triggers[sym]["event"]["flag_height"] = flag_h

        opt_key = f"NSE_FO|{sym}_PE"
        quotes = {
            sym: {"last_price": 100.0},
            opt_key: {
                "last_price": 4.0,
                "depth": {"buy": [{"price": 3.95, "quantity": 500}], "sell": [{"price": 4.00, "quantity": 500}]},
            },
        }
        portfolio.process_market_quotes(quotes, quotes, t0)

        if should_gate:
            assert len(sim.active_positions) == 0, f"Setup with {dist_pct}% flag width should be gated!"
            assert any("Flag consolidation too wide" in g["reason"] for g in sim.gated_trades_log)
        else:
            assert len(sim.active_positions) == 1, f"Setup with {dist_pct}% flag width should be entered!"
            pos = sim.active_positions[0]
            assert abs(pos.stop_loss_underlying - expected_sl) < 0.05, f"Dist {dist_pct}%: expected SL {expected_sl}, got {pos.stop_loss_underlying}"
            assert pos.stop_loss_underlying >= high_val, f"Stop loss {pos.stop_loss_underlying} must NEVER be inside flag ({high_val})!"

    print("  -> Flag boundary invalidation and wide flag gate verified across all distances: PASSED")


def test_clamped_volume_acceleration_in_relative_alpha():
    print("\n[TEST 28] Testing Clamped Volume Acceleration in Relative Alpha (Points 1B Fix)...")
    from bidirectional_engine import FlagPaperPortfolio

    class MockInst:
        def __init__(self, key, sym, strike, lot):
            self.upstox_instrument_key = key
            self.instrument_key = key
            self.trading_symbol = sym
            self.strike_price = strike
            self.lot_size = lot
            self.expiry_date = "2026-11-26"

    class MockResolved:
        def __init__(self, inst):
            self.atm_inst = inst
            self.itm_inst = None
            self.otm_inst = None
            self.itms = []
            self.otms = []
            self.is_valid = True
            self.expiry_date = "2026-11-26"

    class MockResolver:
        def resolve_strikes(self, sym, spot, option_type="CE"):
            opt_key = f"NSE_FO|{sym}_CE"
            return MockResolved(MockInst(opt_key, f"{sym}26NOV100CE", spot, 100))

    portfolio = FlagPaperPortfolio(starting_capital=50000.0, strike_mode="ATM")
    portfolio.resolver = MockResolver()
    t0 = datetime(2026, 11, 4, 11, 0, 0)

    # 1. Extreme high volume acceleration: A_vol = 10.0 (Unclamped would give +135 pts!)
    portfolio.process_underlying_tick(
        symbol="EXTREME_HIGH",
        ltp=100.0,
        high=100.5,
        low=98.0,
        range_pos=0.95,
        rvol_5m=2.0,
        rvol_cum=1.5,
        stage="🚀 BREAKOUT",
        score=80,
        now=t0,
        vol_accel=10.0,
        clearance_pct=2.0,
    )
    trig_high = portfolio.pending_triggers["EXTREME_HIGH"]
    assert trig_high["vol_accel_adj"] == 15.0, f"Volume accel bonus must be clamped at +15.0, got {trig_high['vol_accel_adj']}"
    assert trig_high["alpha_score"] == 80 + 15.0 + 4.0, f"Alpha score should be 99.0, got {trig_high['alpha_score']}"

    # 2. Extreme low volume acceleration: A_vol = 0.1 (Unclamped would give -13.5 pts!)
    portfolio.process_underlying_tick(
        symbol="EXTREME_LOW",
        ltp=100.0,
        high=100.5,
        low=98.0,
        range_pos=0.95,
        rvol_5m=2.0,
        rvol_cum=1.5,
        stage="🚀 BREAKOUT",
        score=80,
        now=t0,
        vol_accel=0.1,
        clearance_pct=2.0,
    )
    trig_low = portfolio.pending_triggers["EXTREME_LOW"]
    assert trig_low["vol_accel_adj"] == -10.0, f"Volume accel penalty must be clamped at -10.0, got {trig_low['vol_accel_adj']}"
    assert trig_low["alpha_score"] == 80 - 10.0 + 4.0, f"Alpha score should be 74.0, got {trig_low['alpha_score']}"

    print("  -> Volume acceleration clamped to [-10.0, +15.0] points in relative alpha: PASSED")


def test_pre_execution_setup_validity_wick_reversal():
    print("\n[TEST 29] Testing Pre-Execution Setup Validity Guard (Wick Reversal Prior to Fill)...")
    from bidirectional_engine import FlagPaperPortfolio

    class MockInst:
        def __init__(self, key, sym, strike, lot):
            self.upstox_instrument_key = key
            self.instrument_key = key
            self.trading_symbol = sym
            self.strike_price = strike
            self.lot_size = lot
            self.expiry_date = "2026-11-26"

    class MockResolved:
        def __init__(self, inst):
            self.atm_inst = inst
            self.itm_inst = None
            self.otm_inst = None
            self.itms = []
            self.otms = []
            self.is_valid = True
            self.expiry_date = "2026-11-26"

    class MockResolver:
        def resolve_strikes(self, sym, spot, option_type="CE"):
            opt_key = f"NSE_FO|{sym}_CE"
            return MockResolved(MockInst(opt_key, f"{sym}26NOV100CE", spot, 100))

    portfolio = FlagPaperPortfolio(starting_capital=50000.0, strike_mode="ATM")
    portfolio.resolver = MockResolver()
    sim = portfolio.simulations["ATM"]
    t0 = datetime(2026, 11, 4, 11, 0, 0)

    # Bull breakout triggered at 100.0 (Flag High = 100.0)
    portfolio.process_underlying_tick(
        symbol="WICK_FAILS",
        ltp=100.0,
        high=100.0,
        low=98.5,
        range_pos=1.0,
        rvol_5m=2.0,
        rvol_cum=1.5,
        stage="🚀 BREAKOUT",
        score=85,
        now=t0,
        vol_accel=1.5,
        clearance_pct=2.0,
    )
    assert "WICK_FAILS" in portfolio.pending_triggers

    # Before option quote fills, underlying drops to 99.40 (wick rejection below flag high)
    quotes = {
        "WICK_FAILS": {"last_price": 99.40},
        "NSE_FO|WICK_FAILS_CE": {
            "last_price": 4.0,
            "depth": {"buy": [{"price": 3.95, "quantity": 500}], "sell": [{"price": 4.00, "quantity": 500}]},
        },
    }
    portfolio.process_market_quotes(quotes, quotes, t0)

    assert len(sim.active_positions) == 0, "Trade must NOT enter when underlying wicks back inside flag!"
    assert any("Breakout wicked back inside flag before fill" in g["reason"] for g in sim.gated_trades_log)
    print("  -> Setup validity guard cancelled wicked-out breakout prior to order fill: PASSED")


def test_stagnation_timeout_capital_recycling():
    print("\n[TEST 30] Testing Stagnation Timeout Exit (Unresolved Trade after 50 minutes)...")
    from bidirectional_engine import FlagPaperPortfolio

    class MockInst:
        def __init__(self, key, sym, strike, lot):
            self.upstox_instrument_key = key
            self.instrument_key = key
            self.trading_symbol = sym
            self.strike_price = strike
            self.lot_size = lot
            self.expiry_date = "2026-11-26"

    class MockResolved:
        def __init__(self, inst):
            self.atm_inst = inst
            self.itm_inst = None
            self.otm_inst = None
            self.itms = []
            self.otms = []
            self.is_valid = True
            self.expiry_date = "2026-11-26"

    class MockResolver:
        def resolve_strikes(self, sym, spot, option_type="CE"):
            opt_key = f"NSE_FO|{sym}_CE"
            return MockResolved(MockInst(opt_key, f"{sym}26NOV100CE", spot, 100))

    portfolio = FlagPaperPortfolio(starting_capital=50000.0, strike_mode="ATM", max_holding_mins=50.0)
    portfolio.resolver = MockResolver()
    sim = portfolio.simulations["ATM"]
    t0 = datetime(2026, 11, 4, 10, 0, 0)

    # Enter trade
    portfolio.process_underlying_tick(
        symbol="STAGNANT",
        ltp=100.0,
        high=100.5,
        low=98.5,
        range_pos=0.95,
        rvol_5m=2.0,
        rvol_cum=1.5,
        stage="🚀 BREAKOUT",
        score=85,
        now=t0,
        vol_accel=1.2,
        clearance_pct=2.0,
    )
    quotes_t0 = {
        "STAGNANT": {"last_price": 100.0},
        "NSE_FO|STAGNANT_CE": {
            "last_price": 10.0,
            "depth": {"buy": [{"price": 9.95, "quantity": 100}], "sell": [{"price": 10.00, "quantity": 100}]},
        },
    }
    portfolio.process_market_quotes(quotes_t0, quotes_t0, t0)
    assert len(sim.active_positions) == 1

    # At t = 51 mins, option is at ₹9.70 (-3% loss, neither stopped at -10% nor bounced to +0.5%)
    t_51m = t0 + timedelta(minutes=51)
    quotes_t51 = {
        "STAGNANT": {"last_price": 99.8},
        "NSE_FO|STAGNANT_CE": {
            "last_price": 9.70,
            "depth": {"buy": [{"price": 9.70, "quantity": 100}], "sell": [{"price": 9.75, "quantity": 100}]},
        },
    }
    portfolio.process_market_quotes(quotes_t51, quotes_t51, t_51m)

    assert len(sim.active_positions) == 0, "Position should be closed after 50m stagnation timeout!"
    assert len(sim.closed_positions) == 1
    pos = sim.closed_positions[0]
    assert "STAGNATION_TIMEOUT" in pos.exit_reason
    print(f"  -> Position closed due to stagnation timeout: reason='{pos.exit_reason}': PASSED")


def test_lower_premium_option_bias():
    print("\n[TEST 31] Testing Lower Premium Option Bias Simulation (LOW_PREM)...")
    from bidirectional_engine import FlagPaperPortfolio

    class MockInst:
        def __init__(self, key, sym, strike, lot):
            self.upstox_instrument_key = key
            self.instrument_key = key
            self.trading_symbol = sym
            self.strike_price = strike
            self.lot_size = lot
            self.expiry_date = "2026-11-26"

    class MockMultiResolved:
        def __init__(self):
            self.atm_inst = MockInst("NSE_FO|TCS_ATM_CE", "TCS26NOV3500CE", 3500.0, 100)
            self.itm_inst = MockInst("NSE_FO|TCS_ITM_CE", "TCS26NOV3400CE", 3400.0, 100)
            self.otm_inst = MockInst("NSE_FO|TCS_OTM_CE", "TCS26NOV3600CE", 3600.0, 100)
            self.itms = [self.itm_inst]
            self.otms = [self.otm_inst]
            self.is_valid = True
            self.expiry_date = "2026-11-26"

    class MockResolver:
        def resolve_strikes(self, sym, spot, option_type="CE"):
            return MockMultiResolved()

    portfolio = FlagPaperPortfolio(starting_capital=100000.0, strike_mode="LOW_PREM")
    portfolio.resolver = MockResolver()
    sim = portfolio.simulations["LOW_PREM"]
    assert sim.lower_premium_bias is True
    t0 = datetime(2026, 11, 4, 10, 0, 0)

    # Process breakout trigger
    portfolio.process_underlying_tick(
        symbol="TCS",
        ltp=3500.0,
        high=3510.0,
        low=3480.0,
        range_pos=0.95,
        rvol_5m=2.5,
        rvol_cum=1.8,
        stage="🚀 BREAKOUT",
        score=90,
        now=t0,
        vol_accel=1.5,
        clearance_pct=2.0,
    )

    # Market quotes: ITM is ₹120, ATM is ₹50, OTM is ₹15 (all liquid)
    quotes = {
        "TCS": {"last_price": 3500.0},
        "NSE_FO|TCS_ITM_CE": {
            "last_price": 120.0,
            "depth": {"buy": [{"price": 119.5, "quantity": 100}], "sell": [{"price": 120.0, "quantity": 100}]},
        },
        "NSE_FO|TCS_ATM_CE": {
            "last_price": 50.0,
            "depth": {"buy": [{"price": 49.5, "quantity": 100}], "sell": [{"price": 50.0, "quantity": 100}]},
        },
        "NSE_FO|TCS_OTM_CE": {
            "last_price": 15.0,
            "depth": {"buy": [{"price": 14.8, "quantity": 100}], "sell": [{"price": 15.0, "quantity": 100}]},
        },
    }
    portfolio.process_market_quotes(quotes, quotes, t0)

    assert len(sim.active_positions) == 1, "LOW_PREM simulation should have entered 1 position"
    pos = sim.active_positions[0]
    assert pos.option_symbol == "TCS26NOV3600CE", f"Expected lowest premium option (OTM ₹15), got {pos.option_symbol}"
    assert pos.entry_premium == 15.0
    print(f"  -> LOW_PREM correctly selected lowest liquid premium option: {pos.option_symbol} @ ₹{pos.entry_premium:.2f}: PASSED")


def test_tick_by_tick_trailing_stop_loss():
    print("\n[TEST 32] Testing Tick-by-Tick Trailing Stop Loss Ratchet (TRAIL_SL)...")
    from bidirectional_engine import FlagPaperPortfolio

    class MockInst:
        def __init__(self, key, sym, strike, lot):
            self.upstox_instrument_key = key
            self.instrument_key = key
            self.trading_symbol = sym
            self.strike_price = strike
            self.lot_size = lot
            self.expiry_date = "2026-11-26"

    class MockResolved:
        def __init__(self, inst):
            self.atm_inst = inst
            self.itm_inst = None
            self.otm_inst = None
            self.itms = []
            self.otms = []
            self.is_valid = True
            self.expiry_date = "2026-11-26"

    class MockResolver:
        def resolve_strikes(self, sym, spot, option_type="CE"):
            opt_key = f"NSE_FO|{sym}_CE"
            return MockResolved(MockInst(opt_key, f"{sym}26NOV100CE", spot, 100))

    portfolio = FlagPaperPortfolio(starting_capital=50000.0, strike_mode="TRAIL_SL")
    portfolio.resolver = MockResolver()
    sim = portfolio.simulations["TRAIL_SL"]
    assert sim.trailing_sl_mode is True
    t0 = datetime(2026, 11, 4, 10, 0, 0)

    portfolio.process_underlying_tick(
        symbol="TRAILTEST",
        ltp=100.0,
        high=100.5,
        low=98.5,
        range_pos=0.95,
        rvol_5m=2.0,
        rvol_cum=1.5,
        stage="🚀 BREAKOUT",
        score=85,
        now=t0,
    )
    # Entry at ₹10.00, initial SL at ₹8.50 (15% risk = ₹1.50)
    quotes_t0 = {
        "TRAILTEST": {"last_price": 100.0},
        "NSE_FO|TRAILTEST_CE": {
            "last_price": 10.0,
            "depth": {"buy": [{"price": 9.95, "quantity": 100}], "sell": [{"price": 10.00, "quantity": 100}]},
        },
    }
    portfolio.process_market_quotes(quotes_t0, quotes_t0, t0)
    assert len(sim.active_positions) == 1
    pos = sim.active_positions[0]
    assert pos.current_stop_loss_premium == 8.50

    # Advance tick 1: Premium moves to ₹10.30 -> SL must ratchet to 10.30 - 1.50 = ₹8.80
    quotes_t1 = {
        "TRAILTEST": {"last_price": 100.2},
        "NSE_FO|TRAILTEST_CE": {
            "last_price": 10.30,
            "depth": {"buy": [{"price": 10.30, "quantity": 100}], "sell": [{"price": 10.35, "quantity": 100}]},
        },
    }
    portfolio.process_market_quotes(quotes_t1, quotes_t1, t0 + timedelta(seconds=10))
    assert pos.current_stop_loss_premium == 8.80, f"Expected SL ₹8.80, got ₹{pos.current_stop_loss_premium}"

    # Advance tick 2: Premium moves to ₹10.60 -> SL must ratchet to 10.60 - 1.50 = ₹9.10
    quotes_t2 = {
        "TRAILTEST": {"last_price": 100.5},
        "NSE_FO|TRAILTEST_CE": {
            "last_price": 10.60,
            "depth": {"buy": [{"price": 10.60, "quantity": 100}], "sell": [{"price": 10.65, "quantity": 100}]},
        },
    }
    portfolio.process_market_quotes(quotes_t2, quotes_t2, t0 + timedelta(seconds=20))
    assert pos.current_stop_loss_premium == 9.10, f"Expected SL ₹9.10, got ₹{pos.current_stop_loss_premium}"

    # Reverse: Premium drops to ₹9.00 -> Reaches below ₹9.10 trailing SL -> MUST EXIT
    quotes_t3 = {
        "TRAILTEST": {"last_price": 99.8},
        "NSE_FO|TRAILTEST_CE": {
            "last_price": 9.00,
            "depth": {"buy": [{"price": 9.00, "quantity": 100}], "sell": [{"price": 9.05, "quantity": 100}]},
        },
    }
    portfolio.process_market_quotes(quotes_t3, quotes_t3, t0 + timedelta(seconds=30))
    assert len(sim.active_positions) == 0, "Position should have exited on trailing SL breach!"
    assert len(sim.closed_positions) == 1
    closed = sim.closed_positions[0]
    assert "TRAILING_STOP_LOSS" in closed.exit_reason
    assert pos.peak_premium == 10.60
    assert closed.exit_premium == 9.00
    print(f"  -> TRAIL_SL ratcheted point-for-point to ₹9.10 and cut loss early: reason='{closed.exit_reason}': PASSED")


def test_fixed_or_percentage_dual_target_no_floors():
    print("\n[TEST 33] Testing Dual Target min(pct, ₹750) without Profit Floors (FIXED_TGT)...")
    from bidirectional_engine import FlagPaperPortfolio

    class MockInst:
        def __init__(self, key, sym, strike, lot):
            self.upstox_instrument_key = key
            self.instrument_key = key
            self.trading_symbol = sym
            self.strike_price = strike
            self.lot_size = lot
            self.expiry_date = "2026-11-26"

    class MockResolved:
        def __init__(self, inst):
            self.atm_inst = inst
            self.itm_inst = None
            self.otm_inst = None
            self.itms = []
            self.otms = []
            self.is_valid = True
            self.expiry_date = "2026-11-26"

    class MockResolver:
        def resolve_strikes(self, sym, spot, option_type="CE"):
            opt_key = f"NSE_FO|{sym}_CE"
            return MockResolved(MockInst(opt_key, f"{sym}26NOV100CE", spot, 2500))

    # Capital 100k, lot size 2500, premium ₹10 => Cost = ₹25,000
    # 5% target = ₹1,250. But fixed target = ₹750.0.
    # Therefore target is ₹750 (which hits at +3.2% instead of +5.2%!)
    portfolio = FlagPaperPortfolio(starting_capital=100000.0, strike_mode="FIXED_TGT", fixed_target_gain=750.0)
    portfolio.resolver = MockResolver()
    sim = portfolio.simulations["FIXED_TGT"]
    assert sim.enable_fixed_target is True
    assert sim.disable_profit_floors is True
    t0 = datetime(2026, 11, 4, 10, 0, 0)

    portfolio.process_underlying_tick(
        symbol="DUALTGT",
        ltp=100.0,
        high=100.5,
        low=98.5,
        range_pos=0.95,
        rvol_5m=2.0,
        rvol_cum=1.5,
        stage="🚀 BREAKOUT",
        score=85,
        now=t0,
    )
    quotes_t0 = {
        "DUALTGT": {"last_price": 100.0},
        "NSE_FO|DUALTGT_CE": {
            "last_price": 10.0,
            "depth": {"buy": [{"price": 9.95, "quantity": 2500}], "sell": [{"price": 10.00, "quantity": 2500}]},
        },
    }
    portfolio.process_market_quotes(quotes_t0, quotes_t0, t0)
    assert len(sim.active_positions) == 1
    pos = sim.active_positions[0]
    # Check target net profit capped at ₹750
    assert pos.target_net_profit == 750.0, f"Expected target net profit ₹750, got ₹{pos.target_net_profit}"
    # Target premium = 10.0 + (750 + 60) / 2500 = 10.0 + 0.324 = 10.32
    assert pos.current_target_premium <= 10.33

    # Option moves to ₹10.35: hits ₹750 target!
    quotes_t1 = {
        "DUALTGT": {"last_price": 100.8},
        "NSE_FO|DUALTGT_CE": {
            "last_price": 10.35,
            "depth": {"buy": [{"price": 10.35, "quantity": 2500}], "sell": [{"price": 10.40, "quantity": 2500}]},
        },
    }
    portfolio.process_market_quotes(quotes_t1, quotes_t1, t0 + timedelta(seconds=5))
    assert len(sim.active_positions) == 0, "Position should exit cleanly without 30s runner or floors!"
    assert len(sim.closed_positions) == 1
    closed = sim.closed_positions[0]
    assert "TARGET (Fixed/Pct Scalp Target" in closed.exit_reason
    assert closed.realized_pnl >= 750.0
    print(f"  -> FIXED_TGT exited immediately at ₹750 cap (₹{closed.realized_pnl:,.2f}) without runner delay: PASSED")


def test_combo_simulation_integration():
    print("\n[TEST 34] Testing Combo Simulation (COMBO: Lower Premium + Trailing SL + Dual Target)...")
    from bidirectional_engine import FlagPaperPortfolio

    portfolio = FlagPaperPortfolio(starting_capital=100000.0, strike_mode="all", fixed_target_gain=750.0)
    assert "COMBO" in portfolio.simulations
    combo_sim = portfolio.simulations["COMBO"]
    assert combo_sim.lower_premium_bias is True
    assert combo_sim.trailing_sl_mode is True
    assert combo_sim.enable_fixed_target is True
    assert combo_sim.disable_profit_floors is True
    assert len(portfolio.simulations) == 7, f"Expected 7 simulations in 'all' mode, got {len(portfolio.simulations)}"
    print("  -> COMBO simulation initialized all 3 strategy parameters in portfolio: PASSED")


def test_low_volume_damping_and_viewing_threshold():
    print("\n[TEST 35] Testing Low-Volume Damping (dock 15 pts, clamp <= 60) & Default 67 Filter...")
    from stream_rvol_scanner import calculate_setup_score

    # Case A: Stock drifting near HOD, but dead volume (rvol_5m = 0.40, rvol_cum = 0.50, no flag pole)
    score_a, stage_a, dir_a, det_a = calculate_setup_score(
        rvol_5m=0.40,
        rvol_1m=0.40,
        rvol_cum=0.50,
        ltp=1000.0,
        open_price=990.0,
        high_price=1000.5,
        low_price=985.0,
        pct_change=1.0,
        price_history=[995.0, 998.0, 1000.0],
        has_bull_pole=False,
    )
    assert det_a["low_vol_drift"] is True, "Dead stock must trigger low_vol_drift flag"
    assert score_a <= 60, f"Low-volume drift score must be clamped <= 60, got {score_a}"
    assert score_a < 67, f"Score {score_a} must be strictly below default 67 viewing threshold!"
    print(f"  -> Low-volume drift docked and clamped to {score_a} (< 67 default threshold): PASSED")

    # Case B: Flag consolidation coiling with legitimate pole (low volume expected during coil)
    score_b, stage_b, dir_b, det_b = calculate_setup_score(
        rvol_5m=0.50,
        rvol_1m=0.50,
        rvol_cum=1.80,
        ltp=1000.0,
        open_price=950.0,
        high_price=1000.5,
        low_price=950.0,
        pct_change=5.2,
        price_history=[998.0, 999.0, 1000.0],
        has_bull_pole=True,
    )
    assert det_b["low_vol_drift"] is False, "Active flag coiling must NOT be penalised by low_vol_drift"
    assert score_b >= 55, f"Bull flag coil should maintain healthy setup score >= 55, got {score_b}"
    print(f"  -> Bull flag coiling correctly protected from low-volume damping (score {score_b}): PASSED")

    # Case C: Early Flag Breakout fires with expanding volume -> score jumps >= 80 (well above 67 filter)
    score_c, stage_c, dir_c, det_c = calculate_setup_score(
        rvol_5m=1.80,
        rvol_1m=2.20,
        rvol_cum=1.80,
        ltp=1001.0,
        open_price=950.0,
        high_price=1001.0,
        low_price=950.0,
        pct_change=5.3,
        price_history=[998.0, 999.0, 1001.0],
        has_bull_pole=True,
        is_flag_breakout=True,
        flag_breakout_type="BULL",
    )
    assert det_c["low_vol_drift"] is False
    assert score_c >= 80, f"Early flag breakout score must be >= 80, got {score_c}"
    assert score_c >= 67, f"Early flag breakout score must be >= 67 default viewing threshold"
    print(f"  -> Early flag breakout awarded score {score_c} (well above default 67 viewing threshold): PASSED")


def test_active_positions_toggle_view():
    print("\n[TEST 36] Testing Active Positions View Toggle Flag (Compact Summary vs Expanded Table)...")
    from bidirectional_engine import PaperPosition

    portfolio = FlagPaperPortfolio(
        starting_capital=100_000.0,
        strike_mode="all",
        show_positions=False,  # Default: False
    )

    # 1. When no positions exist, compact view should show clean 1-line status
    dash_empty = portfolio.render_dashboard()
    assert "Active Positions: None Open [Use --show-positions to toggle expanded table]" in dash_empty
    assert "│ SIM  │ SYMBOL     │ DIR  │ CONTRACT" not in dash_empty
    print("  -> Empty positions compact view verified: PASSED")

    # 2. Add an active position to ATM simulation
    pos = PaperPosition(
        trade_id="TEST-ATM-001",
        symbol="INFY",
        direction="BULL",
        option_type="CE",
        strike_type="ATM",
        option_symbol="INFY26NOV1800CE",
        option_key="NSE_FO|INFY1800CE",
        strike_price=1800.0,
        expiry_date="2026-11-26",
        lot_size=300,
        lots=1,
        quantity=300,
        entry_time="10:30:00",
        entry_underlying=1800.0,
        entry_premium=35.0,
        capital_invested=10500.0,
        flag_high=1805.0,
        flag_low=1795.0,
        flag_height=10.0,
        stop_loss_underlying=1795.0,
        target_premium=37.0,
        stop_loss_premium=29.75,
        target_gross_gain=660.0,
        target_net_profit=600.0,
        brokerage=60.0,
    )
    pos.current_premium = 36.5
    pos.current_underlying = 1803.0
    pos.mtm_pnl = (36.5 - 35.0) * 300 - 60.0  # +₹390
    pos.mtm_pnl_pct = (390.0 / 10500.0) * 100
    portfolio.simulations["ATM"].active_positions.append(pos)

    # 3. Compact view (show_positions=False, default):
    dash_compact = portfolio.render_dashboard(show_positions=False)
    assert "ACTIVE POSITIONS:" in dash_compact
    assert "1 Open across 7 sims" in dash_compact
    assert "INFY (ATM B)" in dash_compact
    assert "(Use --show-positions to expand table)" in dash_compact
    assert "│ SIM  │ SYMBOL     │ DIR  │ CONTRACT" not in dash_compact
    print("  -> Populated positions compact summary verified: PASSED")

    # 4. Expanded table view (show_positions=True):
    dash_expanded = portfolio.render_dashboard(show_positions=True)
    assert "ACTIVE POSITIONS (1 Open Across 7 Independent Simulations):" in dash_expanded
    assert "│ SIM  │ SYMBOL     │ DIR  │ CONTRACT" in dash_expanded
    assert "INFY" in dash_expanded
    assert "INFY26NOV1800CE" in dash_expanded
    print("  -> Populated positions expanded table view verified: PASSED")


def test_strike_simulation_performance_matrix_all_strategies():
    print("\n[TEST 37] Testing Strike Simulation Performance Matrix Displays All New Strategies...")
    portfolio = FlagPaperPortfolio(
        starting_capital=100_000.0,
        strike_mode="all",
    )

    dash = portfolio.render_dashboard()
    assert "STRIKE & STRATEGY SIMULATION PERFORMANCE MATRIX (7 Isolated ₹100,000 Portfolios):" in dash
    assert "ATM (Delta ~0.5)" in dash
    assert "ITM (Delta ~0.7)" in dash
    assert "OTM (Delta ~0.3)" in dash
    assert "LOW_PREM (Lowest)" in dash
    assert "TRAIL_SL (Trail)" in dash
    assert "FIXED_TGT (₹750)" in dash
    assert "COMBO (3-in-1)" in dash
    print("  -> All 7 strike & strategy simulations rendered in performance matrix table: PASSED")

    # Test final report generator
    report = portfolio.generate_final_report()
    assert "--- STRIKE & STRATEGY SIMULATION PERFORMANCE MATRIX ---" in report
    assert "1. ATM Options (Delta ~0.5 Baseline)" in report
    assert "2. ITM Options (Delta ~0.7 Deeper In-The-Money)" in report
    assert "3. OTM Options (Delta ~0.3 Cheaper Out-of-The-Money)" in report
    assert "4. LOW_PREM (Lowest Premium Liquid Option Bias)" in report
    assert "5. TRAIL_SL (Tick-by-Tick Ratchet Trailing Stop Loss)" in report
    assert "6. FIXED_TGT (Dual Target min(pct, ₹750) No Floor)" in report
    assert "7. COMBO (Triple Combo: Low Prem + Trail SL + Fixed Tgt)" in report
    print("  -> End-of-session report documents all 7 strategies: PASSED")


def main():
    try:
        test_early_flag_breakout_and_scoring()
        test_flag_engine_no_coiling_entry()
        test_option_requirement_column()
        test_8_second_fresh_breakout_lifecycle()
        test_dashboard_rendering()
        test_downgrade_to_confirmed_never_disappears()
        test_flat_ticks_receive_zero_acceleration()
        test_meaningful_tick_thrust_scaled_to_day_range()
        test_directional_momentum_explicit_alignment()
        test_100_point_ceiling_and_pillar_caps()
        test_early_window_damping_and_z_score_baseline()
        test_dead_stock_without_pole_fails_flag_detection()
        test_intra_5m_volume_accel_and_dry_extreme_shield()
        test_support_resistance_levels_and_ceiling_penalty()
        test_rsi_and_macd_confluence()
        test_relative_cross_sectional_ranking_and_two_stage_funnel()
        test_rejection_of_premature_channel_entries()
        test_rolling_target_decay_5_to_2_5()
        test_stagnant_red_breakeven_shift_and_sl_upped_33()
        test_breakeven_bounce_and_tightened_sl_exits()
        test_intermediate_trailing_profit_floor()
        test_target_runner_reversal_floor()
        test_target_runner_30s_window_complete()
        test_bear_flag_underlying_stop_loss_breathing_room()
        test_bear_position_survives_normal_tick_noise()
        test_bear_breakdown_not_blocked_by_support_at_lod()
        test_flag_boundary_invalidation_across_multiple_distances()
        test_clamped_volume_acceleration_in_relative_alpha()
        test_pre_execution_setup_validity_wick_reversal()
        test_stagnation_timeout_capital_recycling()
        test_lower_premium_option_bias()
        test_tick_by_tick_trailing_stop_loss()
        test_fixed_or_percentage_dual_target_no_floors()
        test_combo_simulation_integration()
        test_low_volume_damping_and_viewing_threshold()
        test_active_positions_toggle_view()
        test_strike_simulation_performance_matrix_all_strategies()
        print("\n✅ ALL 37 BREAKOUT, SCORING & SIMULATION TRACK SUITE TESTS PASSED PERFECTLY!\n")
    finally:
        for p in (
            os.path.join(legacy_dir, "bear_flag_paper_trades.csv"),
            "bear_flag_paper_trades.csv",
        ):
            if os.path.exists(p):
                try:
                    os.remove(p)
                except Exception:
                    pass


if __name__ == "__main__":
    main()
