# AGENT_CONTEXT.md: Master Handoff & Platform Blueprint
===============================================================================
**System**: High-Throughput Distributed Options Algorithmic Trading Platform (Spec 2.0)  
**Target Market**: National Stock Exchange of India (NSE F&O Equities & Indices), MCX Commodities  
**Broker Gateways**: Upstox V3 API (Market Data Ingestion), Groww API (Live Execution)  
**Document Purpose**: Definitive continuity context and operational blueprint for successor AI agents  
**Status**: Active Production Architecture | Tests: 100% Green (134/134 passing)  
**Strict Operating Rules**: ZERO GIT COMMIT / ZERO GIT PUSH | ZERO MADE-UP / FABRICATED QUOTES  

---

## 1. Welcome to the Platform (Successor Agent Orientation)

Hello successor agent. You are stepping into an ultra-low-latency, multi-process algorithmic trading system built for the Indian derivative markets. This document contains the distilled engineering wisdom, operational nuances, architectural history, design invariants, and immediate priorities established across our extensive engineering sessions with the platform's architect and user.

When you digest this document, you should feel as though you have been here from the first commit. You have complete context: why decisions were made, what pitfalls were encountered, how bugs were resolved, what principles are non-negotiable, and where the roadmap leads next.

### 1.1 Non-Negotiable Ground Rules & Invariants

Before touching any code, memorize these core invariants:

1. **STRICTLY ZERO GIT COMMIT / ZERO GIT PUSH**:
   - Under no circumstances should you run `git commit`, `git push`, or alter git history.
   - The user manages source control out-of-band. All edits must remain in the working tree as clean, uncommitted modifications.
   - Always verify your working copy with `git status` to ensure no accidental branches, stashes, or commits were created.

2. **STRICT ZERO-FABRICATION PRICING INVARIANT**:
   - **Never make up, synthesize, or default prices to `LTP` or `entry_price`**.
   - If market depth is missing, the book has no bids, or an option tick arrives without quote depth:
     - For options: `bid = 0.0`, `ask = 0.0`.
     - For exits: Sell orders execute strictly at prevailing genuine `bid`. If `bid <= 0.0`, exit is recorded at `0.0` or rejected. Never default to `entry_price` (which falsely creates a 0% P&L break-even illusion).
     - In historical simulation: Quotes are queried via exact binary search on the ingested depth dataset. Missing quotes return `None`; if `ask <= 0` or `bid <= 0`, the strike is rejected by spread gates. We purged every instance of `if ask <= 0: ask = ltp` and `else trade.entry_price`.
   - Real trading decisions must use genuine market depth or abort.

3. **100% GREEN TEST SUITE INVARIANT**:
   - The system is protected by 134 comprehensive unit and integration tests across core, common, execution, simulation, and UI layers.
   - Always verify all changes by running:
     ```bash
     .venv/bin/python runner.py test
     ```
   - No feature, refactor, or hotfix is complete until all 134 tests pass cleanly.

4. **USER INTERACTION STYLE & PREFERENCES**:
   - The user is sharp, deeply experienced in quantitative trading, and values clean, modular, robust engineering.
   - **Zero Regressions**: The user famously remarked: *"How come you always fix one thing and break another. Fix this lol"*. Treat this as your primary directive: test all affected components before declaring victory.
   - **Pragmatic & Direct**: Provide concise, factual summaries backed by terminal output, test runs, and simulation tables. Avoid fluff or hand-waving explanations.
   - **Modular Design**: Prefer clean, decoupled architectures (e.g. separate profile JSONs in `config/tracks/`, dedicated CLI flags, discrete IPC topics).

---

## 2. Complete Conversation Narrative & Architectural Evolution

To understand the platform as it exists today, you must understand how it evolved through four major generational phases:

```
┌──────────────────────────────────────────────────────────────────────────────────┐
│                             PLATFORM EVOLUTION ROADMAP                           │
├─────────────────────┬────────────────────┬────────────────────┬──────────────────┤
│ Generation 1.0      │ Generation 2.0     │ Generation 2.5     │ Generation 3.0   │
│ Monolithic Engine   │ Multi-Process ZMQ  │ Multi-Regime       │ Audit Hardening  │
│ • Single Python loop│ • POSIX SHM OHLC   │   Execution        │   & Dual WS      │
│ • Linear 0-100 score│ • 2-tier Feed ZMQ  │ • 12 Tracks (v1+v2)│ • Dual WebSockets│
│ • CSV trade journals│ • QuotesBroker LVC │ • config/tracks/   │ • 3,225 WS Keys  │
│ • In-memory candles │ • Headless Sim     │ • --track flag     │ • Zero-Price Fix │
└─────────────────────┴────────────────────┴────────────────────┴──────────────────┘
```

