# Proposed Execution Tracks Specification: Multi-Regime & Risk-Reward Framework

## 1. Executive Summary & Design Rationale

In options trading, market regimes and time-of-day mechanics dictate the survival and profitability of an execution track. A fixed percentage stop loss (such as $-5\%$ or $-8\%$) on an option contract frequently triggers not because the underlying breakout failed, but because of **synthetic option frictions**: bid-ask spread widening, sudden theta decay during midday consolidation, and delta mismatch.

To build an explainable, robust, and consistent execution portfolio, tracks must be purpose-built for specific **Risk-to-Reward quadrants** and **market regimes**.

```
                                      ▲ REWARD POTENTIAL
                                      │
              LOW RISK / HIGH REWARD  │  HIGH RISK / HIGH REWARD
              ──────────────────────  │  ───────────────────────
             1. Structural Convexity  │  5. Gamma Runner
             2. Opening Thrust Convex │  6. Volatility Expansion
                                      │
              • Underlying-anchored SL│  • OTM2 cheap premium
              • Unlimited run targets │  • Explosive +50% to +120%
              • Win Rate: 38% - 44%   │  • Win Rate: 25% - 32%
              • Payoff: 3.5:1 to 5:1  │  • Payoff: 5:1 to 8:1
    ──────────────────────────────────┼──────────────────────────────────►
                                      │                               RISK
              LOW RISK / LOW REWARD   │  ADAPTIVE REGIME (ALL-WEATHER)
              ─────────────────────   │  ────────────────────────────
              3. Deep ITM Delta Scalp │  7. Adaptive Time-of-Day
              4. Micro-Chandelier Lock│  8. Trend Pullback V2 (ATR)
                                      │
              • Deep ITM (Delta ~0.85)│  • ATM morning, ITM midday
              • Zero theta drag       │  • Spread-gated (<1.5%)
              • +7% Target / -4% SL   │  • Dynamic asymmetric ratchets
              • Win Rate: 68% - 75%   │  • Balanced Sharpe & Max DD
                                      ▼
```

---

## 2. Multi-Quadrant Strategy Tracks

### Quadrant I: Low Risk / High Reward (Asymmetric Convexity)

#### Track 1: `StructuralConvexityTrack` (The Invalidation Free-Roller)
* **Core Philosophy**: Never use a fixed arbitrary option percentage stop. Anchor risk strictly to the **underlying stock's structural support** (flag consolidation low or breakout bar low). If the underlying violates support, cut immediately with minimal option loss. If momentum follows through, remove all upside caps and trail with a wide profit floor.
* **Moneyness**: **ATM** ($\Delta \approx 0.50$).
* **Friction Gate**: Reject contracts where $\frac{\text{Ask} - \text{Bid}}{\text{Ask}} > 0.015$ (max $1.5\%$ spread).
* **Risk & Exit Logic**:
  1. **Structural Invalidation (Primary SL)**:
     $$\text{Exit Condition: } \text{LTP}_{\text{underlying}} \le \text{flag\_low}$$
     Because the flag consolidation range is typically $0.5\% - 0.8\%$ of underlying price, a violation of `flag_low` translates to only a $-4\%$ to $-6\%$ option loss.
  2. **Catastrophic Emergency SL**: Hard $-12\%$ option stop to guard against gap-downs.
  3. **Stage 1 (Breakeven Free-Roll)**: When option gain reaches $+8\%$, ratchet Stop Loss to $\text{Entry Price} + \text{brokerage buffer}$ ($+0.5\%$). Risk is completely eliminated.
  4. **Stage 2 (Asymmetric 50% Profit Trail)**: Once $+15\%$ profit is achieved, trail stop loss locking in $50\%$ of peak profit:
     $$\text{Stop Loss} = \text{Entry Price} \times \left(1.0 + 0.50 \times \text{Peak Profit Pct}\right)$$
  5. **No Time Timeout**: Runners are allowed to compound as long as the underlying makes consecutive higher lows.
