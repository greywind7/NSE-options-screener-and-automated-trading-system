# Simulation Hypothesis Experimentation Report (`Nov 16 – Nov 25, 2026`)

> **Scope & Clean-Data Baseline:** 7 recorded sessions (`2026-11-16`, `2026-11-17`, `2026-11-18`, `2026-11-19`, `2026-11-20`, `2026-11-23` expiry, `2026-11-25`) across **all 20 tracks** (`12 v1` + `8 v2`) in both **`paper`** (instant fill at ask) and **`paper_delayed`** (randomized latency fill), executed with **zero edits to existing repository source or config files** via day-local scratch harnesses matching stock `ColumnarSimEngine` bit-for-bit (`2026-11-16` baseline parity check: `2,644` trades, `-₹74,753` net).

---

## 1. Executive Summary

We tested **30 configurations** total:
- **`H0_BASELINE`** — current production scoring (`ScoringEngine3`) and track configs (`v1` + `v2`).
- **13 Single-Hypothesis Variants (`H1`–`H6b`)** — isolating one parameter/rule change at a time.
- **9 Original Mixed Variants (`M1`–`M9`)** — multi-rule mixes that all kept **Midday Freeze (`11:30–13:30`)**.
- **6 Non-Midday Winner Combinations (`W1`–`W6`)** — mixing the best individual winners **without** Midday Freeze or hard per-symbol caps after diagnosing why `M1`–`M9` underperformed.

### Top-Line Takeaways

1. **Champion Overall Profit — `W2_15S_FLAG25_OPT_SPREAD`** (`Min Elapsed >= 15s` + `OLS Flag Top 25% of Pole` + `Option Convexity Target/SL Scaling` + `ATR/Spread Gate`):
   - **7-Day Paper Net P&L (All 20 tracks):** **`+₹1,511,447`** (**`+55.6%`** / `+₹539,949` over Baseline `+₹971,498`), Max Daily Drawdown **`₹67,771`** (**`-38%`** vs Baseline `₹109,330`), **`5/7`** green days (turns `2026-11-25` from `-₹51,796` into **`+₹120,218`**!).
   - **7-Day Paper-Delayed Net P&L:** **`+₹1,421,488`** (**`+71.6%`** / `+₹593,283` over Baseline `+₹828,205`!). Slippage across all 7 days drops from `₹143,293` (Baseline) to **`₹89,959`** because waiting `15s` + requiring the upper 25% of the pole + gating wide spreads avoids fleeting micro-spikes that mean-revert within 100–250ms.
   - **If pruning the 3 structurally losing `v2` tracks** (17 tracks: `12 v1` + `5 v2`): **`+₹1,700,622` Paper / `+₹1,630,824` Paper-Delayed**.

2. **Highest Efficiency, Lowest Drawdown & Expiry-Proof — `W3_15S_FLAG25_3BAR045_OPT_SPREAD`** (`W2` + `PURE Net 3-Bar Expansion >= 0.45%`):
   - **Opps & Commissions Cut by ~32–40%:** Opportunities drop `1,477 → 889` (`-39.8%`), trades drop `19,080 → 13,033` (`-31.7%`), saving **`₹362,820`** in brokerage (`₹781,980` vs `₹1,144,800`).
   - **7-Day Paper Net P&L:** **`+₹1,322,853`** (`+36.2%` vs Baseline), **`6/7` GREEN DAYS**, Max Daily DD only **`₹52,123`**.
   - **7-Day Paper-Delayed Net P&L:** **`+₹1,253,793`** (`+51.4%` vs Baseline), slippage only **`₹69,060`**, and **`v2` hits its #1 highest net P&L of all 30 tests (`+₹383,092`, `+240%` vs Baseline `+₹112,644`)**!
   - **Conquers Both Problem Regimes:** Makes **Expiry Day (`2026-11-23`) GREEN on both `v1` (`+₹10,895`) and `v2` (`+₹7,529`)** (`+₹18,424` total vs Baseline `-₹57,533`) AND keeps **choppy `2026-11-25` GREEN (`+₹28,429`)**!

3. **Biggest Surprise — Midday Freeze (`11:30–13:30`) Was the #1 Drag in `M1`–`M9`:**
   - Blocking `11:30–13:30` (`H1_MIDDAY_FREEZE`) collapsed 7-day Paper P&L by **`-63.9%`** (`+₹971,498 → +₹351,084`) and quadrupled Max Daily DD to **`₹426,779`**. On `Nov 18`, `Nov 20`, and especially `Nov 25`, morning chop stopped out early trades while profitable recovery/follow-through breakouts happened during `11:30–13:30`.
   - Direct A/B proof: `M9_ALL_SIGNALS_SCALED` (had Midday Freeze) earned `+₹715,632`; removing Midday Freeze in `W1_15S_OPT_SPREAD` jumped P&L to **`+₹1,483,864`** (`+₹768,232`, **more than double**)!

4. **`15s` Elapsed Floor Beats `30s` Resoundingly & Matches Your Hypothesis:**
   - `H4a_ELAPSED_15S` earned **`+₹1,167,005`** (`+20.1%` vs Baseline `+₹971,498`), whereas `H4b_ELAPSED_30S` **lost money vs Baseline** (`+₹824,119`, `-15.2%`) and hurt `v2` badly (`+₹112.6k → +₹9.7k` Paper, `-₹48.6k` Delayed). `15s` filters opening second-1 RVOL noise without missing real breakouts.

5. **Structural Finding on Tracks (`v1` 12/12 Profitable vs `v2` Split):**
   - **All 12 `v1` tracks are solidly profitable** under Baseline (`+₹858,854`, `6/7` green days) and climb to **`+₹1,212,473`** (`W2`) or `7/7` zero-DD green days (`+₹815,047` on `W4` / `+₹664,543` on `W6`).
   - **`v2` (`+₹112,644` Baseline)** is held back by **3 losing tracks** totaling `-₹200,780` in Baseline: `structural_convexity_ATM` (`-₹103,383`, 14.8% WR), `gamma_runner_OTM2` (`-₹56,767`, 25.9% WR), and `delta_scalp_ITM2` (`-₹40,630`, 19.3% WR). Meanwhile the other 5 `v2` tracks (`adaptive_regime_ATM`, `volatility_expansion_ATM`, `trend_pullback_v2_ITM1`, `micro_chandelier_ITM1`, `opening_thrust_convex_ATM`) earned **`+₹313,424`** in Baseline and **`+₹461,305`** (`+₹488,149` on `W2`!) with Option Scaling!

---

## 2. Full 30-Experiment Leaderboard (7 Days: `Nov 16–25, 2026`)

> **Legend:** `Opps` = accepted opportunities (`7d` sum); `Trades` = closed trades across all 20 tracks; `WR` = Paper Win Rate (`> +2.5%` move); `DD` = Max Daily Drawdown across the 7 daily P&Ls (`cumulative peak-to-trough`); `Green` = profitable days out of 7; `Pruned (17t)` = `v1 (12)` + the `5` profitable `v2` tracks (excluding `structural_convexity_ATM`, `gamma_runner_OTM2`, `delta_scalp_ITM2`).

| Rank | Experiment ID | Rule Description | Opps | Trades | WR | Paper Net (All 20) | Paper DD | Green | Paper v1 (12t) | Paper v2 (8t) | Delayed Net (All 20) | Delayed DD | Pruned 17t Paper | Pruned 17t Delayed |
|---:|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| **1** | **`W2_15S_FLAG25_OPT_SPREAD`** | 15s + Flag25 + OptScale + SpreadGate | 1,349 | 18,864 | 34.7% | **+₹1,511,447** | ₹67,771 | 5/7 | **+₹1,212,473** | +₹298,975 | **+₹1,421,488** | ₹67,771 | **+₹1,700,622** | **+₹1,630,824** |
| **2** | **`W1_15S_OPT_SPREAD`** | 15s + OptScale + SpreadGate | 1,355 | 18,964 | 34.7% | **+₹1,483,864** | ₹67,771 | 5/7 | +₹1,195,900 | +₹287,965 | +₹1,376,967 | ₹67,771 | +₹1,668,485 | +₹1,589,419 |
| **3** | **`H6b_OPT_SCALE_SPREAD`** | Option Convexity Scale + ATR/Spread Gate | 1,385 | 19,252 | 34.3% | **+₹1,329,644** | ₹54,938 | **6/7** | +₹1,062,412 | +₹267,233 | +₹1,181,570 | ₹82,294 | +₹1,523,716 | +₹1,393,972 |
| **4** | **`W3_15S_FLAG25_3BAR045_OPT_SPREAD`** | 15s + Flag25 + 3Bar0.45 + OptScale + SpreadGate | **889** | **13,033** | **35.2%** | **+₹1,322,853** | **₹52,123** | **6/7** | +₹939,761 | **+₹383,092** | **+₹1,253,793** | **₹57,033** | +₹1,396,041 | +₹1,354,474 |
| **5** | `W5_15S_FLAG25_NOFLIP_OPT_SPREAD` | W2 + Block Opposite-Dir Flips (No Cap) | 1,021 | 15,013 | 34.6% | +₹1,261,876 | ₹118,544 | 5/7 | +₹990,236 | +₹271,640 | +₹1,193,512 | ₹124,438 | +₹1,417,485 | +₹1,369,186 |
| **6** | **`H4a_ELAPSED_15S`** | Min Candle Elapsed >= 15s | 1,446 | 18,763 | 33.1% | +₹1,167,005 | ₹96,581 | 4/7 | +₹989,836 | +₹177,169 | +₹1,014,754 | ₹188,987 | +₹1,355,183 | +₹1,211,455 |
| **7** | `H6a_OPT_SCALE` | Option Convexity Target/SL Scaling Only | 1,477 | 19,644 | 33.9% | +₹1,118,620 | ₹149,715 | 4/7 | +₹966,375 | +₹152,245 | +₹972,379 | ₹248,678 | +₹1,360,568 | +₹1,232,903 |
| **8** | **`W4_3BAR045_OPT_SPREAD`** | PURE 3Bar0.45 + OptScale + SpreadGate | 923 | 13,425 | 34.4% | +₹1,101,494 | **₹37,962** | **6/7** | +₹815,047 (**7/7, ₹0 DD**) | +₹286,447 | +₹988,718 (**6/7**) | ₹98,915 | +₹1,204,888 | +₹1,106,713 |
| **9** | **`H2b_PURE_3BAR_045`** | PURE Net 3-Bar Expansion >= 0.45% | 970 | 13,251 | 33.2% | +₹1,028,615 | ₹52,830 | 5/7 | +₹777,656 | +₹250,959 | +₹906,560 | ₹73,530 | +₹1,109,872 | +₹1,003,119 |
| **10** | **`H3b_FLAG_BUF_25`** | Flag Trigger >= Pole Top 25% | 1,465 | 18,895 | 32.8% | +₹1,019,668 | ₹112,462 | 4/7 | +₹908,160 | +₹111,508 | +₹821,316 | ₹198,206 | +₹1,222,171 | +₹1,041,761 |
| **11** | `H3a_FLAG_BUF_15` | Flag OLS + 15% Buffer (Top 35% of Pole) | 1,469 | 18,995 | 32.8% | +₹996,127 | ₹103,076 | 4/7 | +₹909,809 | +₹86,318 | +₹804,243 | ₹201,979 | +₹1,197,033 | +₹1,029,093 |
| **12** | **`H0_BASELINE`** | **Current Baseline (Unfiltered)** | **1,477** | **19,080** | **32.8%** | **+₹971,498** | **₹109,330** | **4/7** | **+₹858,854** | **+₹112,644** | **+₹828,205** | **₹175,134** | **+₹1,172,278** | **+₹1,051,368** |
| **13** | `H3c_FLAG_FULL_POLE` | Flag Strict Full Pole High/Low (Ref) | 1,430 | 18,483 | 33.0% | +₹928,583 | ₹242,818 | 4/7 | +₹807,705 | +₹120,878 | +₹730,551 | ₹344,817 | +₹1,128,020 | +₹956,564 |
| **14** | `W6_BODY025_ATR025_OPT_SPREAD` | Body0.25 + ATR0.25 + OptScale + SpreadGate | 733 | 10,496 | 34.6% | +₹855,833 | **₹21,500** | **6/7** | +₹664,543 (**7/7, ₹0 DD**) | +₹191,291 | +₹761,490 | **₹23,553** | +₹965,934 | +₹883,850 |
| **15** | `H4b_ELAPSED_30S` | Min Candle Elapsed >= 30s (Ref) | 1,427 | 18,703 | 32.7% | +₹824,119 | ₹187,045 | 4/7 | +₹814,444 | +₹9,676 | +₹595,188 | ₹350,744 | +₹1,057,872 | +₹851,403 |
| **16** | `H5a_CAP_2` | Cap Alerts <= 2 per Symbol/Day | 1,078 | 14,903 | 33.0% | +₹813,029 | ₹115,794 | 5/7 | +₹775,496 | +₹37,532 | +₹671,716 | ₹125,536 | +₹985,866 | +₹862,998 |
| **17** | `H2a_MIN_ATR_030` | Min 5m ATR >= 0.30% | 604 | 8,270 | 33.3% | +₹787,955 | ₹79,034 | 6/7 | +₹668,585 | +₹119,370 | +₹658,647 | ₹82,847 | +₹846,730 | +₹723,255 |
| **18** | `M9_ALL_SIGNALS_SCALED` | Midday + 15s + Flag15 + OptScaleSpread (No Cap) | 749 | 10,880 | 33.5% | +₹715,632 | ₹300,841 | 4/7 | +₹572,913 | +₹142,718 | +₹679,229 | ₹367,175 | +₹854,500 | +₹821,263 |
| **19** | `H2c_BODY_025_ATR_025` | Min ATR >= 0.25% & PURE Body >= 0.25% | 776 | 10,379 | 33.0% | +₹669,040 | ₹99,222 | 5/7 | +₹582,447 (**7/7, ₹0 DD**) | +₹86,593 | +₹565,583 | ₹150,973 | +₹766,193 | +₹674,458 |
| **20** | `H5b_CAP_2_NO_FLIP` | Cap <= 2 & Block Opposite Flips | 944 | 13,407 | 32.6% | +₹646,018 | ₹117,620 | 4/7 | +₹577,466 | +₹68,552 | +₹537,389 | ₹164,914 | +₹785,488 | +₹691,827 |
| **21** | `M1_CORE_CLEAN` | Midday + 15s + Flag15 + Cap2 | 735 | 10,202 | 32.2% | +₹610,484 | ₹265,067 | 4/7 | +₹501,925 | +₹108,559 | +₹513,743 | ₹353,268 | +₹746,243 | +₹660,096 |
| **22** | `M2_BALANCED` | M1 + Min ATR >= 0.30% | 294 | 4,529 | 33.1% | +₹480,696 | ₹68,058 | 4/7 | +₹381,086 | +₹99,610 | +₹436,259 | ₹71,975 | +₹534,141 | +₹492,469 |
| **23** | `M7_SPREAD_GATE_CORE` | M1 + NoFlip + OptScaleSpread | 629 | 9,389 | 32.5% | +₹434,654 | ₹362,092 | 4/7 | +₹402,053 | +₹32,601 | +₹357,653 | ₹441,722 | +₹594,886 | +₹524,360 |
| **24** | `M4_BALANCED_SCALED` | M2 + Option Scaling | 294 | 4,664 | 34.0% | +₹424,290 | ₹80,551 | 3/7 | +₹330,474 | +₹93,816 | +₹390,975 | ₹125,467 | +₹480,719 | +₹452,994 |
| **25** | `M6_HIGH_CONVICTION` | Midday + 15s + Flag25 + 3Bar0.45 + ATR0.30 + Cap2NoFlip + Scaled | 269 | 4,342 | 33.6% | +₹374,740 | ₹79,864 | 3/7 | +₹304,854 | +₹69,886 | +₹357,050 | ₹106,264 | +₹449,315 | +₹436,917 |
| **26** | `H1_MIDDAY_FREEZE` | Midday Freeze Only (`11:30–13:30`) | 830 | 11,105 | 31.4% | +₹351,084 | ₹426,779 | 3/7 | +₹322,823 | +₹28,261 | +₹324,593 | ₹453,524 | +₹511,019 | +₹488,184 |
| **27** | `M3_SMART_IMPULSE` | M1 + 3Bar0.45 + ATR0.25 | 348 | 5,264 | 31.9% | +₹329,718 | ₹233,101 | 3/7 | +₹254,938 | +₹74,780 | +₹313,284 | ₹258,681 | +₹413,328 | +₹403,613 |
| **28** | `H5c_CAP_1` | Cap Alerts <= 1 per Symbol/Day | 632 | 9,311 | 32.3% | +₹318,394 | ₹211,055 | 3/7 | +₹339,144 | -₹20,751 | +₹213,016 | ₹294,508 | +₹447,893 | +₹360,127 |
| **29** | `M8_BODY025_SPREAD_SCALED` | Midday + 15s + Flag15 + ATR0.25 + Body0.25 + Cap2NoFlip + Scaled | 321 | 5,087 | 33.4% | +₹274,435 | ₹233,661 | 3/7 | +₹208,225 | +₹66,210 | +₹213,254 | ₹292,778 | +₹359,718 | +₹304,447 |
| **30** | `M5_IMPULSE_SCALED` | M3 + Option Scaling | 348 | 5,439 | 32.3% | +₹221,441 | ₹346,371 | 3/7 | +₹153,067 | +₹68,374 | +₹220,166 | ₹356,945 | +₹315,545 | +₹316,457 |

