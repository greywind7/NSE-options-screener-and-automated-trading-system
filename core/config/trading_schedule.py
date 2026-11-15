"""
Canonical Trading Schedule and Market Cutoff Times.
Single source of truth for exchange trading hours, entry freezes, and EOD auto-squareoff buffers.
Resolves Gap 4 (14 conflicting market hours and EOD definitions) and CRIT-006 (MCX vs NSE hours).
"""
from dataclasses import dataclass
from datetime import datetime, time
from typing import Any, Optional, Union


def normalize_time_str(val: Any, default: str = "15:20") -> str:
    """Standardize time input to 'HH:MM' string format."""
    if not val:
        return default
    if isinstance(val, (datetime,)):
        return val.strftime("%H:%M")
    if isinstance(val, (time,)):
        return val.strftime("%H:%M")
    s = str(val).strip()
    if ":" in s:
        parts = s.split(":")
        try:
            h = int(parts[0])
            m = int(parts[1])
            return f"{h:02d}:{m:02d}"
        except ValueError:
            return default
    return default


@dataclass(frozen=True)
class MarketSchedule:
    exchange: str
    market_open_time: str
    market_close_time: str
    midday_freeze_start: str
    midday_freeze_end: str
    live_entry_freeze_time: str
    live_eod_squareoff_time: str
    paper_eod_squareoff_time: str

    def _to_hm(self, t: Union[str, datetime, time, float]) -> str:
        if isinstance(t, (int, float)):
            dt = datetime.fromtimestamp(t)
            return dt.strftime("%H:%M")
        return normalize_time_str(t, default=self.market_open_time)

    def is_market_open(self, cur_time: Union[str, datetime, time, float]) -> bool:
        """Check if time is within continuous market session."""
        hm = self._to_hm(cur_time)
        return self.market_open_time <= hm < self.market_close_time

    def is_midday_lull(self, cur_time: Union[str, datetime, time, float]) -> bool:
        """Check if time falls in midday chop/freeze window."""
        hm = self._to_hm(cur_time)
        return self.midday_freeze_start <= hm < self.midday_freeze_end

    def is_live_entry_frozen(self, cur_time: Union[str, datetime, time, float]) -> bool:
        """Check if new live entries should be blocked due to EOD cutoff."""
        hm = self._to_hm(cur_time)
        return hm >= self.live_entry_freeze_time or hm < self.market_open_time

    def is_live_squareoff_time(self, cur_time: Union[str, datetime, time, float]) -> bool:
        """Check if live positions must be forcibly squared off to prevent broker RMS penalty."""
        hm = self._to_hm(cur_time)
        return hm >= self.live_eod_squareoff_time

    def is_paper_squareoff_time(self, cur_time: Union[str, datetime, time, float]) -> bool:
        """Check if paper/sim positions must be squared off."""
        hm = self._to_hm(cur_time)
        return hm >= self.paper_eod_squareoff_time


NSE_SCHEDULE = MarketSchedule(
    exchange="NSE",
    market_open_time="09:15",
    market_close_time="15:30",
    midday_freeze_start="11:00",
    midday_freeze_end="13:30",
    live_entry_freeze_time="15:10",
    live_eod_squareoff_time="15:15",
    paper_eod_squareoff_time="15:20",
)

MCX_SCHEDULE = MarketSchedule(
    exchange="MCX",
    market_open_time="09:00",
    market_close_time="23:30",
    midday_freeze_start="13:30",
    midday_freeze_end="14:30",
    live_entry_freeze_time="23:10",
    live_eod_squareoff_time="23:15",
    paper_eod_squareoff_time="23:20",
)


def get_trading_schedule(exchange: str = "NSE") -> MarketSchedule:
    """Retrieve canonical schedule for target exchange, with overrides from config if present."""
    ex = exchange.upper().strip() if exchange else "NSE"
    if ex == "MCX":
        return MCX_SCHEDULE

    try:
        from core.config.config_loader import get_config_manager
        cfg = get_config_manager()
        exec_cfg = cfg.execution.get("execution", {})
        live_cfg = exec_cfg.get("live", {})
        if exec_cfg:
            return MarketSchedule(
                exchange="NSE",
                market_open_time=exec_cfg.get("market_open_time", "09:15"),
                market_close_time=exec_cfg.get("market_close_time", "15:30"),
                midday_freeze_start=exec_cfg.get("midday_freeze_start", "11:00"),
                midday_freeze_end=exec_cfg.get("midday_freeze_end", "13:30"),
                live_entry_freeze_time=live_cfg.get("live_entry_freeze_time") or exec_cfg.get("live_entry_freeze_time", "15:10"),
                live_eod_squareoff_time=live_cfg.get("live_eod_squareoff_time") or exec_cfg.get("live_eod_squareoff_time", "15:15"),
                paper_eod_squareoff_time=exec_cfg.get("paper_eod_squareoff_time", "15:20"),
            )
    except Exception:
        pass

    return NSE_SCHEDULE