### Phase 1: The Monolithic Limitations (Legacy V1)
In the initial codebase, the platform ran as a monolithic loop. It attempted to process WebSocket feeds, aggregate 5-minute candles, evaluate continuous 0-100 linear technical scores, query option prices, and execute paper trades in a single thread or simple threadpool.
- **Failures Identified**:
  - Python Global Interpreter Lock (GIL) bottlenecks caused tick dropping during 09:15 market open bursts.
  - Linear 0-100 scores generated continuous "false positive" signals in choppy markets.
  - Option price lookups stalled the main loop because of blocking REST requests to the broker.
  - Simulated backtests produced unrealistic profits because they defaulted missing option quotes to equity LTP or entry prices.

### Phase 2: Architecture Spec 2.0 (The Distributed Multi-Process Pivot)
We redesigned the platform from the ground up as a distributed, multi-process, low-latency system inspired by institutional quantitative architectures:
1. **Separation of Concerns into OS Processes**:
   - `FeedHandler`: Ingestion only. Publishes raw ticks over ZeroMQ IPC.
   - `CandleManager`: Single writer. Aggregates wall-clock 5-minute candles directly into a fixed 777 KB POSIX Shared Memory segment (`shm_ohlc_221`).
   - `ScoringEngine3`: Alpha generation. Lock-free busy-spin loop (< 2 microseconds) reading SHM candles and emitting `SetupAlert` packets over ZeroMQ PUB.
   - `RMSService`: Risk gatekeeper. Subscribes to alerts, resolves ATM/ITM/OTM option strikes, enforces spread gates against `QuotesBroker`, and emits `ActionableOpportunity` packets.
   - `ExecutionSuperstructures`: Independent OS processes for `paper`, `delayed` (simulated execution queue with 30s-60s latency), and `live` (Groww API gateway).
2. **POSIX Shared Memory & Seqlocks**:
   - 221 F&O equities $\times$ 78 bars per session $\times$ 48 bytes per candle = 777 KB.
   - 16-byte Seqlock headers per row guarantee atomic, lock-free reader synchronization with zero OS mutex overhead.

### Phase 3: Strategy Tracks Evolution (v1 to v2 Multi-Regime)
Initially, execution relied on three baseline archetypes (`fast_momentum`, `trend_pullback`, `fixed_target`). During our brainstorming sessions, the user highlighted several key strategic insights:
- **Jitter Removal**: Early versions had a "45-second jitter buffer" that delayed trade entry. The user noted: *"Jitters can happen anytime, in fact whenever an alert happens, it IS a jitter event. Let's remove that"*. We eliminated the artificial delay, enabling instant entry upon breakout confirmation.
- **Risk-Reward Spectrum Expansion**: The user requested a diverse family of tracks spanning high-risk/high-reward, low-risk/low-reward, and low-risk/high-reward profiles.
- **We designed and implemented 8 new v2 Multi-Regime Tracks**:
  1. `structural_convexity`: Enforces structural invalidation if spot crosses the pattern's pole low or flag boundary.
  2. `opening_thrust_convex`: Aggressive morning trades (09:15-09:45) targeting explosive opening breakout volume.
  3. `delta_scalp`: Deep ITM (`ITM2`, $\Delta \approx 0.85$) scalping with 4% target and tight 2.5% stop-loss.
  4. `micro_chandelier`: ATR-based chandelier trailing ratchet for disciplined trend capture.
  5. `gamma_runner`: Deep OTM (`OTM2`, $\Delta \approx 0.30$) convexity harvester targeting explosive 35% gains with small position size.
  6. `volatility_expansion`: Enforces pre-breakout range compression (NR4 / Bollinger squeeze) before entry.
  7. `adaptive_regime`: Dynamic moneyness switching based on time-of-day market regime (Morning ATM $\to$ Midday ITM1 $\to$ Afternoon ATM).
  8. `trend_pullback_v2`: High-conviction pullback entries into the 20-EMA / VWAP zone.
