"""
Strike Selection Service & Option Liquidity Gate (RMS Tier).
Subscribes to scoring alerts, resolves ATM/ITM1/OTM1 option contracts,
enforces hybrid tick spread gate (Gap 1 / HIGH-004), and publishes ActionableOpportunities
over ZeroMQ PUB for mode-specific execution superstructures.
"""
import argparse
import csv
from datetime import date, datetime, timedelta
import json
import os
import signal
import sys
import time
from typing import Any, Dict, List, Optional
import zmq

from core.common.ipc_schemas import ActionableOpportunity, SetupAlert, pack_message, unpack_message
from core.common.logging_utils import get_logger
from core.common.symbol_utils import normalize_instrument_token
from core.common.time_utils import IST
from core.quotes.quotes_broker import QuotesBrokerClient, DEFAULT_QUOTES_ROUTER_IPC

_logger = get_logger("RMS_SERVICE")

DEFAULT_ALERTS_SUB_IPC = "ipc:///tmp/scoring_alerts_pub.ipc"
DEFAULT_RMS_PUB_IPC = "ipc:///tmp/rms_strike_pub.ipc"


def get_current_expiry_tag(ref_date: Optional[date] = None) -> str:
    """Compute nearest Thursday expiry tag (e.g. '26NOV') for fallback symbol construction."""
    d = ref_date or date.today()
    days_ahead = (3 - d.weekday()) % 7
    thursday = d + timedelta(days=days_ahead)
    return f"{thursday.day:02d}{thursday.strftime('%b').upper()}"


def get_strike_step(price: float) -> float:
    """Calculate strike step interval based on underlying price tier."""
    if price < 100:
        return 2.5
    elif price < 250:
        return 5.0
    elif price < 500:
        return 10.0
    elif price < 1000:
        return 20.0
    elif price < 2500:
        return 50.0
    else:
        return 100.0


def passes_spread_gate(bid: float, ask: float, max_spread_pct: float = 0.03) -> bool:
    """Return True iff quote has valid positive market depth and (ask - bid) / ask <= max_spread_pct."""
    if ask <= 0.0 or bid <= 0.0 or bid >= ask:
        return False
    return ((ask - bid) / ask) <= float(max_spread_pct)


def passes_atr_spread_gate(
    bid: float,
    ask: float,
    spot: float,
    atr: float,
    min_move_to_spread_mult: float = 2.0,
) -> bool:
    """Reject contracts whose bid-ask spread consumes too much of an expected 0.5-ATR option move."""
    if ask <= 0.0 or bid <= 0.0 or bid >= ask:
        return False
    if spot <= 0.0 or atr <= 0.0 or min_move_to_spread_mult <= 0.0:
        return True
    prem_pct = ask / spot
    spread_pct = (ask - bid) / ask
    exp_opt_move = (0.5 * atr / spot) / max(1e-6, prem_pct)
    return exp_opt_move >= float(min_move_to_spread_mult) * spread_pct