---

## 3. Day-by-Day Breakdown (`Nov 16 – Nov 25`)

Each cell shows **Paper Net (`v1` / `v2`)** on top and **Delayed Net** underneath (`All 20` tracks):

| Experiment | `Nov 16` | `Nov 17` | `Nov 18` | `Nov 19` | `Nov 20` | `Nov 23` *(Expiry)* | `Nov 25` *(Choppy)* | 7-Day Total |
|---|---|---|---|---|---|---|---|---|
| **`H0_BASELINE`** *(P)*<br>*(Delayed)* | `-74,753` `(+3.9k/-78.7k)`<br>`-84,214` | `+540,062` `(+364.2k/+175.8k)`<br>`+533,256` | `+248,529` `(+196.8k/+51.7k)`<br>`+242,527` | `+230,611` `(+163.3k/+67.3k)`<br>`+204,592` | `+136,379` `(+108.0k/+28.4k)`<br>`+107,179` | `-57,533` `(-31.8k/-25.7k)`<br>`-56,708` | `-51,796` `(+54.4k/-106.2k)`<br>`-118,426` | **`+971,498`**<br>**`+828,205`** |
| **`W2_15S_FLAG25_OPT_SPREAD`** *(P)*<br>*(Delayed)* | `-54,535` `(+8.7k/-63.2k)`<br>`-60,747` | **`+647,132`** `(+436.1k/+211.1k)`<br>`+645,676` | `+344,497` `(+258.3k/+86.2k)`<br>`+338,487` | **`+304,092`** `(+228.5k/+75.6k)`<br>`+289,281` | **`+217,814`** `(+135.0k/+82.8k)`<br>`+206,663` | `-67,771` `(-35.9k/-31.9k)`<br>`-67,771` | **`+120,218`** `(+181.9k/-61.6k)`<br>**`+69,899`** | **`+1,511,447`**<br>**`+1,421,488`** |
| **`W1_15S_OPT_SPREAD`** *(P)*<br>*(Delayed)* | `-13,845` `(+38.4k/-52.3k)`<br>`-18,558` | `+631,957` `(+417.0k/+215.0k)`<br>`+626,436` | `+344,497` `(+258.3k/+86.2k)`<br>`+338,487` | `+266,571` `(+197.6k/+68.9k)`<br>`+253,475` | **`+218,481`** `(+135.9k/+82.6k)`<br>`+202,276` | `-67,771` `(-35.9k/-31.9k)`<br>`-67,771` | `+103,974` `(+184.6k/-80.6k)`<br>`+42,622` | **`+1,483,864`**<br>**`+1,376,967`** |
| **`H6b_OPT_SCALE_SPREAD`** *(P)*<br>*(Delayed)* | **`+12,048`** `(+55.7k/-43.7k)`<br>**`+5,829`** | `+565,428` `(+366.0k/+199.4k)`<br>`+551,184` | `+317,511` `(+239.2k/+78.3k)`<br>`+315,558` | `+271,006` `(+204.3k/+66.7k)`<br>`+260,030` | `+146,629` `(+103.9k/+42.7k)`<br>`+131,263` | `-54,938` `(-28.5k/-26.5k)`<br>`-54,488` | `+71,960` `(+121.7k/-49.8k)`<br>`-27,806` | **`+1,329,644`**<br>**`+1,181,570`** |
| **`W3_15S_FLAG25_3BAR045_OPT_SPREAD`** *(P)*<br>*(Delayed)* | `-52,123` `(-22.4k/-29.7k)`<br>`-57,033` | `+584,959` `(+359.5k/+225.4k)`<br>`+576,139` | **`+409,178`** `(+288.2k/+121.0k)`<br>**`+404,064`** | `+158,829` `(+124.9k/+33.9k)`<br>`+144,735` | `+175,156` `(+94.4k/+80.7k)`<br>`+174,686` | **`+18,424`** `(+10.9k/+7.5k)`<br>**`+18,424`** | **`+28,429`** `(+84.2k/-55.8k)`<br>`-7,220` | **`+1,322,853`**<br>**`+1,253,793`** |
| **`W4_3BAR045_OPT_SPREAD`** *(P)*<br>*(Delayed)* | **`+8,675`** `(+20.5k/-11.8k)`<br>**`+3,537`** | `+506,878` `(+307.4k/+199.5k)`<br>`+483,200` | `+371,150` `(+262.2k/+109.0k)`<br>`+368,087` | `+120,787` `(+96.0k/+24.8k)`<br>`+106,343` | `+100,710` `(+60.6k/+40.2k)`<br>`+94,760` | **`+31,256`** `(+18.3k/+13.0k)`<br>**`+31,706`** | `-37,962` `(+50.1k/-88.1k)`<br>`-98,915` | **`+1,101,494`**<br>**`+988,718`** |
| **`W5_15S_FLAG25_NOFLIP_OPT_SPREAD`** *(P)*<br>*(Delayed)* | `-118,544` `(-47.5k/-71.1k)`<br>`-124,438` | `+509,176` `(+319.3k/+189.9k)`<br>`+514,430` | `+344,624` `(+253.9k/+90.7k)`<br>`+345,972` | `+277,117` `(+183.3k/+93.8k)`<br>`+267,581` | `+175,197` `(+130.2k/+45.0k)`<br>`+160,853` | `-67,400` `(-35.9k/-31.5k)`<br>`-67,400` | **`+141,705`** `(+186.9k/-45.2k)`<br>**`+96,514`** | **`+1,261,876`**<br>**`+1,193,512`** |
| **`H4a_ELAPSED_15S`** *(P)*<br>*(Delayed)* | `-56,939` `(+24.0k/-80.9k)`<br>`-66,632` | `+615,378` `(+412.8k/+202.6k)`<br>`+620,282` | `+263,362` `(+205.6k/+57.8k)`<br>`+260,147` | `+238,455` `(+166.9k/+71.6k)`<br>`+221,177` | `+203,330` `(+140.5k/+62.8k)`<br>`+168,768` | `-68,183` `(-37.5k/-30.7k)`<br>`-68,183` | `-28,398` `(+77.6k/-106.0k)`<br>`-120,804` | **`+1,167,005`**<br>**`+1,014,754`** |
| **`H2b_PURE_3BAR_045`** *(P)*<br>*(Delayed)* | `-52,830` `(-14.0k/-38.8k)`<br>`-61,815` | `+543,687` `(+326.3k/+217.4k)`<br>`+527,712` | `+301,306` `(+222.8k/+78.5k)`<br>`+295,144` | `+123,606` `(+98.7k/+24.9k)`<br>`+109,455` | `+97,197` `(+82.6k/+14.6k)`<br>`+83,382` | **`+25,386`** `(+15.6k/+9.8k)`<br>**`+26,211`** | `-9,738` `(+45.7k/-55.4k)`<br>`-73,530` | **`+1,028,615`**<br>**`+906,560`** |
| **`H1_MIDDAY_FREEZE`** *(P)*<br>*(Delayed)* | `-62,582` `(-2.0k/-60.6k)`<br>`-63,669` | `+542,866` `(+334.5k/+208.4k)`<br>`+558,120` | `+96,669` `(+82.9k/+13.8k)`<br>`+94,258` | `+200,910` `(+150.2k/+50.7k)`<br>`+189,409` | `-2,754` `(+15.2k/-17.9k)`<br>`-11,670` | `-69,092` `(-36.6k/-32.5k)`<br>`-69,092` | **`-354,933`** `(-221.2k/-133.7k)`<br>`-372,762` | **`+351,084`**<br>**`+324,593`** |

---

## 4. Deep Dive on Each Individual Hypothesis

### 4.1 Option Convexity Scaling & ATR/Spread Gate (`H6a` & `H6b`) — **#1 Single Lever**
- **Mechanism tested:**
  - `H6a_OPT_SCALE`: Normalized premium (`prem_pct = entry_ask / spot`, reference `1.5%`) computes `scale = clamp(0.015 / prem_pct, 0.45, 1.25)`. Scales `target_pct * scale` and spreads-aware `sl_pct = max(clamp(spread_pct * 2.5, 3.5%, 12%), min(orig_sl * 1.25, orig_sl * max(0.75, scale)))`. Fixes cheap high-delta OTM options aiming too low and expensive high-strike/low-leverage ITM options aiming for unreachable `+15%–25%` option spikes on normal spot moves.
  - `H6b_OPT_SCALE_SPREAD`: Adds a pre-entry micro-structure gate (`exp_opt_move = (0.5 * ATR / spot) / prem_pct < 2.0 * spread_pct`), blocking options whose bid–ask spread consumes half an average 5m ATR swing.
