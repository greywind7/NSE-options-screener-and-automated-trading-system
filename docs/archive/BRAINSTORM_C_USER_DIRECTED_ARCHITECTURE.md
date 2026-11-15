# Track C: Distributed Event-Driven Architecture Redesign

> **Document Type**: Comprehensive Architectural Blueprint & Refactor Specification  
> **Source**: User-Directed Synthesis & Modern Quantitative Trading Stack  
> **Core Concepts**: Multi-Process Topology, ZeroMQ IPC, Shared Memory (Lock-Free Reader/Writer), Separate Quotes/OHLC Managers, No-Score Qualitative Matrix, Look-Ahead Delayed Fills, Columnar Indexed Sim Engine.

---

## 1. System Topology & Process Decomposition

Instead of a monolithic Python process burdened by the Global Interpreter Lock (GIL), thread contention, and synchronous multi-adapter execution, the system is decomposed into specialized, decoupled OS processes communicating over high-speed IPC (ZeroMQ) and POSIX Shared Memory.

```mermaid
flowchart TD
    subgraph Market Ingestion
        WS[Broker WebSocket / Replay Feed] --> FeedHandler[Feed Listener Process]
        FeedHandler -->|ZMQ PUB: Equity Ticks| SocketEq[ipc:///tmp/feed_equity.ipc]
        FeedHandler -->|ZMQ PUB: Option Ticks| SocketOpt[ipc:///tmp/feed_options.ipc]
    end

    subgraph Data & State Managers
        SocketEq --> CandleMgr[Candle / OHLC Manager Process]
        SocketOpt --> QuotesMgr[Quotes & Depth Manager Process]
        
        CandleMgr -->|Single Writer| ShmCandles[(Shared Memory: OHLCV & Indicators)]
        QuotesMgr -->|Single Writer| ShmQuotes[(Shared Memory: Option Depth & Greeks)]
        QuotesMgr -->|Circular Buffer| RingBuffer[(10-Min Historical Depth Cache)]
        
        CandleMgr -->|ZMQ PUB: Bar Closed Events| SocketEvents[ipc:///tmp/market_events.ipc]
    end

    subgraph Strategy & Alpha
        ShmCandles -.->|Lock-Free Reader| Strat1[Momentum Strategy Engine]
        ShmCandles -.->|Lock-Free Reader| Strat2[Future Strategies...]
        ShmQuotes -.->|Lock-Free Reader| Strat1
        SocketEvents --> Strat1
        
        Strat1 -->|ZMQ PUSH: Order Intents| SocketOrders[ipc:///tmp/order_router.ipc]
    end

    subgraph Execution & Routing
        SocketOrders --> Router[Order Execution Router]
        Router -->|Live Intents| LiveExec[Live Broker Process]
        Router -->|Paper Intents| PaperExec[Paper Execution Engine]
        Router -->|Delayed Intents| DelayedExec[Look-Ahead Delayed Engine]
        
        RingBuffer -.->|Instant Lookahead Lookup| DelayedExec
        ShmQuotes -.->|Instant Depth Lookup| PaperExec
    end
```

### Architectural Principles:
1. **Separation of Concerns**: Ingestion, candle construction, quote caching, strategy evaluation, and order execution run in separate OS processes with dedicated CPU affinities.
2. **Strict Reader/Writer Demarcation**: Data managers are the **Single Writers** to their respective shared memory segments. Strategy and execution workers are strictly **Concurrent Readers**. No locking!
3. **Bandwidth Partitioning**: 90% of feed traffic (option quotes) is isolated to `QuotesMgr`. The strategy engine never consumes or parses raw option ticks.

---

## 2. Shared Memory Architecture & Data Managers

### 2.1 The Candle / OHLC Manager
The Candle Manager subscribes exclusively to `ipc:///tmp/feed_equity.ipc`.
- Aggregates equity ticks into multi-resolution bars: 1-sec, 1-min, 3-min, 5-min.
- Computes running VWAP, ATR(14), Volume Multiples, and Donchian channels using pre-compiled NumPy/Numba kernels.
- Writes the current state of all tracked equities into a POSIX Shared Memory segment (`multiprocessing.shared_memory` or PyArrow Plasma).

