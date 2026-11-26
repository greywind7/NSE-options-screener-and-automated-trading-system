# System Audit Report — Antigravity Trading Platform
**Date**: 2026-11-22 | **Auditor**: AI Code Review  
**Git HEAD**: `c32f53e` — *Scoring engine updated and charts visuals and bugs fixed*

---

> [!IMPORTANT]
> Findings are ordered by risk. Priority 1 = Feed Manager (untested dual-connection). Priority 2 = Web UI. Priority 3 = System-wide. Each section ends with **Action Required** items.

---

## Priority 1 — Feed Manager (`core/feed/feed_handler.py`)

### Background
The commit `f954e2b` *"added multiple connections for websocket"* migrated from a single websocket that published to one ZMQ PUB socket, to two parallel Upstox WebSocket connections partitioned by underlying symbol (hash-sharding), both publishing to the **same two ZMQ PUB sockets** (`feed_equity.ipc` / `feed_options.ipc`).

---

### 🔴 BUG-002: Dual `asyncio.run()` Calls Destroy the Event Loop on Reconnect — `_ws_loop` Becomes Stale

**Location**: [`feed_handler.py:1115-1122`](file:///Users/yorha/Documents/Trading%20Stuff/core/feed/feed_handler.py#L1115-L1122)

```python
while self._running:
    try:
        asyncio.run(dual_runner())   # Creates a NEW event loop each iteration
    except Exception as e:
        ...
        time.sleep(2.0)
```

Each call to `asyncio.run()` creates a **brand new event loop**, runs it, then destroys it. Inside `dual_runner()`:

```python
async def dual_runner():
    self._ws_loop = asyncio.get_running_loop()  # Captured here
    await asyncio.gather(...)
```

After `asyncio.run()` returns (network failure causes both coroutines to exit), `self._ws_loop` now **holds a reference to the destroyed loop**. The outer loop calls `asyncio.run()` again creating a new loop — but **during the gap**, if `stop()` is called:

```python
# stop() does:
if self._ws_loop and self._ws_loop.is_running():
    asyncio.run_coroutine_threadsafe(conn.ws.close(), self._ws_loop)
```

`self._ws_loop.is_running()` returns `False` on the dead loop — so `conn.ws.close()` is never called. Any WebSocket connection still alive leaks. Additionally, `run_coroutine_threadsafe` calls dispatched from `publish_tick` during the reconnect window silently fail because they target the dead loop.

**Fix**: Replace `asyncio.run()` outer loop with a persistent loop:
```python
loop = asyncio.new_event_loop()
self._ws_loop = loop
try:
    loop.run_until_complete(dual_runner())
finally:
    loop.close()
```
Or set `self._ws_loop = None` in a `finally` clause inside the outer `while` before each restart.

---

### 🔴 BUG-008: `ssl_ctx.verify_mode = ssl.CERT_NONE` — Disabled TLS Verification

**Location**: [`feed_handler.py:1013-1015`](file:///Users/yorha/Documents/Trading%20Stuff/core/feed/feed_handler.py#L1013-L1015)

```python
ssl_ctx = ssl.create_default_context()
ssl_ctx.check_hostname = False
ssl_ctx.verify_mode = ssl.CERT_NONE
```

TLS certificate verification is completely disabled for every Upstox WebSocket connection. The Upstox API has valid certificates — this is a production **man-in-the-middle vulnerability** with zero benefit.

**Fix**: Remove the two override lines. `create_default_context()` already has correct defaults.

---

### 🔴 BUG-001: Mock Mode — Dynamic WS Subscription Dispatch Is Silently Dead

**Location**: [`feed_handler.py:827-836`](file:///Users/yorha/Documents/Trading%20Stuff/core/feed/feed_handler.py#L827-L836)

```python
if (to_add or to_remove) and self._ws_loop:
    # ... dispatch via run_coroutine_threadsafe
```

In mock mode (`_run_mock_stream`), `self._ws_loop` is **never set** (it's only set inside `dual_runner()` which only runs in WebSocket mode). Dynamic subscription changes are computed correctly but never dispatched — no WebSocket message is sent. Mock mode cannot simulate realistic dynamic options subscriptions.

**Fix**: Log a warning when `evaluate_dynamic_options` returns changes but `_ws_loop` is `None`.

---

### 🔴 BUG-004: Hash-Based Shard Assignment Is Not Stable Across Restarts

**Location**: [`feed_handler.py:473-483`](file:///Users/yorha/Documents/Trading%20Stuff/core/feed/feed_handler.py#L473-L483)

```python
sorted_symbols = sorted(list(all_und_symbols))
for idx, sym_u in enumerate(sorted_symbols):
    c_id = idx % self.num_connections
```

Symbols are sharded round-robin over `sorted_symbols`. If a new symbol is added to the manifest, its sort-index shifts all subsequent symbols to the **opposite connection**. After a mid-day restart with an updated manifest, up to half the equity symbols migrate connections, and all their dynamic options follow.

**Fix**: Use `abs(hash(sym)) % num_connections` for a stable consistent hash that survives list changes. Only the "NIFTY pinned to 0" override needs special handling.

---

### 🟡 BUG-003: Duplicate `self.running` / `self._running` Flags — Stop Race Condition

**Location**: [`feed_handler.py:165-166`](file:///Users/yorha/Documents/Trading%20Stuff/core/feed/feed_handler.py#L165-L166)

Both `self.running` and `self._running` are set identically throughout. The WebSocket loop checks only `self._running`. Public API uses `self.running`. This is dead redundancy and a subtle stop-ordering hazard: `stop()` acquires `_publish_lock` to close sockets, but if called from within the asyncio loop thread, it deadlocks (a coroutine in the loop could be waiting on the same lock in `publish_tick`).

**Fix**: Eliminate `self.running`. Use only `self._running`. Add a comment that SIGINT is always delivered to the main thread on Unix (making the deadlock theoretical for the signal-handler use case).

---

### 🟡 BUG-005: `ConnectionState.last_atm_by_symbol` / `last_reval_time_by_symbol` Are Written but Never Read

**Location**: [`feed_handler.py:72-73, 725-728`](file:///Users/yorha/Documents/Trading%20Stuff/core/feed/feed_handler.py#L72-L73)

The throttle checks in `evaluate_dynamic_options` read from `self.last_reval_time_by_symbol` and `self.last_atm_by_symbol` (feed-level). The per-connection counterparts (`conn.last_atm_by_symbol`, `conn.last_reval_time_by_symbol`) are **written but never read**. Dead weight on every `ConnectionState`.

**Fix**: Remove from `ConnectionState` dataclass.

---

### 🟡 BUG-006: `_prune_dynamic_options_to_fit` Removes Keys from Other Symbols but Doesn't Update Their `ConnectionState` Sets

**Location**: [`feed_handler.py:573-583`](file:///Users/yorha/Documents/Trading%20Stuff/core/feed/feed_handler.py#L573-L583)

When capacity pruning selects keys from **other symbols** (`for sym, opt_set in list(options_by_sym.items()): if sym == current_symbol: continue`), the caller only updates `conn.active_options_by_symbol[lookup_sym]`. The pruned keys remain in `conn.active_options_by_symbol[other_sym]`. Next evaluation of `other_sym` produces a stale `current_keys` and may try to unsubscribe already-removed keys or miss re-adding them.

**Fix**: After capacity pruning, loop over pruned keys and `discard` from each relevant symbol's set:
```python
for k in capacity_pruned:
    for sym_set in conn.active_options_by_symbol.values():
        sym_set.discard(k)
```

---

### 🟡 BUG-007: `_active_ws` Setter Silently Overwrites Connection 0's WebSocket

**Location**: [`feed_handler.py:206-209`](file:///Users/yorha/Documents/Trading%20Stuff/core/feed/feed_handler.py#L206-L209)

```python
@_active_ws.setter
def _active_ws(self, val: Any) -> None:
    if self.connections:
        self.connections[0].ws = val
```

This setter always targets `connections[0]`. Any code doing `self._active_ws = ws` would silently lose the actual connection 0 state. It's not called internally but is a public footgun.

**Fix**: Remove the setter or raise `NotImplementedError`.

---

### 🟢 DOC DRIFT-001: Spec Shows One WebSocket; Implementation Has Two

**Files**: [`ARCHITECTURE_SPEC_2_0.md`](file:///Users/yorha/Documents/Trading%20Stuff/ARCHITECTURE_SPEC_2_0.md) §3.1, [`LOW_LEVEL_DESIGN_2_0.md`](file:///Users/yorha/Documents/Trading%20Stuff/LOW_LEVEL_DESIGN_2_0.md) §4.1

Both docs show `FeedWS --> FeedHandler` (singular). Implementation now runs `num_connections=2` parallel WebSocket workers. Both class docstring and startup banner correctly say "Dual-Connection" but the Mermaid architecture diagrams still show one feed arrow.

**Fix**: Update diagrams in both docs to show `FeedWS-1` and `FeedWS-2`, and annotate the 2,000 key/connection Upstox limit that motivates the split.

---

## Priority 2 — Web UI

### 🔴 BUG-010: `exit_price.toFixed(2)` Crashes on `null` — Breaks Closed Trades Panel

**Location**: [`live_tab.js:300`](file:///Users/yorha/Documents/Trading%20Stuff/web/static/js/live_tab.js#L300)

```javascript
if (isClosed) {
    exitPriceDisplay = `₹${tr.exit_price.toFixed(2)}`;  // ← null crash
```

The backend `_read_journal_trades` defaults `exit_price` to `data.get("exit_price")` which returns `None` if the field is absent. A closed trade with a missing `exit_price` field causes `TypeError: Cannot read properties of null (reading 'toFixed')`, crashing `renderDetailPanel` and leaving the trades table blank with no visible error.

**Fix**: `Number(tr.exit_price || 0).toFixed(2)` — same pattern already used elsewhere in the UI.

---

### 🔴 BUG-011: `_build_live_summary` Displays Average P&L Per Track — Misleading as "Net P&L"

**Location**: [`live_service.py:398-401`](file:///Users/yorha/Documents/Trading%20Stuff/web/live_service.py#L398-L401)

```python
overall = {
    "net_pnl":  round(tot_net / n_active, 2),   # ← per-track average
    "raw_total_net": round(tot_net, 2),          # ← actual total (not displayed)
```

The live tab header shows `overall.net_pnl` labeled "Net P&L". But this is the **average per active track**, not the total platform P&L. With 2 tracks running, the displayed number is half the actual. `raw_total_net` exists but `LiveTab.renderStats` never reads it.

**Fix**: Either change `LiveTab.renderStats` to use `overall.raw_total_net`, or rename to `avg_net_pnl_per_track` and add `total_net_pnl` as the primary field.

---

### 🔴 BUG-014: `handle_sim_run` Has Race Condition — Two Concurrent Requests Both Pass `is_running` Guard

**Location**: [`server.py:251-260`](file:///Users/yorha/Documents/Trading%20Stuff/web/server.py#L251-L260)

```python
if self.simulation_service.is_running:
    return web.json_response({"error": "Simulation already running"}, status=409)

task = asyncio.create_task(self.simulation_service.run_simulation_job(body))
```

`is_running` is not set until `run_simulation_job` actually executes (after the event loop schedules the task). Two concurrent POST `/api/sim/run` requests both check before either sets the flag — both pass, and two simulation processes are spawned.

**Fix**: Set `self.simulation_service.is_running = True` **before** `create_task`:
```python
self.simulation_service.is_running = True
task = asyncio.create_task(...)
```

---

### 🟡 BUG-009: Old WebSocket Not Closed Before Reconnect — Can Accumulate Open Sockets

**Location**: [`app.js:64-104`](file:///Users/yorha/Documents/Trading%20Stuff/web/static/js/app.js#L64-L104)

`connectWebSocket()` replaces `this.ws` without calling `.close()` on the previous instance. If the old socket transitions to `OPEN` after the new one was created (unstable network race), it fires `onclose` unexpectedly and may call `scheduleReconnect()` while already connected.

**Fix**: Add at the top of `connectWebSocket()`:
```javascript
if (this.ws) {
    this.ws.onclose = null;
    this.ws.onerror = null;
    this.ws.close();
}
```

---

### 🟡 BUG-012: `LiveService.start()` Has No Guard — Double-Call Creates Two Listener Tasks

**Location**: [`live_service.py:49-53`](file:///Users/yorha/Documents/Trading%20Stuff/web/live_service.py#L49-L53)

```python
async def start(self) -> None:
    self.running = True
    self._zmq_ctx = zmq.asyncio.Context()
    self._listener_task = asyncio.create_task(self._listen_options_stream())
```

If `on_startup` fires twice (e.g., test harness, server restart without teardown), two background tasks both subscribe to the same ZMQ endpoints and populate `self.lvc` with duplicate data. The old `_zmq_ctx` is overwritten without being terminated — socket leak.

**Fix**: Add a guard:
```python
if self._listener_task and not self._listener_task.done():
    return
```

---

### 🟡 BUG-013: Simulation Worker Error Path Returns `{"error": ..., "status": "failed"}` — `_on_sim_done` Callback Misses It

**Location**: [`simulation_service.py:508-512`](file:///Users/yorha/Documents/Trading%20Stuff/web/simulation_service.py#L508-L512), [`server.py:263-273`](file:///Users/yorha/Documents/Trading%20Stuff/web/server.py#L263-L273)

When a simulation fails, `run_simulation_job` catches the exception and **returns** an error dict instead of raising. The `_on_sim_done` callback in `server.py` checks `t.exception()` — which is `None` because the coroutine returned normally. The error is set in `self.simulation_service.latest_error`, but the callback does not set `is_running = False` (the `finally` block does). The UI must poll `/api/sim/status` to discover the error.

**Fix**: The behavior is functionally acceptable — the `finally` block correctly cleans up `is_running`. But `_on_sim_done` checking for exceptions is now misleading. Remove the `exc` check from the callback or convert the error return to a raise.

---

### 🟡 BUG-015: Trade Analysis Modal Charts Not Reset on Re-Open — Stale State from Previous Trade

**Location**: [`app.js:358-369`](file:///Users/yorha/Documents/Trading%20Stuff/web/static/js/app.js#L358-L369)

Chart objects are created once and reused via `if (!this.tamUnderlyingLocalChart)`. `setData()` is called each time, which is correct. However, `setTamRange(this.tamCurrentRange)` is called at the end with the **previous session's range**. If the range was `"fullday"` last time, the local charts are hidden (`display: none`) and their canvas dimensions are 0×0 when `_setupCanvas()` and `render()` are called, corrupting their internal state.

**Fix**: Call `this.tamCurrentRange = "local"` or `this.setTamRange("local")` at the start of `openTradeModal`.

---

### 🟡 BUG-016: Simulation Worker Re-Reads Parquet Even When Parent Has It Cached

**Location**: [`simulation_service.py:53-54`](file:///Users/yorha/Documents/Trading%20Stuff/web/simulation_service.py#L53-L54)

```python
def _simulation_worker_process(...):
    sim_svc = SimulationService(recordings_dir=recordings_dir)  # fresh instance
    results = sim_svc._execute_simulation_sync(...)              # calls _ensure_cache() → reads disk
```

The parent's `cached_equity_df` / `cached_options_df` are never transmitted to the worker. Every first run re-reads from disk inside the worker even when the parent has the data in memory. The param-hash deduplication cache (parent) skips the entire process for identical re-runs, but date changes always hit disk in the worker.

**Fix**: Serialize and pass `cached_equity_df` / `cached_options_df` as pickle bytes through the temp file, or use a shared memory array.

---

### 🟡 BUG-017: `_build_intraday_curve` Is O(n²) — Called Every Broadcast Second

**Location**: [`live_service.py:587-619`](file:///Users/yorha/Documents/Trading%20Stuff/web/live_service.py#L587-L619)

```python
for cur_t in eval_times:          # ~200 time points
    for tr in trades:             # potentially 50+ trades
        if tr.get("status") == "CLOSED":
            if x_ts <= cur_t: ...
```

For 200 time points × 50 trades = 10,000 iterations per curve build, running every second while WS clients are connected.

**Fix**: Pre-sort closed trades by `exit_time` and advance a pointer:
```python
sorted_exits = sorted([t for t in trades if t.get("status") == "CLOSED"],
                       key=lambda t: float(t.get("exit_time") or 0))
ptr = 0
for cur_t in eval_times:
    while ptr < len(sorted_exits) and float(sorted_exits[ptr].get("exit_time") or 0) <= cur_t:
        # accumulate
        ptr += 1
```

---

### 🟢 DOC DRIFT-002: `WEB_VIEW_FLOW.md` References `trade_journal_*.csv` — Actual Format Is `journal_*.jsonl`

**Files**: [`WEB_VIEW_FLOW.md`](file:///Users/yorha/Documents/Trading%20Stuff/WEB_VIEW_FLOW.md) §2 diagram, [`live_service.py:177`](file:///Users/yorha/Documents/Trading%20Stuff/web/live_service.py#L177)

Doc: `recordings/YYYYMMDD/trade_journal_*.csv`  
Code: scans for `fname.startswith("journal_") and fname.endswith(".jsonl")`

Format is JSONL, not CSV. Filename is `journal_<mode>_<track>.jsonl`, not `trade_journal_*.csv`.

**Fix**: Update the diagram in `WEB_VIEW_FLOW.md`.

---

### 🟢 DOC DRIFT-003: `CANDLE_DTYPE` Definition Differs Between Architecture Spec and LLD

**Files**: [`ARCHITECTURE_SPEC_2_0.md §5.1`](file:///Users/yorha/Documents/Trading%20Stuff/ARCHITECTURE_SPEC_2_0.md#L402-L418) vs [`LOW_LEVEL_DESIGN_2_0.md §3.1`](file:///Users/yorha/Documents/Trading%20Stuff/LOW_LEVEL_DESIGN_2_0.md#L184-L197)

| Field | ARCH_SPEC | LLD |
|---|---|---|
| `vwap` | ✅ present | ❌ absent |
| `range_compression` | ✅ present | ❌ absent |
| `is_closed` | ❌ absent | ✅ present |
| `_reserved` padding | ❌ absent | ✅ present |

The two canonical struct definitions disagree on 4 fields. The actual implementation in `core/shm/layout.py` is authoritative.

**Fix**: Read the actual implementation and sync both docs.

---

## Priority 3 — System-Wide

### 🔴 BUG-018: `start_budget` Is Hardcoded to ₹2,50,000 in `live_service.py`

**Location**: [`live_service.py:463`](file:///Users/yorha/Documents/Trading%20Stuff/web/live_service.py#L463)

```python
start_budget = 250000.0  # ← hardcoded, not from config
```

If the user changes `capital` in `config/strategies.json`, the live tab still shows ₹2,50,000 as starting capital. The simulation service reads capital from params correctly.

**Fix**: Load from `get_config_manager()` or accept as a constructor argument.

---

### 🔴 BUG-019: `_read_journal_trades` Overwrites Critical Fields (Including `entry_price`) from Subsequent Events

**Location**: [`live_service.py:316-321`](file:///Users/yorha/Documents/Trading%20Stuff/web/live_service.py#L316-L321)

```python
for k, v in data.items():
    if v is not None:
        tr[k] = v  # ← overwrites ALL non-null fields
```

Any event emitted after `TRADE_OPEN` with a numeric field set to `0.0` (which is not `None`) will overwrite the real value. Example: a `TICK_UPDATE` event that includes `entry_price: 0.0` would zero out the entry price, corrupting all subsequent P&L calculations.

**Fix**: Whitelist fields that may be updated after entry:
```python
UPDATEABLE_FIELDS = {"current_bid", "highest_price", "stop_loss", "target_price", 
                     "ratchet_level", "metadata", "status"}
for k, v in data.items():
    if v is not None and (k in UPDATEABLE_FIELDS or trade_id not in trades_map):
        tr[k] = v
```

---

### 🟡 BUG-020: Brokerage Default Is ₹60 in Live Service, ₹30 in Simulation Service

**Location**: [`live_service.py:428`](file:///Users/yorha/Documents/Trading%20Stuff/web/live_service.py#L428) vs [`simulation_service.py:560`](file:///Users/yorha/Documents/Trading%20Stuff/web/simulation_service.py#L560)

```python
# live_service.py — round-trip default
b_fee = float(tr.get("metadata", {}).get("brokerage", 60.0))

# simulation_service.py — per-side default (×2 applied internally)
brokerage = float(glob.get("brokerage_per_order", 30.0))
```

The numbers are consistent in intent (₹30/side = ₹60 round-trip) but the magic number `60.0` in the live service diverges if brokerage changes.

**Fix**: Centralize in `config/strategies.json`. Both services read it from config.

---

### 🟡 BUG-021: `ProtectedPositionRegistry` Is In-Memory — Cross-Process Sync Unclear

**Location**: [`feed_handler.py:192`](file:///Users/yorha/Documents/Trading%20Stuff/core/feed/feed_handler.py#L192), [`core/feed/position_registry.py`](file:///Users/yorha/Documents/Trading%20Stuff/core/feed/position_registry.py)

The spec (ARCH_SPEC §6.2) describes cross-process position protection, but the registry is used as a singleton. Each OS process has its own Python singleton — changes in one process (superstructure marking a fill as protected) are invisible to the `FeedHandler` process unless the registry uses shared memory or file-backed IPC.

If the registry is purely in-memory, the `FeedHandler` would only see positions registered in **its own process** — meaning dynamic options protection only works if `FeedHandler` and superstructures run in the same process, which contradicts the multi-process architecture.

**Fix**: Audit `position_registry.py`. If in-memory, document the limitation clearly and ensure the `load_instruments()` startup query covers recovery from journal files at minimum.

---

## Dead Code

| ID | Location | Detail |
|---|---|---|
| DEAD-001 | [`feed_handler.py:206-209`](file:///Users/yorha/Documents/Trading%20Stuff/core/feed/feed_handler.py#L206-L209) | `_active_ws` setter is never called — remove |
| DEAD-002 | [`feed_handler.py:1124-1129`](file:///Users/yorha/Documents/Trading%20Stuff/core/feed/feed_handler.py#L1124-L1129) | `_run_poll_stream` is a hot-sleep stub — implement or remove |
| DEAD-003 | [`feed_handler.py:38`](file:///Users/yorha/Documents/Trading%20Stuff/core/feed/feed_handler.py#L38) | `TickEvent` imported but never constructed — remove import |

---

## Documentation Compliance Summary

| Spec Requirement | Status |
|---|---|
| Dual ZMQ IPC sockets (`feed_equity.ipc`, `feed_options.ipc`) | ✅ Compliant |
| Dynamic ATM ±3 strike subscription with hysteresis | ✅ Compliant |
| Active position protection guard | ✅ Compliant (cross-process caveat in BUG-021) |
| Single Feed WebSocket (per diagrams) | ⚠️ **Doc Drift** — 2 connections in code |
| `CANDLE_DTYPE` struct definition | ⚠️ **Doc Drift** — ARCH_SPEC vs LLD disagree |
| Trade journals as CSV | ⚠️ **Doc Drift** — JSONL in code |
| `start_budget` = configurable capital | ❌ Hardcoded ₹2,50,000 in LiveService |
| TLS certificate verification | ❌ Disabled in production WS path |
| REST fallback poller | ❌ Stub — not implemented |
| `ProtectedPositionRegistry` cross-process sync | ❓ Unknown — needs audit |

---

## Prioritized Action Queue

| ID | Severity | File | Action |
|---|---|---|---|
| BUG-002 | 🔴 Critical | `feed_handler.py` | Fix stale `_ws_loop` on reconnect |
| BUG-008 | 🔴 Critical | `feed_handler.py` | Re-enable TLS certificate verification |
| BUG-010 | 🔴 Critical | `live_tab.js` | Guard `exit_price.toFixed(2)` from null |
| BUG-011 | 🔴 High | `live_service.py` | Fix averaged vs total net P&L display |
| BUG-014 | 🔴 High | `server.py` | Fix `is_running` race condition |
| BUG-004 | 🟡 Medium | `feed_handler.py` | Use stable consistent hash for shard assignment |
| BUG-006 | 🟡 Medium | `feed_handler.py` | Update other-symbol sets after capacity prune |
| BUG-009 | 🟡 Medium | `app.js` | Close old WebSocket before reconnect |
| BUG-012 | 🟡 Medium | `live_service.py` | Guard `start()` against double-call |
| BUG-013 | 🟡 Medium | `simulation_service.py` | Surface errors from worker to UI |
| BUG-018 | 🟡 Medium | `live_service.py` | Load `start_budget` from config |
| BUG-019 | 🟡 Medium | `live_service.py` | Whitelist updatable fields in journal reader |
| BUG-003 | 🟢 Low | `feed_handler.py` | Remove duplicate `running`/`_running` |
| BUG-005 | 🟢 Low | `feed_handler.py` | Remove dead per-connection ATM/reval maps |
| BUG-007 | 🟢 Low | `feed_handler.py` | Remove `_active_ws` setter |
| BUG-015 | 🟢 Low | `app.js` | Reset `tamCurrentRange = "local"` on modal open |
| BUG-016 | 🟢 Low | `simulation_service.py` | Pass cached DataFrames to worker |
| BUG-017 | 🟢 Low | `live_service.py` | O(n²) → O(n log n) curve builder |
| BUG-020 | 🟢 Low | Both services | Centralize brokerage default in config |
| BUG-021 | 🟢 Low | `position_registry.py` | Audit and document cross-process sync |
| DOC DRIFT-001 | Docs | Both spec docs | Update diagrams for dual-connection |
| DOC DRIFT-002 | Docs | `WEB_VIEW_FLOW.md` | Fix journal format/filename |
| DOC DRIFT-003 | Docs | ARCH_SPEC + LLD | Sync `CANDLE_DTYPE` definition |
| DEAD-001–003 | Cleanup | `feed_handler.py` | Remove setter, stub, unused import |

---

---

## Priority 4 — CandleManager & Shared Memory (`core/candles/`)

### 🔴 BUG-CM-001: `warmup_from_parquet` Crashes with `AttributeError` on `--continue` Restart

**Location**: [`candle_manager.py:376`](file:///Users/yorha/Documents/Trading%20Stuff/core/candles/candle_manager.py#L376)

```python
if tok in self.token_to_idx:   # AttributeError: 'CandleManager' has no attribute 'token_to_idx'
```

The class uses `self.symbol_to_index` throughout — `token_to_idx` does not exist. Any mid-day restart with `--continue` will raise `AttributeError` and abort the warmup path, leaving candle state cold. This path is **only exercised on restart**, so it escapes unit tests.

**Fix**: Replace `self.token_to_idx` with `self.symbol_to_index`.

---

### 🔴 BUG-CM-004: Seqlock Does Not Protect Previous Bar Writes on Bar Transition

**Location**: [`candle_manager.py:253-300`](file:///Users/yorha/Documents/Trading%20Stuff/core/candles/candle_manager.py#L253-L300)

The seqlock header guards **the current bar index**. At bar transition the code writes `prev_b["atr"]` and `prev_b["is_closed"] = True` for the **previous bar index** — but those writes happen outside the seqlock increment of the previous bar's header. Concurrent ScoringEngine readers spinning on the previous bar's seqlock see `seq` as even (looks clean) while the writer is simultaneously mutating `is_closed` and `atr`. This is a classic seqlock correctness bug for the bar being finalized.

**Fix**: Bracket the previous-bar finalization writes with `headers[prev_idx]["sequence_id"] += 1` before and `+= 2` after (odd → even transition).

---

### 🟡 BUG-CM-002: `received_timestamp` Used for Bar Alignment Instead of `exchange_timestamp`

**Location**: [`candle_manager.py:403`](file:///Users/yorha/Documents/Trading%20Stuff/core/candles/candle_manager.py#L403)

```python
ts = tick.get("received_timestamp", time.time())
```

The LLD spec §3.2 requires `exchange_timestamp` for bar boundary calculations to avoid introducing network jitter into 5-minute bar alignment. Using received timestamp can shift bar transitions by hundreds of milliseconds under load.

**Fix**: Use `exchange_timestamp` with `received_timestamp` as fallback: `tick.get("exchange_timestamp") or tick.get("received_timestamp", time.time())`.

---

### 🟡 BUG-CM-003: First Bar Volume Is Zero Until Second Tick Arrives

**Location**: [`candle_manager.py:237-276`](file:///Users/yorha/Documents/Trading%20Stuff/core/candles/candle_manager.py#L237-L276)

On the first tick of the trading session, `prev_v = 0`, so `delta_vol = vol - 0 = vol`. But at a **bar transition** (new 5-min bar opens), the code resets `active_bar["volume"] = delta_vol` for the **new** bar. If `prev_v == 0` on the very first tick (no previous bar), `delta_vol` is correct. However if `prev_v == 0` because the **previous bar had zero volume** (e.g. pre-market tick), `delta_vol = vol - 0 = full_cumulative_vol` — which over-reports the first bar. Edge case in pre-market, real impact depends on recording start time.

**Fix**: Track a separate `_last_recorded_vol` per symbol that persists across bars, initialized to `None`; skip delta calculation if `None`.

---

### 🟡 BUG-SHM-001: Default SHM Is File-Backed mmap, Not in CPU L2 Cache

**Location**: [`shm_layout.py:MmapSharedMemoryAdapter`](file:///Users/yorha/Documents/Trading%20Stuff/core/candles/shm_layout.py)

The default adapter writes to `/tmp/{name}.shm` (a memory-mapped file). Architecture docs claim "777 KB fits in L2 cache for microsecond reads." This is only valid when `FORCE_POSIX_SHM=1` is set (uses POSIX `/dev/shm` on Linux). On macOS and in the default configuration the data goes through the filesystem page cache — not L2.

**Fix / Doc Update**: Add explicit note in `ARCHITECTURE_SPEC_2_0.md` §5 that the L2 cache claim assumes `FORCE_POSIX_SHM=1` on Linux. No code change required, documentation drift only.

---

## Priority 5 — Scoring Engine (`strategies/momentum/scoring_engine.py`)

### 🟡 BUG-SE-001: `evaluate_universe` Called from `run_spin_loop` with No Guard After `stop()`

**Location**: [`scoring_engine.py:571-591`](file:///Users/yorha/Documents/Trading%20Stuff/strategies/momentum/scoring_engine.py#L571-L591)

```python
while self.running:
    try:
        self.evaluate_universe()
        time.sleep(0.005)
    except Exception as e:
        print(...)   # Exception swallowed — loop continues even after shm.close()
```

After `stop()` closes `self.shm`, if a concurrent tick comes in and triggers `_read_candle_seqlock` (which accesses the closed SHM), the exception is printed but the loop **continues** because `self.running` is set to `False` only after `shm.close()`. There's a one-tick window where `running=True` and `shm` is closed. The `except Exception` swallows the segfault-equivalent numpy buffer access error silently.

**Fix**: Set `self.running = False` **before** calling `self.shm.close()`.

---

### 🟢 BUG-SE-002: RMS `expiry_tag` Hardcoded to `"26NOV"`

**Location**: [`rms_service.py:155`](file:///Users/yorha/Documents/Trading%20Stuff/core/rms/rms_service.py#L155)

```python
expiry_tag = "26NOV"
```

The fallback option symbol constructor (`NSE:{symbol}{expiry_tag}{strike}{opt_type}`) uses a hardcoded expiry. This path is only used when `options_map` doesn't have the strike (i.e. manifest lookup miss). In the week of 26-Nov expiry this happened to work; after rollover it will produce silently wrong instrument tokens that return no quotes.

**Fix**: Compute `expiry_tag` dynamically from the nearest weekly/monthly expiry using `datetime`. Or load it from `config/strategies.json` as `default_expiry_tag`.

---

### 🟡 BUG-SE-003: `_check_pure_breakout` Recomputes `avg_rov` with `/300.0` Divisor — Assumes Exactly 5-Minute Bars

**Location**: [`scoring_engine.py:451`](file:///Users/yorha/Documents/Trading%20Stuff/strategies/momentum/scoring_engine.py#L451)

```python
avg_rov = float(sum(b["volume"] / 300.0 for b in prev)) / 3.0
```

The 300.0 (seconds per bar) is a magic constant matching the 5-minute bar design. If the bar interval is ever changed (the architecture allows configurable intervals), this will silently produce wrong volume-rate-of-change comparisons.

**Fix**: Use `self.bar_seconds` or reference the bar duration from `SHM_DTYPE` configuration.

---

## Priority 6 — RMS Service (`core/rms/rms_service.py`)

### 🟢 BUG-RMS-001: `process_alert` Does Not Publish if `strikes` Is Empty but Returns `None` Silently

**Location**: [`rms_service.py:225-248`](file:///Users/yorha/Documents/Trading%20Stuff/core/rms/rms_service.py#L225-L248)

When `resolve_strikes` returns `{}` (spread gate rejected all strikes, or manifest miss), `process_alert` returns `None` with no log. Under high-rVOL conditions with wide spreads this causes a silent alert blackhole — the scoring engine fired a valid signal but nothing reaches execution. No observability.

**Fix**: Add a `print(f"[RMS] Alert {alert.alert_id} rejected: no liquid strikes for {alert.symbol}")` before returning `None`.

---

### 🟢 BUG-RMS-002: `ProtectedPositionRegistry` — Cross-Process Sync via File Poll Works But Has 1-Second Lag

**Location**: [`position_registry.py:259-291`](file:///Users/yorha/Documents/Trading%20Stuff/core/feed/position_registry.py#L259-L291)

Resolved from BUG-021. The registry uses `_ensure_loaded_under_lock()` with mtime-based file reload — every read checks if the file changed. This means cross-process sync works (execution superstructure writes JSON file; FeedHandler re-reads on next `get_protected_keys()` call) but with up to 1-second polling latency. If FeedHandler evaluates dynamic ATM resubscription immediately after a position opens, it may read stale data and unsubscribe the just-opened position's contract.

**Status**: Architecture doc note BUG-021 is resolved — sync mechanism exists. Lag is acceptable for most cases but document the 1-second window.

---

## Priority 7 — QuotesBroker (`core/quotes/quotes_broker.py`)

### 🟡 BUG-QB-001: `_handle_request` Receives `frames[2]` Without Validating Empty Delimiter in `frames[1]`

**Location**: [`quotes_broker.py:159-163`](file:///Users/yorha/Documents/Trading%20Stuff/core/quotes/quotes_broker.py#L159-L163)

```python
frames = self.router.recv_multipart(flags=zmq.NOBLOCK)
if len(frames) < 3:
    return
client_id = frames[0]
# frame 1 is empty delimiter  ← comment, not enforced
payload = unpack_message(frames[2])
```

If a DEALER sends a malformed frame without the empty delimiter (e.g. during reconnect storms), `frames[2]` will be the wrong frame and `unpack_message` will raise or return garbage. The check only validates `len >= 3`, not that `frames[1] == b""`.

**Fix**: Add `if frames[1] != b"": return` after the length check.

---

### 🟡 BUG-QB-002: `_heartbeat_loop` Publishes `ref_counts` as Full Dict Every Second — O(active_tokens) JSON

**Location**: [`quotes_broker.py:256`](file:///Users/yorha/Documents/Trading%20Stuff/core/quotes/quotes_broker.py#L256)

```python
"ref_counts": {t: len(subs) for t, subs in self.subscribers_by_token.items() if subs},
```

Every 1-second heartbeat serializes the full ref_count dict (up to 663 tokens × 221 symbols). This adds ~15-30 KB of msgpack per second for every heartbeat subscriber just for diagnostics. The heartbeat's primary purpose is to drive the delayed execution 1-second clock.

**Fix**: Only send `{"type": "HEARTBEAT", "timestamp": now}` in the high-frequency path. Emit ref_counts only every 60 seconds or on explicit `GET_SNAPSHOT` request.

---

## Priority 8 — Historical Service (`web/historical_service.py`)

### 🔴 BUG-HS-001: `_parse_journal_file` Overwrites `entry_price` from Any Subsequent Event

**Location**: [`historical_service.py:300-306`](file:///Users/yorha/Documents/Trading%20Stuff/web/historical_service.py#L300-L306) *(same as BUG-019 found in live_service.py — confirmed in historical_service too)*

```python
for k, v in data.items():
    if v is not None:
        tr[k] = v   # Overwrites entry_price, lot_size, etc. from EXIT event
```

If the `TRADE_EXIT` event journal line includes `entry_price` (which it often does for context), the merge overwrites the original `entry_price` from the `TRADE_ENTRY` event. For trades where the exit event carries a re-stated entry_price (e.g. slippage adjustment), the return_pct calculation becomes wrong.

**Fix**: Create an explicit whitelist of fields that may be updated: `{"status", "exit_price", "exit_time", "exit_reason", "realized_pnl", "metadata"}`. Protect `entry_price`, `entry_time`, `lot_size`, `quantity` from being overwritten.

---

### 🟡 BUG-HS-002: `get_historical_overview` Divides `gross_pnl` and `net_pnl` by Active Track Count

**Location**: [`historical_service.py:136-138`](file:///Users/yorha/Documents/Trading%20Stuff/web/historical_service.py#L136-L138)

```python
"gross_pnl": round(tot_gross / n_active, 2),
"net_pnl": round(tot_net / n_active, 2),
```

The `overall` summary divides by number of active tracks — reporting **average per-track P&L** as the top-level number. Users looking at the overall session P&L will see half (or a third) of actual total if two or three tracks traded. The field is labeled `net_pnl` with no "average" qualifier.

**Fix**: Remove the division by `n_active` for `overall` section. Store both `net_pnl` (total) and `avg_net_pnl` (per track). The `raw_total_net` field already has the correct value — the labeled fields are wrong.

---

### 🟢 BUG-HS-003: `_parse_live_trades_csv` Hardcodes `brokerage=60.0` Default

**Location**: [`historical_service.py:348`](file:///Users/yorha/Documents/Trading%20Stuff/web/historical_service.py#L348)

```python
"metadata": {"brokerage": float(row.get("brokerage", 60.0) or 60.0)},
```

Same pattern as BUG-020. Magic number fallback not from config.

---

## Priority 9 — Candle Service (`web/candle_service.py`)

### 🟢 BUG-CS-001: `CandleService._df_cache` LRU Eviction Pops First Key (Insertion-Order Oldest, Not LRU)

**Location**: [`candle_service.py:102-104`](file:///Users/yorha/Documents/Trading%20Stuff/web/candle_service.py#L102-L104)

```python
if len(self._df_cache) >= 3:
    first_k = next(iter(self._df_cache))
    self._df_cache.pop(first_k, None)
```

This is FIFO eviction (oldest inserted), not LRU. If a user repeatedly views the same date, it will never be evicted (correct), but the entry at the bottom of the insertion order gets dropped even if it was recently accessed.

**Fix**: Use `functools.lru_cache` or `OrderedDict.move_to_end()` on each access, or just use `cachetools.LRUCache`.

---

### 🟡 BUG-CS-002: `CandleService.get_candles` Builds OR-filter Across Token, Symbol, and `NSE:SYMBOL` Simultaneously

**Location**: [`candle_service.py:46-60`](file:///Users/yorha/Documents/Trading%20Stuff/web/candle_service.py#L46-L60)

```python
filters = []
if norm_tok:
    filters.append(pl.col("token") == norm_tok)
if clean_sym:
    filters.append(pl.col("symbol") == clean_sym)
    if not is_option and ":" not in clean_sym:
        filters.append(pl.col("token") == f"NSE:{clean_sym}")  # ← added to same OR chain
...
combined_filter = filters[0]
for f in filters[1:]:
    combined_filter = combined_filter | f
```

The `NSE:{sym}` token filter is OR-combined with the symbol name filter. For equities this means rows that match **either** the symbol name OR the token string are returned, which can include unrelated rows if `symbol` column contains ambiguous values. The correct intent is to match by token OR by symbol (not to union all three).

**Fix**: Normalize all callers to always pass the instrument token (`NSE:SYMBOL`); remove the extra filter arm.

---

## Priority 10 — Alert Service (`web/alert_service.py`)

### 🟢 BUG-AS-001: `_read_shm_candles` Opens SHM on Every Call — No Connection Reuse

**Location**: [`alert_service.py:224-254`](file:///Users/yorha/Documents/Trading%20Stuff/web/alert_service.py#L224-L254)

```python
shm = create_or_attach_shm(name="shm_ohlc_221", create=False)
...
finally:
    shm.close()
```

Each HTTP request that hits `get_alert_candles` with a live session opens and closes the SHM mapping. On macOS this involves a file open/close syscall pair per request. Under 10+ concurrent alert chart requests this creates unnecessary fd churn.

**Fix**: Cache the `shm` handle at `AlertService.__init__` and only reconnect on `OSError`. This is a performance improvement, not a correctness bug.

---

## Priority 11 — Simulation Engine (`simulation/columnar_sim_engine.py`)

### 🟡 BUG-SIM-001: `_run_direct_option_simulation` Hardcodes `start_capital = 250000.0`

**Location**: [`columnar_sim_engine.py:766`](file:///Users/yorha/Documents/Trading%20Stuff/simulation/columnar_sim_engine.py#L766)

```python
"start_capital": 250000.0,
```

The main `run_simulation` path uses `self.capital` (passed from config), but the fallback path hardcodes ₹2,50,000. Same pattern as BUG-018 in live_service.

---

### 🟡 BUG-SIM-002: `get_option_quote` Uses `np.searchsorted` Without Validating Sorted Input

**Location**: [`columnar_sim_engine.py:430`](file:///Users/yorha/Documents/Trading%20Stuff/simulation/columnar_sim_engine.py#L430)

```python
idx = int(np.searchsorted(ts_arr, timestamp, side="right") - 1)
```

`np.searchsorted` requires the array to be sorted. `_build_options_index` sorts by `["token", "exchange_timestamp"]` but only when `exchange_timestamp` is in columns. If the parquet file lacks this column, `ts_arr` is `np.zeros(len)` (all zeros) — `searchsorted` on zeros always returns 0 or len, breaking all quote lookups silently.

**Fix**: Assert `exchange_timestamp` column presence at load time, or add a defensive sort check before building the index.

---

### 🟡 BUG-SIM-003: Batch Simulation `runId` Collides When Multiple Runs Start in Same Second

**Location**: [`simulation_tab.js:695`](file:///Users/yorha/Documents/Trading%20Stuff/web/static/js/simulation_tab.js#L695)

```javascript
payload.run_id = `RUN-${this.runs.length + 1}`;
```

In batch mode, `this.runs.length` is incremented **after** each `runSingleSimAsync` resolves. But `gatherPayload()` on line 474 also uses `this.runs.length + 1` as `runId`. Two runs with the same length at the same time share the same ID. During sequential batch execution this is unlikely but not impossible if the previous run's storage write is async.

**Fix**: Use a monotonically incrementing counter or timestamp-based ID (`RUN-${Date.now()}`).

---

### 🟢 BUG-SIM-004: Delayed EOD Square-off Uses `current_bid` — Falls Back to `entry_price` if Bid Is Zero

**Location**: [`columnar_sim_engine.py:722`](file:///Users/yorha/Documents/Trading%20Stuff/simulation/columnar_sim_engine.py#L722)

```python
bid = trade.current_bid if trade.current_bid > 0 else trade.entry_price
```

For deeply ITM/OTM options that haven't received a quote update since entry, `current_bid` could be stale (from entry time). The EOD square-off at entry_price produces a 0% return, inflating breakeven count.

**Fix**: Use the last-known `get_option_quote(trade.token, last_ts)` for EOD price, same as the active-trade tick loop.

---

## Priority 12 — Simulation Tab JS (`web/static/js/simulation_tab.js`)

### 🟡 BUG-STJ-001: Simulation Run Card Shows "Avg P&L" But Labels It As Total

**Location**: [`simulation_tab.js:880`](file:///Users/yorha/Documents/Trading%20Stuff/web/static/js/simulation_tab.js#L880)

```javascript
const avgNet = trackCount > 0 ? totalNet / trackCount : 0;
...
summaryHtml = `<span>Avg: ${window.App.formatINR(avgNet)}</span>`
```

The card correctly labels this as "Avg", but the `historical_service.py` overall section (BUG-HS-002) feeds the same wrong averaged number into the historical tab. The simulation card is correct; the historical overview is wrong. These two paths need to be consistent.

---

### 🟡 BUG-STJ-002: `pollStatus` Only Clears on `current_job_id === run.id` — Stale Poll if Server Restarted Mid-Run

**Location**: [`simulation_tab.js:765`](file:///Users/yorha/Documents/Trading%20Stuff/web/static/js/simulation_tab.js#L765)

```javascript
if (st.current_job_id !== run.id && !st.has_results && !st.error) {
    return;  // Poll continues indefinitely
}
```

If the server restarts between the launch and status poll, `server_boot_id` changes (detected correctly and clears runs), but if the boot ID check is missed (e.g. server didn't set it on first response), the timer polls forever because `current_job_id` will never match. The `pollCount <= 15` guard only exists in `runSingleSimAsync`, not in `pollStatus`.

**Fix**: Add a max-polls guard to `pollStatus`: after 300 polls (5 minutes at 1s interval) with no progress, mark the run as failed.

---

## Updated Documentation Compliance Summary

| Spec Requirement | Status |
|---|---|
| Dual ZMQ IPC sockets (`feed_equity.ipc`, `feed_options.ipc`) | ✅ Compliant |
| Dynamic ATM ±3 strike subscription with hysteresis | ✅ Compliant |
| Active position protection guard | ✅ Compliant (file-poll sync, 1s lag — see BUG-RMS-002) |
| Single Feed WebSocket (per diagrams) | ⚠️ **Doc Drift** — 2 connections in code |
| `CANDLE_DTYPE` struct definition | ⚠️ **Doc Drift** — ARCH_SPEC §5.1 wrong fields |
| Trade journals as CSV | ⚠️ **Doc Drift** — JSONL in code |
| `start_budget` = configurable capital | ❌ Hardcoded in LiveService, HistoricalService, SimEngine |
| TLS certificate verification | ❌ Disabled in production WS path |
| REST fallback poller | ❌ Stub — not implemented |
| `exchange_timestamp` for bar alignment | ❌ `received_timestamp` used in CandleManager |
| Seqlock correctness for bar finalization | ❌ Previous-bar writes unprotected on transition |
| `warmup_from_parquet` for `--continue` mode | ❌ Crashes with `AttributeError` on first use |
| RMS expiry tag dynamic calculation | ❌ Hardcoded `"26NOV"` — will break after rollover |
| L2 cache residence for SHM reads | ⚠️ **Doc Drift** — only true with `FORCE_POSIX_SHM=1` |

---

## Prioritized Action Queue (Updated — Full System)

| ID | Severity | File | Action |
|---|---|---|---|
| BUG-002 | 🔴 Critical | `feed_handler.py` | Fix stale `_ws_loop` on reconnect — persistent event loop |
| BUG-008 | 🔴 Critical | `feed_handler.py` | Re-enable TLS certificate verification |
| BUG-010 | 🔴 Critical | `live_tab.js` | Guard `exit_price.toFixed(2)` from null |
| BUG-CM-001 | 🔴 Critical | `candle_manager.py:376` | Fix `token_to_idx` → `symbol_to_index` AttributeError on restart |
| BUG-CM-004 | 🔴 Critical | `candle_manager.py:253-300` | Fix seqlock gap on previous-bar finalization writes |
| BUG-HS-001 | 🔴 Critical | `historical_service.py:300` | Whitelist updatable fields in journal merger; protect `entry_price` |
| BUG-011 | 🔴 High | `live_service.py` | Fix averaged vs total net P&L display |
| BUG-014 | 🔴 High | `server.py` | Fix `is_running` race condition in concurrent sim requests |
| BUG-HS-002 | 🔴 High | `historical_service.py:136` | Overall `net_pnl` divides by track count — show total, not average |
| BUG-SE-002 | 🟡 Medium | `rms_service.py:155` | Dynamic expiry tag — hardcoded `"26NOV"` will break on rollover |
| BUG-CM-002 | 🟡 Medium | `candle_manager.py:403` | Use `exchange_timestamp` for bar alignment, not `received_timestamp` |
| BUG-QB-001 | 🟡 Medium | `quotes_broker.py:159` | Validate empty delimiter frame before unpack |
| BUG-QB-002 | 🟡 Medium | `quotes_broker.py:256` | Throttle ref_counts in heartbeat — 15-30 KB/s overhead |
| BUG-SIM-002 | 🟡 Medium | `columnar_sim_engine.py:430` | Validate `exchange_timestamp` column before `searchsorted` |
| BUG-SE-001 | 🟡 Medium | `scoring_engine.py:583` | Set `running=False` before `shm.close()` to avoid exception swallow |
| BUG-004 | 🟡 Medium | `feed_handler.py` | Use stable consistent hash for shard assignment |
| BUG-006 | 🟡 Medium | `feed_handler.py` | Update other-symbol sets after capacity prune |
| BUG-009 | 🟡 Medium | `app.js` | Close old WebSocket before reconnect |
| BUG-012 | 🟡 Medium | `live_service.py` | Guard `start()` against double-call |
| BUG-013 | 🟡 Medium | `simulation_service.py` | Surface errors from worker to UI |
| BUG-RMS-002 | 🟡 Medium | `position_registry.py` | Document 1-second cross-process sync lag for position protection |
| BUG-STJ-002 | 🟡 Medium | `simulation_tab.js:765` | Add max-poll timeout guard to `pollStatus` |
| BUG-CS-002 | 🟡 Medium | `candle_service.py:46` | Fix OR-filter incorrectly ORing token and symbol columns |
| BUG-SIM-003 | 🟡 Medium | `simulation_tab.js:695` | Use monotonic ID for batch run_id — collision risk |
| BUG-018 | 🟡 Medium | `live_service.py` | Load `start_budget` from config |
| BUG-019 | 🟡 Medium | `live_service.py` | Whitelist updatable fields in journal reader |
| BUG-CM-003 | 🟢 Low | `candle_manager.py:276` | Pre-market first-bar volume overcounting |
| BUG-SHM-001 | 🟢 Low | `shm_layout.py` | Doc drift: L2 cache claim only valid with `FORCE_POSIX_SHM=1` |
| BUG-SE-003 | 🟢 Low | `scoring_engine.py:451` | Magic 300.0 bar-seconds constant — parameterize |
| BUG-RMS-001 | 🟢 Low | `rms_service.py:225` | Log rejected alerts (silent blackhole for observability) |
| BUG-AS-001 | 🟢 Low | `alert_service.py:224` | Cache SHM handle in AlertService, don't open/close per request |
| BUG-CS-001 | 🟢 Low | `candle_service.py:102` | FIFO eviction in `_df_cache` — replace with LRU |
| BUG-SIM-001 | 🟢 Low | `columnar_sim_engine.py:766` | Hardcoded `start_capital=250000.0` in fallback path |
| BUG-SIM-004 | 🟢 Low | `columnar_sim_engine.py:722` | EOD square-off uses stale `current_bid` instead of last-known quote |
| BUG-HS-003 | 🟢 Low | `historical_service.py:348` | Hardcoded `brokerage=60.0` default in CSV parser |
| BUG-003 | 🟢 Low | `feed_handler.py` | Remove duplicate `running`/`_running` |
| BUG-005 | 🟢 Low | `feed_handler.py` | Remove dead per-connection ATM/reval maps |
| BUG-007 | 🟢 Low | `feed_handler.py` | Remove `_active_ws` setter |
| BUG-015 | 🟢 Low | `app.js` | Reset `tamCurrentRange = "local"` on trade modal open |
| BUG-016 | 🟢 Low | `simulation_service.py` | Pass cached DataFrames to worker |
| BUG-017 | 🟢 Low | `live_service.py` | O(n²) → O(n log n) intraday curve builder |
| BUG-020 | 🟢 Low | Both services | Centralize brokerage default in `config/strategies.json` |
| BUG-STJ-001 | 🟢 Low | `simulation_tab.js:880` | Align "Avg" labeling consistency with historical_service |
| DOC DRIFT-001 | Docs | Both spec docs | Update diagrams for dual-connection architecture |
| DOC DRIFT-002 | Docs | `WEB_VIEW_FLOW.md` | Fix journal format/filename: CSV → JSONL |
| DOC DRIFT-003 | Docs | ARCH_SPEC + LLD | Sync `CANDLE_DTYPE` definition (ARCH_SPEC §5.1 is wrong) |
| DOC DRIFT-004 | Docs | ARCH_SPEC §5 | Add caveat: L2 cache claim requires `FORCE_POSIX_SHM=1` on Linux |
| DOC DRIFT-005 | Docs | ARCH_SPEC §6 | Document 1-second cross-process position registry sync lag |
| DEAD-001 | Cleanup | `feed_handler.py:206` | `_active_ws` setter never called — remove |
| DEAD-002 | Cleanup | `feed_handler.py:1124` | `_run_poll_stream` hot-sleep stub — implement or remove |
| DEAD-003 | Cleanup | `feed_handler.py:38` | `TickEvent` imported but never constructed — remove import |

---

> [!NOTE]
> **Remaining unaudited files**: `execution/superstructures/base_superstructure.py`, `execution/superstructures/delayed.py`, `execution/superstructures/live.py`, `execution/superstructures/paper.py`, `common/tracks/base_track.py`, `common/tracks/track1_fast_momentum.py`, `common/tracks/track2_trend_pullback.py`, `runner.py`, `web/static/js/historical_tab.js`, `web/static/js/alerts_tab.js`, `web/static/js/chart_engine.js`, `web/static/js/simulation_tab.js` (lines 900–1445), `core/common/ipc_schemas.py`. Audit complete for all P1–P12 components above.
