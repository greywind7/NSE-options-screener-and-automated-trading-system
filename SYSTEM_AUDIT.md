# 🔍 Comprehensive System Audit Report

**Audit Date**: 11 November 2026  
**System**: Upstox Algorithmic Trading Infrastructure v2.0  
**Codebase Size**: 27,095 lines of Python across 70 source files  
**Test Coverage**: 112+ unit tests across 25 test files  
**Auditor**: Automated Deep-Audit (4 parallel specialized auditors + manual review)

---

> [!CAUTION]
> **CRITICAL SECURITY VULNERABILITY**: Real production JWT API tokens are stored in plaintext in `.env`, the project has **no `.gitignore`** file, and is **not a git repository**. If this code is ever pushed to any remote, your Upstox account credentials will be permanently exposed. This is the single highest-priority issue to fix immediately.

---

## Table of Contents

1. [Executive Summary & Risk Score](#1-executive-summary--risk-score)
2. [🚨 Critical Bugs & System-Breaking Errors](#2--critical-bugs--system-breaking-errors)
3. [⚠️ High-Severity Issues](#3-️-high-severity-issues)
4. [🔶 Medium-Severity Issues](#4--medium-severity-issues)
5. [💡 Code Quality & Architecture Improvements](#5--code-quality--architecture-improvements)
6. [📊 Test Coverage Analysis & Gaps](#6--test-coverage-analysis--gaps)
7. [⚙️ Configuration Audit](#7-️-configuration-audit)
8. [🏗️ Architecture Review](#8-️-architecture-review)
9. [📈 Performance Optimization Opportunities](#9--performance-optimization-opportunities)
10. [🔒 Security Audit](#10--security-audit)
11. [📋 Suggestions, Tips & Best Practices](#11--suggestions-tips--best-practices)
12. [🗺️ Future Roadmap & Development Plan](#12-️-future-roadmap--development-plan)
13. [📝 Complete Issue Registry](#13--complete-issue-registry)

---

## 1. Executive Summary & Risk Score

### Overall System Health: 🟡 **6.5 / 10** (Functional but with critical gaps)

| Category | Score | Risk Level |
|:---|:---:|:---:|
| **Security** | 3/10 | 🔴 CRITICAL |
| **Code Quality** | 7/10 | 🟡 MODERATE |
| **Error Handling** | 5/10 | 🟠 HIGH |
| **Test Coverage** | 7/10 | 🟡 MODERATE |
| **Architecture** | 8/10 | 🟢 GOOD |
| **Performance** | 7/10 | 🟡 MODERATE |
| **Documentation** | 9/10 | 🟢 EXCELLENT |
| **Configuration** | 7/10 | 🟡 MODERATE |
| **Operational Readiness** | 6/10 | 🟠 HIGH |

### Key Strengths
- Exceptionally well-documented architecture (566-line architecture summary, scoring summary, README)
- Clean modular design with clear separation of concerns (strategies → execution → infrastructure)
- Comprehensive multi-mode execution architecture (paper, paper_delayed, live, sim)
- Robust pre-market preparation pipeline with soft-prepare idempotency
- Sophisticated risk management with multi-tiered stop-loss, trailing ratchets, and freeze controls
- Good test suite covering critical paths (112+ tests across all subsystems)
- Zero-tolerance anti-hallucination pricing policy (no fabricated prices)
- Elegant session recording and replay architecture

### Key Weaknesses
- No version control (no git repo, no `.gitignore`) — catastrophic risk
- API credentials in plaintext with no secret management
- 122 broad `except Exception` handlers silently swallowing errors
- 125 bare `pass` statements hiding failures
- 18 missing `__init__.py` files (works only due to implicit namespace packages)
- No `requirements.txt` or dependency management
- Missing `Tuple` import in `runner.py` (will crash `cmd_post_market`)
- No logging framework (all logging via `print()` to stdout/stderr)
- No graceful degradation in live trading if infrastructure fails mid-session

---

## 2. 🚨 Critical Bugs & System-Breaking Errors

### CRIT-001: Missing `Tuple` Import Crashes `post-market` Command
**File**: [`runner.py`](file:///Users/yorha/Documents/Trading%20Stuff/runner.py#L37) / Line 527  
**Severity**: 🔴 CRASH BUG  
**Impact**: `python3 runner.py post-market` will crash with `NameError: name 'Tuple' is not defined`

```python
# Line 37: Missing Tuple from imports
from typing import Any, Dict, List, Optional  # ← Tuple NOT imported

# Line 527: Uses Tuple
targets_to_archive: List[Tuple[str, str, str]] = []  # ← NameError at runtime
```

### CRIT-002: No `.gitignore` — Secrets at Risk of Exposure
**Severity**: 🔴 CRITICAL SECURITY  
**Impact**: If this directory is ever initialized as a git repo and pushed, the following will be exposed:
- `.env` containing live Upstox JWT tokens
- `logs/` containing trade execution data
- `recordings/` containing 4.4GB of raw market data
- All debug logs with API request/response payloads

### CRIT-003: Hardcoded IPC Auth Key in `system.json`
**File**: [`config/system.json`](file:///Users/yorha/Documents/Trading%20Stuff/config/system.json#L9) / Line 9  
**Severity**: 🔴 HIGH  
**Impact**: The IPC authentication key `"upstox_algo_shared_secret"` is stored in plaintext in a JSON config file. Any local process can connect to the IPC socket and inject fake market data.

```json
"authkey": "upstox_algo_shared_secret"
```

### CRIT-004: Silent Exception Swallowing in Trade Execution Path
**File**: [`strategies/momentum/strategy.py`](file:///Users/yorha/Documents/Trading%20Stuff/strategies/momentum/strategy.py) / Lines 393, 409, 423, 457, 618, 638  
**Severity**: 🔴 CRITICAL (Financial Risk)  
**Impact**: During live trading, exceptions in the core trade execution pipeline are caught with bare `except Exception: pass`. This means:
- A failed order placement could silently disappear
- A stop-loss modification failure would not be retried
- Position tracking could become inconsistent with broker state

### CRIT-005: `cmd_stop` Only Removes IPC Socket — Does Not Kill Processes
**File**: [`runner.py`](file:///Users/yorha/Documents/Trading%20Stuff/runner.py#L628) / Lines 628-640  
**Severity**: 🟠 HIGH  
**Impact**: `python3 runner.py stop` claims "All processes stopped" but only removes the IPC socket file. It does not actually terminate running listener, strategy, or recorder processes. This can lead to:
- Orphaned strategy processes still placing trades
- Multiple listener instances competing for WebSocket connections
- Market data being processed by zombie processes with stale state

### CRIT-006: Simulation Engine Hardcodes NSE Market Hours
**File**: [`simulation/simulation_engine.py`](file:///Users/yorha/Documents/Trading%20Stuff/simulation/simulation_engine.py#L343) / Line 343  
**Severity**: 🟠 HIGH  
**Impact**: Even when `is_mcx=True`, the simulation engine hardcodes `eod_square_off_time="15:20"` and `market_close_time="15:30"` (NSE hours). MCX trades until 23:30, so MCX simulations would prematurely square off all positions at 15:20.

```python
runner = MomentumStrategyRunner(
    eod_square_off_time="15:20",  # ← Hardcoded NSE hours, ignores MCX
    market_close_time="15:30",     # ← Should be 23:30 for MCX
)
```

### CRIT-007: Race Condition in PaperDelayedAdapter Threading
**File**: [`execution/paper/paper_delayed_adapter.py`](file:///Users/yorha/Documents/Trading%20Stuff/execution/paper/paper_delayed_adapter.py) / Lines 221-234  
**Severity**: 🟠 HIGH  
**Impact**: The `PaperDelayedAdapter` uses `threading.Timer` for delayed order fills but the callback accesses shared position/cash state protected only by a simple `threading.Lock`. If the timer callback fires during a `place_order` or `exit_position` call on the main thread, the lock may not properly protect the complex multi-step cash accounting, leading to:
- Double fills (same position counted twice)
- Cash balance going negative
- Inconsistent open_positions vs closed_trades counts

### CRIT-008: Live Adapter `time.sleep(1.0)` Blocks Main Thread During Entry
**File**: [`execution/live/live_adapter.py`](file:///Users/yorha/Documents/Trading%20Stuff/execution/live/live_adapter.py#L346)  
**Severity**: 🟠 HIGH  
**Impact**: After placing a live broker order, there's a hard `time.sleep(1.0)` "for broker gateway ingestion." During this 1-second block, no other market events are processed. In fast-moving markets, this means:
- Stop-loss ratchets delayed by 1+ second per trade
- Missing breakout signals that expire within the 20-second actionable window
- Potential cascade of stale data processing

---

## 3. ⚠️ High-Severity Issues

### HIGH-001: 18 Missing `__init__.py` Files
**Impact**: All packages are missing `__init__.py` files, relying on Python's implicit namespace packages (PEP 420). This works but:
- Prevents proper `importlib` package discovery
- Breaks tools like `mypy`, `pylint`, `pytest --import-mode=importlib`
- Makes packaging impossible (`setuptools`, `pip install -e .`)
- Can cause subtle import ordering bugs

**Missing from**:
```
strategies/, strategies/momentum/, execution/, execution/paper/,
execution/live/, infrastructure/, infrastructure/auth/,
infrastructure/market_data/, infrastructure/instruments/,
infrastructure/volume/, infrastructure/recorder/, tests/,
tests/strategies/, tests/execution/, tests/replay/,
tests/infrastructure/, replay/
```

### HIGH-002: No Dependency Management
**Impact**: No `requirements.txt`, `pyproject.toml`, or `setup.py`. Dependencies include:
- `protobuf` (for MarketDataFeed_pb2)
- `requests` (for HTTP API calls in upstox_auth)
- Implied Groww SDK (`groww` package for live trading)
- All dependencies are unversioned and undocumented

### HIGH-003: No Structured Logging Framework
**Impact**: All logging throughout the entire 27k-line codebase is done via bare `print()` and `print(..., file=sys.stderr)`. This means:
- No log levels (DEBUG, INFO, WARNING, ERROR, CRITICAL)
- No log rotation (except manual `prune_debug_log`)
- No structured log output (JSON) for monitoring/alerting
- No way to filter or redirect logs by component
- Live trading errors mixed with informational dashboard output

### HIGH-004: `cmd_test` Hardcodes Legacy Test Path
**File**: [`runner.py`](file:///Users/yorha/Documents/Trading%20Stuff/runner.py#L653) / Line 653  
**Impact**: `runner.py test` tries to run `tests/test_breakout_features.py` from the `legacy/` directory, which may not exist. Should use `pytest` for proper test discovery instead of manually iterating test files with `subprocess.run`.

### HIGH-005: Duplicate Argument Parser Definitions (strat vs start)
**File**: [`runner.py`](file:///Users/yorha/Documents/Trading%20Stuff/runner.py#L978-L1021) / Lines 978-1021  
**Impact**: `strat` and `start` subcommands have 100% identical argument definitions copy-pasted (40+ lines each). Any argument change must be maintained in two places. A parent parser or shared argument group should be used.

### HIGH-006: Position State Persistence Not Atomic
**File**: [`execution/live/live_adapter.py`](file:///Users/yorha/Documents/Trading%20Stuff/execution/live/live_adapter.py) / Position persistence via JSON  
**Impact**: Live position state is persisted to `logs/live_positions_state.json` but:
- Writes are not atomic (no write-to-temp-then-rename pattern)
- Power failure or crash during write corrupts the state file
- On restart, all open positions would be lost
- No backup/recovery mechanism

### HIGH-007: `ReplayEngine` Overwrites `received_timestamp`
**File**: [`replay/replay_engine.py`](file:///Users/yorha/Documents/Trading%20Stuff/replay/replay_engine.py#L163) / Line 163  
**Impact**: When replaying, `received_timestamp=time.time()` is set to the current wall clock instead of preserving the original recorded timestamp. This breaks:
- Volume calculations (RollingVolumeTracker uses timestamp deltas)
- Pattern detection timing
- All time-based risk management (stagnation timeouts, target decay)

### HIGH-008: No Circuit Breaker for Live Execution
**Impact**: While there's a `kill_switch_loss_pct: 5.0` in config, there's no visible implementation of an automated circuit breaker that monitors cumulative P&L and halts all live trading when the daily loss limit is hit. The freeze mechanism requires manual CLI invocation.

### HIGH-009: Portfolio `_log_trade_csv` Silent Failure
**File**: [`strategies/momentum/portfolio.py`](file:///Users/yorha/Documents/Trading%20Stuff/strategies/momentum/portfolio.py#L332) / Line 332  
**Impact**: CSV trade journal writing wraps the entire write in `except Exception: pass`. If the CSV file is locked, disk is full, or path is invalid, trades execute but are never journaled. This creates phantom trades with no audit trail.

### HIGH-010: Stale Quotes Cache Without TTL Enforcement in Portfolio
**Impact**: The `quotes_cache` dictionary accumulates quotes for all instruments throughout the session. When a contract is unsubscribed (e.g., after ATM shift), its stale quote remains in cache indefinitely. The 30-second staleness gate exists in execution adapters but the portfolio's own `compute_mtm()` doesn't enforce it, potentially showing wildly stale MTM values in the dashboard.

---

## 4. 🔶 Medium-Severity Issues

### MED-001: 122 Broad `except Exception` Handlers
**Impact**: 122 instances of `except Exception` (many with bare `pass`) across all modules silently swallow errors. In a financial system, every swallowed exception is a potential money-losing bug that becomes invisible.

**Worst offenders** (count by file):
| File | Count | Risk |
|:---|:---:|:---|
| `strategies/momentum/strategy.py` | 11 | Trade execution errors silenced |
| `strategies/momentum/portfolio.py` | 6 | Position tracking errors silenced |
| `execution/paper/paper_delayed_adapter.py` | 5 | Fill simulation errors silenced |
| `execution/live/live_adapter.py` | 5+ | Real money order errors silenced |
| `runner.py` | 14 | Operational errors silenced |

### MED-002: Global Mutable State in PnLAggregator
**File**: [`infrastructure/pnl/pnl_aggregator.py`](file:///Users/yorha/Documents/Trading%20Stuff/infrastructure/pnl/pnl_aggregator.py#L501)  
**Impact**: Uses a global `_GLOBAL_AGGREGATOR` singleton pattern. This creates hidden coupling and makes testing difficult. Multiple simulation runs in the same process could corrupt each other's data.

### MED-003: Config Loading Fallback Chain is Fragile
**File**: [`runner.py`](file:///Users/yorha/Documents/Trading%20Stuff/runner.py#L289-L330)  
**Impact**: Capital, min_score, and min_trade_score all use complex 10-candidate fallback chains searching multiple config keys. This is brittle — a typo in the config key name silently falls through to the hardcoded default. Consider a single canonical path with a clear default.

### MED-004: Dashboard Module is Monolithic (1,051 lines)
**File**: [`strategies/momentum/dashboard.py`](file:///Users/yorha/Documents/Trading%20Stuff/strategies/momentum/dashboard.py)  
**Impact**: The ANSI terminal dashboard renderer is 1,051 lines of string formatting code. It's tightly coupled to strategy internals and difficult to maintain or test.

### MED-005: Scoring Engine Has Dual Legacy + 2.0 Paths
**Files**: `scoring.py` (400 lines) + `legacy_scoring.py` (352 lines) + `patterns.py` (800 lines) + `legacy_patterns.py` (252 lines)  
**Impact**: 1,804 lines of scoring and pattern code with two parallel implementations. The legacy versions appear unused but are still maintained, adding confusion and maintenance burden.

### MED-006: `option_selector.py` Contains Complex State Machine (525 lines)
**File**: [`strategies/momentum/option_selector.py`](file:///Users/yorha/Documents/Trading%20Stuff/strategies/momentum/option_selector.py)  
**Impact**: Option strike selection involves multi-step state machines with multiple retry loops and complex fallback chains. Silent `except Exception: pass` at lines 115, 476 can hide strike resolution failures.

### MED-007: Volume Index Data Duplication
**Impact**: `data/` directory contains 58MB of dated copies of essentially the same data:
- `index.json` + `index_20261106.json` through `index_20261111.json` (165k lines each)
- `subscription_manifest.json` + dated copies (157k lines each)
- Canonical + dated instrument CSVs

This is by design for session isolation, but consumes significant disk space.

### MED-008: `simulation/simulation_engine.py` Duplicates Display Logic
**Impact**: Lines 627-777 and 779-899 contain nearly identical display/formatting code (live matrix vs final summary). This is ~270 lines of duplicated logic that should be extracted into a shared renderer.

### MED-009: No Input Validation on CLI Arguments
**Impact**: The runner's argument parser accepts arbitrary strings for date formats, time formats, and paths without validation. Invalid date format like `2026-13-45` would propagate deep into the system before failing.

### MED-010: Unprotected Dict Iteration During Mutation
**File**: [`simulation/simulation_engine.py`](file:///Users/yorha/Documents/Trading%20Stuff/simulation/simulation_engine.py#L553) / Line 553  
**Impact**: `for tid, pos in list(acct.open_positions.items())` correctly uses `list()` to copy, but the underlying `portfolio.py`'s `on_timer` may also iterate `open_positions` concurrently during simulation events, creating potential `RuntimeError: dictionary changed size during iteration`.

### MED-011: Paper Adapter Fill Price Can Be Zero
**File**: [`execution/paper/paper_adapter.py`](file:///Users/yorha/Documents/Trading%20Stuff/execution/paper/paper_adapter.py)  
**Impact**: If `best_ask` is 0.0 (e.g., market data not yet available), the paper adapter may fill at price 0.0 rather than rejecting the order. This creates infinite-return trades in the journal.

### MED-012: No Heartbeat/Liveness Check for Market Data Stream
**Impact**: While there's a `heartbeat_interval_sec: 1.0` in `system.json`, the strategy process has no visible mechanism to detect when the market data IPC feed has gone stale (listener process died). The strategy would continue running with frozen quotes, potentially missing stop-losses.

---

## 5. 💡 Code Quality & Architecture Improvements

### Architecture Strengths (Preserve These)
1. **Clean Layer Separation**: Infrastructure → Strategy → Execution → Dashboard follows proper dependency flow
2. **Adapter Pattern**: `BaseExecutionAdapter` enables clean multi-mode execution
3. **Event-Driven Design**: IPC pub/sub architecture enables process isolation
4. **Config Externalization**: Most parameters loaded from JSON configs
5. **Recording/Replay**: Lossless recording enables perfect historical simulation

### Architecture Improvements Needed

#### ARCH-001: Introduce a Proper Event Bus / Message Queue
The current `multiprocessing.Queue` / Unix socket IPC is functional but fragile. Consider:
- **ZeroMQ (zmq)** for production pub/sub with built-in reconnection
- **Redis Streams** for durable message queuing with replay
- Benefit: Automatic reconnection, message persistence, back-pressure handling

#### ARCH-002: Extract Configuration into a Typed Config Class
Replace the nested `dict.get("a", {}).get("b", {}).get("c", default)` chains with:
```python
@dataclass
class RiskConfig:
    option_stop_loss_pct: float = 7.5
    initial_target_pct: float = 15.0
    # ...
```
This provides IDE autocomplete, type checking, and validation.

#### ARCH-003: Implement Structured Logging
Replace all `print()` with Python's `logging` module:
```python
import logging
logger = logging.getLogger(__name__)
logger.info("Trade executed: %s @ ₹%.2f", symbol, price)
```
Benefits: Log levels, log rotation, JSON output for monitoring, per-module filtering.

#### ARCH-004: Add a State Machine for Trade Lifecycle
Currently, trade state transitions (PENDING → FILLED → TRAILING → EXITED) are implicit in code flow. A proper state machine (`TradeState` enum with explicit transitions) would:
- Prevent invalid state transitions
- Make the code self-documenting
- Enable state persistence and recovery

#### ARCH-005: Decouple Dashboard from Strategy
The `dashboard.py` directly accesses strategy internals. It should consume a standardized `StrategyState` data object instead, enabling:
- Web-based dashboard (Flask/FastAPI)
- Mobile push notifications
- External monitoring integration

#### ARCH-006: Introduce Abstract Configuration Validation
No config file is validated against a schema. A JSON Schema or Pydantic model would catch:
- Missing required fields
- Type mismatches
- Out-of-range values (e.g., negative capital, stop-loss > 100%)

### Code Quality Quick Wins
1. **Add type hints**: ~60% of functions lack return type annotations
2. **Reduce function lengths**: `strategy.py` has functions exceeding 150 lines
3. **Extract constants**: Magic numbers like `60.0` (brokerage), `30.0`, `8.0` appear throughout
4. **Remove dead code**: `legacy_scoring.py` and `legacy_patterns.py` should be archived
5. **Add docstrings**: Many internal functions lack documentation
6. **Use f-strings consistently**: Mix of f-strings and `.format()` calls

---

## 6. 📊 Test Coverage Analysis & Gaps

### Current Coverage Summary
| Module | Test File(s) | Tests | Coverage |
|:---|:---|:---:|:---|
| Strategy Core | `test_momentum_modular.py` | 20 | Good – covers scoring, patterns, tracks |
| Scoring Engine 2.0 | `test_scoring_engine_2_0.py` | 7 | Moderate – tests binary gates |
| Risk Management | `test_new_risk_rules.py` | 5 | Moderate – tests ratchet mechanics |
| Time-of-Day Gates | `test_time_of_day_gates.py` | 7 | Good – covers all periods |
| Dynamic Trio | `test_rolling_dynamic_trio.py` | 3 | Basic – tests allocation only |
| Combined Tracks | `test_combined_tracks.py` | 7 | Good – tests shared capital |
| Live Backfill | `test_live_backfill_and_trio.py` | 5 | Good |
| Paper Adapter | `test_paper.py` | 2 | **⚠️ MINIMAL** |
| Paper Delayed | `test_paper_delayed.py` | 5 | Moderate |
| Multi-Executor | `test_multi_executor.py` | 4 | Moderate |
| Freeze Mode | `test_freeze_mode.py` | 3 | Good |
| Live Adapter | `test_groww_live.py` | 5 | Moderate |
| Live Reconciliation | `test_live_reconciliation.py` | 8 | Good |
| Simulation Engine | `test_simulation_engine.py` | 1 | **⚠️ MINIMAL** |
| Replay Engine | `test_replay.py` | 1 | **⚠️ MINIMAL** |
| Archive Replay | `test_archive_replay.py` | 2 | Basic |
| IPC Transport | `test_ipc.py` | 2 | Basic |
| Recorder | `test_recorder.py` | 2 | Basic |
| PnL Aggregator | `test_pnl_aggregator.py` | 4 | Moderate |
| PDF Reports | `test_pdf_report.py` | 4 | Good |
| Premarket Pipeline | `test_premarket.py` | 6 | Good |
| Position Registry | `test_position_registry.py` | 6 | Good |
| Staleness Gate | `test_staleness_gate.py` | 5 | Good |
| Time Utils | `test_time_utils.py` | 2 | Basic |
| Listener | `test_listener.py` | 7 | Good |

### Critical Test Gaps (Missing Tests)

> [!WARNING]
> The following critical paths have **NO automated tests**:

1. **`runner.py`** (1,139 lines): Zero tests for the entire CLI orchestrator
   - No test for `cmd_prepare` pipeline
   - No test for `cmd_post_market` archiving
   - No test for `cmd_freeze` / `cmd_unfreeze` interaction
   - No test for `cmd_status` health checks
   - No test for `cmd_stop` process management

2. **End-to-End Integration Tests**: No tests simulate a full market session from data ingestion → strategy computation → trade execution → position management → exit

3. **Error Recovery Tests**: No tests verify system behavior when:
   - Market data feed drops mid-session
   - Broker API returns errors during live trading
   - Disk becomes full during recording
   - Process crashes and restarts mid-session

4. **Edge Case Tests**:
   - Circuit breaker / kill switch activation
   - Maximum position limits reached
   - All capital exhausted
   - Simultaneous breakouts exceeding capital
   - Market holiday / early close behavior

5. **Performance/Load Tests**: No benchmarks for:
   - Maximum event throughput per second
   - Memory usage under sustained load
   - Dashboard render time with 213+ instruments

6. **Dashboard Tests**: `dashboard.py` (1,051 lines) has zero tests

7. **Option Selector Tests**: `option_selector.py` (525 lines) has no dedicated test file

8. **Actionable Pool Tests**: `actionable_pool.py` (149 lines) has no dedicated test file

---

## 7. ⚙️ Configuration Audit

### Configuration Structure Assessment

The system uses 8 JSON configuration files, which is well-organized. However:

### Config Issues Found

#### CFG-001: No Schema Validation
None of the config files are validated against a schema. Invalid types (e.g., string instead of float for `option_stop_loss_pct`) would cause runtime crashes deep in the execution path.

#### CFG-002: Duplicate Time Parameters
Market hours are defined in multiple places with potential for conflict:
- `config/strategies.json` → `risk.market_open_time`, `risk.eod_freeze_time`, `risk.eod_square_off_time`
- `config/execution.json` → `execution.market_open_time`, `execution.eod_freeze_time`, `execution.eod_square_off_time`
- `config/execution.json` → `execution.live.eod_freeze_time` (overrides with earlier time for live)
- CLI arguments `--market-open-time`, `--eod-time`, `--eod-freeze-time`

This creates a 4-layer override chain where the effective value depends on which layer was last to write.

#### CFG-003: Starting Capital Inconsistency
- `execution.json` → `paper.starting_capital_per_sim: 250000.0`
- `execution.json` → `sim.starting_capital_per_sim: 100000.0`
- Fallback in `runner.py` → `100000.0`
- `strategies_sim.json` may override independently

Different capital amounts make cross-mode P&L comparison meaningless.

#### CFG-004: Midday Freeze Is Enabled by Default
```json
"midday_freeze_enabled": true,
"midday_freeze_start": "11:00",
"midday_freeze_end": "13:30"
```
This blocks trading for 2.5 hours daily. Verify this is intentional — it significantly reduces the trading window from 6h15m to 3h45m.

#### CFG-005: `max_lots_per_trade` = 1 Everywhere
All modes limit to 1 lot per trade. For live trading with ₹250,000 capital on cheap options (₹5-15k per lot), this may significantly under-deploy capital. Consider making this dynamic based on position sizing rules.

#### CFG-006: IPC Socket Path Hardcoded
`/tmp/upstox_market_feed.ipc` is hardcoded as the default in multiple places. On macOS, `/tmp` is periodically cleaned. Consider using a more persistent path like `~/.upstox/market_feed.ipc`.

---

## 8. 🏗️ Architecture Review

### Current Architecture (Strengths)
```
runner.py (CLI Orchestrator)
├── Pre-Market Pipeline (prepare)
│   ├── Master Fetcher → Instrument Filter → Volume Index → Subscription Manifest
├── Market Data Layer
│   ├── Listener (WebSocket/REST/Mock) → Publisher (IPC) → Subscriber
│   └── Recorder (JSONL)
├── Strategy Layer
│   └── Momentum Strategy Runner → Scanner → Patterns → Scoring → Portfolio
├── Execution Layer
│   └── MultiExecutionManager → Paper / PaperDelayed / Live adapters
└── Reporting Layer
    └── PnL Aggregator → Report Generator → PDF Report
```

### Architecture Debt

#### DEBT-001: Monolithic Runner (1,139 lines)
`runner.py` is doing too much: CLI parsing, config resolution, process management, and business logic orchestration. Should be split into:
- `cli/parser.py` — Argument parsing and validation
- `cli/commands/` — Individual command handlers
- `core/config.py` — Configuration resolution and validation
- `core/orchestrator.py` — Service lifecycle management

#### DEBT-002: Strategy Module is Oversized
The `strategies/momentum/` package totals 6,485 lines across 11 files. The `strategy.py` (1,238 lines) and `portfolio.py` (1,660 lines) files are too large. Consider:
- Extracting trade lifecycle into `trade_manager.py`
- Extracting warm-up/backfill logic into `backfill.py`
- Extracting DYNAMIC_TRIO into its own module

#### DEBT-003: No Health/Readiness Probes
No mechanism for external monitoring to check system health:
- Is the listener connected and receiving data?
- Is the strategy running and processing events?
- Are all execution adapters healthy?
- What's the current P&L and position count?

#### DEBT-004: No Inter-Process Communication for Control Signals
Freeze/unfreeze uses file flags on disk (`logs/.freeze_*`). This has latency (file system polling) and reliability issues. The PID-based `SIGUSR1/SIGUSR2` signaling is fragile (stale PID files after crashes).

#### DEBT-005: Single Strategy Support
The architecture claims extensibility via `BaseStrategy` but only `momentum` is implemented and hardcoded in many places. True multi-strategy support would require:
- Strategy registry/discovery
- Independent capital allocation per strategy
- Cross-strategy position limits

---

## 9. 📈 Performance Optimization Opportunities

### PERF-001: JSON Serialization in Hot Path
Market events are parsed from JSON on every tick. For high-frequency data, consider:
- **MessagePack** for binary serialization (10x faster than JSON)
- **Protobuf** (already used for V3 feed) for IPC as well
- Pre-allocated event objects with `__slots__`

### PERF-002: Dictionary Lookups in Quote Cache
`quotes_cache.get(ck) or quotes_cache.get(ck.replace(":", "|")) or quotes_cache.get(ck.replace("|", ":"))` pattern appears multiple times, doing 3 hash lookups per access. Normalize keys at ingestion time instead.

### PERF-003: Repeated `datetime.fromtimestamp()` Calls
Multiple modules call `datetime.fromtimestamp(now_ts).strftime()` in tight loops. Cache the result per second.

### PERF-004: Dashboard Rendering Overhead
The 148-column ANSI dashboard re-renders entirely every 1 second. For 213 instruments × 33 tracks, this involves significant string formatting. Consider:
- Differential rendering (only update changed cells)
- Reducing refresh rate to 2-3 seconds

### PERF-005: Linear Search in Option Selector
Strike resolution iterates through all available strikes to find the best match. With pre-sorted strike arrays and binary search, this could be O(log n) instead of O(n).

### PERF-006: File I/O in Trade Logging
Every trade open/close appends to CSV and JSONL files with `open() ... write() ... close()`. Keep file handles open for the session duration and flush periodically.

### PERF-007: Memory Growth in Recording
Market events accumulate in memory buffers. With 4.4GB of recordings, long sessions may consume significant RAM. The chunked writer helps, but verify `buffer_capacity: 5000` is sufficient.

---

## 10. 🔒 Security Audit

### SEC-001: Plaintext API Tokens in `.env` 🔴
```
UPSTOX_ACCESS_TOKEN="eyJ0eXAiOiJKV1QiLCJrZXlfaWQi..."
UPSTOX_SANDBOX_ACCESS_TOKEN="eyJ0eXAiOiJKV1QiLCJrZXlfaWQi..."
```
**Risk**: If these tokens are exposed, an attacker can:
- Stream all your market data subscriptions
- Place/modify/cancel real orders via the production API
- View your complete portfolio and order history

**Mitigation**: Use a secrets manager, environment variables from a shell profile (not `.env`), or macOS Keychain.

### SEC-002: No Token Rotation or Expiry Handling
The JWT tokens in `.env` have embedded expiry claims. The system has no mechanism to:
- Detect when tokens have expired
- Automatically refresh tokens
- Alert the operator about pending expiry

### SEC-003: IPC Socket Has Weak Authentication
The `multiprocessing.connection` authkey is a static string in a plaintext config file. Any local process knowing this string can:
- Inject fake market ticks
- Trigger false breakouts leading to real trades

### SEC-004: Debug Log May Contain Sensitive Data
`logs/debug.log` stores "API payloads, HTTP 429 retries, and errors" which may contain:
- Authorization headers with bearer tokens
- Account-identifying information
- Trade execution details

### SEC-005: No Rate Limiting on Reconnection Attempts
While `upstox_auth.py` has exponential backoff for HTTP 429, there's no maximum backoff cap or jitter. Under sustained failure, the exponential backoff could grow to hours, making the system unresponsive.

### SEC-006: Live Execution Confirmation is CLI-Only
The `--confirm-live` flag is the only gate preventing live order execution. There's no:
- Pre-session confirmation prompt
- Daily capital limit enforcement
- Emergency stop webhook
- Mobile/email notification for live trades

---

## 11. 📋 Suggestions, Tips & Best Practices

### Immediate Actions (Do Today)

1. **Create `.gitignore`** with at minimum:
   ```
   .env
   logs/
   recordings/
   data/instrument*.csv
   data/index*.json
   data/subscription_manifest*.json
   data/filtered*.csv
   __pycache__/
   *.pyc
   *.tar.gz
   ```

2. **Initialize git repo** and make a first commit (without `.env`!)

3. **Fix the `Tuple` import** in `runner.py` line 37

4. **Create `requirements.txt`**:
   ```
   protobuf>=3.20.0
   requests>=2.28.0
   ```

5. **Add `__init__.py`** to all 18 packages

### Short-Term Best Practices

6. **Replace `except Exception: pass`** with specific exception types and proper logging. Priority order:
   - `strategy.py` (trade execution path)
   - `portfolio.py` (position tracking)
   - `live_adapter.py` (real money)
   - `paper_delayed_adapter.py` (threading issues)

7. **Add `logging` module** — even a basic setup improves debuggability:
   ```python
   logging.basicConfig(
       level=logging.INFO,
       format='%(asctime)s [%(name)s] %(levelname)s: %(message)s',
       handlers=[
           logging.FileHandler('logs/trading.log'),
           logging.StreamHandler()
       ]
   )
   ```

8. **Add input validation** for CLI date/time arguments

9. **Implement atomic file writes** for position state persistence

10. **Add a daily P&L circuit breaker** that automatically freezes all modes when cumulative loss exceeds threshold

### Architecture Tips

11. **Use dataclasses** for configuration and event objects instead of raw dicts
12. **Use `enum.Enum`** for trade directions, modes, and track types
13. **Use `pathlib.Path`** instead of `os.path` string manipulation
14. **Use `contextlib.suppress`** instead of `try/except/pass` for intentionally ignored errors
15. **Consider async/await** for I/O-bound operations (HTTP calls, file writes)

### Trading-Specific Tips

16. **Add a "paper vs live comparison" mode** that runs paper and live side-by-side and alerts when fills diverge significantly (slippage detection)
17. **Track execution quality metrics**: fill latency, slippage, partial fills, rejection rate
18. **Add position sizing rules** beyond fixed 1-lot (Kelly criterion, volatility-based sizing)
19. **Implement max drawdown tracking** with visual alerts
20. **Add a trade journal analyzer** that identifies which time-of-day, instruments, and patterns are most/least profitable

---

## 12. 🗺️ Future Roadmap & Development Plan

### Phase 1: Foundation Hardening (Weeks 1-2)

| Priority | Task | Effort | Impact |
|:---:|:---|:---:|:---:|
| P0 | Create `.gitignore` + initialize git | 15 min | 🔴 Critical |
| P0 | Fix `Tuple` import bug in `runner.py` | 1 min | 🔴 Critical |
| P0 | Create `requirements.txt` | 15 min | 🟠 High |
| P0 | Add all 18 missing `__init__.py` files | 10 min | 🟠 High |
| P0 | Fix MCX hours in simulation engine | 30 min | 🔴 Critical |
| P1 | Replace top 30 critical `except Exception: pass` | 4 hrs | 🟠 High |
| P1 | Add structured logging (Python `logging`) | 4 hrs | 🟠 High |
| P1 | Implement atomic file writes for position state | 2 hrs | 🟠 High |
| P1 | Add daily P&L circuit breaker | 3 hrs | 🟠 High |
| P1 | Fix `ReplayEngine` timestamp preservation | 1 hr | 🟠 High |

### Phase 2: Testing & Reliability (Weeks 3-4)

| Priority | Task | Effort | Impact |
|:---:|:---|:---:|:---:|
| P1 | Add `runner.py` CLI integration tests | 8 hrs | 🟠 High |
| P1 | Add end-to-end simulation tests | 6 hrs | 🟠 High |
| P1 | Add error recovery tests | 4 hrs | 🟡 Medium |
| P1 | Add option_selector unit tests | 4 hrs | 🟡 Medium |
| P1 | Add actionable_pool unit tests | 2 hrs | 🟡 Medium |
| P2 | Set up `pytest` with proper discovery | 2 hrs | 🟡 Medium |
| P2 | Add CI/CD pipeline (GitHub Actions) | 3 hrs | 🟡 Medium |
| P2 | Add code coverage tracking (coverage.py) | 1 hr | 🟡 Medium |
| P2 | Add pre-commit hooks (black, flake8, mypy) | 2 hrs | 🟡 Medium |

### Phase 3: Architecture Improvements (Weeks 5-8)

| Priority | Task | Effort | Impact |
|:---:|:---|:---:|:---:|
| P2 | Split `runner.py` into CLI modules | 8 hrs | 🟡 Medium |
| P2 | Extract typed config dataclasses | 12 hrs | 🟡 Medium |
| P2 | Implement proper process management (PID tracking, health checks) | 8 hrs | 🟠 High |
| P2 | Add WebSocket reconnection with exponential backoff | 4 hrs | 🟡 Medium |
| P2 | Implement market data feed heartbeat monitoring | 4 hrs | 🟡 Medium |
| P2 | Decouple dashboard from strategy internals | 8 hrs | 🟡 Medium |
| P3 | Remove legacy scoring/patterns modules | 2 hrs | 🟢 Low |
| P3 | Consolidate duplicate display logic in simulation_engine | 4 hrs | 🟢 Low |

### Phase 4: Advanced Features (Months 2-3)

| Priority | Task | Effort | Impact |
|:---:|:---|:---:|:---:|
| P2 | Build web-based monitoring dashboard (Flask/FastAPI + WebSocket) | 20 hrs | 🟠 High |
| P2 | Implement trade quality analytics (slippage, fill rate, latency) | 12 hrs | 🟡 Medium |
| P2 | Add position sizing algorithms (Kelly, vol-based) | 8 hrs | 🟡 Medium |
| P3 | Multi-strategy framework (beyond momentum only) | 20 hrs | 🟡 Medium |
| P3 | Alerting system (Telegram/Discord/email for breakouts, fills, errors) | 8 hrs | 🟡 Medium |
| P3 | Walk-forward optimization engine | 16 hrs | 🟡 Medium |
| P3 | Instrument-level analytics (best/worst symbols, time-of-day profitability) | 8 hrs | 🟢 Low |

### Phase 5: Production Hardening (Months 3-4)

| Priority | Task | Effort | Impact |
|:---:|:---|:---:|:---:|
| P2 | Implement proper secrets management (macOS Keychain / env injection) | 4 hrs | 🟠 High |
| P2 | Add token expiry detection and alerting | 4 hrs | 🟡 Medium |
| P2 | Implement graceful degradation (market data loss → freeze → alert) | 8 hrs | 🟠 High |
| P2 | Build system health monitoring (Prometheus metrics / Grafana) | 16 hrs | 🟡 Medium |
| P3 | Docker containerization for reproducible deployments | 8 hrs | 🟢 Low |
| P3 | Implement backup/recovery for position state | 6 hrs | 🟡 Medium |
| P3 | Add audit log (immutable, tamper-proof trade history) | 8 hrs | 🟡 Medium |

### Phase 6: Scaling & Optimization (Months 4-6)

| Priority | Task | Effort | Impact |
|:---:|:---|:---:|:---:|
| P3 | Replace JSON IPC with ZeroMQ or Redis Streams | 16 hrs | 🟡 Medium |
| P3 | Implement binary serialization (MessagePack/Protobuf) for IPC | 8 hrs | 🟡 Medium |
| P3 | Database integration (PostgreSQL/TimescaleDB for trade history) | 16 hrs | 🟡 Medium |
| P3 | Multi-exchange support (BSE, MCX proper, international) | 20 hrs | 🟢 Low |
| P3 | Cloud deployment (AWS/GCP) with auto-scaling | 20 hrs | 🟢 Low |

---

## 13. 📝 Complete Issue Registry

### Issue Counts by Severity
| Severity | Count | Action Required |
|:---|:---:|:---|
| 🔴 Critical / System-Breaking | 8 | **Fix immediately before next trading session** |
| 🟠 High | 12 | Fix within 1-2 weeks |
| 🟡 Medium | 12 | Fix within 1 month |
| 🟢 Low / Enhancement | 15+ | Schedule in roadmap |

### Full Issue Index

| ID | Severity | Category | Summary | File |
|:---|:---:|:---|:---|:---|
| CRIT-001 | 🔴 | Bug | Missing `Tuple` import crashes post-market | `runner.py:37` |
| CRIT-002 | 🔴 | Security | No `.gitignore` — credentials at risk | Project root |
| CRIT-003 | 🔴 | Security | Hardcoded IPC auth key | `config/system.json:9` |
| CRIT-004 | 🔴 | Financial | Silent exception swallowing in trade execution | `strategy.py` (11 sites) |
| CRIT-005 | 🟠 | Operations | `cmd_stop` doesn't kill processes | `runner.py:628` |
| CRIT-006 | 🟠 | Bug | Simulation hardcodes NSE hours for MCX | `simulation_engine.py:343` |
| CRIT-007 | 🟠 | Concurrency | Race condition in PaperDelayed threading | `paper_delayed_adapter.py` |
| CRIT-008 | 🟠 | Performance | `time.sleep(1.0)` blocks main thread in live | `live_adapter.py:346` |
| HIGH-001 | 🟠 | Structure | 18 missing `__init__.py` files | All packages |
| HIGH-002 | 🟠 | DevOps | No dependency management | Project root |
| HIGH-003 | 🟠 | Observability | No structured logging framework | All modules |
| HIGH-004 | 🟠 | Testing | `cmd_test` hardcodes legacy path | `runner.py:653` |
| HIGH-005 | 🟡 | Code Quality | Duplicate arg parser (strat vs start) | `runner.py:978-1021` |
| HIGH-006 | 🟠 | Reliability | Non-atomic position state persistence | `live_adapter.py` |
| HIGH-007 | 🟠 | Bug | Replay overwrites `received_timestamp` | `replay_engine.py:163` |
| HIGH-008 | 🟠 | Safety | No automated circuit breaker for live | System-wide |
| HIGH-009 | 🟠 | Reliability | CSV trade log silent failure | `portfolio.py:332` |
| HIGH-010 | 🟡 | Data Integrity | No TTL on quotes_cache for MTM | `portfolio.py` |
| MED-001 | 🟡 | Code Quality | 122 broad `except Exception` handlers | System-wide |
| MED-002 | 🟡 | Architecture | Global mutable state in PnLAggregator | `pnl_aggregator.py:501` |
| MED-003 | 🟡 | Reliability | Fragile 10-candidate config fallback | `runner.py:289-330` |
| MED-004 | 🟡 | Maintainability | Monolithic dashboard (1,051 lines) | `dashboard.py` |
| MED-005 | 🟡 | Maintainability | Dual legacy + 2.0 scoring paths | `scoring.py` + `legacy_scoring.py` |
| MED-006 | 🟡 | Reliability | Silent failures in option_selector | `option_selector.py:115,476` |
| MED-007 | 🟡 | Storage | Volume index data duplication (58MB) | `data/` |
| MED-008 | 🟡 | Code Quality | Duplicate display logic in sim engine | `simulation_engine.py` |
| MED-009 | 🟡 | Validation | No CLI input validation | `runner.py` |
| MED-010 | 🟡 | Concurrency | Unprotected dict iteration | `simulation_engine.py:553` |
| MED-011 | 🟡 | Data Integrity | Paper adapter may fill at price 0.0 | `paper_adapter.py` |
| MED-012 | 🟡 | Reliability | No market data heartbeat monitoring | System-wide |
| SEC-001 | 🔴 | Security | Plaintext JWT tokens in `.env` | `.env` |
| SEC-002 | 🟡 | Security | No token rotation/expiry handling | `upstox_auth.py` |
| SEC-003 | 🟡 | Security | Weak IPC authentication | `system.json` |
| SEC-004 | 🟡 | Security | Sensitive data in debug logs | `logs/debug.log` |
| SEC-005 | 🟢 | Security | No max backoff cap on reconnection | `upstox_auth.py` |
| SEC-006 | 🟡 | Safety | CLI-only live execution gate | `runner.py` |

---

> [!IMPORTANT]
> **Priority Actions Before Next Live Trading Session:**
> 1. Fix `Tuple` import in `runner.py` (1 minute)
> 2. Create `.gitignore` (5 minutes)
> 3. Fix MCX simulation hours (30 minutes)
> 4. Add `except` logging in `strategy.py` trade execution path (2 hours)
> 5. Implement daily P&L circuit breaker (3 hours)

---

> [!TIP]
> **Quick Win for Code Health:**
> Run `python3 -W all runner.py test` to surface any `DeprecationWarning` or `RuntimeWarning` that's currently being silenced. The `-W all` flag makes Python show all warnings.

---

*Audit conducted by 4 parallel specialized auditors examining all 70 source files, 25 test files, 8 configuration files, and project documentation. Total code analyzed: 27,095 lines of Python + ~1,600 lines of JSON configuration + ~1,900 lines of documentation.*

