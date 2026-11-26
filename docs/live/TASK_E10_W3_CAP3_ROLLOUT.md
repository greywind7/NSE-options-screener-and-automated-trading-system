# Task Doc: `E10_W3_CAP3` Rollout & `gamma_runner_OTM2` Retirement

**Status**: Implemented & Verified in Codebase (`2026-11-26`)  
**Next Milestone**: 8-Day Live-Paper Forward Validation (`+8` Sessions) & Out-of-Sample 45-Experiment Re-Run  
**Experiment Baseline**: [`SIMULATION_HYPOTHESIS_REPORT.md`](file:///Users/yorha/Documents/Trading%20Stuff/docs/live/SIMULATION_HYPOTHESIS_REPORT.md)  
**Saved Benchmark Snapshots**: [`docs/live/experiments/nov16_26_45exp/`](file:///Users/yorha/Documents/Trading%20Stuff/docs/live/experiments/nov16_26_45exp)

---

## 1. Decision Summary

Based on the **45-experiment × 8-session (`Nov 16 – Nov 26, 2026`) tick-by-tick study** (both `paper` and `paper_delayed` `200ms–500ms` latency modes):

1. **Promote `E10_W3_CAP3` as Production Default**:
   - **Baseline Results (`Nov 16–26`, all 20 tracks)**: `+₹1,874,596` Paper (`₹0` MaxDD, `8/8` green days) / `+₹1,705,447` Delayed (`91.0%` slippage retention, `₹3,664` MaxDD), reducing trade count by `31.4%` (`23,597 → 16,179`) and cutting brokerage by `₹445,080` vs `H0_BASELINE`.
   - **After Retiring `gamma_runner_OTM2` (19 active tracks)**: `+₹1,948,082` Paper / `+₹1,778,671` Delayed across the 8 benchmark sessions.
   - **Expiry & Chop Edge**: `+₹398,480` Paper / `+₹321,682` Delayed across `Nov 23 + Nov 26` expiry sessions (vs `H0_BASELINE`: `-₹368,707` P / `-₹329,815` D).
2. **Retire `gamma_runner_OTM2` & Split Composites (`composite_split_*`)**:
   - `gamma_runner_OTM2` lost money in **`0/45`** experiment configurations across both Paper and Delayed (`Avg -₹79,678` P / `-₹81,556` D). Removed from `enabled_tracks` in `default.json` and `v2.json` (`19` active default tracks: `12 v1` + `7 v2`).
   - Removed unused equal-share split composite tracks (`composite_split_ITM`, `composite_split_OTM`, `combined_star` with `shared_pool: false`) from [`config/strategies.json`](file:///Users/yorha/Documents/Trading%20Stuff/config/strategies.json), keeping all `composite_shared_*` tracks intact.

---

## 2. Production Implementation Checklist

| Component | Parameter / Behavior | File | Status |
|---|---|---|---|
| **Strategy Config** | `min_candle_elapsed_sec: 15.0`, `flag_pole_buffer_pct: 0.25`, `pure_3bar_expansion_pct: 0.0045`, `max_alerts_per_symbol_per_day: 3`, `midday_freeze.enabled: false`, `option_convexity_scaling.enabled: true`, `risk.atr_spread_gate_enabled: true` (`2.0x`), `risk.max_spread_pct: 0.03`, `risk.enable_circuit_breaker: false` | [`config/strategies.json`](file:///Users/yorha/Documents/Trading%20Stuff/config/strategies.json) | Done |
| **Track Profiles** | Removed `gamma_runner` (`OTM2`) from `enabled_tracks` (`default` = 19 tracks, `v2` = 7 tracks) | [`config/tracks/default.json`](file:///Users/yorha/Documents/Trading%20Stuff/config/tracks/default.json), [`config/tracks/v2.json`](file:///Users/yorha/Documents/Trading%20Stuff/config/tracks/v2.json) | Done |
| **Signal Engine** | Enforces `15s` active-candle elapsed gate, Top-25% pole breakout floor (`flag_pole_buffer_pct`), and `0.45%` 3-bar cumulative expansion (`pure_3bar_expansion_pct`) | [`strategies/momentum/scoring_engine.py`](file:///Users/yorha/Documents/Trading%20Stuff/strategies/momentum/scoring_engine.py) | Done |
| **RMS Strike Gate & Cap** | Added `passes_atr_spread_gate()` (`2.0x` expected half-ATR move vs spread) alongside `3%` spread cap, plus per-symbol daily quota (`3` accepted alerts/day; illiquid rejections do not burn quota) | [`core/rms/rms_service.py`](file:///Users/yorha/Documents/Trading%20Stuff/core/rms/rms_service.py) | Done |
| **Option Convexity Scaling** | `compute_convexity_scaled_targets()` scales targets (`0.45x–1.25x` around `1.5%` premium ratio) and enforces spread-aware SL floor (`max(3.5%, min(12%, 2.5x spread))`) via `option_scaling_cfg` passed by `TrackFactory` | [`common/tracks/base_track.py`](file:///Users/yorha/Documents/Trading%20Stuff/common/tracks/base_track.py), [`common/tracks/factory.py`](file:///Users/yorha/Documents/Trading%20Stuff/common/tracks/factory.py) | Done |
| **Tick-by-Tick Simulator & Web UI** | Unified `E10_W3_CAP3` scoring/RMS/scaling logic in `ColumnarSimEngine` & exposed knobs in `SimulationService` | [`simulation/columnar_sim_engine.py`](file:///Users/yorha/Documents/Trading%20Stuff/simulation/columnar_sim_engine.py), [`web/simulation_service.py`](file:///Users/yorha/Documents/Trading%20Stuff/web/simulation_service.py) | Done |
| **Unit & Integration Tests** | Updated preset tests (`7` v2 tracks, `gamma_runner_OTM2` retired) + added tests for `15s`/`FLAG25`/`3BAR045`, `passes_atr_spread_gate`, `3`/sym/day cap, and `OptScale` | `tests/` (`150` tests passing) | Done |

---

## 3. Repeat Experiment After 8 New Trading Days (Out-of-Sample Validation)

All baseline `Nov 16–26` per-session raw JSON results are preserved under [`docs/live/experiments/nov16_26_45exp/`](file:///Users/yorha/Documents/Trading%20Stuff/docs/live/experiments/nov16_26_45exp). Once 8 new clean recorded sessions exist under `recordings/`, re-run the full 45-experiment suite into a fresh folder and compare against the `Nov 16–26` snapshot:

```bash
# Run all 45 experiments on 8 new recorded sessions without overwriting nov16_26_45exp/
SIM45_DATES="2026-11-27,2026-11-28,..." \
SIM45_RESULTS_DIR="docs/live/experiments/oos_next8_45exp" \
SIM45_REPORT_PATH="docs/live/SIMULATION_HYPOTHESIS_REPORT_OOS.md" \
  .venv/bin/python scripts/run_45_experiments.py all 3 5

# Generate formatted report from the new results
SIM45_DATES="2026-11-27,2026-11-28,..." \
SIM45_RESULTS_DIR="docs/live/experiments/oos_next8_45exp" \
SIM45_REPORT_PATH="docs/live/SIMULATION_HYPOTHESIS_REPORT_OOS.md" \
  .venv/bin/python scripts/generate_full_report_45.py
```

### Out-of-Sample Promotion Criteria
- [ ] `E10_W3_CAP3` remains positive in `paper_delayed` on out-of-sample sessions with low MaxDD and high Delayed retention (`>= 85%`).
- [ ] Verify whether `E10_W3_CAP3` still outperforms `H0_BASELINE`, `W1`, and `W2` risk-adjusted on unseen Chop and Expiry days.
- [ ] Confirm `gamma_runner_OTM2` stays unprofitable (`< 0` net) out-of-sample before permanently deleting its track file, and monitor `delta_scalp_ITM2` Delayed expectancy under `E10_W3_CAP3`.
- [ ] Confirm live/paper-delayed forward journals match tick-by-tick `ColumnarSimEngine` within acceptable slippage bounds before allocating live capital.
