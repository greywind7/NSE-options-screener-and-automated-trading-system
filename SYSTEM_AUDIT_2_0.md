# System Audit Report 2.0 — Antigravity Trading Platform
**Date**: 2026-11-25 | **Auditor**: Antigravity (Gemini 4 Hydrogen)  
**Baseline**: Working tree on `master` (post-Feed-Manager & Pricing-Invariant hotfixes)  
**Test Suite Status**: `134/134` passing (`runner.py test`, 8.12s) | JS Syntax: `7/7` passing (`node --check`)

---

## Executive Summary

A full-codebase, line-by-line audit was conducted across all tiers: Ingestion (`core/feed/`), Shared Memory & Candles (`core/candles/`), Ticker Plant (`core/quotes/`), Risk & Strike Resolution (`core/rms/`), Market Recorder (`core/recorder/`), Scoring 3.0 (`strategies/momentum/`), Execution Tracks (`common/tracks/`), Superstructures (`execution/superstructures/`), Columnar Simulation (`simulation/`), Web Services & SPA (`web/`), and CLI Orchestration (`runner.py`).

### Key Takeaways
1. **Verified Fixed from Previous Audit (`SYSTEM_AUDIT.md` / `AUDIT_TODO.md`)**:
   - Feed Manager dual-WS persistent event loop, TLS verification, CRC32 sharding, capacity pruning, and `max_stream_underlyings_with_options: 221` are **fixed and verified**.
   - `CandleManager.warmup_from_parquet` (`BUG-CM-001`) already uses `self.symbol_to_index` (no `AttributeError`).
   - `CandleManager` seqlock (`BUG-CM-004`) is **per-symbol** (`headers` shape `(221,)`), so previous-bar finalization writes are already bracketed by the symbol's seqlock.
   - `ScoringEngine3.stop()` (`BUG-SE-001`) already sets `self.running = False` before `self.shm.close()`.
   - `web/server.py` `handle_sim_run` (`BUG-014`) already sets `self.simulation_service.is_running = True` before `asyncio.create_task(...)`.
2. **Critical Regressions & Newly Discovered Bugs**:
   - **Zero-Price Fabrication Leaks**: `FeedHandler.publish_tick()` undoes its own protobuf zero-price guard via `bid=float(bid or ltp)` / `ask=float(ask or ltp)` (since `0.0 or ltp == ltp`). Similar `ltp`/`entry_price` fallbacks remain in `QuotesBroker.on_tick()`, `BaseSuperstructure.start()`, `LiveSuperstructure.reconcile_broker_positions()`, `LiveService`, `HistoricalService`, and `SimulationService`.
   - **Broken Underlying Tick Routing (`StructuralConvexityTrack`)**: `BaseTrack.on_opportunity()` never populates `metadata["underlying_symbol"]`, so `BaseTrack.on_tick()` never dispatches `on_underlying_tick()` in live/paper execution.
   - **Silent Heartbeat Timeout Misses (v2 Tracks)**: `BaseTrack.on_heartbeat()` whitelists only v1 timeout strings, silently ignoring all 5 v2 timeout/cutoff reasons (`MORNING_SESSION_CUTOFF`, `VELOCITY_TIMEOUT`, `DECELERATION_TIMEOUT`, `MIDDAY_CONSOLIDATION_TIMEOUT`, `TIMEOUT`).
   - **Broken CLI Controls (`runner.py`)**: `freeze`/`unfreeze`/`stop-live` write flag files and signal `strategy.pid`, but neither `runner.py` nor `BaseSuperstructure` reads `.freeze_*` files or handles `SIGUSR1`/`SIGUSR2`. `cmd_start_v2` also drops `--confirm-live`, `--capital`, and `--eod-*` flags.

---

## 1. Critical Findings (P0 — Fix Before Live Capital)

