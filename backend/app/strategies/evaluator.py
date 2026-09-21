from dataclasses import dataclass

from ..models import OrderSide


@dataclass(frozen=True)
class CrossDecision:
    action: OrderSide | None
    fast_average: float
    slow_average: float
    previous_fast_average: float
    previous_slow_average: float


def evaluate_sma_cross(closes: list[float], fast_period: int, slow_period: int) -> CrossDecision | None:
    """Evaluate a close-only SMA crossover without look-ahead or partial candles."""
    if fast_period < 2 or slow_period <= fast_period or len(closes) < slow_period + 1:
        return None
    previous = closes[:-1]
    previous_fast = sum(previous[-fast_period:]) / fast_period
    previous_slow = sum(previous[-slow_period:]) / slow_period
    current_fast = sum(closes[-fast_period:]) / fast_period
    current_slow = sum(closes[-slow_period:]) / slow_period
    action = None
    if previous_fast <= previous_slow and current_fast > current_slow:
        action = OrderSide.BUY
    elif previous_fast >= previous_slow and current_fast < current_slow:
        action = OrderSide.SELL
    return CrossDecision(action, current_fast, current_slow, previous_fast, previous_slow)
