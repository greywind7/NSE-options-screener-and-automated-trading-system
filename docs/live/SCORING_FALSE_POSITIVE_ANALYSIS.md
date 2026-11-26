# Scoring Engine 3.0 — Empirical Alert Accuracy & False-Positive Analysis

**Date**: 2026-11-25  
**Scope**: Full tick-by-tick evaluation of **33,782,873 recorded equity ticks** across all **11 recorded sessions** (`2026-11-09` through `2026-11-25`) using exact [`CandleManager`](file:///Users/yorha/Documents/Trading%20Stuff/core/candles/candle_manager.py) + [`ScoringEngine3`](file:///Users/yorha/Documents/Trading%20Stuff/strategies/momentum/scoring_engine.py) logic with dated volume baselines (`data/index_YYYYMMDD.json`), cross-checked against live recorded `logs/alerts_*.jsonl` (`2026-11-20`, `11-23`, `11-25`) and ATM option depth (`option_depth.parquet`).

---

## Executive Summary

Across 11 trading days, `ScoringEngine3` generated **6,017 alerts** (**547 alerts/day**). Measured against the subsequent real-time underlying equity ticks and ATM option bids:

- **Strict Equity Continuation (`+0.5%` move before `-0.3%` pullback within 30 min)**: Only **17.7%** (`1,065 / 6,017`) succeed — meaning **82.3% fail strict continuation**.
- **Symmetric Short-Horizon Continuation (`+0.3%` before `-0.3%`)**: **34.4%** (**65.6% fail**).
- **15m / 30m Positive Drift (`ret > 0%`)**: **45.3%** at 15 min (`mean = -0.010%`, `median = -0.025%`) and **45.8%** at 30 min (`mean = -0.002%`). Unfiltered alerts have **slightly negative average follow-through**.
- **ATM Option Depth Outcomes (`n = 1,999` alerts with liquid spread $\le 3\%$)**: Only **26.3%** reach `+10%` gain before hitting `-5%` SL, and only **16.0%** reach `+15%` before hitting `-5%` SL (**17.8%** reach `+15%` before `-10%` SL).
- **Primary Signal Culprit**: **87.8%** of all alerts (`5,284 / 6,017`) come from `PURE_BULL_BREAKOUT` / `PURE_BEAR_BREAKDOWN` with **no minimum percentage thrust floor**, causing **67.0%** of `PURE` alerts to fire on tiny `< 0.3%` micro-candles inside low-ATR stocks (`72.9%` of all alerts occur when 5m `ATR < 0.35%`).

Layering five structural filters discovered in this data reduces alert spam by **91.6%** (**547/day → 46.1/day**) while lifting strict continuation precision by **+67% relative (`17.7% → 29.6%`)** and flipping 15-minute mean drift positive.

---

## 1. Evaluation Methodology

1. **Exact Tick-by-Tick Replay**: Each session's `recordings/<date>/equity_ticks.parquet` was sorted chronologically by `exchange_timestamp` and replayed tick-by-tick through `CandleManager.on_tick()`, evaluating `ScoringEngine3.evaluate_universe()` at 1-second simulated intervals prior to `15:15 IST`.
2. **Underlying Outcome Labels (30-Min Chronological Tick Path)**:
   - **`TP_05_03` (Strict Continuation)**: Reaches `+0.50%` favorable move *before* hitting `-0.30%` adverse move within 30 minutes.
   - **`TP_03_03` (Symmetric Continuation)**: Reaches `+0.30%` *before* hitting `-0.30%`.
   - **`TP_ATR`**: Reaches `+1.0 × ATR` *before* hitting `-0.75 × ATR`.
   - **`INV_15m` (Structural Invalidation)**: Breaks back below `flag_low` (Bull) or above `flag_high` (Bear) within 15 minutes.
3. **ATM Option Outcome Labels**: Nearest ATM `CE`/`PE` passing the 3% spread gate (`0 < bid < ask`, quote age $\le 30\text{s}$), tracked tick-by-tick on `bids` for 30 minutes (`+10% before -5%`, `+15% before -5%`, `+15% before -10%`).

---

## 2. Full Session-by-Session Results (11 Recorded Days + Live Logs)

### 2.1 Tick-Replayed Alerts Across All 11 Dates

| Date | Equity Ticks | Alerts | `TP_05_03` | `TP_03_03` | `TP_ATR` | `Pos @ 15m` | `Pos @ 30m` | `INV_15m` | Med `r15` | Med `MFE30` | Med `MAE30` | Liquid ATM (`n`) | Opt `+10/-5` | Opt `+15/-5` | Opt `+15/-10` |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| `2026-11-09` | 1,626,742 | 759 | 17.3% | 32.1% | 30.0% | 41.8% | 39.7% | 16.6% | -0.054% | +0.200% | -0.298% | 230 | 15.2% | 6.5% | 6.5% |
| `2026-11-11` | 2,253,554 | 631 | 22.8% | 38.8% | 37.4% | 50.1% | 49.6% | 11.6% | +0.004% | +0.256% | -0.234% | 116 | 29.3% | 14.7% | 14.7% |
| `2026-11-12` | 2,131,737 | 581 | 14.6% | 28.1% | 26.3% | 39.6% | 43.0% | 15.3% | -0.055% | +0.165% | -0.243% | 265 | 16.6% | 10.2% | 10.6% |
| `2026-11-13` | 2,039,171 | 508 | 14.0% | 31.3% | 30.1% | 42.1% | 41.7% | 17.3% | -0.027% | +0.197% | -0.241% | 61 | 27.9% | 16.4% | 16.4% |
| `2026-11-16` | 3,421,562 | 483 | 15.7% | 32.9% | 30.8% | 42.7% | 46.6% | 13.3% | -0.039% | +0.187% | -0.232% | 153 | 30.1% | 19.0% | 22.2% |
| `2026-11-17` | 3,528,774 | 559 | 15.2% | 31.5% | 33.5% | 44.0% | 46.3% | 11.3% | -0.032% | +0.199% | -0.219% | 251 | 37.1% | 21.9% | 24.3% |
| `2026-11-18` | 2,920,891 | 372 | 14.0% | 32.8% | 30.9% | 45.7% | 47.6% | 11.0% | -0.017% | +0.204% | -0.233% | 151 | 37.1% | 21.9% | 25.8% |
| `2026-11-19` | 3,594,170 | 582 | 16.8% | 33.3% | 33.0% | 48.3% | 45.5% | 12.7% | -0.009% | +0.199% | -0.223% | 191 | 42.9% | 35.6% | 41.9% |
| `2026-11-20` | 3,567,584 | 523 | **23.5%** | **42.8%** | **40.3%** | **53.7%** | 49.9% | 10.9% | **+0.022%** | +0.266% | -0.205% | 105 | **51.4%** | **44.8%** | **49.5%** |
| `2026-11-23` | 3,552,317 | 473 | 16.9% | 34.7% | 29.4% | 45.7% | 44.2% | **19.0%** | -0.019% | +0.213% | -0.251% | 5 | 40.0% | 40.0% | 40.0% |
| `2026-11-25` | 3,700,171 | 546 | 22.0% | 39.9% | 33.9% | 45.8% | **52.2%** | 13.2% | -0.024% | +0.266% | -0.271% | 471 | 13.4% | **3.6%** | **3.8%** |
| **TOTAL / ALL** | **33,782,873** | **6,017** | **17.7%** | **34.4%** | **32.4%** | **45.3%** | **45.8%** | **13.9%** | **-0.025%** | **+0.208%** | **-0.241%** | **1,999** | **26.3%** | **16.0%** | **17.8%** |

### 2.2 Cross-Check vs Live-Recorded `logs/alerts_*.jsonl`

| Live Session | Alerts | `TP_05_03` | `TP_03_03` | `Pos @ 15m` | `Pos @ 30m` | `INV_15m` | Liquid ATM (`n`) | Opt `+10/-5` | Opt `+15/-5` | Opt `+15/-10` |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| `LIVE 2026-11-20` | 829 | 22.1% | 38.1% | 48.5% | 48.6% | 15.3% | 193 | 46.6% | 38.3% | 44.6% |
| `LIVE 2026-11-23` | 530 | 15.7% | 31.7% | 43.6% | 42.5% | 17.7% | 5 | 40.0% | 40.0% | 40.0% |
| `LIVE 2026-11-25` | 644 | 18.8% | 34.3% | 41.9% | 48.1% | 12.3% | 565 | 11.9% | 3.2% | 3.4% |

*(Note: Historical `logs/alerts_2026-11-09.jsonl` had `4,391` rows and `2026-11-11.jsonl` had `1,650` rows due to Nov 19 replay runs / earlier config parameters; the clean tick-replay yields 759 and 631 respectively.)*

---

## 3. Root Causes & Patterns Among False Positives

### Pattern 1 — `PURE` Breakouts Bypass Minimum Magnitude (`87.8%` of Alert Volume, `70%` Micro-Candles)

| Archetype / Body Bucket | Count | Share / Day | `TP_05_03` | `TP_03_03` | `INV_15m` | Opt `+10/-5` | Opt `+15/-5` |
|---|---:|---:|---:|---:|---:|---:|---:|
| `FLAG_BULL_BREAKOUT` | 359 | 32.6 | 22.3% | 39.0% | 27.6% | 30.3% | 22.0% |
| `FLAG_BEAR_BREAKDOWN` | 374 | 34.0 | 21.7% | 40.4% | 24.6% | 18.8% | 10.3% |
| `PURE_BULL_BREAKOUT` | 2,747 | 249.7 | 18.6% | 34.9% | 12.2% | 29.1% | 18.5% |
| `PURE_BEAR_BREAKDOWN` | 2,537 | 230.6 | **15.5%** | **32.2%** | 12.3% | 23.2% | **12.7%** |
| `PURE` Body `[0.0%, 0.2%)` | 1,549 | 140.8 | **10.3%** | **27.6%** | **17.5%** | **21.1%** | **13.6%** |
| `PURE` Body `[0.2%, 0.4%)` | 2,981 | 271.0 | 18.6% | 35.3% | 11.1% | 27.2% | 15.6% |
| `PURE` Body `[0.4%, 0.6%)` | 587 | 53.4 | **24.2%** | **39.0%** | **7.0%** | **34.2%** | **21.4%** |
| `PURE` Body `>= 0.6%` | 167 | 15.2 | **29.3%** | **41.3%** | **2.4%** | **42.1%** | **26.3%** |

- **Why this happens in code**: In [`_check_flag_breakout()`](file:///Users/yorha/Documents/Trading%20Stuff/strategies/momentum/scoring_engine.py#L262), poles require `min_pole_pct >= 0.50%`. In [`_check_pure_breakout()`](file:///Users/yorha/Documents/Trading%20Stuff/strategies/momentum/scoring_engine.py#L451-L465), **no percentage thrust floor exists**—only `curr_thrust >= 2.0 * max(avg_body_3, 0.35 * ATR)`. On quiet stocks, `baseline_thrust` shrinks to `< 0.15%`, so **67.0% of all `PURE` alerts (`med_body = 0.247%`) fire on sub-`0.3%` noise ticks** (`PURE body < 0.2%` has only **10.3%** `TP_05_03`!).

---

### Pattern 2 — Low-Volatility Stock Overload (`ATR_pct < 0.35%` = `72.9%` of Alerts)

| 5m `ATR / Price` Bucket | Alerts | Alerts/Day | `TP_05_03` | `TP_03_03` | `INV_15m` | Opt `+10/-5` | Opt `+15/-5` | Opt `+15/-10` |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| `[0.00%, 0.20%)` | 1,410 | 128.2 | **10.6%** | **25.8%** | **16.0%** | 26.2% | 16.3% | 18.7% |
| `[0.20%, 0.35%)` | 2,977 | 270.6 | 17.5% | 34.6% | 13.1% | 24.2% | **14.0%** | **15.2%** |
| `[0.35%, 0.50%)` | 925 | 84.1 | 21.3% | 40.6% | 13.8% | 27.4% | 18.1% | 19.4% |
| `[0.50%, 1.00%)` | 555 | 50.5 | **27.4%** | **43.1%** | **10.6%** | **35.8%** | **23.5%** | **27.9%** |
| `>= 1.00%` | 150 | 13.6 | **30.7%** | 39.3% | 23.3% | 27.4% | 14.5% | 16.1% |

- **Why this kills P&L**: Scoring treats a `0.15%`-ATR stock equally with a `0.60%`-ATR mover. Yet option spreads are `1.3%–2.2%` + ₹60 brokerage regardless of underlying ATR—so low-ATR stocks lack the velocity to clear option friction before theta and bid noise stop them out.

---

### Pattern 3 — The OLS Sloping Flag Bug (`flag_below_pole`: `30.3%` of All Flag Alerts)

| Flag Condition | Alerts | `TP_05_03` | `INV_15m` | Opt `+10/-5` | Opt `+15/-5` | Opt `+15/-10` |
|---|---:|---:|---:|---:|---:|---:|
| **Trigger Inside Pole** (`trig < pole_high` Bull / `> pole_low` Bear) | 222 (30.3%) | 18.5% | **40.1%** | 21.8% | **11.5%** | **12.8%** |
| **Clean Break Beyond Pole Extreme** | 511 (69.7%) | **23.5%** | **20.0%** | **26.3%** | **18.7%** | **22.8%** |

- **Why this happens in code**: In [`_check_flag_breakout()`](file:///Users/yorha/Documents/Trading%20Stuff/strategies/momentum/scoring_engine.py#L359-L362), fitting `np.polyfit(xs, ys, 1)` on 2–4 contraction bars allows steep downward-sloping lines (`m < 0` on Bull flags) to project `proj_trigger` **below** `pole_high` (and similarly upward lines on Bear flags above `pole_low`), firing a "breakout" while price is still trapped mid-consolidation. Those invalid-structure flags suffer **2× higher invalidation (`40.1%` vs `20.0%`)** and nearly half the option `+15%/-5%` win rate (`11.5%` vs `18.7%`).

---

### Pattern 4 — Early-Bar Elapsed-Time RoV Artifacts (`sec_in_bar < 30s`)

| Trigger Timing Inside 5m Bar | Alerts | `TP_05_03` | `INV_15m` | Mean `r30` | Opt `+15/-5` |
|---|---:|---:|---:|---:|---:|
| Scheduled `0–15s` into bar | 363 | **15.4%** | **27.5%** | -0.028% | 17.8% |
| Scheduled `15–60s` into bar | 991 | 16.9% | 16.4% | -0.003% | 15.6% |
| Scheduled `180–300s` into bar | 1,870 | **19.1%** | **10.9%** | **+0.005%** | 17.6% |
| `< 10s` from candle first tick | 276 | 17.4% | **30.1%** | **-0.044%** | **14.9%** |
| `>= 60s` from candle first tick | 4,532 | 18.0% | **12.2%** | -0.001% | 15.9% |

- **Why this happens in code**: `curr_rov = c["volume"] / max(1.0, now_ts - c["timestamp"])`. In the first 10–30 seconds of a bar, dividing even small opening ticks by tiny `elapsed` yields inflated `curr_rov` bursts—producing **2.5× higher 15-minute structural invalidation (`27.5%–30.1%` vs `10.9%–12.2%`)**.

---

### Pattern 5 — Alert Churn, Repeated Entries (`seq >= 3`) & Direction Flip-Flops (`28.8%` of Alerts)

| Symbol Sequence / Flip Category | Alerts | `TP_05_03` | `INV_15m` | Mean `r15` | Opt `+10/-5` | Opt `+15/-5` | Opt `+15/-10` |
|---|---:|---:|---:|---:|---:|---:|---:|
| **1st alert on symbol** | 2,149 | **19.4%** | 12.9% | -0.015% | **32.3%** | **20.3%** | **22.3%** |
| 2nd alert on symbol | 1,702 | 18.4% | 12.5% | +0.005% | 25.5% | 14.8% | 16.9% |
| 3rd–4th alert on symbol | 1,766 | 15.5% | 15.5% | -0.012% | 21.6% | 13.5% | 15.3% |
| **5th+ alert on symbol** | 400 | **15.2%** | **18.5%** | **-0.033%** | **17.4%** | **8.3%** | **8.3%** |
| **Flipped opposite direction** | 1,730 (28.8%) | **14.9%** | 13.9% | -0.014% | **20.7%** | **12.7%** | **13.4%** |
| Same-direction repeat (`seq > 1`) | 2,138 | 18.3% | 15.0% | -0.002% | 24.7% | 14.2% | 16.8% |

- **Takeaway**: `5th+` alerts on the same stock cut ATM `+15%/-5%` win rate from **20.3% down to 8.3%**, and stocks flipping Bull ↔ Bear (`28.8%` of alerts!) are classic whipsaw chop traps (`TP_05_03 = 14.9%`, `Opt +10/-5 = 20.7%` vs `32.3%` on first signals).

---

### Pattern 6 — Time-of-Day Degradation (`79.7%` of Alerts Fire After `11:30 IST`)

| Time Window (IST) | Alerts | Share | `TP_05_03` | `TP_03_03` | Mean `r30` | Opt `+10/-5` | Opt `+15/-5` |
|---|---:|---:|---:|---:|---:|---:|---:|
| `09:15–09:29` | 0 | 0.0% | — | — | — | — | — |
| `09:30–10:29` | 448 | 7.4% | **27.2%** | **39.3%** | **+0.032%** | **36.1%** | **24.1%** |
| `10:30–11:29` | 776 | 12.9% | 19.5% | 38.5% | -0.020% | 26.6% | 18.0% |
| `11:30–13:29` (Midday) | 2,413 | **40.1%** | 17.6% | 35.1% | -0.001% | 26.5% | **14.3%** |
| `13:30–15:15` (Late) | 2,380 | **39.6%** | **15.5%** | **31.3%** | -0.003% | **23.8%** | 15.4% |

- **Notable interaction**: `09:30–10:29` outperforms every other window (`27.2%` `TP_05_03`, `36.1%` Opt `+10/-5`, `+0.032%` mean 30m return), yet accounts for only **7.4%** of alerts (and `09:15–09:29` is completely blocked by `if bar_idx < 3: return None`). Meanwhile, afternoon `PURE_BEAR_BREAKDOWN >= 13:30` (`n=1,088`) drops to **13.7%** `TP_05_03` / **10.2%** `Opt +15/-5`, and `FLAG_BULL_BREAKOUT >= 13:30` drops to **11.9%** `TP_05_03` / `-0.074%` mean 15m return.

---

### Pattern 7 — Why Today (`2026-11-25`) Hit Only `3.6%` on `+15% / -5% SL` Despite `22.0%` Equity Continuation

| Date | Liquid Opts | Med Ask (INR) | Med Ask / Spot | Med Spread | Med Opt Max Gain (30m) | Med Opt Max DD (30m) | Opt `+15/-5` |
|---|---:|---:|---:|---:|---:|---:|---:|
| `2026-11-19` | 191 | ₹10.05 | **1.09%** | 2.03% | +12.50% | -6.58% | **35.6%** |
| `2026-11-20` | 105 | ₹14.75 | **0.92%** | 1.89% | +17.32% | -12.92% | **44.8%** |
| `2026-11-25` | 471 | **₹36.90** | **3.62%** | 1.28% | **+3.26%** | **-3.28%** | **3.6%** |

- **Structural Discovery**: On `2026-11-19`/`11-20` (late expiry cycle), ATM option premiums were ~**0.9%–1.1% of spot**, delivering ~3–5× higher percentage convexity (`+12.5%` to `+17.3%` median 30m peak gain). After rollover, `2026-11-25` ATM contracts cost **3.62% of spot (`₹36.90` median)**—cutting percentage gain per 0.5% stock move by ~3.5× (`+3.26%` median peak gain vs `-1.28%` spread + `-3.28%` drawdown), making a fixed `+15%` option target unreachable in 30 minutes even when equity breakouts succeeded (`22.0%` `TP_05_03`).
- Meanwhile, **4,010 / 6,017 alerts (66.6%)** had **no liquid ATM contract** across the 11-day catalog (`NO_CONTRACTS`: 2,378; `SPREAD_GT_3PCT`: 808; `STALE_QUOTE`: 631; `ATM_TOO_FAR`: 122; `ZERO_OR_CROSSED_BOOK`: 25; `NO_PRE_QUOTE`: 46). `ScoringEngine3` currently fires alerts blind to option spread/DTE convexity.

---

## 4. Candidate Filter Stack Ablation Results

Testing progressive false-positive filters on the exact 6,017-alert dataset demonstrates the tradeoff between alert suppression and signal precision:

| Filter Stack | Alerts (11d) | Alerts / Day | `TP_05_03` | `TP_03_03` | `Pos @ 15m` | `INV_15m` | Mean `r15` | Opt `+10/-5` | Opt `+15/-5` | Opt `+15/-10` |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| **0. Current Baseline** | 6,017 | 547.0 | 17.7% | 34.4% | 45.3% | 13.9% | -0.010% | 26.3% | 16.0% | 17.8% |
| **1. Fix OLS `flag_below_pole` + `sched >= 30s`** | 5,201 | 472.8 | 18.1% | 34.4% | 45.5% | 12.1% | -0.009% | 27.1% | 16.3% | 18.0% |
| **2. + `PURE` thrust $\ge 0.40\%$ (or Clean Flag)** | 1,022 | 92.9 | **25.6%** | 39.8% | 44.6% | **8.9%** | -0.016% | **35.1%** | **23.2%** | **25.6%** |
| **3. + Block direction flips & `seq > 2`** | 722 | 65.6 | 26.5% | 41.4% | 45.6% | 8.0% | -0.007% | **37.1%** | 22.8% | 25.4% |
| **4. + Midday freeze (`11:30–13:30`) & `ATR >= 0.30%`** | 507 | **46.1** | **29.6%** | **44.0%** | **48.1%** | **8.1%** | **+0.008%** | 35.2% | 21.8% | 24.2% |
| **5. Clean Flags Only (`ATR >= 0.35%`, `sched >= 30s`)** | 194 | 17.6 | 28.9% | 42.8% | **48.5%** | 18.0% | -0.009% | **37.7%** | **26.2%** | **27.9%** |

---

## 5. Concrete Actionable Recommendations for `ScoringEngine3` & `RMSService`

1. **Enforce `min_pole_pct` (`0.40%–0.50%`) Floor on `PURE` Breakouts**: Eliminates ~80% of micro-candle alert noise (`PURE body < 0.3%`) and raises equity continuation from `10–18%` to `24–29%`.
2. **Require `5m ATR / Price >= 0.30%–0.35%`**: Drops sleepy low-volatility stocks whose price motion cannot cover option spreads and brokerage.
3. **Fix Flag Breakout OLS Projection**: Require `close >= max(proj_trigger, pole_high)` on `FLAG_BULL_BREAKOUT` and `close <= min(proj_trigger, pole_low)` on `FLAG_BEAR_BREAKDOWN` (plus clamp OLS slope), cutting Flag invalidation in half (`40.1% → 20.0%`).
4. **Add Minimum Bar-Elapsed Guard (`elapsed >= 30s`)**: Prevents opening-tick RoV division spikes from firing premature reversals.
5. **Limit Per-Symbol Churn & Direction Flip-Flops**: Cap alerts per symbol (`max <= 2` active/session or enforce cooldown across flips after an adverse chop signal).
6. **Wire Midday Freeze (`11:30–13:30 IST`) + Early-DTE Convexity Scaling**: Block midday momentum noise by default, and scale option SL/target percentages dynamically by ATM premium-to-spot / DTE instead of using fixed `-5% / +15%` across both 1-DTE (`1.0%` premium) and 30-DTE (`3.6%` premium) sessions.
