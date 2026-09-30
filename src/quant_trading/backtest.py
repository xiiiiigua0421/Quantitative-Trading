"""Deterministic daily event backtest using raw OHLC and corporate actions."""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field

import pandas as pd

from .config import AppConfig
from .risk import affordable_shares, commission_for, execution_price, size_buy
from .strategy import generate_signals


@dataclass
class BacktestResult:
    equity: pd.DataFrame
    trades: pd.DataFrame
    signals: pd.DataFrame
    metrics: dict
    warnings: list[str]


@dataclass
class _Receivable:
    amount: float
    pay_date: pd.Timestamp | None


@dataclass
class _Account:
    cash: float
    shares: float = 0.0
    receivables: list[_Receivable] = field(default_factory=list)
    ledger: list[dict] | None = None

    @property
    def dividend_receivable(self) -> float:
        return sum(item.amount for item in self.receivables)

    def equity_at(self, price: float) -> float:
        return self.cash + self.shares * price + self.dividend_receivable

    def record(self, date, action, shares=0.0, price=0.0, commission=0.0, notional=0.0, reason="", signal_date=None):
        if self.ledger is not None:
            self.ledger.append({
                "date": date, "signal_date": signal_date, "action": action,
                "shares": shares, "price": price, "commission": commission,
                "notional": notional, "cash_after": self.cash, "reason": reason,
            })

    def corporate_actions(self, date: pd.Timestamp, bar: pd.Series, warnings: list[str] | None):
        ratio = float(bar.get("split_ratio", 1.0))
        if ratio != 1.0 and self.shares:
            old_shares = self.shares
            self.shares *= ratio
            self.record(date, "SPLIT", shares=self.shares - old_shares, reason=f"ratio={ratio:g}; holding={self.shares:g}")
            if warnings is not None and not math.isclose(self.shares, round(self.shares), abs_tol=1e-9):
                warnings.append(f"{date.date()}: split produced fractional shares ({self.shares:g}); retained as an entitlement, without estimating cash-in-lieu.")
        dividend = float(bar.get("dividend", 0.0))
        # Entitlement belongs to the holding before ex-date opening orders.
        if dividend and self.shares:
            raw_pay_date = bar.get("dividend_pay_date", "")
            pay_date = pd.Timestamp(raw_pay_date).normalize() if pd.notna(raw_pay_date) and str(raw_pay_date).strip() else None
            amount = self.shares * dividend
            self.receivables.append(_Receivable(amount, pay_date))
            self.record(date, "DIVIDEND", self.shares, dividend, notional=amount, reason="Ex-date entitlement; recognized as a receivable before open.")
            if pay_date is None and warnings is not None:
                warnings.append(f"{date.date()}: dividend payment date missing; ${amount:.2f} remains receivable.")
        outstanding = []
        for receivable in self.receivables:
            if receivable.pay_date is not None and receivable.pay_date <= date:
                self.cash += receivable.amount
                self.record(date, "DIVIDEND_PAYMENT", notional=receivable.amount, reason=f"Payment due {receivable.pay_date.date()}; credited on first available session.")
            else:
                outstanding.append(receivable)
        self.receivables = outstanding

    def buy(self, date, qty: int, raw_price: float, config: AppConfig, signal_date=None, reason="MA regime entry"):
        if qty <= 0:
            return
        price = execution_price(raw_price, "BUY", config.costs)
        notional = qty * price
        commission = commission_for(notional, config.costs)
        self.cash -= notional + commission
        self.shares += qty
        self.record(date, "BUY", qty, price, commission, notional, reason, signal_date)

    def sell(self, date, raw_price: float, config: AppConfig, signal_date=None, reason="MA regime exit"):
        if self.shares <= 0:
            return
        price = execution_price(raw_price, "SELL", config.costs)
        qty = self.shares
        notional = qty * price
        commission = commission_for(notional, config.costs)
        self.cash += notional - commission
        self.shares = 0.0
        self.record(date, "SELL", qty, price, commission, notional, reason, signal_date)


