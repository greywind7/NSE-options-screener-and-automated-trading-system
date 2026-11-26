# Master Completed Tasks Journal (`TASKS_DONE.md`)

Chronological log of all completed engineering tasks, refactors, fixes, and feature rollouts across the Antigravity Trading Platform.

---

## 2026-11-26 — Batch Simulation Reports, Dual 1:1 Square Charts & Market Regime Analytics (`IMPROVEMENTS #2`)

Completed all deliverables for Batch Simulation Analytics alongside additive UX refinements (`154/154` tests passing):

- **Market Regime Classifier ([`web/regime_classifier.py`](file:///Users/yorha/Documents/Trading%20Stuff/web/regime_classifier.py))**:
  - Builds a **5-minute equal-weighted F&O synthetic basket & `% Advancing` breadth index** directly from recorded `equity_ticks.parquet` (~215 equities), eliminating strict dependence on `Nifty 50` (while recording `nifty_return_pct` when present).
  - Deterministically labels sessions (`TREND_UP`, `TREND_DOWN`, `CHOPPY_RANGE`, `V_REVERSAL`, `HIGH_VOL_EXPIRY`) via return, efficiency ratio, realized volatility, reversal swing, and Thursday expiry flag, plus intraday windows (`OPENING` `09:15–11:30`, `MIDDAY` `11:30–13:30`, `CLOSING` `13:30–15:20`), cached in memory per date.
- **Cross-Session Batch Analytics Engine ([`web/batch_analytics.py`](file:///Users/yorha/Documents/Trading%20Stuff/web/batch_analytics.py))**:
  - Aggregates per-`(track, mode)` Net/Gross P&L, Return %, Brokerage Drag (% GP), Expectancy (`₹/trade`), Win/Flat/Loss rates, Profit Factor, Payoff Ratio, Max Drawdown (`%` & `₹` across EOD daily + trade-sequence curves), Calmar Ratio, Daily Win Rate (`green_days/sessions`), Ulcer Index, Worst-Day Loss, Max Consecutive Losses, Daily P&L CV, and Annualized Sharpe.
  - Added **45° Linear Slope Consistency (`linearity_score`, `slope_deviation_pct`)** separating smooth low-variance equity curves (`most_consistent`) from raw green-day counts (`max_green_days`).
  - Pairs `paper` vs `paper_delayed` modes per track for `delayed_retention_pct`, `slippage_cost_inr`, and `latency_resilience` tier, plus Multi-Batch comparison (`build_batch_comparison_summary`) and Markdown/CSV exporters.
- **Backend Wiring & REST APIs ([`web/simulation_service.py`](file:///Users/yorha/Documents/Trading%20Stuff/web/simulation_service.py), [`web/server.py`](file:///Users/yorha/Documents/Trading%20Stuff/web/server.py))**:
  - Added `batches_history` registry (auto-populated during batch runs and cleared in `cleanup()`), `GET/POST /api/sim/batches`, `GET /api/sim/batch/{batch_id}` (including virtual `__all__` and `regime`/`window`/`rank_by` filters), `GET /api/sim/batch/{batch_id}/export?format=md|csv`, and `GET /api/sim/regimes`.
- **Dual 1:1 Square Charts & Unified Leaderboard UI ([`web/static/js/chart_engine.js`](file:///Users/yorha/Documents/Trading%20Stuff/web/static/js/chart_engine.js), [`web/static/js/simulation_tab.js`](file:///Users/yorha/Documents/Trading%20Stuff/web/static/js/simulation_tab.js), [`web/static/index.html`](file:///Users/yorha/Documents/Trading%20Stuff/web/static/index.html), [`web/static/css/style.css`](file:///Users/yorha/Documents/Trading%20Stuff/web/static/css/style.css))**:
  - Two side-by-side **1:1 square** canvas charts: Left **`BatchWinnersChart`** (deduplicated spotlight winners plotted with fixed per-metric colors and alternating multi-color dashes when a track wins multiple categories) + Right **`BatchEquityDrawdownChart`** (track/portfolio equity + underwater drawdown sub-panel + Delayed overlay).
  - Unified **`Track Rankings & Performance Leaderboard`** combining metric pills (`Net P&L`, `45° Slope`, `Max Green %`, `Calmar`, `Profit Factor`, `Lowest MaxDD`, `Exp / Trade`, `Delayed Ret %`, `Sharpe`) and `All / Paper / Paper Delayed` mode separation, paired with **Session-by-Session P&L Attribution** (clicking any date jumps to its Single Run Report; clicking `Opening`/`Midday`/`Closing` chips filters by entry window).
- **Verification**: Unit & aiohttp endpoint suite in [`tests/ui/test_batch_analytics.py`](file:///Users/yorha/Documents/Trading%20Stuff/tests/ui/test_batch_analytics.py), JS checks (`node --check`), and updated [`docs/sys/WEB_VIEW_FLOW.md`](file:///Users/yorha/Documents/Trading%20Stuff/docs/sys/WEB_VIEW_FLOW.md).

---

## 2026-11-26 — `E10_W3_CAP3` Promotion, Track Retirement & Docs Reorganization (`0478643`)

Promoted the winning configuration from the **45-experiment × 8-session (`Nov 16–26`) tick-by-tick study** into production and cleaned up config/docs structure (`150/150` tests passing):

- **Scoring Engine & Risk Rules**:
  - Enabled `min_candle_elapsed_sec: 15.0`, `flag_pole_buffer_pct: 0.25` (Top-25% pole floor), `pure_3bar_expansion_pct: 0.0045` (`0.45%` 3-bar expansion) in [`config/strategies.json`](file:///Users/yorha/Documents/Trading%20Stuff/config/strategies.json) and [`strategies/momentum/scoring_engine.py`](file:///Users/yorha/Documents/Trading%20Stuff/strategies/momentum/scoring_engine.py).
  - Added [`passes_atr_spread_gate()`](file:///Users/yorha/Documents/Trading%20Stuff/core/rms/rms_service.py) (`2.0x` expected half-ATR move vs bid-ask spread) + `max_alerts_per_symbol_per_day: 3` (illiquid spread rejections do not consume quota) in [`RMSService`](file:///Users/yorha/Documents/Trading%20Stuff/core/rms/rms_service.py) and [`ColumnarSimEngine`](file:///Users/yorha/Documents/Trading%20Stuff/simulation/columnar_sim_engine.py).
  - Added `compute_convexity_scaled_targets()` (`0.45x–1.25x` target scaling around `1.5%` premium ratio + spread-aware SL floor) in [`BaseTrack`](file:///Users/yorha/Documents/Trading%20Stuff/common/tracks/base_track.py) + [`TrackFactory`](file:///Users/yorha/Documents/Trading%20Stuff/common/tracks/factory.py), and exposed all knobs in [`SimulationService`](file:///Users/yorha/Documents/Trading%20Stuff/web/simulation_service.py).
- **Track Pruning & Config Cleanup**:
  - Retired `gamma_runner_OTM2` (`0/45` profitable configs) from [`default.json`](file:///Users/yorha/Documents/Trading%20Stuff/config/tracks/default.json) & [`v2.json`](file:///Users/yorha/Documents/Trading%20Stuff/config/tracks/v2.json) (`19` active default tracks: `12 v1` + `7 v2`).
  - Retired redundant split composite tracks (`composite_split_ITM`, `composite_split_OTM`, `combined_star`) while preserving all `composite_shared_*` pooled tracks, and removed duplicate `enabled_tracks` from `config/strategies.json` in favor of `track_profile: "default"`.
- **Docs Reorganization**:
  - Split project documentation into `docs/sys/` (source of truth), `docs/live/` (active operations & research), and `docs/archive/` (historical specs & audits); archived study JSON snapshots under [`docs/live/experiments/nov16_26_45exp/`](file:///Users/yorha/Documents/Trading%20Stuff/docs/live/experiments/nov16_26_45exp) and follow-up test procedure in [`EXPERIMENT_E10_W3_CAP3_OOS.md`](file:///Users/yorha/Documents/Trading%20Stuff/docs/live/EXPERIMENT_E10_W3_CAP3_OOS.md).

---

## 2026-11-26 — Live/Sim Execution Parity & Tick-by-Tick Min-Heap Replay (`96f0c51`)

- Made `enable_circuit_breaker` configurable (`false` default) across `config/strategies.json`, `ModeRiskManager`, `BaseSuperstructure`, `ColumnarSimEngine`, and `SimulationService`.
- Unified strike spread gating on strict percentage `passes_spread_gate()` (`max_spread_pct = 0.03`), removing the live-only `min_absolute_spread_inr` parity gap.
- Routed `TICK.EQ` updates through [`QuotesBroker`](file:///Users/yorha/Documents/Trading%20Stuff/core/quotes/quotes_broker.py) so live underlying prices reach [`BaseTrack.on_tick()`](file:///Users/yorha/Documents/Trading%20Stuff/common/tracks/base_track.py) (`StructuralConvexityTrack`) with option-symbol alias guards.
- Replaced brute-force `0.5s` option polling in [`ColumnarSimEngine.run_simulation()`](file:///Users/yorha/Documents/Trading%20Stuff/simulation/columnar_sim_engine.py) with an event-driven chronological min-heap (`opt_heap` cursor advance, `delayed_heap`, and active-only `1.0s` heartbeats on `open_tracks`), eliminating missed intra-0.5s exits while cutting full-day simulation runtime (`524.6s -> 399.1s` on `2026-11-26`) and preventing sustained multi-core thermal throttling.
- Fixed browser refresh losing simulation results by storing lightweight summaries in `localStorage` and hydrating full run details from `/api/sim/run/{id}`.

---

## 2026-11-25 — System Audit 2.0 Core & Web Remediation (`4761ebb`)

Resolved `CRIT-01..05`, `HIGH-01..07`, and most `MED` findings from archived [`2026-11-25_SYSTEM_AUDIT_2_0.md`](file:///Users/yorha/Documents/Trading%20Stuff/docs/archive/2026-11-25_SYSTEM_AUDIT_2_0.md) (`142/142` tests passing):

- **Zero-Fabrication & Signal Integrity**: Fixed `FeedHandler.publish_tick()` `bid or ltp` & `is_opt` init; removed `ltp`/`entry_price` fallbacks in `QuotesBroker`, `BaseSuperstructure`, `LiveSuperstructure`, `LiveService`, `HistoricalService`, and `SimulationService`; wired `underlying_symbol` metadata & v2 heartbeat timeout reasons in `BaseTrack`.
- **Risk, Recovery & CLI Controls**: Wired `logs/.freeze_*` files + `SIGUSR1`/`SIGUSR2` & `--confirm-live`/`--capital`/`--eod-*` overrides in `runner.py`; restored `CompositeTrack` & `DelayedSuperstructure` `--continue` capital/PnL with immutable-field whitelists; fixed `RMSService` top-level `risk` config + dynamic Thursday expiry (`_nearest_thursday_tag`); eliminated shared `target_pct`/`sl_pct` mutation in v2 tracks; enforced delayed `capital_delta` in `ColumnarSimEngine`; added `fcntl.flock` to `ProtectedPositionRegistry` and `_reconcile_lock` + `execution.json` defaults in `LiveSuperstructure`.
- **Core, Sim & Web Polish**: Fixed `CandleManager` `exchange_timestamp` alignment, missing-minute `vol_at_min_edges` backfill, and `_last_recorded_vol`; validated `QuotesBroker` ZMQ delimiter frames, isolated `UNSUBSCRIBE` client IDs, and removed heartbeat `ref_counts` serialization; fixed `TrackFactory` `eff_brokerage` forwarding and duplicate return; added `track_profile` to `compute_param_hash`, `OrderedDict` LRU caches in `CandleService`, cached SHM handle in `AlertService`, $O(N \log N)$ intraday curves in `LiveService`, truth-sum overall P&L, `runner.py report` nested JSONL parsing, and isolated `inproc://` IPC across tests.
