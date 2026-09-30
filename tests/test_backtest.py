"""Small, hand-calculated ledger cases rather than performance-fit assertions."""

import pandas as pd
import pytest

from quant_trading.backtest import run_backtest
from quant_trading.config import AppConfig, CostConfig, RiskConfig, StrategyConfig
from quant_trading.risk import size_buy
from quant_trading.strategy import generate_signals


def bars(closes, opens=None, **events):
    opens = closes if opens is None else opens
    frame = pd.DataFrame({
        "open": opens, "high": [max(o, c) for o, c in zip(opens, closes)],
        "low": [min(o, c) for o, c in zip(opens, closes)], "close": closes,
        "volume": [10000] * len(closes), "split_ratio": [1.0] * len(closes),
        "dividend": [0.0] * len(closes), "dividend_pay_date": [""] * len(closes),
    }, index=pd.bdate_range("2024-01-02", periods=len(closes), name="date"))
    for column, values in events.items():
        frame[column] = values
    return frame


def config(fast=1, slow=2, target=1.0, max_drawdown=1.0, costs=None):
    return AppConfig(
        initial_cash=1000.0, strategy=StrategyConfig(fast, slow),
        costs=costs or CostConfig(commission_bps=0.0, slippage_bps=0.0),
        risk=RiskConfig(target, 1.0, 1_000_000.0, 0.0, max_drawdown),
    )


def test_signal_executes_at_next_open_and_existing_position_is_not_rebalanced():
    source = bars([10.0, 11.0, 12.0, 13.0, 14.0])
    result = run_backtest(source, config(fast=2, slow=3, target=0.5))
    trades = result.trades
    assert trades["action"].tolist() == ["BUY"]
    assert trades.iloc[0]["signal_date"] == source.index[2]
    assert trades.iloc[0]["date"] == source.index[3]
    assert trades.iloc[0]["shares"] == 38
    assert result.equity["shares"].tolist() == [0, 0, 0, 38, 38]
    assert result.equity.iloc[-1]["cash"] == 506
    assert result.metrics["ending_equity"] == 1038
    assert result.metrics["benchmark_total_return"] == pytest.approx(0.076)
    assert result.metrics["evaluation_sessions"] == 2


def test_future_prices_and_splits_cannot_rewrite_past_signals():
    original = bars([100.0, 110.0, 120.0, 61.0, 65.0], split_ratio=[1, 1, 1, 2, 1])
    changed = original.copy()
    changed.loc[changed.index[-1], "close"] = 1000
    changed.loc[changed.index[-1], "split_ratio"] = 10
    first = generate_signals(original, StrategyConfig(1, 2))
    second = generate_signals(changed, StrategyConfig(1, 2))
    pd.testing.assert_frame_equal(first.iloc[:-1], second.iloc[:-1])
    # On the split date 120 becomes 60 in the current share unit; no false exit.
    assert first.iloc[3]["slow_ma"] == 60.5
    assert first.iloc[3]["target"] == 1


def test_all_in_costs_limit_buy_size_and_appear_in_cash():
    source = bars([10.0, 20.0, 22.0], opens=[10.0, 20.0, 20.0])
    costs = CostConfig(commission_bps=100.0, slippage_bps=1000.0)
    result = run_backtest(source, config(costs=costs))
    trade = result.trades.iloc[0]
    assert trade["shares"] == 45
    assert trade["price"] == pytest.approx(22)
    assert trade["commission"] == pytest.approx(9.9)
    assert result.equity.iloc[-1]["cash"] == pytest.approx(0.1)
    assert result.metrics["ending_equity"] == pytest.approx(990.1)
    assert result.metrics["total_commission"] == pytest.approx(9.9)
    assert result.metrics["benchmark_total_return"] == pytest.approx(result.metrics["total_return"])


def test_cash_buffer_order_cap_and_existing_reservations_are_enforced():
    costs = CostConfig(commission_bps=100, slippage_bps=100, min_commission=2)
    risk = RiskConfig(1, 1, 250, 0.1, 1)
    # $250 all-in order cap gives 24 shares at $10.10 plus $2.424 commission.
    assert size_buy(1000, 1000, 10, costs, risk) == 24
    # $220 cash - $100 buffer - $50 reservation = $70, or 6 whole shares.
    assert size_buy(220, 1000, 10, costs, risk, pending_buy_notional=50) == 6
    # $990 of preexisting marked exposure leaves $10: minimum fee prevents buying.
    assert size_buy(1000, 1000, 10, costs, risk, current_shares=99) == 0