- **Configuration Modularization**:
  - The user suggested: *"How about we make a separate tracks folder in the config folder. While running the strategy we can provide --track option... On the sim side we can have a drop down menu to select which track to use"*.
  - We created `config/tracks/tracks_v1.json`, `config/tracks/tracks_v2.json`, and `config/tracks/tracks_default.json`, wired them into `common/tracks/factory.py`, and added `--track <v1|v2|default>` across `runner.py sim` and `runner.py strat`.

### Phase 4: System Audit, Dual WebSockets & Zero-Price Fixes
The user provided a comprehensive `SYSTEM_AUDIT.md` highlighting potential bottlenecks. We audited every claim, drafted `AUDIT_TODO.md`, and completed Phase 1:
1. **Feed Handler Scale & Dual WebSockets**:
   - Scaled `max_stream_underlyings_with_options` from 110 to 221 (full universe).
   - Ingests all 215 F&O stocks $\times$ 14 popular options ($\text{ATM} \pm 3$ CE/PE) = 3,010 options + 215 equities = 3,225 streamed keys.
   - Sharded across two parallel Upstox WebSockets using deterministic CRC32 hashing:
     - `Conn 0`: ~1,605 keys (includes NIFTY 50 and 107 stocks).
     - `Conn 1`: ~1,619 keys (108 stocks).
     - Well below Upstox's hard limit of 2,000 keys per connection.
2. **Purged All Hallucinated / Fallback Prices**:
   - Eliminated `else trade.entry_price` in `base_track.py` and `structural_convexity.py`.
   - Eliminated `ask = ltp` / `bid = ltp` and EOD `trade.entry_price` defaults in `columnar_sim_engine.py`.
   - Tested historical simulation on 2026-11-19: Strategy achieved **+8.4% Paper / +7.9% Delayed** net returns using 100% genuine market quotes.
3. **Fixed RMS Moneyness Strike Resolution Bug**:
   - Fixed missing `itm2_strike` and `otm2_strike` variables in `core/rms/rms_service.py` when using `options_map`. Added comprehensive unit test coverage.

---

## 3. High-Throughput Distributed Architecture Specification (Spec 2.0)

```mermaid
flowchart TD
    subgraph MarketIngestion ["Market Ingestion Tier (Upstox API v3 Dual WebSockets)"]
        WS_0["Upstox WS Conn 0 (Shard 0: ~107 Stocks + NIFTY)"] --> Feed["FeedHandler Process"]
        WS_1["Upstox WS Conn 1 (Shard 1: ~108 Stocks)"] --> Feed
        Feed -->|ZMQ PUB: Equity Ticks| IPC_EQ["ipc:///tmp/feed_equity.ipc (TICK.EQ)"]
        Feed -->|ZMQ PUB: Option Depth Ticks| IPC_OPT["ipc:///tmp/feed_options.ipc (TICK.OPT)"]
    end

    subgraph MemoryTier ["Lock-Free Shared Memory & Cache Tier"]
        IPC_EQ --> CM["CandleManager Process (Single Writer)"]
        CM -->|Write 5m OHLCV Bars| SHM[("POSIX Shared Memory: shm_ohlc_221 (777 KB)")]
        IPC_OPT --> QB["QuotesBroker Process (LVC Cache)"]
        QB -->|ZMQ ROUTER / DEALER| IPC_ROUTER["ipc:///tmp/quotes_router.ipc"]
        QB -->|ZMQ PUB: Heartbeat & Active Depth| IPC_STREAM["ipc:///tmp/quotes_stream.ipc"]
    end

    subgraph AlphaTier ["Alpha Pipeline Tier"]
        SHM -.->|"Lock-Free Busy Spin Loop (< 2 us)"| SE["ScoringEngine 3.0 Process"]
        SE -->|ZMQ PUB: Setup Alerts (pole/flag/clv)| IPC_ALERTS["ipc:///tmp/scoring_alerts_pub.ipc"]
        IPC_ALERTS --> RMS["RMSService Process (Liquidity Gate)"]
        RMS <-->|ZMQ REQ/REP: Snapshot Depth| IPC_ROUTER
        RMS -->|ZMQ PUB: Actionable Opportunities| IPC_OPP["ipc:///tmp/rms_strike_pub.ipc"]
    end

    subgraph ExecutionTier ["Execution Superstructures Tier"]
        IPC_OPP --> SuperPaper["Paper Superstructure (Immediate Fill)"]
        IPC_OPP --> SuperDelayed["Delayed Superstructure (30-60s Latency Queue)"]
        IPC_OPP --> SuperLive["Live Superstructure (Groww API Gateway)"]
        IPC_STREAM --> SuperPaper
        IPC_STREAM --> SuperDelayed
        IPC_STREAM --> SuperLive
        SuperPaper --> JournalPaper[("recordings/YYYYMMDD/journal_paper.jsonl")]
        SuperDelayed --> JournalDelayed[("recordings/YYYYMMDD/journal_delayed.jsonl")]
        SuperLive --> JournalLive[("recordings/YYYYMMDD/journal_live.jsonl")]
    end
```

