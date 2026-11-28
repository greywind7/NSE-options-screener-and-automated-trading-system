# Follow-Up Experiment: `E10_W3_CAP3` 8-Day Out-of-Sample Validation

**Date Created**: 2026-11-26  
**Last Updated**: 2026-11-28 (Refreshed with Tick-Level Scoring & Manifest Parity across `Nov 16–27`)  
**Milestone**: Re-run the full evaluation suite across `+8` new recorded trading sessions (`OOS`) to confirm out-of-sample stability of `E10_W3_CAP3` vs baseline/alternative variants.  
**Latest Report (`Nov 16–27`, 9 Sessions, 51 Experiments)**: [`SIMULATION_HYPOTHESIS_REPORT.md`](file:///Users/yorha/Documents/Trading%20Stuff/docs/live/SIMULATION_HYPOTHESIS_REPORT.md)  
**Saved Pre-Parity 8-Day Snapshot (`Nov 16–26`, 45 Experiments)**: [`docs/live/experiments/nov16_26_45exp/`](file:///Users/yorha/Documents/Trading%20Stuff/docs/live/experiments/nov16_26_45exp)  
**Saved Tick-Parity 9-Day Snapshot (`Nov 16–27`, 51 Experiments)**: [`docs/live/experiments/nov16_27_51exp_tick/`](file:///Users/yorha/Documents/Trading%20Stuff/docs/live/experiments/nov16_27_51exp_tick)  
**Production Rollout Details**: Logged in [`TASKS_DONE.md`](file:///Users/yorha/Documents/Trading%20Stuff/docs/live/TASKS_DONE.md#2026-11-26--e10_w3_cap3-promotion-track-retirement--docs-reorganization-0478643)

---

## 1. Baseline References

### A. Tick-Level Parity Reference (`Nov 16–27`, 9 Clean Sessions, 19 Active Tracks)
- **`E10_W3_CAP3` (`51-exp` tick-level scoring + manifest strike ladder)**: **`+₹2,564,818` Paper (`₹0` MaxDD, `9/9` green) / `+₹2,187,811` Delayed (`₹0` MaxDD, `9/9` green, `85.3%` retention)**; cuts alerts `43.5%` (`2,591 → 1,463`), trades `26.5%` (`25,791 → 18,969`), and brokerage by `₹409,320` vs `H0_BASELINE` (`+₹1,053,315` P / `+₹627,657` D).
- **`Nov 16–26` (8-Day `19`-track Tick-Parity Slice)**: **`+₹2,288,234` Paper / `+₹1,970,588` Delayed** (`8/8` green, `₹0` P & `₹0` D MaxDD; `+₹340,152` P / `+₹191,916` D vs legacy `1.0s`-scoring snapshot).
- **Chop, Expiry & `Nov 27` (`Nov 23 + Nov 26 + Nov 27`)**: `+₹827,709` Paper / `+₹677,355` Delayed (vs `H0_BASELINE`: `-₹313,380` P / `-₹469,567` D).

### B. Legacy Pre-Parity Snapshot (`Nov 16–26`, 8 Clean Sessions, `1.0s` Phase-1 Scoring)
- **`E10_W3_CAP3` (all 20 pre-retirement tracks)**: `+₹1,874,596` Paper (`₹0` MaxDD, `8/8` green) / `+₹1,705,447` Delayed (`91.0%` slippage retention, `₹3,664` MaxDD); `19` active tracks: `+₹1,948,082` Paper / `+₹1,778,671` Delayed.

---

## 2. Out-of-Sample Re-Run Procedure (After `+8` New Trading Days)

Do **not** overwrite [`docs/live/experiments/nov16_26_45exp/`](file:///Users/yorha/Documents/Trading%20Stuff/docs/live/experiments/nov16_26_45exp) or [`docs/live/experiments/nov16_27_51exp_tick/`](file:///Users/yorha/Documents/Trading%20Stuff/docs/live/experiments/nov16_27_51exp_tick). Once 8 new clean recorded sessions exist under `recordings/`, write the out-of-sample run into a dedicated directory and report:

```bash
# 1. Execute all 51 experiments on the next 8 clean recorded dates
SIM45_DATES="2026-11-28,2026-12-01,..." \
SIM45_CACHE_DIR="data/sim51_oos_cache" \
SIM45_RESULTS_DIR="docs/live/experiments/oos_next8_51exp" \
SIM45_REPORT_PATH="docs/live/SIMULATION_HYPOTHESIS_REPORT_OOS.md" \
  .venv/bin/python scripts/run_45_experiments.py all 3 5

# 2. Generate formatted OOS hypothesis report
SIM45_DATES="2026-11-28,2026-12-01,..." \
SIM45_RESULTS_DIR="docs/live/experiments/oos_next8_51exp" \
SIM45_REPORT_PATH="docs/live/SIMULATION_HYPOTHESIS_REPORT_OOS.md" \
  .venv/bin/python scripts/generate_full_report_45.py
```

---

## 3. Out-of-Sample Validation Criteria

- [ ] `E10_W3_CAP3` stays solidly net-positive in `paper_delayed` across the 8 unseen sessions with low MaxDD and `>= 85%` Delayed retention.
- [ ] `E10_W3_CAP3` continues to outperform `H0_BASELINE`, `W1`, and `W2` on risk-adjusted return across unseen Chop and Expiry days.
- [ ] Confirm `gamma_runner_OTM2` remains negative (`< 0` net) out-of-sample before permanently deleting its track source file, and check `delta_scalp_ITM2` Delayed expectancy under `E10_W3_CAP3`.
- [ ] Reconcile live/paper-delayed forward trade journals against [`ColumnarSimEngine`](file:///Users/yorha/Documents/Trading%20Stuff/simulation/columnar_sim_engine.py) tick-by-tick replays for slippage parity prior to live capital scaling.
