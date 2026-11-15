"""
Opportunity Pool & Candidate Quarantine Gatekeeper.
Manages candidate lifecycle, non-blocking cooldown quarantine (5.0s per symbol, Gap 2),
and dispatches SetupAlerts to RMS over ZeroMQ PUB.
"""
import argparse
import signal
import sys
import time
from typing import Dict, List, Optional
import zmq

from core.common.ipc_schemas import SetupAlert, pack_message

DEFAULT_ALERTS_PUB_IPC = "ipc:///tmp/scoring_alerts_pub.ipc"
DEFAULT_COOLDOWN_SEC = 5.0


class OpportunityPool:
    def __init__(
        self,
        pub_ipc: Optional[str] = None,
        cooldown_sec: Optional[float] = None,
        cooldown_seconds: Optional[float] = None,
        ctx: Optional[zmq.Context] = None,
    ):
        from core.config.config_loader import get_config_manager
        cfg = get_config_manager()
        ipc = cfg.get_ipc_endpoints()
        scoring_cfg = cfg.get_scoring_config()

        self.pub_ipc = pub_ipc or ipc.get("scoring_alerts_ipc", DEFAULT_ALERTS_PUB_IPC)
        effective_cd = cooldown_sec if cooldown_sec is not None else cooldown_seconds
        self.cooldown_sec = float(effective_cd if effective_cd is not None else scoring_cfg.get("quarantine_cooldown_sec", DEFAULT_COOLDOWN_SEC))

        # Symbol cooldown quarantine: symbol -> last_alert_timestamp
        self._cooldown_map: Dict[str, float] = {}

        self._owns_ctx = ctx is None
        self.ctx = ctx or zmq.Context()
        self.pub = self.ctx.socket(zmq.PUB)
        self.pub.setsockopt(zmq.LINGER, 0)
        self.pub.setsockopt(zmq.SNDHWM, 100000)
        self.pub.bind(self.pub_ipc)

    def submit_alert(self, alert: SetupAlert) -> bool:
        """
        Validate alert against non-blocking quarantine cooldown.
        Dispatches to ZeroMQ PUB if approved. Returns True if published, False if quarantined.
        """
        now = alert.timestamp or time.time()
        last_ts = self._cooldown_map.get(alert.symbol, 0.0)

        # Gap 2: Non-blocking 5.0s quarantine per symbol
        if (now - last_ts) < self.cooldown_sec:
            return False

        self._cooldown_map[alert.symbol] = now

        topic = b"ALERT." + alert.symbol.encode("utf-8")
        payload = pack_message(alert)
        self.pub.send_multipart([topic, payload], flags=zmq.NOBLOCK)
        return True

    def is_quarantined(self, symbol: str, now: Optional[float] = None) -> bool:
        """Check if symbol is currently in cooldown quarantine."""
        now = now or time.time()
        last_ts = self._cooldown_map.get(symbol, 0.0)
        return (now - last_ts) < self.cooldown_sec

    def reset_cooldown(self, symbol: Optional[str] = None) -> None:
        """Reset quarantine for one or all symbols."""
        if symbol:
            self._cooldown_map.pop(symbol, None)
        else:
            self._cooldown_map.clear()

    def stop(self) -> None:
        try:
            self.pub.close(linger=0)
            if self._owns_ctx:
                self.ctx.destroy(linger=0)
        except Exception:
            pass
        print("[OPPORTUNITY_POOL] Stopped cleanly.")