### 3.1 Inter-Process Communication (IPC) Socket Topology

All inter-process communication occurs over zero-copy Unix domain sockets (`ipc:///tmp/...`) using binary MessagePack serialization (`core/common/ipc_schemas.py`).

| Socket Name | Pattern | Endpoint | Publisher | Subscriber(s) | Message Schema |
|---|---|---|---|---|---|
| `feed_equity_ipc` | PUB/SUB | `ipc:///tmp/feed_equity.ipc` | `FeedHandler` | `CandleManager`, `MarketRecorder` | `EquityTickEvent` (topic: `TICK.EQ`) |
| `feed_options_ipc` | PUB/SUB | `ipc:///tmp/feed_options.ipc` | `FeedHandler` | `QuotesBroker`, `MarketRecorder` | `OptionDepthTickEvent` (topic: `TICK.OPT`) |
| `scoring_alerts_ipc` | PUB/SUB | `ipc:///tmp/scoring_alerts_pub.ipc` | `ScoringEngine3` | `RMSService`, `Dashboard` | `SetupAlert` (topic: `ALERT.<SYMBOL>`) |
| `rms_strike_ipc` | PUB/SUB | `ipc:///tmp/rms_strike_pub.ipc` | `RMSService` | Superstructures (`paper`, `delayed`, `live`) | `ActionableOpportunity` (topic: `OPP.<SYMBOL>`) |
| `quotes_router_ipc` | ROUTER/DEALER | `ipc:///tmp/quotes_router.ipc` | `QuotesBroker` | `RMSService`, Superstructures | Request: list of tokens; Reply: dict of depth quotes |
| `quotes_stream_ipc` | PUB/SUB | `ipc:///tmp/quotes_stream.ipc` | `QuotesBroker` | Superstructures | `OptionDepthTickEvent`, `HeartbeatEvent` |

---

## 4. Execution Tracks & Strategy Archetypes

The platform supports 12 modular execution track archetypes partitioned into **v1 Baseline** and **v2 Multi-Regime** tracks.

### 4.1 Track Profiles (`config/tracks/`)

Execution tracks are declared in dedicated JSON profile files:
- `config/tracks/tracks_v1.json`: Classical baseline tracks (`fast_momentum`, `trend_pullback`, `fixed_target`, `composite`).
- `config/tracks/tracks_v2.json`: 8 multi-regime tracks covering the risk-reward matrix.
- `config/tracks/tracks_default.json`: Unified profile running all v1 and v2 tracks concurrently.

You can select a profile on the CLI:
```bash
# Run simulation with v2 tracks
.venv/bin/python runner.py sim --date 2026-11-19 --track v2

# Run paper trading with v1 tracks
.venv/bin/python runner.py strat --mode paper --track v1
```

### 4.2 Comprehensive Track Specification Table