### 🔴 CRIT-01: `FeedHandler.publish_tick()` Re-Introduces Option Bid/Ask Fabrication
- **Location**: [core/feed/feed_handler.py:787-788](file:///Users/yorha/Documents/Trading%20Stuff/core/feed/feed_handler.py#L787-L788), [core/feed/feed_handler.py:1063](file:///Users/yorha/Documents/Trading%20Stuff/core/feed/feed_handler.py#L1063)
- **Detail**: The protobuf parser (lines 1063–1081) carefully sets `bid = 0.0`, `ask = 0.0` for options lacking book depth. However, `publish_tick()` constructs `OptionDepthTickEvent` with:
  ```python
  bid=float(bid or ltp),
  ask=float(ask or ltp),
  ```
  In Python, `0.0 or ltp` evaluates to `ltp`, completely bypassing the zero-fabrication guard. Additionally, in `fullFeed -> marketFF` (line 1063), `is_opt` is only assigned *inside* `if m.marketLevel and m.marketLevel.bidAskQuote:`, causing an `UnboundLocalError` on line 1096 when `bidAskQuote` is empty (silently swallowed at line 1106).
- **Fix**: Initialize `is_opt = is_option_contract(canonical_token)` before the `feed_union` branch, and in `publish_tick()` pass `bid=float(bid)` and `ask=float(ask)` directly for options (`is_opt=True`).

### 🔴 CRIT-02: Zero-Price Fallbacks Remain Across QuotesBroker, Superstructures, and Web Services
- **Locations**:
  - [core/quotes/quotes_broker.py:102-103](file:///Users/yorha/Documents/Trading%20Stuff/core/quotes/quotes_broker.py#L102-L103): `bid = float(tick.get("bid", ltp))`, `ask = float(tick.get("ask", ltp))` (also crashes with `TypeError` if `bid` is `None`).
  - [execution/superstructures/base_superstructure.py:436-437](file:///Users/yorha/Documents/Trading%20Stuff/execution/superstructures/base_superstructure.py#L436-L437): `bid = float(data.get("bid", ltp))`, `ask = float(data.get("ask", ltp))`.
  - [execution/superstructures/live.py:1200-1201](file:///Users/yorha/Documents/Trading%20Stuff/execution/superstructures/live.py#L1200-L1201): Ghost position reconciliation sets `if exit_p <= 0: exit_p = ep`.
  - [web/live_service.py:112-113](file:///Users/yorha/Documents/Trading%20Stuff/web/live_service.py#L112-L113), [L256-257](file:///Users/yorha/Documents/Trading%20Stuff/web/live_service.py#L256-L257), [L433](file:///Users/yorha/Documents/Trading%20Stuff/web/live_service.py#L433), [L448](file:///Users/yorha/Documents/Trading%20Stuff/web/live_service.py#L448), [L523](file:///Users/yorha/Documents/Trading%20Stuff/web/live_service.py#L523): Multiple `bid or ltp`, `or entry_price`, and `float(tr.get("exit_price") or entry_p)` fallbacks (turns `0.0` exit prices into 0% breakeven).
  - [web/historical_service.py:175](file:///Users/yorha/Documents/Trading%20Stuff/web/historical_service.py#L175), [L365](file:///Users/yorha/Documents/Trading%20Stuff/web/historical_service.py#L365): `exit_p = float(tr.get("exit_price") or entry_p)`.
  - [web/simulation_service.py:693](file:///Users/yorha/Documents/Trading%20Stuff/web/simulation_service.py#L693): `exit_p = float(tr.get("exit_price") or entry_p)`.
- **Fix**: Replace `or entry_p` with explicit `None` checks (`float(tr["exit_price"]) if tr.get("exit_price") is not None else 0.0`), and default missing option `bid`/`ask` to `0.0` instead of `ltp`.

### 🔴 CRIT-03: `StructuralConvexityTrack.on_underlying_tick()` Is Dead in Live/Paper Mode
- **Location**: [common/tracks/base_track.py:257-276](file:///Users/yorha/Documents/Trading%20Stuff/common/tracks/base_track.py#L257-L276), [common/tracks/base_track.py:325-339](file:///Users/yorha/Documents/Trading%20Stuff/common/tracks/base_track.py#L325-L339)
- **Detail**: `BaseTrack.on_tick()` checks `und_sym = (t.metadata.get("underlying_symbol") or "")` to detect underlying equity ticks and invoke `self.on_underlying_tick(t, ltp=ltp, ts=ts)`. However, `BaseTrack.on_opportunity()` never writes `"underlying_symbol": opp.symbol` into `trade.metadata`. Thus `und_sym` is always `""` and `on_underlying_tick()` is never called in live/paper execution. (Additionally, `QuotesBroker` only receives `TICK.OPT` from `FeedHandler`, so underlying equity subscriptions via `subscribe_quotes([opp.symbol])` never receive equity ticks from `QuotesBroker` anyway.)
- **Fix**: Populate `"underlying_symbol": opp.symbol` in `BaseTrack.on_opportunity()`, and ensure equity ticks (or SHM LTP polling) reach `BaseSuperstructure` for active trades.

### 🔴 CRIT-04: `BaseTrack.on_heartbeat()` Silently Ignores All v2 Timeout/Cutoff Reasons
- **Location**: [common/tracks/base_track.py:404](file:///Users/yorha/Documents/Trading%20Stuff/common/tracks/base_track.py#L404)
- **Detail**: `on_heartbeat()` only triggers exits when `reason` is in `("STAGNATION_TIMEOUT", "FIXED_STAGNATION_TIMEOUT", "PULLBACK_STAGNATION_TIMEOUT", "MAX_HOLD_TIMEOUT", "PULLBACK_TIMEOUT")`. All v2 tracks use different reason strings that are ignored on heartbeat:
  - `OpeningThrustConvexityTrack`: `"MORNING_SESSION_CUTOFF"` (10:45 IST hard cutoff)
  - `DeltaScalpTrack`: `"VELOCITY_TIMEOUT"`
  - `VolatilityExpansionTrack`: `"DECELERATION_TIMEOUT"`
  - `AdaptiveRegimeTrack`: `"MIDDAY_CONSOLIDATION_TIMEOUT"`
  - `TrendPullbackV2Track`: `"TIMEOUT"`
- **Fix**: Include all v2 timeout/cutoff reason strings in `on_heartbeat()` (or have `evaluate_exit_rules` return a structured/recognized timeout flag).

### 🔴 CRIT-05: `runner.py` `freeze` / `unfreeze` / `stop-live` & `start` Flags Are Disconnected
- **Location**: [runner.py:448-455](file:///Users/yorha/Documents/Trading%20Stuff/runner.py#L448-L455), [runner.py:970-1094](file:///Users/yorha/Documents/Trading%20Stuff/runner.py#L970-L1094), [execution/superstructures/base_superstructure.py:246-255](file:///Users/yorha/Documents/Trading%20Stuff/execution/superstructures/base_superstructure.py#L246-L255)
- **Detail**:
  1. `runner.py freeze` creates `logs/.freeze_<mode>` and sends `SIGUSR1` to `logs/strategy.pid` (the supervisor PID), but neither `runner.py` nor `BaseSuperstructure` handles `SIGUSR1`/`SIGUSR2` or checks `logs/.freeze_*` files in `is_frozen()`.
  2. `cmd_start_v2` ignores `--confirm-live`, `--capital`, `--eod-time`, `--eod-freeze-time`, and `--config` when spawning `Paper`/`Delayed`/`LiveSuperstructure` subprocesses. Notably, `--mode live --confirm-live` still runs `LiveSuperstructure(live_confirmed=False)`.
- **Fix**: Check `logs/.freeze_all` and `logs/.freeze_{self.mode}` in `BaseSuperstructure.is_frozen()` (with `stat` caching or `SIGUSR1`/`SIGUSR2` forwarding), and pass CLI overrides (`live_confirmed`, `capital`, `eod_*`) in `cmd_start_v2`.

---

## 2. High-Severity Findings (P1)

### 🟠 HIGH-01: `--continue` Recovery Fails to Restore Track Capital & Overwrites Immutable Fields
- **Location**: [execution/superstructures/base_superstructure.py:154-215](file:///Users/yorha/Documents/Trading%20Stuff/execution/superstructures/base_superstructure.py#L154-L215)
- **Detail**: `_recover_state_from_journals()` restores `track.active_trades` and `track.closed_trades` and calls `self.risk_mgr.update_pnl(trade.realized_pnl)`, **but**:
  - Never calls `track.commit_capital(trade.entry_ask * trade.quantity)` for recovered `OPEN` trades.
  - Never restores `track.realized_pnl` from recovered `CLOSED` trades.
  - Uses unrestricted `trades_map[trade_id].update(data)`, allowing later events to overwrite `entry_price` or `quantity`.
- **Fix**: Restore `track.commit_capital(...)` and `track.realized_pnl`, and apply the immutable-field whitelist during journal replay.

### 🟠 HIGH-02: Journal Parsers Overwrite Immutable Entry Fields & Misreport Overall P&L
- **Locations**: [web/live_service.py:316-321](file:///Users/yorha/Documents/Trading%20Stuff/web/live_service.py#L316-L321), [L398-401](file:///Users/yorha/Documents/Trading%20Stuff/web/live_service.py#L398-L401); [web/historical_service.py:136-138](file:///Users/yorha/Documents/Trading%20Stuff/web/historical_service.py#L136-L138), [L301-306](file:///Users/yorha/Documents/Trading%20Stuff/web/historical_service.py#L301-L306)
- **Detail**: Confirmed from `AUDIT_TODO.md` (`TASK-05`, `TASK-12`): both journal parsers overwrite `entry_price`/`lot_size`/`quantity` on subsequent events, and both `overall` summaries divide `tot_net` and `tot_gross` by `n_active`, displaying per-track average as `"net_pnl"`.

### 🟠 HIGH-03: `RMSService` Ignores `config/strategies.json` Spread Gate & Hardcodes `"26NOV"` Expiry
- **Location**: [core/rms/rms_service.py:62](file:///Users/yorha/Documents/Trading%20Stuff/core/rms/rms_service.py#L62), [core/rms/rms_service.py:180](file:///Users/yorha/Documents/Trading%20Stuff/core/rms/rms_service.py#L180)
- **Detail**:
  1. `rms_service.py` reads `risk_cfg.get("spread_gate", {"max_spread_pct": 0.03, "min_spread_abs": 0.50})`, whereas `config/strategies.json` defines `max_spread_pct` and `min_absolute_spread_inr` directly under `risk` (no `spread_gate` sub-dict). Config edits are silently ignored.
  2. `expiry_tag = "26NOV"` is hardcoded on line 180 for fallback symbol construction.
- **Fix**: Read `max_spread_pct` and `min_absolute_spread_inr` directly from `risk_cfg` (with `spread_gate` fallback), and compute `expiry_tag` dynamically from the nearest Thursday (or config/manifest).

### 🟠 HIGH-04: `AdaptiveRegimeTrack` & `TrendPullbackV2Track` Mutate Instance `target_pct`/`sl_pct`
- **Location**: [common/tracks/adaptive_regime.py:62-70](file:///Users/yorha/Documents/Trading%20Stuff/common/tracks/adaptive_regime.py#L62-L70), [common/tracks/trend_pullback_v2.py:54](file:///Users/yorha/Documents/Trading%20Stuff/common/tracks/trend_pullback_v2.py#L54), [common/tracks/base_track.py:455-456](file:///Users/yorha/Documents/Trading%20Stuff/common/tracks/base_track.py#L455-L456)
- **Detail**: Both tracks mutate `self.target_pct` / `self.sl_pct` inside `on_opportunity()`. In `paper_delayed` or `live` mode, `BaseTrack.on_order_result()` fires asynchronously later and recalculates `trade.target_price` and `trade.stop_loss` using `self.target_pct` and `self.sl_pct`. A concurrent opportunity on another symbol or across a regime boundary overwrites those instance attributes before the first trade fills.
- **Fix**: Store per-trade `target_pct` and `sl_pct` in `trade.metadata` and read them in `on_order_result()`.

### 🟠 HIGH-05: `ColumnarSimEngine` Delayed Mode Skips Capital Delta Adjustment
- **Location**: [simulation/columnar_sim_engine.py:683-709](file:///Users/yorha/Documents/Trading%20Stuff/simulation/columnar_sim_engine.py#L683-L709)
- **Detail**: Unlike `DelayedSuperstructure._process_queue()` (which checks `capital_delta > 0 and track.available_capital < capital_delta` and adjusts `commit_capital`/`release_capital`), `ColumnarSimEngine` Step C fills delayed orders without adjusting committed capital for price movement during the delay window.

### 🟠 HIGH-06: `LiveSuperstructure` Dual-Thread Reconciliation Race & Hardcoded Defaults
- **Location**: [execution/superstructures/live.py:72-75](file:///Users/yorha/Documents/Trading%20Stuff/execution/superstructures/live.py#L72-L75), [L1528-1541](file:///Users/yorha/Documents/Trading%20Stuff/execution/superstructures/live.py#L1528-L1541)
- **Detail**: Both `_order_polling_worker` (background thread) and `on_heartbeat` (main ZMQ thread) call `reconcile_broker_positions()` without mutual exclusion around the API call, risking duplicate concurrent Groww API calls. Also, `initial_capital=100_000.0` and `brokerage_per_order=20.0` are hardcoded in `__init__` instead of reading `config/execution.json` (`250_000.0` / `30.0`).

### 🟠 HIGH-07: `live_tab.js` Null `exit_price` Crash
- **Location**: [web/static/js/live_tab.js:300](file:///Users/yorha/Documents/Trading%20Stuff/web/static/js/live_tab.js#L300)
- **Detail**: `tr.exit_price.toFixed(2)` throws `TypeError` when `exit_price` is `null`.

---

## 3. Medium-Severity Findings (P2)

| ID | Component | Location | Issue |
|---|---|---|---|
| **MED-01** | `CandleManager` | [candle_manager.py:403](file:///Users/yorha/Documents/Trading%20Stuff/core/candles/candle_manager.py#L403), [L271](file:///Users/yorha/Documents/Trading%20Stuff/core/candles/candle_manager.py#L271) | Uses `received_timestamp` instead of `exchange_timestamp` for bar alignment; sets `active_bar["timestamp"]` to first tick's timestamp instead of the aligned 5m boundary (`09:15:00`, `09:20:00`, etc.), shifting `ScoringEngine3` elapsed calculations. |
| **MED-02** | `CandleManager` | [candle_manager.py:325-332](file:///Users/yorha/Documents/Trading%20Stuff/core/candles/candle_manager.py#L325-L332) | `calculate_rvol()` reads `vol_at_min_edges[idx, min_offset - 1]`; if a stock had no ticks in the previous minute, `vol_at_min_edges` is `0`, attributing the entire bar's volume to the current minute and inflating extrapolated rVOL. |
| **MED-03** | `QuotesBroker` | [quotes_broker.py:159-163](file:///Users/yorha/Documents/Trading%20Stuff/core/quotes/quotes_broker.py#L159-L163), [L216-220](file:///Users/yorha/Documents/Trading%20Stuff/core/quotes/quotes_broker.py#L216-L220), [L256](file:///Users/yorha/Documents/Trading%20Stuff/core/quotes/quotes_broker.py#L256) | (1) `frames[1] == b""` delimiter not validated; (2) `UNSUBSCRIBE` with an unrecognized `client_id` pops another client's subscription; (3) `_heartbeat_loop` serializes full `ref_counts` every 1s; (4) `_lvc` never prunes evicted options. |
| **MED-04** | `MarketRecorder` | [market_recorder.py:163-168](file:///Users/yorha/Documents/Trading%20Stuff/core/recorder/market_recorder.py#L163-L168), [L209-214](file:///Users/yorha/Documents/Trading%20Stuff/core/recorder/market_recorder.py#L209-L214) | Every flush reads the entire existing `equity_ticks.parquet` / `option_depth.parquet`, concatenates, and rewrites the whole file ($O(N^2)$ disk I/O over the session, blocking the ZMQ subscriber loop). |
| **MED-05** | `ProtectedPositionRegistry` | [position_registry.py:266-318](file:///Users/yorha/Documents/Trading%20Stuff/core/feed/position_registry.py#L266-L318) | Cross-process read-modify-write on `logs/active_positions.json` lacks `fcntl.flock`, risking lost updates if two superstructures open/close positions concurrently within the same mtime granularity; 1s mtime lag on `FeedHandler` reads. |
| **MED-06** | `BaseTrack` | [base_track.py:451-456](file:///Users/yorha/Documents/Trading%20Stuff/common/tracks/base_track.py#L451-L456), [L436](file:///Users/yorha/Documents/Trading%20Stuff/common/tracks/base_track.py#L436) | `on_order_result()` does not reset/clamp `highest_price` when `fill_price != entry_ask`; `exit_trade()` only unsubscribes `[trade.token]` and leaks the underlying equity subscription from line 286. |
| **MED-07** | `TrackFactory` | [common/tracks/factory.py:524](file:///Users/yorha/Documents/Trading%20Stuff/common/tracks/factory.py#L524), [L543](file:///Users/yorha/Documents/Trading%20Stuff/common/tracks/factory.py#L543), [L568-570](file:///Users/yorha/Documents/Trading%20Stuff/common/tracks/factory.py#L568-L570) | Composite sub-track instantiation omits `eff_brokerage`; duplicate `return tracks` at EOF. |
| **MED-08** | `ColumnarSimEngine` | [columnar_sim_engine.py:430](file:///Users/yorha/Documents/Trading%20Stuff/simulation/columnar_sim_engine.py#L430), [L789](file:///Users/yorha/Documents/Trading%20Stuff/simulation/columnar_sim_engine.py#L789) | `np.searchsorted` assumes sorted `ts_arr` without validating monotonicity; `_run_direct_option_simulation` hardcodes `start_capital=250000.0` and falls back to `ltp`. |
| **MED-09** | `Web / Sim / Alerts` | [simulation_service.py:53](file:///Users/yorha/Documents/Trading%20Stuff/web/simulation_service.py#L53), [L194](file:///Users/yorha/Documents/Trading%20Stuff/web/simulation_service.py#L194); [candle_service.py:46](file:///Users/yorha/Documents/Trading%20Stuff/web/candle_service.py#L46); [live_service.py:49](file:///Users/yorha/Documents/Trading%20Stuff/web/live_service.py#L49), [L463](file:///Users/yorha/Documents/Trading%20Stuff/web/live_service.py#L463), [L587](file:///Users/yorha/Documents/Trading%20Stuff/web/live_service.py#L587) | Worker re-reads Parquet from disk; `compute_param_hash` omits `track_profile`; `CandleService` OR-filter across token/symbol; `LiveService.start()` unguarded, `start_budget` hardcoded, `_build_intraday_curve` $O(N^2)$. |
| **MED-10** | `runner.py` | [runner.py:928-935](file:///Users/yorha/Documents/Trading%20Stuff/runner.py#L928-L935) | `cmd_report` reads flat JSONL keys (`t.get("entry_price")`), failing on `BaseSuperstructure` nested `{"event": ..., "data": {...}}` records. |
| **MED-11** | `Web Frontend` | [app.js:64](file:///Users/yorha/Documents/Trading%20Stuff/web/static/js/app.js#L64), [L358](file:///Users/yorha/Documents/Trading%20Stuff/web/static/js/app.js#L358); [simulation_tab.js:778](file:///Users/yorha/Documents/Trading%20Stuff/web/static/js/simulation_tab.js#L778), [L848](file:///Users/yorha/Documents/Trading%20Stuff/web/static/js/simulation_tab.js#L848) | `connectWebSocket()` does not close prior WS; `openTradeModal` does not reset `tamCurrentRange = "local"`; `startBatchSimulation` uses `RUN-${this.runs.length + 1}`; `pollStatus` lacks max-poll timeout. |

---

## 4. Low-Severity, Config & Doc Drift Findings (P3)

- **Config Duplication**: [config/strategies.json:130-172](file:///Users/yorha/Documents/Trading%20Stuff/config/strategies.json#L130-L172) defines `composite_split_OTM` twice.
- **Hardcoded Constants**: `300.0` bar seconds in [scoring_engine.py:364](file:///Users/yorha/Documents/Trading%20Stuff/strategies/momentum/scoring_engine.py#L364), [L455](file:///Users/yorha/Documents/Trading%20Stuff/strategies/momentum/scoring_engine.py#L455); `brokerage=60.0` in `live_service.py` / `historical_service.py`; `start_budget=250000.0` in `live_service.py` / `historical_service.py`.
- **AlertService SHM Churn**: [web/alert_service.py:227](file:///Users/yorha/Documents/Trading%20Stuff/web/alert_service.py#L227) opens and closes `shm_ohlc_221` on every request instead of caching the handle.
- **CandleService FIFO Cache**: [web/candle_service.py:102](file:///Users/yorha/Documents/Trading%20Stuff/web/candle_service.py#L102) evicts by insertion order instead of LRU.
- **Test ResourceWarnings**: `tests/strategies/test_scoring_engine_3_0.py` emits `ResourceWarning` for unclosed ZMQ sockets/contexts and `/tmp/test_scoring_shm.shm`.
- **Doc Drift**: `AGENT_CONTEXT.md` §4.1 references `config/tracks/tracks_v1.json` / `tracks_v2.json` / `tracks_default.json`, whereas actual files are `config/tracks/v1.json`, `v2.json`, and `default.json`. `README.md` §2 diagram still shows a single WebSocket and `(CSV)` trade journals.

---

## 5. Recommended Execution Order

1. **Batch 1 — Zero-Price & Core Signal Integrity (`CRIT-01`, `CRIT-02`, `CRIT-03`, `CRIT-04`)**:
   - Fix `FeedHandler.publish_tick()` `bid or ltp` & `is_opt` initialization.
   - Purge remaining `ltp` / `entry_price` fallbacks in `QuotesBroker`, `BaseSuperstructure`, `LiveSuperstructure`, `LiveService`, `HistoricalService`, and `SimulationService`.
   - Fix `BaseTrack.on_opportunity()` `underlying_symbol` metadata and `BaseTrack.on_heartbeat()` v2 timeout/cutoff reasons.
2. **Batch 2 — Risk, Capital & Live Safety (`CRIT-05`, `HIGH-01`..`HIGH-07`)**:
   - Wire `logs/.freeze_*` into `BaseSuperstructure.is_frozen()` and pass `--confirm-live` / `--capital` / `--eod-*` in `runner.py`.
   - Fix `--continue` capital/PnL restoration and immutable field whitelists in journal parsers.
   - Fix `RMSService` config key mismatch + dynamic expiry tag; fix `AdaptiveRegimeTrack`/`TrendPullbackV2Track` per-trade `target_pct`/`sl_pct`; fix `ColumnarSimEngine` delayed capital delta; fix `LiveSuperstructure` reconciliation lock & config defaults; fix `live_tab.js` null guard and overview `net_pnl` totals.
3. **Batch 3 — Performance, Concurrency & Polish (`MED-01`..`MED-11`, `P3`)**:
   - `CandleManager` `exchange_timestamp` & bar alignment; `QuotesBroker` delimiter/unsub/heartbeat; `MarketRecorder` part-file or incremental flush; `ProtectedPositionRegistry` `flock`; Web UI & `runner.py` cleanup.