```python
# Conceptual Shared Memory Struct for Multi-Timeframe Equity State
import numpy as np
from multiprocessing import shared_memory

EQUITY_DTYPE = np.dtype([
    ('symbol', 'S16'),
    ('timestamp', 'f8'),
    ('ltp', 'f4'),
    ('vwap_1m', 'f4'),
    ('atr_14', 'f4'),
    ('volume_multiple', 'f4'),
    ('consolidation_bars', 'i4'),
    ('range_compression_pct', 'f4'),
    ('regime_state', 'i2'), # 1=BULL, -1=BEAR, 0=CHOP
])
```

- **Zero-Copy Readers**: Strategies open the shared memory block via `mmap` / NumPy buffer. Reading the state of 20 tracked stocks takes **$< 1\ \mu\text{s}$** without deserialization overhead.
- **Bar Close Pulse**: When a 1-minute or 5-minute bar completes, the manager publishes a 16-byte message over `ipc:///tmp/market_events.ipc` (`[EVENT: BAR_CLOSE, SYMBOL: RELIANCE, TF: 1M]`).

### 2.2 The Quotes & Depth Manager
The Quotes Manager subscribes to `ipc:///tmp/feed_options.ipc`.
- Maintains the latest 5-depth bid/ask, LTP, spread, and implied volatility for all active option contracts.
- Stores the active strike state in a memory-mapped hash map: `(strike_id) -> Struct(bid, ask, ltp, spread, ts)`.
- Maintains a **Rolling Circular Ring Buffer** of the last 10 minutes of quote snapshots in RAM, indexed by timestamp for instant lookahead retrieval.

---

## 3. Qualitative Setup Matrix: "No-Score" Decision Engine

### 3.1 Eliminating the Continuous Score ($0-100$)
Continuous scores create artificial boundary cliffs: a score of 69.9 is rejected while 70.1 executes, despite being statistically indistinguishable. Moreover, linear score summing allows a strong arbitrary bonus (+10 regime) to push a garbage breakout into execution.

### 3.2 The Discrete Qualitative Matrix
The Strategy Engine evaluates setups against a deterministic **Feature Matrix**:

```mermaid
flowchart LR
    subgraph Market Regime Filter
        R1[Regime: STRONG_TREND] --> M1[Allow All Quality Tiers]
        R2[Regime: NEUTRAL_RANGE] --> M2[Allow Tier 1 Only]
        R3[Regime: CHOPPY / CONTRACTION] --> M3[Trading HALTED]
    end

    subgraph Structural Pattern Predicates
        P1[Range Compression >= 4 Bars]
        P2[Volume Surge >= 2.5x Median]
        P3[Close in Top 15% of Candle]
        P4[Spread <= 2.0%]
    end

    P1 & P2 & P3 & P4 --> Qualify{Meets All Invariants?}
    Qualify -->|Yes| Order[Emit OrderIntent: Tier 1]
    Qualify -->|No| Discard[Discard Setup]
```

### 3.3 Qualitative Setup Matrix Specification

| Parameter | Tier 1 (Pristine Momentum) | Tier 2 (Retest & Continuation) | Disqualified / Ineligible |
| :--- | :--- | :--- | :--- |
| **Market Regime** | Aligned (Nifty slope $> 0.15\%$ for calls) | Neutral ($-0.15\%$ to $+0.15\%$) | Choppy (ADX $< 18$) or Opposite Trend |
| **Consolidation** | $\ge 4$ bars within $\le 0.35\%$ band | Breakout pullback held above VWAP | Erratic / wide range whip |
| **Volume Multiple** | $\ge 2.5\times$ 20-period median | $\ge 1.8\times$ 20-period median | $< 1.5\times$ (Fakeout risk) |
| **Option Spread** | $\le 1.5\%$ of ask | $\le 2.5\%$ of ask | $> 3.0\%$ (Mathematical veto) |
| **Option Moneyness** | Discrete ATM ($k=0$) | Discrete ITM1 ($k=-1$) | Illiquid OTM strikes |
| **Capital Sizing** | Full Lot Allocation ($1.0\times$) | Conservative Allocation ($0.6\times$) | Zero Allocation |

