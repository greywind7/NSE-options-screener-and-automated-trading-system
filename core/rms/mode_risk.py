"""
Mode Risk Manager: Mode-Specific Capital and Risk Coordinator.
Tracks mode-specific realized PnL, available capital, committed capital,
and daily loss circuit breakers for Paper, Paper Delayed, Live, and Sim modes.
"""
import sys
from typing import Any, Dict, List, Optional


class ModeRiskManager:
    """
    Independent Risk Management module imported by execution superstructures and simulation.
    Ensures PnL tracking and circuit breaker triggers remain strictly segregated per mode.
    """
    def __init__(
        self,
        mode: str,
        starting_capital: float = 250000.0,
        daily_loss_limit_inr: float = 10000.0,
        max_capital_per_trade_pct: float = 0.50,
        enable_circuit_breaker: bool = True,
    ):
        self.mode = mode.lower()
        self.starting_capital = float(starting_capital)
        self.daily_loss_limit_inr = float(daily_loss_limit_inr)
        self.max_capital_per_trade_pct = float(max_capital_per_trade_pct)
        self.enable_circuit_breaker = bool(enable_circuit_breaker)

        self.daily_pnl = 0.0
        self.committed_capital = 0.0
        self.frozen = False
        self.circuit_breaker_tripped = False

    @property
    def realized_pnl(self) -> float:
        return self.daily_pnl

    @property
    def available_capital(self) -> float:
        """Available uncommitted cash for new positions."""
        net_worth = self.starting_capital + self.daily_pnl
        return max(0.0, net_worth - self.committed_capital)

    def is_circuit_breaker_tripped(self) -> bool:
        if self.frozen:
            return True
        if not self.enable_circuit_breaker:
            return False
        return self.circuit_breaker_tripped or (self.daily_pnl <= -self.daily_loss_limit_inr)

    def can_allocate(self, required_capital: float) -> bool:
        """Check whether capital is available for an entry order."""
        if self.is_circuit_breaker_tripped():
            return False
        if required_capital <= 0.0:
            return False
        if required_capital > self.available_capital:
            return False
        if required_capital > (self.starting_capital * self.max_capital_per_trade_pct):
            return False
        return True

    def commit_capital(self, amount: float) -> bool:
        """Reserve capital for an active trade."""
        if not self.can_allocate(amount):
            return False
        self.committed_capital += amount
        return True

    def release_capital(self, committed_amount: float, realized_pnl: float = 0.0) -> None:
        """Release committed capital upon trade exit and record realized PnL."""
        self.committed_capital = max(0.0, self.committed_capital - committed_amount)
        self.update_pnl(realized_pnl)

    def update_pnl(self, realized_pnl_delta: float) -> None:
        """Update cumulative realized daily PnL and evaluate circuit breaker if enabled."""
        self.daily_pnl += realized_pnl_delta
        if self.enable_circuit_breaker and self.daily_pnl <= -self.daily_loss_limit_inr:
            self.circuit_breaker_tripped = True
            self.frozen = True
            print(
                f"[{self.mode.upper()}_RMS] Daily loss circuit breaker tripped! "
                f"PnL: {self.daily_pnl:.2f} <= -{self.daily_loss_limit_inr:.2f}. Mode frozen.",
                file=sys.stderr,
            )
