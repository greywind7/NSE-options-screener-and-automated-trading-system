# Platform Enhancements & Alpha Roadmap (`IMPROVEMENTS.md`)

**Date**: 2026-11-26  
**Scope**: Remaining Post-Rollout Architectural Evolution, Scoring/Alpha Research, Simulation Speedups, Live Groww Hardening, and Carried-Over Audit Remediation.  
**Related Journals**: Completed milestones & implementation details live in [`TASKS_DONE.md`](file:///Users/yorha/Documents/Trading%20Stuff/docs/live/TASKS_DONE.md); pending `E10_W3_CAP3` out-of-sample protocol lives in [`EXPERIMENT_E10_W3_CAP3_OOS.md`](file:///Users/yorha/Documents/Trading%20Stuff/docs/live/EXPERIMENT_E10_W3_CAP3_OOS.md); historical audit archive is [`2026-11-25_SYSTEM_AUDIT_2_0.md`](file:///Users/yorha/Documents/Trading%20Stuff/docs/archive/2026-11-25_SYSTEM_AUDIT_2_0.md).

---

## Executive Summary & Synergy Map

Many of the remaining structural, operational, simulation, and alpha improvements below reinforce one another when designed together rather than in isolation:

- **Core/Strategy Decoupling (#2) + Strategy Modularization (#2, #10)**: Moving `RMSService` out of `runner.py start` into `core` turns option strike resolution & liquidity verification into a shared platform service, allowing `momentum`, a new `mean_reversion` engine (`11:30–13:30` midday chop), and future strategies to subscribe to the same Ticker Plant without duplicating RMS instances.
- **Immutable Config Presets + Derived Profiles (#3) + Disk Simulation Cache (#6) + Parameter Optimization (#5)**: Versioned `base` / `live` / `simulation` config presets plus user-derived diff profiles yield deterministic config hashes, enabling disk-cached `paper` simulations and fast walk-forward parameter optimization directly feeding Batch Analytics (`TASKS_DONE.md`).
- **Mid-Day Freeze Runtime Enforcement (#4) + Mean Reversion (#10)**: Wiring runtime `midday_freeze` exemption hooks lets `momentum` pause selectively during extreme chop (if enabled later) or hand capital cleanly to a mean-reversion sleeve.
- **Master Journal Collation (#7) + Deterministic `origin_track` Recovery (#1) + Safe Isolated Testing (#8)**: Staging lock-free per-track JSONL files during market hours and atomically merging into a master trade journal on exit cleans up `recordings/` while keeping `--continue` recovery and web analytics clean and race-free.

---

## Part 1 — Architecture, Data & Execution Infrastructure

### 1. Deterministic `origin_track` Recording in Composite Trade Journals (`--continue`) — **[IMPLEMENTED 2026-11-28]**
- **Implemented Design**:
  1. Promoted `origin_track` and `sub_track_rule` to strict immutable fields on `TradeState` (emitted on `TRADE_ENTRY_SUBMITTED`, `TRADE_FILLED`, and `TRADE_EXIT`).
  2. Strict validation in `CompositeTrack.add_recovered_trade()` and `BaseSuperstructure._recover_state_from_journals()` quarantines ambiguous/legacy records missing valid sub-track `origin_track` into `quarantined_legacy_trades.jsonl`.
  3. Live Telemetry, Historical Review, and Trade Analysis Modals display the `Origin / Rule` column and badges.

### 2. Web UI Admin Tab, Decoupled Core RMS, and Multi-Strategy Architecture — **[Admin/Health UI & Core RMS IMPLEMENTED 2026-11-28; Multi-Strategy Framework Deferred]**
- **Implemented (`Admin Ops` & `System Health` Tabs + Core RMS Decoupling)**:
  - Added [`web/admin_service.py`](file:///Users/yorha/Documents/Trading%20Stuff/web/admin_service.py), [`web/health_service.py`](file:///Users/yorha/Documents/Trading%20Stuff/web/health_service.py), [`core/ops/operations.py`](file:///Users/yorha/Documents/Trading%20Stuff/core/ops/operations.py), and vertical left-sidebar tabs **Admin Ops** (dropdown activity flow across Pre-Market, Core, Granular Mode-Track Strategy, Risk/Auth, Post-Market, Processes/Audit) and **System Health & Day's Log Inspector** (immediate `RED` banner/badge on today's `ERROR` logs or dead PIDs, dual `mmap` + POSIX SHM probe).
  - Decoupled `RMSService` into the Core Tier (`runner.py core --rms` / `runner.py rmsservice`) with `strategy_id` & `strategy_tags` fields publishing on `OPP.<strategy_id>.<symbol>` while `cmd_start_v2()` / Admin attaches automatically when Core RMS is alive.
- **Deferred for Later**:
  - Modular multi-strategy `BaseStrategy` / `strategies/registry.py` plugin framework and second signal family (`mean_reversion`).

### 3. Web UI Config Manager with Immutable Presets (`base`, `live`, `simulation`)
- **Current Limitation**: Configuration lives in flat JSON files (`config/execution.json`, `config/strategies.json`, `config/tracks/*.json`) edited manually; live parameters and experimental simulation tweaks risk colliding.
- **Target Design**:
  1. **Three Protected Root Presets** (version-controlled, locked read-only in UI):
     - `config/presets/base.json`: Canonical shared defaults, risk invariants, trading schedule, brokerage, and base track definitions.
     - `config/presets/live.json`: Conservative production overrides targeting `base.json` (strict caps, EOD 15:10 freeze / 15:15 squareoff, confirmed live tracks only).
     - `config/presets/simulation.json`: Experimental research baseline (relaxed gates, multi-track ablations).
  2. **Derived Profiles (`config/profiles/<name>.json`)**:
     - Store `{ "name": "...", "parent": "base|live|simulation", "created_at": "...", "overrides": {...} }` as a sparse diff over its immutable parent.
     - UI Config Manager supports: **Inspect Effective Config** (merged tree with origin badges), **Clone/Derive New Profile**, **Edit & Schema-Validate Overrides**, and **Delete Custom Profile** (blocked for root presets or active live profiles).

### 4. Runtime Mid-Day Freeze Window Enforcement (`11:30–13:30` IST)
- **Current State**: [`config/strategies.json`](file:///Users/yorha/Documents/Trading%20Stuff/config/strategies.json) defines `"midday_freeze": {"enabled": false, "start": "11:30", "end": "13:30"}` (`enabled: false` per `E10_W3_CAP3`), and [`MarketSchedule`](file:///Users/yorha/Documents/Trading%20Stuff/core/config/trading_schedule.py) defines `is_midday_lull()`, **but** neither [`BaseSuperstructure`](file:///Users/yorha/Documents/Trading%20Stuff/execution/superstructures/base_superstructure.py) nor [`ColumnarSimEngine`](file:///Users/yorha/Documents/Trading%20Stuff/simulation/columnar_sim_engine.py) yet wires runtime `enabled`/`allow_exits`/`exempt_strategies`/`exempt_tracks` enforcement.
- **Target Design**: Enforce unified runtime parity across Live, Paper, Paper-Delayed, and Columnar Simulation when `midday_freeze.enabled = true`, while exempting configured strategies (e.g., `mean_reversion`) and keeping active trailing exits unblocked (`allow_exits=true`).

### 5. Optimization of Per-Track Parameters (LP / MILP + Fast Surrogate Search)
- **Portfolio Allocation Layer (`scipy.optimize.linprog` / HiGHS)**: Using historical multi-day track returns from Batch Reports (`TASKS_DONE.md`), optimize capital allocation weights $w_k$ across tracks and intraday time buckets subject to drawdown / CVaR and daily exposure bounds.
- **Signal-Decoupled Exit Parameter Optimization**: Extract fixed post-entry option depth slices `[t_0..EOD: bid(t), ask(t)]` into compact NumPy arrays so evaluating candidate $(\text{sl\_pct}, \text{target\_pct}, \text{ratchet\_trigger}, \text{stagnation\_sec}, \text{decay\_rate})$ across hundreds of entries takes `< 5ms`, guarded strictly by **Walk-Forward Cross-Validation** and **Parameter Stability Neighborhoods**, exporting winning $\theta^*$ as a derived config profile (#3).

### 6. Content-Addressed On-Disk Simulation Cache (Paper Only)
- **Current Limitation**: [`SimulationService.cache_by_param_hash`](file:///Users/yorha/Documents/Trading%20Stuff/web/simulation_service.py) is in-memory only and is wiped on server restart.
- **Target Design**:
  1. **Strict Paper-Only Caching Rule**: Never cache non-deterministic random `paper_delayed` draws unless explicitly seeded; cache `modes == ["paper"]` cleanly.
  2. **Deterministic Cache Key (SHA-256)**: Target `date` + Parquet fingerprints `(size, mtime_ns)` + full canonical resolved config + `CODE_VERSION` / AST hash of `ColumnarSimEngine` and `common/tracks/`.
  3. **Storage Layout**: Store compressed artifacts under `cache/simulations/<date>/<hash[:16]>.pkl.gz` with LRU size eviction (e.g., 500 MB cap).

### 7. Standardized Logging & Lock-Free Staged → Master Trade Journal Pipeline
- **Logging Standardization — [IMPLEMENTED 2026-11-28]**: All `core/`, `strategies/`, `execution/`, `web/`, and `scripts/` modules emit unified `[YYYY-MM-DD HH:MM:SS.mmm IST] [LEVEL] [LOGGER] message` format via [`core/common/logging_utils.py`](file:///Users/yorha/Documents/Trading%20Stuff/core/common/logging_utils.py) with `RotatingFileHandler` support.
- **Master Trade Journal Pipeline (`recordings/YYYY-MM-DD/`)** *(Remaining)*:
  1. During market hours: each `exec_<mode>_<track>` process writes lock-free to `.staging/journal_<mode>_<track>.jsonl`.
  2. On exit / `runner.py post-market`: atomically merge into `master_trades.jsonl` (deduplicated by `(trade_id, event, timestamp)`) + normalized summary Parquet/JSON.
  3. Update unified journal reader (#15) to read seamlessly from both `master_trades.jsonl` and active `.staging/` files.

### 8. Hardening the Test Suite & Eliminating Live Process Interference
- **Hard Sandbox Guard in Production Constructors**: When `TRADING_TEST_MODE=1`, raise `RuntimeError` immediately if any test attempts to bind production `/tmp/*.ipc`, `shm_ohlc_221`, or `logs/active_positions.json`.
- **Shared Isolated Test Fixture (`tests/helpers.py`) & `ResourceWarning` Purge**: Provide unique temp/IPC/`shm` fixtures and close all ZMQ sockets/contexts + file descriptors cleanly (specifically fixing the remaining `ResourceWarning: unclosed file '/tmp/test_scoring_shm.shm'` in [`test_scoring_engine_3_0.py`](file:///Users/yorha/Documents/Trading%20Stuff/tests/strategies/test_scoring_engine_3_0.py)).
- **Expanded Test Coverage**: Add regression coverage for CLI `freeze`/`unfreeze`/`stop-live` flags, `MarketSchedule` runtime freeze, `CompositeTrack` sub-track recovery, and `MockGrowwGateway` fault injection.

### 9. Ensure Forward `Nifty 50` & `India VIX` Recording in `equity_ticks.parquet`
- **Observation (`2026-11-26`)**: Sessions from `2026-11-18` and `2026-11-26` contain the `215` F&O cash equities without `Nifty 50` (`NSE_INDEX:Nifty 50`), so Batch Regime analytics currently uses its self-contained 5m F&O synthetic basket + breadth classifier. Verify `NSE_INDEX:Nifty 50` (and India VIX if subscribed) flows cleanly through `TICK.EQ` into [`MarketRecorder`](file:///Users/yorha/Documents/Trading%20Stuff/core/recorder/market_recorder.py) so direct index overlay works automatically on future recordings.

---

## Part 2 — Groww Live Execution Audit & Mock Broker Sandbox

Before allocating real capital to [`LiveSuperstructure`](file:///Users/yorha/Documents/Trading%20Stuff/execution/superstructures/live.py), fix and stress-test the remaining broker-layer hazards:

1. **Multi-Process State File Collision (Severe Architectural Risk)**: Under `runner.py start --mode live`, each track launches in its own OS process (`exec_live_<track>`), yet every `LiveSuperstructure` shares `logs/live_positions_state.json`, polls `reconcile_broker_positions()` against the same Groww account, and writes `logs/live_trades.csv` concurrently. Partition state per process or funnel all live broker calls through a single centralized **`GrowwOrderGateway`** ZeroMQ process.
2. **Upstox Token vs Groww `trading_symbol` Mismatch**: Map Upstox numeric/exchange tokens (`NSE_FO|43210`) cleanly to verified Groww `trading_symbol` strings (`RELIANCE26NOV2500CE`) via `groww_instruments` validation at `prepare` step.
3. **Verified Groww SDK Contract & API Limits**: Validate all `growwapi.GrowwAPI` signatures, enum values (`COMPLETE`, `OPEN`, `TRIGGER_PENDING`, etc.), and API rate limits against the installed SDK version.
4. **Resting SL Order Type & `0.05` Tick Rounding**: Enforce strict `0.05` INR tick rounding on `price` and `trigger_price` and protective slippage limit buffers on `SL` orders.
5. **Naked Position Hazard if Resting SL Attachment Fails**: Add immediate retry with backoff -> emergency market square-off fallback -> operator freeze + critical alert if `_place_resting_sl_order()` fails after `BUY` fill.
6. **Partial Fills & NSE Freeze Quantity Splitting**: Anchor resting SL quantity strictly to broker `filled_quantity` and slice quantities exceeding NSE `freeze_qty`.
7. **Token Expiry & Clock Sync**: Strictly separate `api_key` from daily TOTP `access_token` (`live.py:110` fallback), validate session on startup, and ensure EOD square-off uses IST consistently.
8. **Dedicated Groww Mock Broker Sandbox (`MockGrowwGateway` / `runner.py mock-broker`)**: Simulate live depth fills against `quotes_stream.ipc`, `TRIGGER_PENDING` resting SLs, 25-modification cap, `0.05` tick validation, and fault injection (401 expiry, 5s timeout idempotency, manual mobile closes).

---

## Part 3 — Scoring Engine Refinements & New Alpha Strategies

### A. Remaining Structural Refinements to `ScoringEngine3`
> *Note*: `min_candle_elapsed_sec = 15s`, Top-25% pole breakout buffer (`flag_pole_buffer_pct = 0.25`), `0.45%` 3-bar expansion (`pure_3bar_expansion_pct`), and the `2.0x` ATR-vs-Spread edge gate (`passes_atr_spread_gate`) were promoted in `E10_W3_CAP3` (`TASKS_DONE.md`) and pruned below.

1. **2-Candle Contraction OLS Slope Clamping & VCP Compression Check**: In [`strategies/momentum/scoring_engine.py`](file:///Users/yorha/Documents/Trading%20Stuff/strategies/momentum/scoring_engine.py#L379-L422), 2-bar `np.polyfit` slopes can project triggers too loose or unreachable; clamp OLS slope `m <= 0` for bull flags (`>= 0` for bear flags) and gate contraction range vs ATR (`contr_range <= 0.8 * ATR`).
2. **Configurable Timeframe Beyond 5m (`300.0`s)**: Parameterize hardcoded `300.0` divisors (`scoring_engine.py:387,430,484`) via config/macro `BAR_DURATION_SEC` to support 3m / 5m / 15m A/B tests.
3. **Opening Gap vs Continuous Bar Contamination (`bar_idx == 0`)**: Separate opening `09:15–09:20` bar baseline volume & guard `ATR` against overnight auction gap distortions.

### B. New Alpha Strategies Backtestable Against Our 12+ Recorded Parquet Sessions (`2026-11-09` to `2026-11-26`)
1. **Midday VWAP Mean-Reversion / Exhaustion Fade (`11:30–13:30` Specialist)**: Fade extreme VWAP extensions (`> 1.8 sigma` / `2.5 * ATR`) showing climactic volume (`rVOL >= 2.5x`) and anti-CLV wick rejection during `CHOPPY_RANGE` regimes on `ITM1` options.
2. **Opening Range Compression Breakout & Trap Reclaim (`09:25–10:30` IST)**: Trade tight opening 15m coils (`OR_width < 0.65 * Daily ATR`) or failed ORB sweeps reclaiming VWAP.
3. **Cross-Sectional Relative Strength Leader / Laggard Dispersion**: Rank the 215 F&O equities live by normalized return vs basket median, restricting `CE` entries to top-decile leaders and `PE` entries to bottom-decile laggards.
4. **Option-Microstructure & 5-Level Orderbook Imbalance / Expiry Mode**: Exploit recorded 5-level `bids`/`asks` arrays for depth imbalance filtering and tighter expiry-day strike/depth constraints (`ATM` / `ITM1`).

---

## Part 4 — Codebase Efficiency, Hardening & Operational Lifecycle

### 1. [CRITICAL] `FeedHandler` Half-Open Socket Detection, Startup Retry & Unbuffered Reconnect Logging
- **Current State (`core/feed/feed_handler.py:996-1121`)**:
  1. Line `1020` sets `ping_interval=None` and line `1040` awaits `ws.recv()` without `asyncio.wait_for` timeout, risking silent indefinite hangs on half-open Wi-Fi/NAT drops.
  2. Line `996` calls `get_authorized_ws_url()` once before launching `dual_runner()` (fatal exit if network is transiently down on boot).
  3. Reconnect prints omit `flush=True` (invisible under buffered stdout logging) and should refresh `ProtectedPositionRegistry` keys upon reconnect.
- **Required Fix**: Set `ping_interval=20, ping_timeout=10`, wrap `ws.recv()` with a market-hours watchdog timeout, move WS auth retry inside `ws_connection_worker`, add unbuffered structured logging, and publish stale-feed telemetry.

### 2. Fast Shard-Based `MarketRecorder`, Explicit Collation CLI & Web UI Tasks Tab
- Replace the $O(N^2)$ `read_parquet + pl.concat + rewrite` loop in [`MarketRecorder`](file:///Users/yorha/Documents/Trading%20Stuff/core/recorder/market_recorder.py#L163-L214) (`MED-04`) with `<5ms` immutable part-file shards (`*_part_0001.parquet`) collated cleanly on exit / `runner.py collate --date YYYY-MM-DD`, plus a **Web UI Tasks Tab** exposing pre-market and post-market actions.

### 3. Streamlined `ColumnarSimEngine` Internal Layout & Warm Workers
- *Remaining after `4761ebb` / `96f0c51`*: Replace the ~10,000 `partition_by` sub-DataFrames + ~60,000 NumPy array allocations in [`_build_options_index`](file:///Users/yorha/Documents/Trading%20Stuff/simulation/columnar_sim_engine.py#L438-L494) with a single sort + 6 flat contiguous NumPy arrays indexed by `(start_idx, end_idx)` slice bounds; remove duplicate `ITM`/`ITM1` & `OTM`/`OTM1` lookups (`5` binary searches vs `7`); convert per-tick `datetime.fromtimestamp()` calls in the inner loop to integer IST epoch math; reuse shared memory buffers across Web UI worker runs; and verify `_run_direct_option_simulation` synthetic fallback reads config capital cleanly (`MED-08`).

### 4. `CandleManager`, `ScoringEngine3`, `BaseTrack` & `LiveService` Hot-Path Speedups
- **`CandleManager`**: Precompute `(221, 75)` 5m baseline volume sums at `__init__`, replace per-tick `datetime` creation with integer IST arithmetic, and cache `raw_token -> sym_idx`.
- **`ScoringEngine3`**: Track `last_seen_seq[idx]` per symbol to skip unchanged SHM rows (~80–95% of 200 Hz idle spin work) and cache recent-pole flags across bars.
- **`BaseTrack` & Superstructures**: Maintain `O(1)` `trades_by_token` / `trades_by_underlying` indices, attach read-only to `shm_ohlc_221` for underlying equity LTP updates instead of empty `QuotesBroker` equity subscriptions, and evaluate an optional per-mode multi-track worker mode.
- **`LiveService` & WebSocket Transport**: Tail journals incrementally via byte offsets instead of re-reading from byte `0`, coalesce parquet LVC fallback into one cooldown-gated `group_by("token").last()`, and skip idle WebSocket JSON broadcasts via a lightweight `state_version` hash.

### 5. Canonical Shared Helpers, Exception Hygiene & Modular `cli/`
- **Single Canonical Strike Resolver (`common/options/strike_resolver.py`)**: Deduplicate strike lookup/liquidity gates across `RMSService` and `ColumnarSimEngine` with strict zero-fabrication enforcement.
- **Single Canonical Journal Reader (`common/journals/reader.py`)**: Unify journal folding across `BaseSuperstructure`, `LiveService`, `HistoricalService`, and `runner.py report`.
- **Eliminate Silent `except Exception: pass` Handlers**: Audit and replace silent passes with rate-limited structured logging and atomic per-component health/drop counters exposed in the Web UI Admin Health panel.
- **Modular `cli/` Package & Macro Constants**: Split `runner.py` commands into `cli/` submodules shared with Web UI Tasks/Admin, hoist domain magic constants (`300.0` in `scoring_engine.py`, fallback `60.0` brokerage in `live_service.py` / `historical_service.py`, `0.05` tick size) to top-level `UPPER_CASE` module macros or `ConfigManager`.

---

## Part 5 — Carryover Items from Archived Audit (`2026-11-25_SYSTEM_AUDIT_2_0.md`)

All `CRIT-01..05`, `HIGH-01..07`, and most `MED` items from [`2026-11-25_SYSTEM_AUDIT_2_0.md`](file:///Users/yorha/Documents/Trading%20Stuff/docs/archive/2026-11-25_SYSTEM_AUDIT_2_0.md) were resolved and logged in [`TASKS_DONE.md`](file:///Users/yorha/Documents/Trading%20Stuff/docs/live/TASKS_DONE.md). Only these targeted low/medium items remain:

| ID | Component | Location | Remaining Action |
|---|---|---|---|
| **MED-03 (p4)** | `QuotesBroker` | [`core/quotes/quotes_broker.py:61`](file:///Users/yorha/Documents/Trading%20Stuff/core/quotes/quotes_broker.py#L61) | Prune evicted / stale option tokens from `self._lvc` periodically so dict cardinality stays bounded across dynamic strike rolls. |
| **MED-09 (rem)** | `CandleService` & `LiveService` | [`candle_service.py:55`](file:///Users/yorha/Documents/Trading%20Stuff/web/candle_service.py#L55), [`live_service.py:49`](file:///Users/yorha/Documents/Trading%20Stuff/web/live_service.py#L49) | Tighten `CandleService` `token OR symbol` filter precedence (`token` match first, fallback `symbol`) and make `LiveService.start()` idempotent if called twice. |
| **DEAD-001..003** | `FeedHandler` | [`core/feed/feed_handler.py:210`](file:///Users/yorha/Documents/Trading%20Stuff/core/feed/feed_handler.py#L210), [`L909`](file:///Users/yorha/Documents/Trading%20Stuff/core/feed/feed_handler.py#L909) | Remove legacy `_active_ws` property, unused `_run_poll_stream` stub, and unused imports left after dual-WebSocket migration. |
| **DOC-DRIFT** | `ARCHITECTURE_SPEC_2_0.md` | [`ARCHITECTURE_SPEC_2_0.md`](file:///Users/yorha/Documents/Trading%20Stuff/docs/sys/ARCHITECTURE_SPEC_2_0.md) | Clarify that `/dev/shm` L2-cache semantics apply on Linux (`FORCE_POSIX_SHM=1`) vs macOS `/tmp/*.shm` mmap page cache, and document `1s` mtime polling lag in file-backed `ProtectedPositionRegistry`. |