def _validate_bars(bars: pd.DataFrame) -> None:
    if bars.empty:
        raise ValueError("Backtest requires at least one session.")
    if not isinstance(bars.index, pd.DatetimeIndex) or bars.index.has_duplicates or not bars.index.is_monotonic_increasing:
        raise ValueError("Bars require unique, increasing DatetimeIndex sessions.")
    if bars.index.tz is not None or not bars.index.equals(bars.index.normalize()):
        raise ValueError("Bars require naive normalized US session dates.")
    for column in ("open", "close"):
        if column not in bars or not bars[column].map(lambda value: math.isfinite(float(value)) and float(value) > 0).all():
            raise ValueError(f"{column} must contain finite positive raw prices.")
    if "volume" not in bars or not bars["volume"].map(lambda value: math.isfinite(float(value)) and float(value) >= 0).all():
        raise ValueError("volume must contain finite nonnegative values.")
    if "split_ratio" in bars and not bars["split_ratio"].map(lambda value: math.isfinite(float(value)) and float(value) > 0).all():
        raise ValueError("split_ratio must contain finite positive ratios.")
    if "dividend" in bars and not bars["dividend"].map(lambda value: math.isfinite(float(value)) and float(value) >= 0).all():
        raise ValueError("dividend must contain finite nonnegative amounts.")
    if "dividend_pay_date" in bars:
        for session, raw_date in bars["dividend_pay_date"].items():
            if pd.isna(raw_date) or not str(raw_date).strip():
                continue
            if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", str(raw_date)):
                raise ValueError("dividend_pay_date must be YYYY-MM-DD or blank.")
            pay_date = pd.Timestamp(raw_date)
            if pay_date < session:
                raise ValueError("Dividend payment date cannot precede its ex-date.")


def run_backtest(bars: pd.DataFrame, config: AppConfig) -> BacktestResult:
    """Execute each ready close-time regime at the following dataset open.

    Flat-to-long entries are sized once; existing positions are never topped
    up or rebalanced each day. A maximum-drawdown breach at close permanently
    halts entry and liquidates at the next available open. There is no forced
    terminal liquidation. The benchmark invests all affordable initial cash
    at the first nonzero-volume open after indicator warmup and uses identical costs and
    corporate actions, but no strategy allocation cap or drawdown halt.
    """
    config.validate()
    _validate_bars(bars)
    signals = generate_signals(bars, config.strategy)
    warnings: list[str] = list(bars.attrs.get("warnings", []))
    ledger: list[dict] = []
    account = _Account(config.initial_cash, ledger=ledger)
    benchmark = _Account(config.initial_cash)
    peak = config.initial_cash
    halted = False
    benchmark_started = False
    first_tradable: int | None = None
    rows = []
    for index, (date, bar) in enumerate(bars.iterrows()):
        account.corporate_actions(date, bar, warnings)
        benchmark.corporate_actions(date, bar, None)
        raw_open = float(bar["open"])
        tradable = float(bar["volume"]) > 0
        if not tradable:
            warnings.append(f"{date.date()}: zero-volume session; no fill is assumed. Next tradable open uses the latest prior-close target.")
        if index and bool(signals.iloc[index - 1]["ready"]) and tradable:
            if first_tradable is None:
                first_tradable = index
            previous_date = bars.index[index - 1]
            target = int(signals.iloc[index - 1]["target"])
            if halted or target == 0:
                account.sell(date, raw_open, config, previous_date, "Drawdown halt liquidation" if halted else "MA regime exit")
            elif account.shares == 0:
                quantity = size_buy(account.cash, account.equity_at(raw_open), raw_open, config.costs, config.risk)
                account.buy(date, quantity, raw_open, config, previous_date)
            if not benchmark_started:
                quantity = affordable_shares(benchmark.cash, execution_price(raw_open, "BUY", config.costs), config.costs)
                benchmark.buy(date, quantity, raw_open, config, reason="Buy-and-hold baseline after warmup")
                benchmark_started = True
        close = float(bar["close"])
        equity = account.equity_at(close)
        peak = max(peak, equity)
        drawdown = equity / peak - 1
        if not halted and -drawdown + 1e-12 >= config.risk.max_drawdown:
            halted = True
            warnings.append(f"{date.date()}: maximum drawdown halt triggered ({drawdown:.2%}); no further entries, liquidation at next available open.")
        rows.append({
            "date": date, "cash": account.cash, "shares": account.shares,
            "market_value": account.shares * close,
            "dividend_receivable": account.dividend_receivable,
            "equity": equity, "benchmark_equity": benchmark.equity_at(close),
            "drawdown": drawdown, "halted": halted,
        })
    equity_frame = pd.DataFrame(rows).set_index("date")
    trades = pd.DataFrame(ledger, columns=["date", "signal_date", "action", "shares", "price", "commission", "notional", "cash_after", "reason"])
    if first_tradable is None:
        warnings.append("No tradable session after indicator warmup; strategy and benchmark remain in cash.")
    if halted and account.shares:
        warnings.append("A drawdown halt remains unliquidated because no later tradable open is available.")
    if account.dividend_receivable:
        warnings.append(f"Ending equity includes ${account.dividend_receivable:.2f} of unpaid dividend receivables.")
    metrics = _metrics(equity_frame, trades, config.initial_cash, first_tradable)
    if metrics["trade_count"] < 10:
        warnings.append("Fewer than 10 BUY/SELL transactions; performance statistics have limited evidential value.")
    return BacktestResult(equity_frame, trades, signals, metrics, warnings)