def test_exit_uses_following_open_and_final_position_is_not_forced_closed():
    source = bars([10.0, 20.0, 5.0, 6.0], opens=[10.0, 20.0, 20.0, 6.0])
    result = run_backtest(source, config())
    assert result.trades["action"].tolist() == ["BUY", "SELL"]
    assert result.trades["price"].tolist() == [20, 6]
    assert result.trades["shares"].tolist() == [50, 50]
    assert result.metrics["ending_equity"] == 300
    short = run_backtest(source.iloc[:3], config())
    assert short.trades["action"].tolist() == ["BUY"]
    assert short.equity.iloc[-1]["shares"] == 50
    assert short.metrics["ending_equity"] == 250


def test_sell_slippage_and_commission_reduce_proceeds():
    source = bars([10.0, 20.0, 5.0, 6.0], opens=[10, 20, 20, 6])
    costs = CostConfig(commission_bps=100, slippage_bps=100)
    result = run_backtest(source, config(costs=costs))
    # Buy: 49 * 20.20 + 9.898 => 0.302 cash.
    # Sell: 49 * 5.94 - 2.9106 => 288.4514 ending cash.
    assert result.trades["shares"].tolist() == [49, 49]
    assert result.trades.iloc[1]["price"] == pytest.approx(5.94)
    assert result.metrics["ending_equity"] == pytest.approx(288.4514)
    assert result.metrics["total_commission"] == pytest.approx(12.8086)


def test_split_changes_shares_once_without_double_counting_price_adjustment():
    source = bars([100.0, 110.0, 120.0, 61.0, 65.0], opens=[100, 110, 120, 60, 65], split_ratio=[1, 1, 1, 2, 1])
    result = run_backtest(source, config())
    assert result.trades["action"].tolist() == ["BUY", "SPLIT"]
    assert result.equity["shares"].tolist() == [0, 0, 8, 16, 16]
    assert result.equity.iloc[-1]["cash"] == 40
    assert result.metrics["ending_equity"] == 1080
    assert result.metrics["trade_count"] == 1


def test_reverse_split_keeps_fractional_entitlement_and_warns():
    source = bars([100.0, 110.0, 120.0, 1201.0], opens=[100, 110, 120, 1200], split_ratio=[1, 1, 1, 0.1])
    result = run_backtest(source, config())
    assert result.equity.iloc[-1]["shares"] == pytest.approx(0.8)
    assert result.metrics["ending_equity"] == pytest.approx(1000.8)
    assert any("fractional" in warning for warning in result.warnings)


def test_dividend_is_entitled_before_ex_open_and_paid_after_sale():
    source = bars([10.0, 20.0, 21.0, 19.0, 19.0, 19.0], opens=[10, 20, 20, 19, 19, 19])
    source.loc[source.index[3], "dividend"] = 1.0
    source.loc[source.index[3], "dividend_pay_date"] = str(source.index[5].date())
    result = run_backtest(source, config())
    assert result.trades["action"].tolist() == ["BUY", "DIVIDEND", "SELL", "DIVIDEND_PAYMENT"]
    assert result.equity.iloc[3]["dividend_receivable"] == 50
    assert result.equity.iloc[3]["cash"] == 0
    assert result.equity.iloc[3]["equity"] == 1000
    assert result.equity.iloc[4]["shares"] == 0
    assert result.equity.iloc[4]["cash"] == 950
    assert result.equity.iloc[4]["dividend_receivable"] == 50
    assert result.equity.iloc[5]["cash"] == 1000
    assert result.equity.iloc[5]["dividend_receivable"] == 0
    assert result.metrics["trade_count"] == 2


def test_ex_date_buyer_does_not_receive_dividend():
    source = bars([10.0, 20.0, 20.0, 20.0])
    source.loc[source.index[2], "dividend"] = 1.0
    source.loc[source.index[2], "dividend_pay_date"] = str(source.index[3].date())
    result = run_backtest(source, config())
    assert "DIVIDEND" not in result.trades["action"].tolist()
    assert result.equity["dividend_receivable"].sum() == 0


def test_non_session_dividend_payment_becomes_cash_on_next_available_session():
    source = bars([10.0, 20.0, 20.0, 19.0, 19.0])
    source.loc[source.index[3], "dividend"] = 1
    source.loc[source.index[3], "dividend_pay_date"] = "2024-01-06"  # Saturday.
    result = run_backtest(source, config())
    payment = result.trades.loc[result.trades["action"] == "DIVIDEND_PAYMENT"].iloc[0]
    assert payment["date"] == pd.Timestamp("2024-01-08")
    assert result.equity.iloc[-1]["cash"] == 1000
    assert result.equity.iloc[-1]["dividend_receivable"] == 0