- **Result & Verdict:**
  - `H6a` alone lifted Paper Net from `+₹971,498 → +₹1,118,620` (`+15.1%`), **but still bled on choppy `Nov 25` (`-₹94,777`)**.
  - Adding the spread/ATR gate (`H6b`) rejected just **`92`** low-conviction/wide-spread opps (`1,477 → 1,385`), turning `Nov 16` (`-₹74.8k → +₹12.0k`) and `Nov 25` (`-₹51.8k → +₹72.0k`) positive! Total Paper jumped to **`+₹1,329,644` (`+36.9%`)**, Delayed to **`+₹1,181,570` (`+42.7%`)**, and **MaxDD fell in half (`₹109,330 → ₹54,938`)** with `6/7` green days.
  - **Crucial edge case observed:** On **`Nov 25` Paper-Delayed**, `H6b` alone turned `-₹27,806` (vs `+₹71,960` Paper) because 400 wide/noisy equity signals still crossed the ATM gate; combining it with `15s + Flag25` (`W2`) pushed `Nov 25` **Delayed to `+₹69,899`** and **Paper to `+₹120,218`**!

### 4.2 Bar-Elapsed Gate (`H4a: 15s` vs `H4b: 30s`)
- **Hypothesis tested:** Prevent tick bursts in the first few seconds of a 5m bar from artificially inflating extrapolated RVOL before price structure stabilizes.
- **Result:**
  - **`H4a_ELAPSED_15S`** (**+₹1,167,005** Paper / **+₹1,014,754** Delayed): Improved **`+₹195,507` (`+20.1%`)** over Baseline while rejecting only 31 raw opportunities (`1,477 → 1,446`). Biggest winners were trending days (`Nov 17`: `+540k → +615k`; `Nov 20`: `+136k → +203k`).
  - **`H4b_ELAPSED_30S`** (**+₹824,119** Paper / **+₹595,188** Delayed): Dropped **`-₹147,379`** vs Baseline and crushed `v2` (`+112.6k → +9.7k` Paper, `-48.6k` Delayed). Waiting a full `30s` delayed entry past the crisp initial breakout impulse (`Nov 17`: `-76k` vs Baseline; `Nov 18`: `-58k`).
  - **Conclusion:** Your intuition to test `15s` instead of `30s` was spot-on — **`15s` is strictly optimal**.

### 4.3 `PURE` Breakout Quality vs Single-Candle Traps (`H2a`, `H2b`, `H2c`)
- **Hypothesis tested:** Baseline `PURE` breakouts (`max_candle_pct_change`, `min_volume_thrust_mult`) fire even inside sleepy, narrow ranges or after trivial single-bar ticks.
  - `H2a_MIN_ATR_030`: Global 5m `ATR / close >= 0.30%`.
  - `H2b_PURE_3BAR_045`: **Non-trapping impulse alternative** — requires current price to have expanded `>= 0.45%` from the **3-bar swing extreme** (`min(prev 3 lows)` for `BULL`, `max(prev 3 highs)` for `BEAR`), avoiding the `0.4–0.5%` single-bar exhaustion trap while filtering dead drift.
  - `H2c_BODY_025_ATR_025`: Global `ATR >= 0.25%` AND current bar `|close - open| / open >= 0.25%`.
- **Results & Gross vs Net Impact:**

| Variant | Opps | Trades | Gross P&L | Brokerage | Paper Net | MaxDD | Expiry `Nov 23` | `v1` Green Days |
|---|---:|---:|---:|---:|---:|---:|---:|---|
| `H0_BASELINE` | 1,477 | 19,080 | +₹2,116,298 | ₹1,144,800 (54.1%) | +₹971,498 | ₹109,330 | -₹57,533 | 6/7 |
| `H2a_MIN_ATR_030` | 604 | 8,270 | +₹1,284,155 | ₹496,200 (38.6%) | +₹787,955 | ₹79,034 | **+₹16,471** | 6/7 |
| **`H2b_PURE_3BAR_045`** | **970** | **13,251** | +₹1,823,675 | **₹795,060 (43.6%)** | **+₹1,028,615** | **₹52,830** | **+₹25,386** | 6/7 |
| `H2c_BODY_025_ATR_025` | 776 | 10,379 | +₹1,291,780 | ₹622,740 (48.2%) | +₹669,040 | ₹99,222 | **+₹13,328** | **7/7 (₹0 DD!)** |

- **Verdict:**
  - Notice that in `H0_BASELINE`, **₹1,144,800 (54.1% of gross profits!) went to brokerage**.
  - **`H2b_PURE_3BAR_045` is the standout `PURE` upgrade**: it beats Baseline net profit while cutting trades by `-30.5%` (saving `₹349,740` in fees), slices overall MaxDD in half (`₹52,830`), more than **doubles `v2` net profit** (`+₹112.6k → +₹251.0k` Paper, `+₹51.4k → +₹206.6k` Delayed), and turns **expiry day (`Nov 23`) from `-₹57.5k` to `+₹25.4k` (`W4` reaches `+₹31.3k` at 53% WR)**!
  - `H2a_MIN_ATR_030` is too restrictive globally (`-59%` opps), cutting winners on quiet-ATR days (`Nov 17`, `Nov 19`, `Nov 20`).

### 4.4 OLS Flag Zone Buffer (`H3a: Top 35%`, `H3b: Top 25%`, `H3c: Full Pole`)
- **Hypothesis tested:** Current scoring triggers a `BULL` OLS flag breakout as soon as price escapes the downward-sloping flag trendline — often near the middle of the pole before reclaiming pole resistance — whereas requiring full `pole_high` waits too long and misses the core move.
- **Result:**
  - `H3a_FLAG_BUF_15` (**+₹996,127** Paper) and **`H3b_FLAG_BUF_25` (+₹1,019,668** Paper) both beat Baseline (`+₹971,498`), improving `Nov 19` (`+230.6k → +268.1k`), `Nov 20` (`+136.4k → +146.8k`), and `Nov 25` (`-51.8k → -21.5k`, `v1 +54.4k → +94.2k`).
  - `H3c_FLAG_FULL_POLE` (**+₹928,583** Paper / **+₹730,551** Delayed, DD doubled to **`₹242,818`**): Exactly as you suspected, waiting for full `pole_high` entered too late (`Nov 25` dropped to `-₹170,062`) and suffered worse delayed fills.
  - **Verdict:** **`H3b` (upper 25% of pole height, i.e., `trig >= pole_h - 0.25 * pole_ht` for BULL / `<= pole_l + 0.25 * pole_ht` for BEAR)** synergizes strongly with `15s` + `OptScale`: adding `Flag25` to `W1` (`→ W2`) added another **`+₹27,583` Paper / `+₹44,521` Delayed** cleanly across trending + choppy days.

### 4.5 Midday Freeze (`11:30–13:30`) & Per-Symbol Daily Caps (`H1`, `H5a`–`H5c`)
- **`H1_MIDDAY_FREEZE` (`+₹351,084` Paper, DD `₹426,779`, 3/7 green):**
  - Why did Midday Freeze fail so badly across the dataset? Look at the daily delta (`H1 - H0`):
    - `Nov 16`: `+₹12,171` (slightly less chop loss)
    - `Nov 17`: `+₹2,804` (neutral)
    - **`Nov 18`: `-₹151,860`** (`+248.5k → +96.7k`)
    - `Nov 19`: `-₹29,701`
    - **`Nov 20`: `-₹139,133`** (`+136.4k → -2.8k`)
    - `Nov 23`: `-₹11,559`
    - **`Nov 25`: `-₹303,137`** (`-51.8k → -354.9k`, `v1` flipped from `+54.4k` to `-221.2k`!)
  - In our system, morning chop (`09:30–11:30`) stops out noisy open swings; **midday (`11:30–13:30`) is frequently where secondary clean trends and recoveries develop**. Hard-freezing entries blindly threw away the recovery leg while locking in morning stop-outs. (Midday freeze should still be configurable via config/UI per your improvements list, just **disabled by default** in momentum profiles!)
- **Hard Per-Symbol Caps (`H5a: Cap<=2` = `+₹813,029`, `H5c: Cap<=1` = `+₹318,394`, `H5b: Cap<=2 + NoFlip` = `+₹646,018`):**
  - Capping winning leaders to 1–2 alerts cut `Nov 17` net profit in half (`+540k → +329k` at Cap 2, `+253k` at Cap 1).
  - **Nuance on `block_flips` (`W5` vs `W2`):** Blocking opposite-direction intraday flips *without* a hard symbol cap (`W5_15S_FLAG25_NOFLIP_OPT_SPREAD`) made **`Nov 25` the highest single-day choppy score (`+₹141,705` Paper / `+₹96,514` Delayed)**, because whipsaw days flip BULL→BEAR→BULL on the same stock repeatedly! However, on strong reversal/sector days (`Nov 16` and `Nov 17`), blocking flips hurt (`W5` netted `+₹1,261,876` vs `W2` `+₹1,511,447`). If used at all, a **time cooldown** (e.g., 20–30m cooldown on opposite direction per symbol) is strictly preferable to a full-session flip ban.

---

## 5. Per-Track Anatomy (`v1` vs `v2`, Baseline vs `W2` & `W3`)

Total 7-day Net P&L and Win Rate by track (`Paper` / `Paper-Delayed`):

| Profile | Track | Baseline (`H0`)<br>Trades / WR / Paper / Delayed | **`W2` (Max Return)**<br>Trades / WR / Paper / Delayed | **`W3` (High Eff / Low DD)**<br>Trades / WR / Paper / Delayed | Assessment |
|---|---|---:|---:|---:|---|
| **`v1`** | `trend_pullback_ATM` | 999 / 38.2% / +₹107,821 / +₹90,583 | 983 / 39.4% / **+₹131,667 / +₹129,064** | 677 / 39.6% / +₹99,559 / +₹108,265 | Core Star |
| **`v1`** | `trend_pullback_ITM` | 909 / 37.5% / +₹84,160 / +₹69,509 | 903 / 38.8% / **+₹124,041 / +₹120,328** | 630 / 36.8% / +₹100,091 / +₹104,586 | Core Star |
| **`v1`** | `composite_shared_ITM` | 947 / 37.7% / +₹67,490 / +₹35,689 | 947 / 39.8% / **+₹120,083 / +₹113,368** | 647 / 39.3% / +₹91,376 / +₹103,196 | Massive Delayed Boost (`+3.2x`) |
| **`v1`** | `fast_momentum_ATM` | 1001 / 33.2% / +₹56,950 / +₹58,032 | 1002 / 35.5% / **+₹114,355 / +₹111,473** | 693 / 35.1% / +₹91,793 / +₹85,027 | Doubles on OptScale |
| **`v1`** | `composite_shared_ATM` | 1017 / 36.8% / +₹77,476 / +₹68,819 | 1005 / 38.3% / **+₹114,194 / +₹109,329** | 689 / 38.2% / +₹78,486 / +₹81,083 | Core Star |
| **`v1`** | `fixed_target_ATM` | 1073 / 40.0% / +₹83,277 / +₹75,982 | 1048 / 40.6% / **+₹98,348 / +₹98,258** | 720 / 42.5% / +₹75,962 / +₹70,062 | Consistent Winner |
| **`v1`** | `composite_shared_OTM` | 959 / 35.9% / +₹52,060 / +₹62,301 | 944 / 37.0% / **+₹95,859 / +₹87,895** | 663 / 36.3% / +₹65,657 / +₹50,613 | Strong Lift |
| **`v1`** | `fixed_target_ITM` | 998 / 44.3% / +₹74,447 / +₹70,656 | 983 / 44.6% / **+₹93,224 / +₹87,127** | 686 / 45.9% / +₹71,039 / +₹58,980 | Highest WR Track |
| **`v1`** | `fast_momentum_ITM` | 929 / 32.1% / +₹76,957 / +₹70,096 | 930 / 33.7% / +₹87,010 / **+₹104,807** | 643 / 33.4% / +₹74,730 / +₹78,918 | Consistent Winner |
| **`v1`** | `trend_pullback_OTM` | 946 / 39.1% / +₹73,208 / +₹70,777 | 914 / 40.0% / **+₹83,850 / +₹82,075** | 646 / 39.0% / +₹73,868 / +₹74,974 | Consistent Winner |
| **`v1`** | `fast_momentum_OTM` | 941 / 33.4% / +₹52,024 / +₹52,161 | 939 / 35.3% / **+₹80,774 / +₹85,026** | 652 / 34.8% / +₹68,847 / +₹65,454 | Consistent Winner |
| **`v1`** | `fixed_target_OTM` | 1018 / 36.2% / +₹52,985 / +₹52,226 | 972 / 35.7% / **+₹69,066 / +₹56,798** | 674 / 37.2% / +₹48,353 / +₹46,371 | Solid OTM |
| **`v2`** | `volatility_expansion_ATM` | 1169 / 21.7% / +₹81,718 / +₹60,656 | 1190 / 21.6% / **+₹128,190 / +₹117,821** | 800 / 23.5% / +₹125,644 / **+₹118,141** | Top `v2` Star |
| **`v2`** | `adaptive_regime_ATM` | 1014 / 34.7% / +₹71,988 / +₹75,034 | 1088 / 40.7% / **+₹126,502 / +₹107,424** | 725 / 41.1% / +₹109,214 / +₹106,804 | Top `v2` Star (`H6b`: `+138.9k`) |
| **`v2`** | `trend_pullback_v2_ITM1` | 973 / 37.5% / +₹81,432 / +₹76,396 | 975 / 37.5% / **+₹116,614 / +₹108,706** | 663 / 36.7% / +₹96,541 / +₹85,303 | Core `v2` Winner |
| **`v2`** | `micro_chandelier_ITM1` | 1247 / 27.7% / +₹54,810 / +₹39,852 | 1191 / 31.4% / +₹91,559 / +₹86,249 | 801 / 32.3% / **+₹98,312 / +₹90,338** | Strong `v2` Winner |
| **`v2`** | `opening_thrust_convex_ATM` | 87 / 32.2% / +₹23,475 / +₹22,600 | 79 / 34.2% / +₹25,284 / +₹25,074 | 78 / 35.9% / **+₹26,570 / +₹26,360** | Best Risk-Adj (`~₹340/trade`) |
| **`v2`** | `delta_scalp_ITM2` | 1324 / 19.3% / `-₹40,630` / `-₹59,420` | 1257 / 28.3% / `-₹14,985` / `-₹28,811` | 838 / 30.2% / **+₹22,028 / +₹10,139** | Fixed only by `W3` |
| **`v2`** | `gamma_runner_OTM2` | 706 / 25.9% / `-₹56,767` / `-₹55,188` | 680 / 27.2% / `-₹67,945` / `-₹68,936` | 529 / 29.1% / `-₹34,212` / `-₹39,285` | **Structurally Unprofitable** |
| **`v2`** | `structural_convexity_ATM` | 823 / 14.8% / `-₹103,383` / `-₹108,555` | 834 / 14.4% / `-₹106,244` / `-₹111,588` | 579 / 15.9% / `-₹61,005` / `-₹71,535` | **Structurally Unprofitable** |