---

## 4. The Live "Look-Ahead" Paper Delayed Engine

### 4.1 The Core Insight
In live trading, simulated delayed fills currently rely on OS timer threads and blocking quote retries.
**The New Approach**: Instead of sleeping or spawning threads, the Delayed Engine fills orders by reading from the Quotes Manager's **10-minute Rolling Depth Ring Buffer**.

```mermaid
sequenceDiagram
    autonumber
    participant Strat as Strategy Process
    participant DelayEngine as Delayed Paper Engine
    participant QuotesCache as Quotes Ring Buffer (10-Min Cache)
    
    Strat->>DelayEngine: Order Intent at T_trigger (09:30:00) with Delay Delta = 5s
    Note over DelayEngine: Order enqueued in Pending Heap (Target Ts = 09:30:05)
    Note over DelayEngine: Strategy & Feeds continue running at full speed without blocking
    
    Note over DelayEngine: Live Clock advances to 09:30:05
    DelayEngine->>QuotesCache: Instant Lookup: Get Exact Quote at T = 09:30:05
    QuotesCache-->>DelayEngine: Returns Quote (Bid: 104.20, Ask: 104.50)
    Note over DelayEngine: Fill evaluated instantly with Zero OS Sleep and Zero Lock Contention!
```

#### Why This Completely Eliminates Divergence:
1. **Zero Thread Contention**: No `threading.Timer`, no `threading.Lock()`, no GIL freezing.
2. **Microsecond Fill Evaluation**: When current wall clock reaches $T_{\text{target}}$, the exact quote is queried directly from the shared depth cache.
3. **Trigger-Anchored Slippage Protection**:
   $$\text{Slippage} = \frac{P_{\text{depth}}(T_{\text{target}}) - P_{\text{trigger}}}{P_{\text{trigger}}}$$
   If slippage exceeds $3.5\%$, the order is rejected as a "Chased Move" rather than entering at the peak.

---

## 5. The Simulation Revolution: Columnar Indexed Depth

### 5.1 The Current 4.4GB Replay Bottleneck
The current replayer reads every raw tick sequentially from massive recording files. Because 90%+ of ticks are option contracts that the strategy does not hold, the engine spends 95% of its CPU time discarding option ticks, crawling at 2,500 ticks/sec.

### 5.2 Columnar Pre-Indexing with Polars / PyArrow
Separate the simulation data into two distinct structures:

```mermaid
flowchart TD
    RawCSV[4.4GB Daily Tick Recording] --> ETL[One-Time Fast ETL / Polars Ingestion]
    
    ETL --> EqStream[Clean Equity Ticks / 1s Bars ~50MB]
    ETL --> OptParquet[Sorted Columnar Option Depth Table ~400MB]
    
    subgraph Lightning Simulation Engine
        EqStream -->|Stream at 200,000 ticks/sec| SimStrategy[Strategy Alpha & Pattern Detector]
        SimStrategy -->|Breakout Signal at Ts = T| Trigger[Query Target Option Strike]
        
        Trigger -->|O log N Binary ASOF Join| OptParquet
        OptParquet -->|Instant Depth & Fill Price| Trigger
    end
```

### 5.3 Asynchronous On-Demand Depth Lookup
- The simulation engine replays **ONLY equity ticks**. Strategy pattern evaluation processes **100,000+ ticks/second**.
- When an order triggers at timestamp $T_{\text{signal}}$:
  - For **Instant Paper**: Query the Option Parquet/Arrow table for `(contract, timestamp == T_signal)` using `searchsorted` / `asof_join`.
  - For **Delayed Paper**: Query `(contract, timestamp == T_signal + delay_seconds)`.