| Track Archetype | Class File | Moneyness | Target / SL | Risk/Reward Profile | Special Invariant / Exit Mechanism |
|---|---|---|---|---|---|
| `fast_momentum` | `fast_momentum.py` | `ATM` | 12% / 8% | Medium / Medium | Target decay after 180s down to 4% floor; 300s stagnation timeout. |
| `trend_pullback` | `trend_pullback.py` | `ITM1` | 25% / 12% | Medium / High | Trailing stop ratcheting 2% for every 2% advance; 1800s timeout. |
| `fixed_target` | `fixed_target.py` | `ATM` | 15% / 10% | Low / Low | Fixed monetary profit lock at +₹750/lot; 600s stagnation timeout. |
| `composite` | `composite_track.py` | Mixed | Composite | Managed | Multi-track portfolio manager with shared or equal-share capital pool. |
| `structural_convexity`| `structural_convexity.py`| `ATM` / `ITM1`| 18% / 7% | **Low Risk / High Reward** | **Immediate exit if spot equity crosses pole low or flag boundary**. |
| `opening_thrust_convex`| `opening_thrust_convex.py`| `ATM` | 25% / 9% | **High Risk / High Reward** | Entry strictly restricted to 09:15-09:45; aggressive runner. |
| `delta_scalp` | `delta_scalp.py` | `ITM2` | 4% / 2.5% | **High Risk / Low Reward** | Deep ITM ($\Delta \approx 0.85$); tight 90s rapid stagnation timeout. |
| `micro_chandelier` | `micro_chandelier.py` | `ITM1` | 14% / 6% | **Low Risk / Low Reward** | ATR-based trailing chandelier stop (3x ATR multiplier). |
| `gamma_runner` | `gamma_runner.py` | `OTM2` | 35% / 15% | **High Risk / High Reward** | Deep OTM ($\Delta \approx 0.30$); targets explosive momentum convexity. |
| `volatility_expansion`| `volatility_expansion.py`| `ATM` / `ITM1`| 20% / 8% | **High Risk / High Reward** | Requires pre-breakout range compression (NR4 / squeeze). |
| `adaptive_regime` | `adaptive_regime.py` | Time-Adaptive| 15% / 7.5% | **Low Risk / High Reward** | Switches moneyness: Morning ATM $\to$ Midday ITM1 $\to$ Afternoon ATM. |
| `trend_pullback_v2` | `trend_pullback_v2.py`| `ITM1` | 22% / 10% | **Low Risk / High Reward** | Pullback continuation with structural swing-low trailing stop. |

### 4.3 Moneyness Roles & Indexing
Options are categorized into 5 distinct moneyness roles:
- `ATM`: Strike closest to spot underlying price.
- `ITM1`: 1 strike into the money ($\text{CE} \implies \text{ATM} - \text{step}$; $\text{PE} \implies \text{ATM} + \text{step}$).
- `ITM2`: 2 strikes into the money (Deep ITM, $\Delta \approx 0.85$).
- `OTM1`: 1 strike out of the money ($\text{CE} \implies \text{ATM} + \text{step}$; $\text{PE} \implies \text{ATM} - \text{step}$).
- `OTM2`: 2 strikes out of the money (Deep OTM, $\Delta \approx 0.30$).

---

## 5. Scoring Engine 3.0 & Opportunity Pool

The Scoring Engine (`strategies/momentum/scoring_engine.py`) operates as a lock-free, zero-allocation busy-spin loop over the POSIX shared memory candle segment.

### 5.1 Physical Price Action Invariants (Zero Dud Signals)
Scoring Engine 3.0 completely discards continuous linear 0-100 scores in favor of strict physical breakout invariants:
1. **Flag & Pole Detection**: Identifies a multi-bar directional thrust (the "Pole") followed by a tight consolidation range (the "Flag").
2. **50% Max Retracement Invariant**: The flag consolidation cannot retrace more than 50% of the pole's vertical range:
   $$\text{Retracement} = \frac{\text{Pole High} - \text{Flag Low}}{\text{Pole High} - \text{Pole Low}} \le 0.50$$
3. **Close Location Value (CLV)**: The breakout candle must close strongly in its upper quartile:
   $$\text{CLV} = \frac{\text{Close} - \text{Low}}{\text{High} - \text{Low}} \ge 0.75$$
4. **Relative Volume Multiple ($r\text{VOL}$)**: Volume on the breakout bar must be at least $2.0\times$ its 30-day historical time-of-day baseline.
5. **Pattern Metadata Propagation**: The setup alert includes `pole_high`, `pole_low`, `flag_high`, `flag_low`, `atr`, `rvol`, and `clv`. Downstream tracks (e.g. `structural_convexity`) consume these exact levels to enforce structural stops on the equity tick stream.

---

## 6. Project Directory Map & Core Module Responsibilities

