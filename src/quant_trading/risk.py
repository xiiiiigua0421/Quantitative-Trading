"""Whole-share sizing with explicit all-in cost and cash limits."""

from __future__ import annotations

import math

from .config import CostConfig, RiskConfig


def commission_for(notional: float, costs: CostConfig) -> float:
    """Charge commission once per nonzero fill, in dollars."""
    return max(costs.min_commission, abs(notional) * costs.commission_bps / 10_000) if notional else 0.0


def execution_price(raw_price: float, action: str, costs: CostConfig) -> float:
    """Apply adverse slippage to a reference price; BUY pays more, SELL less."""
    if action not in {"BUY", "SELL"}:
        raise ValueError("action must be BUY or SELL.")
    return raw_price * (1 + (1 if action == "BUY" else -1) * costs.slippage_bps / 10_000)


def affordable_shares(budget: float, price: float, costs: CostConfig) -> int:
    """Maximum whole shares whose slipped notional plus commission fits budget.

    ``price`` is already the execution price. This helper is also used by the
    unlevered buy-and-hold benchmark, which has no strategy allocation cap.
    """
    if not math.isfinite(budget) or not math.isfinite(price) or budget <= 0 or price <= 0:
        return 0
    rate = costs.commission_bps / 10_000
    shares = max(0, min(math.floor(budget / (price * (1 + rate))), math.floor((budget - costs.min_commission) / price)))
    # Guard against a floating-point boundary giving one unaffordable share.
    while shares and shares * price + commission_for(shares * price, costs) > budget + 1e-9:
        shares -= 1
    return shares


def size_buy(
    cash: float,
    equity: float,
    price: float,
    costs: CostConfig,
    risk: RiskConfig,
    current_shares: float = 0.0,
    pending_buy_notional: float = 0.0,
) -> int:
    """Size a BUY, including slippage and fees in every dollar budget.

    ``price`` is an unslipped price. Existing holdings are marked at that
    price; ``pending_buy_notional`` is a conservative all-in reservation.
    Cash buffer is a fraction of current equity. Returns zero when no whole
    share is affordable. A strategy must decide whether to rebalance; this
    function only enforces limits on the proposed additional exposure.
    """
    inputs = (cash, equity, price, current_shares, pending_buy_notional)
    if any(not math.isfinite(value) for value in inputs) or price <= 0 or equity <= 0:
        return 0
    if current_shares < 0 or pending_buy_notional < 0:
        raise ValueError("Holdings and pending BUY reservation must be nonnegative.")
    existing = current_shares * price + pending_buy_notional
    budget = min(
        equity * risk.target_fraction - existing,
        equity * risk.max_position_fraction - existing,
        risk.max_order_notional,
        cash - equity * risk.cash_buffer_fraction - pending_buy_notional,
    )
    return affordable_shares(budget, execution_price(price, "BUY", costs), costs)
