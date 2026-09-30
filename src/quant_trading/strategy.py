"""Causal moving-average signals shared by backtests and paper execution."""

from __future__ import annotations

from collections import deque

import pandas as pd

from .config import StrategyConfig


def generate_signals(bars: pd.DataFrame, cfg: StrategyConfig) -> pd.DataFrame:
    """Return close-time regimes; a regime can only trade at a later open.

    Raw closing prices are kept in the current share unit. At each split's
    effective session, historical closes in the indicator window are divided
    by its ratio. Previously published signal rows are never recalculated.
    Cash dividends are deliberately not subtracted from indicator prices.
    """
    if not 1 <= cfg.fast_window < cfg.slow_window:
        raise ValueError("Moving-average windows must satisfy 1 <= fast < slow.")
    history: deque[float] = deque(maxlen=cfg.slow_window)
    rows = []
    for _, bar in bars.iterrows():
        ratio = float(bar.get("split_ratio", 1.0))
        if ratio <= 0:
            raise ValueError("split_ratio must be positive.")
        if ratio != 1.0:
            history = deque((price / ratio for price in history), maxlen=cfg.slow_window)
        history.append(float(bar["close"]))
        prices = list(history)
        fast = sum(prices[-cfg.fast_window:]) / cfg.fast_window if len(prices) >= cfg.fast_window else float("nan")
        slow = sum(prices) / cfg.slow_window if len(prices) == cfg.slow_window else float("nan")
        ready = len(prices) == cfg.slow_window
        rows.append({"fast_ma": fast, "slow_ma": slow, "ready": ready, "target": int(ready and fast > slow)})
    result = pd.DataFrame(rows, index=bars.index, columns=["fast_ma", "slow_ma", "ready", "target"])
    result.index.name = bars.index.name
    return result
