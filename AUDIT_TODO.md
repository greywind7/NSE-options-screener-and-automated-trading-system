# System Audit Implementation Blueprint & Execution Roadmap (AUDIT_TODO)
**Document Version**: 2.0.0 | **Date**: 2026-11-23  
**Target Git Branch**: Working Copy (Zero Git Commits / Zero Pushes Enforced)  
**Status**: Priority 1 (Feed Manager) & Price Integrity Hotfixes Complete; Priorities 2–12 Planned in Detail.

---

## 1. Executive Summary & Verification of Completed Items

### 1.1 Feed Manager Pre-Flight Double Check (Completed Today)
The Feed Manager ([core/feed/feed_handler.py](file:///Users/yorha/Documents/Trading%20Stuff/core/feed/feed_handler.py)) has been overhauled and verified for live deployment:

| Area | Before Fix | After Fix | Verified Status |
|---|---|---|---|
| **Event Loop Lifecycle** | `asyncio.run(dual_runner())` inside `while` loop destroyed event loop on every reconnect, leaving `_ws_loop` dead. | Persistent `loop = asyncio.new_event_loop()`, bound to `self._ws_loop`, cleanly cancels pending tasks and closes on shutdown. | ✅ Verified |
| **TLS Certificate Verification** | `ssl_ctx.verify_mode = ssl.CERT_NONE` disabled TLS validation. | Restored standard CA validation via `ssl.create_default_context()`. | ✅ Verified |
| **Connection Sharding** | `idx % num_connections` on sorted symbol list shifted 50% of symbols on any manifest change; `hash()` had per-process random seed. | Deterministic `zlib.crc32(sym.encode()) % num_connections` with `NIFTY` and `NIFTY 50` pinned to Connection 0. Exactly 107 vs 108 symbols. | ✅ Verified |
| **Capacity Pruning** | `_prune_dynamic_options_to_fit` pruned keys from other symbols without removing them from `conn.active_options_by_symbol[other_sym]`. | Iterates `conn.active_options_by_symbol.values()` and calls `.discard(k)` for all capacity-pruned keys. | ✅ Verified |
| **Running Flags & Accessors** | Redundant `self.running` and `self._running`; dangerous `_active_ws` setter. | Unified to `self._running` with `@property running` / setter. Removed `_active_ws` setter. Cleaned dead `last_atm_by_symbol` from `ConnectionState`. | ✅ Verified |
| **Universe Key Ceiling** | `max_stream_underlyings_with_options: 110` in `config/system.json`. | Updated to `221` (full universe). Total streamed keys: 3,224 (1,612 per connection), staying strictly below the 1,800/conn limit and Upstox 2,000/conn hard ceiling. | ✅ Verified |

---

### 1.2 Strike Moneyness Population & Resolution (`ITM2`, `ITM1`, `ATM`, `OTM1`, `OTM2`)
We verified end-to-end population and handling across all core modules:

1. **RMS Strike Generation ([core/rms/rms_service.py:161-167](file:///Users/yorha/Documents/Trading%20Stuff/core/rms/rms_service.py#L161-L167))**:
   * Evaluates setup direction (`BULL` $\implies$ `CE`, `BEAR` $\implies$ `PE`).
   * Computes exact strikes:
     * `ATM`: Nearest strike to spot.
     * `ITM1`: $\pm 1$ strike step into the money ($\text{CE} \implies \text{ATM} - \text{step}$, $\text{PE} \implies \text{ATM} + \text{step}$).
     * `ITM2`: $\pm 2$ strike steps into the money (Deep ITM, $\Delta \approx 0.85$).
     * `OTM1`: $\pm 1$ strike step out of the money ($\text{CE} \implies \text{ATM} + \text{step}$, $\text{PE} \implies \text{ATM} - \text{step}$).
     * `OTM2`: $\pm 2$ strike steps out of the money (Deep OTM, $\Delta \approx 0.30$).
   * Aliases `ITM` $\to$ `ITM1` and `OTM` $\to$ `OTM1` for full backwards compatibility with legacy v1 tracks.
2. **Simulation Catalog Strike Generation ([simulation/columnar_sim_engine.py:486-505](file:///Users/yorha/Documents/Trading%20Stuff/simulation/columnar_sim_engine.py#L486-L505))**:
   * Resolves `ATM`, `ITM1`, `ITM2`, `OTM1`, `OTM2` from parquet depth catalog matching RMS logic exactly.
   * Enforces ATM proximity invariant: rejects signals if nearest recorded strike is $> \max(1.5 \times \text{step}, 2\% \times \text{spot})$ away.
3. **Track Routing & Moneyness Matching ([common/tracks/base_track.py:110-125](file:///Users/yorha/Documents/Trading%20Stuff/common/tracks/base_track.py#L110-L125))**:
   * `DeltaScalpTrack`: Pinned to `preferred_mode="ITM2"`.
   * `MicroChandelierTrack`: Pinned to `preferred_mode="ITM1"`.
   * `TrendPullbackV2Track`: Pinned to `preferred_mode="ITM1"`.
   * `GammaRunnerTrack`: Pinned to `preferred_mode="OTM2"`.
   * `AdaptiveRegimeTrack`: Dynamic time-switching:
     * Morning ($09:15 - 10:30$): `ATM`.
     * Midday Chop ($10:30 - 13:30$): `ITM1`.
     * Afternoon Expansion ($13:30 - 15:30$): `ATM`.
   * `StructuralConvexityTrack`, `OpeningThrustConvexityTrack`, `VolatilityExpansionTrack`: `ATM` with fallback to `ITM1`.

---

### 1.3 Elimination of Fallback / Made-Up Prices (Strict Pricing Invariant)
All synthetic or made-up price fallbacks have been identified and eliminated:

1. **Eliminated `else trade.entry_price` in [common/tracks/base_track.py](file:///Users/yorha/Documents/Trading%20Stuff/common/tracks/base_track.py#L345)**:
   * *Old*: `bid = trade.current_bid if trade.current_bid > 0 else trade.entry_price`
   * *New*: `bid = trade.current_bid if trade.current_bid > 0 else 0.0`. Never fabricate a 0.0% P&L exit at entry price if the market has zero bids.
2. **Eliminated `else trade.entry_price` in [common/tracks/structural_convexity.py](file:///Users/yorha/Documents/Trading%20Stuff/common/tracks/structural_convexity.py#L51-L56)**:
   * *Old*: Underlying invalidation exited at `trade.entry_price` if `current_bid` was 0.
   * *New*: Strictly exits at `trade.current_bid if trade.current_bid > 0.0 else 0.0`.
3. **Eliminated `ask = ltp` / `bid = ltp` in [simulation/columnar_sim_engine.py](file:///Users/yorha/Documents/Trading%20Stuff/simulation/columnar_sim_engine.py#L442-L446)**:
   * *Old*: If recorded `ask <= 0` or `bid <= 0`, it fabricated `ask = ltp` or `bid = ltp`.
   * *New*: Removed completely. Returns genuine recorded book depth. If `ask <= 0` or `bid <= 0`, the strike is rejected by spread and liquidity gates.
4. **Eliminated `bid = trade.entry_price` in [simulation/columnar_sim_engine.py](file:///Users/yorha/Documents/Trading%20Stuff/simulation/columnar_sim_engine.py#L748)**:
   * *Old*: Final EOD auto square-off defaulted to `trade.entry_price` if `current_bid` was 0.
   * *New*: Queries exact market quote at EOD `last_ts` via `self.get_option_quote(trade.token, last_ts)`. Falls back to `trade.current_bid` or `0.0`. Never fabricates `entry_price`.
5. **Eliminated Option Depth Fabrication in [core/feed/feed_handler.py](file:///Users/yorha/Documents/Trading%20Stuff/core/feed/feed_handler.py#L1063-L1091)**:
   * *Old*: If protobuf lacked bids/asks or arrived via `ltpc` union, it set `bid, ask = ltp, ltp`.
   * *New*: For option contracts (`is_option_contract(canonical_key)`), if the book has no bids or asks, `bid = 0.0` and `ask = 0.0`. Prevents downstream execution modules from seeing an artificial crossed or zero-spread book.

---

## 2. Priority Action Items (Remaining Priorities 2–12)

The remaining findings from [SYSTEM_AUDIT.md](file:///Users/yorha/Documents/Trading%20Stuff/SYSTEM_AUDIT.md) are organized below by domain, with exact code blueprints, watchouts, and verification plans.

---

### Phase 1: Core Engine & Data Integrity

#### TASK-01: Protect Previous-Bar Finalization in CandleManager Seqlock
* **Audit Ref**: `BUG-CM-004` | **Severity**: 🔴 Critical  
* **File**: [core/candles/candle_manager.py:253-300](file:///Users/yorha/Documents/Trading%20Stuff/core/candles/candle_manager.py#L253-L300)
* **Problem**: When a new 5-minute bar starts, `CandleManager` finalizes the previous bar by writing `prev_b["atr"]` and `prev_b["is_closed"] = True`. However, the seqlock sequence increment only guarded the *current* bar index. Readers spinning on the previous bar's seqlock see an even sequence (clean) while the writer is mutating fields.
* **Implementation Blueprint**:
  ```python
  # When transitioning from prev_idx to curr_idx:
  # 1. Acquire odd sequence on previous bar header
  prev_hdr = headers[sym_idx, prev_idx]
  prev_hdr["sequence_id"] += 1
  
  # 2. Finalize previous bar values
  candles[sym_idx, prev_idx]["atr"] = calculated_atr
  candles[sym_idx, prev_idx]["is_closed"] = True
  
  # 3. Release even sequence on previous bar header
  prev_hdr["sequence_id"] += 1
  ```
* **Watchouts & Regression Hazards**:
  * Ensure sequence arithmetic uses `uint32` overflow wrapping.
  * Check that concurrent readers in `scoring_engine.py` re-read on odd sequence and don't infinite-spin if a writer crashes.
* **Verification**: Add a stress test in `tests/core/test_candle_manager.py` with 4 concurrent reader threads spinning while bars transition rapidly.

---

#### TASK-02: Use Exchange Timestamp for Candle Bar Alignment
* **Audit Ref**: `BUG-CM-002` | **Severity**: 🟡 Medium  
* **File**: [core/candles/candle_manager.py:403](file:///Users/yorha/Documents/Trading%20Stuff/core/candles/candle_manager.py#L403)
* **Problem**: `ts = tick.get("received_timestamp", time.time())` introduces local clock drift and network jitter into 5-minute bar alignment.
* **Implementation Blueprint**:
  ```python
  ts = tick.get("exchange_timestamp") or tick.get("received_timestamp") or time.time()
  ```
* **Watchouts & Regression Hazards**:
  * Exchange timestamps on NSE sometimes arrive in milliseconds or microseconds ($\times 10^3$ or $\times 10^6$). Ensure `ts > 1e11` is scaled to seconds (`ts / 1000.0` or `ts / 1e6`).
* **Verification**: Unit test feeding ticks with varying network arrival delays and verifying bar boundaries align exactly to 09:15, 09:20, etc.

---

#### TASK-03: Scoring Engine Shutdown Order
* **Audit Ref**: `BUG-SE-001` | **Severity**: 🟡 Medium  
* **File**: [strategies/momentum/scoring_engine.py:571-591](file:///Users/yorha/Documents/Trading%20Stuff/strategies/momentum/scoring_engine.py#L571-L591)
* **Problem**: In `stop()`, `self.shm.close()` was called while `self.running` was still `True`, causing the spin loop to throw numpy buffer access errors that were swallowed by `except Exception`.
* **Implementation Blueprint**:
  ```python
  def stop(self) -> None:
      self.running = False
      time.sleep(0.02)  # Allow running spin iteration to exit
      if hasattr(self, "shm") and self.shm is not None:
          self.shm.close()
  ```
* **Watchouts**: None. Clean ordering.

---

#### TASK-04: Dynamic RMS Expiry Tag (Eliminate Hardcoded "26NOV")
* **Audit Ref**: `BUG-SE-002` | **Severity**: 🟡 Medium  
* **File**: [core/rms/rms_service.py:155-160](file:///Users/yorha/Documents/Trading%20Stuff/core/rms/rms_service.py#L155-L160)
* **Problem**: Fallback option symbol construction used `expiry_tag = "26NOV"`. Will produce invalid tokens after November contract rollover.
* **Implementation Blueprint**:
  ```python
  # Compute nearest Thursday or load from config:
  from datetime import datetime, date, timedelta
  def get_current_expiry_tag(ref_date: Optional[date] = None) -> str:
      d = ref_date or date.today()
      # Nearest Thursday
      days_ahead = (3 - d.weekday()) % 7
      thursday = d + timedelta(days=days_ahead)
      day_str = f"{thursday.day:02d}"
      mon_str = thursday.strftime("%b").upper()
      return f"{day_str}{mon_str}"
  ```
* **Watchouts**: Account for monthly vs weekly expiries and NSE market holidays that shift expiry to Wednesday.

---

### Phase 2: Trade Journal & Execution Protection

#### TASK-05: Whitelist Updatable Fields in Trade Journal Parsers
* **Audit Ref**: `BUG-019` & `BUG-HS-001` | **Severity**: 🔴 Critical  
* **Files**: [web/live_service.py:316-321](file:///Users/yorha/Documents/Trading%20Stuff/web/live_service.py#L316-L321), [web/historical_service.py:300-306](file:///Users/yorha/Documents/Trading%20Stuff/web/historical_service.py#L300-L306)
* **Problem**: `_read_journal_trades` iterates JSONL event records and merges `tr[k] = v` for all non-null fields. Subsequent events (like `TICK_UPDATE` or `TRADE_EXIT`) can overwrite `entry_price`, `entry_time`, `lot_size`, `quantity`, and `symbol`.
* **Implementation Blueprint**:
  ```python
  IMMUTABLE_FIELDS = {
      "trade_id", "mode", "track", "symbol", "token", 
      "option_type", "strike", "lot_size", "quantity", 
      "entry_ask", "entry_price", "entry_time", "entry_reason"
  }
  
  if trade_id not in trades_map:
      trades_map[trade_id] = dict(data)
  else:
      tr = trades_map[trade_id]
      for k, v in data.items():
          if v is not None and k not in IMMUTABLE_FIELDS:
              tr[k] = v
  ```
* **Watchouts**: In `metadata`, deep-merge dictionary keys instead of replacing the entire metadata object.

---

#### TASK-06: Configurable Capital & Budget Allocation
* **Audit Ref**: `BUG-018` & `BUG-SIM-001` | **Severity**: 🟡 Medium  
* **Files**: [web/live_service.py:463](file:///Users/yorha/Documents/Trading%20Stuff/web/live_service.py#L463), [simulation/columnar_sim_engine.py:766](file:///Users/yorha/Documents/Trading%20Stuff/simulation/columnar_sim_engine.py#L766)
* **Problem**: Starting capital is hardcoded to `250000.0`. Modifying `capital` in `config/strategies.json` has no effect on Live tab or sim fallback.
* **Implementation Blueprint**:
  ```python
  cfg = get_config_manager()
  strat_cfg = cfg.strategy.get("strategies", {}).get("momentum", {}).get("risk", {})
  start_budget = float(strat_cfg.get("capital", 250000.0))
  ```

---

#### TASK-07: Centralize Brokerage Default
* **Audit Ref**: `BUG-020` & `BUG-HS-003` | **Severity**: 🟢 Low  
* **Files**: `live_service.py:428`, `simulation_service.py:560`, `historical_service.py:348`
* **Problem**: ₹60 round-trip vs ₹30/side magic numbers scattered across services.
* **Implementation Blueprint**:
  * Define `brokerage_per_order: 30.0` in `config/strategies.json` under `risk`.
  * All services read from `cfg.get_risk_config().get("brokerage_per_order", 30.0)`.

---

### Phase 3: Web Server & Simulation Engine

#### TASK-08: Concurrency Race Guard on Simulation Launch
* **Audit Ref**: `BUG-014` | **Severity**: 🔴 High  
* **File**: [web/server.py:251-260](file:///Users/yorha/Documents/Trading%20Stuff/web/server.py#L251-L260)
* **Problem**: `if self.simulation_service.is_running:` check runs before `is_running` is set inside `run_simulation_job`. Concurrent POSTs both pass the guard.
* **Implementation Blueprint**:
  ```python
  if self.simulation_service.is_running:
      return web.json_response({"error": "Simulation already running"}, status=409)
  
  # Lock immediately
  self.simulation_service.is_running = True
  task = asyncio.create_task(self.simulation_service.run_simulation_job(body))
  ```

---

#### TASK-09: Defensive Timestamp Sorting Check in SimEngine
* **Audit Ref**: `BUG-SIM-002` | **Severity**: 🟡 Medium  
* **File**: [simulation/columnar_sim_engine.py:430](file:///Users/yorha/Documents/Trading%20Stuff/simulation/columnar_sim_engine.py#L430)
* **Problem**: `np.searchsorted` requires sorted arrays. If a recording lacks `exchange_timestamp`, `ts_arr` defaults to unsorted or zero arrays.
* **Implementation Blueprint**:
  ```python
  if not np.all(ts_arr[:-1] <= ts_arr[1:]):
      sort_order = np.argsort(ts_arr)
      data["timestamps"] = ts_arr[sort_order]
      data["asks"] = data["asks"][sort_order]
      data["bids"] = data["bids"][sort_order]
      data["ltps"] = data["ltps"][sort_order]
      data["bid_qtys"] = data["bid_qtys"][sort_order]
      data["ask_qtys"] = data["ask_qtys"][sort_order]
  ```

---

#### TASK-10: Monotonic Batch Simulation Run IDs & Poll Timeout
* **Audit Ref**: `BUG-SIM-003` & `BUG-STJ-002` | **Severity**: 🟡 Medium  
* **File**: [web/static/js/simulation_tab.js:695, 765](file:///Users/yorha/Documents/Trading%20Stuff/web/static/js/simulation_tab.js#L695)
* **Problem**: `RUN-${this.runs.length + 1}` can collide. `pollStatus` can spin infinitely if server reboots mid-run.
* **Implementation Blueprint**:
  ```javascript
  payload.run_id = `RUN-${Date.now()}-${Math.floor(Math.random() * 1000)}`;
  
  // In pollStatus:
  run._pollCount = (run._pollCount || 0) + 1;
  if (run._pollCount > 300) { // 5 minutes max
      clearInterval(this.pollTimer);
      run.status = "failed";
      run.error = "Simulation timed out after 5 minutes.";
  }
  ```

---

### Phase 4: Web UI Polish & Performance Optimizations

#### TASK-11: Guard Null `exit_price` in Closed Trades Panel
* **Audit Ref**: `BUG-010` | **Severity**: 🔴 Critical  
* **File**: [web/static/js/live_tab.js:300](file:///Users/yorha/Documents/Trading%20Stuff/web/static/js/live_tab.js#L300)
* **Problem**: `tr.exit_price.toFixed(2)` throws `TypeError` if `exit_price` is null/undefined.
* **Implementation Blueprint**:
  ```javascript
  exitPriceDisplay = `₹${Number(tr.exit_price || 0).toFixed(2)}`;
  ```

---

#### TASK-12: Total vs. Average Net P&L Display Clarification
* **Audit Ref**: `BUG-011` & `BUG-HS-002` | **Severity**: 🔴 High  
* **Files**: [web/live_service.py:398-401](file:///Users/yorha/Documents/Trading%20Stuff/web/live_service.py#L398-L401), [web/historical_service.py:136-138](file:///Users/yorha/Documents/Trading%20Stuff/web/historical_service.py#L136-L138)
* **Problem**: Overview summary divided net P&L by active tracks, reporting per-track average as "Net P&L".
* **Implementation Blueprint**:
  ```python
  overall = {
      "net_pnl": round(tot_net, 2),                          # Total platform P&L
      "avg_net_pnl_per_track": round(tot_net / max(1, n_active), 2),
      "raw_total_net": round(tot_net, 2),
  }
  ```
  Update frontend stat renderers to display Total Net P&L prominently and show Average per Track in subtext.

---

#### TASK-13: QuotesBroker Heartbeat Throttling
* **Audit Ref**: `BUG-QB-002` | **Severity**: 🟡 Medium  
* **File**: [core/quotes/quotes_broker.py:256](file:///Users/yorha/Documents/Trading%20Stuff/core/quotes/quotes_broker.py#L256)
* **Problem**: Serializes full token ref_counts dictionary every second (15–30 KB/s overhead).
* **Implementation Blueprint**:
  * Send high-frequency heartbeat: `{"type": "HEARTBEAT", "timestamp": now}`.
  * Only serialize `ref_counts` every 60 seconds or when requested via ROUTER REQ/REP.

---

#### TASK-14: AlertService SHM Mapping Cache
* **Audit Ref**: `BUG-AS-001` | **Severity**: 🟢 Low  
* **File**: [web/alert_service.py:224-254](file:///Users/yorha/Documents/Trading%20Stuff/web/alert_service.py#L224-L254)
* **Problem**: Calls `create_or_attach_shm` and `close()` on every HTTP request.
* **Implementation Blueprint**:
  * Cache `self._shm` at `AlertService.__init__` and reuse across requests. Reconnect only on `OSError` / broken pipe.

---

#### TASK-15: Documentation Drift Synchronization
* **Audit Ref**: `DOC DRIFT-001`, `002`, `003`, `004`
* **Files**: `ARCHITECTURE_SPEC_2_0.md`, `LOW_LEVEL_DESIGN_2_0.md`, `WEB_VIEW_FLOW.md`
* **Changes**:
  1. Update architecture diagrams to show dual parallel WebSockets (`FeedWS-1` and `FeedWS-2`).
  2. Update journal references from CSV to JSONL (`journal_<mode>_<track>.jsonl`).
  3. Synchronize `CANDLE_DTYPE` struct across ARCH_SPEC and LLD.
  4. Note that POSIX `/dev/shm` L2 cache residence requires Linux with `FORCE_POSIX_SHM=1`.

---

## 3. Implementation Phasing & Dependency Graph

```
Today (Completed)
└── Priority 1 (Feed Manager) & Strict Pricing Invariant Hotfixes
    ├── BUG-002: Persistent Event Loop
    ├── BUG-008: Standard TLS Verification
    ├── BUG-004: Stable CRC32 Sharding
    ├── BUG-006: Multi-Symbol Capacity Pruning
    ├── System Config: 221 underlyings / 1,800 keys per connection
    └── Strict Pricing: Zero fallback / zero made-up prices

Phase 1 (Next Session — Core Reliability)
├── TASK-01: CandleManager Seqlock Previous-Bar Guard
├── TASK-02: Exchange Timestamp Alignment
├── TASK-03: Scoring Engine Stop Order
└── TASK-04: Dynamic RMS Expiry Tag

Phase 2 (Trade & Execution Integrity)
├── TASK-05: Immutable Fields Whitelist in Journal Parsers
├── TASK-06: Configurable Capital Allocation
└── TASK-07: Centralized Brokerage Defaults

Phase 3 (Web Server & Simulation Robustness)
├── TASK-08: Concurrent Simulation Launch Lock
├── TASK-09: SimEngine Timestamp Sorting Validation
└── TASK-10: Monotonic Batch Simulation Run IDs & Poll Timeout

Phase 4 (Web UI Polish & Performance)
├── TASK-11: live_tab.js exit_price Null Safety
├── TASK-12: Total vs. Avg Net P&L Clarification
├── TASK-13: QuotesBroker Heartbeat Throttling
├── TASK-14: AlertService SHM Mapping Cache
└── TASK-15: Documentation Drift Updates
```

---

## 4. Verification & Testing Matrix

For every task executed from this blueprint, run the verification commands in the single-batch pattern below:

```bash
# 1. Run unit test suites for modified component
.venv/bin/python -m unittest tests/core/test_listener.py
.venv/bin/python -m unittest tests/core/test_premarket.py

# 2. Run full platform regression test suite (133 tests)
.venv/bin/python runner.py test

# 3. Validate historical simulation playback
.venv/bin/python runner.py sim --date 2026-11-19 --track v2

# 4. Validate frontend script syntax
node --check web/static/js/app.js
node --check web/static/js/live_tab.js
node --check web/static/js/simulation_tab.js
```

---
*Self-contained blueprint ready for sequential phase-by-phase execution.*
