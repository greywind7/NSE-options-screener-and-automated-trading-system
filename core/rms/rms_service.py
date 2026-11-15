"""
Strike Selection Service & Option Liquidity Gate (RMS Tier).
Subscribes to scoring alerts, resolves ATM/ITM1/OTM1 option contracts,
enforces hybrid tick spread gate (Gap 1 / HIGH-004), and publishes ActionableOpportunities
over ZeroMQ PUB for mode-specific execution superstructures.
"""
import argparse
import csv
import os
import signal
import sys
import time
from typing import Any, Dict, List, Optional
import zmq

from core.common.ipc_schemas import ActionableOpportunity, SetupAlert, pack_message, unpack_message
from core.common.symbol_utils import normalize_instrument_token
from core.quotes.quotes_broker import QuotesBrokerClient, DEFAULT_QUOTES_ROUTER_IPC

DEFAULT_ALERTS_SUB_IPC = "ipc:///tmp/scoring_alerts_pub.ipc"
DEFAULT_RMS_PUB_IPC = "ipc:///tmp/rms_strike_pub.ipc"


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
        inst_paths = cfg.instruments.get("instruments", {}).get("paths", {})

        self.alerts_ipc = alerts_ipc or ipc.get("scoring_alerts_ipc", DEFAULT_ALERTS_SUB_IPC)
        self.rms_pub_ipc = rms_pub_ipc or ipc.get("rms_strike_ipc", DEFAULT_RMS_PUB_IPC)
        self.quotes_router_ipc = quotes_router_ipc or ipc.get("quotes_router_ipc", DEFAULT_QUOTES_ROUTER_IPC)
        self.universe_csv = universe_csv or inst_paths.get("filtered_universe_csv", "data/filtered.csv")
        self.spread_gate = risk_cfg.get("spread_gate", {"max_spread_pct": 0.03, "min_spread_abs": 0.50})
        self.running = False

        # Load lot sizes and metadata from universe
        self.lot_sizes: Dict[str, int] = {}
        self.tick_sizes: Dict[str, float] = {}
        self._load_universe()

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

    def resolve_strikes(self, symbol: str, direction: str, trigger_price: float) -> Dict[str, Any]:
        """
        Compute ATM, ITM1, OTM1 strikes and query QuotesBroker for depth.
        Enforces hybrid tick spread gate (Gap 1 / HIGH-004): (ask - bid) <= max(0.50, 0.03 * ask).
        """
        opt_type = "CE" if direction.upper() == "BULL" else "PE"
        step = get_strike_step(trigger_price)
        atm_strike = round(trigger_price / step) * step

        if opt_type == "CE":
            itm_strike = atm_strike - step
            otm_strike = atm_strike + step
        else:
            itm_strike = atm_strike + step
            otm_strike = atm_strike - step

        lot_size = self.lot_sizes.get(symbol.upper(), 1)

        # Generate tokens
        expiry_tag = "26NOV"
        tokens = {
            "ATM": (atm_strike, f"NSE:{symbol.upper()}{expiry_tag}{int(atm_strike)}{opt_type}"),
            "ITM1": (itm_strike, f"NSE:{symbol.upper()}{expiry_tag}{int(itm_strike)}{opt_type}"),
            "OTM1": (otm_strike, f"NSE:{symbol.upper()}{expiry_tag}{int(otm_strike)}{opt_type}"),
        }

        # Query QuotesBroker
        raw_quotes = self.quotes_client.get_quotes([t[1] for t in tokens.values()])

        resolved: Dict[str, Any] = {}
        for role, (strike_val, token_str) in tokens.items():
            norm_token = normalize_instrument_token(token_str)
            q = raw_quotes.get(norm_token)

            # If no live quote in LVC (e.g. simulated or cold), synthesize a nominal quote
            if not q:
                nominal_ltp = max(5.0, abs(trigger_price - strike_val) * 0.5)
                q = {
                    "token": norm_token,
                    "symbol": symbol,
                    "ltp": nominal_ltp,
                    "bid": nominal_ltp - 0.10,
                    "ask": nominal_ltp + 0.10,
                    "bid_qty": 500,
                    "ask_qty": 500,
                    "timestamp": time.time(),
                }

            ask = float(q.get("ask", 0.0))
            bid = float(q.get("bid", 0.0))
            spread = ask - bid

            # Gap 1 / HIGH-004: Hybrid spread gate prevents ₹3.00 OTM starvation
            min_spread_abs = float(self.spread_gate.get("min_spread_abs", 0.50))
            max_spread_pct = float(self.spread_gate.get("max_spread_pct", 0.03))
            max_allowed_spread = max(min_spread_abs, max_spread_pct * ask)
            if spread > max_allowed_spread:
                # Spread is too wide; skip this illiquid strike
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

        return resolved

    def process_alert(self, alert: SetupAlert) -> Optional[ActionableOpportunity]:
        """
        Evaluate SetupAlert against strike liquidity gates and construct ActionableOpportunity.
        """
        # Resolve strikes with hybrid spread gate
        strikes = self.resolve_strikes(alert.symbol, alert.direction, alert.trigger_price)
        if not strikes:
            return None

        opp = ActionableOpportunity(
            opp_id=f"OPP_{alert.symbol}_{int(time.time()*1000)}",
            alert_id=alert.alert_id,
            symbol=alert.symbol,
            direction=alert.direction,
            archetype=alert.archetype,
            trigger_price=alert.trigger_price,
            strikes=strikes,
            timestamp=time.time(),
        )

        topic = b"OPP." + alert.symbol.encode("utf-8")
        payload = pack_message(opp)
        self.pub.send_multipart([topic, payload], flags=zmq.NOBLOCK)
        return opp

    def start(self) -> None:
        """Run RMS subscriber loop."""
        self.running = True
        print(f"[RMS_SERVICE] Active | SUB: {self.alerts_ipc} | PUB: {self.rms_pub_ipc}")

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
                print(f"[RMS_SERVICE] Error: {e}", file=sys.stderr)

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
        print("[RMS_SERVICE] Stopped cleanly.")


def main():
    parser = argparse.ArgumentParser(description="Risk Management Service (RMS)")
    parser.add_argument("--alerts-ipc", default=DEFAULT_ALERTS_SUB_IPC, help="Scoring alerts SUB endpoint")
    parser.add_argument("--rms-pub-ipc", default=DEFAULT_RMS_PUB_IPC, help="RMS opportunities PUB endpoint")
    parser.add_argument("--quotes-router-ipc", default=DEFAULT_QUOTES_ROUTER_IPC, help="Quotes ROUTER endpoint")
    parser.add_argument("--max-daily-loss", type=float, default=DEFAULT_MAX_DAILY_LOSS, help="Daily loss limit")
    args = parser.parse_args()

    rms = RMSService(
        alerts_ipc=args.alerts_ipc,
        rms_pub_ipc=args.rms_pub_ipc,
        quotes_router_ipc=args.quotes_router_ipc,
        max_daily_loss=args.max_daily_loss,
    )

    def sig_handler(sig, frame):
        rms.stop()
        sys.exit(0)

    signal.signal(signal.SIGINT, sig_handler)
    signal.signal(signal.SIGTERM, sig_handler)

    rms.start()


if __name__ == "__main__":
    main()