* **Expected Metrics**: Win Rate: $38\% - 44\%$, Payoff Ratio ($R:R$): $3.5:1 \text{ to } 5:1$, Max Drawdown: Low.

---

#### Track 2: `OpeningThrustConvexityTrack` (First-Hour Breakout Surfer)
* **Core Philosophy**: Capture the highest-velocity institutional thrust of the day. Only operates between **09:15 and 10:30 IST**, when momentum follow-through is highest and IV expansion benefits long option buyers.
* **Moneyness**: **ATM** ($\Delta \approx 0.50 - 0.55$).
* **Entry Filter**:
  - Time window: $09:15 \le \text{time} \le 10:30$ IST.
  - Institutional Thrust: $rVOL \ge 2.5x$, $\text{CLV} \ge 0.80$.
* **Risk & Exit Logic**:
  1. **Stop Loss**: $-6\%$ option SL.
  2. **Scale-Out Profit Taking**:
     - At $+15\%$: Lock breakeven on full position.
     - Target: Open-ended $+30\%$ to $+50\%$ with stepped $1\%$ trail for every $1\%$ price advance above $+15\%$.
  3. **Morning Cutoff**: Auto square-off at 10:45 IST to avoid midday chop.
* **Expected Metrics**: Win Rate: $45\% - 52\%$, Payoff Ratio: $3:1 \text{ to } 4:1$.

---

### Quadrant II: Low Risk / Low Reward (High-Probability Steady Compounding)

#### Track 3: `DeltaScalpTrack` (Deep ITM Intrinsic Flow)
* **Core Philosophy**: Completely eliminate Theta decay, Vega crush, and bid-ask slippage by trading **Deep In-The-Money (ITM2)** contracts.
* **Moneyness**: **ITM2** ($\Delta \approx 0.80 - 0.90$). Extrinsic time value is $< 5\%$; the option moves tick-for-tick with underlying equity.
* **Entry Gate**: $rVOL \ge 2.0x$, $\text{CLV} \ge 0.75$.
* **Risk & Exit Logic**:
  1. **Target**: $+6\%$ to $+8\%$ on option premium (corresponds to a modest $+0.4\% - 0.6\%$ push in underlying equity).
  2. **Stop Loss**: $-4\%$ hard stop on option premium.
  3. **Velocity Timeout**: 180 seconds (3 minutes). If the underlying does not advance within 3 minutes, exit at market. Because time value is negligible, early exit incurs almost zero theta loss.
* **Expected Metrics**: Win Rate: $68\% - 75\%$, Payoff Ratio: $1.4:1 \text{ to } 1.6:1$. Very steady equity curve, immune to midday chop.

---

#### Track 4: `MicroChandelierTrack` (High-Frequency Profit Locking)
* **Core Philosophy**: High-probability scalp that locks profits immediately as price ticks in favor of the position. Ideal for range-bound or rotational market days.
* **Moneyness**: **ITM1 / ATM** ($\Delta \approx 0.60 - 0.70$).
* **Risk & Exit Logic**:
  1. **Target**: $+10\%$ on option premium.
  2. **Initial Stop Loss**: $-5\%$ on option premium.
  3. **Micro-Trailing**:
     - Once $+3\%$ profit is reached: move SL to Entry.
     - Once $+5\%$ profit is reached: move SL to $+3\%$.
     - Once $+7\%$ profit is reached: move SL to $+5\%$.
     - Once $+9\%$ profit is reached: move SL to $+7\%$.
  4. **Stagnation**: 5-minute timeout if profit $< +2\%$.
* **Expected Metrics**: Win Rate: $62\% - 70\%$, Payoff Ratio: $1.2:1 \text{ to } 1.5:1$.

---

### Quadrant III: High Risk / High Reward (Explosive Gamma & Runaway Momentum)

