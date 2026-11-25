# Platform Enhancements & Alpha Roadmap (`IMPROVEMENTS.md`)

**Date**: 2026-11-25  
**Scope**: Post-Audit Architectural Evolution, Detection/Scoring Refinements, Simulation Research Tooling, Live Broker Hardening, and Multi-Strategy Alpha Expansion.  
**Execution Policy**: Design & brainstorming blueprint only. No runtime code changes should be applied until active [`SYSTEM_AUDIT_2_0.md`](file:///Users/yorha/Documents/Trading%20Stuff/SYSTEM_AUDIT_2_0.md) items are resolved and market hours conclude.

---

## Executive Summary & Synergy Map

Many of the structural, UI, simulation, and alpha improvements below reinforce one another when designed together rather than in isolation:

- **Core/Strategy Decoupling (#3) + Strategy Modularization (#3, #11)**: Moving `RMSService` out of `runner.py start` into `core` turns option strike resolution & liquidity verification into a shared platform service, allowing `momentum`, a new `mean_reversion` engine (`11:30–13:30` midday chop), and future strategies to subscribe to the same Ticker Plant without duplicating RMS instances.
- **Immutable Config Presets + Derived Profiles (#4) + Disk Simulation Cache (#7) + Batch Reports (#2)**: Versioned `base` / `live` / `simulation` config presets plus user-derived diff profiles yield deterministic config hashes, enabling disk-cached `paper` simulations, 10-day Batch Reports with regime slicing, and fast parameter optimization (#6).
- **Mid-Day Freeze Configurability (#5) + Market Regime Tagging (#2) + Mean Reversion (#11)**: Converting the `11:30–13:30` momentum lull from a blunt assumption into a configurable rule lets Batch Reports prove whether blocking momentum entries (or handing capital to mean reversion) improves risk-adjusted returns across choppy vs trending days.
- **Master Journal Collation (#8) + Deterministic `origin_track` Recovery (#1) + Safe Isolated Testing (#9)**: Staging lock-free per-track JSONL files during market hours and atomically merging into a master trade journal on exit cleans up `recordings/` while keeping `--continue` recovery and web analytics clean and race-free.

---

## Part 1 — Architecture, Data & Execution Infrastructure

### 1. Deterministic `origin_track` Recording in Composite Trade Journals (`--continue`)
- **Current Limitation**: Composite tracks (`composite_shared_*`, `composite_split_*`) route opportunities into child sub-tracks, and `BaseSuperstructure` recovery relies on `origin_track` / metadata mapping. If an entry event lacks an explicit `origin_track` (or only persists top-level `track`), `--continue` cannot guarantee deterministic routing back to the exact child sub-track after a restart.
- **Target Design**:
  1. Promote `origin_track` and `sub_track_rule` to first-class immutable fields on `TradeState` (emitted on `TRADE_ENTRY_SUBMITTED` and `TRADE_FILLED`).
  2. In [`CompositeTrack.add_recovered_trade()`](file:///Users/yorha/Documents/Trading%20Stuff/common/tracks/composite_track.py), route **strictly** by `trade.origin_track` matching child track names—never random or round-robin assignment. If a legacy journal record lacks `origin_track`, fail loudly with an explicit warning and quarantine rather than silently misattributing SL/target rules.
  3. Expose both `track` (composite group) and `origin_track` (governing exit profile) consistently in Web UI Live and Historical tables.

### 2. Batch Simulation Reports, Metric Ranking, Equity Curves & Market Regime Analytics
- **Current Limitation**: [`SimulationService`](file:///Users/yorha/Documents/Trading%20Stuff/web/simulation_service.py) executes batch dates sequentially into in-memory `runs_history`, but lacks cross-date aggregation, ranking metrics, portfolio equity curves, and market-regime slicing.
- **Target Design**:
  1. **Cross-Session Aggregation Endpoint (`GET /api/sim/batch/{batch_id}`)** & Batch Report UI view aggregating metrics per `(strategy, track, mode)` across all selected dates:
     - **Return & Profitability**: Total Net P&L (INR & %), Expectancy per trade, Profit Factor ($\sum \text{Wins} / |\sum \text{Losses}|$), Payoff Ratio (Avg Win / Avg Loss).
     - **Consistency & Risk ("loses least money" / capital preservation)**: Max Drawdown (% & INR), Calmar Ratio ($\text{Return} / \text{MaxDD}$), Daily Win Rate (% profitable sessions), Ulcer Index, Worst-Day Loss, and Daily P&L Coefficient of Variation.
     - **Ranking Controls**: Sortable leaderboard allowing selection of primary ranking objective (Highest Net P&L, Highest Calmar, Lowest Max Drawdown, Highest Sharpe-like Daily Ratio, Lowest Loss Variance).
  2. **Cumulative Multi-Day Equity & Drawdown Graphs**: Concatenate daily intraday curves chronologically across all batch dates into a continuous cumulative P&L curve + underwater drawdown shaded chart, with vertical session separators and hover tooltips showing `(date, regime, day_pnl)`.
  3. **Market Regime Tagging & Per-Regime Grouping**:
     - *Data Gap Found*: Inspection of `recordings/2026-11-19`, `2026-11-23`, and `2026-11-25` showed `equity_ticks.parquet` currently records the 215 F&O stocks **only**—`Nifty 50` (`NSE_INDEX:Nifty 50`) is absent from historical `equity_ticks.parquet` despite being subscribed in [`FeedHandler`](file:///Users/yorha/Documents/Trading%20Stuff/core/feed/feed_handler.py).
     - *Two-Part Solution*:
       - **Forward Fix**: Verify `NSE_INDEX:Nifty 50` (and India VIX if subscribed) flows through `TICK.EQ` into `MarketRecorder` and `equity_ticks.parquet`.
       - **Historical Backfill Fallback**: Compute a **Synthetic Equal-Weighted F&O Basket & Intraday Breadth Index** directly from the 215 recorded stocks (`5m` median return, Advance/Decline ratio, Efficiency Ratio $= |C_{\text{close}} - O_{\text{open}}| / \sum |\Delta C_{5\text{m}}|$, and intraday realized volatility).
     - *Regime Taxonomy*: Classify each session (and 3 intraday windows: Opening `09:15–11:30`, Midday `11:30–13:30`, Closing `13:30–15:20`) into deterministic labels such as `TREND_UP`, `TREND_DOWN`, `CHOPPY_RANGE`, `V_REVERSAL`, and `HIGH_VOL_EXPIRY`, enabling one-click grouping and sorting by regime in the Batch Report.

### 3. Web UI Admin Tab, Decoupled Core RMS, and Multi-Strategy Architecture
- **Admin & Process Health Tab in Web UI**:
  - Replace blind terminal inspection with an **Operations / Admin** tab backed by REST endpoints (`/api/admin/processes`, `/api/admin/action`, `/api/admin/logs`):
    - **Process Matrix**: Reads `logs/pids.json`, verifies OS PID liveness, CPU/RSS usage, uptime, and ZMQ socket liveness (`quotes_heartbeat.pub`, `feed_equity.ipc`, `feed_options.ipc`, `rms_strike_pub.ipc`, `scoring_alerts_pub.ipc`), plus `shm_ohlc_221` write freshness (`last_update_ns`).
    - **Safe Controls**: Freeze / Unfreeze per mode (`paper`, `paper_delayed`, `live`, `all`), Stop-Live emergency switch, Strategy restart (`--continue`) targeting a selected derived config profile (#4), Rest-Fallback toggle, and Groww TOTP token status / refresh trigger. All mutating admin actions on `live` must require explicit typed confirmation + audit log entry in `logs/admin_audit.jsonl`.
- **Decoupling RMS into Core**:
  - Currently [`runner.py`](file:///Users/yorha/Documents/Trading%20Stuff/runner.py) starts `rms_service` inside `cmd_start_v2` (strategy start), coupling option contract selection to the momentum launch.
  - Because `RMSService`'s true job—mapping `(symbol, direction)` to liquid, spread-checked candidate strikes (`ATM`, `ITM1`, `ITM2`, `OTM1`, `OTM2`) using `QuotesBroker`—is **strategy-agnostic**, promote `rms_service` into the **Core Tier** (`runner.py core --rms` or always-on with `--quotesbroker`).
  - Attach `strategy_id` (e.g., `"momentum"`, `"mean_reversion"`) and optional `strategy_tags` to `SetupAlert` and `ActionableOpportunity`, and publish on topic `OPP.<strategy_id>` so multiple concurrent strategies share one RMS process cleanly without cross-talk.
- **Modular Multi-Strategy Framework**:
  - Define a clean `BaseStrategy` abstraction (`strategies/base_strategy.py`) and plugin registry (`strategies/registry.py`) decoupling:
    - **Shared Feature State**: `CandleManager` SHM (`shm_ohlc_221`) can stay focused on 5m OHLCV + ATR + rVOL (or expand reserved padding bytes carefully for VWAP / Day High-Low) so all strategies read the exact same zero-copy memory.
    - **Strategy Detectors**: Each strategy (`strategies/momentum/`, `strategies/mean_reversion/`, etc.) binds its own isolated process (or vectorized simulation evaluator in `ColumnarSimEngine`) and declares which execution tracks it feeds.

### 4. Web UI Config Manager with Immutable Presets (`base`, `live`, `simulation`)
- **Current Limitation**: Configuration lives in flat JSON files (`config/execution.json`, `config/strategies.json`, `config/tracks/*.json`) edited manually; live parameters and experimental simulation tweaks risk colliding (plus existing bugs like duplicate keys in `strategies.json`).
- **Target Design**:
  1. **Three Protected Root Presets** (version-controlled, locked read-only in UI):
     - `config/presets/base.json`: Canonical shared defaults, risk invariants, trading schedule, brokerage, and base track definitions.
     - `config/presets/live.json`: Conservative production overrides targeting `base.json` (strict caps, EOD 15:10 freeze / 15:15 squareoff, confirmed live tracks only).
     - `config/presets/simulation.json`: Experimental research baseline (relaxed gates, multi-track ablations).
  2. **Derived Profiles (`config/profiles/<name>.json`)**:
     - Every user-created config stores `{ "name": "...", "parent": "base|live|simulation", "created_at": "...", "overrides": {...} }` as a sparse diff over its immutable parent.
     - UI Config Manager supports: **Inspect Effective Config** (merged tree with origin badges showing inherited vs overridden values), **Clone/Derive New Profile**, **Edit & Schema-Validate Overrides**, and **Delete Custom Profile** (deletion blocked for `base`/`live`/`simulation` or any profile currently bound to a running live process).
  3. **Integration**: Derived profiles feed directly into Simulation runs (#7 hash includes exact merged config) and Admin Strategy launches (#3).

### 5. Configurable Mid-Day Freeze Window (`11:30–13:30` IST)
- **Current State**: [`MarketSchedule`](file:///Users/yorha/Documents/Trading%20Stuff/core/config/trading_schedule.py) already defines `midday_freeze_start` / `midday_freeze_end` and `is_midday_lull()`, **but** neither [`BaseSuperstructure`](file:///Users/yorha/Documents/Trading%20Stuff/execution/superstructures/base_superstructure.py) nor [`ColumnarSimEngine`](file:///Users/yorha/Documents/Trading%20Stuff/simulation/columnar_sim_engine.py) honors it (and defaults currently say `11:00–13:30` rather than `11:30–13:30`).
- **Target Design**:
  1. Make midday freeze first-class in config:
     ```json
     "midday_freeze": {
       "enabled": true,
       "start": "11:30",
       "end": "13:30",
       "allow_exits": true,
       "exempt_strategies": ["mean_reversion"],
       "exempt_tracks": []
     }
     ```
  2. **Unified Parity Across Live, Paper, Paper-Delayed, and Simulation**:
     - When `start <= now_ist < end` and the strategy/track is not exempt, new entry opportunities are skipped while existing open positions continue normal SL, trailing ratchet, target decay, and timeout exits (`allow_exits=True`).
     - Exposing `midday_freeze.enabled` + `start` / `end` in the Simulation UI lets Batch Reports (#2) quantify the exact edge of `11:30–13:30` vs unfiltered trading across all recorded sessions.

### 6. Optimization of Per-Track Parameters (LP / MILP + Fast Surrogate Search)
- **Mathematical Feasibility Check**:
  - Why pure Linear Programming (LP) cannot optimize a track's internal SL%/Target%/Ratchet parameters end-to-end: A single trade's exit price and realized profit under trailing step-locks, decaying targets, and stagnation timeouts is a **non-linear, piecewise-discontinuous function** of price path `(high_to_now, elapsed_time, bid_t)`. Expressing full tick-by-tick path dependency over 7M option ticks as MILP binary variables would explode computationally.
  - **Where True LP/MILP *Does* Excel (Portfolio Allocation Layer)**:
    Once we have historical trade returns per track across recorded dates (#2), finding optimal **capital allocation weights $w_k$** across tracks and intraday time buckets to maximize expected net return subject to max drawdown / **CVaR (Rockafellar-Uryasev formulation)** and daily exposure bounds is an **exact Linear Program** (`scipy.optimize.linprog` using HiGHS, already standard in SciPy ecosystem), solving in milliseconds!
  - **How to Solve Per-Track Exit Parameter Optimization Fast & Cleanly (Exact Fast Replay Surrogate + Derivative-Free / Bayesian Search)**:
    1. **Signal-Decoupled Trade Tensor Extraction**: For a given historical date and strategy detector (`ScoringEngine3` + `RMSService`), entry timestamps, option tokens, `entry_ask`, and forward option depth paths `[t_0..EOD: bid(t), ask(t)]` are **fixed** regardless of exit parameters! Extract those post-entry tick slices once into compact NumPy arrays.
    2. **Microsecond Exit Evaluator**: Evaluating a candidate parameter vector $\theta = (\text{sl\_pct}, \text{target\_pct}, \text{ratchet\_trigger}, \text{stagnation\_sec}, \text{decay\_rate})$ across 500 historical entries takes **< 5 milliseconds** (1,000× faster than re-running `ColumnarSimEngine`).
    3. **Optimizer + Walk-Forward Guardrails**: Run Coordinate Search / Differential Evolution / Bayesian Optimization (e.g., Optuna TPE) **per track** against risk-adjusted objective (Calmar or Profit Factor with minimum trade count penalty), strictly enforcing **Walk-Forward Cross-Validation** (optimize on days $1..K$, verify out-of-sample on days $K+1..N$) and **Parameter Stability Neighborhoods** (reject isolated sharp peaks where $\theta \pm 10\%$ collapses into losses).
    4. Export winning $\theta^*$ directly as a **Derived Config Profile** (#4) for paper verification.

### 7. Content-Addressed On-Disk Simulation Cache (Paper Only)
- **Current Limitation**: [`SimulationService.cache_by_param_hash`](file:///Users/yorha/Documents/Trading%20Stuff/web/simulation_service.py#L101) is **in-memory only** (lost on every web restart) and currently omits `track_profile` from `compute_param_hash()` (`MED-09` in [`SYSTEM_AUDIT_2_0.md`](file:///Users/yorha/Documents/Trading%20Stuff/SYSTEM_AUDIT_2_0.md)).
- **Target Design**:
  1. **Strict Paper-Only Caching Rule**: Never cache `paper_delayed` simulations on disk (since random delay draws are non-deterministic unless an explicit `random_seed` is enabled in debug mode). Cache strictly when `modes == ["paper"]` (or cache the `paper` portion independently while always executing `paper_delayed` fresh).
  2. **Deterministic Cache Key (SHA-256)** combining:
     - Target `date` + parquet fingerprints (`(size, mtime_ns)` of `equity_ticks.parquet` and `option_depth.parquet`).
     - Complete canonical resolved configuration (`global_settings`, `track_profile`, `base_tracks`, `composite_tracks`, `custom_tracks`, `midday_freeze`, strategy thresholds).
     - `CODE_VERSION` / AST hash of `ColumnarSimEngine` and `common/tracks/` (prevents serving stale results after bug fixes or exit-rule edits!).
  3. **Storage Layout**: Write compressed JSON or Arrow/Parquet summaries + trade details under `cache/simulations/<date>/<hash[:16]>.pkl.gz` (with `cache/simulations/index.json` LRU/size budget eviction, e.g., capped at 500 MB).
  4. **Payoff**: Unlocks instant Batch Reports (#2) and accelerates Parameter Optimization (#6).

### 8. Standardized Logging & Lock-Free Staged → Master Trade Journal Pipeline
- **Logging Standardization**:
  - Audit and unify `print(...)` calls, `common/utils/` logger setup, and process logs into one canonical structured format (`[YYYY-MM-DD HH:MM:SS.mmm IST] [LEVEL] [SERVICE] message`), writing rotating logs (`RotatingFileHandler`, e.g., 10 MB × 3 backups) under `logs/` and suppressing high-frequency tick-loop noise at `INFO`.
  - Retire unused/stale files in `logs/` (`.log` clutter from 40+ track runs can optionally route to `logs/services/` and `logs/exec/`).
- **Master Trade Journal Pipeline (`recordings/YYYY-MM-DD/`)**:
  - *Why Not Write One Shared File Live?* Writing directly from 40 independent processes to a single JSONL file during live trading risks partial line interleaving or lock contention (`flock` overhead on hot order paths).
  - *Best-of-Both-Worlds Pattern*:
    1. **During Market Hours (Zero Contention)**: Each `exec_<mode>_<track>` process writes lock-free to `recordings/YYYY-MM-DD/.staging/journal_<mode>_<track>.jsonl`.
    2. **Clean Exit & EOD Collation**: On graceful exit (`shutdown()` / `stop`) and as a step inside `runner.py post-market`, atomically merge/upsert all `.staging/*.jsonl` events into `recordings/YYYY-MM-DD/master_trades.jsonl` (deduplicated by `(trade_id, event, timestamp)` and preserving both `track` and `origin_track`), then write a normalized per-trade summary `master_trades_summary.parquet` / `.json` for instant analytics.
    3. **Unified Readers**: Update `BaseSuperstructure._recover_state_from_journals()` (`--continue`), `LiveService`, `HistoricalService`, and `runner.py report` to read transparently from `master_trades.jsonl` + any live `.staging/journal_*.jsonl` files (so live MTM updates remain real-time while closed days stay tidy in one master file!).

### 9. Hardening the Test Suite & Eliminating Live Process Interference
- **Lessons Learned Today (`2026-11-25`)**: Unisolated unit tests in `test_rms_service.py`, `test_listener.py`, `test_position_registry.py`, and `test_scoring_engine_3_0.py` unlinked live `/tmp/*.ipc` sockets during market hours, silently blinding all 40 running strategy processes.
- **Structural Safeguards**:
  1. **Hard Sandbox Guard in Production Constructors**: When `os.environ.get("TRADING_TEST_MODE") == "1"` (set by `runner.py test` and test runner initialization), add a safety assertion in `FeedHandler`, `QuotesBroker`, `RMSService`, `CandleManager`, `ScoringEngine3`, `OpportunityPool`, and `ProtectedPositionRegistry`: **raise `RuntimeError` immediately if a test attempts to bind `/tmp/` production `.ipc` paths, `shm_ohlc_221`, or `logs/active_positions.json`!**
  2. **Shared Isolated Test Fixture (`tests/helpers.py`)**: Provide temp-directory, `inproc://`, and unique SHM name factories (`f"test_shm_{os.getpid()}_{uuid4().hex[:6]}"`) plus clean teardown so `ResourceWarning`s (unclosed ZMQ sockets/contexts and leaked `/tmp/*.shm` files) drop to zero.
  3. **Expanded Test Coverage**: Add dedicated regression tests for `runner.py` CLI flags (`freeze`, `unfreeze`, `stop-live`, `--confirm-live`, `--continue`), `MarketSchedule` midday freeze enforcement, `RMSService` top-level `risk` config parsing, `CompositeTrack` sub-track recovery, and `GrowwMockBroker` failure injection (#10).

---

## Part 2 — Groww Live Execution Audit & Mock Broker Sandbox (#10)

Before allocating real capital to `LiveSuperstructure` ([execution/superstructures/live.py](file:///Users/yorha/Documents/Trading%20Stuff/execution/superstructures/live.py)), a deep pre-live review surfaces **critical structural gaps and broker-specific hazards** that must be fixed and stress-tested:

### Critical Live Broker Risks Identified in Current Code
1. **Multi-Process State File Collision (Severe Architectural Risk)**:
   - Under `runner.py start --mode live`, **each track launches in its own OS process** (`exec_live_<track>`). Yet every `LiveSuperstructure` reads/writes the **same** `logs/live_positions_state.json`, calls `reconcile_broker_positions()` against the **same** Groww account portfolio, and appends to `logs/live_trades.csv` without locking or `track` scoping!
   - *Failure Mode*: Track A opens `RELIANCE 2500 CE`. Fifteen seconds later Track B polls `get_broker_positions()`, sees `RELIANCE 2500 CE` missing from Track B's local `self.positions`, adopts it as an "orphan position" (`tag="ADOPTED_ORPHAN"`), and attaches duplicate SL / exit orders—or worse, `check_eod_squareoff` fires from N processes simultaneously!
   - *Required Fix*: Partition state files/locks per process or funnel all live Groww broker execution through a **single centralized `GrowwOrderGateway` process** over ZeroMQ (keeping per-track accounting isolated while serializing all broker API calls, rate limits, position reconciliation, and SL order modifications in one place!).
2. **Upstox Token vs Groww `trading_symbol` Mismatch**:
   - Line 401 of `live.py` converts `instrument_token` via `instrument_token.split("|")[-1]`. If `options_map` stores an Upstox numeric/exchange token (`NSE_FO|43210`), `trading_sym` passed to `groww_client.place_order()` will be `"43210"` rather than Groww's exchange trading symbol (e.g., `"RELIANCE26NOV2500CE"`), causing guaranteed order rejection. Needs verified mapping table from NSE symbol + expiry + strike + CE/PE to exact Groww contract symbol (`groww_instruments` validation at `prepare` step).
3. **Unverified Groww SDK Contract & API Limits**:
   - Must validate every `GrowwAPI` SDK call signature (`place_order`, `modify_order`, `cancel_order`, `get_order_detail`, `get_positions_for_user`), parameter names (`segment`, `product`, `order_type`, `validity`), status strings (`COMPLETE`, `OPEN`, `TRIGGER_PENDING`, `REJECTED`, `CANCELLED`), and per-second rate limits against the installed `growwapi` package version.
4. **Resting SL Order Type & Gap-Through Vulnerability (`SL` vs `SL_M` + Tick Size)**:
   - Options prices on NSE require strict **tick-size rounding (`0.05` INR)** on `price` and `trigger_price` (otherwise orders reject with invalid price tick).
   - NSE F&O `SL-M` restrictions mean resting SL should use `SL` (Stop-Loss Limit) with a calculated protective slippage buffer (e.g., `limit_price = round_tick(trigger_price * 0.97)`) so fast bid drops fill reliably rather than getting skipped at a fixed limit price.
5. **Naked Position Hazard if Resting SL Attachment Fails**:
   - Currently `live.py` places `BUY` `MARKET`, and *after* fill calls `_place_resting_sl_order()`. If the SL placement fails (margin check, tick size error, transient timeout, `401`), `live.py` still leaves a real open position unprotected at the exchange! Must implement immediate **retry with backoff → emergency market squareoff fallback → critical operator freeze + alert**.
6. **Partial Fills & NSE Freeze Quantity Splitting**:
   - Large lot sizes on high-value or liquid contracts can hit NSE `freeze_qty` or receive partial fills (`filled_quantity < quantity`). Resting SL quantity must track *actual* `filled_quantity` from broker fills, not requested order quantity, and slice orders exceeding `freeze_qty`.
7. **Token Expiry & Clock Synchronization**:
   - Line 110 prioritizes `GROWW_API_KEY` as a fallback for `access_token` (`or os.environ.get("GROWW_API_KEY")`), which is an API key rather than a daily TOTP session token! Separate `api_key` and `access_token` strictly, verify session health on startup via a lightweight read-only broker call, and align EOD square-off strictly to IST (`now(IST)`), not naive `datetime.now()`.

### Dedicated Groww Mock Broker Sandbox (`MockGrowwGateway` / `runner.py mock-broker`)
- Build an out-of-process **Groww Simulator / Mock Broker** (plus an in-memory `MockGrowwAPI` adapter implementing exact `growwapi.GrowwAPI` method signatures and response payloads):
  - Consumes real-time bid/ask streams from `QuotesBroker` (`quotes_stream.ipc`) during market hours or historical `replay`.
  - Simulates realistic exchange matching behavior:
    - Market orders fill against live Ask (`BUY`) / Bid (`SELL`) depth (with configurable partial fills across 5 depth levels!).
    - Tracks resting `SL` / `SL_M` orders as `TRIGGER_PENDING` and fires realistic fills when market Bid drops through `trigger_price`.
    - Enforces configurable **25-modification cap**, `0.05` tick-size validation, freeze quantity caps, margin balance tracking, and **fault-injection switches** (simulated 401 token expiry, 5s network timeouts triggering `_resolve_timed_out_order` idempotency lookup, random order rejections, and out-of-band "manual mobile close" to test ghost/orphan reconciliation!).
  - Allows running full `LiveSuperstructure` end-to-end against live market ticks **with zero real money** before going live.

---

## Part 3 — Scoring Engine Refinements & New Alpha Strategies (#11)

### A. Structural Improvements to `ScoringEngine3` Detection Quality
Code inspection of [`strategies/momentum/scoring_engine.py`](file:///Users/yorha/Documents/Trading%20Stuff/strategies/momentum/scoring_engine.py) highlights five high-leverage detection fixes & upgrades:

1. **Early-Bar RoV Spike Distortion (Elapsed Time Floor)**:
   - Currently `curr_elapsed = max(1.0, now_ts - float(c["timestamp"]))` and `curr_rov = c["volume"] / curr_elapsed` (and note `c["timestamp"]` is currently set to the first tick rather than the 5m boundary, `MED-01`). Even once boundary-aligned, 1 block print at second `09:20:02` divides volume by `2s`, generating artificial `50x` RoV bursts.
   - *Fix*: Require minimum elapsed seconds inside the active bar (e.g., `curr_elapsed >= 15s` or blend with trailing 60-second tick velocity) before triggering intrabar breakouts.
2. **2-Candle Contraction OLS Trendline Instability**:
   - In `_check_flag_breakout`, fitting `np.polyfit(xs, ys, 1)` across only **2** contraction candles (`xs = [0, 1]`) projects the exact linear slope of just two highs/lows. A steep down-sloping 2-bar flag can project `proj_trigger` *below* current price (triggering early), while a tiny upper wick on bar 2 can project an unreachable trigger.
   - *Fix*: Clamp OLS slope `m` to $\le 0$ for bull flags ($\ge 0$ for bear flags), or require `close >= max(proj_trigger, min_structural_pivot)` (e.g., upper quartile of contraction highs) + maximum contraction range vs ATR (`contr_range <= 0.8 * ATR`) to guarantee true volatility compression (VCP / tight flag).
3. **Configurable Timeframe Beyond 5m (`300.0`s)**:
   - Replace hardcoded `300.0` divisors (`MED-03`/`BUG-SE-003`) with parameterized `bar_seconds` so we can A/B test 3m vs 5m vs 15m candles in Batch Simulation Reports (#2).
4. **Opening Gap vs Continuous Bar Contamination (`bar_idx == 0`)**:
   - The `09:15–09:20` bar captures overnight auction imbalances, creating extreme `high - low` and `volume` readings that distort `prev` baselines at `09:20–09:30`. Normalize opening bar baseline volume separately and guard `ATR` against first-bar gap distortions.
5. **Spread-Adjusted Expected Move Filter (Underlying-to-Option Alignment)**:
   - Before firing an `ActionableOpportunity`, check whether the stock's ATR-implied option gain ($\approx \Delta \times \text{ATR}_{5\text{m}}$) comfortably clears the option's prevailing bid-ask spread + ₹60 round-trip brokerage + slippage (`expected_edge / cost_drag >= 3.0`). Avoids taking valid equity breakouts on illiquid stock options whose spreads swallow 3–5% immediately on entry.

---

### B. New Alpha Strategies Backtestable Against Our 11-Day Parquet Dataset

We have **11 recorded sessions** (`2026-11-09` through `2026-11-25`, with ~60–80 MB equity ticks and ~400 MB–1.1 GB 5-level option depth per full session from `2026-11-16` onward). That depth richness enables rigorous empirical testing of **complementary strategies** beyond pure intraday momentum:

1. **Strategy #2: Midday VWAP Mean-Reversion / Exhaustion Fade (`11:30–13:30` Specialist)**
   - **Thesis**: Momentum loses money between `11:30–13:30` because breakouts during low institutional liquidity trap late buyers at range extremes and snap back to fair value. Instead of letting capital sit completely idle, allocate a bounded sleeve to fading volume-climax exhaustion moves.
   - **Signal Rules**:
     - Stock extended $> 1.8 \times \sigma_{\text{VWAP}}$ (or $> 2.5 \times \text{ATR}_{5\text{m}}$ from intraday VWAP).
     - Climactic spike bar (`rVOL >= 2.5x`) fails to close near extreme (**anti-CLV rejection**: Bull spike closes with `CLV <= 0.35`, Bear spike closes with `CLV >= 0.65`) or breaks inside prior bar's range (wick rejection).
     - Market Regime filter (#2): Only active when breadth/efficiency indicates `CHOPPY_RANGE` (disabled during strong `TREND_UP` / `TREND_DOWN` squeeze days!).
   - **Option Execution Profile**: `ITM1` (high delta ~0.65–0.70, lower theta bleed), tight invalidation just beyond the rejection wick extreme, target = intraday VWAP or 50% mean reversion.

2. **Strategy #3: Opening Range Compression Breakout & Bull/Bear Trap Reclaim (`09:25–10:30` IST)**
   - **Thesis**: Stocks that coil tightly inside the first 15 minutes (`3 × 5m` bars, Opening Range width $< 0.65 \times \text{Daily ATR}$ proxy) store directional energy; alternatively, a failed ORB sweep in the first 30 minutes that snaps back across VWAP triggers sharp short-covering rallies.
   - **Option Execution Profile**: Fast `ATM` entry (like `OpeningThrustConvexityTrack`) with aggressive breakeven ratchet once option clears spread + 5%.

3. **Strategy #4: Cross-Sectional Relative Strength Leader / Laggard Dispersion**
   - **Thesis**: On mixed days, buying calls on stocks merely moving with Nifty gets chopped up. Rank all 215 F&O equities live by normalized intraday move relative to the 215-stock basket `(Stock Return - Median Basket Return) / ATR`. Trigger breakouts **strictly** in the top decile (leaders for `CE`) and bottom decile (laggards for `PE`), filtering out weak beta noise.

4. **Strategy #5: Option-Microstructure & Orderbook Imbalance Filter / Expiry Mode**
   - **Thesis**: We already record full **5-level `bids` and `asks` arrays** (`~7.3M+` option snapshots/day!). Currently strategies only look at top-of-book `bid[0]`/`ask[0]`.
   - **Signal / Execution Edge**:
     - Measure 5-level book pressure ($\sum_{i=0}^{2} \text{bid\_qty}_i / \sum_{i=0}^{2} (\text{bid\_qty}_i + \text{ask\_qty}_i)$) and option-volume acceleration relative to strike baseline before entering.
     - **Expiry-Day Mode (like Nov 23)**: Automatically tighten RMS liquidity thresholds (tighter `max_spread_pct`, minimum top-3 bid depth in INR, restrict strikes to `ATM` / `ITM1` where market makers stay active) to capture concentrated expiry convexity while avoiding dead `OTM2` traps.

---

## Part 4 — Codebase Efficiency, Hardening & Operational Lifecycle Improvements

### 1. Fast Shard-Based `MarketRecorder`, Explicit Collation CLI & Web UI Tasks Tab
- **Inefficiency**: [`MarketRecorder`](file:///Users/yorha/Documents/Trading%20Stuff/core/recorder/market_recorder.py#L163-L220) currently flushes every `5,000` ticks by reading the entire day's `equity_ticks.parquet` / `option_depth.parquet` (up to ~1.1 GB by afternoon), concatenating, re-compressing, and rewriting atomically—blocking the ZMQ receive thread for seconds at a time ($O(N^2)$ session I/O).
- **Target Design**:
  1. **Zero-Stall Shard Writes**: Flush batches into immutable numbered part files (`equity_ticks_part_0001.parquet`, `option_depth_part_0001.parquet`) in `<5ms`. Upstream Polars consumers (`CandleManager.warmup_from_parquet`, `CandleService`, `LiveService`, `ColumnarSimEngine`) read partitioned globs seamlessly.
  2. **Automatic Exit Collation + Standalone `collate` Command**: Merge shard files cleanly into single session Parquet files upon graceful recorder exit **and** provide an idempotent standalone `runner.py collate --date YYYY-MM-DD` command (plus staging journal collation into `master_trades.jsonl`).
  3. **Web UI Operational Tasks Tab (Pre-Market & Post-Market)**: Add a dedicated **Tasks** tab alongside Admin monitoring, exposing:
     - **Scan Pending Tasks**: Automatically inspects today's workspace and lists what still needs execution (stale/missing instrument master, filtered F&O CSV, 1m volume baseline, subscription manifest, uncompacted recorder `.parquet` shards, uncollated `.staging/journal_*.jsonl` files, uncompressed session `.tar.gz` archive).
     - **Granular One-Click Execution**: Run individual pre-market steps (`fetch-instruments`, `filter-universe`, `build-volume-index`, `build-manifest`) or post-market steps (`collate-recordings`, `collate-journals`, `archive-session`, `prune-logs`) independently with live progress output.

### 2. Streamlined `ColumnarSimEngine` & Warm Web Sim Workers (Preserving Full Tick Decision Fidelity)
- **Invariant Preserved**: Active positions and pending delayed orders **must continue evaluating every relevant option depth tick** (live bid/ask updates along the holding window, just as live superstructures evaluate subscribed options from `QuotesBroker`). We do **not** drop ticks for active/subscribed option contracts.
- **True Redundancies Eliminated** ([columnar_sim_engine.py](file:///Users/yorha/Documents/Trading%20Stuff/simulation/columnar_sim_engine.py), [simulation_service.py](file:///Users/yorha/Documents/Trading%20Stuff/web/simulation_service.py)):
  1. **Single Sort + Flat Contiguous Arrays**: `load_data()` sorts 7.3M+ rows by `exchange_timestamp` and `_build_options_index()` immediately re-sorts the same DataFrame by `["token", "exchange_timestamp"]`, creates ~10,000 Polars sub-DataFrames via `partition_by`, and allocates ~60,000 tiny NumPy arrays. Instead: sort **once** by `["token", "exchange_timestamp"]` and store 6 flat contiguous NumPy arrays with `(start_idx, end_idx)` slice bounds per token.
  2. **Remove Duplicate Strike Lookups**: Eliminate redundant `"ITM"` + `"ITM1"` and `"OTM"` + `"OTM1"` `resolve_sim_strikes()` queries (5 binary searches instead of 7).
  3. **Avoid Per-Tick Python Object Churn**: Pre-map unique equity tokens to integer IDs once before the event loop and compute IST hour/minute via integer epoch arithmetic instead of millions of `datetime.fromtimestamp(..., tz=IST)` and `normalize_instrument_token()` calls.
  4. **Warm Simulation Worker / Shared Buffers**: Reuse already-cached `equity_ticks` / `option_depth` flat arrays across Web UI worker runs (`_simulation_worker_process`) instead of re-reading ~1.1 GB from disk on every run.

### 3. `CandleManager` Hot-Path Consolidation
- **Inefficiency** ([candle_manager.py:214-332](file:///Users/yorha/Documents/Trading%20Stuff/core/candles/candle_manager.py#L214-L332)): `on_tick()` invokes `_get_bar_index(ts)` twice per tick (creating two timezone-aware `datetime` objects per tick), dynamically runs `np.sum(self.volume_baseline[idx, m0:m_end])` on every tick, and normalizes token strings repeatedly—even during multi-million-row `warmup_from_parquet()` replays.
- **Proposed Fix**: Precompute the `(221, 75)` 5-minute baseline sums once at `__init__`, replace `datetime.fromtimestamp` with pure integer IST epoch arithmetic, cache `raw_token -> sym_idx`, and use a fast-path batch replay loop during warmup.

### 4. `ScoringEngine3` Sequence-ID Dirty Checking
- **Inefficiency** ([scoring_engine.py:513-590](file:///Users/yorha/Documents/Trading%20Stuff/strategies/momentum/scoring_engine.py#L513-L590)): The `200 Hz` spin loop (`5ms` sleep) seqlock-copies all `221` active bars every iteration even when a symbol has received zero ticks since the previous scan, and re-evaluates `_has_recent_pole()` with list allocations repeatedly.
- **Proposed Fix**: Track `last_seen_seq[idx]` per symbol and skip symbols whose `headers[idx]["sequence_id"]` is unchanged (eliminating ~80–95% of idle spin work), and cache recent-pole flags per symbol on bar transitions.

### 5. `BaseTrack` & `BaseSuperstructure` Tick Dispatch & Multi-Process Overhead
- **Inefficiency** ([base_track.py:286-360](file:///Users/yorha/Documents/Trading%20Stuff/common/tracks/base_track.py#L286-L360), [base_superstructure.py:422-508](file:///Users/yorha/Documents/Trading%20Stuff/execution/superstructures/base_superstructure.py#L422-L508)): Running 20 tracks × 2 modes launches **40 separate Python processes**, each holding its own ~80–120 MB RSS footprint, ZMQ sockets, and `ConfigManager`, all waking on every 1s `HEARTBEAT`. Inside `BaseTrack.on_tick()`, every tick triggers two linear scans over `active_trades.values()` with repeated string normalization—while `on_opportunity()` subscribes `QuotesBroker` to underlying equity symbols (`opp.symbol`, `NSE:{symbol}`) that `QuotesBroker` never receives (`TICK.OPT`-only) and `exit_trade()` never cleans up.
- **Proposed Fix**: Maintain `O(1)` lookup indices (`trades_by_token`, `trades_by_underlying`) inside `BaseTrack`; attach read-only to `shm_ohlc_221` for underlying LTP updates (`StructuralConvexityTrack`) instead of dead `QuotesBroker` equity subscriptions; support an optional **per-mode multi-track worker** topology (keeping per-track capital and journals isolated while cutting RAM & IPC overhead by ~10×).

### 6. `LiveService` & WebSocket Incremental Telemetry
- **Inefficiency** ([live_service.py:175-280](file:///Users/yorha/Documents/Trading%20Stuff/web/live_service.py#L175-L280), [server.py:180-196](file:///Users/yorha/Documents/Trading%20Stuff/web/server.py#L180-L196)): Re-parses full journal JSONL files from byte `0` on every change; loops `df.filter()` inside `_sync_lvc_from_flushed_parquet()` across missing tokens without cooldown; computes `_build_intraday_curve` in $O(N^2)$; and serializes/broadcasts full overview JSON every 1s over WebSockets even when state is idle.
- **Proposed Fix**: Tail journals incrementally via byte offsets; coalesce parquet LVC fallback into one `group_by("token").last()` scan with a cooldown; optimize `_build_intraday_curve` to $O(N \log N)$; and gate WebSocket broadcasts on a lightweight `state_version` / hash diff.

### 7. Single Canonical Strike Resolver & Strict Zero-Fabrication Enforcement
- **Inefficiency**: Strike selection (`ATM`/`ITM1`/`ITM2`/`OTM1`/`OTM2`) and liquidity/spread validation exist independently in [`RMSService`](file:///Users/yorha/Documents/Trading%20Stuff/core/rms/rms_service.py), [`ColumnarSimEngine`](file:///Users/yorha/Documents/Trading%20Stuff/simulation/columnar_sim_engine.py), and fallback helpers, causing silent live-vs-backtest divergence and re-introducing `ltp` / `entry_price` fallbacks when quotes are missing.
- **Proposed Fix**: Extract a single shared pure-Python `common/options/strike_resolver.py` used identically by `RMSService` and `ColumnarSimEngine`, enforcing a **non-negotiable zero-fabrication invariant** (`bid == 0.0` / `ask == 0.0` / missing depth $\implies$ reject or preserve `0.0`, never substitute `ltp` or `entry_price`).

### 8. Single Canonical Trade Journal Reader (Zero Schema or Format Clashes)
- **Inefficiency**: Four separate journal readers (`BaseSuperstructure._recover_state_from_journals`, `LiveService._read_journal_trades`, `HistoricalService._parse_journal_file`, and `runner.py cmd_report`) duplicate JSONL lifecycle folding with diverging assumptions.
- **Proposed Fix**: Consolidate into one shared `common/journals/reader.py` enforcing immutable entry fields, metadata deep-merging, `origin_track` preservation, and unified reading across `.staging/journal_*.jsonl` + `master_trades.jsonl` with zero clashes across services.

### 9. Eliminate All Silent Exception Swallowing & Wire Health Telemetry
- **Inefficiency**: Dozens of bare `except Exception: pass` blocks across `FeedHandler`, `BaseSuperstructure`, `OpportunityPool`, `LiveService`, `CandleService`, `MarketRecorder`, and `LiveSuperstructure` silently swallow real bugs (e.g., protobuf parse errors, dropped ZMQ sends, disk write failures).
- **Proposed Fix**: Audit and remove **every silent `pass` exception handler** across the codebase. Replace with rate-limited structured logging **and** increment per-component atomic error/drop counters published to the Web UI **System Health** panel (part of the Admin tab) so operators immediately see degraded components or parse errors.

### 10. Modular `cli/` Package & C-Style Macro Constant Discipline
- **Inefficiency**: `runner.py` spans 1,423 lines mixing CLI definitions, process orchestration, reporting, and pre/post-market automation, while domain constants (`300.0`, `250000.0`, `60.0`, `0.03`) remain hardcoded inline across multiple modules.
- **Proposed Fix**:
  1. Split `runner.py` commands into clean submodules under `cli/` (shared directly with the Web UI Admin & Tasks tabs so UI and CLI execute identical code paths).
  2. Eliminate scattered magic numbers: configurable domain rules must come from `ConfigManager`, and any genuine structural/invariant constants must be hoisted to **C-style `UPPER_CASE` module-level macros at the top of the file** (e.g., `BAR_DURATION_SEC`, `NSE_OPT_TICK_SIZE`, `STALENESS_LIMIT_SEC`).

---

## Part 5 — Recommended Implementation Sequencing (Post-Market & Post-Audit)

| Wave | Focus | Key Items | Dependencies / Prerequisites |
|---|---|---|---|
| **Wave 0 (Immediate Post-Market)** | Critical Correctness & Zero Fabrication | Clear remaining [`SYSTEM_AUDIT_2_0.md`](file:///Users/yorha/Documents/Trading%20Stuff/SYSTEM_AUDIT_2_0.md) items (`CRIT-02`, `CRIT-05`, `HIGH-02`..`07`), test isolation guard (#9), remove silent `except: pass` + health counters, hoist constants & C-style macros | Must finish before building new features on top |
| **Wave 1 (Foundations & Data Pipeline)** | Hygiene, Parity & Storage | Shared `common/options/strike_resolver.py`, shared `common/journals/reader.py`, `MarketRecorder` fast shard writes + exit/CLI collation, standardized logging & staged→master journals, `origin_track` persistence, configurable midday freeze, Nifty 50 recording fix | Enables clean multi-day data, zero live/sim drift & reliable `--continue` |
| **Wave 2 (Research, Tasks & Web Platform)** | Backtest Speed & Ops Control | `ColumnarSimEngine` single-sort flat array refactor + warm workers, disk sim cache (paper only), immutable presets + derived configs, Batch Reports + regime classifier, surrogate parameter optimizer, Web UI Admin/Health & Tasks tabs + `cli/` refactor | Unlocks rapid empirical optimization across all 11+ recorded days |
| **Wave 3 (Core Decoupling & Alpha)** | Multi-Strategy Engine | Decouple `RMSService` to core & add `strategy_id` routing, `CandleManager` & `ScoringEngine3` hot-path optimizations (dirty sequence skip), `BaseTrack` `O(1)` indexing + SHM underlying LTP, Mean Reversion (`11:30–13:30`) & Relative Strength strategies | Requires Wave 1–2 validation |
| **Wave 4 (Live Readiness)** | Groww Hardening | Centralized `GrowwOrderGateway` (single broker writer), Groww symbol/tick/SL-limit verification, `MockGrowwGateway` fault-injection sandbox | Mandatory gate before live capital deployment |