```
Trading Stuff/
├── AGENT_CONTEXT.md               # [THIS FILE] Master agent continuity context and architectural guide
├── AUDIT_TODO.md                  # Comprehensive 15-task audit implementation plan (Phases 1-4)
├── ARCHITECTURE_SPEC_2_0.md       # High-level architecture and Mermaid topology specification
├── LOW_LEVEL_DESIGN_2_0.md        # Detailed low-level engineering specification
├── WEB_VIEW_FLOW.md               # UI, telemetry, and live dashboard dataflow specification
├── runner.py                      # Central CLI entry point for all workflows
├── config/                        # System, strategy, execution, and track configuration JSONs
│   ├── system.json                # Shared memory dimensions, IPC endpoints, feed key limits
│   ├── execution.json             # Capital allocation, execution modes, timeouts
│   └── tracks/                    # Modular track profiles
│       ├── tracks_v1.json         # v1 Baseline profile
│       ├── tracks_v2.json         # v2 Multi-Regime profile
│       └── tracks_default.json    # Default unified profile
├── core/                          # Low-latency infrastructure tier
│   ├── feed/
│   │   ├── feed_handler.py        # Dual-connection Upstox WebSocket feed with CRC32 sharding
│   │   ├── position_registry.py   # Active open position subscription protection guard
│   │   └── proto/                 # Upstox MarketDataFeedV3 Protobuf schemas
│   ├── candles/
│   │   ├── candle_manager.py      # Single-writer SHM candle aggregator (wall-clock 5m bars)
│   │   └── shm_layout.py          # 48-byte CANDLE_DTYPE and 16-byte SEQLOCK_DTYPE definitions
│   ├── quotes/
│   │   └── quotes_broker.py       # In-memory LVC cache, ROUTER/DEALER snapshot server
│   ├── rms/
│   │   └── rms_service.py         # Option strike resolver, hybrid spread gate, opportunity publisher
│   ├── recorder/
│   │   └── market_recorder.py     # Lossless Parquet streaming recorder (equity + option ticks)
│   ├── instruments/
│   │   ├── subscription_manifest.py # 3,225 streamed keys manifest generator (221 underlyings)
│   │   ├── filter_universe.py     # Filter top F&O stocks and MCX commodity benchmarks
│   │   └── build_volume_index.py  # 30-day volume baseline index builder
│   └── common/
│       ├── ipc_schemas.py         # ZeroMQ MessagePack schemas (EquityTickEvent, SetupAlert, etc.)
│       ├── symbol_utils.py        # Token normalizer (NSE:RELIANCE <-> NSE_EQ|INE002A01018)
│       └── time_utils.py          # Market hours and IST timezone helpers
├── common/                        # Strategy tracks and shared interfaces
│   ├── interfaces.py              # ExecutionContext and TradeState dataclasses
│   └── tracks/                    # Track implementations
│       ├── base_track.py          # BaseTrack lifecycle, capital budgeting, zero-price exits
│       ├── factory.py             # Dynamic track factory and profile loader
│       ├── fast_momentum.py       # Fast momentum track with target decay
│       ├── trend_pullback.py      # Trend pullback track with trailing ratchet
│       ├── fixed_target.py        # Fixed ₹750 monetary target track
│       ├── composite_track.py     # Multi-track composite portfolio manager
│       ├── structural_convexity.py# Low-risk structural invalidation track
│       └── ... (v2 track files)
├── execution/                     # Superstructures tier
│   └── superstructures/
│       ├── base_superstructure.py # Base superstructure engine and JSONL journal logger
│       ├── paper.py               # Paper trading superstructure (immediate fill)
│       ├── delayed.py             # Delayed trading superstructure (30s-60s latency queue)
│       └── live.py                # Live Groww API adapter with TOTP and order reconciliation
├── simulation/                    # High-fidelity vectorized backtesting tier
│   ├── columnar_sim_engine.py     # Chronological tick replayer, exact depth lookup, zero fabrication
│   └── historical_replay.py       # Parquet streaming tick replayer over ZeroMQ
├── ui/                            # Live operator dashboard
│   ├── dashboard_app.py           # Rich terminal and web socket telemetry dashboard
│   └── web/                       # Web frontend assets
└── tests/                         # Comprehensive automated test suite (134 tests)
```

---

## 7. Master Operational Playbook (CLI Commands)

The platform is managed exclusively via the unified entry point `runner.py`:

### 7.1 Pre-Market Preparation
Run every morning before 09:00 IST to download instrument masters, filter eligible F&O equities, compute 30-day volume baselines, and compile the subscription manifest:
```bash
# Soft prepare (uses existing data if already downloaded today)
.venv/bin/python runner.py prepare

# Force re-download and rebuild from scratch
.venv/bin/python runner.py prepare --force
```