#### Track 5: `GammaRunnerTrack` (Explosive OTM Convexity)
* **Core Philosophy**: Target outsized asymmetric moves on explosive multi-day breakout leaders. Uses cheap Out-of-the-Money options where Gamma expansion produces $+50\%$ to $+120\%$ returns on strong trending days.
* **Moneyness**: **OTM1 / OTM2** ($\Delta \approx 0.28 - 0.38$). Small capital outlay per lot.
* **Entry Filter**:
  - Ultra-Selective: $rVOL \ge 3.0x$, $\text{CLV} \ge 0.85$.
  - Underlying breaking to a 3-day high or breaking a multi-day flag.
* **Risk & Exit Logic**:
  1. **Stop Loss**: $-15\%$ to $-18\%$ option SL (wider percentage buffer to withstand OTM delta fluctuations, but smaller absolute INR risk due to low premium).
  2. **Target**: $+50\%$ to $+100\%$.
  3. **Trailing Ratchets**:
     - At $+25\%$ profit: Move SL to Breakeven ($+1\%$).
     - At $+50\%$ profit: Move SL to $+25\%$.
     - At $+75\%$ profit: Move SL to $+50\%$.
* **Expected Metrics**: Win Rate: $25\% - 32\%$, Payoff Ratio: $5:1 \text{ to } 8:1$. High volatility in daily returns, explosive upside on trending days.

---

#### Track 6: `VolatilityExpansionTrack` (Vega Thrust Rider)
* **Core Philosophy**: Ride simultaneous price expansion and implied volatility (IV) expansion during high-momentum market openings and news/earnings breakouts.
* **Moneyness**: **ATM** ($\Delta \approx 0.50$).
* **Entry Filter**:
  - Current candle volume rate $ROV \ge 3.5 \times \text{average consolidation ROV}$.
  - Candle range $\ge 1.5 \times ATR$.
* **Risk & Exit Logic**:
  1. **Target**: $+25\%$ option gain.
  2. **Stop Loss**: $-8\%$ option SL.
  3. **Momentum Deceleration Exit**: If candle close rate slows down for 2 consecutive 1-minute intervals after entry, exit immediately at market to avoid IV crush.
* **Expected Metrics**: Win Rate: $40\% - 46\%$, Payoff Ratio: $3:1 \text{ to } 4:1$.

---

### Quadrant IV: Adaptive & Hybrid Consistency Tracks (All-Weather)

#### Track 7: `AdaptiveRegimeTrack` (Time-and-Volatility Switched)
* **Core Philosophy**: Seamlessly adapt contract moneyness, risk limits, and holding durations based on the prevailing time-of-day regime:
  - **Phase 1: Morning Thrust (09:15 - 10:30 IST)**: High velocity $\rightarrow$ **ATM**, Target $+18\%$, SL $-7\%$, Breakeven at $+7\%$.
  - **Phase 2: Midday Grind (10:30 - 13:30 IST)**: Low velocity $\rightarrow$ **ITM1** (Theta-Shielded), Target $+8\%$, SL $-5\%$, 10-minute timeout.
  - **Phase 3: Afternoon Continuation (13:30 - 15:00 IST)**: Institutional positioning $\rightarrow$ **ATM**, 50% peak-profit trailing stop.
* **Friction Gate**: Strict rejection if $(\text{Ask} - \text{Bid}) / \text{Ask} > 0.015$.
* **Expected Metrics**: Win Rate: $52\% - 60\%$, Payoff Ratio: $2.2:1 \text{ to } 2.8:1$. Balanced Sharpe ratio with stable daily compounding.

---

