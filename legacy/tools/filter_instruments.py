#!/usr/bin/env python3
"""
filter_instruments.py
=====================
High-performance loader and filter for market instruments (Groww / Indian Exchanges).
Zero external dependencies required (uses Python standard library).
Optionally supports Pandas if installed.

Features:
- Fast loading of large CSV instrument files (140k+ records in ~1 second).
- Comprehensive filtering by Exchange, Segment, Type, Underlying, Expiry, Strike range, etc.
- Option chain generator (pairing CE & PE strikes).
- In-memory indexing for O(1) token and symbol lookups.
- Unique underlying symbols explorer with FNO contract breakdowns.
- Prominent instrument and underlying company/index names in all outputs.
- Export to CSV, JSON, or pandas DataFrame.
- Full Command Line Interface (CLI) for terminal usage.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import sys
from dataclasses import asdict, dataclass
from datetime import date, datetime
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Set, Tuple, Union


# ---------------------------------------------------------------------------
# Data Model
# ---------------------------------------------------------------------------

@dataclass
class Instrument:
    exchange: str
    exchange_token: str
    trading_symbol: str
    groww_symbol: str
    name: str
    instrument_type: str  # CE, PE, FUT, EQ, IDX
    segment: str          # CASH, FNO, COMMODITY
    series: str
    isin: str
    underlying_symbol: str
    underlying_exchange_token: str
    expiry_date: str      # YYYY-MM-DD or empty
    strike_price: float   # strike price, or 0.0 / -0.01
    lot_size: int
    tick_size: float
    freeze_quantity: int
    is_reserved: bool
    buy_allowed: bool
    sell_allowed: bool
    internal_trading_symbol: str
    is_intraday: bool
    display_name: str = ""

    @property
    def is_call(self) -> bool:
        """Returns True if the instrument is a Call Option (CE)."""
        return self.instrument_type == "CE"

    @property
    def is_put(self) -> bool:
        """Returns True if the instrument is a Put Option (PE)."""
        return self.instrument_type == "PE"

    @property
    def is_option(self) -> bool:
        """Returns True if the instrument is an option (CE or PE)."""
        return self.instrument_type in ("CE", "PE")

    @property
    def is_future(self) -> bool:
        """Returns True if the instrument is a Future (FUT)."""
        return self.instrument_type == "FUT"

    @property
    def is_equity(self) -> bool:
        """Returns True if the instrument is Cash Equity (EQ)."""
        return self.instrument_type == "EQ"

    @property
    def is_index(self) -> bool:
        """Returns True if the instrument is an Index (IDX)."""
        return self.instrument_type == "IDX"

    @property
    def is_tradable(self) -> bool:
        """Returns True if both buy and sell are allowed and not reserved."""
        return self.buy_allowed and self.sell_allowed and not self.is_reserved

    @property
    def upstox_instrument_key(self) -> str:
        """
        Generate the corresponding Upstox API v2/v3 instrument_key.
        - Equities: NSE_EQ|INE... / BSE_EQ|INE...
        - Indices: NSE_INDEX|Nifty 50, BSE_INDEX|SENSEX, etc.
        - Derivatives (FNO): NSE_FO|{exchange_token}
        - Commodities: MCX_FO|{exchange_token}
        """
        exch = self.exchange.upper()
        seg = self.segment.upper()
        itype = self.instrument_type.upper()

        if seg == "CASH":
            if itype == "IDX":
                idx_map = {
                    "NIFTY": "NSE_INDEX|Nifty 50",
                    "NIFTY 50": "NSE_INDEX|Nifty 50",
                    "BANKNIFTY": "NSE_INDEX|Nifty Bank",
                    "FINNIFTY": "NSE_INDEX|Nifty Fin Service",
                    "MIDCPNIFTY": "NSE_INDEX|NIFTY MID SELECT",
                    "NIFTYIT": "NSE_INDEX|NIFTY IT",
                    "SENSEX": "BSE_INDEX|SENSEX",
                    "BANKEX": "BSE_INDEX|BANKEX",
                }
                clean_sym = self.trading_symbol.upper()
                if clean_sym in idx_map:
                    return idx_map[clean_sym]
                return f"{exch}_INDEX|{self.name or self.trading_symbol}"
            if self.isin:
                return f"{exch}_EQ|{self.isin}"
            return f"{exch}_EQ|{self.exchange_token}"
        elif seg == "FNO":
            return f"{exch}_FO|{self.exchange_token}"
        elif seg == "COMMODITY":
            return f"MCX_FO|{self.exchange_token}" if exch == "MCX" else f"{exch}_FO|{self.exchange_token}"
        return f"{exch}_{seg}|{self.exchange_token}"

    def to_dict(self) -> Dict[str, Any]:
        """Convert instrument instance to a plain dictionary, ensuring name and upstox key are populated."""
        d = asdict(self)
        if not d.get("name") and self.display_name:
            d["name"] = self.display_name
        d["upstox_instrument_key"] = self.upstox_instrument_key
        return d


# ---------------------------------------------------------------------------
# Instrument Master / Manager
# ---------------------------------------------------------------------------

class InstrumentManager:
    """
    Loads, indexes, and provides fast search and filtering over instrument data.
    """

    def __init__(self, instruments: List[Instrument], source_path: Optional[str] = None):
        self.instruments: List[Instrument] = instruments
        self.source_path = source_path
        self._by_token: Dict[Tuple[str, str], Instrument] = {}
        self._by_symbol: Dict[Tuple[str, str], Instrument] = {}
        self._by_underlying: Dict[str, List[Instrument]] = {}
        self._underlying_names: Dict[str, str] = {}
        self._build_indexes()

    def _build_indexes(self) -> None:
        """Build internal lookup tables and resolve company/index names."""
        # 1. Collect all explicit company/index names from CASH EQ and IDX instruments
        for inst in self.instruments:
            if inst.name:
                clean_name = inst.name.strip()
                if inst.trading_symbol:
                    self._underlying_names[inst.trading_symbol.upper()] = clean_name
                if inst.underlying_symbol:
                    self._underlying_names[inst.underlying_symbol.upper()] = clean_name

        # 2. Build primary search indexes and resolve display_name for options/futures
        for inst in self.instruments:
            # Set display_name: use explicit name if present, else lookup underlying's name
            if inst.name:
                inst.display_name = inst.name
            elif inst.underlying_symbol and inst.underlying_symbol.upper() in self._underlying_names:
                inst.display_name = self._underlying_names[inst.underlying_symbol.upper()]
            elif inst.underlying_symbol:
                inst.display_name = inst.underlying_symbol
            else:
                inst.display_name = inst.trading_symbol

            # Index by (exchange, token) and token alone
            self._by_token[(inst.exchange.upper(), inst.exchange_token)] = inst
            self._by_token[("", inst.exchange_token)] = inst

            # Index by (exchange, trading_symbol) and symbol alone
            self._by_symbol[(inst.exchange.upper(), inst.trading_symbol.upper())] = inst
            self._by_symbol[("", inst.trading_symbol.upper())] = inst

            # Index by underlying symbol
            if inst.underlying_symbol:
                und_key = inst.underlying_symbol.upper()
                if und_key not in self._by_underlying:
                    self._by_underlying[und_key] = []
                self._by_underlying[und_key].append(inst)

    def __len__(self) -> int:
        return len(self.instruments)

    def __iter__(self) -> Iterable[Instrument]:
        return iter(self.instruments)

    def __getitem__(self, index: int) -> Instrument:
        return self.instruments[index]

    # -----------------------------------------------------------------------
    # Direct Lookups
    # -----------------------------------------------------------------------

    def get_by_token(self, token: Union[str, int], exchange: Optional[str] = None) -> Optional[Instrument]:
        """Find an instrument by its exchange token."""
        exch = exchange.upper() if exchange else ""
        return self._by_token.get((exch, str(token)))

    def get_by_symbol(self, trading_symbol: str, exchange: Optional[str] = None) -> Optional[Instrument]:
        """Find an instrument by its exact trading symbol."""
        exch = exchange.upper() if exchange else ""
        return self._by_symbol.get((exch, trading_symbol.strip().upper()))

    def get_underlying_name(self, underlying_symbol: str) -> str:
        """Get the full company/index name for an underlying symbol if known."""
        return self._underlying_names.get(underlying_symbol.strip().upper(), underlying_symbol)

    # -----------------------------------------------------------------------
    # Unique Underlyings Exploration (FNO / COMMODITY / CASH)
    # -----------------------------------------------------------------------

    def get_underlyings(
        self,
        segment: Optional[str] = "FNO",
        exchange: Optional[str] = None,
    ) -> List[str]:
        """
        Get a sorted list of unique underlying symbols available.

        Parameters
        ----------
        segment : str, optional
            Filter by segment (default: 'FNO'). Pass None or 'ALL' for all segments.
        exchange : str, optional
            Filter by exchange (e.g. 'NSE', 'BSE', 'MCX').

        Returns
        -------
        List[str]
            Alphabetically sorted list of distinct underlying symbols.
        """
        seg = None if not segment or segment.upper() == "ALL" else segment.upper()
        exch = exchange.upper() if exchange else None

        symbols: Set[str] = set()
        for inst in self.instruments:
            if not inst.underlying_symbol:
                continue
            if seg and inst.segment.upper() != seg:
                continue
            if exch and inst.exchange.upper() != exch:
                continue
            symbols.add(inst.underlying_symbol.upper())

        return sorted(symbols)

    def get_underlying_summary(
        self,
        segment: Optional[str] = "FNO",
        exchange: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """
        Get all unique underlying symbols along with contract counts (CE, PE, FUT),
        full company/index name, and expiry date range.

        Returns
        -------
        List of dicts sorted by total_contracts descending.
        """
        seg = None if not segment or segment.upper() == "ALL" else segment.upper()
        exch = exchange.upper() if exchange else None

        grouped: Dict[Tuple[str, str], Dict[str, Any]] = {}

        for inst in self.instruments:
            if not inst.underlying_symbol:
                continue
            if seg and inst.segment.upper() != seg:
                continue
            if exch and inst.exchange.upper() != exch:
                continue

            key = (inst.underlying_symbol.upper(), inst.exchange.upper())
            if key not in grouped:
                und_sym = key[0]
                grouped[key] = {
                    "underlying_symbol": und_sym,
                    "name": self.get_underlying_name(und_sym),
                    "exchange": key[1],
                    "segment": inst.segment.upper(),
                    "total_contracts": 0,
                    "ce_count": 0,
                    "pe_count": 0,
                    "fut_count": 0,
                    "expiries": set(),
                }

            entry = grouped[key]
            entry["total_contracts"] += 1
            if inst.is_call:
                entry["ce_count"] += 1
            elif inst.is_put:
                entry["pe_count"] += 1
            elif inst.is_future:
                entry["fut_count"] += 1

            if inst.expiry_date:
                entry["expiries"].add(inst.expiry_date)

        # Post-process into sorted list
        results: List[Dict[str, Any]] = []
        for entry in grouped.values():
            sorted_exp = sorted(entry["expiries"])
            results.append({
                "underlying_symbol": entry["underlying_symbol"],
                "name": entry["name"],
                "exchange": entry["exchange"],
                "segment": entry["segment"],
                "total_contracts": entry["total_contracts"],
                "ce_count": entry["ce_count"],
                "pe_count": entry["pe_count"],
                "fut_count": entry["fut_count"],
                "expiries_count": len(sorted_exp),
                "nearest_expiry": sorted_exp[0] if sorted_exp else "-",
                "furthest_expiry": sorted_exp[-1] if sorted_exp else "-",
            })

        results.sort(key=lambda x: (x["total_contracts"], x["underlying_symbol"]), reverse=True)
        return results

    def get_fno_underlying_instruments(
        self,
        target_segment: str = "CASH",
        exchange: Optional[str] = "NSE",
        output: Optional[str] = None,
        export_keys: Optional[str] = None,
    ) -> List[Instrument]:
        """
        Extract the actual tradable instruments for all FNO-eligible underlyings.
        - target_segment="CASH" -> Resolves Cash Equity (EQ) or Index (IDX) with ISIN/tokens.
        - target_segment="FUT"  -> Resolves near-month active Future contract.
        """
        target_seg = target_segment.strip().upper() if target_segment else "CASH"
        exch = exchange.upper() if exchange else "NSE"

        # 1. Get all unique FNO underlyings
        fno_underlyings = self.get_underlyings(segment="FNO", exchange=exch)

        results: List[Instrument] = []

        for und_sym in fno_underlyings:
            if target_seg in ("CASH", "EQ"):
                # Try cash equity
                eq = self.get_equity(und_sym, exchange=exch)
                if eq:
                    results.append(eq)
                    continue
                # Try cash index (NIFTY, BANKNIFTY, etc.)
                indices = self.filter(
                    trading_symbol=und_sym,
                    exchange=exch,
                    instrument_type="IDX",
                    segment="CASH",
                )
                if indices:
                    results.append(indices[0])
                    continue
            elif target_seg in ("FUT", "FNO"):
                futs = self.get_futures(und_sym, exchange=exch)
                if futs:
                    results.append(futs[0])
                    continue

        if output:
            out = output.strip()
            if out.lower().endswith(".json"):
                self.export_json(results, out)
            else:
                self.export_csv(results, out)
            print(f"Exported {len(results):,} FNO underlying {target_seg} instruments to: {out}")

        if export_keys:
            out_k = export_keys.strip()
            self.export_keys(results, out_k)
            print(f"Exported {len(results):,} Upstox keys to: {out_k}")

        return results

    # -----------------------------------------------------------------------
    # Filtering
    # -----------------------------------------------------------------------

    def filter(
        self,
        exchange: Optional[Union[str, Sequence[str]]] = None,
        segment: Optional[Union[str, Sequence[str]]] = None,
        instrument_type: Optional[Union[str, Sequence[str]]] = None,
        type: Optional[Union[str, Sequence[str]]] = None,                  # Alias for instrument_type
        underlying_symbol: Optional[Union[str, Sequence[str]]] = None,
        underlying: Optional[Union[str, Sequence[str]]] = None,            # Alias for underlying_symbol
        trading_symbol: Optional[str] = None,
        symbol: Optional[str] = None,                                      # Alias for trading_symbol
        name: Optional[str] = None,
        series: Optional[str] = None,
        expiry_date: Optional[Union[str, Sequence[str]]] = None,
        expiry: Optional[Union[str, Sequence[str]]] = None,                # Alias for expiry_date
        expiry_from: Optional[str] = None,
        expiry_to: Optional[str] = None,
        strike_price: Optional[Union[float, int, Sequence[Union[float, int]]]] = None,
        strike: Optional[Union[float, int, Sequence[Union[float, int]]]] = None, # Alias for strike_price
        min_strike: Optional[float] = None,
        max_strike: Optional[float] = None,
        buy_allowed: Optional[bool] = None,
        sell_allowed: Optional[bool] = None,
        is_tradable: Optional[bool] = None,
        is_intraday: Optional[bool] = None,
        query: Optional[str] = None,
        search: Optional[str] = None,                                      # Alias for query
        custom_filter: Optional[Callable[[Instrument], bool]] = None,
        output: Optional[str] = None,                                      # Save results to CSV or JSON
        export_keys: Optional[str] = None,                                 # Export Upstox keys to file
    ) -> List[Instrument]:
        """
        Filter instruments matching all specified criteria.
        Optionally saves results directly to `output` (CSV/JSON) or `export_keys` (TXT/JSON).
        """
        # Resolve convenient aliases
        if instrument_type is None and type is not None:
            instrument_type = type
        if underlying_symbol is None and underlying is not None:
            underlying_symbol = underlying
        if trading_symbol is None and symbol is not None:
            trading_symbol = symbol
        if expiry_date is None and expiry is not None:
            expiry_date = expiry
        if strike_price is None and strike is not None:
            strike_price = strike
        if query is None and search is not None:
            query = search

        def _to_upper_set(val: Optional[Union[str, Sequence[str]]]) -> Optional[Set[str]]:
            if val is None:
                return None
            if isinstance(val, str):
                return {val.strip().upper()}
            return {x.strip().upper() for x in val}

        def _to_float_set(val: Optional[Union[float, int, Sequence[Union[float, int]]]]) -> Optional[Set[float]]:
            if val is None:
                return None
            if isinstance(val, (int, float)):
                return {float(val)}
            return {float(x) for x in val}

        exchanges = _to_upper_set(exchange)
        segments = _to_upper_set(segment)
        types = _to_upper_set(instrument_type)
        underlyings = _to_upper_set(underlying_symbol)
        expiries = _to_upper_set(expiry_date)
        strikes = _to_float_set(strike_price)

        sym_search = trading_symbol.strip().upper() if trading_symbol else None
        name_search = name.strip().upper() if name else None
        series_search = series.strip().upper() if series else None
        q_search = query.strip().upper() if query else None

        # Candidate pool optimization: if single underlying_symbol, start from indexed pool
        if underlyings and len(underlyings) == 1:
            key = next(iter(underlyings))
            candidates = self._by_underlying.get(key, [])
        else:
            candidates = self.instruments

        results: List[Instrument] = []

        for inst in candidates:
            if exchanges and inst.exchange.upper() not in exchanges:
                continue
            if segments and inst.segment.upper() not in segments:
                continue
            if types and inst.instrument_type.upper() not in types:
                continue
            if underlyings and inst.underlying_symbol.upper() not in underlyings:
                continue
            if expiries and inst.expiry_date not in expiries:
                continue
            if expiry_from and (not inst.expiry_date or inst.expiry_date < expiry_from):
                continue
            if expiry_to and (not inst.expiry_date or inst.expiry_date > expiry_to):
                continue
            if strikes is not None and inst.strike_price not in strikes:
                continue
            if min_strike is not None and inst.strike_price < min_strike:
                continue
            if max_strike is not None and inst.strike_price > max_strike:
                continue
            if is_tradable is not None and inst.is_tradable != is_tradable:
                continue
            if buy_allowed is not None and inst.buy_allowed != buy_allowed:
                continue
            if sell_allowed is not None and inst.sell_allowed != sell_allowed:
                continue
            if is_intraday is not None and inst.is_intraday != is_intraday:
                continue
            if series_search and inst.series.upper() != series_search:
                continue
            if sym_search and sym_search not in inst.trading_symbol.upper():
                continue
            if name_search and (name_search not in inst.name.upper() and name_search not in inst.display_name.upper()):
                continue
            if q_search and (
                q_search not in inst.trading_symbol.upper()
                and q_search not in inst.name.upper()
                and q_search not in inst.display_name.upper()
                and q_search not in inst.underlying_symbol.upper()
            ):
                continue
            if custom_filter and not custom_filter(inst):
                continue

            results.append(inst)

        # Save to file if output requested
        if output:
            out = output.strip()
            if out.lower().endswith(".json"):
                self.export_json(results, out)
            else:
                self.export_csv(results, out)
            print(f"Exported {len(results):,} filtered instruments to: {out}")

        if export_keys:
            out_k = export_keys.strip()
            self.export_keys(results, out_k)
            print(f"Exported {len(results):,} Upstox keys to: {out_k}")

        return results

    # -----------------------------------------------------------------------
    # Trading Convenience Helpers
    # -----------------------------------------------------------------------

    def get_expiries(self, underlying: str, exchange: Optional[str] = None) -> List[str]:
        """
        Get sorted list of unique expiry dates for an underlying (e.g. 'NIFTY').
        """
        filtered = self.filter(
            underlying_symbol=underlying,
            exchange=exchange,
            segment="FNO" if exchange != "MCX" else None,
        )
        expiries = {i.expiry_date for i in filtered if i.expiry_date}
        return sorted(expiries)

    def get_futures(self, underlying: str, exchange: Optional[str] = None) -> List[Instrument]:
        """
        Get all active Futures contracts for an underlying, sorted by expiry.
        """
        futs = self.filter(
            underlying_symbol=underlying,
            instrument_type="FUT",
            exchange=exchange,
        )
        return sorted(futs, key=lambda x: x.expiry_date or "")

    def get_equity(self, symbol: str, exchange: str = "NSE") -> Optional[Instrument]:
        """
        Get the cash equity instrument for a given symbol (e.g. 'RELIANCE').
        """
        eqs = self.filter(
            trading_symbol=symbol,
            exchange=exchange,
            instrument_type="EQ",
            segment="CASH",
        )
        for eq in eqs:
            if eq.trading_symbol.upper() == symbol.upper():
                return eq
        return eqs[0] if eqs else None

    def get_option_chain(
        self,
        underlying: str,
        expiry: Optional[str] = None,
        exchange: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """
        Generate an option chain matrix pairing Call (CE) and Put (PE) at each strike.
        """
        if expiry is None:
            available_expiries = self.get_expiries(underlying, exchange=exchange)
            today_str = date.today().isoformat()
            future_expiries = [e for e in available_expiries if e >= today_str]
            expiry = future_expiries[0] if future_expiries else (available_expiries[0] if available_expiries else None)

        if not expiry:
            return []

        options = self.filter(
            underlying_symbol=underlying,
            expiry_date=expiry,
            instrument_type=["CE", "PE"],
            exchange=exchange,
        )

        matrix: Dict[float, Dict[str, Any]] = {}
        for opt in options:
            strike = opt.strike_price
            if strike not in matrix:
                matrix[strike] = {
                    "strike_price": strike,
                    "expiry_date": expiry,
                    "CE": None,
                    "PE": None,
                }
            if opt.is_call:
                matrix[strike]["CE"] = opt
            elif opt.is_put:
                matrix[strike]["PE"] = opt

        return [matrix[k] for k in sorted(matrix.keys())]

    # -----------------------------------------------------------------------
    # Summary & Analytics
    # -----------------------------------------------------------------------

    def summary(self) -> Dict[str, Any]:
        """Return aggregate summary metrics of the loaded dataset."""
        exchanges: Dict[str, int] = {}
        segments: Dict[str, int] = {}
        types: Dict[str, int] = {}
        underlying_counts: Dict[str, int] = {}

        for inst in self.instruments:
            exchanges[inst.exchange] = exchanges.get(inst.exchange, 0) + 1
            segments[inst.segment] = segments.get(inst.segment, 0) + 1
            types[inst.instrument_type] = types.get(inst.instrument_type, 0) + 1
            if inst.underlying_symbol:
                underlying_counts[inst.underlying_symbol] = underlying_counts.get(inst.underlying_symbol, 0) + 1

        top_underlyings = sorted(underlying_counts.items(), key=lambda x: x[1], reverse=True)[:10]

        return {
            "total_instruments": len(self.instruments),
            "source_path": self.source_path,
            "exchanges": exchanges,
            "segments": segments,
            "instrument_types": types,
            "top_underlyings": dict(top_underlyings),
        }

    # -----------------------------------------------------------------------
    # Export Methods
    # -----------------------------------------------------------------------

    def to_dataframe(self, instruments: Optional[Sequence[Instrument]] = None):
        """
        Convert instruments to a pandas DataFrame (requires pandas installed).
        """
        try:
            import pandas as pd
        except ImportError:
            raise ImportError(
                "pandas is not installed in the current Python environment.\n"
                "Install it using: pip install pandas\n"
                "Or use the standard library methods (to_dicts, export_csv, export_json)."
            )
        data = [i.to_dict() for i in (instruments if instruments is not None else self.instruments)]
        return pd.DataFrame(data)

    @staticmethod
    def export_csv(instruments: Sequence[Instrument], output_path: str) -> None:
        """Export list of instruments to a CSV file."""
        dirname = os.path.dirname(os.path.abspath(output_path))
        if dirname:
            os.makedirs(dirname, exist_ok=True)

        if not instruments:
            print(f"Warning: 0 instruments matched filter. Writing empty CSV with headers to: {output_path}", file=sys.stderr)
            # Create a sample dict to get complete field names
            dummy = Instrument(
                exchange="", exchange_token="", trading_symbol="", groww_symbol="",
                name="", instrument_type="", segment="", series="", isin="",
                underlying_symbol="", underlying_exchange_token="", expiry_date="",
                strike_price=0.0, lot_size=1, tick_size=0.05, freeze_quantity=0,
                is_reserved=False, buy_allowed=False, sell_allowed=False,
                internal_trading_symbol="", is_intraday=False
            )
            fieldnames = list(dummy.to_dict().keys())
            with open(output_path, "w", newline="", encoding="utf-8") as f:
                writer = csv.DictWriter(f, fieldnames=fieldnames)
                writer.writeheader()
            return

        first_row = instruments[0].to_dict()
        fieldnames = list(first_row.keys())
        with open(output_path, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            for inst in instruments:
                writer.writerow(inst.to_dict())

    @staticmethod
    def export_json(instruments: Sequence[Instrument], output_path: str, indent: int = 2) -> None:
        """Export list of instruments to a JSON file."""
        dirname = os.path.dirname(os.path.abspath(output_path))
        if dirname:
            os.makedirs(dirname, exist_ok=True)
        data = [i.to_dict() for i in instruments]
        with open(output_path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=indent)

    @staticmethod
    def export_keys(instruments: Sequence[Instrument], output_path: str) -> None:
        """
        Export Upstox instrument_keys (one per line, or JSON array if .json).
        Ready for feeding directly into Upstox Market Data and Candle APIs.
        """
        dirname = os.path.dirname(os.path.abspath(output_path))
        if dirname:
            os.makedirs(dirname, exist_ok=True)
        keys = [i.upstox_instrument_key for i in instruments]
        if output_path.lower().endswith(".json"):
            with open(output_path, "w", encoding="utf-8") as f:
                json.dump(keys, f, indent=2)
        else:
            with open(output_path, "w", encoding="utf-8") as f:
                f.write("\n".join(keys) + "\n")


# ---------------------------------------------------------------------------
# Loader Function
# ---------------------------------------------------------------------------

def _parse_bool(val: Any) -> bool:
    if isinstance(val, bool):
        return val
    s = str(val).strip().lower()
    return s in ("1", "true", "yes", "t")


def _parse_float(val: Any, default: float = 0.0) -> float:
    try:
        s = str(val).strip()
        return float(s) if s else default
    except (ValueError, TypeError):
        return default


def _parse_int(val: Any, default: int = 0) -> int:
    try:
        s = str(val).strip()
        if not s:
            return default
        return int(float(s))
    except (ValueError, TypeError):
        return default


def load_instruments(csv_path: str = "instrument.csv") -> InstrumentManager:
    """
    Load instruments from the specified CSV file and return an InstrumentManager.
    """
    if not os.path.exists(csv_path):
        raise FileNotFoundError(f"Instruments file not found at: {csv_path}")

    instruments: List[Instrument] = []

    with open(csv_path, mode="r", encoding="utf-8", errors="replace") as f:
        reader = csv.DictReader(f)
        for row in reader:
            raw_name = (row.get("name") or "").strip()
            inst = Instrument(
                exchange=(row.get("exchange") or "").strip(),
                exchange_token=(row.get("exchange_token") or "").strip(),
                trading_symbol=(row.get("trading_symbol") or "").strip(),
                groww_symbol=(row.get("groww_symbol") or "").strip(),
                name=raw_name,
                instrument_type=(row.get("instrument_type") or "").strip(),
                segment=(row.get("segment") or "").strip(),
                series=(row.get("series") or "").strip(),
                isin=(row.get("isin") or "").strip(),
                underlying_symbol=(row.get("underlying_symbol") or "").strip(),
                underlying_exchange_token=(row.get("underlying_exchange_token") or "").strip(),
                expiry_date=(row.get("expiry_date") or "").strip(),
                strike_price=_parse_float(row.get("strike_price"), 0.0),
                lot_size=_parse_int(row.get("lot_size"), 1),
                tick_size=_parse_float(row.get("tick_size"), 0.05),
                freeze_quantity=_parse_int(row.get("freeze_quantity"), 0),
                is_reserved=_parse_bool(row.get("is_reserved")),
                buy_allowed=_parse_bool(row.get("buy_allowed")),
                sell_allowed=_parse_bool(row.get("sell_allowed")),
                internal_trading_symbol=(row.get("internal_trading_symbol") or "").strip(),
                is_intraday=_parse_bool(row.get("is_intraday")),
                display_name=raw_name,
            )
            instruments.append(inst)

    return InstrumentManager(instruments, source_path=csv_path)


# Convenience alias
def filter_instruments(
    csv_path: str = "instrument.csv",
    output: Optional[str] = None,
    export_keys: Optional[str] = None,
    **kwargs,
) -> List[Instrument]:
    """
    Direct one-liner helper to load and filter instruments.
    Supports optional output="filename.csv" or export_keys="keys.txt".
    """
    manager = load_instruments(csv_path)
    return manager.filter(output=output, export_keys=export_keys, **kwargs)


# ---------------------------------------------------------------------------
# CLI & Terminal Formatter
# ---------------------------------------------------------------------------

def _print_table(
    rows: List[Dict[str, Any]],
    columns: Sequence[str],
    max_col_widths: Optional[Dict[str, int]] = None,
) -> None:
    """Print tabular data cleanly to stdout with optional column width truncation."""
    if not rows:
        print("No matching records found.")
        return

    max_col_widths = max_col_widths or {}

    def _format_cell(val: Any, col: str) -> str:
        s = str(val if val is not None else "")
        limit = max_col_widths.get(col)
        if limit and len(s) > limit:
            return s[: limit - 3] + "..."
        return s

    # Calculate column widths based on formatted cell contents
    widths = {col: len(col) for col in columns}
    for row in rows:
        for col in columns:
            cell_str = _format_cell(row.get(col, ""), col)
            if len(cell_str) > widths[col]:
                widths[col] = len(cell_str)

    # Header
    header = " | ".join(col.ljust(widths[col]) for col in columns)
    separator = "-+-".join("-" * widths[col] for col in columns)
    print(header)
    print(separator)

    # Rows
    for row in rows:
        line = " | ".join(_format_cell(row.get(col, ""), col).ljust(widths[col]) for col in columns)
        print(line)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Load, explore, and filter Indian market instruments (instrument.csv).",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # List all unique underlying symbols available in FNO:
  python3 filter_instruments.py --underlyings

  # Detailed FNO contract breakdown for each underlying:
  python3 filter_instruments.py --underlyings --underlying-details

  # List unique commodity underlyings:
  python3 filter_instruments.py --underlyings COMMODITY

  # Search NIFTY Call options for a specific expiry:
  python3 filter_instruments.py --underlying NIFTY --type CE --expiry 2026-10-29

  # Display Option Chain for NIFTY:
  python3 filter_instruments.py --option-chain NIFTY --expiry 2026-10-29

  # Find Cash Equity with company name:
  python3 filter_instruments.py --symbol RELIANCE --segment CASH

  # Search by company name:
  python3 filter_instruments.py --name "Edelweiss" --limit 20
        """,
    )

    parser.add_argument("--csv", default="instrument.csv", help="Path to instrument.csv (default: instrument.csv)")
    parser.add_argument("--summary", action="store_true", help="Display file statistics and category breakdowns")
    parser.add_argument(
        "--underlyings",
        nargs="?",
        const="FNO",
        default=None,
        metavar="SEGMENT",
        help="List all unique underlying symbols available (default segment: FNO; or specify CASH, COMMODITY, or ALL)",
    )
    parser.add_argument(
        "--underlying-details",
        action="store_true",
        help="When used with --underlyings, show contract counts (CE/PE/FUT) and expiry range for each",
    )
    parser.add_argument("--exchange", "-e", help="Exchange to filter (e.g. NSE, BSE, MCX)")
    parser.add_argument("--segment", "-s", help="Segment (e.g. CASH, FNO, COMMODITY)")
    parser.add_argument("--type", "-t", help="Instrument type (e.g. CE, PE, FUT, EQ, IDX)")
    parser.add_argument("--underlying", "-u", help="Underlying symbol (e.g. NIFTY, BANKNIFTY, RELIANCE)")
    parser.add_argument("--symbol", help="Exact or partial trading symbol")
    parser.add_argument("--name", help="Partial company or asset name")
    parser.add_argument("--search", help="Free-text search across symbol and name")
    parser.add_argument("--expiry", help="Expiry date (YYYY-MM-DD)")
    parser.add_argument("--expiry-from", help="Minimum expiry date (inclusive)")
    parser.add_argument("--expiry-to", help="Maximum expiry date (inclusive)")
    parser.add_argument("--strike", type=float, help="Exact strike price")
    parser.add_argument("--min-strike", type=float, help="Minimum strike price")
    parser.add_argument("--max-strike", type=float, help="Maximum strike price")
    parser.add_argument("--token", help="Lookup instrument by exchange token")
    parser.add_argument("--expiries", help="List all distinct expiry dates for an underlying")
    parser.add_argument("--option-chain", help="Generate option chain for underlying")
    parser.add_argument(
        "--fno-stocks",
        action="store_true",
        help="Export the underlying CASH equity stocks for all FNO-eligible underlyings (with ISIN, tokens, and Upstox keys)",
    )
    parser.add_argument(
        "--target-segment",
        default="CASH",
        choices=["CASH", "EQ", "FUT"],
        help="Target segment when resolving FNO underlyings: CASH (stock equity) or FUT (near-month future) (default: CASH)",
    )
    parser.add_argument("--limit", type=int, default=50, help="Maximum rows to display in terminal (default: 50)")
    parser.add_argument("--output", "-o", help="Save filtered results to a CSV or JSON file (e.g. output.csv)")
    parser.add_argument("--export-keys", "-k", help="Export Upstox instrument_keys directly (e.g. keys.txt or keys.json)")
    parser.add_argument("--format", choices=["table", "json"], default="table", help="Terminal output format")

    args = parser.parse_args()

    # Verify CSV file existence
    if not os.path.exists(args.csv):
        print(f"Error: File not found: {args.csv}", file=sys.stderr)
        sys.exit(1)

    print(f"Loading {args.csv}...")
    manager = load_instruments(args.csv)
    print(f"Loaded {len(manager):,} instruments.\n")

    # 1. Summary mode
    if args.summary:
        stats = manager.summary()
        print("=" * 60)
        print("INSTRUMENTS DATASET SUMMARY")
        print("=" * 60)
        print(f"Total Instruments : {stats['total_instruments']:,}")
        print(f"Source File       : {stats['source_path']}")
        print("\nExchanges:")
        for ex, c in stats["exchanges"].items():
            print(f"  - {ex:12}: {c:,}")
        print("\nSegments:")
        for seg, c in stats["segments"].items():
            print(f"  - {seg:12}: {c:,}")
        print("\nInstrument Types:")
        for t, c in stats["instrument_types"].items():
            print(f"  - {t:12}: {c:,}")
        print("\nTop 10 Underlyings by contract count:")
        for und, c in stats["top_underlyings"].items():
            name = manager.get_underlying_name(und)
            print(f"  - {und:15} ({name[:25]}): {c:,}")
        print("=" * 60)

        if args.output:
            out = args.output.strip()
            dirname = os.path.dirname(os.path.abspath(out))
            if dirname:
                os.makedirs(dirname, exist_ok=True)
            with open(out, "w", encoding="utf-8") as f:
                json.dump(stats, f, indent=2)
            print(f"Exported summary statistics to: {out}")
        return

    # 2. FNO Underlying Instruments Mode (e.g. --fno-stocks)
    if args.fno_stocks:
        fno_insts = manager.get_fno_underlying_instruments(
            target_segment=args.target_segment,
            exchange=args.exchange,
            output=args.output,
            export_keys=args.export_keys,
        )
        print(f"Resolved {len(fno_insts):,} FNO underlying {args.target_segment} instruments:\n")
        cols = [
            "exchange",
            "exchange_token",
            "trading_symbol",
            "name",
            "segment",
            "instrument_type",
            "isin",
            "upstox_instrument_key",
        ]
        dicts = [r.to_dict() for r in fno_insts[: args.limit]]
        _print_table(dicts, cols, max_col_widths={"name": 28, "upstox_instrument_key": 24})
        if len(fno_insts) > args.limit:
            print(f"\n... Showing first {args.limit} of {len(fno_insts):,} instruments. Use --limit to view more or --output to save.")
        return

    # 3. Underlyings list / exploration mode
    if args.underlyings is not None:
        target_seg = args.underlyings.strip().upper()
        details = manager.get_underlying_summary(segment=target_seg, exchange=args.exchange)
        seg_label = target_seg if target_seg != "ALL" else "All Segments"
        exch_label = f" on {args.exchange.upper()}" if args.exchange else ""

        print(f"Found {len(details)} unique underlying symbols in {seg_label}{exch_label}:\n")

        # Save to file if output requested
        if args.output:
            out = args.output.strip()
            dirname = os.path.dirname(os.path.abspath(out))
            if dirname:
                os.makedirs(dirname, exist_ok=True)
            if out.lower().endswith(".json"):
                with open(out, "w", encoding="utf-8") as f:
                    json.dump(details, f, indent=2)
            else:
                fieldnames = list(details[0].keys()) if details else ["underlying_symbol", "name", "exchange"]
                with open(out, "w", newline="", encoding="utf-8") as f:
                    writer = csv.DictWriter(f, fieldnames=fieldnames)
                    writer.writeheader()
                    writer.writerows(details)
            print(f"Exported {len(details):,} underlying symbols to: {out}\n")

        if args.format == "json":
            print(json.dumps(details[: args.limit], indent=2))
            return

        if args.underlying_details:
            cols = [
                "underlying_symbol",
                "name",
                "exchange",
                "total_contracts",
                "ce_count",
                "pe_count",
                "fut_count",
                "expiries_count",
                "nearest_expiry",
            ]
            _print_table(details[: args.limit], cols, max_col_widths={"name": 32})
            if len(details) > args.limit:
                print(f"\n... Showing {args.limit} of {len(details)} underlyings. Use --limit to show more.")
        else:
            # Clean compact view: symbol, company name, contract count
            for i, row in enumerate(details[: args.limit], 1):
                name_str = f"({row['name']})" if row['name'] and row['name'] != row['underlying_symbol'] else ""
                print(f"{i:>4}. {row['underlying_symbol']:<15} {name_str:<35} [{row['total_contracts']} contracts | Exch: {row['exchange']}]")

            if len(details) > args.limit:
                print(f"\n... Showing {args.limit} of {len(details)} underlyings. Use --limit to view more.")
            print("\nTip: Add '--underlying-details' to see full breakdown of CE/PE/FUT and expiries.")
        return

    # 3. Expiries list mode
    if args.expiries:
        expiries = manager.get_expiries(args.expiries, exchange=args.exchange)
        und_name = manager.get_underlying_name(args.expiries)
        print(f"Available expiry dates for {args.expiries.upper()} - {und_name} ({len(expiries)} dates):")
        for exp in expiries:
            print(f"  - {exp}")

        if args.output:
            out = args.output.strip()
            dirname = os.path.dirname(os.path.abspath(out))
            if dirname:
                os.makedirs(dirname, exist_ok=True)
            with open(out, "w", encoding="utf-8") as f:
                if out.lower().endswith(".json"):
                    json.dump(expiries, f, indent=2)
                else:
                    f.write("\n".join(expiries) + "\n")
            print(f"\nExported {len(expiries)} expiry dates to: {out}")
        return

    # 4. Option Chain mode
    if args.option_chain:
        chain = manager.get_option_chain(args.option_chain, expiry=args.expiry, exchange=args.exchange)
        if not chain:
            print(f"No option chain data found for underlying '{args.option_chain}'.")
            return

        und_name = manager.get_underlying_name(args.option_chain)
        expiry_used = chain[0]["expiry_date"]
        print(f"Option Chain: {args.option_chain.upper()} ({und_name}) | Expiry: {expiry_used} | {len(chain)} strikes\n")

        # Format option chain table
        display_rows = []
        for row in chain:
            ce = row["CE"]
            pe = row["PE"]
            display_rows.append({
                "CE Token": ce.exchange_token if ce else "-",
                "CE Symbol": ce.trading_symbol if ce else "-",
                "CE Lot": ce.lot_size if ce else "-",
                "STRIKE": f"{row['strike_price']:,.2f}".rstrip("0").rstrip("."),
                "PE Symbol": pe.trading_symbol if pe else "-",
                "PE Token": pe.exchange_token if pe else "-",
                "PE Lot": pe.lot_size if pe else "-",
            })

        if args.output:
            out = args.output.strip()
            dirname = os.path.dirname(os.path.abspath(out))
            if dirname:
                os.makedirs(dirname, exist_ok=True)
            if out.lower().endswith(".json"):
                chain_export = []
                for row in chain:
                    chain_export.append({
                        "strike_price": row["strike_price"],
                        "expiry_date": row["expiry_date"],
                        "CE": row["CE"].to_dict() if row["CE"] else None,
                        "PE": row["PE"].to_dict() if row["PE"] else None,
                    })
                with open(out, "w", encoding="utf-8") as f:
                    json.dump(chain_export, f, indent=2)
            else:
                cols = ["CE Token", "CE Symbol", "CE Lot", "STRIKE", "PE Symbol", "PE Token", "PE Lot"]
                with open(out, "w", newline="", encoding="utf-8") as f:
                    writer = csv.DictWriter(f, fieldnames=cols)
                    writer.writeheader()
                    writer.writerows(display_rows)
            print(f"Exported option chain ({len(chain)} strikes) to: {out}\n")

        cols = ["CE Token", "CE Symbol", "CE Lot", "STRIKE", "PE Symbol", "PE Token", "PE Lot"]
        _print_table(display_rows[: args.limit], cols)
        if len(display_rows) > args.limit:
            print(f"\n... Showing {args.limit} of {len(display_rows)} strikes. Use --limit to show more.")
        return

    # 5. Token direct lookup
    if args.token:
        found = manager.get_by_token(args.token, exchange=args.exchange)
        if found:
            print("Instrument found:")
            print(json.dumps(found.to_dict(), indent=2))
            if args.output:
                out = args.output.strip()
                manager.export_json([found], out)
                print(f"Exported instrument to: {out}")
        else:
            print(f"No instrument found for token: {args.token}")
        return

    # 6. General filtering
    results = manager.filter(
        exchange=args.exchange,
        segment=args.segment,
        instrument_type=args.type,
        underlying_symbol=args.underlying,
        trading_symbol=args.symbol,
        name=args.name,
        expiry_date=args.expiry,
        expiry_from=args.expiry_from,
        expiry_to=args.expiry_to,
        strike_price=args.strike,
        min_strike=args.min_strike,
        max_strike=args.max_strike,
        query=args.search,
        output=args.output,
        export_keys=args.export_keys,
    )

    print(f"Found {len(results):,} matching instruments.\n")


    # Output to stdout
    if args.format == "json":
        data = [r.to_dict() for r in results[: args.limit]]
        print(json.dumps(data, indent=2))
    else:
        # Include 'name' and 'upstox_instrument_key' in the results table!
        cols = [
            "exchange",
            "exchange_token",
            "trading_symbol",
            "name",
            "instrument_type",
            "segment",
            "underlying_symbol",
            "expiry_date",
            "strike_price",
            "lot_size",
            "upstox_instrument_key",
        ]
        dicts = [r.to_dict() for r in results[: args.limit]]
        _print_table(dicts, cols, max_col_widths={"name": 28, "upstox_instrument_key": 24})
        if len(results) > args.limit:
            print(f"\n... Showing first {args.limit} of {len(results):,} results. Use --limit to view more or --output to save.")


if __name__ == "__main__":
    main()