- **Result**: An entire trading day's simulation completes in **3 to 5 seconds** instead of 15 to 20 minutes!
- Delayed paper and instant paper are evaluated against the identical, immutable depth table, eliminating all random tick-skipping discrepancies.

---

## 6. Execution Layer Refactor: Decoupled Multiprocess Workers

### 6.1 Demise of the MultiExecutor God Object
The current `MultiExecutor` wraps Paper, Paper Delayed, and Live into a single synchronous multiplexer. A freeze or delay in one mode impacts all modes.

### 6.2 Independent Execution Workers (ZeroMQ PUSH/PULL)
The `OrderRouter` receives orders from strategies and fans them out to independent processes:
- **`LiveBrokerWorker`**: Dedicated process with exclusive network connection to broker REST/WebSocket APIs. Runs with high OS priority. Completely isolated from testing/paper code.
- **`PaperWorker`**: Instant simulated fills against shared memory depth.
- **`DelayedPaperWorker`**: Evaluates delayed fills via the lookahead ring buffer.

```mermaid
flowchart LR
    Router[Order Router Process]
    
    Router -->|ZMQ PULL/PUSH| LiveWorker[Live Broker Worker Process]
    Router -->|ZMQ PULL/PUSH| PaperWorker[Instant Paper Worker Process]
    Router -->|ZMQ PULL/PUSH| DelayedWorker[Look-Ahead Delayed Worker Process]
    
    LiveWorker --> BrokerAPI[Broker Gateway / Exchange]
    PaperWorker --> PaperLedger[Paper Journal]
    DelayedWorker --> DelayedLedger[Delayed Journal]
```

---

## 7. Recommended External Technology Stack

| Capability | Recommended Library | Justification |
| :--- | :--- | :--- |
| **Inter-Process Messaging** | `pyzmq` (ZeroMQ) | High-throughput, low-latency ($< 10\ \mu\text{s}$), brokerless IPC socket communication. |
| **Fast Columnar Depth & Sim** | `polars` & `pyarrow` | Memory-mapped Arrow tables, zero-copy reads, instant multi-threaded `asof_join` for simulation. |
| **Shared Memory IPC** | `multiprocessing.shared_memory` | Native POSIX shared memory buffers; lock-free single-writer multi-reader architecture. |
| **Vectorized Technical Math** | `numba` & `numpy` | JIT-compiled C-speed execution for VWAP, ATR, and range compression calculations. |
| **Fast Serialization** | `msgpack` | Binary serialization $5\times$ faster than JSON with smaller payload footprint. |
| **Historical Analytics** | `duckdb` | Instant SQL queries directly over compressed Parquet quote files for post-trade analysis. |

---

## 8. Comparative Synthesis: Track C vs. Current Architecture

| Architectural Dimension | Current Architecture | Track C Redesign |
| :--- | :--- | :--- |
| **Process Model** | Monolithic single process | Distributed multi-process with ZeroMQ IPC |
| **Feed Handling** | Mixed equity and option ticks in single queue | Dedicated split sockets: `feed_equity` vs `feed_options` |
| **Memory Access** | Shared Python dicts guarded by `threading.Lock` | Lock-free POSIX Shared Memory (Single-Writer Multi-Reader) |
| **Strategy Ingestion** | Consumes raw ticks (10k/sec) | Consumes pre-aggregated candles & shared memory features |
| **Scoring Model** | Continuous $0-100$ linear score + arbitrary bonuses | Discrete Qualitative Setup Matrix (No continuous score) |
| **Delayed Execution** | `threading.Timer` + OS thread sleep (750ms contention) | Zero-sleep ring buffer lookahead cache |
| **Simulation Speed** | 2,500 ticks/sec (~20 mins/day) | 100,000+ ticks/sec (~5 seconds/day via Polars ASOF) |
| **Live Isolation** | Live execution blocked by paper/delayed locks | Live broker worker completely isolated in dedicated OS process |