### 7.2 Running Simulations (Historical Backtesting)
Simulates a past session using exact recorded tick depth:
```bash
# Run historical simulation on 2026-11-19 with v2 tracks
.venv/bin/python runner.py sim --date 2026-11-19 --track v2

# Run historical simulation on 2026-11-19 with v1 tracks
.venv/bin/python runner.py sim --date 2026-11-19 --track v1

# Run historical simulation in batch mode across all recorded dates
.venv/bin/python runner.py sim --batch --track default
```

### 7.3 Live & Paper Trading
```bash
# Start paper trading superstructure with v2 tracks
.venv/bin/python runner.py strat --mode paper --track v2

# Start paper delayed trading superstructure (tests 30-60s execution latency)
.venv/bin/python runner.py strat --mode paper_delayed --track v2

# Start full multi-process system (Feed, CandleManager, Scoring, RMS, QuotesBroker)
.venv/bin/python runner.py start --mode paper --track v2

# Resume mid-day after restart without losing open positions
.venv/bin/python runner.py start --continue --mode paper
```

### 7.4 Risk Management & Circuit Breakers
```bash
# Emergency freeze: block new entries across all execution modes
.venv/bin/python runner.py freeze --mode all

# Resume normal entry processing
.venv/bin/python runner.py unfreeze --mode all

# Stop live trading immediately
.venv/bin/python runner.py stop-live
```

### 7.5 Testing & Validation
```bash
# Run full automated regression suite (all 134 tests)
.venv/bin/python runner.py test
```

---

## 8. Audit Implementation Roadmap (What To Do Next)

The platform audit roadmap is fully detailed in [`AUDIT_TODO.md`](file:///Users/yorha/Documents/Trading%20Stuff/AUDIT_TODO.md). Here is your executive priority guide:

```
┌────────────────────────────────────────────────────────────────────────┐
│                        REMAINING AUDIT PHASES                          │
├────────────────────────────────┬───────────────────────────────────────┤
│ Phase 1: Core Feed & Pricing   │ ✅ COMPLETE (Dual WS, 3,225 keys,     │
│                                │    Zero-Price fix, RMS Moneyness fix) │
├────────────────────────────────┼───────────────────────────────────────┤
│ Phase 2: Quotes Broker & SHM   │ ⏳ NEXT UP: Broker key canonicalization│
│          Parity                │    and dynamic option unsubscriptions │
├────────────────────────────────┼───────────────────────────────────────┤
│ Phase 3: Scoring 3.0 & Pool    │ ⏳ PENDING: Ring-buffer capacity &    │
│                                │    multi-timeframe volume index       │
├────────────────────────────────┼───────────────────────────────────────┤
│ Phase 4: Superstructures &     │ ⏳ PENDING: Slippage modeling &       │
│          Simulation Precision  │    Groww rate-limit circuit breakers  │
└────────────────────────────────┴───────────────────────────────────────┘
```

### Immediate Action Items for Incoming Agent:
1. **Task 05 (`core/quotes/quotes_broker.py`)**:
   - Ensure `QuotesBroker` LVC uses `normalize_instrument_token` universally on tick ingestion and client query lookup to eliminate colon vs pipe (`:` vs `|`) lookup misses.
2. **Task 06 (`core/feed/feed_handler.py` & `core/quotes/quotes_broker.py`)**:
   - Synchronize dynamic strike unsubscriptions between `FeedHandler` and `QuotesBroker` so evictions from WebSocket stream cleanly purge from QuotesBroker LVC unless protected by open position registry.
3. **Task 08 (`strategies/momentum/opportunity_pool.py`)**:
   - Verify ring-buffer capacity limits and opportunity TTL expiry (default 120s) so old signals never get re-emitted to RMS.
4. **Task 11 (`execution/superstructures/base_superstructure.py`)**:
   - Validate JSONL journal atomic flushing and ensure zero duplicate trade ID records on mid-day `--continue` reconstitution.

---

## 9. Final Words of Guidance

You are working with an exceptionally capable and ambitious algorithmic trading codebase. The foundation is robust, the tests are green, the pricing integrity is strictly enforced, and the multi-regime tracks possess genuine statistical edge.

Respect the constraints:
- **No git commits/pushes.**
- **No made-up numbers.**
- **100% green tests at all times.**

Good luck. Take the platform forward to new heights!
===============================================================================