#### Track 8: `TrendPullbackV2Track` (ATR-Calibrated Stepped Trailer)
* **Core Philosophy**: Refactor the existing `trend_pullback` track to eliminate the synthetic $-5\%$ stop-loss friction. Calibrate initial stop loss to the actual bid-ask spread plus volatility buffer, and trail dynamically as price advances.
* **Moneyness**: **ITM1** ($\Delta \approx 0.70 - 0.75$).
* **Risk & Exit Logic**:
  1. **Dynamic Stop Loss**:
     $$\text{SL Pct} = \max\left(0.06, 1.5 \times \text{Spread Pct} + 0.03\right)$$
     Guarantees the bid-ask spread cannot immediately trigger an invalid stop-out.
  2. **Breakeven Activation**: At $+4\%$ profit, lock in Breakeven.
  3. **Stepped Trailing**: For every $+1\%$ gain above $+4\%$, trail stop loss upward by $+0.8\%$.
  4. **Max Target**: $+25\%$ on Ask.
* **Expected Metrics**: Win Rate: $48\% - 55\%$, Payoff Ratio: $2.5:1 \text{ to } 3.5:1$.

---

## 3. Comprehensive Comparison Matrix

| Track Name | Quadrant | Moneyness | Initial SL | Target | Breakeven Lock | Trailing Rule | Max Timeout | Target Win % | Target R:R |
| :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- |
| **`StructuralConvexity`** | Low Risk / High Reward | ATM ($\Delta 0.50$) | Underlying Support / -10% | Open-Ended | At $+8\%$ | 50% of Peak Profit | None | 40% - 44% | 4:1 to 5:1 |
| **`OpeningThrustConvex`** | Low Risk / High Reward | ATM ($\Delta 0.50$) | -6% | +35% to +50% | At $+15\%$ | 1% step per 1% advance | 10:45 IST | 45% - 52% | 3:1 to 4:1 |
| **`DeltaScalp`** | Low Risk / Low Reward | ITM2 ($\Delta 0.85$) | -4% | +7% | None | Fixed Target | 180 sec | 70% - 75% | 1.5:1 |
| **`MicroChandelier`** | Low Risk / Low Reward | ITM1 ($\Delta 0.70$) | -5% | +10% | At $+3\%$ | Stepped +1% trail | 300 sec | 65% - 70% | 1.4:1 |
| **`GammaRunner`** | High Risk / High Reward | OTM1/2 ($\Delta 0.30$) | -15% | +60% to +100% | At $+25\%$ | Tiered +25% locks | None | 26% - 32% | 6:1 to 8:1 |
| **`VolatilityExpansion`**| High Risk / High Reward | ATM ($\Delta 0.50$) | -8% | +25% | At $+10\%$ | Momentum Deceleration | 300 sec | 42% - 46% | 3:1 |
| **`AdaptiveRegime`** | All-Weather Adaptive | Time-Switched | -5% to -7% | +8% to +18% | At $+7\%$ | 50% Peak Trail (PM) | 600 sec (Midday)| 54% - 60% | 2.5:1 |
| **`TrendPullbackV2`** | Volatility-Calibrated | ITM1 ($\Delta 0.72$) | Spread-Gated (-6%) | +25% | At $+4\%$ | Stepped +0.8% trail | 3600 sec | 50% - 55% | 3:1 |

---

## 4. Implementation & Simulation Strategy

1. **Factory Integration**: Add the new track classes to [`common/tracks/factory.py`](file:///Users/yorha/Documents/Trading%20Stuff/common/tracks/factory.py) so they can be selected individually or composed in [`config/strategies.json`](file:///Users/yorha/Documents/Trading%20Stuff/config/strategies.json).
2. **Simulation Matrix Verification**: Run parallel multi-track simulations across recorded trading days (`2026-11-17`, `2026-11-18`, `2026-11-19`) to evaluate their Sharpe ratio, win rate, and drawdown profiles side-by-side.
3. **Execution Routing**: For live deployment, allocate capital dynamically:
   - 40% Capital to **`StructuralConvexity`** (Asymmetric Compounder)
   - 35% Capital to **`AdaptiveRegime`** (All-Weather Consistency)
   - 25% Capital to **`DeltaScalp`** (High-Win-Rate Low-Drawdown Stabilizer)