def _metrics(equity: pd.DataFrame, trades: pd.DataFrame, initial_cash: float, first_tradable: int | None) -> dict:
    evaluated = equity.iloc[first_tradable:] if first_tradable is not None else equity.iloc[:0]
    returns = evaluated["equity"].pct_change()
    if len(returns):
        returns.iloc[0] = evaluated["equity"].iloc[0] / initial_cash - 1
    periods = len(evaluated)
    ending = float(equity["equity"].iloc[-1])
    total_return = ending / initial_cash - 1
    daily_vol = float(returns.std(ddof=1)) if periods > 1 else float("nan")
    transactions = trades.loc[trades["action"].isin(["BUY", "SELL"])]
    avg_equity = float(evaluated["equity"].mean()) if periods else initial_cash
    return {
        "total_return": total_return,
        "cagr": (ending / initial_cash) ** (252 / periods) - 1 if periods and ending > 0 else (-1.0 if ending <= 0 else None),
        "volatility": daily_vol * math.sqrt(252) if math.isfinite(daily_vol) else None,
        "sharpe": float(returns.mean()) / daily_vol * math.sqrt(252) if math.isfinite(daily_vol) and daily_vol > 0 else None,
        "max_drawdown": float(equity["drawdown"].min()),
        "trade_count": int(len(transactions)),
        "exposure": float((evaluated["shares"] > 0).mean()) if periods else 0.0,
        "turnover": float(transactions["notional"].sum()) / avg_equity if avg_equity > 0 else None,
        "total_commission": float(transactions["commission"].sum()),
        "benchmark_total_return": float(equity["benchmark_equity"].iloc[-1]) / initial_cash - 1,
        "ending_equity": ending,
        "evaluation_sessions": periods,
        "evaluation_start": str(evaluated.index[0].date()) if periods else None,
        "metric_notes": [
            "Signals use each session's close; fills use the next dataset session's raw open with adverse slippage and commission.",
            "Annualization uses 252 sessions; the first evaluated daily return includes that session's opening fill costs and intraday move. Warmup sessions are excluded from annualization, volatility, Sharpe and exposure.",
            "Sharpe uses zero risk-free rate and sample daily standard deviation (ddof=1); undefined statistics are null. Maximum drawdown is a nonpositive fraction including initial capital.",
            "Benchmark uses full affordable initial cash and whole shares at the first nonzero-volume open after slow-window warmup, independent of signal direction; same dates, costs, raw prices and dividend/split accounting. No reinvestment of dividends.",
            "Dividends accrue before ex-date opening orders and remain in equity until payment; payment on a non-session date is credited on the next available session. Unknown payment dates stay receivable.",
            "Exposure is the fraction of evaluated closes with positive holdings; turnover is total BUY/SELL fill notional divided by average evaluated equity; trade_count excludes corporate actions.",
            "Positions remain marked at the final close; final signals do not execute without a later session. Fractional split entitlements are retained, including on liquidation; new purchases use whole shares.",
            "Allocation and order limits apply at entry only; exits fully liquidate and may exceed the entry order cap. Price moves and splits can subsequently change exposure. Drawdown checks occur only at close and cannot protect against an opening gap.",
            "Zero-volume sessions produce no fills for either strategy or benchmark. At the next tradable open, the latest prior-close regime governs entry/exit; a drawdown halt remains in effect.",
        ],
    }
