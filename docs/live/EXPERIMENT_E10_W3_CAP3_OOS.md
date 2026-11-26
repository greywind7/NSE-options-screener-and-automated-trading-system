# Follow-Up Experiment: `E10_W3_CAP3` 8-Day Out-of-Sample Validation

**Date Created**: 2026-11-26  
**Milestone**: Re-run the 45-experiment evaluation suite across `+8` new recorded trading sessions (`OOS`) to confirm out-of-sample stability of `E10_W3_CAP3` vs baseline/alternative variants.  
**In-Sample Baseline Report (`Nov 16–26`)**: [`SIMULATION_HYPOTHESIS_REPORT.md`](file:///Users/yorha/Documents/Trading%20Stuff/docs/live/SIMULATION_HYPOTHESIS_REPORT.md)  
**Saved In-Sample Snapshots**: [`docs/live/experiments/nov16_26_45exp/`](file:///Users/yorha/Documents/Trading%20Stuff/docs/live/experiments/nov16_26_45exp)  
**Production Rollout Details**: Logged in [`TASKS_DONE.md`](file:///Users/yorha/Documents/Trading%20Stuff/docs/live/TASKS_DONE.md#2026-11-26--e10_w3_cap3-promotion-track-retirement--docs-reorganization-0478643)

---

## 1. Baseline Reference (`Nov 16–26`, 8 Clean Sessions)

- **`E10_W3_CAP3` (all 20 pre-retirement tracks)**: `+₹1,874,596` Paper (`₹0` MaxDD, `8/8` green days) / `+₹1,705,447` Delayed (`91.0%` slippage retention, `₹3,664` MaxDD); `31.4%` fewer trades (`23,597 → 16,179`) and `₹445,080` lower brokerage than `H0_BASELINE`.
- **19 Active Default Tracks (post `gamma_runner_OTM2` retirement)**: `+₹1,948,082` Paper / `+₹1,778,671` Delayed (`Nov 16–26`).
- **Chop & Expiry Sessions (`Nov 23 + Nov 26`)**: `+₹398,480` Paper / `+₹321,682` Delayed (vs `H0_BASELINE`: `-₹368,707` P / `-₹329,815` D).

---

## 2. Out-of-Sample Re-Run Procedure (After `+8` New Trading Days)

Do **not** overwrite [`docs/live/experiments/nov16_26_45exp/`](file:///Users/yorha/Documents/Trading%20Stuff/docs/live/experiments/nov16_26_45exp). Once 8 new clean recorded sessions exist under `recordings/`, write the out-of-sample run into a dedicated directory and report:

```bash
# 1. Execute all 45 experiments on the next 8 clean recorded dates
SIM45_DATES="2026-11-27,2026-11-28,..." \
SIM45_RESULTS_DIR="docs/live/experiments/oos_next8_45exp" \
SIM45_REPORT_PATH="docs/live/SIMULATION_HYPOTHESIS_REPORT_OOS.md" \
  .venv/bin/python scripts/run_45_experiments.py all 3 5

# 2. Generate formatted OOS hypothesis report
SIM45_DATES="2026-11-27,2026-11-28,..." \
SIM45_RESULTS_DIR="docs/live/experiments/oos_next8_45exp" \
SIM45_REPORT_PATH="docs/live/SIMULATION_HYPOTHESIS_REPORT_OOS.md" \
  .venv/bin/python scripts/generate_full_report_45.py
```

---

## 3. Out-of-Sample Validation Criteria

- [ ] `E10_W3_CAP3` stays solidly net-positive in `paper_delayed` across the 8 unseen sessions with low MaxDD and `>= 85%` Delayed retention.
- [ ] `E10_W3_CAP3` continues to outperform `H0_BASELINE`, `W1`, and `W2` on risk-adjusted return across unseen Chop and Expiry days.
- [ ] Confirm `gamma_runner_OTM2` remains negative (`< 0` net) out-of-sample before permanently deleting its track source file, and check `delta_scalp_ITM2` Delayed expectancy under `E10_W3_CAP3`.
- [ ] Reconcile live/paper-delayed forward trade journals against [`ColumnarSimEngine`](file:///Users/yorha/Documents/Trading%20Stuff/simulation/columnar_sim_engine.py) tick-by-tick replays for slippage parity prior to live capital scaling.