class RMSService:
    def __init__(
        self,
        alerts_ipc: Optional[str] = None,
        rms_pub_ipc: Optional[str] = None,
        quotes_router_ipc: Optional[str] = None,
        universe_csv: Optional[str] = None,
        ctx: Optional[zmq.Context] = None,
        **kwargs,
    ):
        from core.config.config_loader import get_config_manager
        cfg = get_config_manager()
        ipc = cfg.get_ipc_endpoints()
        risk_cfg = cfg.get_risk_config()
        scoring_cfg = cfg.get_scoring_config()
        inst_paths = cfg.instruments.get("instruments", {}).get("paths", {})

        self.alerts_ipc = alerts_ipc or ipc.get("scoring_alerts_ipc", DEFAULT_ALERTS_SUB_IPC)
        self.rms_pub_ipc = rms_pub_ipc or ipc.get("rms_strike_ipc", DEFAULT_RMS_PUB_IPC)
        self.quotes_router_ipc = quotes_router_ipc or ipc.get("quotes_router_ipc", DEFAULT_QUOTES_ROUTER_IPC)
        self.universe_csv = universe_csv or inst_paths.get("filtered_universe_csv", "data/filtered.csv")
        self.manifest_path = kwargs.get("manifest_path") or inst_paths.get("subscription_manifest_json", "data/subscription_manifest.json")
        spread_gate_cfg = risk_cfg.get("spread_gate", {})
        self.spread_gate = {
            "max_spread_pct": float(
                spread_gate_cfg.get("max_spread_pct", risk_cfg.get("max_spread_pct", 0.03))
            ),
        }
        self.atr_spread_gate_enabled = bool(
            kwargs.get("atr_spread_gate_enabled", risk_cfg.get("atr_spread_gate_enabled", True))
        )
        self.atr_spread_gate_mult = float(
            kwargs.get("atr_spread_gate_mult", risk_cfg.get("atr_spread_gate_mult", 2.0))
        )
        cap_raw = kwargs.get(
            "max_alerts_per_symbol_per_day",
            scoring_cfg.get("max_alerts_per_symbol_per_day", risk_cfg.get("max_alerts_per_symbol_per_day", 3)),
        )
        self.max_alerts_per_symbol_per_day: Optional[int] = int(cap_raw) if cap_raw is not None and int(cap_raw) > 0 else None
        self._daily_symbol_counts: Dict[tuple, int] = {}
        self.default_expiry_tag = str(
            kwargs.get("default_expiry_tag") or risk_cfg.get("default_expiry_tag") or get_current_expiry_tag()
        )
        self.running = False

        # Load lot sizes and metadata from universe
        self.lot_sizes: Dict[str, int] = {}
        self.tick_sizes: Dict[str, float] = {}
        self.options_map: Dict[str, Any] = {}
        self._load_universe()
        self._load_options_map()

        # ZeroMQ context and sockets
        self._owns_ctx = ctx is None
        self.ctx = ctx or zmq.Context()

        # Subscriber to alerts
        self.sub = self.ctx.socket(zmq.SUB)
        self.sub.setsockopt(zmq.LINGER, 0)
        self.sub.setsockopt(zmq.RCVHWM, 100000)
        self.sub.setsockopt(zmq.SUBSCRIBE, b"ALERT.")
        self.sub.connect(self.alerts_ipc)

        # Publisher for actionable opportunities
        self.pub = self.ctx.socket(zmq.PUB)
        self.pub.setsockopt(zmq.LINGER, 0)
        self.pub.setsockopt(zmq.SNDHWM, 100000)
        self.pub.bind(self.rms_pub_ipc)

        # Client to query QuotesBroker LVC
        self.quotes_client = QuotesBrokerClient(router_ipc=self.quotes_router_ipc, ctx=self.ctx)

    def _load_universe(self) -> None:
        if os.path.exists(self.universe_csv):
            with open(self.universe_csv, "r", encoding="utf-8") as f:
                reader = csv.DictReader(f)
                for row in reader:
                    sym = (
                        row.get("trading_symbol")
                        or row.get("tradingsymbol")
                        or row.get("symbol")
                        or row.get("display_name")
                    )
                    if sym:
                        s = sym.strip().upper()
                        ls = int(row.get("lot_size") or 1)
                        ts = float(row.get("tick_size") or 0.05)
                        self.lot_sizes[s] = ls
                        self.tick_sizes[s] = ts

    def _load_options_map(self) -> None:
        if os.path.exists(self.manifest_path):
            try:
                with open(self.manifest_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    self.options_map = data.get("options_map", {})
            except Exception as e:
                _logger.warning(f"Warning loading options map: {e}")

    def resolve_strikes(self, symbol: str, direction: str, trigger_price: float, atr: float = 0.0) -> Dict[str, Any]:
        """
        Compute ATM, ITM1, ITM2, OTM1, OTM2 strikes and query QuotesBroker for depth.
        Enforces hybrid tick spread gate (Gap 1 / HIGH-004): (ask - bid) <= max(0.50, 0.03 * ask).

        Parameters:
            symbol: Trading symbol of the underlying equity (e.g. "RELIANCE", "TCS").
            direction: Breakout direction ("BULL" or "BEAR").
            trigger_price: Prevailing price of the underlying equity when breakout alerted.

        Returns:
            Dictionary of resolved strikes mapped by moneyness role ('ATM', 'ITM1', 'ITM2',
            'OTM1', 'OTM2', plus backward-compatible 'ITM', 'OTM'). Each value contains genuine
            market depth (bid, ask, ltp, bid_qty, ask_qty, lot_size, token).
            Returns empty dict if no liquid strikes satisfy the spread gate.

        Architectural Invariant:
            Zero made-up or synthetic quotes. If QuotesBroker lacks a quote or the bid/ask spread
            fails the liquidity gate, the strike is rejected immediately.
        """
        opt_type = "CE" if direction.upper() == "BULL" else "PE"
        lot_size = self.lot_sizes.get(symbol.upper(), 1)

        # Select real strikes from options_map if available and within range
        opt_map = self.options_map.get(symbol.upper(), {}).get("strikes", {})
        avail_strikes = sorted([float(s) for s in opt_map.keys()]) if opt_map else []

        if avail_strikes and (min(avail_strikes) <= trigger_price <= max(avail_strikes)):
            atm_idx = min(range(len(avail_strikes)), key=lambda i: abs(avail_strikes[i] - trigger_price))
            atm_strike = avail_strikes[atm_idx]
            step = get_strike_step(trigger_price)
            if abs(atm_strike - trigger_price) > max(step * 1.5, trigger_price * 0.02):
                return {}
            if opt_type == "CE":
                itm_idx = max(0, atm_idx - 1)
                itm2_idx = max(0, atm_idx - 2)
                otm_idx = min(len(avail_strikes) - 1, atm_idx + 1)
                otm2_idx = min(len(avail_strikes) - 1, atm_idx + 2)
            else:
                itm_idx = min(len(avail_strikes) - 1, atm_idx + 1)
                itm2_idx = min(len(avail_strikes) - 1, atm_idx + 2)
                otm_idx = max(0, atm_idx - 1)
                otm2_idx = max(0, atm_idx - 2)
            itm_strike = avail_strikes[itm_idx]
            itm2_strike = avail_strikes[itm2_idx]
            otm_strike = avail_strikes[otm_idx]
            otm2_strike = avail_strikes[otm2_idx]
        else:
            step = get_strike_step(trigger_price)
            atm_strike = round(trigger_price / step) * step
            if opt_type == "CE":
                itm_strike = atm_strike - step
                itm2_strike = atm_strike - 2 * step
                otm_strike = atm_strike + step
                otm2_strike = atm_strike + 2 * step
            else:
                itm_strike = atm_strike + step
                itm2_strike = atm_strike + 2 * step
                otm_strike = atm_strike - step
                otm2_strike = atm_strike - 2 * step

        expiry_tag = self.default_expiry_tag

        token_roles = {
            "ATM": atm_strike,
            "ITM1": itm_strike,
            "ITM2": itm2_strike,
            "OTM1": otm_strike,
            "OTM2": otm2_strike,
        }

        query_tokens = []
        tokens_by_role = {}
        for role, strike_val in token_roles.items():
            mapped_key = None
            st_key_float = f"{float(strike_val):.1f}"
            st_key_int = f"{float(strike_val):.0f}"
            if st_key_float in opt_map:
                mapped_key = opt_map[st_key_float].get(opt_type)
            elif st_key_int in opt_map:
                mapped_key = opt_map[st_key_int].get(opt_type)
            elif str(strike_val) in opt_map:
                mapped_key = opt_map[str(strike_val)].get(opt_type)

            fallback_sym = f"NSE:{symbol.upper()}{expiry_tag}{int(strike_val)}{opt_type}"
            token_str = normalize_instrument_token(mapped_key) if mapped_key else fallback_sym
            tokens_by_role[role] = (strike_val, token_str, fallback_sym)
            query_tokens.append(token_str)
            if fallback_sym != token_str:
                query_tokens.append(fallback_sym)

        # Query QuotesBroker
        raw_quotes = self.quotes_client.get_quotes(query_tokens)

        resolved: Dict[str, Any] = {}
        for role, (strike_val, token_str, fallback_sym) in tokens_by_role.items():
            norm_token = normalize_instrument_token(token_str)
            q = raw_quotes.get(norm_token) or raw_quotes.get(normalize_instrument_token(fallback_sym))

            # Strictly require valid live quote from QuotesBroker LVC. Never fabricate synthetic values.
            if not q:
                continue

            ask = float(q.get("ask", 0.0))
            bid = float(q.get("bid", 0.0))
            ltp = float(q.get("ltp", 0.0))
            max_spread_pct = float(self.spread_gate.get("max_spread_pct", 0.03))
            if not passes_spread_gate(bid, ask, max_spread_pct=max_spread_pct):
                # Spread is too wide or quote invalid; skip this illiquid strike
                continue
            if self.atr_spread_gate_enabled and atr > 0.0:
                if not passes_atr_spread_gate(
                    bid,
                    ask,
                    spot=trigger_price,
                    atr=atr,
                    min_move_to_spread_mult=self.atr_spread_gate_mult,
                ):
                    continue

            resolved[role] = {
                "token": norm_token,
                "strike": strike_val,
                "option_type": opt_type,
                "ltp": float(q.get("ltp", 0.0)),
                "bid": bid,
                "ask": ask,
                "bid_qty": int(q.get("bid_qty", 0)),
                "ask_qty": int(q.get("ask_qty", 0)),
                "lot_size": lot_size,
            }

        if "ITM1" in resolved and "ITM" not in resolved:
            resolved["ITM"] = resolved["ITM1"]
        if "OTM1" in resolved and "OTM" not in resolved:
            resolved["OTM"] = resolved["OTM1"]

        return resolved

    def process_alert(self, alert: SetupAlert) -> Optional[ActionableOpportunity]:
        """
        Evaluate SetupAlert against strike liquidity gates, per-symbol daily cap, and emit ActionableOpportunity.
        """
        now_ts = float(alert.timestamp or time.time())
        date_str = datetime.fromtimestamp(now_ts, tz=IST).strftime("%Y-%m-%d")
        cap_key = (date_str, alert.symbol.upper())
        if (
            self.max_alerts_per_symbol_per_day is not None
            and self._daily_symbol_counts.get(cap_key, 0) >= self.max_alerts_per_symbol_per_day
        ):
            _logger.warning(
                f"Alert {alert.alert_id} capped: {alert.symbol} reached "
                f"{self.max_alerts_per_symbol_per_day}/day limit"
            )
            return None

        # Resolve strikes with spread & ATR/spread edge gates
        strikes = self.resolve_strikes(
            alert.symbol,
            alert.direction,
            alert.trigger_price,
            atr=float(getattr(alert, "atr", 0.0) or 0.0),
        )
        if not strikes:
            _logger.warning(
                f"Alert {alert.alert_id} rejected: no liquid strikes for "
                f"{alert.symbol} {alert.direction} @ {alert.trigger_price:.2f}"
            )
            return None

        self._daily_symbol_counts[cap_key] = self._daily_symbol_counts.get(cap_key, 0) + 1

        strat_id = str(getattr(alert, "strategy_id", None) or "momentum")
        strat_tags = dict(getattr(alert, "strategy_tags", None) or {})
        opp = ActionableOpportunity(
            opp_id=f"OPP_{alert.symbol}_{int(time.time()*1000)}",
            alert_id=alert.alert_id,
            symbol=alert.symbol,
            direction=alert.direction,
            archetype=alert.archetype,
            trigger_price=alert.trigger_price,
            strikes=strikes,
            timestamp=time.time(),
            flag_high=alert.flag_high,
            flag_low=alert.flag_low,
            pole_high=getattr(alert, "pole_high", None),
            pole_low=getattr(alert, "pole_low", None),
            atr=alert.atr,
            rvol=alert.rvol,
            clv=alert.clv,
            strategy_id=strat_id,
            strategy_tags=strat_tags,
        )

        topic = f"OPP.{strat_id}.{alert.symbol}".encode("utf-8")
        payload = pack_message(opp)
        self.pub.send_multipart([topic, payload], flags=zmq.NOBLOCK)
        _logger.info(
            f"Published {opp.opp_id} ({alert.symbol} {alert.direction} @ {alert.trigger_price:.2f}) "
            f"-> strikes={list(strikes.keys())}"
        )
        return opp

    def start(self) -> None:
        """Run RMS subscriber loop."""
        self.running = True
        _logger.info(f"Active | SUB: {self.alerts_ipc} | PUB: {self.rms_pub_ipc}")

        poller = zmq.Poller()
        poller.register(self.sub, zmq.POLLIN)

        while self.running:
            try:
                events = dict(poller.poll(timeout=100))
                if self.sub in events:
                    topic, payload = self.sub.recv_multipart()
                    alert_dict = unpack_message(payload)
                    alert = SetupAlert(**alert_dict)
                    self.process_alert(alert)
            except zmq.ZMQError:
                break
            except Exception as e:
                _logger.error(f"RMSService loop error: {e}")

    def stop(self) -> None:
        self.running = False
        try:
            self.sub.close(linger=0)
            self.pub.close(linger=0)
            self.quotes_client.close()
            if self._owns_ctx:
                self.ctx.destroy(linger=0)
        except Exception:
            pass
        _logger.info("RMSService stopped cleanly.")


def main():
    parser = argparse.ArgumentParser(description="Risk Management Service (RMS)")
    parser.add_argument("--alerts-ipc", default=DEFAULT_ALERTS_SUB_IPC, help="Scoring alerts SUB endpoint")
    parser.add_argument("--rms-pub-ipc", default=DEFAULT_RMS_PUB_IPC, help="RMS opportunities PUB endpoint")
    parser.add_argument("--quotes-router-ipc", default=DEFAULT_QUOTES_ROUTER_IPC, help="Quotes ROUTER endpoint")
    parser.add_argument("--universe-csv", default=None, help="Path to filtered universe CSV")
    args = parser.parse_args()

    rms = RMSService(
        alerts_ipc=args.alerts_ipc,
        rms_pub_ipc=args.rms_pub_ipc,
        quotes_router_ipc=args.quotes_router_ipc,
        universe_csv=args.universe_csv,
    )

    def sig_handler(sig, frame):
        rms.stop()
        sys.exit(0)

    signal.signal(signal.SIGINT, sig_handler)
    signal.signal(signal.SIGTERM, sig_handler)

    rms.start()


if __name__ == "__main__":
    main()

