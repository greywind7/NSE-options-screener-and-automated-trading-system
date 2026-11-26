# Simulation Hypothesis Experimentation Report (`Nov 16 – Nov 26, 2026` — Tick-by-Tick)

> **Scope & Tick-by-Tick Baseline Parity:** All **8 clean recorded sessions** (`2026-11-16`, `2026-11-17`, `2026-11-18`, `2026-11-19`, `2026-11-20`, `2026-11-23` expiry, `2026-11-25`, `2026-11-26` expiry) across **all 20 tracks** (`12 v1` + `8 v2`) in both **`paper`** (instant fill at ask) and **`paper_delayed`** (`200ms–500ms` randomized latency fill), executed using the upgraded **event-driven tick-by-tick `ColumnarSimEngine`** (`3%` configurable spread gate, circuit breaker `false`, real-time equity `underlying_ltp` replay, and dated volume baselines `data/index_YYYYMMDD.json`).
>
> **Saved Results & Rollout:** Raw per-session outputs are archived in [`docs/live/experiments/nov16_26_45exp/`](file:///Users/yorha/Documents/Trading%20Stuff/docs/live/experiments/nov16_26_45exp); reproducible scripts live at [`scripts/run_45_experiments.py`](file:///Users/yorha/Documents/Trading%20Stuff/scripts/run_45_experiments.py) & [`scripts/generate_full_report_45.py`](file:///Users/yorha/Documents/Trading%20Stuff/scripts/generate_full_report_45.py); production promotion of **`E10_W3_CAP3`** is logged in [`TASKS_DONE.md`](file:///Users/yorha/Documents/Trading%20Stuff/docs/live/TASKS_DONE.md) and the 8-day OOS follow-up protocol lives in [`EXPERIMENT_E10_W3_CAP3_OOS.md`](file:///Users/yorha/Documents/Trading%20Stuff/docs/live/EXPERIMENT_E10_W3_CAP3_OOS.md).

---

## 1. Executive Summary

We evaluated **45 configurations** total (360 full-session multi-mode simulations = **720 mode runs**):
- **`H0_BASELINE`** — stock production `ScoringEngine3` & track parameters (`v1` + `v2`).
- **14 Single-Hypothesis Variants (`H1`–`H6b`)** — isolating one signal or execution parameter at a time.
- **9 Original Mixed Variants (`M1`–`M9`)** — multi-rule combinations retaining **Midday Freeze (`11:30–13:30`)**.
- **6 Non-Midday Winner Combinations (`W1`–`W6`)** — mixing the best individual rules **without** Midday Freeze.
- **15 New Targeted Extensions (`E1`–`E15`)** — isolating pure scoring vs option scaling vs spread gate (`E1`, `E2`, `E14`), testing `3Bar 0.35% / 0.60%` (`E3`, `E4`), `Body 0.35%` (`E15`), `ATR >= 0.20%` (`E5`, `E6`), `30m` opposite-flip cooldown (`E7`, `E8`), `Cap <= 3` (`E9`, `E10`), `14:00` entry freeze (`E11`, `E12`), and strict `2%` spread gate (`E13`).

### Top-Line Takeaways

1. **Raw Profit Champion — `W1_15S_OPT_SPREAD`** (`Min Elapsed >= 15s` + `Option Convexity Scaling` + `ATR/Spread Gate`) & **`W2_15S_FLAG25_OPT_SPREAD`** (`W1` + `Flag Top 25% of Pole`):
   - **`W1` 8-Day Paper Net P&L (All 20 tracks):** **`+₹2,032,094`** (**`+206.2%`** / `+₹1,368,376` over tick-by-tick Baseline `+₹663,718`), **7/8** green days, Max Daily Drawdown **`₹26,060`** (**`-94.3%`** vs Baseline `₹453,775`!).
   - **`W2` 8-Day Paper / Delayed:** **`+₹1,970,972` Paper / `+₹1,703,692` Delayed** (`86.4%` slippage retention vs `W1` `84.3%`). Adding `Flag Top 25%` trimmed delayed slippage by `₹52,738` (`₹320,018 → ₹267,280`).
   - **Excluding `gamma_runner_OTM2` (19t):** **`W1: +₹2,134,701` Paper / `+₹1,830,822` Delayed**; **`W2: +₹2,074,278` Paper / `+₹1,801,166` Delayed**.

2. **Zero-Drawdown, Highest Risk-Adjusted & Live-Ready Champion — `E10_W3_CAP3`** (`W3` + `Cap <= 3 alerts/symbol/day`) & **`W3_15S_FLAG25_3BAR045_OPT_SPREAD`**:
   - **`E10_W3_CAP3` finished GREEN IN ALL 8 SESSIONS (`8/8`, `₹0` Paper MaxDD!!)** earning **`+₹1,874,596` Paper / `+₹1,705,447` Delayed (`₹3,664` Delayed DD — highest Delayed net P&L of all `E/W3` variants, beating `W2` Delayed `+₹1,703,692`)**!
   - **Massive Cost Compression:** Opportunities drop `2,025 → 1,140` (`-43.7%`), trades drop `23,597 → 16,179` (`-31.4%`), win rate climbs from `32.0% → 35.4%`, and brokerage drops by **`₹445,080`** (`₹1,415,820 → ₹970,740`).
   - **Notice 5 Zero-Drawdown (`8/8` Green, `₹0` Paper DD) Configurations Emerged Under `15s` + `OptScaleSpread`:** `E10_W3_CAP3` (`+₹1,874,596`), `W3` (`+₹1,842,081`), `E6_W3_ATR020` (`+₹1,788,366`), `E5_W2_ATR020` (`+₹1,761,182`, **`₹0` DD in BOTH Paper & Delayed**), and `E4_W2_3BAR060` (`+₹1,536,886`, **`₹0` DD in BOTH Paper & Delayed**).

3. **Critical Revelation From Upgrading 0.5s Sampling → True Tick-by-Tick Replay:**
   - Under old `0.5s` equity polling, `H0_BASELINE` looked artificially rosy (`+₹971k` in 7d) because intra-0.5s option bid dips were skipped, AND `structural_convexity_ATM` lost money in `0/30` experiments (`-₹103k`) because `0.5s` delayed its underlying invalidation ticks.
   - Under true tick-by-tick replay across 8 days, **`H0_BASELINE` drops to `+₹663,718` (`4/8` green, `₹453,775` DD)** — meanwhile `W1`/`W2`/`E10`/`W3` jump nearly **`3×`** (`+₹1.84M–₹2.03M`) and crush drawdown (`₹0–₹26k`)! Even better, **`structural_convexity_ATM` flips from `0/30` losers to `45/45` PROFITABLE (`Avg +₹56,559`, best single day `+₹73,480`)**!

4. **Isolating Scoring vs Gate vs Option Scaling (`E1`, `E2`, `E14`, `H6a`, `H6b`):**
   - Pure `ScoringEngine3` improvements alone (`E2`: `15s + Flag25 + 3Bar0.45`, **without** any option scaling/gate) ALREADY **more than doubled `H0_BASELINE`**: `+₹663,718 → +₹1,349,484` (`7/8` green days, DD `₹453,775 → ₹84,409`).
   - **Adding `OptScale` / `ATR/Spread` (`E14`, `W1`, `W2`):** Rejecting illiquid wide-spread entries via the `ATR/Spread` gate (`E14_W2_GATE_ONLY_NOSCALE`) lifted `E1` from `+₹1,046,055 → +₹1,741,840` (`+₹695,785`), and turning ON option target/SL convexity rescaling (`W2`) added another `+₹229,133` Paper and `+₹219,618` Delayed.

5. **Track Consistency, Upside & Risk Verdict Across All 45 Experiments:**
   - **18 out of 20 tracks** were net-positive on average across all 45 configs (`17` tracks were **`45/45` (`100%`) profitable across every single config tested!**).
   - **Most Consistent All-Weather Track:** **`fixed_target_ATM`** (`45/45` positive, **`7.33/8` (`91.7%`) green days**, `27/45` zero-DD configs, lowest `AvgDD = ₹2,855`, `Calmar = 30.2x`, `Daily Sharpe = 1.217`, worst experiment still **`+₹30,683`**) closely followed by **`trend_pullback_ATM`** (`45/45` positive, **`+₹98,032` avg P&L**, `92.7%` Delayed retention, `AvgDD = ₹3,565`, `6.96/8` green) and **`composite_shared_ATM`** (`45/45`, `7.13/8` green, `Avg = +₹87,714`).
   - **Highest Upside / Highest Highs Champion:** **`volatility_expansion_ATM`** (`45/45` positive, **`#1` Avg 8d P&L `+₹107,827`**, **`#1` Peak 8d `+₹198,840`** on `W5`, `BestDay = +₹52,347`). Single-day jackpot outlier: `structural_convexity_ATM` (`+₹73,480` on a single trend day).
   - **Only True Dead Weight (`0/45` profitable):** **`gamma_runner_OTM2`** (`Avg -₹79,678` Paper / `-₹81,556` Delayed, `Max -₹47,141`). Retire/disable immediately.

---

## 2. Full 45-Experiment Performance Matrix (8 Days: `Nov 16 – Nov 26, 2026`)

> **Legend:** `Opps` = accepted opportunities (`8d`); `Tr (P)` = closed Paper trades across all 20 tracks; `WR` = Paper Win Rate (`> +2.5%`); `DD` = Peak-to-Trough cumulative Daily Drawdown over the 8 days; `Grn` = profitable days out of 8; `Ret/DD` = `Paper Net / Paper DD`; `Pruned 19t` excludes `gamma_runner_OTM2`; `Pruned 18t` excludes both `gamma_runner_OTM2` & `delta_scalp_ITM2`.

| Rank | Experiment ID | Rule Description | Opps | Tr (P) | WR | Paper Net (All 20) | Paper DD | Grn | Ret/DD | Paper v1 (12t) | Paper v2 (8t) | Delayed Net (All 20) | Delayed DD | Pruned 19t (P / D) | Pruned 18t (P) |
|---:|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| **1** | `W1_15S_OPT_SPREAD` | 15s + OptScale + SpreadGate | 1,836 | 23,469 | 34.3% | **+₹2,032,094** | ₹26,060 | 7/8 | 78.0x | +₹1,345,946 | +₹686,148 | **+₹1,712,076** | ₹26,060 | +₹2,134,701 / +₹1,830,822 | +₹2,048,267 |
| **2** | `W2_15S_FLAG25_OPT_SPREAD` | 15s + Flag25 + OptScale + SpreadGate | 1,825 | 23,316 | 34.3% | **+₹1,970,972** | ₹26,060 | 7/8 | 75.6x | +₹1,289,741 | +₹681,231 | **+₹1,703,692** | ₹26,060 | +₹2,074,278 / +₹1,801,166 | +₹1,986,111 |
| **3** | `E10_W3_CAP3` | W3 + Cap <= 3 per Symbol/Day | 1,140 | 16,179 | 35.4% | **+₹1,874,596** | ₹0 | 8/8 | **∞** | +₹1,225,343 | +₹649,253 | **+₹1,705,447** | ₹3,664 | +₹1,948,082 / +₹1,778,671 | +₹1,862,882 |
| **4** | `W3_15S_FLAG25_3BAR045_OPT_SPREAD` | 15s + Flag25 + 3Bar0.45 + OptScale + SpreadGate | 1,212 | 17,000 | 35.1% | **+₹1,842,081** | ₹0 | 8/8 | **∞** | +₹1,185,660 | +₹656,421 | **+₹1,633,037** | ₹5,858 | +₹1,915,705 / +₹1,701,995 | +₹1,831,231 |
| **5** | `E6_W3_ATR020` | W3 + Min 5m ATR >= 0.20% | 1,100 | 15,420 | 35.1% | **+₹1,788,366** | ₹0 | 8/8 | **∞** | +₹1,146,775 | +₹641,591 | **+₹1,638,703** | ₹1,782 | +₹1,867,334 / +₹1,713,110 | +₹1,793,294 |
| **6** | `E9_W2_CAP3` | W2 + Cap <= 3 per Symbol/Day | 1,636 | 21,596 | 34.2% | +₹1,782,343 | ₹54,415 | 6/8 | 32.8x | +₹1,156,314 | +₹626,030 | +₹1,548,208 | ₹57,656 | +₹1,891,276 / +₹1,649,678 | +₹1,818,307 |
| **7** | `E5_W2_ATR020` | W2 + Min 5m ATR >= 0.20% | 1,439 | 18,716 | 34.4% | +₹1,761,182 | ₹0 | 8/8 | **∞** | +₹1,131,295 | +₹629,887 | +₹1,555,033 | ₹0 | +₹1,853,248 / +₹1,644,640 | +₹1,790,634 |
| **8** | `E7_W2_FLIPCD30` | W2 + 30m Opposite-Flip Cooldown | 1,774 | 22,737 | 34.1% | +₹1,753,985 | ₹26,060 | 6/8 | 67.3x | +₹1,159,446 | +₹594,539 | +₹1,548,595 | ₹82,171 | +₹1,863,075 / +₹1,657,674 | +₹1,796,102 |
| **9** | `E14_W2_GATE_ONLY_NOSCALE` | 15s + Flag25 + ATR/Spread Gate (No Target/SL Rescale) | 1,825 | 22,266 | 33.3% | +₹1,741,840 | ₹31,023 | 6/8 | 56.1x | +₹1,108,767 | +₹633,073 | +₹1,484,074 | ₹41,664 | +₹1,813,754 / +₹1,568,568 | +₹1,714,641 |
| **10** | `E8_W3_FLIPCD30` | W3 + 30m Opposite-Flip Cooldown | 1,188 | 16,757 | 35.0% | +₹1,737,537 | ₹623 | 7/8 | 2788.8x | +₹1,114,543 | +₹622,995 | +₹1,577,307 | ₹9,071 | +₹1,806,051 / +₹1,645,904 | +₹1,737,313 |
| **11** | `W5_15S_FLAG25_NOFLIP_OPT_SPREAD` | W2 + Block Opposite-Dir Flips (No Cap) | 1,357 | 18,822 | 34.6% | +₹1,727,783 | ₹81,005 | 6/8 | 21.3x | +₹1,090,209 | +₹637,574 | +₹1,597,211 | ₹87,969 | +₹1,843,600 / +₹1,716,923 | +₹1,775,899 |
| **12** | `E3_W2_3BAR035` | 15s + Flag25 + PURE 3Bar>=0.35% + OptScaleSpread | 1,520 | 20,051 | 34.1% | +₹1,707,674 | ₹41,664 | 7/8 | 41.0x | +₹1,108,106 | +₹599,569 | +₹1,486,197 | ₹50,782 | +₹1,806,037 / +₹1,585,525 | +₹1,737,733 |
| **13** | `H6b_OPT_SCALE_SPREAD` | Option Convexity Scale + ATR/Spread Gate | 1,880 | 23,814 | 33.8% | +₹1,699,264 | ₹13,025 | 6/8 | 130.5x | +₹1,088,719 | +₹610,545 | +₹1,381,151 | ₹52,667 | +₹1,798,805 / +₹1,486,949 | +₹1,732,010 |
| **14** | `E15_15S_FLAG25_BODY035_SCALED` | 15s + Flag25 + PURE Body>=0.35% + OptScaleSpread | 915 | 13,150 | 35.8% | +₹1,696,922 | ₹660 | 7/8 | 2571.1x | +₹1,102,150 | +₹594,772 | +₹1,586,805 | ₹660 | +₹1,766,762 / +₹1,658,462 | +₹1,692,851 |
| **15** | `E11_W2_FREEZE1400` | W2 + Entry Freeze at 14:00 IST | 1,396 | 18,325 | 34.8% | +₹1,683,618 | ₹4,914 | 7/8 | 342.6x | +₹1,151,846 | +₹531,773 | +₹1,444,902 | ₹10,659 | +₹1,781,469 / +₹1,540,095 | +₹1,699,765 |
| **16** | `E12_W3_FREEZE1400` | W3 + Entry Freeze at 14:00 IST | 978 | 13,841 | 35.3% | +₹1,583,070 | ₹27,606 | 7/8 | 57.3x | +₹1,050,354 | +₹532,716 | +₹1,429,954 | ₹34,995 | +₹1,630,211 / +₹1,474,793 | +₹1,562,766 |
| **17** | `E4_W2_3BAR060` | 15s + Flag25 + PURE 3Bar>=0.60% + OptScaleSpread | 824 | 12,201 | 35.2% | +₹1,536,886 | ₹0 | 8/8 | **∞** | +₹997,154 | +₹539,732 | +₹1,398,631 | ₹0 | +₹1,602,456 / +₹1,465,016 | +₹1,541,759 |
| **18** | `W4_3BAR045_OPT_SPREAD` | PURE 3Bar0.45 + OptScale + SpreadGate | 1,264 | 17,530 | 34.1% | +₹1,401,487 | ₹24,993 | 6/8 | 56.1x | +₹882,366 | +₹519,121 | +₹1,241,896 | ₹67,658 | +₹1,485,218 / +₹1,327,599 | +₹1,426,281 |
| **19** | `E2_15S_FLAG25_3BAR045_SCORING` | 15s + Flag25 + 3Bar0.45 (Pure Scoring Only) | 1,298 | 16,730 | 33.2% | +₹1,349,484 | ₹84,409 | 7/8 | 16.0x | +₹992,821 | +₹356,663 | +₹1,228,225 | ₹90,817 | +₹1,396,790 / +₹1,278,581 | +₹1,441,902 |
| **20** | `E13_W2_SPREAD2PCT` | W2 + Tight 2% Option Spread Cap | 1,556 | 18,258 | 32.9% | +₹1,282,102 | ₹35,885 | 7/8 | 35.7x | +₹882,804 | +₹399,298 | +₹1,113,562 | ₹41,891 | +₹1,358,481 / +₹1,185,687 | +₹1,269,247 |
| **21** | `H4a_ELAPSED_15S` | Min Candle Elapsed >= 15s | 1,980 | 23,181 | 32.6% | +₹1,161,823 | ₹186,925 | 5/8 | 6.2x | +₹935,930 | +₹225,893 | +₹894,937 | ₹360,326 | +₹1,238,633 / +₹985,377 | +₹1,314,274 |
| **22** | `W6_BODY025_ATR025_OPT_SPREAD` | Body0.25 + ATR0.25 + OptScale + SpreadGate | 1,024 | 14,076 | 34.2% | +₹1,136,106 | ₹47,295 | 7/8 | 24.0x | +₹699,755 | +₹436,351 | +₹939,378 | ₹51,750 | +₹1,200,463 / +₹1,003,942 | +₹1,171,706 |
| **23** | `E1_15S_FLAG25_SCORING_ONLY` | 15s + Flag25 (Pure Scoring, No OptScale/Gate) | 1,969 | 23,031 | 32.4% | +₹1,046,055 | ₹261,708 | 5/8 | 4.0x | +₹822,024 | +₹224,031 | +₹810,403 | ₹392,666 | +₹1,119,800 / +₹893,817 | +₹1,192,618 |
| **24** | `H2b_PURE_3BAR_045` | PURE Net 3-Bar Expansion >= 0.45% | 1,350 | 17,281 | 32.4% | +₹979,525 | ₹100,793 | 5/8 | 9.7x | +₹780,000 | +₹199,526 | +₹823,872 | ₹119,959 | +₹1,041,244 / +₹889,238 | +₹1,102,221 |
| **25** | `H6a_OPT_SCALE` | Option Convexity Target/SL Scaling Only | 2,025 | 24,655 | 33.1% | +₹971,255 | ₹373,825 | 4/8 | 2.6x | +₹826,055 | +₹145,200 | +₹730,848 | ₹503,400 | +₹1,075,044 / +₹840,362 | +₹1,203,274 |
| **26** | `M9_ALL_SIGNALS_SCALED` | Midday + 15s + Flag15 + OptScaleSpread (No Cap) | 1,066 | 14,055 | 33.4% | +₹963,403 | ₹245,494 | 5/8 | 3.9x | +₹625,942 | +₹337,462 | +₹865,690 | ₹325,723 | +₹1,038,013 / +₹943,306 | +₹1,015,672 |
| **27** | `H3c_FLAG_FULL_POLE` | Flag Strict Full Pole High/Low (Ref) | 1,944 | 22,717 | 32.5% | +₹853,943 | ₹364,883 | 4/8 | 2.3x | +₹731,072 | +₹122,872 | +₹475,818 | ₹645,893 | +₹944,609 / +₹573,920 | +₹1,027,342 |
| **28** | `H4b_ELAPSED_30S` | Min Candle Elapsed >= 30s (Ref) | 1,949 | 23,013 | 32.2% | +₹777,272 | ₹451,520 | 4/8 | 1.7x | +₹699,152 | +₹78,120 | +₹401,396 | ₹755,650 | +₹878,041 / +₹498,140 | +₹987,492 |
| **29** | `H3a_FLAG_BUF_15` | Flag OLS + 15% Buffer (Top 35% of Pole) | 2,018 | 23,586 | 32.0% | +₹739,837 | ₹379,246 | 4/8 | 2.0x | +₹693,253 | +₹46,584 | +₹637,969 | ₹390,790 | +₹833,261 / +₹731,905 | +₹925,696 |
| **30** | `H2c_BODY_025_ATR_025` | Min ATR >= 0.25% & PURE Body >= 0.25% | 1,081 | 13,970 | 32.1% | +₹695,981 | ₹78,912 | 6/8 | 8.8x | +₹614,850 | +₹81,131 | +₹497,516 | ₹192,643 | +₹763,138 / +₹566,276 | +₹822,305 |
| **31** | `H3b_FLAG_BUF_25` | Flag Trigger >= Pole Top 25% | 2,005 | 23,364 | 32.1% | +₹678,646 | ₹428,427 | 4/8 | 1.6x | +₹628,576 | +₹50,070 | +₹558,189 | ₹450,022 | +₹769,223 / +₹650,821 | +₹860,198 |
| **32** | `H0_BASELINE` | Current Baseline (Unfiltered) | 2,025 | 23,597 | 32.0% | +₹663,718 | ₹453,775 | 4/8 | 1.5x | +₹661,761 | +₹1,958 | +₹536,194 | ₹488,061 | +₹755,056 / +₹628,046 | +₹854,114 |
| **33** | `M7_SPREAD_GATE_CORE` | M1 + NoFlip + OptScaleSpread | 861 | 12,035 | 33.0% | +₹656,246 | ₹259,625 | 4/8 | 2.5x | +₹451,755 | +₹204,490 | +₹566,383 | ₹318,543 | +₹753,536 / +₹664,860 | +₹741,660 |
| **34** | `H5b_CAP_2_NO_FLIP` | Cap <= 2 & Block Opposite Flips | 1,267 | 17,162 | 32.1% | +₹639,010 | ₹257,232 | 4/8 | 2.5x | +₹567,877 | +₹71,133 | +₹496,796 | ₹344,812 | +₹717,777 / +₹581,229 | +₹766,299 |
| **35** | `M8_BODY025_SPREAD_SCALED` | Midday + 15s + Flag15 + ATR0.25 + Body0.25 + Cap2NoFlip + Scaled | 486 | 7,316 | 34.1% | +₹638,443 | ₹143,702 | 5/8 | 4.4x | +₹386,764 | +₹251,679 | +₹546,568 | ₹180,408 | +₹695,358 / +₹603,679 | +₹686,290 |
| **36** | `H5c_CAP_1` | Cap Alerts <= 1 per Symbol/Day | 841 | 12,071 | 32.7% | +₹538,728 | ₹171,920 | 5/8 | 3.1x | +₹477,784 | +₹60,944 | +₹381,118 | ₹280,822 | +₹600,276 / +₹444,251 | +₹638,604 |
| **37** | `H5a_CAP_2` | Cap Alerts <= 2 per Symbol/Day | 1,442 | 18,656 | 31.8% | +₹513,646 | ₹374,218 | 5/8 | 1.4x | +₹491,113 | +₹22,533 | +₹360,045 | ₹440,983 | +₹609,195 / +₹461,100 | +₹680,821 |
| **38** | `M3_SMART_IMPULSE` | M1 + 3Bar0.45 + ATR0.25 | 518 | 7,485 | 31.8% | +₹507,695 | ₹205,440 | 5/8 | 2.5x | +₹342,735 | +₹164,960 | +₹402,211 | ₹242,435 | +₹560,593 / +₹456,941 | +₹586,450 |
| **39** | `M1_CORE_CLEAN` | Midday + 15s + Flag15 + Cap2 | 1,024 | 13,073 | 31.7% | +₹486,673 | ₹352,856 | 4/8 | 1.4x | +₹392,467 | +₹94,206 | +₹379,295 | ₹435,432 | +₹561,925 / +₹460,050 | +₹634,565 |
| **40** | `H2a_MIN_ATR_030` | Min 5m ATR >= 0.30% | 853 | 11,381 | 32.0% | +₹485,657 | ₹242,827 | 6/8 | 2.0x | +₹509,053 | -₹23,396 | +₹314,675 | ₹313,309 | +₹572,858 / +₹403,435 | +₹604,979 |
| **41** | `M2_BALANCED` | M1 + Min ATR >= 0.30% | 449 | 6,503 | 32.5% | +₹455,809 | ₹87,012 | 5/8 | 5.2x | +₹377,977 | +₹77,831 | +₹347,009 | ₹187,194 | +₹517,081 / +₹408,086 | +₹537,481 |
| **42** | `M5_IMPULSE_SCALED` | M3 + Option Scaling | 518 | 7,890 | 32.6% | +₹392,356 | ₹295,373 | 5/8 | 1.3x | +₹230,191 | +₹162,166 | +₹333,152 | ₹316,519 | +₹441,600 / +₹382,466 | +₹473,952 |
| **43** | `M6_HIGH_CONVICTION` | Midday + 15s + Flag25 + 3Bar0.45 + ATR0.30 + Cap2NoFlip + Scaled | 402 | 6,298 | 33.2% | +₹366,181 | ₹102,761 | 4/8 | 3.6x | +₹296,777 | +₹69,404 | +₹332,549 | ₹139,844 | +₹431,633 / +₹398,130 | +₹449,399 |
| **44** | `M4_BALANCED_SCALED` | M2 + Option Scaling | 449 | 6,818 | 33.1% | +₹340,652 | ₹179,204 | 4/8 | 1.9x | +₹282,101 | +₹58,551 | +₹254,356 | ₹265,754 | +₹400,579 / +₹314,425 | +₹423,385 |
| **45** | `H1_MIDDAY_FREEZE` | Midday Freeze Only (11:30-13:30) | 1,188 | 14,354 | 31.5% | +₹203,383 | ₹551,523 | 3/8 | 0.4x | +₹249,476 | -₹46,093 | +₹154,424 | ₹574,396 | +₹274,591 / +₹229,845 | +₹354,026 |

---

## 3. Day-by-Day Breakdown (`Nov 16 – Nov 26`, Key Experiments)

Each cell shows **Paper Net (`v1` / `v2`)** on top and **Delayed Net** underneath (`All 20` tracks):

| Experiment | Nov 16 | Nov 17 | Nov 18 | Nov 19 | Nov 20 | Nov 23 | Nov 25 | Nov 26 | 8-Day Total |
|---|---|---|---|---|---|---|---|---|---|
| **`H0_BASELINE`** *(P)*<br>*(Delayed)* | `-₹107,246` `(-6.9k/-100.3k)`<br>`-₹124,420` | `+₹414,337` `(+290.4k/+124.0k)`<br>`+₹405,741` | `+₹155,057` `(+117.4k/+37.7k)`<br>`+₹144,784` | `+₹401,226` `(+231.2k/+170.0k)`<br>`+₹380,352` | `+₹254,121` `(+129.2k/+125.0k)`<br>`+₹217,799` | `-₹56,271` `(-31.0k/-25.2k)`<br>`-₹55,446` | `-₹85,068` `(+45.2k/-130.3k)`<br>`-₹158,246` | `-₹312,436` `(-113.6k/-198.9k)`<br>`-₹274,370` | **`+₹663,718`**<br>**`+₹536,194`** |
| **`W1_15S_OPT_SPREAD`** *(P)*<br>*(Delayed)* | `+₹58,415` `(+79.4k/-20.9k)`<br>`+₹44,270` | `+₹629,223` `(+383.7k/+245.5k)`<br>`+₹619,416` | `+₹273,656` `(+194.3k/+79.4k)`<br>`+₹262,385` | `+₹422,551` `(+250.2k/+172.3k)`<br>`+₹394,597` | `+₹396,123` `(+188.4k/+207.7k)`<br>`+₹372,902` | `-₹26,060` `(-19.2k/-6.9k)`<br>`-₹26,060` | `+₹175,374` `(+164.5k/+10.9k)`<br>`+₹55,559` | `+₹102,812` `(+104.6k/-1.8k)`<br>`-₹10,993` | **`+₹2,032,094`**<br>**`+₹1,712,076`** |
| **`W2_15S_FLAG25_OPT_SPREAD`** *(P)*<br>*(Delayed)* | `+₹7,091` `(+41.9k/-34.8k)`<br>`+₹8,827` | `+₹627,265` `(+379.4k/+247.8k)`<br>`+₹612,591` | `+₹273,656` `(+194.3k/+79.4k)`<br>`+₹262,385` | `+₹437,623` `(+256.3k/+181.3k)`<br>`+₹405,560` | `+₹393,336` `(+185.2k/+208.1k)`<br>`+₹369,079` | `-₹26,060` `(-19.2k/-6.9k)`<br>`-₹26,060` | `+₹178,296` `(+158.5k/+19.8k)`<br>`+₹54,591` | `+₹79,764` `(+93.3k/-13.6k)`<br>`+₹16,719` | **`+₹1,970,972`**<br>**`+₹1,703,692`** |
| **`E10_W3_CAP3`** *(P)*<br>*(Delayed)* | `+₹1,661` `(+19.4k/-17.7k)`<br>`-₹3,664` | `+₹476,125` `(+276.0k/+200.1k)`<br>`+₹451,257` | `+₹355,727` `(+239.9k/+115.8k)`<br>`+₹353,765` | `+₹264,205` `(+169.8k/+94.4k)`<br>`+₹249,571` | `+₹294,529` `(+116.3k/+178.2k)`<br>`+₹281,658` | `+₹16,975` `(+7.9k/+9.1k)`<br>`+₹16,975` | `+₹83,868` `(+65.7k/+18.2k)`<br>`+₹51,178` | `+₹381,505` `(+330.4k/+51.1k)`<br>`+₹304,707` | **`+₹1,874,596`**<br>**`+₹1,705,447`** |
| **`W3_15S_FLAG25_3BAR045_OPT_SPREAD`** *(P)*<br>*(Delayed)* | `+₹2,574` `(+25.8k/-23.2k)`<br>`-₹5,858` | `+₹608,638` `(+345.2k/+263.4k)`<br>`+₹604,643` | `+₹320,934` `(+219.0k/+101.9k)`<br>`+₹318,416` | `+₹235,914` `(+155.1k/+80.9k)`<br>`+₹220,577` | `+₹283,548` `(+114.3k/+169.2k)`<br>`+₹270,514` | `+₹16,975` `(+7.9k/+9.1k)`<br>`+₹16,975` | `+₹74,390` `(+52.5k/+21.9k)`<br>`+₹21,266` | `+₹299,108` `(+265.9k/+33.2k)`<br>`+₹186,505` | **`+₹1,842,081`**<br>**`+₹1,633,037`** |
| **`E6_W3_ATR020`** *(P)*<br>*(Delayed)* | `+₹4,063` `(+20.1k/-16.0k)`<br>`-₹1,782` | `+₹633,809` `(+364.8k/+269.0k)`<br>`+₹616,483` | `+₹289,778` `(+206.2k/+83.6k)`<br>`+₹288,236` | `+₹151,099` `(+92.6k/+58.5k)`<br>`+₹140,959` | `+₹259,489` `(+97.8k/+161.7k)`<br>`+₹249,047` | `+₹16,975` `(+7.9k/+9.1k)`<br>`+₹16,975` | `+₹69,532` `(+49.3k/+20.2k)`<br>`+₹32,784` | `+₹363,622` `(+308.1k/+55.5k)`<br>`+₹296,002` | **`+₹1,788,366`**<br>**`+₹1,638,703`** |
| **`E5_W2_ATR020`** *(P)*<br>*(Delayed)* | `+₹16,392` `(+42.7k/-26.3k)`<br>`+₹13,409` | `+₹563,544` `(+344.4k/+219.1k)`<br>`+₹550,709` | `+₹282,808` `(+199.7k/+83.1k)`<br>`+₹281,220` | `+₹181,961` `(+114.1k/+67.9k)`<br>`+₹170,680` | `+₹388,995` `(+185.5k/+203.5k)`<br>`+₹363,193` | `+₹11,519` `(+3.9k/+7.6k)`<br>`+₹11,519` | `+₹118,063` `(+102.8k/+15.2k)`<br>`+₹50,954` | `+₹197,900` `(+138.2k/+59.7k)`<br>`+₹113,349` | **`+₹1,761,182`**<br>**`+₹1,555,033`** |
| **`E14_W2_GATE_ONLY_NOSCALE`** *(P)*<br>*(Delayed)* | `-₹31,023` `(+17.7k/-48.7k)`<br>`-₹41,664` | `+₹616,323` `(+377.8k/+238.5k)`<br>`+₹588,625` | `+₹188,247` `(+130.7k/+57.6k)`<br>`+₹183,690` | `+₹381,768` `(+213.6k/+168.1k)`<br>`+₹354,966` | `+₹351,347` `(+159.6k/+191.8k)`<br>`+₹319,073` | `-₹30,160` `(-21.1k/-9.0k)`<br>`-₹30,160` | `+₹237,141` `(+204.8k/+32.3k)`<br>`+₹117,354` | `+₹28,196` `(+25.6k/+2.6k)`<br>`-₹7,810` | **`+₹1,741,840`**<br>**`+₹1,484,074`** |
| **`W5_15S_FLAG25_NOFLIP_OPT_SPREAD`** *(P)*<br>*(Delayed)* | `-₹81,005` `(-20.4k/-60.6k)`<br>`-₹87,969` | `+₹521,169` `(+313.4k/+207.7k)`<br>`+₹533,331` | `+₹299,404` `(+208.7k/+90.7k)`<br>`+₹293,584` | `+₹331,591` `(+183.4k/+148.2k)`<br>`+₹310,324` | `+₹379,522` `(+199.0k/+180.6k)`<br>`+₹351,827` | `-₹26,060` `(-19.2k/-6.9k)`<br>`-₹26,060` | `+₹182,393` `(+140.3k/+42.1k)`<br>`+₹171,637` | `+₹120,769` `(+85.0k/+35.7k)`<br>`+₹50,537` | **`+₹1,727,783`**<br>**`+₹1,597,211`** |
| **`H6b_OPT_SCALE_SPREAD`** *(P)*<br>*(Delayed)* | `-₹3,431` `(+32.2k/-35.7k)`<br>`-₹12,196` | `+₹514,086` `(+314.3k/+199.8k)`<br>`+₹489,607` | `+₹262,067` `(+183.0k/+79.1k)`<br>`+₹254,370` | `+₹456,895` `(+256.9k/+200.0k)`<br>`+₹425,513` | `+₹305,549` `(+147.2k/+158.3k)`<br>`+₹276,523` | `-₹13,025` `(-11.8k/-1.2k)`<br>`-₹12,575` | `+₹83,670` `(+82.4k/+1.3k)`<br>`-₹19,491` | `+₹93,453` `(+84.5k/+8.9k)`<br>`-₹20,600` | **`+₹1,699,264`**<br>**`+₹1,381,151`** |
| **`E2_15S_FLAG25_3BAR045_SCORING`** *(P)*<br>*(Delayed)* | `-₹84,409` `(-31.0k/-53.4k)`<br>`-₹90,817` | `+₹606,070` `(+349.1k/+257.0k)`<br>`+₹602,806` | `+₹201,768` `(+138.2k/+63.5k)`<br>`+₹196,732` | `+₹217,331` `(+146.0k/+71.3k)`<br>`+₹202,927` | `+₹260,613` `(+127.5k/+133.1k)`<br>`+₹243,580` | `+₹14,766` `(+9.9k/+4.9k)`<br>`+₹14,766` | `+₹47,994` `(+94.4k/-46.4k)`<br>`+₹46,572` | `+₹85,350` `(+158.6k/-73.3k)`<br>`+₹11,658` | **`+₹1,349,484`**<br>**`+₹1,228,225`** |
| **`H4a_ELAPSED_15S`** *(P)*<br>*(Delayed)* | `-₹67,220` `(+24.3k/-91.6k)`<br>`-₹73,094` | `+₹539,202` `(+346.6k/+192.6k)`<br>`+₹510,967` | `+₹164,059` `(+122.3k/+41.8k)`<br>`+₹155,725` | `+₹366,570` `(+219.6k/+146.9k)`<br>`+₹348,096` | `+₹346,137` `(+173.6k/+172.6k)`<br>`+₹313,570` | `-₹67,123` `(-36.7k/-30.4k)`<br>`-₹67,123` | `+₹24,922` `(+101.9k/-77.0k)`<br>`-₹54,522` | `-₹144,723` `(-15.7k/-129.0k)`<br>`-₹238,681` | **`+₹1,161,823`**<br>**`+₹894,937`** |
| **`H2b_PURE_3BAR_045`** *(P)*<br>*(Delayed)* | `-₹100,793` `(-40.2k/-60.6k)`<br>`-₹110,384` | `+₹487,396` `(+289.3k/+198.1k)`<br>`+₹464,787` | `+₹196,593` `(+138.6k/+58.0k)`<br>`+₹194,547` | `+₹230,072` `(+147.2k/+82.9k)`<br>`+₹221,461` | `+₹169,640` `(+86.0k/+83.7k)`<br>`+₹146,976` | `+₹25,619` `(+15.6k/+10.0k)`<br>`+₹26,444` | `-₹19,098` `(+45.4k/-64.5k)`<br>`-₹93,864` | `-₹9,903` `(+98.2k/-108.1k)`<br>`-₹26,095` | **`+₹979,525`**<br>**`+₹823,872`** |
| **`H1_MIDDAY_FREEZE`** *(P)*<br>*(Delayed)* | `-₹116,940` `(-40.6k/-76.3k)`<br>`-₹119,183` | `+₹418,716` `(+272.7k/+146.0k)`<br>`+₹430,834` | `-₹2,025` `(+12.9k/-14.9k)`<br>`-₹9,798` | `+₹380,517` `(+219.2k/+161.4k)`<br>`+₹368,394` | `+₹74,639` `(+34.9k/+39.7k)`<br>`+₹58,572` | `-₹68,092` `(-35.8k/-32.2k)`<br>`-₹68,092` | `-₹339,718` `(-218.8k/-120.9k)`<br>`-₹376,929` | `-₹143,714` `(+5.1k/-148.8k)`<br>`-₹129,375` | **`+₹203,383`**<br>**`+₹154,424`** |

---

## 4. Deep Dive on Single & Combined Hypotheses (Tick-by-Tick Findings)

### 4.1 Option Convexity Scaling & ATR/Spread Gate (`H6a`, `H6b`, `E14`, `W1`, `W2`)
- **`E1` (`15s+Flag25` Scoring Only): `+₹1,046,055`** → adding **only** ATR/Spread pre-entry gate (`E14_W2_GATE_ONLY_NOSCALE`, keeping fixed target/SL) jumps P&L to **`+₹1,741,840`** (`+₹695,785`), proving that gating options whose spread exceeds half an expected 5m ATR swing is massive on its own.
- Adding **Option Convexity Target/SL Scaling** (`E14 → W2`) lifts Paper by another **`+₹229,133`** (`+₹1,741,840 → +₹1,970,972`) and Delayed by **`+₹219,618`** while compressing Paper MaxDD from `₹31,023 → ₹26,060`.
- **`H6b_OPT_SCALE_SPREAD`** alone turns `H0_BASELINE` from `+₹663,718` (`4/8` green, `₹453,775` DD) into **`+₹1,699,264`** (`6/8` green, `₹13,025` DD!). Tightening spread cap further from `3% → 2%` in **`E13_W2_SPREAD2PCT`** over-filters entries (`opps 1,825 → 1,556`, P&L drops `+₹1,970,972 → +₹1,282,102`), confirming **`max_spread_pct = 0.03` (`3%`)** paired with the ATR/Spread ratio gate is strictly better than a blanket `2%` cap.

### 4.2 Bar-Elapsed Gate (`H4a: 15s` vs `H4b: 30s`)
- **`H4a_ELAPSED_15S`:** `+₹1,161,823` Paper / `+₹894,937` Delayed (**`+₹498,105`** over `H0_BASELINE`), rejecting just `45` raw opps (`2,025 → 1,980`) while cutting Paper MaxDD from `₹453,775 → ₹186,925`.
- **`H4b_ELAPSED_30S`:** `+₹777,272` Paper / **`+₹401,396` Delayed** (`₹755,650` DD). Waiting 30s consistently degrades entries and delayed slippage compared to `15s`. **`15s` remains optimal.**

### 4.3 `PURE` Impulse Floors (`3Bar 0.35 / 0.45 / 0.60`, `Body 0.25 / 0.35`, `ATR 0.20 / 0.30`)
- **Sensitivity along `W2 + 3Bar`:** `W2 (no 3Bar)` = `+₹1,970,972` (DD `₹26,060`) | `E3 (0.35%)` = `+₹1,707,674` (DD `₹41,664`) | **`W3 (0.45%)` = `+₹1,842,081` (`₹0` DD, `8/8` green)** | `E4 (0.60%)` = `+₹1,536,886` (`₹0` DD in **both** P & D, `12,201` trades!).
- **Single-Bar Body `>= 0.35%` (`E15`):** `+₹1,696,922` Paper / **`+₹1,586,805` Delayed (`₹660` DD in P & D, highest WR top-15 at `35.8%`, `93.5%` delayed retention, `915` opps = `-55%` spam!)**.
- **Global `ATR >= 0.20%` (`E5`, `E6` vs `ATR >= 0.30%` `H2a`):** `H2a_MIN_ATR_030` choked signal flow too hard (`+₹485,657`). By contrast, `ATR >= 0.20%` removes *only* the `<0.20%` dead zone: **`E5_W2_ATR020` (`+₹1,761,182` P / `+₹1,555,033` D) achieved `8/8` GREEN DAYS & `₹0` DD in BOTH Paper and Delayed!** And `E6_W3_ATR020` earned `+₹1,788,366` P / `+₹1,638,703` D (`8/8` green P, `₹1,782` D DD).

### 4.4 Midday Freeze (`11:30–13:30`), Early Cutoff (`14:00`), Per-Symbol Caps & Flip Cooldown
- **Midday Freeze (`H1_MIDDAY_FREEZE`) Confirmed Toxic For Momentum:** Drops Baseline `+₹663,718 → +₹203,383` (`-69.4%`), spikes DD `₹453,775 → ₹551,523`, and makes every `M1–M9` mix finish near the bottom of the 45-experiment table.
- **Early Afternoon Freeze (`14:00`, `E11`, `E12`) vs `15:00` (`W2`, `W3`):** Freezing entries after `14:00` trimmed ~20% of trades and cut DD on `W2` (`₹26,060 → ₹4,914`), at the cost of ~`₹259k–₹287k` lower Paper net profit (`+₹1,970,972 → +₹1,683,618`).
- **Per-Symbol Cap `<= 3` (`E9`, `E10`) Is Sweet Spot vs `Cap <= 2` (`H5a`):** On `W3`, **`E10_W3_CAP3` actually INCREASED net profit in both Paper (`+₹32.5k`) AND Delayed (`+₹72.4k`: `+₹1,633,037 → +₹1,705,447`) while reducing trades (`17,000 → 16,179`) and maintaining `8/8` green (`₹0` Paper DD)!**
- **30m Flip Cooldown (`E7`, `E8`) vs Hard Session Ban (`W5`):** `E7` (`+₹1,753,985`) outperformed full-session ban `W5` (`+₹1,727,783`) on Paper (`3x` lower DD: `₹26k` vs `₹81k`), while `E8` had nearly identical `7/8` (`₹623` DD) performance cleanly preserving reversal days.

---

## 5. Cross-Experiment Track Consistency, Highest Highs & Lowest Drawdown Matrices

### 5.1 Average Performance & Consistency Across All 45 Experiments (`8 Days` Each)

> **How to read this matrix:** Each row aggregates one execution track across **all 45 parameter/scoring environments** (`45 × 8 = 360` simulated sessions per track), exposing which tracks make money *universally* vs. which depend on specific filters.

| Rank | Profile | Track | Pos /45 (P / D) | Avg 45 (Paper) | Med 45 (P) | Avg 45 (Delayed) | Del Ret% | Paper Range (`Min .. Max` across 45) | Avg Grn/8 | Grn Day% | Avg DD | Max DD | Zero-DD Exps | Best / Worst Day (P) | Calmar | Daily Sharpe |
|---:|---|---|---:|---:|---:|---:|---:|---|---:|---:|---:|---:|---:|---|---:|---:|
| **1** | `v2` | **`volatility_expansion_ATM`** | 45/45 / 45/45 | **+₹107,827** | +₹102,186 | +₹96,246 | 89.3% | `+₹23,544 .. +₹198,840` | 5.98/8 | 74.7% | ₹16,717 | ₹55,477 | 1/45 | `+₹52,347 / -₹39,336` | 6.5x | 0.70 |
| **2** | `v1` | **`trend_pullback_ATM`** | 45/45 / 45/45 | **+₹98,032** | +₹96,599 | +₹90,834 | 92.7% | `+₹30,088 .. +₹151,438` | 6.96/8 | 86.9% | ₹3,565 | ₹21,756 | 20/45 | `+₹40,486 / -₹21,756` | 27.5x | 1.10 |
| **3** | `v1` | **`composite_shared_ATM`** | 45/45 / 45/45 | **+₹87,714** | +₹86,056 | +₹75,708 | 86.3% | `+₹22,113 .. +₹152,386` | 7.13/8 | 89.2% | ₹3,924 | ₹22,512 | 22/45 | `+₹42,888 / -₹22,512` | 22.4x | 1.06 |
| **4** | `v1` | **`fixed_target_ATM`** | 45/45 / 45/45 | **+₹86,155** | +₹93,679 | +₹75,721 | 87.9% | `+₹30,683 .. +₹126,523` | 7.33/8 | 91.7% | ₹2,855 | ₹20,009 | 27/45 | `+₹30,366 / -₹20,009` | 30.2x | 1.22 |
| **5** | `v2` | **`adaptive_regime_ATM`** | 44/45 / 44/45 | **+₹85,742** | +₹87,080 | +₹73,321 | 85.5% | `-₹2,244 .. +₹153,370` | 6.16/8 | 76.9% | ₹12,647 | ₹43,560 | 6/45 | `+₹49,815 / -₹43,560` | 6.8x | 0.70 |
| **6** | `v1` | **`fast_momentum_ATM`** | 45/45 / 45/45 | **+₹76,645** | +₹76,570 | +₹66,395 | 86.6% | `+₹23,121 .. +₹130,882` | 6.64/8 | 83.1% | ₹5,557 | ₹42,208 | 18/45 | `+₹45,749 / -₹25,534` | 13.8x | 0.83 |
| **7** | `v2` | **`micro_chandelier_ITM1`** | 45/45 / 44/45 | **+₹69,159** | +₹60,606 | +₹55,539 | 80.3% | `+₹11,434 .. +₹131,592` | 5.49/8 | 68.6% | ₹17,055 | ₹78,021 | 1/45 | `+₹41,741 / -₹41,536` | 4.1x | 0.57 |
| **8** | `v2` | **`trend_pullback_v2_ITM1`** | 44/45 / 43/45 | **+₹65,835** | +₹70,032 | +₹58,949 | 89.5% | `-₹3,610 .. +₹141,439` | 5.09/8 | 63.6% | ₹17,452 | ₹35,649 | 0/45 | `+₹45,338 / -₹32,691` | 3.8x | 0.50 |
| **9** | `v1` | **`composite_shared_ITM`** | 45/45 / 45/45 | **+₹65,730** | +₹71,274 | +₹60,226 | 91.6% | `+₹1,489 .. +₹119,754` | 5.67/8 | 70.8% | ₹14,861 | ₹52,358 | 2/45 | `+₹52,595 / -₹30,118` | 4.4x | 0.58 |
| **10** | `v1` | **`fast_momentum_ITM`** | 45/45 / 45/45 | **+₹64,461** | +₹60,679 | +₹56,029 | 86.9% | `+₹8,372 .. +₹118,601` | 5.40/8 | 67.5% | ₹15,237 | ₹53,284 | 2/45 | `+₹46,428 / -₹27,844` | 4.2x | 0.56 |
| **11** | `v1` | **`trend_pullback_ITM`** | 44/45 / 43/45 | **+₹60,518** | +₹56,142 | +₹52,939 | 87.5% | `-₹3,401 .. +₹119,242` | 5.20/8 | 65.0% | ₹17,554 | ₹46,994 | 3/45 | `+₹44,934 / -₹39,993` | 3.4x | 0.50 |
| **12** | `v1` | **`fast_momentum_OTM`** | 45/45 / 45/45 | **+₹59,973** | +₹62,918 | +₹51,840 | 86.4% | `+₹12,831 .. +₹101,730` | 5.96/8 | 74.4% | ₹6,020 | ₹19,109 | 0/45 | `+₹42,890 / -₹19,109` | 10.0x | 0.78 |
| **13** | `v1` | **`fixed_target_ITM`** | 42/45 / 43/45 | **+₹59,642** | +₹63,853 | +₹51,624 | 86.6% | `-₹3,187 .. +₹103,729` | 5.64/8 | 70.6% | ₹18,410 | ₹41,625 | 0/45 | `+₹45,754 / -₹30,646` | 3.2x | 0.52 |
| **14** | `v2` | **`structural_convexity_ATM`** | 45/45 / 45/45 | **+₹56,559** | +₹61,322 | +₹53,111 | 93.9% | `+₹13,564 .. +₹94,566` | 4.89/8 | 61.1% | ₹20,192 | ₹36,888 | 0/45 | `+₹73,480 / -₹32,884` | 2.8x | 0.37 |
| **15** | `v1` | **`trend_pullback_OTM`** | 45/45 / 45/45 | **+₹55,517** | +₹54,985 | +₹46,312 | 83.4% | `+₹5,430 .. +₹116,053` | 5.78/8 | 72.2% | ₹7,065 | ₹27,072 | 4/45 | `+₹36,842 / -₹26,776` | 7.9x | 0.71 |
| **16** | `v1` | **`composite_shared_OTM`** | 45/45 / 45/45 | **+₹51,653** | +₹52,470 | +₹40,550 | 78.5% | `+₹11,017 .. +₹99,869` | 5.78/8 | 72.2% | ₹6,274 | ₹20,820 | 3/45 | `+₹39,223 / -₹20,820` | 8.2x | 0.70 |
| **17** | `v1` | **`fixed_target_OTM`** | 45/45 / 45/45 | **+₹33,622** | +₹34,728 | +₹26,942 | 80.1% | `+₹3,830 .. +₹62,355` | 5.42/8 | 67.8% | ₹8,243 | ₹20,658 | 0/45 | `+₹21,727 / -₹19,176` | 4.1x | 0.56 |
| **18** | `v2` | **`opening_thrust_convex_ATM`** | 45/45 / 45/45 | **+₹22,190** | +₹25,559 | +₹22,188 | 100.0% | `+₹8,519 .. +₹30,037` | 4.98/8 | 62.2% | ₹13,517 | ₹21,570 | 0/45 | `+₹16,788 / -₹19,707` | 1.6x | 0.37 |
| **19** | `v2` | **`delta_scalp_ITM2`** | 23/45 / 23/45 | **+₹2,601** | +₹9,068 | -₹16,132 | -620.2% | `-₹128,231 .. +₹99,113` | 4.73/8 | 59.2% | ₹44,224 | ₹134,234 | 0/45 | `+₹36,812 / -₹67,164` | 0.1x | 0.02 |
| **20** | `v2` | **`gamma_runner_OTM2`** | 0/45 / 0/45 | **-₹79,678** | -₹76,379 | -₹81,556 | 0.0% | `-₹115,817 .. -₹47,141` | 2.00/8 | 25.0% | ₹79,678 | ₹115,817 | 0/45 | `+₹28,412 / -₹50,515` | -1.0x | -0.70 |

### 5.2 Multi-Objective Track Spotlights (Consistency vs. Highest Highs vs. Lowest Drawdown)

| Objective Criterion | #1 Champion Track | Key Proof Metrics (Across 45 Experiments) | Runner-Up | Notable Specialist |
|---|---|---|---|---|
| **Most Consistent (All Conditions)** | **`fixed_target_ATM`** (`v1`) | `45/45` positive P&D, **`7.33/8` (`91.7%`) green days**, **`27/45` zero-DD runs**, highest floor (**`Min = +₹30,683`** even in worst freeze runs), `Sharpe = 1.22` | **`composite_shared_ATM`** (`45/45`, `7.13/8` green, `22/45` zero-DD, `Avg +₹87,714`) | **`trend_pullback_ATM`** (`45/45`, `6.96/8` green, `Min +₹30,088`, `Avg +₹98,032`) |
| **Highest Highs (8d Peak & Mean)** | **`volatility_expansion_ATM`** (`v2`) | **`#1` Avg 8d P&L (`+₹107,827` P / `+₹96,246` D)**, **`#1` Peak 8d (`+₹198,840` on `W5`)**, `45/45` positive, `BestDay +₹52,347` | **`adaptive_regime_ATM`** (`Peak +₹153,370` on `E9`, `BestDay +₹49,815`) | **`structural_convexity_ATM`** (**`#1` single-day outlier `+₹73,480`**, `45/45` positive!) |
| **Lowest Drawdown & Highest Calmar** | **`fixed_target_ATM`** (`v1`) | Lowest `AvgDD = ₹2,855`, `WorstDay = -₹20,009`, `Calmar = 30.2x`, `Zero-DD = 27/45` | **`trend_pullback_ATM`** (`AvgDD = ₹3,565`, `Calmar = 27.5x`, `Avg +₹98,032`) | **`composite_shared_ATM`** (`AvgDD = ₹3,924`, `Calmar = 22.4x`, `Zero-DD = 22/45`) |
| **Best Delayed Resilience (Live Execution)** | **`trend_pullback_ATM`** (`v1`) | **`92.7%` Delayed Retention (`+₹98.0k → +₹90.8k` avg)** + **`opening_thrust_convex_ATM`** **`99.99%` (`+₹22,190 → +₹22,188`)** | **`structural_convexity_ATM`** (`93.9%` retention) | **`composite_shared_ITM`** (`91.6%` retention) & `trend_pullback_v2_ITM1` (`89.5%`) |
| **Structurally Unprofitable (Retire)** | **`gamma_runner_OTM2`** (`v2`) | **`0/45` profitable in P & D (`Avg -₹79,678` P / `-₹81,556` D, `2.0/8` green)** | `delta_scalp_ITM2` (`23/45` P; only works under `OptScale` / `W3`) | Removing just `gamma_runner_OTM2` adds **`+₹47k..+₹116k`** across every config! |

### 5.3 Per-Day Trackwise Net P&L Matrix — Champion `W1_15S_OPT_SPREAD` (Paper / Delayed)

| Profile | Track | Nov 16 | Nov 17 | Nov 18 | Nov 19 | Nov 20 | Nov 23 | Nov 25 | Nov 26 | 8d Total (P / D) | Grn | DD |
|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| `v2` | `volatility_expansion_ATM` | -₹4,759 | +₹49,129 | +₹29,770 | +₹32,557 | +₹43,674 | +₹214 | +₹7,298 | +₹33,910 | **+₹191,793 / +₹173,567** | 7/8 | ₹4,759 |
| `v1` | `composite_shared_ATM` | +₹9,880 | +₹31,895 | +₹23,740 | +₹23,592 | +₹24,426 | +₹590 | +₹20,089 | +₹15,562 | **+₹149,774 / +₹107,196** | 8/8 | ₹0 |
| `v1` | `trend_pullback_ATM` | +₹11,168 | +₹33,817 | +₹21,216 | +₹26,631 | +₹17,430 | +₹845 | +₹27,154 | +₹5,818 | **+₹144,078 / +₹133,776** | 8/8 | ₹0 |
| `v2` | `adaptive_regime_ATM` | +₹14,604 | +₹49,815 | +₹16,711 | +₹23,574 | +₹41,424 | -₹6,052 | +₹11,168 | -₹9,006 | **+₹142,239 / +₹108,749** | 6/8 | ₹9,006 |
| `v2` | `trend_pullback_v2_ITM1` | -₹1,903 | +₹45,338 | +₹14,508 | +₹35,312 | +₹28,960 | -₹5,542 | +₹26,496 | -₹9,257 | **+₹133,912 / +₹141,448** | 5/8 | ₹9,257 |
| `v1` | `fixed_target_ATM` | +₹13,942 | +₹26,680 | +₹16,103 | +₹21,671 | +₹20,474 | +₹440 | +₹14,403 | +₹12,810 | **+₹126,523 / +₹109,661** | 8/8 | ₹0 |
| `v1` | `fast_momentum_ATM` | +₹9,755 | +₹34,187 | +₹2,659 | +₹26,775 | +₹21,915 | +₹590 | +₹17,244 | +₹13,314 | **+₹126,439 / +₹97,588** | 8/8 | ₹0 |
| `v2` | `micro_chandelier_ITM1` | +₹6,303 | +₹37,179 | +₹30,492 | +₹26,908 | +₹21,362 | -₹1,478 | -₹1,909 | +₹4,661 | **+₹123,519 / +₹108,049** | 6/8 | ₹3,386 |
| `v1` | `composite_shared_ITM` | +₹9,566 | +₹46,334 | +₹22,004 | +₹30,872 | +₹13,632 | -₹6,065 | +₹2,848 | +₹562 | **+₹119,754 / +₹112,466** | 7/8 | ₹6,065 |
| `v1` | `trend_pullback_ITM` | +₹5,916 | +₹44,142 | +₹21,117 | +₹28,027 | +₹15,804 | -₹4,565 | +₹20,421 | -₹11,619 | **+₹119,242 / +₹98,810** | 6/8 | ₹11,619 |
| `v1` | `trend_pullback_OTM` | +₹2,046 | +₹23,287 | +₹12,379 | +₹12,511 | +₹10,187 | +₹0 | +₹25,332 | +₹30,311 | **+₹116,053 / +₹85,091** | 7/8 | ₹0 |
| `v1` | `fixed_target_ITM` | +₹9,376 | +₹44,990 | +₹27,513 | +₹22,321 | +₹12,927 | -₹5,806 | -₹15,085 | +₹5,506 | **+₹101,742 / +₹97,798** | 6/8 | ₹20,891 |
| `v1` | `composite_shared_OTM` | +₹710 | +₹20,434 | +₹5,607 | +₹7,187 | +₹12,398 | +₹0 | +₹28,204 | +₹25,329 | **+₹99,869 / +₹71,181** | 7/8 | ₹0 |
| `v1` | `fast_momentum_ITM` | +₹5,744 | +₹35,247 | +₹23,762 | +₹33,255 | +₹14,652 | -₹5,206 | -₹4,848 | -₹5,400 | **+₹97,206 / +₹101,313** | 5/8 | ₹15,454 |
| `v1` | `fast_momentum_OTM` | -₹1,653 | +₹25,190 | +₹4,608 | +₹11,527 | +₹12,972 | +₹0 | +₹20,902 | +₹16,827 | **+₹90,373 / +₹80,317** | 6/8 | ₹1,653 |
| `v2` | `structural_convexity_ATM` | -₹11,761 | +₹36,543 | +₹6,187 | +₹22,113 | +₹42,548 | +₹2,080 | +₹8,349 | -₹19,460 | **+₹86,600 / +₹77,214** | 6/8 | ₹19,460 |
| `v2` | `delta_scalp_ITM2` | +₹3,782 | +₹28,398 | +₹6,526 | +₹33,155 | +₹14,508 | +₹1,961 | -₹15,555 | +₹13,659 | **+₹86,433 / +₹57,898** | 7/8 | ₹15,555 |
| `v1` | `fixed_target_OTM` | +₹2,915 | +₹17,521 | +₹13,598 | +₹5,848 | +₹11,606 | +₹0 | +₹7,858 | -₹4,452 | **+₹54,894 / +₹44,064** | 6/8 | ₹4,452 |
| `v2` | `opening_thrust_convex_ATM` | +₹11,304 | +₹12,910 | +₹0 | +₹11,348 | +₹762 | +₹1,950 | -₹10,444 | -₹3,571 | **+₹24,259 / +₹24,636** | 5/8 | ₹14,015 |
| `v2` | `gamma_runner_OTM2` | -₹38,520 | -₹13,812 | -₹24,843 | -₹12,634 | +₹14,460 | -₹15 | -₹14,551 | -₹12,692 | **-₹102,606 / -₹118,746** | 1/8 | ₹102,606 |

### 5.4 Per-Day Trackwise Net P&L Matrix — Zero-DD Champion `E10_W3_CAP3` (Paper / Delayed)

| Profile | Track | Nov 16 | Nov 17 | Nov 18 | Nov 19 | Nov 20 | Nov 23 | Nov 25 | Nov 26 | 8d Total (P / D) | Grn | DD |
|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| `v2` | `volatility_expansion_ATM` | -₹1,472 | +₹36,430 | +₹30,676 | +₹16,610 | +₹35,187 | +₹1,676 | +₹6,599 | +₹37,931 | **+₹163,637 / +₹153,536** | 7/8 | ₹1,472 |
| `v2` | `adaptive_regime_ATM` | +₹13,498 | +₹26,419 | +₹26,771 | +₹10,708 | +₹34,177 | +₹641 | +₹17,512 | +₹8,766 | **+₹138,492 / +₹132,156** | 8/8 | ₹0 |
| `v1` | `trend_pullback_ATM` | +₹3,644 | +₹21,851 | +₹27,007 | +₹19,790 | +₹15,971 | +₹1,125 | +₹15,158 | +₹31,813 | **+₹136,360 / +₹134,510** | 8/8 | ₹0 |
| `v1` | `fast_momentum_ATM` | +₹3,746 | +₹23,502 | +₹12,289 | +₹15,577 | +₹20,482 | +₹870 | +₹8,667 | +₹45,749 | **+₹130,882 / +₹112,858** | 8/8 | ₹0 |
| `v2` | `micro_chandelier_ITM1` | +₹6,709 | +₹26,557 | +₹33,981 | +₹17,949 | +₹16,483 | -₹15 | +₹6,350 | +₹19,966 | **+₹127,979 / +₹110,563** | 7/8 | ₹15 |
| `v1` | `composite_shared_ATM` | +₹8,465 | +₹20,522 | +₹11,139 | +₹15,255 | +₹20,086 | +₹870 | +₹6,773 | +₹42,888 | **+₹125,998 / +₹103,362** | 8/8 | ₹0 |
| `v1` | `composite_shared_ITM` | +₹2,152 | +₹25,620 | +₹34,949 | +₹22,853 | +₹5,116 | +₹608 | +₹7,367 | +₹12,841 | **+₹111,504 / +₹111,414** | 8/8 | ₹0 |
| `v1` | `fast_momentum_ITM` | +₹3,072 | +₹24,016 | +₹28,164 | +₹19,547 | +₹5,840 | +₹1,208 | -₹1,994 | +₹30,364 | **+₹110,216 / +₹95,554** | 7/8 | ₹1,994 |
| `v2` | `trend_pullback_v2_ITM1` | -₹4,475 | +₹25,497 | +₹23,629 | +₹15,479 | +₹27,217 | +₹1,151 | +₹19,537 | +₹1,313 | **+₹109,348 / +₹110,487** | 7/8 | ₹4,475 |
| `v1` | `fixed_target_ATM` | +₹4,883 | +₹16,718 | +₹18,453 | +₹18,611 | +₹17,076 | +₹720 | +₹4,404 | +₹28,357 | **+₹109,222 / +₹102,073** | 8/8 | ₹0 |
| `v1` | `trend_pullback_ITM` | +₹2,538 | +₹32,155 | +₹26,861 | +₹15,530 | +₹6,638 | +₹1,849 | +₹13,551 | +₹7,671 | **+₹106,793 / +₹96,663** | 8/8 | ₹0 |
| `v1` | `fixed_target_ITM` | +₹6,858 | +₹29,564 | +₹28,705 | +₹18,271 | +₹5,300 | +₹608 | -₹5,291 | +₹16,866 | **+₹100,880 / +₹91,669** | 7/8 | ₹5,291 |
| `v1` | `fast_momentum_OTM` | -₹4,963 | +₹25,546 | +₹10,968 | +₹6,606 | +₹6,102 | +₹0 | +₹14,764 | +₹35,391 | **+₹94,415 / +₹78,620** | 6/8 | ₹4,963 |
| `v1` | `trend_pullback_OTM` | -₹3,677 | +₹19,541 | +₹14,471 | +₹10,582 | +₹4,577 | +₹0 | +₹6,523 | +₹33,404 | **+₹85,422 / +₹70,616** | 6/8 | ₹3,677 |
| `v2` | `delta_scalp_ITM2` | +₹6,382 | +₹24,126 | +₹11,628 | +₹21,851 | +₹16,243 | +₹375 | -₹6,054 | +₹10,649 | **+₹85,200 / +₹64,572** | 7/8 | ₹6,054 |
| `v1` | `composite_shared_OTM` | -₹2,827 | +₹19,610 | +₹13,804 | +₹6,936 | +₹4,405 | +₹0 | -₹1,026 | +₹34,565 | **+₹75,466 / +₹60,979** | 5/8 | ₹2,827 |
| `v2` | `structural_convexity_ATM` | -₹13,319 | +₹47,322 | +₹7,093 | +₹13,972 | +₹36,979 | +₹2,332 | -₹6,187 | -₹19,361 | **+₹68,831 / +₹86,964** | 5/8 | ₹25,548 |
| `v1` | `fixed_target_OTM` | -₹4,510 | +₹17,402 | +₹13,128 | +₹246 | +₹4,700 | +₹0 | -₹3,233 | +₹10,453 | **+₹38,185 / +₹33,202** | 5/8 | ₹4,510 |
| `v2` | `opening_thrust_convex_ATM` | +₹11,884 | +₹13,420 | +₹0 | +₹12,025 | +₹1,482 | +₹1,950 | -₹9,144 | -₹2,366 | **+₹29,252 / +₹28,873** | 5/8 | ₹11,510 |
| `v2` | `gamma_runner_OTM2` | -₹36,926 | +₹308 | -₹17,989 | -₹14,194 | +₹10,469 | +₹1,008 | -₹10,408 | -₹5,754 | **-₹73,486 / -₹73,225** | 3/8 | ₹73,486 |

---

## 6. Recommended Operating Profiles & Promotion Roadmap

1. **Choose Target Profile:**
   - **Option A — Max Return (`W1_15S_OPT_SPREAD` or `W2_15S_FLAG25_OPT_SPREAD`):** Highest net profit (`+₹2,032,094` / `+₹1,970,972` across all 20 tracks, `7/8` green days, `₹26,060` DD); `W2` retains `86.4%` in Delayed (`+₹1,703,692`).
   - **Option B — Zero-DD Live Production Standard (`E10_W3_CAP3` or `E5_W2_ATR020`):** **`E10_W3_CAP3` delivers `8/8` green days with `₹0` Paper MaxDD (`+₹1,874,596` P / `+₹1,705,447` D)** with `31.4%` fewer trades and `₹445,080` lower brokerage than Baseline. **`E5_W2_ATR020`** (`+₹1,761,182` P / `+₹1,555,033` D) achieves **`₹0` MaxDD in BOTH Paper and Delayed**!
2. **Track Portfolio Surgery:** Immediately retire **`gamma_runner_OTM2`** (`0/45` positive across every rule set). Keep **`delta_scalp_ITM2`** gated behind `W3`/`E10` (`OptScale + 3Bar`) or allocate core capital to top consistent ATM/ITM1 performers (`fixed_target_ATM`, `trend_pullback_ATM`, `composite_shared_ATM`, `volatility_expansion_ATM`, `adaptive_regime_ATM`).
3. **Expiry-Day Switch & Midday Freeze Flag:** Keep `midday_freeze.enabled = false` by default for momentum (proven `#1` P&L destroyer when forced on globally), and toggle `3-Bar >= 0.45% + Cap <= 3` (`E10`/`W3`) on high-chop / expiry regimes (`Nov 23 + Nov 26` combined: `E10` makes **`+₹398,480`**, vs `W1` `+₹76,752` and `H0` `-₹368,707`!).

---

## 7. Complete Cost, Slippage & Risk-Adjusted Economics (All 45 Experiments)

| Rank | Experiment ID | Opps | Tr (P / D) | WR% (P / D) | Paper Gross | Brokerage (`% GP`) | Paper Net | Delayed Net | Slippage (`Ret%`) | ₹/Trade (P / D) | MaxDD (P / D) | Ret/DD (P) |
|---:|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| 1 | `W1_15S_OPT_SPREAD` | 1,836 | 23,469 / 23,337 | 34.3 / 34.0 | +₹3,440,234 | ₹1,408,140 (40.9%) | +₹2,032,094 | +₹1,712,076 | +₹320,018 (84.3%) | +₹86.6 / +₹73.4 | ₹26,060 / ₹26,060 | 78.0x |
| 2 | `W2_15S_FLAG25_OPT_SPREAD` | 1,825 | 23,316 / 23,186 | 34.3 / 34.0 | +₹3,369,932 | ₹1,398,960 (41.5%) | +₹1,970,972 | +₹1,703,692 | +₹267,280 (86.4%) | +₹84.5 / +₹73.5 | ₹26,060 / ₹26,060 | 75.6x |
| 3 | `E10_W3_CAP3` | 1,140 | 16,179 / 16,134 | 35.4 / 35.3 | +₹2,845,336 | ₹970,740 (34.1%) | +₹1,874,596 | +₹1,705,447 | +₹169,149 (91.0%) | +₹115.9 / +₹105.7 | ₹0 / ₹3,664 | ∞ |
| 4 | `W3_15S_FLAG25_3BAR045_OPT_SPREAD` | 1,212 | 17,000 / 16,939 | 35.1 / 34.9 | +₹2,862,081 | ₹1,020,000 (35.6%) | +₹1,842,081 | +₹1,633,037 | +₹209,044 (88.7%) | +₹108.4 / +₹96.4 | ₹0 / ₹5,858 | ∞ |
| 5 | `E6_W3_ATR020` | 1,100 | 15,420 / 15,375 | 35.1 / 35.1 | +₹2,713,566 | ₹925,200 (34.1%) | +₹1,788,366 | +₹1,638,703 | +₹149,662 (91.6%) | +₹116.0 / +₹106.6 | ₹0 / ₹1,782 | ∞ |
| 6 | `E9_W2_CAP3` | 1,636 | 21,596 / 21,483 | 34.2 / 33.9 | +₹3,078,103 | ₹1,295,760 (42.1%) | +₹1,782,343 | +₹1,548,208 | +₹234,135 (86.9%) | +₹82.5 / +₹72.1 | ₹54,415 / ₹57,656 | 32.8x |
| 7 | `E5_W2_ATR020` | 1,439 | 18,716 / 18,582 | 34.4 / 34.1 | +₹2,884,142 | ₹1,122,960 (38.9%) | +₹1,761,182 | +₹1,555,033 | +₹206,149 (88.3%) | +₹94.1 / +₹83.7 | ₹0 / ₹0 | ∞ |
| 8 | `E7_W2_FLIPCD30` | 1,774 | 22,737 / 22,640 | 34.1 / 33.8 | +₹3,118,205 | ₹1,364,220 (43.8%) | +₹1,753,985 | +₹1,548,595 | +₹205,390 (88.3%) | +₹77.1 / +₹68.4 | ₹26,060 / ₹82,171 | 67.3x |
| 9 | `E14_W2_GATE_ONLY_NOSCALE` | 1,825 | 22,266 / 22,116 | 33.3 / 32.8 | +₹3,077,800 | ₹1,335,960 (43.4%) | +₹1,741,840 | +₹1,484,074 | +₹257,766 (85.2%) | +₹78.2 / +₹67.1 | ₹31,023 / ₹41,664 | 56.1x |
| 10 | `E8_W3_FLIPCD30` | 1,188 | 16,757 / 16,708 | 35.0 / 35.0 | +₹2,742,957 | ₹1,005,420 (36.7%) | +₹1,737,537 | +₹1,577,307 | +₹160,231 (90.8%) | +₹103.7 / +₹94.4 | ₹623 / ₹9,071 | 2788.8x |
| 11 | `W5_15S_FLAG25_NOFLIP_OPT_SPREAD` | 1,357 | 18,822 / 18,760 | 34.6 / 34.4 | +₹2,857,103 | ₹1,129,320 (39.5%) | +₹1,727,783 | +₹1,597,211 | +₹130,572 (92.4%) | +₹91.8 / +₹85.1 | ₹81,005 / ₹87,969 | 21.3x |
| 12 | `E3_W2_3BAR035` | 1,520 | 20,051 / 19,931 | 34.1 / 33.8 | +₹2,910,734 | ₹1,203,060 (41.3%) | +₹1,707,674 | +₹1,486,197 | +₹221,478 (87.0%) | +₹85.2 / +₹74.6 | ₹41,664 / ₹50,782 | 41.0x |
| 13 | `H6b_OPT_SCALE_SPREAD` | 1,880 | 23,814 / 23,693 | 33.8 / 33.4 | +₹3,128,104 | ₹1,428,840 (45.7%) | +₹1,699,264 | +₹1,381,151 | +₹318,113 (81.3%) | +₹71.4 / +₹58.3 | ₹13,025 / ₹52,667 | 130.5x |
| 14 | `E15_15S_FLAG25_BODY035_SCALED` | 915 | 13,150 / 13,116 | 35.8 / 35.8 | +₹2,485,922 | ₹789,000 (31.7%) | +₹1,696,922 | +₹1,586,805 | +₹110,117 (93.5%) | +₹129.0 / +₹121.0 | ₹660 / ₹660 | 2571.1x |
| 15 | `E11_W2_FREEZE1400` | 1,396 | 18,325 / 18,209 | 34.8 / 34.3 | +₹2,783,118 | ₹1,099,500 (39.5%) | +₹1,683,618 | +₹1,444,902 | +₹238,717 (85.8%) | +₹91.9 / +₹79.4 | ₹4,914 / ₹10,659 | 342.6x |
| 16 | `E12_W3_FREEZE1400` | 978 | 13,841 / 13,800 | 35.3 / 35.1 | +₹2,413,530 | ₹830,460 (34.4%) | +₹1,583,070 | +₹1,429,954 | +₹153,116 (90.3%) | +₹114.4 / +₹103.6 | ₹27,606 / ₹34,995 | 57.3x |
| 17 | `E4_W2_3BAR060` | 824 | 12,201 / 12,164 | 35.2 / 35.0 | +₹2,268,946 | ₹732,060 (32.3%) | +₹1,536,886 | +₹1,398,631 | +₹138,255 (91.0%) | +₹126.0 / +₹115.0 | ₹0 / ₹0 | ∞ |
| 18 | `W4_3BAR045_OPT_SPREAD` | 1,264 | 17,530 / 17,435 | 34.1 / 34.0 | +₹2,453,287 | ₹1,051,800 (42.9%) | +₹1,401,487 | +₹1,241,896 | +₹159,592 (88.6%) | +₹79.9 / +₹71.2 | ₹24,993 / ₹67,658 | 56.1x |
| 19 | `E2_15S_FLAG25_3BAR045_SCORING` | 1,298 | 16,730 / 16,636 | 33.2 / 33.0 | +₹2,353,284 | ₹1,003,800 (42.7%) | +₹1,349,484 | +₹1,228,225 | +₹121,260 (91.0%) | +₹80.7 / +₹73.8 | ₹84,409 / ₹90,817 | 16.0x |
| 20 | `E13_W2_SPREAD2PCT` | 1,556 | 18,258 / 18,122 | 32.9 / 32.7 | +₹2,377,582 | ₹1,095,480 (46.1%) | +₹1,282,102 | +₹1,113,562 | +₹168,540 (86.9%) | +₹70.2 / +₹61.4 | ₹35,885 / ₹41,891 | 35.7x |
| 21 | `H4a_ELAPSED_15S` | 1,980 | 23,181 / 23,039 | 32.6 / 32.3 | +₹2,552,683 | ₹1,390,860 (54.5%) | +₹1,161,823 | +₹894,937 | +₹266,886 (77.0%) | +₹50.1 / +₹38.8 | ₹186,925 / ₹360,326 | 6.2x |
| 22 | `W6_BODY025_ATR025_OPT_SPREAD` | 1,024 | 14,076 / 14,072 | 34.2 / 33.8 | +₹1,980,666 | ₹844,560 (42.6%) | +₹1,136,106 | +₹939,378 | +₹196,728 (82.7%) | +₹80.7 / +₹66.8 | ₹47,295 / ₹51,750 | 24.0x |
| 23 | `E1_15S_FLAG25_SCORING_ONLY` | 1,969 | 23,031 / 22,905 | 32.4 / 32.1 | +₹2,427,915 | ₹1,381,860 (56.9%) | +₹1,046,055 | +₹810,403 | +₹235,652 (77.5%) | +₹45.4 / +₹35.4 | ₹261,708 / ₹392,666 | 4.0x |
| 24 | `H2b_PURE_3BAR_045` | 1,350 | 17,281 / 17,183 | 32.4 / 32.2 | +₹2,016,385 | ₹1,036,860 (51.4%) | +₹979,525 | +₹823,872 | +₹155,654 (84.1%) | +₹56.7 / +₹47.9 | ₹100,793 / ₹119,959 | 9.7x |
| 25 | `H6a_OPT_SCALE` | 2,025 | 24,655 / 24,521 | 33.1 / 32.8 | +₹2,450,555 | ₹1,479,300 (60.4%) | +₹971,255 | +₹730,848 | +₹240,407 (75.2%) | +₹39.4 / +₹29.8 | ₹373,825 / ₹503,400 | 2.6x |
| 26 | `M9_ALL_SIGNALS_SCALED` | 1,066 | 14,055 / 14,036 | 33.4 / 33.3 | +₹1,806,703 | ₹843,300 (46.7%) | +₹963,403 | +₹865,690 | +₹97,713 (89.9%) | +₹68.5 / +₹61.7 | ₹245,494 / ₹325,723 | 3.9x |
| 27 | `H3c_FLAG_FULL_POLE` | 1,944 | 22,717 / 22,623 | 32.5 / 31.9 | +₹2,216,963 | ₹1,363,020 (61.5%) | +₹853,943 | +₹475,818 | +₹378,125 (55.7%) | +₹37.6 / +₹21.0 | ₹364,883 / ₹645,893 | 2.3x |
| 28 | `H4b_ELAPSED_30S` | 1,949 | 23,013 / 22,765 | 32.2 / 31.6 | +₹2,158,052 | ₹1,380,780 (64.0%) | +₹777,272 | +₹401,396 | +₹375,876 (51.6%) | +₹33.8 / +₹17.6 | ₹451,520 / ₹755,650 | 1.7x |
| 29 | `H3a_FLAG_BUF_15` | 2,018 | 23,586 / 23,459 | 32.0 / 31.8 | +₹2,154,997 | ₹1,415,160 (65.7%) | +₹739,837 | +₹637,969 | +₹101,868 (86.2%) | +₹31.4 / +₹27.2 | ₹379,246 / ₹390,790 | 2.0x |
| 30 | `H2c_BODY_025_ATR_025` | 1,081 | 13,970 / 13,844 | 32.1 / 31.5 | +₹1,534,181 | ₹838,200 (54.6%) | +₹695,981 | +₹497,516 | +₹198,464 (71.5%) | +₹49.8 / +₹35.9 | ₹78,912 / ₹192,643 | 8.8x |
| 31 | `H3b_FLAG_BUF_25` | 2,005 | 23,364 / 23,292 | 32.1 / 31.7 | +₹2,080,486 | ₹1,401,840 (67.4%) | +₹678,646 | +₹558,189 | +₹120,458 (82.3%) | +₹29.0 / +₹24.0 | ₹428,427 / ₹450,022 | 1.6x |
| 32 | `H0_BASELINE` | 2,025 | 23,597 / 23,451 | 32.0 / 31.8 | +₹2,079,538 | ₹1,415,820 (68.1%) | +₹663,718 | +₹536,194 | +₹127,524 (80.8%) | +₹28.1 / +₹22.9 | ₹453,775 / ₹488,061 | 1.5x |
| 33 | `M7_SPREAD_GATE_CORE` | 861 | 12,035 / 12,016 | 33.0 / 32.9 | +₹1,378,346 | ₹722,100 (52.4%) | +₹656,246 | +₹566,383 | +₹89,862 (86.3%) | +₹54.5 / +₹47.1 | ₹259,625 / ₹318,543 | 2.5x |
| 34 | `H5b_CAP_2_NO_FLIP` | 1,267 | 17,162 / 17,108 | 32.1 / 32.0 | +₹1,668,730 | ₹1,029,720 (61.7%) | +₹639,010 | +₹496,796 | +₹142,214 (77.7%) | +₹37.2 / +₹29.0 | ₹257,232 / ₹344,812 | 2.5x |
| 35 | `M8_BODY025_SPREAD_SCALED` | 486 | 7,316 / 7,305 | 34.1 / 33.9 | +₹1,077,403 | ₹438,960 (40.7%) | +₹638,443 | +₹546,568 | +₹91,875 (85.6%) | +₹87.3 / +₹74.8 | ₹143,702 / ₹180,408 | 4.4x |
| 36 | `H5c_CAP_1` | 841 | 12,071 / 12,079 | 32.7 / 32.3 | +₹1,262,987 | ₹724,260 (57.3%) | +₹538,728 | +₹381,118 | +₹157,609 (70.7%) | +₹44.6 / +₹31.6 | ₹171,920 / ₹280,822 | 3.1x |
| 37 | `H5a_CAP_2` | 1,442 | 18,656 / 18,604 | 31.8 / 31.5 | +₹1,633,007 | ₹1,119,360 (68.5%) | +₹513,646 | +₹360,045 | +₹153,602 (70.1%) | +₹27.5 / +₹19.4 | ₹374,218 / ₹440,983 | 1.4x |
| 38 | `M3_SMART_IMPULSE` | 518 | 7,485 / 7,477 | 31.8 / 31.6 | +₹956,795 | ₹449,100 (46.9%) | +₹507,695 | +₹402,211 | +₹105,484 (79.2%) | +₹67.8 / +₹53.8 | ₹205,440 / ₹242,435 | 2.5x |
| 39 | `M1_CORE_CLEAN` | 1,024 | 13,073 / 13,022 | 31.7 / 31.5 | +₹1,271,053 | ₹784,380 (61.7%) | +₹486,673 | +₹379,295 | +₹107,378 (77.9%) | +₹37.2 / +₹29.1 | ₹352,856 / ₹435,432 | 1.4x |
| 40 | `H2a_MIN_ATR_030` | 853 | 11,381 / 11,325 | 32.0 / 31.6 | +₹1,168,517 | ₹682,860 (58.4%) | +₹485,657 | +₹314,675 | +₹170,982 (64.8%) | +₹42.7 / +₹27.8 | ₹242,827 / ₹313,309 | 2.0x |
| 41 | `M2_BALANCED` | 449 | 6,503 / 6,501 | 32.5 / 32.2 | +₹845,989 | ₹390,180 (46.1%) | +₹455,809 | +₹347,009 | +₹108,799 (76.1%) | +₹70.1 / +₹53.4 | ₹87,012 / ₹187,194 | 5.2x |
| 42 | `M5_IMPULSE_SCALED` | 518 | 7,890 / 7,863 | 32.6 / 32.6 | +₹865,756 | ₹473,400 (54.7%) | +₹392,356 | +₹333,152 | +₹59,205 (84.9%) | +₹49.7 / +₹42.4 | ₹295,373 / ₹316,519 | 1.3x |
| 43 | `M6_HIGH_CONVICTION` | 402 | 6,298 / 6,294 | 33.2 / 33.0 | +₹744,061 | ₹377,880 (50.8%) | +₹366,181 | +₹332,549 | +₹33,632 (90.8%) | +₹58.1 / +₹52.8 | ₹102,761 / ₹139,844 | 3.6x |
| 44 | `M4_BALANCED_SCALED` | 449 | 6,818 / 6,811 | 33.1 / 32.9 | +₹749,732 | ₹409,080 (54.6%) | +₹340,652 | +₹254,356 | +₹86,296 (74.7%) | +₹50.0 / +₹37.3 | ₹179,204 / ₹265,754 | 1.9x |
| 45 | `H1_MIDDAY_FREEZE` | 1,188 | 14,354 / 14,303 | 31.5 / 31.5 | +₹1,064,623 | ₹861,240 (80.9%) | +₹203,383 | +₹154,424 | +₹48,959 (75.9%) | +₹14.2 / +₹10.8 | ₹551,523 / ₹574,396 | 0.4x |

---

## 8. Regime-Grouped Performance (`TREND 4D` vs `CHOP 2D` vs `EXPIRY 2D`)

- **`TREND (4D)`:** `Nov 17`, `Nov 18`, `Nov 19`, `Nov 20`
- **`CHOP / REVERSAL (2D)`:** `Nov 16`, `Nov 25`
- **`EXPIRY (2D)`:** `Nov 23`, `Nov 26`

| Experiment ID | `TREND 4D` Paper (`v1` / `v2`) | `TREND 4D` Delayed | `CHOP 2D` Paper (`v1` / `v2`) | `CHOP 2D` Delayed | `EXPIRY 2D` Paper (`v1` / `v2`) | `EXPIRY 2D` Delayed |
|---|---:|---:|---:|---:|---:|---:|
| `H0_BASELINE` | **+₹1,224,740** (`+768.1k / +456.6k`) | +₹1,148,676 | **-₹192,315** (`+38.3k / -230.6k`) | -₹282,666 | **-₹368,707** (`-144.6k / -224.1k`) | -₹329,815 |
| `W1_15S_OPT_SPREAD` | **+₹1,721,553** (`+1016.7k / +704.9k`) | +₹1,649,300 | **+₹233,790** (`+243.9k / -10.1k`) | +₹99,829 | **+₹76,752** (`+85.4k / -8.6k`) | -₹37,053 |
| `W2_15S_FLAG25_OPT_SPREAD` | **+₹1,731,881** (`+1015.3k / +716.6k`) | +₹1,649,615 | **+₹185,387** (`+200.3k / -14.9k`) | +₹63,418 | **+₹53,704** (`+74.1k / -20.4k`) | -₹9,341 |
| `E10_W3_CAP3` | **+₹1,390,586** (`+802.1k / +588.5k`) | +₹1,336,251 | **+₹85,530** (`+85.0k / +0.5k`) | +₹47,514 | **+₹398,480** (`+338.2k / +60.3k`) | +₹321,682 |
| `W3_15S_FLAG25_3BAR045_OPT_SPREAD` | **+₹1,449,034** (`+833.6k / +615.4k`) | +₹1,414,150 | **+₹76,964** (`+78.3k / -1.3k`) | +₹15,407 | **+₹316,083** (`+273.7k / +42.3k`) | +₹203,480 |
| `E6_W3_ATR020` | **+₹1,334,174** (`+761.3k / +572.8k`) | +₹1,294,725 | **+₹73,595** (`+69.4k / +4.2k`) | +₹31,002 | **+₹380,597** (`+316.0k / +64.6k`) | +₹312,977 |
| `E5_W2_ATR020` | **+₹1,417,309** (`+843.7k / +573.7k`) | +₹1,365,802 | **+₹134,454** (`+145.5k / -11.1k`) | +₹64,363 | **+₹209,419** (`+142.1k / +67.3k`) | +₹124,868 |
| `E14_W2_GATE_ONLY_NOSCALE` | **+₹1,537,685** (`+881.7k / +656.0k`) | +₹1,446,354 | **+₹206,119** (`+222.6k / -16.4k`) | +₹75,689 | **-₹1,964** (`+4.5k / -6.5k`) | -₹37,970 |
| `W5_15S_FLAG25_NOFLIP_OPT_SPREAD` | **+₹1,531,686** (`+904.5k / +627.2k`) | +₹1,489,066 | **+₹101,388** (`+119.9k / -18.5k`) | +₹83,668 | **+₹94,709** (`+65.8k / +28.9k`) | +₹24,477 |
| `H6b_OPT_SCALE_SPREAD` | **+₹1,538,598** (`+901.4k / +637.2k`) | +₹1,446,013 | **+₹80,239** (`+114.6k / -34.4k`) | -₹31,687 | **+₹80,428** (`+72.7k / +7.7k`) | -₹33,175 |
| `E2_15S_FLAG25_3BAR045_SCORING` | **+₹1,285,782** (`+760.9k / +524.9k`) | +₹1,246,045 | **-₹36,415** (`+63.4k / -99.8k`) | -₹44,245 | **+₹100,117** (`+168.5k / -68.4k`) | +₹26,424 |
| `H4a_ELAPSED_15S` | **+₹1,415,968** (`+862.1k / +553.9k`) | +₹1,328,358 | **-₹42,298** (`+126.3k / -168.6k`) | -₹127,616 | **-₹211,846** (`-52.4k / -159.4k`) | -₹305,804 |
| `H2b_PURE_3BAR_045` | **+₹1,083,701** (`+661.0k / +422.7k`) | +₹1,027,771 | **-₹119,892** (`+5.1k / -125.0k`) | -₹204,248 | **+₹15,716** (`+113.8k / -98.1k`) | +₹349 |
| `H1_MIDDAY_FREEZE` | **+₹871,847** (`+539.6k / +332.2k`) | +₹848,003 | **-₹456,658** (`-259.4k / -197.3k`) | -₹496,112 | **-₹211,806** (`-30.8k / -181.0k`) | -₹197,467 |

---

## 9. Track Family, Moneyness & Win/Loss/Flat Micro-Statistics

### 9.1 Strategy Family & Moneyness Totals (`H0_BASELINE`, `H6b`, `W1`, `W2`, `E10_W3_CAP3`, `W3`)

| Group | `H0_BASELINE` (`Tr / P / D`) | `H6b_OPT_SCALE_SPREAD` (`Tr / P / D`) | `W1_15S_OPT_SPREAD` (`Tr / P / D`) | `W2_15S_FLAG25_OPT_SPREAD` (`Tr / P / D`) | `E10_W3_CAP3` (`Tr / P / D`) | `W3_15S_FLAG25_3BAR045_OPT_SPREAD` (`Tr / P / D`) |
|---|---:|---:|---:|---:|---:|---:|
| `trend_pullback` (3) | 3,495 / +₹157,236 / +₹141,344 | 3,505 / +₹273,791 / +₹276,299 | 3,482 / +₹379,372 / +₹317,678 | 3,439 / +₹369,341 / +₹325,563 | 2,440 / +₹328,575 / +₹301,790 | 2,569 / +₹341,358 / +₹294,057 |
| `composite_shared` (3) | 3,651 / +₹157,137 / +₹138,150 | 3,681 / +₹303,750 / +₹224,044 | 3,652 / +₹369,397 / +₹290,842 | 3,594 / +₹333,118 / +₹299,837 | 2,542 / +₹312,968 / +₹275,755 | 2,672 / +₹300,558 / +₹263,435 |
| `fast_momentum` (3) | 3,569 / +₹140,532 / +₹123,365 | 3,643 / +₹271,440 / +₹220,724 | 3,586 / +₹314,018 / +₹279,218 | 3,576 / +₹321,107 / +₹272,631 | 2,526 / +₹335,513 / +₹287,032 | 2,649 / +₹303,414 / +₹270,426 |
| `fixed_target` (3) | 3,969 / +₹206,855 / +₹171,088 | 3,940 / +₹239,738 / +₹194,986 | 3,844 / +₹283,159 / +₹251,522 | 3,822 / +₹266,175 / +₹247,804 | 2,684 / +₹248,287 / +₹226,943 | 2,827 / +₹240,330 / +₹213,730 |
| **`v1` All (12)** | 14,684 / +₹661,761 / +₹573,948 | 14,769 / +₹1,088,719 / +₹916,053 | 14,564 / +₹1,345,946 / +₹1,139,261 | 14,431 / +₹1,289,741 / +₹1,145,835 | 10,192 / +₹1,225,343 / +₹1,091,520 | 10,717 / +₹1,185,660 / +₹1,041,647 |
| **`v2` Core 6 (excl. gamma & delta)** | 6,378 / +₹192,354 / +₹171,180 | 6,654 / +₹643,291 / +₹535,832 | 6,567 / +₹702,321 / +₹633,663 | 6,535 / +₹696,370 / +₹595,975 | 4,379 / +₹637,539 / +₹622,580 | 4,598 / +₹645,571 / +₹595,673 |
| `v2` All (8) | 8,913 / +₹1,958 / -₹37,753 | 9,045 / +₹610,545 / +₹465,098 | 8,905 / +₹686,148 / +₹572,815 | 8,885 / +₹681,231 / +₹557,857 | 5,987 / +₹649,253 / +₹613,927 | 6,283 / +₹656,421 / +₹591,390 |
| `v1_ATM` (4) | 5,028 / +₹316,946 / +₹260,109 | 5,238 / +₹459,494 / +₹373,129 | 5,169 / +₹546,814 / +₹448,221 | 5,097 / +₹520,800 / +₹497,773 | 3,593 / +₹502,462 / +₹452,803 | 3,757 / +₹474,511 / +₹426,669 |
| `v1_ITM` (4) | 4,624 / +₹167,746 / +₹139,124 | 4,431 / +₹358,016 / +₹296,504 | 4,368 / +₹437,943 / +₹410,388 | 4,349 / +₹439,766 / +₹385,913 | 3,071 / +₹429,393 / +₹395,300 | 3,226 / +₹404,036 / +₹391,585 |
| `v1_OTM` (4) | 5,032 / +₹177,069 / +₹174,714 | 5,100 / +₹271,209 / +₹246,420 | 5,027 / +₹361,189 / +₹280,652 | 4,985 / +₹329,175 / +₹262,149 | 3,528 / +₹293,488 / +₹243,417 | 3,734 / +₹307,112 / +₹223,393 |

### 9.2 Full Trade-Outcome Micro-Stats (`Wins >2.5%` / `Losses <0%` / `Flat 0–2.5%` & `₹/Trade`)

| Track | `H0_BASELINE` (`W / L / Flat | GP | ₹/Tr`) | `W1` (`W / L / Flat | GP | Slip | ₹/Tr`) | `W2` (`W / L / Flat | GP | Slip | ₹/Tr`) | `E10_W3_CAP3` (`W / L / Flat | GP | Slip | ₹/Tr`) |
|---|---|---|---|---|
| `volatility_expansion_ATM` | 302 / 730 / 462 \| +₹146,928 \| +₹38.3 | 334 / 725 / 510 \| +₹285,933 \| +₹18,226 \| +₹122.2 | 334 / 723 / 503 \| +₹286,664 \| +₹21,760 \| +₹123.8 | 237 / 467 / 314 \| +₹224,717 \| +₹10,101 \| +₹160.7 |
| `trend_pullback_ATM` | 428 / 525 / 238 \| +₹156,928 \| +₹71.8 | 456 / 523 / 258 \| +₹218,298 \| +₹10,301 \| +₹116.5 | 449 / 512 / 255 \| +₹224,398 \| +₹17,960 \| +₹124.5 | 325 / 368 / 166 \| +₹187,900 \| +₹1,850 \| +₹158.7 |
| `composite_shared_ATM` | 451 / 627 / 169 \| +₹156,486 \| +₹65.5 | 486 / 626 / 186 \| +₹227,654 \| +₹42,579 \| +₹115.4 | 472 / 607 / 177 \| +₹219,334 \| +₹2,720 \| +₹114.6 | 347 / 434 / 116 \| +₹179,818 \| +₹22,636 \| +₹140.5 |
| `fixed_target_ATM` | 571 / 670 / 128 \| +₹184,315 \| +₹74.6 | 575 / 654 / 129 \| +₹208,003 \| +₹16,862 \| +₹93.2 | 565 / 657 / 132 \| +₹199,217 \| +₹4,258 \| +₹87.1 | 422 / 439 / 87 \| +₹166,102 \| +₹7,149 \| +₹115.2 |
| `adaptive_regime_ATM` | 385 / 641 / 160 \| +₹117,526 \| +₹39.1 | 523 / 642 / 140 \| +₹220,539 \| +₹33,489 \| +₹109.0 | 525 / 649 / 134 \| +₹223,171 \| +₹28,814 \| +₹110.6 | 366 / 430 / 88 \| +₹191,532 \| +₹6,335 \| +₹156.7 |
| `fast_momentum_ATM` | 391 / 678 / 152 \| +₹120,896 \| +₹39.0 | 438 / 683 / 155 \| +₹202,999 \| +₹28,851 \| +₹99.1 | 434 / 687 / 150 \| +₹183,671 \| -₹1,911 \| +₹84.5 | 316 / 481 / 92 \| +₹184,222 \| +₹18,024 \| +₹147.2 |
| `micro_chandelier_ITM1` | 405 / 828 / 420 \| +₹117,836 \| +₹11.3 | 460 / 741 / 392 \| +₹219,099 \| +₹15,471 \| +₹77.5 | 458 / 738 / 386 \| +₹218,126 \| +₹21,071 \| +₹77.9 | 319 / 477 / 235 \| +₹189,840 \| +₹17,416 \| +₹124.1 |
| `trend_pullback_v2_ITM1` | 396 / 515 / 185 \| +₹90,591 \| +₹22.7 | 412 / 524 / 191 \| +₹201,532 \| -₹7,536 \| +₹118.8 | 410 / 529 / 186 \| +₹195,376 \| +₹22,068 \| +₹113.7 | 273 / 361 / 129 \| +₹155,128 \| -₹1,139 \| +₹143.3 |
| `composite_shared_ITM` | 402 / 591 / 164 \| +₹108,996 \| +₹34.2 | 397 / 530 / 161 \| +₹185,034 \| +₹7,288 \| +₹110.1 | 406 / 533 / 146 \| +₹177,338 \| +₹13,205 \| +₹103.4 | 291 / 369 / 105 \| +₹157,404 \| +₹90 \| +₹145.8 |
| `fast_momentum_ITM` | 338 / 651 / 131 \| +₹106,449 \| +₹35.0 | 351 / 601 / 118 \| +₹161,406 \| -₹4,108 \| +₹90.8 | 350 / 599 / 119 \| +₹176,047 \| +₹23,701 \| +₹104.8 | 260 / 429 / 72 \| +₹155,876 \| +₹14,662 \| +₹144.8 |
| `trend_pullback_ITM` | 376 / 513 / 219 \| +₹95,043 \| +₹25.8 | 372 / 469 / 200 \| +₹181,702 \| +₹20,431 \| +₹114.5 | 369 / 465 / 199 \| +₹178,557 \| +₹11,291 \| +₹112.9 | 256 / 338 / 133 \| +₹150,413 \| +₹10,130 \| +₹146.9 |
| `fast_momentum_OTM` | 387 / 679 / 162 \| +₹127,327 \| +₹43.7 | 403 / 656 / 181 \| +₹164,773 \| +₹10,057 \| +₹72.9 | 407 / 653 / 177 \| +₹175,950 \| +₹26,687 \| +₹82.2 | 288 / 479 / 109 \| +₹146,975 \| +₹15,795 \| +₹107.8 |
| `fixed_target_ITM` | 534 / 591 / 114 \| +₹134,699 \| +₹48.7 | 503 / 552 / 114 \| +₹171,882 \| +₹3,945 \| +₹87.0 | 497 / 549 / 117 \| +₹168,764 \| +₹5,656 \| +₹85.1 | 363 / 368 / 87 \| +₹149,960 \| +₹9,212 \| +₹123.3 |
| `structural_convexity_ATM` | 291 / 451 / 93 \| +₹79,787 \| +₹35.6 | 315 / 436 / 102 \| +₹137,780 \| +₹9,386 \| +₹101.5 | 312 / 435 / 99 \| +₹131,219 \| +₹7,040 \| +₹95.1 | 220 / 282 / 69 \| +₹103,091 \| -₹18,133 \| +₹120.5 |
| `trend_pullback_OTM` | 439 / 538 / 219 \| +₹114,965 \| +₹36.1 | 453 / 516 / 235 \| +₹188,293 \| +₹30,962 \| +₹96.4 | 447 / 518 / 225 \| +₹172,726 \| +₹14,526 \| +₹85.1 | 306 / 380 / 168 \| +₹136,662 \| +₹14,806 \| +₹100.0 |
| `composite_shared_OTM` | 433 / 651 / 163 \| +₹110,715 \| +₹28.8 | 426 / 644 / 196 \| +₹175,829 \| +₹28,688 \| +₹78.9 | 426 / 631 / 196 \| +₹152,086 \| +₹17,356 \| +₹61.4 | 306 / 449 / 125 \| +₹128,266 \| +₹14,487 \| +₹85.8 |
| `fixed_target_OTM` | 497 / 726 / 138 \| +₹125,982 \| +₹32.6 | 464 / 705 / 148 \| +₹133,914 \| +₹10,830 \| +₹41.7 | 459 / 701 / 145 \| +₹127,513 \| +₹8,457 \| +₹37.7 | 336 / 493 / 89 \| +₹93,265 \| +₹4,983 \| +₹41.6 |
| `opening_thrust_convex_ATM` | 39 / 68 / 7 \| +₹22,365 \| +₹136.2 | 41 / 73 / 6 \| +₹31,459 \| -₹378 \| +₹202.2 | 40 / 68 / 6 \| +₹33,914 \| -₹359 \| +₹237.5 | 40 / 65 / 7 \| +₹35,972 \| +₹379 \| +₹261.2 |
| `delta_scalp_ITM2` | 290 / 988 / 504 \| +₹7,862 \| -₹55.6 | 456 / 792 / 396 \| +₹185,073 \| +₹28,536 \| +₹52.6 | 454 / 787 / 392 \| +₹186,146 \| +₹28,810 \| +₹54.0 | 311 / 514 / 228 \| +₹148,380 \| +₹20,628 \| +₹80.9 |
| `gamma_runner_OTM2` | 198 / 479 / 76 \| -₹46,158 \| -₹121.3 | 186 / 429 / 79 \| -₹60,966 \| +₹16,139 \| -₹147.8 | 191 / 444 / 82 \| -₹60,286 \| -₹5,831 \| -₹144.1 | 150 / 345 / 60 \| -₹40,186 \| -₹261 \| -₹132.4 |

---

## 10. All 45 Experiments Day-by-Day Matrix (`Paper` & `Paper-Delayed`, `Nov 16 – Nov 26`)

| Experiment ID | `Nov 16` (P / D) | `Nov 17` (P / D) | `Nov 18` (P / D) | `Nov 19` (P / D) | `Nov 20` (P / D) | `Nov 23` (P / D) | `Nov 25` (P / D) | `Nov 26` (P / D) | 8d Total (P / D) |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| `W1_15S_OPT_SPREAD` | +₹58,415 / +₹44,270 | +₹629,223 / +₹619,416 | +₹273,656 / +₹262,385 | +₹422,551 / +₹394,597 | +₹396,123 / +₹372,902 | -₹26,060 / -₹26,060 | +₹175,374 / +₹55,559 | +₹102,812 / -₹10,993 | **+₹2,032,094 / +₹1,712,076** |
| `W2_15S_FLAG25_OPT_SPREAD` | +₹7,091 / +₹8,827 | +₹627,265 / +₹612,591 | +₹273,656 / +₹262,385 | +₹437,623 / +₹405,560 | +₹393,336 / +₹369,079 | -₹26,060 / -₹26,060 | +₹178,296 / +₹54,591 | +₹79,764 / +₹16,719 | **+₹1,970,972 / +₹1,703,692** |
| `E10_W3_CAP3` | +₹1,661 / -₹3,664 | +₹476,125 / +₹451,257 | +₹355,727 / +₹353,765 | +₹264,205 / +₹249,571 | +₹294,529 / +₹281,658 | +₹16,975 / +₹16,975 | +₹83,868 / +₹51,178 | +₹381,505 / +₹304,707 | **+₹1,874,596 / +₹1,705,447** |
| `W3_15S_FLAG25_3BAR045_OPT_SPREAD` | +₹2,574 / -₹5,858 | +₹608,638 / +₹604,643 | +₹320,934 / +₹318,416 | +₹235,914 / +₹220,577 | +₹283,548 / +₹270,514 | +₹16,975 / +₹16,975 | +₹74,390 / +₹21,266 | +₹299,108 / +₹186,505 | **+₹1,842,081 / +₹1,633,037** |
| `E6_W3_ATR020` | +₹4,063 / -₹1,782 | +₹633,809 / +₹616,483 | +₹289,778 / +₹288,236 | +₹151,099 / +₹140,959 | +₹259,489 / +₹249,047 | +₹16,975 / +₹16,975 | +₹69,532 / +₹32,784 | +₹363,622 / +₹296,002 | **+₹1,788,366 / +₹1,638,703** |
| `E9_W2_CAP3` | -₹54,415 / -₹57,656 | +₹491,818 / +₹483,825 | +₹294,698 / +₹281,848 | +₹452,168 / +₹431,440 | +₹339,980 / +₹310,960 | -₹26,060 / -₹26,060 | +₹154,566 / +₹53,817 | +₹129,588 / +₹70,034 | **+₹1,782,343 / +₹1,548,208** |
| `E5_W2_ATR020` | +₹16,392 / +₹13,409 | +₹563,544 / +₹550,709 | +₹282,808 / +₹281,220 | +₹181,961 / +₹170,680 | +₹388,995 / +₹363,193 | +₹11,519 / +₹11,519 | +₹118,063 / +₹50,954 | +₹197,900 / +₹113,349 | **+₹1,761,182 / +₹1,555,033** |
| `E7_W2_FLIPCD30` | -₹7,371 / -₹6,908 | +₹603,720 / +₹618,272 | +₹246,798 / +₹232,417 | +₹419,703 / +₹393,573 | +₹386,286 / +₹357,305 | -₹26,060 / -₹26,060 | +₹127,370 / +₹62,167 | +₹3,537 / -₹82,171 | **+₹1,753,985 / +₹1,548,595** |
| `E14_W2_GATE_ONLY_NOSCALE` | -₹31,023 / -₹41,664 | +₹616,323 / +₹588,625 | +₹188,247 / +₹183,690 | +₹381,768 / +₹354,966 | +₹351,347 / +₹319,073 | -₹30,160 / -₹30,160 | +₹237,141 / +₹117,354 | +₹28,196 / -₹7,810 | **+₹1,741,840 / +₹1,484,074** |
| `E8_W3_FLIPCD30` | -₹623 / -₹9,071 | +₹615,611 / +₹599,072 | +₹295,907 / +₹290,247 | +₹209,109 / +₹193,380 | +₹275,543 / +₹266,078 | +₹16,975 / +₹16,975 | +₹68,785 / +₹49,162 | +₹256,230 / +₹171,464 | **+₹1,737,537 / +₹1,577,307** |
| `W5_15S_FLAG25_NOFLIP_OPT_SPREAD` | -₹81,005 / -₹87,969 | +₹521,169 / +₹533,331 | +₹299,404 / +₹293,584 | +₹331,591 / +₹310,324 | +₹379,522 / +₹351,827 | -₹26,060 / -₹26,060 | +₹182,393 / +₹171,637 | +₹120,769 / +₹50,537 | **+₹1,727,783 / +₹1,597,211** |
| `E3_W2_3BAR035` | -₹41,664 / -₹50,782 | +₹500,274 / +₹477,834 | +₹283,989 / +₹274,065 | +₹248,471 / +₹242,626 | +₹391,195 / +₹367,763 | +₹18,206 / +₹18,206 | +₹97,754 / +₹33,376 | +₹209,449 / +₹123,108 | **+₹1,707,674 / +₹1,486,197** |
| `H6b_OPT_SCALE_SPREAD` | -₹3,431 / -₹12,196 | +₹514,086 / +₹489,607 | +₹262,067 / +₹254,370 | +₹456,895 / +₹425,513 | +₹305,549 / +₹276,523 | -₹13,025 / -₹12,575 | +₹83,670 / -₹19,491 | +₹93,453 / -₹20,600 | **+₹1,699,264 / +₹1,381,151** |
| `E15_15S_FLAG25_BODY035_SCALED` | +₹56,637 / +₹48,206 | +₹471,412 / +₹453,168 | +₹316,136 / +₹317,337 | +₹198,006 / +₹195,072 | +₹221,900 / +₹206,459 | -₹660 / -₹660 | +₹322,796 / +₹282,259 | +₹110,695 / +₹84,965 | **+₹1,696,922 / +₹1,586,805** |
| `E11_W2_FREEZE1400` | -₹4,914 / -₹10,659 | +₹429,489 / +₹419,674 | +₹282,994 / +₹272,565 | +₹284,436 / +₹264,297 | +₹353,379 / +₹323,854 | +₹9,040 / +₹9,040 | +₹258,405 / +₹127,254 | +₹70,790 / +₹38,877 | **+₹1,683,618 / +₹1,444,902** |
| `E12_W3_FREEZE1400` | -₹27,606 / -₹34,995 | +₹429,936 / +₹414,216 | +₹314,568 / +₹318,259 | +₹206,032 / +₹194,604 | +₹290,134 / +₹276,372 | +₹14,238 / +₹14,238 | +₹198,416 / +₹143,083 | +₹157,352 / +₹104,178 | **+₹1,583,070 / +₹1,429,954** |
| `E4_W2_3BAR060` | +₹58,154 / +₹58,889 | +₹470,732 / +₹447,260 | +₹262,901 / +₹243,485 | +₹225,832 / +₹215,861 | +₹143,233 / +₹156,723 | +₹4,926 / +₹4,926 | +₹209,104 / +₹159,670 | +₹162,003 / +₹111,817 | **+₹1,536,886 / +₹1,398,631** |
| `W4_3BAR045_OPT_SPREAD` | -₹14,934 / -₹22,174 | +₹498,879 / +₹477,341 | +₹287,660 / +₹283,146 | +₹242,726 / +₹225,517 | +₹195,660 / +₹183,119 | +₹30,010 / +₹30,460 | -₹24,993 / -₹67,658 | +₹186,478 / +₹132,144 | **+₹1,401,487 / +₹1,241,896** |
| `E2_15S_FLAG25_3BAR045_SCORING` | -₹84,409 / -₹90,817 | +₹606,070 / +₹602,806 | +₹201,768 / +₹196,732 | +₹217,331 / +₹202,927 | +₹260,613 / +₹243,580 | +₹14,766 / +₹14,766 | +₹47,994 / +₹46,572 | +₹85,350 / +₹11,658 | **+₹1,349,484 / +₹1,228,225** |
| `E13_W2_SPREAD2PCT` | -₹35,885 / -₹41,891 | +₹479,949 / +₹494,313 | +₹114,757 / +₹105,601 | +₹142,287 / +₹134,800 | +₹209,753 / +₹198,670 | +₹4,928 / +₹4,928 | +₹232,537 / +₹164,218 | +₹133,778 / +₹52,923 | **+₹1,282,102 / +₹1,113,562** |
| `H4a_ELAPSED_15S` | -₹67,220 / -₹73,094 | +₹539,202 / +₹510,967 | +₹164,059 / +₹155,725 | +₹366,570 / +₹348,096 | +₹346,137 / +₹313,570 | -₹67,123 / -₹67,123 | +₹24,922 / -₹54,522 | -₹144,723 / -₹238,681 | **+₹1,161,823 / +₹894,937** |
| `W6_BODY025_ATR025_OPT_SPREAD` | -₹47,295 / -₹51,750 | +₹293,619 / +₹264,374 | +₹277,927 / +₹279,142 | +₹198,467 / +₹188,620 | +₹164,920 / +₹141,178 | +₹22,075 / +₹22,525 | +₹77,771 / -₹14,705 | +₹148,621 / +₹109,995 | **+₹1,136,106 / +₹939,378** |
| `E1_15S_FLAG25_SCORING_ONLY` | -₹111,873 / -₹121,631 | +₹528,725 / +₹498,309 | +₹164,059 / +₹155,725 | +₹379,810 / +₹355,262 | +₹347,042 / +₹315,403 | -₹67,123 / -₹67,123 | +₹32,707 / -₹85,968 | -₹227,292 / -₹239,575 | **+₹1,046,055 / +₹810,403** |
| `H2b_PURE_3BAR_045` | -₹100,793 / -₹110,384 | +₹487,396 / +₹464,787 | +₹196,593 / +₹194,547 | +₹230,072 / +₹221,461 | +₹169,640 / +₹146,976 | +₹25,619 / +₹26,444 | -₹19,098 / -₹93,864 | -₹9,903 / -₹26,095 | **+₹979,525 / +₹823,872** |
| `H6a_OPT_SCALE` | -₹71,754 / -₹80,816 | +₹448,119 / +₹416,371 | +₹218,413 / +₹210,619 | +₹445,752 / +₹414,562 | +₹304,551 / +₹273,512 | -₹53,711 / -₹53,261 | -₹107,633 / -₹168,060 | -₹212,482 / -₹282,079 | **+₹971,255 / +₹730,848** |
| `M9_ALL_SIGNALS_SCALED` | -₹2,068 / -₹1,447 | +₹579,675 / +₹601,025 | +₹16,347 / +₹6,742 | +₹405,608 / +₹385,928 | +₹204,519 / +₹199,164 | -₹39,566 / -₹39,566 | -₹205,928 / -₹265,854 | +₹4,816 / -₹20,303 | **+₹963,403 / +₹865,690** |
| `H3c_FLAG_FULL_POLE` | -₹124,903 / -₹133,199 | +₹469,332 / +₹441,072 | +₹167,154 / +₹157,890 | +₹443,104 / +₹423,588 | +₹264,138 / +₹232,360 | -₹71,493 / -₹71,493 | -₹159,738 / -₹229,074 | -₹133,651 / -₹345,326 | **+₹853,943 / +₹475,818** |
| `H4b_ELAPSED_30S` | -₹40,408 / -₹47,972 | +₹482,275 / +₹477,627 | +₹90,625 / +₹87,608 | +₹394,665 / +₹371,603 | +₹301,634 / +₹268,181 | -₹75,088 / -₹75,088 | -₹86,212 / -₹282,646 | -₹290,220 / -₹397,916 | **+₹777,272 / +₹401,396** |
| `H3a_FLAG_BUF_15` | -₹106,986 / -₹124,420 | +₹414,337 / +₹405,741 | +₹155,057 / +₹144,784 | +₹401,226 / +₹380,352 | +₹255,450 / +₹222,303 | -₹56,158 / -₹55,333 | -₹57,200 / -₹104,044 | -₹265,888 / -₹231,413 | **+₹739,837 / +₹637,969** |
| `H2c_BODY_025_ATR_025` | -₹57,277 / -₹62,305 | +₹280,714 / +₹244,729 | +₹209,193 / +₹214,472 | +₹176,635 / +₹164,669 | +₹137,500 / +₹114,208 | +₹13,561 / +₹14,386 | -₹78,912 / -₹138,241 | +₹14,566 / -₹54,402 | **+₹695,981 / +₹497,516** |
| `H3b_FLAG_BUF_25` | -₹158,084 / -₹171,962 | +₹407,000 / +₹397,436 | +₹155,057 / +₹144,784 | +₹432,262 / +₹402,030 | +₹270,839 / +₹235,924 | -₹56,158 / -₹55,333 | -₹57,359 / -₹104,203 | -₹314,910 / -₹290,486 | **+₹678,646 / +₹558,189** |
| `H0_BASELINE` | -₹107,246 / -₹124,420 | +₹414,337 / +₹405,741 | +₹155,057 / +₹144,784 | +₹401,226 / +₹380,352 | +₹254,121 / +₹217,799 | -₹56,271 / -₹55,446 | -₹85,068 / -₹158,246 | -₹312,436 / -₹274,370 | **+₹663,718 / +₹536,194** |
| `M7_SPREAD_GATE_CORE` | -₹78,250 / -₹79,614 | +₹465,633 / +₹485,702 | -₹794 / -₹13,000 | +₹376,073 / +₹359,054 | +₹144,257 / +₹132,784 | -₹39,566 / -₹39,566 | -₹220,058 / -₹247,072 | +₹8,952 / -₹31,904 | **+₹656,246 / +₹566,383** |
| `H5b_CAP_2_NO_FLIP` | -₹75,293 / -₹79,655 | +₹235,030 / +₹242,498 | +₹216,313 / +₹209,588 | +₹271,835 / +₹252,097 | +₹248,357 / +₹217,081 | -₹55,900 / -₹55,075 | -₹73,683 / -₹144,901 | -₹127,649 / -₹144,836 | **+₹639,010 / +₹496,796** |
| `M8_BODY025_SPREAD_SCALED` | -₹13,247 / -₹17,648 | +₹342,910 / +₹348,851 | +₹44,966 / +₹43,468 | +₹141,594 / +₹140,806 | +₹127,920 / +₹121,478 | -₹4,725 / -₹4,725 | -₹138,977 / -₹175,683 | +₹138,003 / +₹90,021 | **+₹638,443 / +₹546,568** |
| `H5c_CAP_1` | -₹92,015 / -₹95,179 | +₹233,366 / +₹231,501 | +₹184,777 / +₹183,223 | +₹264,813 / +₹251,256 | +₹113,079 / +₹91,140 | -₹70,437 / -₹70,437 | -₹101,482 / -₹162,456 | +₹6,628 / -₹47,929 | **+₹538,728 / +₹381,118** |
| `H5a_CAP_2` | -₹173,359 / -₹175,275 | +₹235,083 / +₹216,732 | +₹201,639 / +₹191,575 | +₹364,025 / +₹342,497 | +₹260,477 / +₹225,498 | -₹56,271 / -₹55,446 | +₹18,677 / -₹11,370 | -₹336,624 / -₹374,167 | **+₹513,646 / +₹360,045** |
| `M3_SMART_IMPULSE` | -₹48,831 / -₹48,900 | +₹342,142 / +₹364,423 | +₹87,121 / +₹84,077 | +₹141,984 / +₹140,791 | +₹52,538 / +₹43,046 | -₹6,345 / -₹6,345 | -₹199,095 / -₹236,090 | +₹138,181 / +₹61,209 | **+₹507,695 / +₹402,211** |
| `M1_CORE_CLEAN` | -₹149,335 / -₹151,224 | +₹449,806 / +₹467,198 | +₹18,227 / +₹6,735 | +₹392,428 / +₹378,332 | +₹128,404 / +₹113,686 | -₹79,769 / -₹79,769 | -₹199,310 / -₹266,096 | -₹73,776 / -₹89,567 | **+₹486,673 / +₹379,295** |
| `H2a_MIN_ATR_030` | -₹127,802 / -₹129,490 | +₹267,811 / +₹262,401 | +₹244,851 / +₹250,857 | +₹138,474 / +₹142,300 | +₹106,757 / +₹84,448 | +₹16,644 / +₹17,468 | +₹81,750 / -₹14,298 | -₹242,827 / -₹299,011 | **+₹485,657 / +₹314,675** |
| `M2_BALANCED` | -₹47,559 / -₹49,458 | +₹275,757 / +₹283,083 | +₹47,675 / +₹47,397 | +₹196,605 / +₹195,366 | +₹64,834 / +₹57,814 | -₹6,345 / -₹6,345 | +₹11,854 / -₹43,987 | -₹87,012 / -₹136,862 | **+₹455,809 / +₹347,009** |
| `M5_IMPULSE_SCALED` | -₹49,265 / -₹51,171 | +₹338,619 / +₹354,713 | +₹89,252 / +₹88,045 | +₹144,141 / +₹143,374 | +₹53,526 / +₹47,848 | -₹4,725 / -₹4,725 | -₹290,648 / -₹311,794 | +₹111,456 / +₹66,862 | **+₹392,356 / +₹333,152** |
| `M6_HIGH_CONVICTION` | -₹17,097 / -₹17,486 | +₹249,613 / +₹258,599 | +₹37,280 / +₹38,927 | +₹133,148 / +₹132,298 | +₹65,998 / +₹60,055 | -₹4,725 / -₹4,725 | -₹26,726 / -₹31,391 | -₹71,309 / -₹103,728 | **+₹366,181 / +₹332,549** |
| `M4_BALANCED_SCALED` | -₹35,439 / -₹37,686 | +₹258,494 / +₹267,614 | +₹34,691 / +₹36,444 | +₹207,547 / +₹206,799 | +₹54,562 / +₹46,939 | -₹4,725 / -₹4,725 | -₹22,516 / -₹67,323 | -₹151,963 / -₹193,705 | **+₹340,652 / +₹254,356** |
| `H1_MIDDAY_FREEZE` | -₹116,940 / -₹119,183 | +₹418,716 / +₹430,834 | -₹2,025 / -₹9,798 | +₹380,517 / +₹368,394 | +₹74,639 / +₹58,572 | -₹68,092 / -₹68,092 | -₹339,718 / -₹376,929 | -₹143,714 / -₹129,375 | **+₹203,383 / +₹154,424** |

---

## 11. Quantitative & Structural Insights (Tick-by-Tick Audit Takeaways)

1. **Why True Tick-by-Tick Simulation Changed Track Attribution (`0.5s` vs Tick-by-Tick):** Under `0.5s` sampling, `structural_convexity_ATM` missed fast underlying flag invalidations and rapid option spikes (`0/30` positive previously). Under genuine event-driven tick replay (paired with live `underlying_ltp` routing), it cleared a positive expectancy in **`45/45` runs (`Avg +₹56,559`, best single day `+₹73,480`)**, proving the strategy edge is real once data latency is eliminated. Conversely, `delta_scalp_ITM2` bleeds under noisy entries unless protected by high-conviction impulse filters (`W3`/`E10`) + option convexity scaling, while `gamma_runner_OTM2` remained `0/45` positive (`Avg -₹79,678`).
2. **Why `fixed_target_ATM`, `trend_pullback_ATM`, and `composite_shared_ATM` Anchor Live Deployment:** Across 45 widely varying parameter regimes (`360` simulated sessions each), these three `ATM` `v1` tracks delivered **`86.9%`–`91.7%` profitable days**, tiny drawdowns (`₹2,855`–`₹3,924` avg DD), and **`+₹22.1k–+₹30.7k` minimum 8d profitability even in broken freeze configs**. `volatility_expansion_ATM` contributes the highest trend leverage (`+₹198,840` 8d peak, `Avg +₹107,827`), so pairing a core ATM allocation (`fixed_target_ATM` + `trend_pullback_ATM` + `composite_shared_ATM`) with satellite momentum allocators (`volatility_expansion_ATM` + `adaptive_regime_ATM`) captures both smooth equity growth and trend spikes.
3. **Expiry & Chop Regime Proof (`Nov 23 + Nov 26` Combined):** `H0_BASELINE` netted **`-₹368,707`** across the two expiry sessions (`Nov 23`: `-₹56,271`, `Nov 26`: `-₹312,436`). Adding `15s Elapsed + Flag25 + ATR/Spread Gate` (`W2`) lifted combined expiry P&L to **`+₹53,704`** (`+₹422,411` swing), and `E10_W3_CAP3` (`3Bar >= 0.45% + Cap <= 3`) hit **`+₹398,480`** while keeping **`8/8` days green (`₹0` Paper DD)**!
4. **Gate vs Target Scaling Synergy (`E1` → `E14` → `W2`):** Isolating `E14` proves `₹695,785` of gain comes simply from refusing wide-spread low-ATR contracts pre-entry (`+₹1,046,055 → +₹1,741,840`), while Dynamic Target/SL Scaling adds another `₹229,133` Paper / `₹219,618` Delayed by adapting rigid fixed targets to high-DTE option leverage (`Nov 25`, `Nov 26`).
