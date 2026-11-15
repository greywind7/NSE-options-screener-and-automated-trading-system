# 🔍 Comprehensive Self-Audit & Code Reconnaissance Report

**Audit Date**: 14 November 2026  
**System**: Upstox / Groww Algorithmic Trading Infrastructure v2.0  
**Scope**: Code Verification of User Review Items, Concurrency/Simulation Deep-Dive, Exhaustive Search of System-Wide Redundancies, Gaps & Misses, and Cross-Audit of `SYSTEM_AUDIT.md`  
**Purpose**: Reconnaissance & Scoping Only (Zero code modifications; distinguishing verified genuine architectural flaws from false alarms)

---

## Table of Contents

1. [Executive Reconnaissance Summary](#1-executive-reconnaissance-summary)
2. [Section 1: The `paper_delayed` Concurrency & Simulation Discrepancy](#2-section-1-the-paper_delayed-concurrency--simulation-discrepancy)
   - [Live Execution: Thread Contention & Ghost Prices](#live-execution-thread-contention--ghost-prices)
   - [Simulation Engine: Why Divergence Occurs Without Threads](#simulation-engine-why-divergence-occurs-without-threads)
   - [The Butterfly Effect: Why Paper and Delayed Become Two Different Strats](#the-butterfly-effect-why-paper-and-delayed-become-two-different-strats)
3. [Section 2: Exhaustive Audit of System-Wide Redundancies, Contradictions & Gaps](#3-section-2-exhaustive-audit-of-system-wide-redundancies-contradictions--gaps)
   - [Gap 1: Order Dispatch & Multi-Mode Asymmetry (`_dispatch_execution_exits`)](#gap-1-order-dispatch--multi-mode-asymmetry-_dispatch_execution_exits)
   - [Gap 2: Head-of-Line Blocking in Actionable Setup Pool](#gap-2-head-of-line-blocking-in-actionable-setup-pool)
   - [Gap 3: Dual Trade Logging & Journal Redundancy](#gap-3-dual-trade-logging--journal-redundancy)
   - [Gap 4: Market Hours & EOD Precedence Conflicts (14 Overrides)](#gap-4-market-hours--eod-precedence-conflicts-14-overrides)
   - [Gap 5: Freeze Mode Bypasses & Disk Polling Redundancy](#gap-5-freeze-mode-bypasses--disk-polling-redundancy)
   - [Gap 6: Dynamic Trio Leaderboard Discarding Allocator Math](#gap-6-dynamic-trio-leaderboard-discarding-allocator-math)
   - [Gap 7: Key Normalization Proliferation (33 Duplicate Replaces)](#gap-7-key-normalization-proliferation-33-duplicate-replaces)
   - [Gap 8: Option Resolver File Reload Loop on Failure](#gap-8-option-resolver-file-reload-loop-on-failure)
   - [Gap 9: Runner CLI Parser Duplication & Legacy Test Coupling](#gap-9-runner-cli-parser-duplication--legacy-test-coupling)
4. [Section 3: Scoring Engine 2.0 Structural Flaws & Market Regime Biases](#4-section-3-scoring-engine-20-structural-flaws--market-regime-biases)
5. [Section 4: Option Resolving, Liquidity Gates & OTM Track Starvation](#5-section-4-option-resolving-liquidity-gates--otm-track-starvation)
6. [Section 5: Verification of `SYSTEM_AUDIT.md` (Genuine vs False Alarms)](#6-section-5-verification-of-system_auditmd-genuine-vs-false-alarms)
7. [Section 6: Comprehensive Defect & Reconnaissance Registry](#7-section-6-comprehensive-defect--reconnaissance-registry)

---

## 1. Executive Reconnaissance Summary

Following a deep forensic scan across all 70 source files in `strategies/`, `execution/`, `infrastructure/`, `simulation/`, and `config/`, we have identified that:

1. **Live Sim vs Offline Sim Discrepancy**: Live paper-delayed and offline simulation exhibit the exact same divergence symptoms for **wildly different structural reasons**:
   * **In Live**: Real OS `threading.Timer` threads cause severe lock contention, thread-timer delays, quote-polling sleeps, and timestamp desynchronization ("ghost prices").
   * **In Offline Sim**: No OS threads exist, but **three silent bugs** break simulation accuracy:
     1. `_rebuild_held_keys()` is never called when queuing pending orders, causing option ticks to bypass exit evaluations completely.
     2. Coarse timestamp lumping fills orders at whatever price exists when the *next underlying stock ticks*, completely skipping intermediate option ticks.
     3. Tighter, chased stop-losses cause early stop-outs that permanently desynchronize cash and capital allocation.
2. **System-Wide Gaps & Redundancies**: Beyond the initial review, we discovered **9 major structural gaps**:
   * **Exit Dispatch Blindspot**: Live exits are completely ignored or misrouted if multiple modes run together.
   * **Head-of-Line Blocking**: Actionable pool gets stuck on unexecutable setups, blocking all subsequent candidates.
   * **Dual Logging**: Both `MultiTrackPortfolio` and execution adapters write duplicate entries to `paper_trades.csv`.
   * **14 Conflicting EOD Definitions**: Live EOD square-off (15:15) is silently overridden by global defaults (15:20).
   * **Dynamic Trio Math Bypass**: Hardcoded static dictionaries discard all rolling lookbacks whenever Nifty trends.

---

## 2. Section 1: The `paper_delayed` Concurrency & Simulation Discrepancy

### Live Execution: Thread Contention & Ghost Prices
In live mode ([`execution/paper/paper_delayed_adapter.py`](file:///Users/yorha/Documents/Trading%20Stuff/execution/paper/paper_delayed_adapter.py)), the single-threaded event loop interacts with `threading.Timer` background threads:

```
[Main Event Loop: Single-Threaded]                   [Background Worker: threading.Timer]
  │                                                    │
  ├─ 1. place_order() acquires self._lock             │
  │     Spawns Timer(delay_sec, _execute_fill) ───────┼──► OS Timer counts down
  │     Releases self._lock                           │
  │                                                    │
  │                                                    ├─ 2. Timer fires: _execute_fill()
  │                                                    │     now_ts = time.time()  [OLD TIMESTAMP]
  │                                                    │     Acquires self._lock
  │                                                    │
  │                                                    ├─ 3. Quote Retry Loop (Lines 333-350):
  │                                                    │     time.sleep(0.15) x up to 5 times (750ms!)
  │                                                    │
  ├─ 4. Real Market Tick Arrives                       │
  │     Main thread blocked on GIL/Lock! ──────────────┼──► Tick processing delayed
  │                                                    │
  │                                                    ├─ 4. Quote arrives after 750ms:
  │                                                    │     fill_price = ask  [NEW PRICE]
  │                                                    │     Stamps "filled_at": now_ts [OLD TIMESTAMP]
  │                                                    │     Stamps "fill_price": fill_price [NEW PRICE]
  ▼                                                    ▼     Releases self._lock
```

* **Ghost Prices Verified**: `now_ts` is captured at line 310 *before* the 5-attempt retry sleep loop (up to 750ms). When the fresh quote finally arrives, it stamps the *newest* price with the *oldest* timestamp in the trade log.

---

### Simulation Engine: Why Divergence Occurs Without Threads

In offline simulation ([`simulation/simulation_engine.py`](file:///Users/yorha/Documents/Trading%20Stuff/simulation/simulation_engine.py)), there are **zero OS threads**. The simulation runs single-threaded using `SimulationDummyAdapter`. Yet paper and paper-delayed diverge massively due to three specific bugs in `portfolio.py`:

#### Bug A: Missing `_rebuild_held_keys` on Pending Orders
* In [`strategies/momentum/portfolio.py` lines 1280–1296](file:///Users/yorha/Documents/Trading%20Stuff/strategies/momentum/portfolio.py#L1280-L1296):
  ```python
  if self.min_delay_ms > 0:
      self.pending_orders.append({
          "symbol": symbol, "contract": contract, "target_fill_ts": now_ts + (delay_ms / 1000.0), ...
      })
      return []
  ```
* Notice that `self._rebuild_held_keys()` is **not called**.
* In [`strategies/momentum/strategy.py` lines 740–746](file:///Users/yorha/Documents/Trading%20Stuff/strategies/momentum/strategy.py#L740-L746):
  ```python
  if self._is_option_contract(key, event, display_name):
      for m_name, port in self.portfolios.items():
          if port.is_instrument_held(key, display_name):
              closed_trades = port.update_positions(self.quotes_cache, now_ts=now_ts)
      return
  ```
* Because `_active_held_keys` was not rebuilt, `port.is_instrument_held(key)` evaluates to **`False`**!
* **The Consequence**: When subsequent ticks arrive for that exact option during the delay window, `update_positions` **is completely bypassed**. The pending order **never matures on its own option's ticks**!

#### Bug B: Coarse Lumping & Price Skipping
* Because option ticks don't trigger `update_positions`, the pending order sits idle until some **unrelated underlying equity ticks** (line 952) or the 1-second timer fires.
* If an order is set to mature at `09:30:00.300`, but the next equity tick is `TATASTEEL` at `09:30:00.850`, `now_ts >= target_fill_ts` finally evaluates to True.
* It fills at whatever price happens to be in `quotes_cache` at `09:30:00.850`, **completely missing all the real ticks that occurred between 300ms and 850ms**.

---

### The Butterfly Effect: Why Paper and Delayed Become Two Different Strats

Even with identical market inputs, a 300ms delay permanently breaks correlation between the two portfolios within 60 minutes:

```
                            Breakout Triggered @ 09:20:00
                                          │
            ┌─────────────────────────────┴─────────────────────────────┐
            ▼                                                           ▼
       PAPER (Instant)                                         PAPER-DELAYED (300ms)
    Fills instantly @ ₹100.00                                   Fills after pop @ ₹106.00
    SL placed @ ₹92.50 (7.5% below 100)                         SL placed @ ₹98.05 (7.5% below 106!)
            │                                                           │
            │ Price dips to ₹97.00 on retest                            │ Price dips to ₹97.00 on retest
            ▼                                                           ▼
    SURVIVES RETEST! (97.00 > 92.50)                            STOPPED OUT FOR LOSS! (97.00 < 98.05)
            │                                                           │
            │ Rides rally to ₹130 (+30% Win)                            │ Cash is freed up at 09:21
            ▼                                                           ▼
    Capital locked in winning Stock A                           Enters Stock B at 09:22
            │                                                           │
            └─────────────────────────────┬─────────────────────────────┘
                                          ▼
                         MIDDAY CASCADE (11:00 AM):
       • Portfolios hold completely different positions
       • Cash balances are completely desynchronized
       • Dynamic Trio ranks different champions for each
       • Literally executing two completely different strategies!
```

---

## 3. Section 2: Exhaustive Audit of System-Wide Redundancies, Contradictions & Gaps

### Gap 1: Order Dispatch & Multi-Mode Asymmetry (`_dispatch_execution_exits`)
* **Files**: [`strategies/momentum/strategy.py` lines 692, 743, 954, 1047, 1106, 1112](file:///Users/yorha/Documents/Trading%20Stuff/strategies/momentum/strategy.py#L692)
* **Code**:
  ```python
  if m_name == "paper" or (m_name == list(self.portfolios.keys())[0]):
      self._dispatch_execution_exits(closed_trades)
      self._dispatch_stop_loss_updates(port)
  ```
* **The Fatal Flaw**:
  1. If running `--mode paper,live` or `--mode paper_delayed,live`, `list(self.portfolios.keys())[0]` is `"paper"`.
  2. When the **live portfolio** closes a position, `m_name == "live"` evaluates to `False`. **The live exit is completely dropped!** No sell order is ever transmitted to the broker, abandoning real money positions!
  3. When the **paper portfolio** closes a trade, `_dispatch_execution_exits` broadcasts `place_order(transaction_type="SELL")` across **all** adapters in `multi_executor`. It issues a broker sell order for live even if live held a different quantity or strike!

---

### Gap 2: Head-of-Line Blocking in Actionable Setup Pool
* **File**: [`strategies/momentum/strategy.py` lines 944–950](file:///Users/yorha/Documents/Trading%20Stuff/strategies/momentum/strategy.py#L944-L950)
* **Code**:
  ```python
  sorted_candidates = self.actionable_pool.get_sorted_candidates(now_ts)
  if sorted_candidates:
      top_cand = sorted_candidates[0]
      trade_opened = self._execute_candidate(top_cand, now_ts)
      if trade_opened:
          self.actionable_pool.remove(top_cand["key"])
  ```
* **The Fatal Flaw**:
  If `top_cand` fails to open a trade (e.g. symbol already held, insufficient cash, option spread $>3\%$, or missing option strike), `trade_opened` returns `False`.
  * `top_cand` is **not removed** from the pool!
  * On the next 10,000 ticks across the entire market, `top_cand` remains at index 0.
  * **Head-of-Line Blocking**: Candidates #2, #3, etc., behind it are **never evaluated or traded** until `top_cand` reaches its 20-second expiration.
  * Burns massive CPU repeatedly re-resolving strikes and checking quotes on every single market tick.

---

### Gap 3: Dual Trade Logging & Journal Redundancy
* **Files**: [`strategies/momentum/portfolio.py` line 1447](file:///Users/yorha/Documents/Trading%20Stuff/strategies/momentum/portfolio.py#L1447) and [`execution/paper/paper_adapter.py` line 379](file:///Users/yorha/Documents/Trading%20Stuff/execution/paper/paper_adapter.py#L379)
* Both `MultiTrackPortfolio._log_trade_csv` AND `PaperExecutionAdapter._log_trade_journal` append closed trades to the exact same file path (`recordings/{date}/paper_trades.csv`).
* Both components also make independent calls to `get_global_pnl_aggregator().record_trade_exit()`, leading to duplicate event recording.

---

### Gap 4: Market Hours & EOD Precedence Conflicts (14 Overrides)
* Market hours and EOD square-off times are independently defined in **14 different locations**:
  - `config/strategies.json` (`15:20`)
  - `config/execution.json` global (`15:20`) vs live (`15:15`)
  - `config/strategies_sim.json` (`15:20` / `15:00` freeze)
  - Signatures in `runner.py`, `strategy.py`, `risk.py`, `portfolio.py`, `multi_executor.py`, `paper_adapter.py`, `live_adapter.py`, `simulation_engine.py`.
* **The Live Override Gap**:
  In [`execution/multi_executor.py` line 478](file:///Users/yorha/Documents/Trading%20Stuff/execution/multi_executor.py#L478):
  `e_sq = normalize_time_str(exec_cfg.get("eod_square_off_time", "15:20"))`
  It reads the global `15:20` and passes it to `LiveExecutionAdapter`, silently overriding the `15:15` defined under `execution.live`. Live trading misses its 15:15 pre-close buffer!

---

### Gap 5: Freeze Mode Bypasses & Disk Polling Redundancy
* In [`strategies/momentum/strategy.py` line 1014](file:///Users/yorha/Documents/Trading%20Stuff/strategies/momentum/strategy.py#L1014):
  `self.multi_executor.is_frozen(self.mode)`
  When `self.mode = "paper,paper_delayed"`, it checks exact string match for `"paper,paper_delayed"`, which fails.
* In [`strategies/momentum/portfolio.py` line 830](file:///Users/yorha/Documents/Trading%20Stuff/strategies/momentum/portfolio.py#L830):
  `os.path.exists(os.path.join(self.freeze_dir, f".freeze_{self.mode}"))`
  Looks for `.freeze_paper,paper_delayed` on disk, bypassing single-mode freeze files.
* **Redundancy**: 7 different functions poll `os.path.exists("logs/.freeze_*")` repeatedly on every tick.

---

### Gap 6: Dynamic Trio Leaderboard Discarding Allocator Math
* **File**: [`strategies/momentum/portfolio.py` lines 643–662](file:///Users/yorha/Documents/Trading%20Stuff/strategies/momentum/portfolio.py#L643-L662)
* The configuration in `config/strategies.json` sets sophisticated parameters: 7-day lookback, 10-minute rolling window, 50/50 ROI/Winrate blend, proportional weights, and fixed-cap blends.
* **The Bypass**: If Nifty changes by $\ge 0.35\%$, lines 643–662 **completely bypass all of that math** and force hardcoded champions:
  `self.dynamic_trio_champions = ["TRAIL_SL_ITM", "TRAIL_SL_ATM", "ITM"]` with static weights `[0.50, 0.30, 0.20]`, immediately returning!
* **Empty Tracks Bug**: When running `--track DYNAMIC_TRIO` by itself, constituent tracks are not instantiated in `self.tracks`, causing the leaderboard to evaluate to an empty list `[]` and fall back to equal weight `1/3`.

---

### Gap 7: Key Normalization Proliferation (33 Duplicate Replaces)
* Across 8 files in `strategies/`, `execution/`, and `simulation/`, there are **33 separate instances** of:
  `ck.replace(":", "|")` and `ck.replace("|", ":")`.
* Because instrument tokens are not sanitized at ingestion, every single quote cache lookup performs 3–5 string mutations and dictionary hash probes.

---

### Gap 8: Option Resolver File Reload Loop on Failure
* **File**: [`strategies/momentum/option_selector.py` lines 60–120](file:///Users/yorha/Documents/Trading%20Stuff/strategies/momentum/option_selector.py#L60-L120)
* If `manifest_path` is missing or fails to load, `self._loaded` remains `False`.
* On every call to `resolve_strike()` on every tick, it re-attempts to open and parse the JSON and CSV files from disk over and over.

---

### Gap 9: Runner CLI Parser Duplication & Legacy Test Coupling
* **File**: [`runner.py` lines 978–1022](file:///Users/yorha/Documents/Trading%20Stuff/runner.py#L978-L1022)
* 44 identical lines of argument parser code are copy-pasted between `strat` and `start`.
* [`runner.py` lines 653–663](file:///Users/yorha/Documents/Trading%20Stuff/runner.py#L653-L663) runs tests by calling `subprocess.run` inside `legacy/tests/test_breakout_features.py`, hard-coupling the runner to a legacy directory.

---

## 4. Section 3: Scoring Engine 2.0 Structural Flaws & Market Regime Biases

1. **The 65-Point Counter-Trend Veto**:
   In [`scoring.py` line 330](file:///Users/yorha/Documents/Trading%20Stuff/strategies/momentum/scoring.py#L330), when Nifty is up $+0.25\%$, counter-trend setups are clamped: `raw_bear = min(65.0, raw_bear - 15.0)`. Because `minimum_trade_score = 70`, **every short setup is mathematically prohibited from executing**, regardless of volume or individual stock breakdowns.
2. **Artificial Dud Inflation**:
   When Nifty is green, marginal long setups receive `raw_bull += 5.0` and `abs_score += 10.0`, artificially inflating low-conviction setups past 70, leading to stop-outs.
3. **Continuous Score vs Qualitative Market State**:
   Attempting to optimize a single continuous numerical score ($S \in [0, 100]$) across both trending and choppy regimes fails fundamentally because a score of 70 represents different market realities during trend days versus chop days.

---

## 5. Section 4: Option Resolving, Liquidity Gates & OTM Track Starvation

1. **Option Resolving Is Strike-Offset Based (User Misunderstanding / False Alarm)**:
   [`option_selector.py` line 230](file:///Users/yorha/Documents/Trading%20Stuff/strategies/momentum/option_selector.py#L230) already uses strike offset (`min(abs(strike - spot))` and `atm_idx ± 1`). The delta fields in `config/strategies.json` (`delta_atm: 0.50`, etc.) are dead, phantom keys.
2. **Why OTM Tracks Rarely Trade**:
   In [`risk.py` line 200](file:///Users/yorha/Documents/Trading%20Stuff/strategies/momentum/risk.py#L200), `max_spread_pct: 3.0%` is enforced. On low-premium OTM options (e.g. ₹3.00), a minimum 2-tick spread (₹3.00 / ₹3.10) is $3.28\% > 3.0\%$, triggering an immediate reject. Low-priced options cannot mathematically satisfy a 3% percentage spread gate.

---

## 6. Section 5: Verification of `SYSTEM_AUDIT.md` (Genuine vs False Alarms)

| Audit ID | Claim | Verification Result | Status | Technical Explanation |
| :--- | :--- | :--- | :---: | :--- |
| **CRIT-001** | Missing `Tuple` import in `runner.py` crashes `post-market` | **Verified Genuine** | 🔴 **CRITICAL** | `runner.py` line 39 omits `Tuple`. Line 527 executes `List[Tuple[str, str, str]]`, crashing on `runner.py post-market`. |
| **CRIT-002** | No `.gitignore` — API keys & data at risk | **False Alarm** (Current State) | 🟢 **RESOLVED** | `.gitignore` exists and contains `.env`, `data/*`, `logs/*`, `recordings/*`. Git repository is active. |
| **CRIT-003** | Hardcoded IPC Auth Key in `system.json` | **Verified Genuine** | 🟡 **MODERATE** | `"authkey": "upstox_algo_shared_secret"` is plaintext in `config/system.json`. |
| **CRIT-004** | Silent Exception Swallowing (`except Exception: pass`) | **Verified Genuine** | 🔴 **CRITICAL** | 122 instances of bare `except Exception: pass` exist across execution and strategy pipelines. |
| **CRIT-005** | `cmd_stop` only removes IPC socket, doesn't kill processes | **Verified Genuine** | 🔴 **CRITICAL** | `runner.py` lines 628–640 only calls `os.remove(ipc_addr)`. Running listener and strategy runner processes are never terminated, leaving zombie background processes. |
| **CRIT-006** | Simulation Engine hardcodes NSE hours (ignores MCX) | **Verified Genuine** | 🟠 **HIGH** | `simulation_engine.py` line 343 passes `eod_square_off_time="15:20"` unconditionally, ignoring MCX 23:30 close time. |
| **CRIT-007** | Race condition in `PaperDelayedAdapter` threading | **Verified Genuine** | 🔴 **CRITICAL** | Detailed in Section 1. Background timers mutate state across threads with lock contention. |
| **CRIT-008** | Live Adapter `time.sleep(1.0)` blocks main thread during entry | **Partially False / Overstated** | 🟡 **LOW** | `time.sleep(1.0)` is located in `_reconcile_timeout_order` (timeout recovery handler), NOT on the normal order entry hot path. |
| **HIGH-001** | 18 missing `__init__.py` files | **Verified Genuine** | 🟡 **MODERATE** | `strategies/`, `strategies/momentum/`, `execution/` lack `__init__.py`. Relies on PEP 420; breaks mypy and packaging. |
| **HIGH-004** | `cmd_test` hardcodes legacy test path | **Verified Genuine** | 🟡 **MODERATE** | `runner.py` line 653 hardcodes running `tests/test_breakout_features.py` from `legacy/` via `subprocess`. |
| **HIGH-007** | `ReplayEngine` overwrites `received_timestamp` with wall clock | **Verified Genuine** | 🔴 **CRITICAL** | `replay_engine.py` line 163 sets `received_timestamp=time.time()`, destroying historical tick timestamps and breaking time gates on replay. |
| **HIGH-008** | No circuit breaker for live execution daily loss limit | **Verified Genuine** | 🟠 **HIGH** | `kill_switch_loss_pct: 5.0` in `config/execution.json` is a phantom key. Zero code in Python checks or enforces daily loss cutoffs. |
| **HIGH-009** | Portfolio `_log_trade_csv` silent failure | **False Alarm** | 🟢 **INVALID** | `_log_trade_csv` in `portfolio.py` (line 1635) has no `try/except: pass`. Auditor confused line 332 (`atexit.register` test cleanup) with trade logging. |
| **HIGH-010** | Stale quotes cache without TTL in portfolio MTM | **Verified Genuine** | 🟡 **MODERATE** | `compute_mtm()` uses cached prices without verifying quote age, while entry gates enforce 30s freshness. |
| **MED-003** | Config loading fallback chain is fragile | **Verified Genuine** | 🟡 **MODERATE** | 10-candidate cascaded fallback chains in `runner.py` and `strategy.py` make configuration fragile and typo-prone. |
| **MED-010** | Unprotected dict iteration in `simulation_engine.py` | **False Alarm** | 🟢 **INVALID** | Line 553 uses `list(acct.open_positions.items())`. Simulation is completely single-threaded; no concurrent mutation occurs. |
| **MED-011** | Paper adapter fill price can be 0.0 | **False Alarm** | 🟢 **INVALID** | `paper_adapter.py` line 284 explicitly checks `if fill_price <= 0: return REJECTED_NO_QUOTE`. Orders with price $\le 0$ are rejected. |

---

## 7. Section 6: Comprehensive Defect & Reconnaissance Registry

### Summary of All Verified Structural Items to Scope for Solutions

```
                               VERIFIED DEFECTS MAP
                                         │
        ┌────────────────────────────────┼────────────────────────────────┐
        ▼                                ▼                                ▼
[Execution & Concurrency]        [Strategy & Scoring]           [Infrastructure & Architecture]
• Live Threading & Contention   • 65-Pt Regime Cap Veto        • Gap 1: _dispatch_execution_exits
• Ghost Prices (Old TS/New P)   • Dud Inflation (+10 Bonus)    • Gap 2: Actionable Pool Head-of-Line
• Sim Missing _rebuild_held_keys• Continuous Score vs Chop     • Gap 3: Dual Trade Logging
• Sim Tick Skipping & Lumping   • Config Divergence (70 vs 80) • Gap 4: 14 Conflicting EOD Times
• Delayed Fill Chased Price SL  • 3% Spread Gate vs OTM        • Gap 5: Freeze Mode Bypasses
• Strategy Runner mode String   • Dynamic Trio Allocator Bypass• Gap 6: Option Resolver Reload Loop
• CRIT-005: cmd_stop Zombies    • HIGH-007: Replay Timestamps  • CRIT-001: Tuple NameError
```

1. **`PaperDelayedAdapter` Concurrency**: Replace multi-threaded wall-clock timers with an event-driven, single-threaded simulation queue aligned with `portfolio.py`'s pending order model.
2. **Simulation Tick Ingestion Fix**: Rebuild `_active_held_keys` when appending pending orders so option ticks evaluate exits and trigger pending order fills at the true tick timestamp.
3. **Delayed SL & Target Anchoring**: Calculate stop-losses and targets from the initial breakout trigger price rather than the chased delayed fill price.
4. **Exit Dispatch Routing**: Fix `_dispatch_execution_exits` so live exits are dispatched per mode and adapter rather than relying on `m_name == "paper"`.
5. **Actionable Pool Non-Blocking**: Evict or quarantine unexecutable setups immediately from the top of the actionable pool to prevent head-of-line blocking.
6. **Strategy Runner Mode Architecture**: Store execution modes as a normalized `List[str]`, parse kwargs for all active modes, and fix string matching in `is_frozen()`.
7. **Scoring Engine Regime Overhaul**: Remove the hard 65-point suppression cap (`raw_suppress_cap`) and replace blunt additive bonuses (+10 / -25) with qualitative market regime filtering.
8. **Config & EOD Unification**: Reconcile `strategies.json`, `strategies_sim.json`, and `execution.json` so parameters (min score 70 vs 80, targets 15% vs 50%, EOD 15:15 vs 15:20) have a single source of truth.
9. **Low-Premium Spread Gate**: Adapt the option spread gate to handle cheap OTM options using absolute tick-size allowances rather than a flat percentage.
10. **Infrastructure Hotfixes**: Import `Tuple` in `runner.py`, implement process termination in `cmd_stop`, fix MCX hours in `SimulationEngine`, and preserve historical timestamps in `ReplayEngine`.
