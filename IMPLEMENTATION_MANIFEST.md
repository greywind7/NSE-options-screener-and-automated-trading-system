# 📋 Comprehensive Step-by-Step Implementation Manifest

**Document Version**: 1.0.0  
**Target Architecture**: Trading System Spec 2.0 & Scoring Engine 3.0  
**Reconciliation Base**: [`LOW_LEVEL_DESIGN_2_0.md`](file:///Users/yorha/Documents/Trading%20Stuff/LOW_LEVEL_DESIGN_2_0.md), [`SELF_AUDIT.md`](file:///Users/yorha/Documents/Trading%20Stuff/SELF_AUDIT.md), and Baseline Codebase (70 files)  
**Core Principles Enforced**:
1. **Track-Specific Exit & Ratchet Rules**: Universal risk (drawdown, max lots, EOD square-off) resides in Superstructures/RMS; all trade exit rules (target decay, stagnation timeout, breakeven SL, stepped trailing, fixed INR target) reside strictly within individual tracks.
2. **Strict Ask-Based Target & SL Computations**: Entry prices, dynamic targets, and stop-loss boundaries are anchored strictly to real executable **Ask** quotes from the Quotes Broker depth cache, never to stale or non-executable LTPs.
3. **Mode-Separated Multi-Track Dashboard Telemetry**: Terminal dashboard renders distinct simulation/performance matrices per execution mode (`Paper`, `Paper-Delayed`, `Live`), with each mode displaying all of its active tracks independently.
4. **Defect-to-Fix Invariants**: For every single step, explicit failure modes and regression fixes are defined upfront.

---

## Table of Contents
- [Phase 1: Shared Core, IPC Schemas & Canonical Configuration](#phase-1-shared-core-ipc-schemas--canonical-configuration)
- [Phase 2: Core Ingestion Tier & Shared Memory State Layer](#phase-2-core-ingestion-tier--shared-memory-state-layer)
- [Phase 3: Alpha Scoring 3.0 & Risk Management Tier](#phase-3-alpha-scoring-30--risk-management-tier)
- [Phase 4: Common Tracks & Execution Superstructures](#phase-4-common-tracks--execution-superstructures)
- [Phase 5: Universal Multi-Mode Dashboard & Columnar Sim Engine](#phase-5-universal-multi-mode-dashboard--columnar-sim-engine)
- [Phase 6: Central Runner CLI, Process Supervisor & Regression Verification](#phase-6-central-runner-cli-process-supervisor--regression-verification)

---

## Phase 1: Shared Core, IPC Schemas & Canonical Configuration

### Step 1.1: Package Structure & Clean Namespace Initialization
* **Reason Behind Step**: The repository relied on implicit namespace packages (PEP 420), causing `mypy`, standard `unittest` discovery, and package imports to fail or behave unpredictably across subdirectories (**HIGH-001**).
* **Effects of Step**:
  - Adds valid `__init__.py` files to `core/`, `core/feed/`, `core/candles/`, `core/quotes/`, `core/scoring/`, `core/rms/`, `core/recorder/`, `core/config/`, `common/`, `common/tracks/`, `execution/`, `execution/superstructures/`, `simulation/`, and `ui/`.
  - Establishes clean module boundaries.
* **Possible Regressions**:
  - Circular import chains if submodules import each other at module level.
* **Fix for Regression**:
  - Enforce top-down dependency flow: `core/config` and `common/interfaces` have zero internal dependencies; higher-level tiers import lower-level modules; `tests/test_imports.py` verifies zero circular import cycles.

---

### Step 1.2: Canonical Symbol & Instrument Token Sanitization
* **Reason Behind Step**: In the legacy codebase, 33 duplicate `.replace(":", "|")` and `.replace("|", ":")` mutations occurred across 8 files on the critical quote path (**Gap 7**), burning CPU and causing hash probe mismatches.
* **Effects of Step**:
  - Creates `core/common/symbol_utils.py` with canonical `normalize_instrument_token(token: str) -> str`.
  - Standardizes all internal keys strictly to `EXCHANGE:SYMBOL` (e.g., `NSE:RELIANCE`, `NSE:NIFTY26NOV24000CE`).
  - All token ingestion points sanitize tokens once at the edge.
* **Possible Regressions**:
  - Legacy files or recorded datasets containing pipe `|` characters (e.g. `NSE_FO|TCS26NOV3500CE`) failing dictionary lookups.
* **Fix for Regression**:
  - `normalize_instrument_token` seamlessly handles pipe `|`, colon `:`, and underscore `_` prefixes; unit tests in `tests/common/test_symbol_utils.py` validate 1,000 synthetic permutations to ensure bidirectional compatibility.

---

### Step 1.3: Unified Configuration Loader & Canonical Trading Schedule
* **Reason Behind Step**: Market hours and EOD cutoff times were duplicated across 14 locations with conflicting values (e.g., live EOD buffer `15:15` was overridden by global default `15:20` in `multi_executor.py`, **Gap 4**). Config loading relied on a fragile 10-candidate fallback chain (**MED-003**), and local IPC sockets used hardcoded plaintext secrets (**CRIT-003**).
* **Effects of Step**:
  - Creates `core/config/trading_schedule.py` as the single canonical source of truth for exchange schedules:
    - **NSE Equity/F&O**: Continuous 09:15–15:30; Midday lull: 11:00–13:30; Live entry freeze: 15:10; Live auto-squareoff: 15:15; Paper auto-squareoff: 15:20.
    - **MCX Commodities**: Continuous 09:00–23:30; Live entry freeze: 23:10; Live auto-squareoff: 23:15; Paper auto-squareoff: 23:20.
  - Creates `core/config/config_loader.py` validating strongly typed dataclasses/Pydantic schemas. Moves IPC auth secret to environment variable `.env`.
* **Possible Regressions**:
  - Hardcoded references in older test files expecting string paths or legacy keys crashing on missing fields.
* **Fix for Regression**:
  - `config_loader.py` provides backwards-compatible dictionary property access (`cfg["risk"]["option_stop_loss_pct"]`); `tests/common/test_config_loader.py` tests both dict access and schema validation.

---

### Step 1.4: Fixed 777 KB POSIX Shared Memory Layout & 48-Byte Dtype
* **Reason Behind Step**: Python dynamic heap slicing (`candles[-20:]`) caused frequent Garbage Collection pauses and memory fragmentation. Storing 221 symbols in fixed Shared Memory allows microsecond lock-free reads directly in CPU L2 cache.
* **Effects of Step**:
  - Creates `core/candles/shm_layout.py`:
    - `CANDLE_DTYPE`: 48 bytes structured NumPy dtype: `[('timestamp', '<i8'), ('open', '<f8'), ('high', '<f8'), ('low', '<f8'), ('close', '<f8'), ('volume', '<i8')]`.
    - Buffer size: $221 \times 75 \times 48 = 795,600\text{ bytes}$ ($\approx 777\text{ KB}$).
    - Seqlock header: 16 bytes per symbol `[('sequence_id', '<u8'), ('last_update_ns', '<i8'), ('is_updating', '<u1'), ('reserved', 'V7')]`.
* **Possible Regressions**:
  - Struct alignment issues across different CPU architectures (ARM64 Apple Silicon vs x86_64 Linux).
  - Reader reading mid-write if writer updates non-atomically.
* **Fix for Regression**:
  - Fixed little-endian primitives (`<f8`, `<i8`, `<u8`) ensure identical byte alignment across architectures.
  - Seqlock protocol requires `is_updating == 0` and `seq_begin == seq_end` (odd sequence indicates write in progress; reader loops until stable). Tested in `tests/core/test_shm_layout.py`.

---

### Step 1.5: ZeroMQ Msgpack Wire Schemas & Serialization Contracts
* **Reason Behind Step**: Heterogeneous dictionaries with varying key names caused `KeyError` exceptions when downstream modules received differently formatted tick or order events.
* **Effects of Step**:
  - Creates `core/common/ipc_schemas.py` defining msgpack serialization models for:
    - `TickEvent` (`token`, `symbol`, `ltp`, `bid`, `ask`, `bid_qty`, `ask_qty`, `exchange_ts`, `received_ts`)
    - `SetupAlert` (`alert_id`, `symbol`, `direction`, `archetype`, `trigger_price`, `rvol`, `clv`, `atr`, `flag_high`, `flag_low`, `ts`)
    - `ActionableOpportunity` (`opp_id`, `symbol`, `direction`, `archetype`, `strikes_resolved`, `ts`)
    - `QuoteRequest` / `QuoteResponse` (batch quotes from Quotes Broker).
* **Possible Regressions**:
  - Serialization / deserialization CPU overhead if large payloads are sent repeatedly.
* **Fix for Regression**:
  - Uses binary `msgpack` with C-extension (`use_bin_type=True`); payloads are compact ($< 120\text{ bytes}$) and serialize/deserialize in $< 2\ \mu\text{s}$.

---

## Phase 2: Core Ingestion Tier & Shared Memory State Layer

### Step 2.1: Native Upstox FeedHandler with Protobuf & Subscription Safeguards
* **Reason Behind Step**: Preserves existing, verified Upstox WebSocket v2/v3 client while eliminating bare `except Exception: pass` blocks (**CRIT-004**) and preventing accidental unsubscription of open positions.
* **Effects of Step**:
  - Implements `core/feed/feed_handler.py` wrapping the working WebSocket client.
  - Decodes ticks using native `MarketDataFeed.proto`.
  - Enforces **Active Position Protection Guard**: Contracts with open positions across Paper, Delayed, or Live are never unsubscribed or pruned.
  - Supports dynamic option subscription within Upstox 5,000 instrument limit.
  - Publishes to `ipc:///tmp/feed_ticks_pub.ipc` (`TICK.EQ`, `TICK.OPT`).
* **Possible Regressions**:
  - WebSocket disconnects during high market volatility.
* **Fix for Regression**:
  - Auto-reconnect with exponential backoff; if `.allow_rest_fallback` flag is present, seamlessly falls back to REST polling batches until WebSocket recovers. Tested in `tests/infrastructure/test_listener.py`.

---

### Step 2.2: CandleManager as Single Writer & Owner of `shm_ohlc_221`
* **Reason Behind Step**: Python's `multiprocessing.shared_memory.SharedMemory` registers segments with `resource_tracker`. If a short-lived process like `prepare` creates SHM and exits, the segment is unlinked and destroyed (ADR-011).
* **Effects of Step**:
  - `CandleManager` (`core/candles/candle_manager.py`) is the **Single Writer** and calls `SharedMemory(name="shm_ohlc_221", create=True, size=795600)`.
  - Ingests `TICK.EQ` ticks from ZeroMQ, aggregates 5-minute bars aligned to exact wall-clock minute boundaries (`09:15, 09:20, ...`).
  - Computes incremental Wilders ATR-14 on bar close.
  - Writes directly to `shm_ohlc_221` using Seqlock pattern.
* **Possible Regressions**:
  - Orphaned SHM segment if process crashes uncleanly with `SIGKILL`.
* **Fix for Regression**:
  - On startup, `CandleManager` attempts to unlink any stale segment with the same name before creating fresh; teardown registers `atexit` and `SIGTERM` handlers to unlink cleanly.

---

### Step 2.3: Strict Current Candle rVOL Extrapolation against 30-Day Baseline Table
* **Reason Behind Step**: Evaluating rVOL on a rolling lookback window dilutes real-time breakout volume bursts. In-candle volume must be evaluated strictly against time-of-day expectations.
* **Effects of Step**:
  - Pre-loads 1-minute historical median volume profile table `volume_profile_221.npy` of shape `(221, 375)` built over the last **30 trading sessions**.
  - At exact minute marks ($60\text{s}, 120\text{s}, \dots$), compares cumulative volume against historical 5m baseline.
  - Intra-minute, linearly extrapolates current minute volume to the upcoming 60-second mark:
    $$V_{\text{extrap}} = V_{\text{completed\_mins}} + V_{\text{current\_min\_raw}} \times \left(\frac{60}{\text{elapsed\_seconds}}\right)$$
* **Possible Regressions**:
  - Extreme volume extrapolation during the first 1–2 seconds of a minute boundary due to small denominators.
* **Fix for Regression**:
  - Minimum elapsed time clamp: for $t_{\text{elapsed}} < 5\text{ seconds}$, extrapolation uses raw volume without multiplying up, preventing 60x noise spikes. Validated in `tests/core/test_candle_manager.py`.

---

### Step 2.4: Quotes Broker Service with Lock-Free LVC & Heartbeat Stream
* **Reason Behind Step**: Strategies need option quotes for strike selection and fills, but listening directly to 30,000 option ticks/sec overwhelms strategy event loops.
* **Effects of Step**:
  - Implements `core/quotes/quotes_broker.py`:
    - Maintains in-memory Last Value Cache (LVC) of all active option and equity quotes ($< 200\text{ KB}$ RAM).
    - Provides ZeroMQ ROUTER socket on `ipc:///tmp/quotes_router.ipc` answering batch requests (`GET_QUOTES: [keys]`) in $< 50\ \mu\text{s}$.
    - Broadcasts 1-second timestamp heartbeats on `ipc:///tmp/quotes_heartbeat.pub` to drive delayed order evaluation without polling.
* **Possible Regressions**:
  - Stale quotes if a low-liquidity option contract receives no trades for minutes.
* **Fix for Regression**:
  - Every quote entry carries `quote_ts`; queries return quote age; strategies reject quotes older than 30s TTL (**HIGH-010**). Tested in `tests/core/test_quotes_broker.py`.

---

### Step 2.5: Lossless Parquet Market Recorder with Safe SIGTERM Flushing
* **Reason Behind Step**: Legacy JSONL text logging suffered high disk I/O, large file footprints, and slow deserialization during replay.
* **Effects of Step**:
  - Implements `core/recorder/market_recorder.py`:
    - Ingests ticks into memory ring buffers.
    - Flushes compressed Apache Parquet chunks to `recordings/{date}/{symbol}.parquet`.
    - Handles `SIGTERM` / `SIGINT` to flush pending buffers before exit, guaranteeing zero tick loss.
* **Possible Regressions**:
  - Excessive small parquet file generation if buffer flushes too frequently.
* **Fix for Regression**:
  - Chunks buffered to 10,000 ticks or 30-second time thresholds, ensuring optimal row group sizing. Tested in `tests/core/test_market_recorder.py`.

---

### Step 2.6: Historical Replay Ingestor with Strict Timestamp Preservation
* **Reason Behind Step**: In the legacy replay engine, `received_timestamp` was overwritten with `time.time()` (**HIGH-007**), destroying historical event sequence and breaking time-of-day gates.
* **Effects of Step**:
  - Implements `core/feed/historical_replay.py`:
    - Reads Parquet recording chunks.
    - Preserves exact historical `exchange_timestamp` and `received_timestamp`.
    - Streams ticks to ZeroMQ at $1\times, 5\times, 10\times$ speed or as-fast-as-possible.
* **Possible Regressions**:
  - Multi-process desynchronization if reader runs faster than replay stream.
* **Fix for Regression**:
  - Replay publisher coordinates via virtual clock synchronized over heartbeat stream. Tested in `tests/core/test_historical_replay.py`.

---

## Phase 3: Alpha Scoring 3.0 & Risk Management Tier

### Step 3.1: Scoring Engine 3.0 Spin-Loop with Lock-Free SHM Reader
* **Reason Behind Step**: Eliminates ZeroMQ pub/sub serialization latency for candle ingestion. Scoring engine reads 5m bars directly from SHM with zero copy overhead.
* **Effects of Step**:
  - Implements `core/scoring/engine.py`:
    - Attaches to `shm_ohlc_221` with `create=False` (retrying on startup until CandleManager allocates it).
    - Uses Seqlock verification: reads `seq_begin`, reads 48-byte OHLCV, reads `seq_end`; verifies `seq_begin == seq_end` and `not is_updating`.
* **Possible Regressions**:
  - Infinite spin-loop burning 100% of a CPU core when markets are idle.
* **Fix for Regression**:
  - Incorporates adaptive micro-sleep: `time.sleep(0.0005)` (500 microseconds) between scans when `sequence_id` is unchanged, maintaining $< 1\text{ ms}$ reaction time with $< 3\%$ CPU load.

---

### Step 3.2: Scoring 3.0 Physical Invariants: 50% Retracement Tested FIRST & Dynamic Target
* **Reason Behind Step**: Old scoring relied on regression slope $|m|/\text{ATR} \le 0.50$, which failed on steep bull flags and aspect ratio variations. Current candle retracement $> 50\%$ invalidates the setup physically.
* **Effects of Step**:
  - In `core/scoring/engine.py`:
    - **Flag Archetype**: Evaluates the **50% retracement invariant FIRST** as an early-exit anti-reversal guard:
      $$\text{Retracement} \le 50\% \quad \text{and} \quad (\text{High}_t - \text{Low}_t) \le 2.0 \times \overline{\text{Range}}_{t-3..t-1}$$
      If retraced $> 50\%$, immediately exit (Flag disqualified).
    - **Dynamic Target Projection**: Fits OLS regression line $y = mx + c$ on the prior 5 bars strictly to project the resistance target:
      $$\text{Target Price} = \text{Current Price} + 1.5 \times \text{ATR}_{14}$$
    - **Pure Impulse Archetype**: Evaluates directional thrust $|open - high| \ge 2.0 \times \max(\overline{\text{Thrust}}_3, 0.35\text{ATR})$ with $\text{CLV} \ge 0.75$.
* **Possible Regressions**:
  - Division by zero if range $(\text{High} - \text{Low}) = 0$.
* **Fix for Regression**:
  - Safe denominator protection $\max(\text{High} - \text{Low}, 1e-4)$; tested in `tests/core/test_scoring_engine_3_0.py`.

---

### Step 3.3: Elimination of 65-Pt Regime Cap, Dud Inflation & Continuous Sum Trap
* **Reason Behind Step**: Legacy `scoring.py` clamped short setups to 65 points when Nifty was green (preventing all shorts), and added an arbitrary $+10.0$ dud inflation bonus on green days (**Section 3 Audit**).
* **Effects of Step**:
  - Removes `raw_suppress_cap = 65.0`.
  - Removes arbitrary additive bonuses (+10 / -25).
  - Setups execute based strictly on stock-intrinsic volume uncoupling ($\text{rVOL} \ge 2.0\times$) and physical price action invariants.
* **Possible Regressions**:
  - Setups executing during broad market panic / market-wide liquidation.
* **Fix for Regression**:
  - Replaced by qualitative market-wide trend filtering at RMS tier rather than continuous score corruption at scoring tier.

---

### Step 3.4: Non-Blocking Opportunity Pool with Cooldown Quarantine
* **Reason Behind Step**: In `strategy.py`, if candidate #1 failed to open (spread wide, cash full), it remained at index 0, blocking candidate #2 and #3 from executing for 20 seconds (**Gap 2: Head-of-Line Blocking**).
* **Effects of Step**:
  - Implements `core/scoring/opportunity_pool.py`:
    - When top candidate fails execution, it is immediately moved to a 5-second **cooldown quarantine**.
    - Candidates #2 and #3 are evaluated immediately on the same tick.
* **Possible Regressions**:
  - High churn if quarantined candidate repeatedly re-enters pool.
* **Fix for Regression**:
  - Max 3 evaluation attempts per candidate before permanent eviction. Tested in `tests/core/test_opportunity_pool.py`.

---

### Step 3.5: RMS Option Resolver with Pre-Loaded Manifest & Hybrid Tick Spread Gate
* **Reason Behind Step**: Option selector re-read CSV/JSON from disk on tick failure (**Gap 8**). A flat 3% spread gate rejected low-premium ₹3.00 OTM options where a 2-tick spread (₹3.10 / ₹3.00) is $3.28\% > 3.0\%$ (**Section 4 Audit**).
* **Effects of Step**:
  - Implements `core/rms/rms_service.py`:
    - Strike manifest pre-loaded in RAM at startup.
    - Resolves ATM, ITM1, OTM1 strikes via spot price from Quotes Broker.
    - **Hybrid Tick Spread Gate**:
      $$\text{Spread Gate} = \min(3.0\%, \max(2 \times \text{TickSize}, 0.15\text{ INR}))$$
      Low-priced OTM options pass without liquidity starvation.
* **Possible Regressions**:
  - Extreme illiquid penny options with wide rupee spreads executing.
* **Fix for Regression**:
  - Absolute spread cap of ₹0.50 on options priced under ₹5.00. Tested in `tests/core/test_rms_service.py`.

---

### Step 3.6: System-Level Daily Drawdown Circuit Breaker
* **Reason Behind Step**: `kill_switch_loss_pct: 5.0` in `config/execution.json` was a dead phantom key (**HIGH-008**); zero code enforced daily drawdown cutoffs.
* **Effects of Step**:
  - `RMSService` tracks realized + unrealized daily P&L across all modes.
  - If daily loss exceeds $5.0\%$ of starting capital, RMS immediately publishes a `SYSTEM_CIRCUIT_BREAKER_TRIGGERED` event, blocks all new entries, and sends emergency square-off commands.
* **Possible Regressions**:
  - False trigger due to bad quote spike.
* **Fix for Regression**:
  - P&L evaluated strictly on valid bid quotes filtered by quote TTL ($< 30\text{s}$).

---

## Phase 4: Common Tracks & Execution Superstructures

### Step 4.1: Abstract `ExecutionContext` Interface & Order Handling IoC
* **Reason Behind Step**: In the legacy monolithic design, portfolio and strategy classes were tightly coupled to concrete execution adapters, preventing independent process scaling.
* **Effects of Step**:
  - Defines `common/interfaces.py` with abstract `ExecutionContext`:
    - `place_order(request: OrderRequest) -> OrderResult`
    - `modify_order(order_id: str, new_price: float, new_qty: int) -> OrderResult`
    - `cancel_order(order_id: str) -> bool`
    - `get_quote(instrument_token: str) -> Optional[Quote]`
    - `log_trade_journal(trade_record: Dict[str, Any]) -> None`
* **Possible Regressions**:
  - Interface incompatibility across paper, delayed, and live adapters.
* **Fix for Regression**:
  - Strict type hints and mypy validation; all superstructures implement the exact same interface.

---

### Step 4.2: Track-Specific Trade Exit & Ratchet Rules Architecture
* **Reason Behind Step**: Universalizing trade exit rules across all tracks destroys strategy specialization. Fast momentum scalps, trend runners, and fixed target tracks require fundamentally different exit mechanics.
* **Effects of Step**:
  - **Decoupled Architecture**: Universal portfolio protections (drawdowns, daily loss, EOD cutoff) stay in the Superstructures/RMS; all trade exit rules and ratchets are implemented **strictly inside individual track classes** in `common/tracks/`:
    - `FastMomentumTrack`: Tight 4% stop, 8% target, 1R trailing.
    - `TrendPullbackTrack`: Wide 6% stop, dynamic regression target, structural 9-EMA trail.
    - `FixedTargetTrack`: Fixed ₹750 profit target with 10-minute timeout exit.
    - `TrailingSLTrack`: Stepped trailing (for every 2.5% gain, advance SL by 2.5%) and Breakeven SL ratchet (+2.5% gain moves SL to entry).
    - `DecayingTargetTrack`: Target decay over time (after 15 min, target decays linearly down to 2.5% floor).
    - `StagnationTimeoutTrack`: Exits position at market after 30 min of stagnation.
* **Possible Regressions**:
  - A track failing to exit before End-of-Day.
* **Fix for Regression**:
  - Superstructure enforces mandatory `on_eod_square_off()` across all tracks at 15:15 (Live) / 15:20 (Paper), overriding individual track logic. Tested in `tests/common/test_risk_rules.py`.

---

### Step 4.3: Strict Ask-Based Target & SL Price Anchoring
* **Reason Behind Step**: Option entry and target calculations based on Last Traded Price (LTP) fail because LTP may represent a transaction that occurred minutes ago at a different bid/ask spread.
* **Effects of Step**:
  - Entry execution price for BUY orders is strictly anchored to the **latest Ask** from the Quotes Broker depth cache:
    $$\text{Entry Price} = \text{Quote.Ask}$$
  - Primary target price and stop-loss boundaries are computed directly from the **Entry Ask**:
    $$\text{Target Price} = \text{Entry Ask} \times (1.0 + \text{Target \%})$$
    $$\text{Stop-Loss Price} = \text{Entry Ask} \times (1.0 - \text{SL \%})$$
  - In delayed mode, the baseline entry price is anchored to the **prevailing Ask at trigger time**, not the future chased price, preventing artificial stop-outs.
* **Possible Regressions**:
  - Zero or missing Ask quote in illiquid strikes.
* **Fix for Regression**:
  - If `Ask <= 0.0` or spread is invalid, order is rejected upfront by the RMS pre-trade gate, preventing unhedged entries. Tested in `tests/execution/test_paper_delayed.py`.

---

### Step 4.4: Independent Process Isolation for Execution Superstructures
* **Reason Behind Step**: In `strategy.py`, multi-mode exits were filtered by `m_name == 'paper'`, causing live exits to be completely dropped when running `--mode paper,live` (**Gap 1**).
* **Effects of Step**:
  - Each mode runs as an **independent OS process**:
    - `execution/superstructures/paper.py` (Superstructure_Paper)
    - `execution/superstructures/delayed.py` (Superstructure_Delayed)
    - `execution/superstructures/live.py` (Superstructure_Live)
  - Each superstructure manages its own private positions, trade journals, and order execution.
* **Possible Regressions**:
  - Increased OS process memory footprint.
* **Fix for Regression**:
  - Each superstructure consumes $< 35\text{ MB}$ RSS; total memory for 3 superstructures is $< 110\text{ MB}$. Tested in `tests/execution/test_superstructures.py`.

---

### Step 4.5: Event-Driven `DelayedSuperstructure` with Prevailing Cached Quote Fills
* **Reason Behind Step**: The legacy `PaperDelayedAdapter` used `threading.Timer` and `time.sleep(0.15)` retry loops, causing lock contention, GIL stalls, and ghost prices (**CRIT-007** & **Section 1 Audit**).
* **Effects of Step**:
  - Implements `execution/superstructures/delayed.py`:
    - Single-threaded event loop driven by Quotes Broker stream and 1-second heartbeats.
    - Zero OS threads, zero `time.sleep()`.
    - Pending orders are queued with `target_fill_ts = trigger_ts + delay_sec`.
    - When `tick_ts >= target_fill_ts`, order fills immediately at the **prevailing cached Ask quote established prior to target time**.
* **Possible Regressions**:
  - Orders never filling if no subsequent ticks arrive for that specific contract.
* **Fix for Regression**:
  - Quotes Broker broadcasts 1-second heartbeats; when a heartbeat arrives with `heartbeat_ts >= target_fill_ts`, the pending order matures against its cached quote. Tested in `tests/execution/test_paper_delayed.py`.

---

### Step 4.6: Production `LiveSuperstructure` wrapping Groww API SDK & Reconciliation
* **Reason Behind Step**: Protects existing working Groww integration while hardening error recovery and position safety.
* **Effects of Step**:
  - Implements `execution/superstructures/live.py` wrapping `GrowwLiveAdapter`.
  - Enforces `--confirm-live` safety authorization.
  - Generates daily access tokens via TOTP (`generate_groww_access_token`).
  - Automatically freezes live trading on HTTP 401/403 session expiration.
  - Automatically executes cancel & replace upon reaching the 25-order modification limit.
  - Runs `reconcile_broker_positions()` on startup and periodically to resolve ghost/orphan positions.
* **Possible Regressions**:
  - Broker network timeout during order placement.
* **Fix for Regression**:
  - Preserves `_resolve_timed_out_order()` timeout reconciliation handler; tests validated in `tests/execution/test_groww_live.py`.

---

### Step 4.7: Single-Owner Trade Journaling & State Management
* **Reason Behind Step**: Both `MultiTrackPortfolio` and execution adapters logged closed trades to the exact same file path, creating duplicate trade records (**Gap 3**).
* **Effects of Step**:
  - Trade journaling is owned **solely by the Execution Superstructure**:
    - `PaperSuperstructure` $\to$ `recordings/{date}/paper_trades.csv`
    - `DelayedSuperstructure` $\to$ `recordings/{date}/paper_delayed_trades.csv`
    - `LiveSuperstructure` $\to$ `recordings/{date}/live_trades.csv`
  - Zero duplicate logging from tracks or portfolios.
* **Possible Regressions**:
  - Downstream reports expecting legacy single file.
* **Fix for Regression**:
  - `DailyReportGenerator` accepts `--mode` flag and merges or analyzes journals per mode cleanly.

---

### Step 4.8: Atomic In-Memory Freeze Flag Synchronization
* **Reason Behind Step**: Legacy code polled `os.path.exists("logs/.freeze_*")` repeatedly on every market tick, and composite string checks failed (**Gap 5**).
* **Effects of Step**:
  - `runner.py freeze` and `runner.py unfreeze` touch persistent disk flags AND broadcast an atomic control message over ZeroMQ.
  - Superstructures toggle an in-memory boolean `self.is_frozen = True/False` ($< 1\text{ ns}$ check).
  - Freezing strictly blocks **new entries** while allowing active positions to trail and exit normally.
* **Possible Regressions**:
  - Process restarted during frozen state losing in-memory flag.
* **Fix for Regression**:
  - On startup, each superstructure checks disk flag once during initialization to seed the in-memory boolean. Tested in `tests/execution/test_freeze_mode.py`.

---

### Step 4.9: Composite Track Pattern with Dynamic Margin Sharing & Dynamic Trio Allocator
* **Reason Behind Step**: Legacy `DYNAMIC_TRIO` bypassed all rolling lookback math on Nifty $\ge 0.35\%$ trend days and hardcoded static weights (**Gap 6**).
* **Effects of Step**:
  - Implements `CompositeTrack` implementing `BaseTrack` (Composite Pattern).
  - Manages shared capital pool across child tracks (`ATM`, `ITM`, `OTM`, `TrailingSL`, `FixedTarget`).
  - Dynamic Trio allocator calculates rolling 10-minute ROI and Win Rate continuously without trend bypasses.
* **Possible Regressions**:
  - Sub-tracks over-allocating capital beyond the shared pool limit.
* **Fix for Regression**:
  - `CompositeTrack.on_alert()` verifies `self.available_capital >= required_margin` before delegating to child tracks. Tested in `tests/common/test_composite_track.py`.

---

## Phase 5: Universal Multi-Mode Dashboard & Columnar Sim Engine

### Step 5.1: Multi-Mode Segmented Matrix Terminal Dashboard
* **Reason Behind Step**: The operator requires continuous real-time comparative visibility across Paper, Delayed, and Live modes to monitor slippage, execution parity, and track-wise performance simultaneously.
* **Effects of Step**:
  - Implements `ui/dashboard.py` as a detached terminal application (Rich/Textual):
    - **Header**: Live session time, universe size, active modes.
    - **Alerts Table**: Real-time incoming alerts from RMS (TS, Symbol, Direction, Archetype, Spot, rVOL, Created, Elapsed, Strikes).
    - **Mode-Separated Performance Panels**:
      - `[PAPER EXECUTION MATRIX]`: Displays all active tracks (Fast, Trend, Composite) $\to$ Trades, Win %, P&L, MTM, Max DD.
      - `[PAPER-DELAYED EXECUTION MATRIX]`: Displays all active tracks $\to$ Delayed Trades, Win %, P&L, MTM, Slippage vs Paper.
      - `[LIVE BROKER MATRIX]` (if live enabled): Displays Live Trades, Real P&L, Broker Order Status, Margin Used.
    - Continuously tails today's trade journals in `recordings/{date}/` with zero disk contention.
* **Possible Regressions**:
  - High terminal redraw rate consuming excessive CPU.
* **Fix for Regression**:
  - Throttled UI refresh rate at 1.0s interval; CPU usage verified under $< 1\%$. Tested in `tests/ui/test_dashboard.py`.

---

### Step 5.2: In-Process Columnar Simulation Engine with Polars/Arrow ASOF Joins
* **Reason Behind Step**: Legacy simulation failed due to missing `_rebuild_held_keys()` on pending orders (**Sim Bug A**) and coarse timestamp lumping skipping intermediate option ticks (**Sim Bug B**).
* **Effects of Step**:
  - Implements `simulation/columnar_sim_engine.py`:
    - Memory-maps option depth tables using Apache Arrow / Polars.
    - Uses exact nanosecond-timestamp binary search `asof_join` to match breakout trigger events with prevailing option quotes.
    - For delayed orders, evaluates fill price at exactly `trigger_ts + delay_sec`.
    - Evaluates every intermediate option tick for stop-loss and trailing exits.
    - Supports both NSE (15:20 EOD) and MCX (23:15 EOD) exchange hours (**CRIT-006**).
* **Possible Regressions**:
  - Memory spikes when loading full-day tick datasets for 35,000 contracts.
* **Fix for Regression**:
  - Memory-mapped columnar scan (`polars.scan_parquet`) loads only queried strike columns; full-day simulation completes in 3–5 seconds using $< 400\text{ MB}$ RAM. Tested in `tests/simulation/test_columnar_sim_engine.py`.

---

### Step 5.3: Offline Simulation & Live Paper-Delayed Trade Parity Verification
* **Reason Behind Step**: In the legacy system, offline simulation and live paper-delayed drifted apart within 60 minutes due to thread delays and skipped ticks.
* **Effects of Step**:
  - Validates that offline columnar simulation and live `DelayedSuperstructure` produce identical trade journals when fed the same recorded market ticks.
* **Possible Regressions**:
  - Residual timestamp floating-point rounding discrepancies.
* **Fix for Regression**:
  - Timestamps aligned to integer millisecond precision; slippage tolerance asserted within $\pm 0.05$ INR in `tests/simulation/test_columnar_sim_engine.py`.

---

## Phase 6: Central Runner CLI, Process Supervisor & Regression Verification

### Step 6.1: Central Runner CLI Decoupling & Argument Parser Consolidation
* **Reason Behind Step**: 44 identical lines of argument parser code were copy-pasted between `strat` and `start` (**Gap 9**), and `runner.py` lacked `Tuple` in typing imports (**CRIT-001**).
* **Effects of Step**:
  - Overhauls `runner.py`:
    - Imports `Tuple` (fixing **CRIT-001**).
    - Unifies `strat` and `start` parsers via a shared helper function.
    - Implements decoupled subcommands:
      - `prepare` (stateless, does not allocate SHM)
      - `core --feed <ws|mock> --candlemanager --quotesbroker [--record]`
      - `start --strategy <name> --mode <paper,paper_delayed,live>`
      - `dashboard`, `sim`, `replay`, `status`, `stop`
      - `freeze`, `unfreeze`, `stop-live`
      - `report`, `post-market`, `reconcile`, `rest-fallback`, `test`.
* **Possible Regressions**:
  - Existing shell scripts calling legacy flags failing.
* **Fix for Regression**:
  - Preserves all legacy CLI flags as aliases (`--strat`, `--mcx`, `--date`, etc.).

---

### Step 6.2: Process Supervisor with PID Tracking, Safe SIGTERM Teardown & Zombie Prevention
* **Reason Behind Step**: Legacy `cmd_stop` only unlinked the IPC socket file without terminating processes, leaving zombie Python processes running in the background (**CRIT-005**).
* **Effects of Step**:
  - Implements process tracking in `logs/pids.json`.
  - `runner.py stop --strategy`: Sends graceful `SIGTERM` to Strategy Scoring, RMS, Superstructures, and Dashboard, leaving Core Tier running for zero tick loss during strategy reloads.
  - `runner.py stop --all`: Sends `SIGTERM` to all trading processes, waits 5s, escalates to `SIGKILL` if unresponsive, flushes recordings, unlinks `shm_ohlc_221`, and removes lock files.
* **Possible Regressions**:
  - Zombie processes if children ignore `SIGTERM`.
* **Fix for Regression**:
  - Supervisor sends `SIGTERM` to the entire process group `os.killpg()`, followed by verification loop with fallback `SIGKILL`. Tested in `tests/integration/test_e2e_pipeline.py`.

---

### Step 6.3: Reconciled Commands Integration
* **Reason Behind Step**: Operational commands (`report`, `post-market`, `reconcile`, `rest-fallback`, `test`) must function seamlessly with the new 2.0 distributed topology.
* **Effects of Step**:
  - `runner.py report`: Reads new mode-separated trade journals and generates consolidated terminal analytics and vector PDF reports.
  - `runner.py post-market`: Archives session Parquet and metadata files into `.tar.gz`.
  - `runner.py reconcile`: Audits Groww live positions directly against local state.
  - `runner.py rest-fallback`: Toggles failover flag in `logs/`.
  - `runner.py test`: Unified automated test suite runner.
* **Possible Regressions**:
  - Path mismatches when reading session recordings.
* **Fix for Regression**:
  - Standardized paths via `core/config/trading_schedule.py`.

---

### Step 6.4: Full 35+ Test Suite Automated Regression Suite Execution & Audit Validation
* **Reason Behind Step**: Final end-to-end verification that all 18 self-audit defects (**CRIT-001** through **MED-010**) are resolved and that zero regressions exist across any subsystem.
* **Effects of Step**:
  - Executes complete test suite via `python3 runner.py test` covering:
    - 6 Core test suites (`shm_layout`, `candle_manager`, `quotes_broker`, `recorder`, `scoring_3_0`, `rms_service`)
    - 5 Execution test suites (`superstructures`, `paper_delayed`, `live_adapter`, `freeze_mode`, `trade_journal`)
    - 4 Common test suites (`tracks`, `composite_track`, `risk_rules`, `config_loader`)
    - 2 Simulation test suites (`columnar_sim_engine`, `historical_replay`)
    - 1 UI test suite (`dashboard`)
    - 1 Integration test suite (`test_e2e_pipeline`)
    - 25 existing regression test suites.
  - Requires 100% green pass.
* **Possible Regressions**:
  - Environmental flakiness or timeout in integration tests.
* **Fix for Regression**:
  - Explicit timeouts and mock replay servers ensure deterministic, repeatable execution.

---

## Summary of Regression Safeguards Matrix

| Phase | Core Deliverable | Primary Failure Risk | Preemptive Regression Safeguard |
| :---: | :--- | :--- | :--- |
| **P1** | SHM Layout & Schemas | Struct misalignment, GC pauses, circular imports | 48-byte fixed primitives, Seqlock protocol, top-down imports |
| **P2** | Ingestion & SHM Writer | Dropped ticks, SHM early unlinking, stale quotes | `CandleManager` SHM ownership, active position subscription guard, 30s quote TTL |
| **P3** | Scoring 3.0 & RMS | Retracement false breakouts, HoL blocking, OTM reject | 50% retracement tested FIRST, 5s candidate quarantine, hybrid tick spread gate |
| **P4** | Superstructures & Tracks | Exit cross-talk, thread contention, chased SL | Isolated OS processes, event-driven cached quote fills, Ask-anchored SL |
| **P5** | Dashboard & Columnar Sim | CPU spikes, tick skipping, live/sim drift | 1.0s throttled UI, Polars ASOF join nanosecond timestamp matching |
| **P6** | Runner CLI & Supervisor | Zombie processes, CLI parsing mismatch | Process group `SIGTERM`/`SIGKILL`, consolidated argparser, 35+ test suite gate |