def test_unpaid_dividend_remains_in_final_equity_with_warning():
    source = bars([10.0, 20.0, 21.0, 19.0], opens=[10, 20, 20, 19])
    source.loc[source.index[3], "dividend"] = 1.0
    result = run_backtest(source, config())
    assert result.equity.iloc[-1]["cash"] == 0
    assert result.equity.iloc[-1]["dividend_receivable"] == 50
    assert result.metrics["ending_equity"] == 1000
    assert any("payment date missing" in warning for warning in result.warnings)
    assert any("unpaid" in warning for warning in result.warnings)


def test_drawdown_halt_is_assessed_at_close_and_exits_next_open_permanently():
    source = bars([10.0, 19.0, 20.0, 10.0, 12.0, 20.0], opens=[10, 19, 20, 20, 12, 20])
    result = run_backtest(source, config(max_drawdown=0.2))
    assert result.equity["halted"].tolist() == [False, False, False, True, True, True]
    assert result.equity["shares"].tolist() == [0, 0, 50, 50, 0, 0]
    assert result.trades["action"].tolist() == ["BUY", "SELL"]
    assert result.trades.iloc[1]["date"] == source.index[4]
    assert result.trades.iloc[1]["reason"] == "Drawdown halt liquidation"
    assert result.metrics["ending_equity"] == 600
    assert result.metrics["max_drawdown"] == -0.5
    assert result.metrics["exposure"] == 0.5


def test_exact_drawdown_threshold_triggers_despite_float_rounding():
    source = bars([10.0, 19.0, 20.0, 16.0, 17.0], opens=[10, 19, 20, 20, 17])
    result = run_backtest(source, config(max_drawdown=0.2))
    assert result.equity.iloc[3]["halted"]
    assert result.equity.iloc[4]["shares"] == 0
    assert result.metrics["ending_equity"] == 850


def test_warmup_without_a_later_session_has_no_strategy_or_benchmark_trade():
    source = bars([10.0, 20.0])
    result = run_backtest(source, config())
    assert result.trades.empty
    assert result.metrics["ending_equity"] == 1000
    assert result.metrics["benchmark_total_return"] == 0
    assert result.metrics["evaluation_sessions"] == 0
    assert result.metrics["cagr"] is None
    assert any("warmup" in warning for warning in result.warnings)


def test_repeat_run_is_identical():
    source = bars([10.0, 20.0, 20.0, 10.0, 12.0])
    first = run_backtest(source, config())
    second = run_backtest(source, config())
    pd.testing.assert_frame_equal(first.equity, second.equity)
    pd.testing.assert_frame_equal(first.trades, second.trades)
    pd.testing.assert_frame_equal(first.signals, second.signals)
    assert first.metrics == second.metrics


def test_zero_volume_open_does_not_fill_and_latest_close_supersedes_old_target():
    source = bars([10.0, 20.0, 5.0, 6.0, 7.0], opens=[10, 20, 20, 6, 7])
    source.loc[source.index[2], "volume"] = 0
    result = run_backtest(source, config())
    # Long signal at day 2 cannot fill at zero-volume day 3. Day 3 publishes
    # a flat target, so day 4 cannot fill the stale long signal either.
    assert result.trades["action"].tolist() == ["BUY"]
    assert result.trades.iloc[0]["date"] == source.index[4]
    # Baseline starts at the first shared tradable open (day 4), regardless
    # of the strategy's flat signal; 166 shares * $7 + $4 cash = $1166.
    assert result.metrics["evaluation_start"] == str(source.index[3].date())
    assert result.metrics["benchmark_total_return"] == pytest.approx(0.166)
    assert any("zero-volume" in warning for warning in result.warnings)


def test_zero_volume_session_delays_halt_liquidation_until_tradable_open():
    source = bars([10.0, 19.0, 20.0, 10.0, 12.0, 13.0], opens=[10, 19, 20, 20, 12, 13])
    source.loc[source.index[4], "volume"] = 0
    result = run_backtest(source, config(max_drawdown=0.2))
    assert result.equity["shares"].tolist() == [0, 0, 50, 50, 50, 0]
    assert result.trades["action"].tolist() == ["BUY", "SELL"]
    assert result.trades.iloc[1]["date"] == source.index[5]
    assert result.metrics["ending_equity"] == 650
