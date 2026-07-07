"""
Risk management for Kehlo Trading: position sizing + multi-level TP/SL plan.

Nothing here talks to the exchange — it only calculates numbers. bot.py
(next phase) will take a TradePlan and use BitgetClient to actually place
the entry order plus one place_tpsl_leg() call per TP level and one for
the SL.
"""

from dataclasses import dataclass, field
from typing import List


@dataclass
class TPLevel:
    price: float
    close_fraction: float  # e.g. 0.4 = close 40% of the position at this level
    move_sl_to_breakeven: bool = False
    reason: str = ""  # what this level targets (from SMCEngine's tp_reasons) — "" if none given


@dataclass
class TradePlan:
    direction: str  # 'long' or 'short'
    entry: float
    stop_loss: float
    position_size: float  # base-currency units, e.g. BTC amount
    tp_levels: List[TPLevel] = field(default_factory=list)

    def risk_amount(self) -> float:
        return abs(self.entry - self.stop_loss) * self.position_size


class RiskManager:
    def __init__(self, risk_per_trade_pct: float = 1.0, tp_split=(0.4, 0.35, 0.25),
                 max_concurrent_positions: int = 3, max_daily_loss_pct: float = 5.0):
        """
        risk_per_trade_pct : % of account equity risked per trade (SL distance)
        tp_split           : fraction of the position closed at TP1/TP2/TP3 —
                             must sum to 1.0
        max_concurrent_positions : hard cap on simultaneously open positions
        max_daily_loss_pct : circuit breaker — once today's realised loss
                             passes this % of equity, stop opening new trades
        """
        if abs(sum(tp_split) - 1.0) > 1e-6:
            raise ValueError("tp_split must sum to 1.0")
        self.risk_per_trade_pct = risk_per_trade_pct
        self.tp_split = tp_split
        self.max_concurrent_positions = max_concurrent_positions
        self.max_daily_loss_pct = max_daily_loss_pct

    def position_size(self, account_equity: float, entry: float, stop_loss: float) -> float:
        """
        size = (equity * risk_per_trade_pct / 100) / |entry - stop_loss|
        Sizing is driven by SL distance, not leverage — leverage changes how
        much MARGIN that size requires, not how much you're risking.
        """
        risk_amount = account_equity * (self.risk_per_trade_pct / 100)
        stop_distance = abs(entry - stop_loss)
        if stop_distance <= 0:
            raise ValueError("stop_loss cannot equal entry")
        return risk_amount / stop_distance

    def build_trade_plan(self, direction: str, entry: float, stop_loss: float,
                          take_profits: List[float], account_equity: float,
                          tp_reasons: List[str] = None) -> TradePlan:
        size = self.position_size(account_equity, entry, stop_loss)
        reasons = tp_reasons if tp_reasons else [""] * len(take_profits)
        tp_levels = [
            TPLevel(price=price, close_fraction=fraction, move_sl_to_breakeven=(i == 0), reason=reason)
            for i, (price, fraction, reason) in enumerate(zip(take_profits, self.tp_split, reasons))
        ]
        return TradePlan(direction=direction, entry=entry, stop_loss=stop_loss,
                          position_size=size, tp_levels=tp_levels)

    def can_open_new_position(self, open_position_count: int, today_realised_pnl_pct: float) -> bool:
        if open_position_count >= self.max_concurrent_positions:
            return False
        if today_realised_pnl_pct <= -abs(self.max_daily_loss_pct):
            return False
        return True