### Key Structural Observations on Tracks
1. **Most Consistently Best Track Overall — `trend_pullback_ATM` (`v1`):**
   - **Profitable in `30/30` experiments** in both `paper` and `paper_delayed`, with the **#1 highest 30-experiment average Net P&L (`+₹79,020` Paper / `+₹72,899` Delayed)** and worst-case experiment still **`+₹11,418`**.
   - **Zero Daily Drawdown (`7/7` green days, `₹0` DD)** in **`H0_BASELINE` (`+₹107,821`)**, **`W2` (`+₹131,667` Paper / `+₹129,064` Delayed)**, and **`W3` (`+₹99,559` Paper / `+₹108,265` Delayed)** — even staying positive on expiry day (`2026-11-23`) and choppy `2026-11-25` across all three!
   - **Close runners-up on pure consistency:** `fixed_target_ATM` (`v1`, highest `5.97/7` average green days across all 30 experiments, lowest avg DD `₹3,296`, `7/7` green under both `W2` and `W3`) and **`adaptive_regime_ATM`** (**#1 most consistent `v2` track**: `30/30` positive, `5.57/7` avg green days, avg `₹7,886` DD, `7/7` green with `₹0` DD under `W3` at `+₹109,214`).
2. **Why 3 `v2` Tracks Consistently Bleed:**
   - `structural_convexity_ATM` (`14.8%` WR, `0/30` profitable experiments) and `gamma_runner_OTM2` (`25.9%` WR, `0/30` profitable experiments) lose steadily across every single scoring filter and execution mode because wide fixed structural stops / aggressive OTM convexity targets get chopped out 75–85% of the time in intraday F&O option pricing.
   - `delta_scalp_ITM2` fails on raw/noisy signals (`5/30` profitable experiments; `19.3%` WR, `-₹59.4k` Delayed in Baseline) because tight scalps cannot overcome ITM2 spread + latency unless filtered by `W3`'s strong 3-bar impulse (`30.2%` WR, `+₹22,028` Paper / `+₹10,139` Delayed).
3. **Pruning Impact:**
   - Retiring/disabling just the bottom 2–3 `v2` tracks (`structural_convexity_ATM`, `gamma_runner_OTM2`, and optionally `delta_scalp_ITM2`) lifts 7-day Net P&L immediately by **`+₹189,175` Paper / `+₹209,336` Delayed** on `W2` (reaching **`+₹1.70M` Paper / `+₹1.63M` Delayed**!) with dramatically smoother daily curves.

### 5.1 Trackwise Consistency Across All 30 Experiments

> **Legend:** `Pos /30` = number of the 30 tested configurations where the track finished net-positive over the 7 days (`Paper` / `Delayed`); `Avg 30 P / D` = mean 7-day Net P&L across all 30 experiments; `Min..Max (P)` = worst to best 7-day Paper P&L across the 30 configs; `Avg Green` = mean green days out of 7; `Avg DD` = mean peak-to-trough daily drawdown across the 30 experiments.

| Rank | Profile | Track | Pos /30 (P / D) | Avg 30 (Paper) | Avg 30 (Delayed) | Paper Range (`Min .. Max`) | Avg Green | Avg DD | `H0` Green / DD | `W2` Green / DD | `W3` Green / DD |
|---:|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| **1** | **`v1`** | **`trend_pullback_ATM`** | **30/30 / 30/30** | **+₹79,020** | **+₹72,899** | `+₹11,418 .. +₹134,945` | 5.87 / 7 | ₹3,444 | **7/7 / ₹0** | **7/7 / ₹0** | **7/7 / ₹0** |
| **2** | **`v2`** | `volatility_expansion_ATM` | 30/30 / 30/30 | +₹71,781 | +₹63,391 | `+₹30,165 .. +₹128,190` | 4.60 / 7 | ₹18,145 | 4/7 / ₹28,250 | 5/7 / ₹8,289 | 6/7 / ₹430 |
| **3** | **`v2`** | **`adaptive_regime_ATM`** | **30/30 / 30/30** | +₹68,165 | +₹62,343 | `+₹12,640 .. +₹138,883` | 5.57 / 7 | ₹7,886 | 6/7 / ₹9,518 | 6/7 / ₹9,151 | **7/7 / ₹0** |
| **4** | **`v1`** | `trend_pullback_ITM` | 30/30 / 30/30 | +₹66,510 | +₹62,277 | `+₹6,626 .. +₹131,564` | 4.57 / 7 | ₹13,001 | 4/7 / ₹13,382 | 5/7 / ₹9,608 | 6/7 / ₹4,609 |
| **5** | **`v1`** | **`fixed_target_ATM`** | **30/30 / 30/30** | +₹63,222 | +₹56,193 | `+₹11,603 .. +₹106,547` | **5.97 / 7** | **₹3,296** | 6/7 / ₹22 | **7/7 / ₹0** | **7/7 / ₹0** |
| **6** | **`v1`** | `composite_shared_ATM` | 30/30 / 30/30 | +₹63,222 | +₹54,873 | `+₹6,894 .. +₹114,194` | 5.60 / 7 | ₹4,363 | 5/7 / ₹208 | **7/7 / ₹0** | 5/7 / ₹3,944 |
| **7** | **`v2`** | `trend_pullback_v2_ITM1` | 30/30 / 30/30 | +₹59,933 | +₹55,361 | `+₹9,178 .. +₹118,232` | 4.33 / 7 | ₹16,077 | 5/7 / ₹14,104 | 5/7 / ₹9,908 | 6/7 / ₹6,978 |
| **8** | **`v1`** | `fast_momentum_ATM` | 30/30 / 30/30 | +₹59,823 | +₹53,730 | `+₹10,980 .. +₹114,805` | 5.33 / 7 | ₹5,690 | 4/7 / ₹8,243 | **7/7 / ₹0** | **7/7 / ₹0** |
| **9** | **`v1`** | `composite_shared_ITM` | 30/30 / 30/30 | +₹59,520 | +₹53,242 | `+₹7,727 .. +₹120,083` | 4.57 / 7 | ₹13,476 | 5/7 / ₹9,431 | 5/7 / ₹10,849 | 5/7 / ₹5,200 |
| **10** | **`v1`** | `fast_momentum_ITM` | 30/30 / 30/30 | +₹58,700 | +₹56,315 | `+₹7,813 .. +₹105,372` | 4.20 / 7 | ₹14,637 | 4/7 / ₹14,896 | 4/7 / ₹12,235 | 6/7 / ₹7,040 |
| **11** | **`v2`** | `micro_chandelier_ITM1` | 30/30 / 30/30 | +₹51,554 | +₹45,565 | `+₹20,912 .. +₹98,312` | 4.13 / 7 | ₹17,819 | 3/7 / ₹32,390 | 4/7 / ₹12,162 | **7/7 / ₹0** |
| **12** | **`v1`** | `trend_pullback_OTM` | 30/30 / 30/30 | +₹48,446 | +₹44,899 | `+₹12,484 .. +₹91,441` | 5.40 / 7 | ₹4,234 | 6/7 / ₹904 | 5/7 / ₹531 | 5/7 / ₹2,202 |
| **13** | **`v1`** | `fixed_target_ITM` | 29/30 / 29/30 | +₹47,997 | +₹43,587 | `-₹3,522 .. +₹93,224` | 4.40 / 7 | ₹16,016 | 5/7 / ₹13,793 | 5/7 / ₹8,007 | 5/7 / ₹2,508 |
| **14** | **`v1`** | `fast_momentum_OTM` | 30/30 / 30/30 | +₹47,081 | +₹42,617 | `+₹10,564 .. +₹84,959` | 5.27 / 7 | ₹5,063 | 6/7 / ₹6,555 | 5/7 / ₹3,612 | 5/7 / ₹5,253 |
| **15** | **`v1`** | `composite_shared_OTM` | 30/30 / 30/30 | +₹45,008 | +₹40,216 | `+₹7,687 .. +₹95,859` | 5.40 / 7 | ₹4,599 | 6/7 / ₹2,814 | 6/7 / **₹0** | 5/7 / ₹1,099 |
| **16** | **`v1`** | `fixed_target_OTM` | 30/30 / 30/30 | +₹37,741 | +₹32,419 | `+₹10,290 .. +₹69,066` | 5.33 / 7 | ₹3,183 | **7/7 / ₹0** | 6/7 / **₹0** | 4/7 / ₹2,047 |
| **17** | **`v2`** | `opening_thrust_convex_ATM` | 30/30 / 30/30 | +₹24,668 | +₹24,059 | `+₹10,796 .. +₹30,834` | 3.97 / 7 | ₹10,189 | 4/7 / ₹10,754 | 4/7 / ₹9,275 | 4/7 / ₹9,275 |
| **18** | **`v2`** | `delta_scalp_ITM2` | 5/30 / 3/30 | -₹19,067 | -₹29,082 | `-₹65,342 .. +₹22,028` | 3.53 / 7 | ₹42,406 | 4/7 / ₹62,335 | 4/7 / ₹44,773 | 4/7 / ₹27,190 |
| **19** | **`v2`** | `gamma_runner_OTM2` | 0/30 / 0/30 | -₹44,230 | -₹45,653 | `-₹69,952 .. -₹19,009` | 2.00 / 7 | ₹45,928 | 3/7 / ₹57,878 | 1/7 / ₹67,945 | 3/7 / ₹34,212 |
| **20** | **`v2`** | `structural_convexity_ATM` | 0/30 / 0/30 | -₹76,666 | -₹79,141 | `-₹120,202 .. -₹10,642` | 1.63 / 7 | ₹80,776 | 2/7 / ₹103,383 | 2/7 / ₹107,613 | 2/7 / ₹82,197 |

### 5.2 Per-Day Trackwise Net P&L Matrix

#### A. Under Champion `W2_15S_FLAG25_OPT_SPREAD` (`Paper` / `Paper-Delayed`)

| Profile | Track | `Nov 16` | `Nov 17` | `Nov 18` | `Nov 19` | `Nov 20` | `Nov 23` *(Expiry)* | `Nov 25` | 7-Day Total (P / D) | Green (P/D) | DD (P/D) |
|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| `v1` | `trend_pullback_ATM` | +₹6,352 | +₹33,847 | +₹28,265 | +₹20,927 | +₹15,112 | +₹645 | +₹26,519 | **+₹131,667 / +₹129,064** | **7/7 / 7/7** | **₹0 / ₹0** |
| `v2` | `volatility_expansion_ATM` | -₹8,289 | +₹47,254 | +₹30,473 | +₹25,472 | +₹29,764 | -₹4,986 | +₹8,503 | **+₹128,190 / +₹117,821** | 5/7 / 5/7 | ₹8,289 / ₹9,273 |
| `v2` | `adaptive_regime_ATM` | +₹9,403 | +₹44,663 | +₹18,353 | +₹22,172 | +₹28,886 | -₹9,151 | +₹12,175 | **+₹126,502 / +₹107,424** | 6/7 / 5/7 | ₹9,151 / ₹12,405 |
| `v1` | `trend_pullback_ITM` | -₹4,276 | +₹52,563 | +₹24,088 | +₹32,032 | +₹13,318 | -₹9,608 | +₹15,922 | **+₹124,041 / +₹120,328** | 5/7 / 5/7 | ₹9,608 / ₹9,608 |
| `v1` | `composite_shared_ITM` | -₹4,721 | +₹50,165 | +₹27,709 | +₹34,889 | +₹8,716 | -₹10,849 | +₹14,174 | **+₹120,083 / +₹113,368** | 5/7 / 5/7 | ₹10,849 / ₹10,849 |
| `v2` | `trend_pullback_v2_ITM1` | -₹9,908 | +₹47,083 | +₹21,425 | +₹34,129 | +₹17,393 | -₹7,903 | +₹14,395 | **+₹116,614 / +₹108,706** | 5/7 / 5/7 | ₹9,908 / ₹9,547 |
| `v1` | `fast_momentum_ATM` | +₹5,248 | +₹35,533 | +₹11,895 | +₹22,335 | +₹18,639 | +₹390 | +₹20,316 | **+₹114,355 / +₹111,473** | **7/7 / 7/7** | **₹0 / ₹0** |
| `v1` | `composite_shared_ATM` | +₹2,174 | +₹36,724 | +₹16,403 | +₹17,362 | +₹17,248 | +₹390 | +₹23,893 | **+₹114,194 / +₹109,329** | **7/7 / 7/7** | **₹0 / ₹0** |
| `v1` | `fixed_target_ATM` | +₹10,080 | +₹25,686 | +₹20,490 | +₹14,364 | +₹16,905 | +₹240 | +₹10,583 | **+₹98,348 / +₹98,258** | **7/7 / 7/7** | **₹0 / ₹0** |
| `v1` | `composite_shared_OTM` | +₹1,529 | +₹37,100 | +₹16,889 | +₹7,287 | +₹10,311 | +₹0 | +₹22,744 | **+₹95,859 / +₹87,895** | 6/7 / 5/7 | ₹0 / ₹1,019 |
| `v1` | `fixed_target_ITM` | -₹282 | +₹39,556 | +₹32,285 | +₹23,722 | +₹4,748 | -₹8,007 | +₹1,204 | **+₹93,224 / +₹87,127** | 5/7 / 5/7 | ₹8,007 / ₹11,113 |
| `v2` | `micro_chandelier_ITM1` | -₹3,265 | +₹34,182 | +₹37,040 | +₹24,860 | +₹10,904 | -₹6,039 | -₹6,123 | **+₹91,559 / +₹86,249** | 4/7 / 4/7 | ₹12,162 / ₹16,304 |
| `v1` | `fast_momentum_ITM` | -₹6,557 | +₹35,322 | +₹30,260 | +₹38,015 | +₹2,205 | -₹9,079 | -₹3,156 | **+₹87,010 / +₹104,807** | 4/7 / 5/7 | ₹12,235 / ₹9,079 |
| `v1` | `trend_pullback_OTM` | -₹531 | +₹31,989 | +₹19,272 | +₹8,772 | +₹8,506 | +₹0 | +₹15,843 | **+₹83,850 / +₹82,075** | 5/7 / 5/7 | ₹531 / ₹1,402 |
| `v1` | `fast_momentum_OTM` | -₹3,612 | +₹33,519 | +₹12,010 | +₹6,513 | +₹9,664 | +₹0 | +₹22,680 | **+₹80,774 / +₹85,026** | 5/7 / 5/7 | ₹3,612 / ₹6,119 |
| `v1` | `fixed_target_OTM` | +₹3,259 | +₹24,061 | +₹18,726 | +₹2,237 | +₹9,648 | +₹0 | +₹11,135 | **+₹69,066 / +₹56,798** | 6/7 / 6/7 | ₹0 / ₹0 |
| `v2` | `opening_thrust_convex_ATM` | +₹5,953 | +₹17,836 | +₹0 | +₹10,760 | -₹1,940 | +₹1,950 | -₹9,275 | **+₹25,284 / +₹25,074** | 4/7 / 4/7 | ₹9,275 / ₹9,498 |
| `v2` | `delta_scalp_ITM2` | -₹10,242 | +₹9,169 | +₹4,991 | +₹21,671 | +₹4,199 | -₹7,450 | -₹37,323 | `-₹14,985 / -₹28,811` | 4/7 / 4/7 | ₹44,773 / ₹51,905 |
| `v2` | `gamma_runner_OTM2` | -₹31,258 | -₹6,083 | -₹9,101 | -₹10,589 | +₹4,658 | -₹232 | -₹15,340 | `-₹67,945 / -₹68,936` | 1/7 / 1/7 | ₹67,945 / ₹68,936 |
| `v2` | `structural_convexity_ATM` | -₹15,593 | +₹16,962 | -₹16,973 | -₹52,836 | -₹11,070 | +₹1,918 | -₹28,652 | `-₹106,244 / -₹111,588` | 2/7 / 2/7 | ₹107,613 / ₹111,588 |

#### B. Under Low-DD / Expiry-Proof `W3_15S_FLAG25_3BAR045_OPT_SPREAD` (`Paper`)

| Profile | Track | `Nov 16` | `Nov 17` | `Nov 18` | `Nov 19` | `Nov 20` | `Nov 23` *(Expiry)* | `Nov 25` | 7-Day Total (P) | Green | DD |
|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| `v2` | `volatility_expansion_ATM` | -₹430 | +₹45,112 | +₹32,013 | +₹14,593 | +₹26,406 | +₹1,784 | +₹6,167 | **+₹125,644** | 6/7 | ₹430 |
| `v2` | `adaptive_regime_ATM` | +₹6,677 | +₹32,409 | +₹26,355 | +₹6,574 | +₹26,137 | +₹706 | +₹10,356 | **+₹109,214** | **7/7** | **₹0** |
| `v1` | `trend_pullback_ITM` | -₹4,609 | +₹40,557 | +₹24,959 | +₹10,753 | +₹8,322 | +₹1,914 | +₹18,196 | **+₹100,091** | 6/7 | ₹4,609 |
| `v1` | `trend_pullback_ATM` | +₹3,240 | +₹28,724 | +₹32,137 | +₹16,672 | +₹10,747 | +₹1,125 | +₹6,913 | **+₹99,559** | **7/7** | **₹0** |
| `v2` | `micro_chandelier_ITM1` | +₹1,407 | +₹30,756 | +₹38,446 | +₹18,306 | +₹8,605 | +₹92 | +₹701 | **+₹98,312** | **7/7** | **₹0** |
| `v2` | `trend_pullback_v2_ITM1` | -₹6,978 | +₹41,489 | +₹25,566 | +₹10,423 | +₹19,782 | +₹1,954 | +₹4,304 | **+₹96,541** | 6/7 | ₹6,978 |
| `v1` | `fast_momentum_ATM` | +₹1,749 | +₹31,182 | +₹17,259 | +₹14,994 | +₹14,357 | +₹870 | +₹11,381 | **+₹91,793** | **7/7** | **₹0** |
| `v1` | `composite_shared_ITM` | -₹5,200 | +₹39,768 | +₹33,121 | +₹17,500 | +₹5,228 | +₹1,584 | -₹626 | **+₹91,376** | 5/7 | ₹5,200 |
| `v1` | `composite_shared_ATM` | -₹3,944 | +₹26,227 | +₹31,743 | +₹9,337 | +₹14,684 | +₹870 | -₹431 | **+₹78,486** | 5/7 | ₹3,944 |
| `v1` | `fixed_target_ATM` | +₹4,936 | +₹19,585 | +₹22,699 | +₹13,717 | +₹12,909 | +₹720 | +₹1,397 | **+₹75,962** | **7/7** | **₹0** |
| `v1` | `fast_momentum_ITM` | -₹7,040 | +₹27,979 | +₹29,630 | +₹16,170 | +₹1,545 | +₹2,442 | +₹4,003 | **+₹74,730** | 6/7 | ₹7,040 |
| `v1` | `trend_pullback_OTM` | -₹2,202 | +₹29,851 | +₹18,907 | +₹7,404 | +₹5,406 | +₹0 | +₹14,502 | **+₹73,868** | 5/7 | ₹2,202 |
| `v1` | `fixed_target_ITM` | -₹2,449 | +₹31,536 | +₹26,926 | +₹14,185 | +₹1,978 | +₹1,370 | -₹2,508 | **+₹71,039** | 5/7 | ₹2,508 |
| `v1` | `fast_momentum_OTM` | -₹5,253 | +₹32,723 | +₹14,784 | +₹4,883 | +₹6,422 | +₹0 | +₹15,288 | **+₹68,847** | 5/7 | ₹5,253 |
| `v1` | `composite_shared_OTM` | -₹1,099 | +₹27,129 | +₹18,294 | +₹1,335 | +₹6,332 | +₹0 | +₹13,666 | **+₹65,657** | 5/7 | ₹1,099 |
| `v1` | `fixed_target_OTM` | -₹549 | +₹24,260 | +₹17,756 | -₹2,047 | +₹6,513 | +₹0 | +₹2,420 | **+₹48,353** | 4/7 | ₹2,047 |
| `v2` | `opening_thrust_convex_ATM` | +₹5,953 | +₹18,346 | +₹0 | +₹11,470 | -₹1,875 | +₹1,950 | -₹9,275 | **+₹26,570** | 4/7 | ₹9,275 |
| `v2` | `delta_scalp_ITM2` | -₹17 | +₹10,710 | +₹13,569 | +₹15,084 | +₹9,872 | -₹2,198 | -₹24,992 | **+₹22,028** | 4/7 | ₹27,190 |
| `v2` | `gamma_runner_OTM2` | -₹24,029 | +₹13,139 | -₹4,561 | -₹10,352 | +₹2,803 | +₹1,008 | -₹12,219 | `-₹34,212` | 3/7 | ₹34,212 |
| `v2` | `structural_convexity_ATM` | -₹12,286 | +₹33,478 | -₹10,426 | -₹32,172 | -₹11,018 | +₹2,232 | -₹30,812 | `-₹61,005` | 2/7 | ₹82,197 |

---

## 6. Recommended Next Steps for Discussion

Before making any edits to production code or configs, here are the concrete choices to review together:

1. **Choose the Target Operating Profile (`W2` vs `W3`):**
   - **Option A — `W2` (`15s Elapsed` + `OLS Flag Top 25% of Pole` + `Option Convexity Scaling & Spread Gate`):** Maximizes raw net profit (**`+₹1.51M` Paper / `+₹1.42M` Delayed** across all 20 tracks; **`+₹1.70M` / `+₹1.63M`** pruned) while retaining high signal throughput (`~193` opps/day average). Expect occasional expiry-day drag unless expiry has a stricter intraday rule.
   - **Option B — `W3` (`W2` + `PURE Net 3-Bar Expansion >= 0.45%`):** Sacrifices only ~12% peak return (**`+₹1.32M` Paper / `+₹1.25M` Delayed** across all 20 tracks) in exchange for **32% fewer trades, `₹363k` lower brokerage, lowest drawdown (`₹52.1k`), `6/7` green days, `+₹383k` on `v2`, and profitable Expiry Day (`Nov 23`)**!
   - **Dynamic Alternative (Expiry-Aware Profile):** Run **`W2`** on regular sessions (`Mon–Wed/Fri` or non-expiry days) and automatically toggle **`PURE 3-Bar >= 0.45%` (`W3`) on Weekly/Monthly Expiry Days** (where `W2` lost `-₹67.8k` on `Nov 23` vs `W3`'s **`+₹18.4k`** and `W4`'s **`+₹31.3k`**)! Combining `W2` on non-expiry days + `W3` on expiry day yields `+₹1,597,642` Paper / `+₹1,507,682` Delayed across all 20 tracks — beating both!

2. **Codebase Promotion Path (When Ready to Implement):**
   - Add `min_candle_elapsed_sec: 15.0`, `flag_pole_buffer_pct: 0.25`, and optional `pure_min_3bar_expansion_pct: 0.0045` to `ScoringEngine3` & `config/scoring.json` (plus an optional `is_expiry_day` strict-impulse mode).
   - Move `Option Convexity Target/SL Scaling` and `ATR/Spread Gate` natively into `BaseTrack` (enabled/configured per track in `config/tracks/`).
   - Decide whether to disable/archive `structural_convexity_ATM`, `gamma_runner_OTM2`, and keep `delta_scalp_ITM2` only under high-conviction impulse filters.
   - Keep **Midday Freeze (`11:30–13:30`)** implemented cleanly as an *opt-in* config flag (`false` by default for momentum profiles), since empirical testing proved blanket freezing severely hurts follow-through sessions.

---

## 7. Complete Cached Economics, Slippage & Marginal Attribution (All 30 Runs)

### 7.1 Full Cost, Slippage & Risk-Adjusted Efficiency Table (`Paper` & `Paper-Delayed`)

> **Legend:** `GP` = Gross P&L (pre-brokerage); `Brok (%GP)` = brokerage paid (`₹60/round-trip`) and % of Gross consumed; `P-Net / D-Net` = 7-day Net P&L; `Slip (%Ret)` = `P-Net - D-Net` slippage cost and Delayed retention % (`D-Net / P-Net`); `₹/Tr (P/D)` = net expectancy per trade; `Ret/DD` = `P-Net / Paper MaxDD`.

| Experiment ID | Opps | Tr (P / D) | WR% (P / D) | Paper Gross | Brok (`% GP`) | Paper Net | Delayed Net | Slippage (`% Ret`) | ₹/Trade (P / D) | MaxDD (P) | Ret/DD |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| `W2_15S_FLAG25_OPT_SPREAD` | 1,349 | 18,864 / 18,812 | 34.7 / 34.6 | +₹2,643,287 | ₹1,131,840 (42.8%) | **+₹1,511,447** | **+₹1,421,488** | +₹89,959 (94.0%) | +₹80.1 / +₹75.6 | ₹67,771 | 22.3x |
| `W1_15S_OPT_SPREAD` | 1,355 | 18,964 / 18,922 | 34.7 / 34.4 | +₹2,621,704 | ₹1,137,840 (43.4%) | +₹1,483,864 | +₹1,376,967 | +₹106,897 (92.8%) | +₹78.2 / +₹72.8 | ₹67,771 | 21.9x |
| `H6b_OPT_SCALE_SPREAD` | 1,385 | 19,252 / 19,203 | 34.3 / 34.0 | +₹2,484,764 | ₹1,155,120 (46.5%) | +₹1,329,644 | +₹1,181,570 | +₹148,075 (88.9%) | +₹69.1 / +₹61.5 | ₹54,938 | 24.2x |
| `W3_15S_FLAG25_3BAR045_OPT_SPREAD` | 889 | 13,033 / 13,025 | **35.2 / 35.1** | +₹2,104,833 | ₹781,980 (**37.2%**) | +₹1,322,853 | +₹1,253,793 | +₹69,060 (**94.8%**) | **+₹101.5 / +₹96.3** | ₹52,123 | 25.4x |
| `W5_15S_FLAG25_NOFLIP_OPT_SPREAD` | 1,021 | 15,013 / 14,976 | 34.6 / 34.4 | +₹2,162,656 | ₹900,780 (41.7%) | +₹1,261,876 | +₹1,193,512 | +₹68,364 (94.6%) | +₹84.1 / +₹79.7 | ₹118,544 | 10.6x |
| `H4a_ELAPSED_15S` | 1,446 | 18,763 / 18,735 | 33.1 / 32.8 | +₹2,292,785 | ₹1,125,780 (49.1%) | +₹1,167,005 | +₹1,014,754 | +₹152,250 (87.0%) | +₹62.2 / +₹54.2 | ₹96,581 | 12.1x |
| `H6a_OPT_SCALE` | 1,477 | 19,644 / 19,578 | 33.9 / 33.6 | +₹2,297,260 | ₹1,178,640 (51.3%) | +₹1,118,620 | +₹972,379 | +₹146,240 (86.9%) | +₹56.9 / +₹49.7 | ₹149,715 | 7.5x |
| `W4_3BAR045_OPT_SPREAD` | 923 | 13,425 / 13,420 | 34.4 / 34.3 | +₹1,906,994 | ₹805,500 (42.2%) | +₹1,101,494 | +₹988,718 | +₹112,776 (89.8%) | +₹82.0 / +₹73.7 | ₹37,962 | **29.0x** |
| `H2b_PURE_3BAR_045` | 970 | 13,251 / 13,207 | 33.2 / 33.1 | +₹1,823,675 | ₹795,060 (43.6%) | +₹1,028,615 | +₹906,560 | +₹122,054 (88.1%) | +₹77.6 / +₹68.6 | ₹52,830 | 19.5x |
| `H3b_FLAG_BUF_25` | 1,465 | 18,895 / 18,881 | 32.8 / 32.3 | +₹2,153,368 | ₹1,133,700 (52.6%) | +₹1,019,668 | +₹821,316 | +₹198,351 (80.5%) | +₹54.0 / +₹43.5 | ₹112,462 | 9.1x |
| `H3a_FLAG_BUF_15` | 1,469 | 18,995 / 18,978 | 32.8 / 32.4 | +₹2,135,827 | ₹1,139,700 (53.4%) | +₹996,127 | +₹804,243 | +₹191,884 (80.7%) | +₹52.4 / +₹42.4 | ₹103,076 | 9.7x |
| `H0_BASELINE` | 1,477 | 19,080 / 19,028 | 32.8 / 32.4 | +₹2,116,298 | ₹1,144,800 (54.1%) | +₹971,498 | +₹828,205 | +₹143,293 (85.3%) | +₹50.9 / +₹43.5 | ₹109,330 | 8.9x |
| `H3c_FLAG_FULL_POLE` | 1,430 | 18,483 / 18,461 | 33.0 / 32.6 | +₹2,037,563 | ₹1,108,980 (54.4%) | +₹928,583 | +₹730,551 | +₹198,032 (78.7%) | +₹50.2 / +₹39.6 | ₹242,818 | 3.8x |
| `W6_BODY025_ATR025_OPT_SPREAD` | 733 | 10,496 / 10,498 | 34.6 / 34.4 | +₹1,485,593 | ₹629,760 (42.4%) | +₹855,833 | +₹761,490 | +₹94,343 (89.0%) | +₹81.5 / +₹72.5 | **₹21,500** | **39.8x** |
| `H4b_ELAPSED_30S` | 1,427 | 18,703 / 18,677 | 32.7 / 32.3 | +₹1,946,299 | ₹1,122,180 (57.7%) | +₹824,119 | +₹595,188 | +₹228,931 (72.2%) | +₹44.1 / +₹31.9 | ₹187,045 | 4.4x |
| `H5a_CAP_2` | 1,078 | 14,903 / 14,889 | 33.0 / 32.6 | +₹1,707,209 | ₹894,180 (52.4%) | +₹813,029 | +₹671,716 | +₹141,312 (82.6%) | +₹54.6 / +₹45.1 | ₹115,794 | 7.0x |
| `H2a_MIN_ATR_030` | 604 | 8,270 / 8,234 | 33.3 / 32.9 | +₹1,284,155 | ₹496,200 (38.6%) | +₹787,955 | +₹658,647 | +₹129,308 (83.6%) | +₹95.3 / +₹80.0 | ₹79,034 | 10.0x |
| `M9_ALL_SIGNALS_SCALED` | 749 | 10,880 / 10,862 | 33.5 / 33.5 | +₹1,368,432 | ₹652,800 (47.7%) | +₹715,632 | +₹679,229 | +₹36,403 (94.9%) | +₹65.8 / +₹62.5 | ₹300,841 | 2.4x |
| `H2c_BODY_025_ATR_025` | 776 | 10,379 / 10,337 | 33.0 / 32.7 | +₹1,291,780 | ₹622,740 (48.2%) | +₹669,040 | +₹565,583 | +₹103,457 (84.5%) | +₹64.5 / +₹54.7 | ₹99,222 | 6.7x |
| `H5b_CAP_2_NO_FLIP` | 944 | 13,407 / 13,392 | 32.6 / 32.2 | +₹1,450,438 | ₹804,420 (55.5%) | +₹646,018 | +₹537,389 | +₹108,629 (83.2%) | +₹48.2 / +₹40.1 | ₹117,620 | 5.5x |
| `M1_CORE_CLEAN` | 735 | 10,202 / 10,186 | 32.2 / 32.0 | +₹1,222,604 | ₹612,120 (50.1%) | +₹610,484 | +₹513,743 | +₹96,741 (84.2%) | +₹59.8 / +₹50.4 | ₹265,067 | 2.3x |
| `M2_BALANCED` | 294 | 4,529 / 4,525 | 33.1 / 33.0 | +₹752,436 | ₹271,740 (**36.1%**) | +₹480,696 | +₹436,259 | +₹44,436 (90.8%) | **+₹106.1 / +₹96.4** | ₹68,058 | 7.1x |
| `M7_SPREAD_GATE_CORE` | 629 | 9,389 / 9,387 | 32.5 / 32.4 | +₹997,994 | ₹563,340 (56.4%) | +₹434,654 | +₹357,653 | +₹77,001 (82.3%) | +₹46.3 / +₹38.1 | ₹362,092 | 1.2x |
| `M4_BALANCED_SCALED` | 294 | 4,664 / 4,665 | 34.0 / 33.8 | +₹704,130 | ₹279,840 (39.7%) | +₹424,290 | +₹390,975 | +₹33,315 (92.1%) | +₹91.0 / +₹83.8 | ₹80,551 | 5.3x |
| `M6_HIGH_CONVICTION` | 269 | 4,342 / 4,340 | 33.6 / 33.6 | +₹635,260 | ₹260,520 (41.0%) | +₹374,740 | +₹357,050 | +₹17,690 (95.3%) | +₹86.3 / +₹82.3 | ₹79,864 | 4.7x |
| `H1_MIDDAY_FREEZE` | 830 | 11,105 / 11,100 | 31.4 / 31.4 | +₹1,017,384 | ₹666,300 (**65.5%**) | +₹351,084 | +₹324,593 | +₹26,491 (92.5%) | +₹31.6 / +₹29.2 | **₹426,779** | 0.8x |
| `M3_SMART_IMPULSE` | 348 | 5,264 / 5,249 | 31.9 / 32.0 | +₹645,558 | ₹315,840 (48.9%) | +₹329,718 | +₹313,284 | +₹16,433 (95.0%) | +₹62.6 / +₹59.7 | ₹233,101 | 1.4x |
| `H5c_CAP_1` | 632 | 9,311 / 9,303 | 32.3 / 31.8 | +₹877,054 | ₹558,660 (63.7%) | +₹318,394 | +₹213,016 | +₹105,377 (66.9%) | +₹34.2 / +₹22.9 | ₹211,055 | 1.5x |
| `M8_BODY025_SPREAD_SCALED` | 321 | 5,087 / 5,084 | 33.4 / 33.2 | +₹579,655 | ₹305,220 (52.7%) | +₹274,435 | +₹213,254 | +₹61,181 (77.7%) | +₹53.9 / +₹41.9 | ₹233,661 | 1.2x |
| `M5_IMPULSE_SCALED` | 348 | 5,439 / 5,436 | 32.3 / 32.5 | +₹547,781 | ₹326,340 (59.6%) | +₹221,441 | +₹220,166 | +₹1,275 (99.4%) | +₹40.7 / +₹40.5 | ₹346,371 | 0.6x |

### 7.2 Marginal Attribution & Interaction Effects
- **`H0 → H4a_ELAPSED_15S` (`+₹195,507` Paper / `+₹186,549` Delayed):** Dropping the first `15s` of every 5m bar only removes 31 opps (`1,477 → 1,446`), yet raises GP by `+₹176,487` and lowers brokerage by `₹19,020`.
- **`H0 → H6a_OPT_SCALE` (`+₹147,122` Paper / `+₹144,174` Delayed) → `H6b_OPT_SCALE_SPREAD` (`+₹211,024` additional Paper / `+₹209,191` Delayed):** Option target/SL convexity scaling adds edge across trending days, while the ATM `ATR / Spread` gate selectively prunes **92** illiquid/wide-spread entries (`1,477 → 1,385`), flipping `Nov 16` and `Nov 25` positive and halving MaxDD (`₹149.7k → ₹54.9k`).
- **Synergy (`H6b → W1 → W2`):** Adding `15s Elapsed` onto `H6b` adds **`+₹154,220` Paper / `+₹195,397` Delayed** (`W1`). Adding `Flag Top 25%` (`W1 → W2`) trims only 6 opps (`1,355 → 1,349`) yet adds **`+₹27,583` Paper / `+₹44,521` Delayed**, proving delayed-execution resilience (`94.0%` retention vs `85.3%` Baseline).
- **Slippage Inversion on High-Conviction ITM Tracks:** Under `H0_BASELINE`, `v1_ITM` suffered **`+₹57,104` delayed slippage** (`+₹303,054 → +₹245,950`). Under `W2` and `W3`, `v1_ITM` delayed fills actually beat instant paper fills (**`W2: -₹1,273` slippage**; **`W3: -₹8,444` slippage**!), because non-spike breakout confirmations frequently retrace a fraction of a tick during the `100–250ms` order delay before continuing higher.

---

## 8. Regime-Grouped Performance (Batch Report Analytical Prototype)

Grouping the 7 sessions into **3 market regimes** illustrates the exact slicing planned for `IMPROVEMENTS.md §2` Batch Simulation Reports:
- **`TREND (4D)`** — `Nov 17`, `Nov 18`, `Nov 19`, `Nov 20` (clean trend follow-through)
- **`CHOP / REVERSAL (2D)`** — `Nov 16`, `Nov 25` (high-frequency two-way chop / reversals)
- **`EXPIRY (1D)`** — `Nov 23` (thin option books / expiry pinning & gamma spikes)

| Experiment ID | `TREND 4D` Paper (`v1` / `v2`) | `TREND 4D` Delayed | `CHOP 2D` Paper (`v1` / `v2`) | `CHOP 2D` Delayed | `EXPIRY 1D` Paper (`v1` / `v2`) | `EXPIRY 1D` Delayed |
|---|---:|---:|---:|---:|---:|---:|
| **`W2_15S_FLAG25_OPT_SPREAD`** | **+₹1,513,535** (`+1,057.8k / +455.7k`) | **+₹1,480,107** | +₹65,683 (`+190.5k / -124.8k`) | +₹9,152 | `-₹67,771` (`-35.9k / -31.9k`) | `-₹67,771` |
| `W1_15S_OPT_SPREAD` | +₹1,461,506 (`+1,008.8k / +452.7k`) | +₹1,420,674 | **+₹90,129** (`+223.0k / -132.9k`) | **+₹24,064** | `-₹67,771` (`-35.9k / -31.9k`) | `-₹67,771` |
| **`W3_15S_FLAG25_3BAR045_OPT_SPREAD`** | +₹1,328,123 (`+867.1k / +461.0k`) | +₹1,299,623 | `-₹23,694` (`+61.8k / -85.5k`) | `-₹64,254` | **+₹18,424** (`+10.9k / +7.5k`) | **+₹18,424** |
| `H6b_OPT_SCALE_SPREAD` | +₹1,300,575 (`+913.4k / +387.1k`) | +₹1,258,035 | +₹84,008 (`+177.5k / -93.5k`) | `-₹21,977` | `-₹54,938` (`-28.5k / -26.5k`) | `-₹54,488` |
| `W4_3BAR045_OPT_SPREAD` | +₹1,099,526 (`+726.2k / +373.4k`) | +₹1,052,390 | `-₹29,288` (`+70.6k / -99.9k`) | `-₹95,378` | **+₹31,256** (`+18.3k / +13.0k`) | **+₹31,706** |
| `W5_15S_FLAG25_NOFLIP_OPT_SPREAD` | +₹1,306,114 (`+886.7k / +419.4k`) | +₹1,288,836 | +₹23,161 (`+139.4k / -116.3k`) | `-₹27,924` | `-₹67,400` (`-35.9k / -31.5k`) | `-₹67,400` |
| `H4a_ELAPSED_15S` | +₹1,320,524 (`+925.8k / +394.8k`) | +₹1,270,373 | `-₹85,337` (`+101.6k / -186.9k`) | `-₹187,435` | `-₹68,183` (`-37.5k / -30.7k`) | `-₹68,183` |
| `H2b_PURE_3BAR_045` | +₹1,065,796 (`+730.4k / +335.4k`) | +₹1,015,693 | `-₹62,568` (`+31.6k / -94.2k`) | `-₹135,344` | **+₹25,386** (`+15.6k / +9.8k`) | **+₹26,211** |
| **`H0_BASELINE`** | +₹1,155,580 (`+832.4k / +323.2k`) | +₹1,087,554 | `-₹126,550` (`+58.3k / -184.8k`) | `-₹202,641` | `-₹57,533` (`-31.8k / -25.7k`) | `-₹56,708` |
| `H2a_MIN_ATR_030` | +₹776,500 (`+553.0k / +223.5k`) | +₹756,976 | `-₹5,015` (`+109.0k / -114.0k`) | `-₹115,625` | +₹16,471 (`+6.6k / +9.9k`) | +₹17,296 |
| `W6_BODY025_ATR025_OPT_SPREAD` | +₹810,268 (`+565.7k / +244.5k`) | +₹772,464 | +₹26,369 (`+89.5k / -63.1k`) | `-₹30,620` | +₹19,196 (`+9.3k / +9.9k`) | +₹19,646 |
| `M9_ALL_SIGNALS_SCALED` | +₹1,064,701 (`+719.4k / +345.3k`) | +₹1,092,858 | `-₹267,130` (`-104.1k / -163.0k`) | `-₹331,690` | `-₹81,939` (`-42.3k / -39.6k`) | `-₹81,939` |
| `H1_MIDDAY_FREEZE` | +₹837,691 (`+582.7k / +255.0k`) | +₹830,117 | `-₹417,515` (`-223.3k / -194.2k`) | `-₹436,432` | `-₹69,092` (`-36.6k / -32.5k`) | `-₹69,092` |

> **Regime Takeaway:** `W2` and `W1` are the **only** full-suite configs green on `CHOP 2D` in **both** Paper and Delayed (`+₹65.7k / +₹9.2k` and `+₹90.1k / +₹24.1k`), whereas every `3-Bar >= 0.45%` / `ATR >= 0.25%–0.30%` filter is green on **`EXPIRY 1D`** (`W4 +₹31.3k`, `H2b +₹25.4k`, `W6 +₹19.2k`, `W3 +₹18.4k`). Meanwhile `H1_MIDDAY_FREEZE` turns `CHOP 2D` from `-₹126.6k` to **`-₹417.5k`** (`3.3x` worse!).

---

## 9. Track Family, Moneyness & Win/Loss/Flat Micro-Statistics

### 9.1 Strategy Family & Moneyness Totals (`H0`, `H6b`, `W2`, `W3`)

| Group | `H0_BASELINE` (`Tr / P-Net / D-Net`) | `H6b_OPT_SCALE_SPREAD` | `W2_15S_FLAG25_OPT_SPREAD` | `W3_15S_FLAG25_3BAR045_OPT_SPREAD` | Key Observation |
|---|---:|---:|---:|---:|---|
| `trend_pullback` (3) | 2,854 / +₹265,189 / +₹230,868 | 2,843 / +₹307,426 / +₹287,835 | 2,800 / **+₹339,558 / +₹331,467** | 1,953 / +₹273,518 / +₹287,824 | #1 `v1` family across all modes |
| `composite_shared` (3) | 2,923 / +₹197,025 / +₹166,809 | 2,964 / +₹261,465 / +₹236,176 | 2,896 / **+₹330,136 / +₹310,592** | 1,999 / +₹235,519 / +₹234,892 | Largest family lift (`+67.6%` Paper, `+86.2%` Delayed) |
| `fast_momentum` (3) | 2,871 / +₹185,931 / +₹180,290 | 2,923 / +₹265,718 / +₹244,578 | 2,871 / **+₹282,140 / +₹301,307** | 1,988 / +₹235,370 / +₹229,400 | `W2` Delayed beats Paper! |
| `fixed_target` (3) | 3,089 / +₹210,709 / +₹198,864 | 3,073 / +₹227,803 / +₹210,060 | 3,003 / **+₹260,638 / +₹242,183** | 2,080 / +₹195,354 / +₹175,412 | Highest win rates, lowest variance |
| **`v2_profitable_5`** | 4,490 / +₹313,424 / +₹274,537 | 4,616 / +₹461,305 / +₹415,323 | 4,523 / **+₹488,149 / +₹445,275** | 3,067 / +₹456,280 / +₹426,945 | Beats any individual `v1` family! |
| **`v2_losing_3`** | 2,853 / `-₹200,780` / `-₹223,163` | 2,833 / `-₹194,072` / `-₹212,402` | 2,771 / `-₹189,175` / `-₹209,336` | 1,946 / **`-₹73,188`** / `-₹100,681` | Only `W3` cuts losses by >60% |
| `v1_ITM` (4) | 3,783 / +₹303,054 / +₹245,950 | 3,828 / +₹357,696 / +₹335,860 | 3,763 / **+₹424,358 / +₹425,631** | 2,606 / +₹337,236 / +₹345,680 | **Negative slippage (`-₹1.3k` W2, `-₹8.4k` W3)** |
| `v1_ATM` (4) | 4,090 / +₹325,524 / +₹293,416 | 4,130 / +₹432,369 / +₹391,001 | 4,038 / **+₹458,564 / +₹448,124** | 2,779 / +₹345,800 / +₹344,436 | Highest total `v1` P&L |
| `v1_OTM` (4) | 3,864 / +₹230,276 / +₹237,465 | 3,845 / +₹272,347 / +₹251,788 | 3,769 / **+₹329,550 / +₹311,794** | 2,635 / +₹256,725 / +₹237,412 | `+43.1%` lift from `W2` convexity |

### 9.2 Full Trade-Outcome Breakdown (`Wins >2.5%` / `Losses <0%` / `Flat 0–2.5%` & `₹/Trade`)

| Track | `H0_BASELINE` (`W / L / Flat \| GP \| ₹/Tr`) | `W2` (`W / L / Flat \| GP \| Slip \| ₹/Tr`) | `W3` (`W / L / Flat \| GP \| Slip \| ₹/Tr`) |
|---|---|---|---|
| `trend_pullback_ATM` | 382 / 430 / 187 \| +₹167,761 \| +₹107.9 | 387 / 407 / 189 \| +₹190,647 \| +₹2,603 \| +₹133.9 | 268 / 293 / 116 \| +₹140,179 \| -₹8,706 \| +₹147.1 |
| `trend_pullback_ITM` | 341 / 412 / 156 \| +₹138,700 \| +₹92.6 | 350 / 400 / 153 \| +₹178,221 \| +₹3,712 \| +₹137.4 | 232 / 287 / 111 \| +₹137,891 \| -₹4,494 \| **+₹158.9** |
| `composite_shared_ITM` | 357 / 478 / 112 \| +₹124,310 \| +₹71.3 | 377 / 462 / 108 \| +₹176,903 \| +₹6,715 \| +₹126.8 | 254 / 320 / 73 \| +₹130,196 \| -₹11,820 \| +₹141.2 |
| `fast_momentum_ATM` | 332 / 550 / 119 \| +₹117,010 \| +₹56.9 | 356 / 533 / 113 \| +₹174,475 \| +₹2,882 \| +₹114.1 | 243 / 377 / 73 \| +₹133,373 \| +₹6,766 \| +₹132.5 |
| `composite_shared_ATM` | 374 / 511 / 132 \| +₹138,496 \| +₹76.2 | 385 / 492 / 128 \| +₹174,494 \| +₹4,865 \| +₹113.6 | 263 / 340 / 86 \| +₹119,826 \| -₹2,597 \| +₹113.9 |
| `fixed_target_ATM` | 429 / 544 / 100 \| +₹147,657 \| +₹77.6 | 426 / 521 / 101 \| +₹161,228 \| +₹90 \| +₹93.8 | 306 / 354 / 60 \| +₹119,162 \| +₹5,901 \| +₹105.5 |
| `composite_shared_OTM` | 344 / 480 / 135 \| +₹109,600 \| +₹54.3 | 349 / 454 / 141 \| +₹152,499 \| +₹7,964 \| +₹101.5 | 241 / 325 / 97 \| +₹105,437 \| +₹15,044 \| +₹99.0 |
| `fixed_target_ITM` | 442 / 477 / 79 \| +₹134,327 \| +₹74.6 | 438 / 465 / 80 \| +₹152,204 \| +₹6,097 \| +₹94.8 | 315 / 320 / 51 \| +₹112,199 \| +₹12,059 \| +₹103.6 |
| `fast_momentum_ITM` | 298 / 536 / 95 \| +₹132,697 \| +₹82.8 | 313 / 527 / 90 \| +₹142,810 \| -₹17,797 \| +₹93.6 | 215 / 371 / 57 \| +₹113,310 \| -₹4,189 \| +₹116.2 |
| `trend_pullback_OTM` | 370 / 416 / 160 \| +₹129,968 \| +₹77.4 | 366 / 386 / 162 \| +₹138,690 \| +₹1,776 \| +₹91.7 | 252 / 272 / 122 \| +₹112,628 \| -₹1,106 \| +₹114.3 |
| `fast_momentum_OTM` | 314 / 506 / 121 \| +₹108,484 \| +₹55.3 | 331 / 486 / 122 \| +₹137,114 \| -₹4,252 \| +₹86.0 | 227 / 343 / 82 \| +₹107,967 \| +₹3,393 \| +₹105.6 |
| `fixed_target_OTM` | 369 / 543 / 106 \| +₹114,065 \| +₹52.0 | 347 / 511 / 114 \| +₹127,386 \| +₹12,268 \| +₹71.1 | 251 / 352 / 71 \| +₹88,793 \| +₹1,982 \| +₹71.7 |
| `volatility_expansion_ATM` | 254 / 549 / 366 \| +₹151,858 \| +₹69.9 | 257 / 547 / 386 \| +₹199,590 \| +₹10,369 \| +₹107.7 | 188 / 366 / 246 \| +₹173,644 \| +₹7,503 \| +₹157.1 |
| `adaptive_regime_ATM` | 352 / 548 / 114 \| +₹132,828 \| +₹71.0 | 443 / 544 / 101 \| +₹191,782 \| +₹19,078 \| +₹116.3 | 298 / 370 / 57 \| +₹152,714 \| +₹2,410 \| +₹150.6 |
| `trend_pullback_v2_ITM1` | 365 / 446 / 162 \| +₹139,812 \| +₹83.7 | 366 / 450 / 159 \| +₹175,114 \| +₹7,908 \| +₹119.6 | 243 / 314 / 106 \| +₹136,321 \| +₹11,238 \| +₹145.6 |
| `micro_chandelier_ITM1` | 346 / 603 / 298 \| +₹129,630 \| +₹44.0 | 374 / 555 / 262 \| +₹163,019 \| +₹5,310 \| +₹76.9 | 259 / 367 / 175 \| +₹146,372 \| +₹7,974 \| +₹122.7 |
| `opening_thrust_convex_ATM` | 28 / 51 / 8 \| +₹28,695 \| +₹269.8 | 27 / 46 / 6 \| +₹30,024 \| +₹210 \| +₹320.1 | 28 / 44 / 6 \| +₹31,250 \| +₹210 \| **+₹340.6** |
| `delta_scalp_ITM2` | 255 / 717 / 352 \| +₹38,810 \| -₹30.7 | 356 / 648 / 253 \| +₹60,435 \| +₹13,826 \| -₹11.9 | 253 / 421 / 164 \| +₹72,308 \| +₹11,889 \| +₹26.3 |
| `gamma_runner_OTM2` | 183 / 451 / 72 \| -₹14,407 \| -₹80.4 | 185 / 421 / 74 \| -₹27,145 \| +₹991 \| -₹99.9 | 154 / 318 / 57 \| -₹2,472 \| +₹5,073 \| -₹64.7 |
| `structural_convexity_ATM` | 122 / 661 / 40 \| -₹54,003 \| -₹125.6 | 120 / 669 / 45 \| -₹56,204 \| +₹5,344 \| -₹127.4 | 92 / 462 / 25 \| -₹26,265 \| +₹10,530 \| -₹105.4 |

---

## 10. All 30 Experiments Day-by-Day Matrix (`Paper` & `Paper-Delayed`)

> **Design Blueprint for `IMPROVEMENTS.md §2` Batch Simulation Reports:** The multi-dimensional matrix format in Sections **5.1**, **5.2**, **8**, and **10** (Track Consistency Leaderboard + Day×Track Matrix + Regime Grouping + Side-by-Side `Paper` vs `Delayed` Slippage) is the target template for the automated Batch Report UI/API.

| Experiment ID | `Nov 16` (P / D) | `Nov 17` (P / D) | `Nov 18` (P / D) | `Nov 19` (P / D) | `Nov 20` (P / D) | `Nov 23` (P / D) | `Nov 25` (P / D) |
|---|---|---|---|---|---|---|---|
| `H0_BASELINE` | -₹74,753 / -₹84,214 | +₹540,062 / +₹533,256 | +₹248,529 / +₹242,527 | +₹230,611 / +₹204,592 | +₹136,379 / +₹107,179 | -₹57,533 / -₹56,708 | -₹51,796 / -₹118,426 |
| `H1_MIDDAY_FREEZE` | -₹62,582 / -₹63,669 | +₹542,866 / +₹558,120 | +₹96,669 / +₹94,258 | +₹200,910 / +₹189,409 | -₹2,754 / -₹11,670 | -₹69,092 / -₹69,092 | -₹354,933 / -₹372,762 |
| `H2a_MIN_ATR_030` | -₹79,034 / -₹82,847 | +₹391,371 / +₹389,277 | +₹265,262 / +₹268,482 | +₹94,279 / +₹92,098 | +₹25,588 / +₹7,119 | +₹16,471 / +₹17,296 | +₹74,018 / -₹32,778 |
| `H2b_PURE_3BAR_045` | -₹52,830 / -₹61,815 | +₹543,687 / +₹527,712 | +₹301,306 / +₹295,144 | +₹123,606 / +₹109,455 | +₹97,197 / +₹83,382 | +₹25,386 / +₹26,211 | -₹9,738 / -₹73,530 |
| `H2c_BODY_025_ATR_025` | -₹7,804 / -₹12,743 | +₹373,836 / +₹364,408 | +₹238,734 / +₹237,523 | +₹123,536 / +₹109,528 | +₹26,630 / +₹3,685 | +₹13,328 / +₹14,154 | -₹99,222 / -₹150,973 |
| `H3a_FLAG_BUF_15` | -₹74,753 / -₹84,214 | +₹555,264 / +₹536,523 | +₹248,529 / +₹242,527 | +₹230,611 / +₹204,592 | +₹139,553 / +₹106,795 | -₹57,421 / -₹56,596 | -₹45,655 / -₹145,384 |
| `H3b_FLAG_BUF_25` | -₹112,462 / -₹119,637 | +₹547,619 / +₹530,327 | +₹248,529 / +₹242,527 | +₹268,104 / +₹249,394 | +₹146,799 / +₹116,912 | -₹57,421 / -₹56,596 | -₹21,500 / -₹141,611 |
| `H3c_FLAG_FULL_POLE` | -₹81,449 / -₹90,483 | +₹592,142 / +₹569,327 | +₹230,724 / +₹224,077 | +₹280,478 / +₹256,900 | +₹149,507 / +₹115,545 | -₹72,756 / -₹72,756 | -₹170,062 / -₹272,061 |
| `H4a_ELAPSED_15S` | -₹56,939 / -₹66,632 | +₹615,378 / +₹620,282 | +₹263,362 / +₹260,147 | +₹238,455 / +₹221,177 | +₹203,330 / +₹168,768 | -₹68,183 / -₹68,183 | -₹28,398 / -₹120,804 |
| `H4b_ELAPSED_30S` | -₹55,904 / -₹61,358 | +₹463,697 / +₹460,711 | +₹190,444 / +₹187,815 | +₹229,998 / +₹202,545 | +₹182,930 / +₹156,219 | -₹76,178 / -₹76,178 | -₹110,867 / -₹274,566 |
| `H5a_CAP_2` | -₹115,794 / -₹125,536 | +₹329,255 / +₹304,132 | +₹261,853 / +₹257,405 | +₹226,374 / +₹209,920 | +₹108,207 / +₹76,247 | -₹57,533 / -₹56,708 | +₹60,667 / +₹6,256 |
| `H5b_CAP_2_NO_FLIP` | -₹57,842 / -₹67,698 | +₹268,955 / +₹259,875 | +₹283,692 / +₹281,713 | +₹155,497 / +₹141,054 | +₹113,337 / +₹87,360 | -₹57,162 / -₹56,337 | -₹60,458 / -₹108,577 |
| `H5c_CAP_1` | -₹154,868 / -₹158,113 | +₹253,043 / +₹251,033 | +₹224,098 / +₹223,364 | +₹207,175 / +₹191,241 | -₹23,837 / -₹40,692 | -₹71,700 / -₹71,700 | -₹115,518 / -₹182,117 |
| `H6a_OPT_SCALE` | -₹6,042 / -₹10,216 | +₹565,847 / +₹551,506 | +₹317,511 / +₹315,558 | +₹244,390 / +₹232,946 | +₹146,629 / +₹131,263 | -₹54,938 / -₹54,488 | -₹94,777 / -₹194,190 |
| `H6b_OPT_SCALE_SPREAD` | +₹12,048 / +₹5,829 | +₹565,428 / +₹551,184 | +₹317,511 / +₹315,558 | +₹271,006 / +₹260,030 | +₹146,629 / +₹131,263 | -₹54,938 / -₹54,488 | +₹71,960 / -₹27,806 |
| `M1_CORE_CLEAN` | -₹95,442 / -₹97,135 | +₹571,314 / +₹590,978 | +₹110,444 / +₹102,978 | +₹224,393 / +₹215,867 | +₹64,843 / +₹54,323 | -₹80,769 / -₹80,769 | -₹184,298 / -₹272,499 |
| `M2_BALANCED` | -₹68,058 / -₹71,975 | +₹336,926 / +₹348,177 | +₹86,657 / +₹86,117 | +₹136,730 / +₹135,104 | -₹8,068 / -₹11,302 | -₹6,315 / -₹6,315 | +₹2,825 / -₹43,547 |
| `M3_SMART_IMPULSE` | -₹65,507 / -₹69,454 | +₹401,618 / +₹419,821 | +₹143,314 / +₹139,800 | +₹83,394 / +₹81,798 | -₹24,281 / -₹26,467 | -₹6,315 / -₹6,315 | -₹202,505 / -₹225,899 |
| `M4_BALANCED_SCALED` | -₹58,056 / -₹57,888 | +₹322,599 / +₹334,278 | +₹92,588 / +₹93,790 | +₹147,710 / +₹146,262 | -₹38,279 / -₹41,109 | -₹4,695 / -₹4,695 | -₹37,577 / -₹79,663 |
| `M5_IMPULSE_SCALED` | -₹73,186 / -₹73,187 | +₹395,427 / +₹407,601 | +₹158,448 / +₹156,956 | +₹87,123 / +₹85,740 | -₹43,470 / -₹42,352 | -₹4,695 / -₹4,695 | -₹298,206 / -₹309,897 |
| `M6_HIGH_CONVICTION` | -₹45,898 / -₹45,730 | +₹323,972 / +₹332,760 | +₹93,058 / +₹94,260 | +₹83,472 / +₹82,025 | -₹40,132 / -₹39,592 | -₹4,695 / -₹4,695 | -₹35,037 / -₹61,977 |
| `M7_SPREAD_GATE_CORE` | -₹47,611 / -₹45,934 | +₹502,446 / +₹519,979 | +₹84,094 / +₹81,192 | +₹242,342 / +₹234,158 | +₹15,474 / +₹9,979 | -₹81,939 / -₹81,939 | -₹280,152 / -₹359,783 |
| `M8_BODY025_SPREAD_SCALED` | -₹54,680 / -₹55,926 | +₹410,419 / +₹414,599 | +₹76,686 / +₹73,071 | +₹75,670 / +₹74,288 | -₹33,064 / -₹33,249 | -₹4,695 / -₹4,695 | -₹195,902 / -₹254,834 |
| `M9_ALL_SIGNALS_SCALED` | -₹48,228 / -₹46,454 | +₹613,007 / +₹641,480 | +₹126,821 / +₹123,341 | +₹261,836 / +₹255,145 | +₹63,037 / +₹72,892 | -₹81,939 / -₹81,939 | -₹218,902 / -₹285,236 |
| `W1_15S_OPT_SPREAD` | -₹13,845 / -₹18,558 | +₹631,957 / +₹626,436 | +₹344,497 / +₹338,487 | +₹266,571 / +₹253,475 | +₹218,481 / +₹202,276 | -₹67,771 / -₹67,771 | +₹103,974 / +₹42,622 |
| `W2_15S_FLAG25_OPT_SPREAD` | -₹54,535 / -₹60,747 | +₹647,132 / +₹645,676 | +₹344,497 / +₹338,487 | +₹304,092 / +₹289,281 | +₹217,814 / +₹206,663 | -₹67,771 / -₹67,771 | +₹120,218 / +₹69,899 |
| `W3_15S_FLAG25_3BAR045_OPT_SPREAD` | -₹52,123 / -₹57,033 | +₹584,959 / +₹576,139 | +₹409,178 / +₹404,064 | +₹158,829 / +₹144,735 | +₹175,156 / +₹174,686 | +₹18,424 / +₹18,424 | +₹28,429 / -₹7,220 |
| `W4_3BAR045_OPT_SPREAD` | +₹8,675 / +₹3,537 | +₹506,878 / +₹483,200 | +₹371,150 / +₹368,087 | +₹120,787 / +₹106,343 | +₹100,710 / +₹94,760 | +₹31,256 / +₹31,706 | -₹37,962 / -₹98,915 |
| `W5_15S_FLAG25_NOFLIP_OPT_SPREAD` | -₹118,544 / -₹124,438 | +₹509,176 / +₹514,430 | +₹344,624 / +₹345,972 | +₹277,117 / +₹267,581 | +₹175,197 / +₹160,853 | -₹67,400 / -₹67,400 | +₹141,705 / +₹96,514 |
| `W6_BODY025_ATR025_OPT_SPREAD` | -₹21,500 / -₹23,553 | +₹376,158 / +₹368,633 | +₹282,560 / +₹280,142 | +₹126,704 / +₹115,464 | +₹24,847 / +₹8,224 | +₹19,196 / +₹19,646 | +₹47,869 / -₹7,067 |
