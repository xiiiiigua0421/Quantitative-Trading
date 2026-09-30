from dataclasses import replace
import json
from urllib.error import HTTPError, URLError

import pandas as pd
import pytest

from quant_trading.broker import AlpacaPaperBroker, BrokerError, BrokerHTTPError, BrokerUnavailable
from quant_trading.config import AppConfig, CostConfig, RiskConfig, StrategyConfig
from quant_trading.execution import (
    NEW_YORK, _locked_database, get_status, paper_step, set_stopped,
)


def make_bars(end="2026-09-29", *, falling=False):
    dates = pd.bdate_range(end=pd.Timestamp(end), periods=12)
    prices = list(range(111, 99, -1)) if falling else list(range(100, 112))
    bars = pd.DataFrame({"open": prices, "high": [p + 1 for p in prices],
                         "low": [p - 1 for p in prices], "close": prices,
                         "volume": 1000, "split_ratio": 1.0, "dividend": 0.0,
                         "dividend_pay_date": pd.NaT}, index=dates)
    bars.attrs["metadata"] = {"symbol": "SPY", "feed": "iex", "adjustment": "raw",
                              "synthetic": False, "corporate_actions_complete": True}
    return bars


@pytest.fixture
def config(tmp_path):
    return AppConfig(symbol="SPY", strategy=StrategyConfig(2, 3),
                     costs=CostConfig(0, 0, 0), risk=RiskConfig(),
                     state_path=tmp_path / "paper.sqlite3")


class FakeBroker:
    def __init__(self, now="2026-09-30 09:35"):
        self.now = pd.Timestamp(now, tz=NEW_YORK)
        self.remote = {}
        self.submissions = []
        self.queries = []
        self.failure = None
        self.equity = 10000.0
        self.cash = 10000.0
        self.external_positions = None
        self.external_orders = []
        self.trading_blocked = False
        self.quote_age = 0
        self.quote_bid = 99.0
        self.quote_ask = 100.0
        self.clock_open = True
        self.calendar_closed = set()
        self.close_time = "16:00"

    def clock(self):
        return {"timestamp": self.now.isoformat(), "is_open": self.clock_open}

    def account(self):
        return {"id": "paper-test-account", "status": "ACTIVE", "cash": str(self.cash),
                "equity": str(self.equity), "buying_power": "1000000",
                "trading_blocked": self.trading_blocked, "account_blocked": False,
                "currency": "USD"}

    def positions(self):
        if self.external_positions is not None:
            return self.external_positions
        qty = sum(float(order["filled_qty"]) * (1 if order["side"] == "buy" else -1)
                  for order in self.remote.values())
        return [{"symbol": "SPY", "qty": str(qty), "side": "long", "asset_class": "us_equity"}] if qty else []

    def orders(self, status="open"):
        assert status == "open"
        return [dict(order) for order in self.remote.values()
                if order["status"] not in {"filled", "canceled", "expired", "rejected"}] + self.external_orders

    def get_order(self, client_order_id):
        self.queries.append(client_order_id)
        return dict(self.remote[client_order_id]) if client_order_id in self.remote else None

    def calendar(self, start, end):
        return [{"date": day.date().isoformat(), "open": "09:30", "close": self.close_time}
                for day in pd.bdate_range(start, end) if day.date().isoformat() not in self.calendar_closed]

    def snapshot(self, symbol, feed="iex"):
        assert symbol == "SPY" and feed == "iex"
        return {"latestQuote": {"bp": self.quote_bid, "ap": self.quote_ask,
                                "t": (self.now - pd.Timedelta(seconds=self.quote_age)).isoformat()}}

    def submit_order(self, *, symbol, qty, side, client_order_id):
        self.submissions.append({"symbol": symbol, "qty": qty, "side": side, "client_order_id": client_order_id})
        if self.failure == "timeout_before":
            raise TimeoutError("secret-value-must-not-be-logged")
        order = {"id": "broker-" + client_order_id, "symbol": symbol, "qty": str(qty),
                 "side": side, "client_order_id": client_order_id, "status": "new", "filled_qty": "0"}
        self.remote[client_order_id] = order
        if self.failure == "timeout_after":
            raise TimeoutError("secret-value-must-not-be-logged")
        if self.failure == "malformed_ack":
            return {"id": order["id"]}  # Broker accepted it, but ack cannot be trusted.
        return dict(order)

    def fill(self, qty=None, status="filled"):
        order = next(iter(self.remote.values()))
        order["filled_qty"] = str(qty if qty is not None else order["qty"])
        order["status"] = status


def step(bars, config, broker, *, submit=True):
    return paper_step(bars, config, broker, submit=submit, now=broker.now)


def test_dry_run_is_read_only_then_same_session_can_submit(config):
    broker = FakeBroker()
    result = step(make_bars(), config, broker, submit=False)
    assert result["action"] == "dry_run"
    assert result["order"]["qty"] == 50
    assert broker.submissions == [] and get_status(config.state_path)["intents"] == []
    assert step(make_bars(), config, broker)["action"] == "submitted"
    assert len(broker.submissions) == 1


@pytest.mark.parametrize("failure", ["timeout_before", "timeout_after"])
def test_timeout_never_reposts_across_restart_or_next_session(config, failure):
    broker = FakeBroker()
    broker.failure = failure
    result = step(make_bars(), config, broker)
    assert result["action"] == "unknown"
    assert "secret-value" not in json.dumps(get_status(config.state_path))
    assert len(broker.submissions) == 1
    assert step(make_bars(), replace(config), broker)["action"] == "blocked"
    broker.now += pd.Timedelta(days=1)
    assert step(make_bars("2026-09-30"), config, broker)["action"] == "blocked"
    assert len(broker.submissions) == 1
    assert len(broker.queries) >= 3


def test_partial_fill_blocks_and_finished_order_is_idempotent_with_new_config(config):
    broker = FakeBroker()
    assert step(make_bars(), config, broker)["action"] == "submitted"
    broker.fill(10, "partially_filled")
    assert step(make_bars(), config, broker)["action"] == "blocked"
    assert get_status(config.state_path)["intents"][0]["filled_qty"] == 10
    broker.fill()
    changed = replace(config, strategy=StrategyConfig(1, 4), risk=replace(config.risk, target_fraction=0.3))
    assert step(make_bars(), changed, broker)["action"] == "already_processed"
    assert len(broker.submissions) == 1


def test_reconciles_ambiguous_acknowledgement_then_fills(config):
    broker = FakeBroker()
    broker.failure = "timeout_after"
    assert step(make_bars(), config, broker)["action"] == "unknown"
    broker.fill()
    assert step(make_bars(), config, broker)["action"] == "already_processed"
    assert get_status(config.state_path)["intents"][0]["status"] == "filled"
    assert len(broker.submissions) == 1


def test_malformed_submission_acknowledgement_is_query_only(config):
    broker = FakeBroker()
    broker.failure = "malformed_ack"
    assert step(make_bars(), config, broker)["action"] == "unknown"
    assert step(make_bars(), config, broker)["action"] == "blocked"
    assert len(broker.submissions) == 1
    broker.fill()
    assert step(make_bars(), config, broker)["action"] == "already_processed"
    assert len(broker.submissions) == 1


def test_pending_cash_reservation_uses_original_cost_config(config):
    broker = FakeBroker()
    expensive = replace(config, costs=CostConfig(10, 10, 2))
    step(make_bars(), expensive, broker)
    broker.fill(10, "partially_filled")
    result = step(make_bars(), replace(config), broker)
    original_qty = broker.submissions[0]["qty"]
    remaining_notional = (original_qty - 10) * 100 * 1.001
    assert result["reserved_buy_cash"] == pytest.approx(remaining_notional + max(2, remaining_notional * .001))
    assert result["action"] == "blocked"


def test_canceled_partial_fill_remains_owned_without_rebuying(config):
    broker = FakeBroker()
    step(make_bars(), config, broker)
    broker.fill(10, "canceled")
    assert step(make_bars(), config, broker)["action"] == "already_processed"
    broker.now += pd.Timedelta(days=1)
    assert step(make_bars("2026-09-30"), config, broker)["action"] == "hold"
    assert len(broker.submissions) == 1


def test_no_daily_rebalance_and_exit_never_shorts(config):
    broker = FakeBroker()
    step(make_bars(), config, broker)
    broker.fill()
    broker.now += pd.Timedelta(days=1)
    assert step(make_bars("2026-09-30"), config, broker)["action"] == "hold"
    assert len(broker.submissions) == 1
    result = step(make_bars("2026-09-30", falling=True), config, broker)
    assert result["action"] == "submitted" and result["order"]["side"] == "sell"
    assert result["order"]["qty"] <= 50


def test_cash_only_sizing_ignores_margin_buying_power(config):
    broker = FakeBroker()
    broker.cash = 1600
    result = step(make_bars(), config, broker, submit=False)
    assert result["order"]["qty"] == 6  # $1,000 equity-based cash buffer retained.


def test_persistent_stop_and_manual_resume(config):
    broker = FakeBroker()
    assert set_stopped(config.state_path)["stopped"] is True
    assert step(make_bars(), config, broker)["action"] == "blocked"
    assert broker.queries == [] and broker.submissions == []
    set_stopped(config.state_path, False)
    assert step(make_bars(), config, broker)["action"] == "submitted"


def test_drawdown_peak_survives_restart_and_halts(config):
    broker = FakeBroker()
    step(make_bars(), config, broker, submit=False)
    broker.equity = 8400
    result = step(make_bars(), config, broker)
    assert result["action"] == "hold" and "drawdown" in result["reason"]
    assert get_status(config.state_path)["risk_halted"]
    assert not get_status(config.state_path)["stopped"]
    assert get_status(config.state_path)["peak_equity"] == 10000
    assert broker.submissions == []


def test_drawdown_liquidation_survives_restart_and_never_reenters(config):
    broker = FakeBroker()
    step(make_bars(), config, broker)
    broker.fill()
    step(make_bars(), config, broker)  # Reconcile filled entry before restart.
    broker.now += pd.Timedelta(days=1)
    broker.equity = 8400
    dry = step(make_bars("2026-09-30"), config, broker, submit=False)
    assert dry["action"] == "dry_run" and dry["order"]["side"] == "sell"
    assert get_status(config.state_path)["risk_halted"]
    assert len(get_status(config.state_path)["intents"]) == 1
    result = step(make_bars("2026-09-30"), replace(config), broker)
    assert result["action"] == "submitted" and result["order"]["qty"] == 50
    assert result["signal"]["risk_halt"]
    exit_order = broker.remote[result["order"]["client_order_id"]]
    exit_order["filled_qty"], exit_order["status"] = exit_order["qty"], "filled"
    step(make_bars("2026-09-30"), config, broker)
    broker.now += pd.Timedelta(days=1)
    broker.equity = 12000  # Recovery or cash deposit never resets risk halt.
    set_stopped(config.state_path, False)
    after = step(make_bars("2026-10-01"), config, broker)
    assert after["action"] == "hold" and after["risk_halted"]
    assert len(broker.submissions) == 2


def test_risk_exit_uses_reconciled_holdings_and_fresh_quote_without_forecast(config):
    broker = FakeBroker()
    step(make_bars(), config, broker)
    broker.fill()
    step(make_bars(), config, broker)
    broker.now += pd.Timedelta(days=1)
    broker.equity = 8000
    stale = make_bars().iloc[:0]
    stale.attrs.clear()
    result = step(stale, config, broker)
    assert result["action"] == "submitted" and result["order"]["side"] == "sell"
    assert result["signal"]["risk_halt"]


@pytest.mark.parametrize("change", [{"synthetic": True}, {"corporate_actions_complete": False},
                                     {"symbol": "QQQ"}, {"feed": "sip"}, {"adjustment": "all"}])
def test_unverified_data_can_only_be_diagnostic_dry_run(config, change):
    broker = FakeBroker()
    bars = make_bars()
    bars.attrs["metadata"].update(change)
    dry = step(bars, config, broker, submit=False)
    assert dry["action"] == "dry_run" and dry["warnings"]
    assert step(bars, config, broker)["action"] == "blocked"
    assert broker.submissions == []


@pytest.mark.parametrize("positions", [
    [{"symbol": "AAPL", "qty": "1", "side": "long"}],
    [{"symbol": "SPY", "qty": "1", "side": "long"}],
    [{"symbol": "SPY", "qty": "0.5", "side": "long"}],
    [{"symbol": "SPY", "qty": "-1", "side": "short"}],
])
def test_unexplained_or_fractional_or_short_positions_fail_closed(config, positions):
    broker = FakeBroker()
    broker.external_positions = positions
    assert step(make_bars(), config, broker)["action"] == "blocked"
    assert broker.submissions == []


def test_unexpected_external_open_order_blocks(config):
    broker = FakeBroker()
    broker.external_orders = [{"symbol": "SPY", "client_order_id": "manual-order"}]
    assert step(make_bars(), config, broker)["action"] == "blocked"
    assert broker.submissions == []


def test_account_disabled_blocks(config):
    broker = FakeBroker()
    broker.trading_blocked = True
    assert step(make_bars(), config, broker)["action"] == "blocked"
    assert broker.submissions == []


def test_stale_or_missing_completed_bar_blocks(config):
    broker = FakeBroker()
    assert "Stale" in step(make_bars().iloc[:-1], config, broker)["reason"]
    bars = make_bars().drop(pd.Timestamp("2026-09-28"))
    assert "missing" in step(bars, config, broker)["reason"]
    assert broker.submissions == []


def test_today_uncompleted_bar_cannot_change_signal(config):
    broker = FakeBroker()
    bars = make_bars()
    first = step(bars, config, broker, submit=False)
    today = bars.iloc[[-1]].copy()
    today.index = pd.DatetimeIndex(["2026-09-30"])
    today[["open", "high", "low", "close"]] = [1, 2, 1, 1]
    second = step(pd.concat([bars, today]), config, broker, submit=False)
    assert first["signal"] == second["signal"]


@pytest.mark.parametrize("age,bid,ask", [(121, 99, 100), (-1, 99, 100), (0, 101, 100), (0, 0, 100), (0, 99, float("nan"))])
def test_bad_quote_blocks(config, age, bid, ask):
    broker = FakeBroker()
    broker.quote_age, broker.quote_bid, broker.quote_ask = age, bid, ask
    assert step(make_bars(), config, broker)["action"] == "blocked"
    assert broker.submissions == []


@pytest.mark.parametrize("now", ["2026-09-30 09:29", "2026-09-30 09:45", "2026-09-30 12:00"])
def test_outside_opening_window_waits(config, now):
    broker = FakeBroker(now)
    assert step(make_bars(), config, broker)["action"] == "wait"
    assert broker.submissions == []


def test_holiday_and_weekend_calendar_controls_trading(config):
    broker = FakeBroker("2026-09-30 09:35")
    broker.calendar_closed = {"2026-09-30"}
    assert step(make_bars(), config, broker)["action"] == "wait"
    broker = FakeBroker("2026-10-03 09:35")
    assert step(make_bars("2026-10-02"), config, broker)["action"] == "wait"


def test_dst_uses_new_york_session_and_utc_now(config):
    broker = FakeBroker("2026-11-02 09:35")
    result = paper_step(make_bars("2026-10-30"), config, broker, now=broker.now.tz_convert("UTC"))
    assert result["action"] == "dry_run"
    assert result["session"] == "2026-11-02"


def test_early_close_is_completed_using_exchange_calendar(config):
    broker = FakeBroker("2026-09-30 13:05")
    broker.close_time = "13:00"
    broker.clock_open = False
    result = step(make_bars("2026-09-30"), config, broker)
    assert result["action"] == "wait"
    assert result["signal"]["date"] == "2026-09-30"
    assert broker.submissions == []


def test_window_is_rechecked_after_read_preparation(config):
    broker = FakeBroker()
    original_clock = broker.clock
    calls = 0

    def delayed_clock():
        nonlocal calls
        calls += 1
        if calls > 1:
            return {"timestamp": (broker.now + pd.Timedelta(minutes=10)).isoformat(), "is_open": True}
        return original_clock()

    broker.clock = delayed_clock
    result = step(make_bars(), config, broker)
    assert result["action"] == "blocked"
    assert "changed" in result["reason"]
    assert broker.submissions == []
    assert get_status(config.state_path)["intents"] == []


def test_concurrent_process_lock_refuses_second_send(config):
    broker = FakeBroker()
    with _locked_database(config.state_path):
        result = step(make_bars(), config, broker)
    assert result["action"] == "blocked" and "Another process" in result["reason"]
    assert broker.submissions == []


class FakeResponse:
    def __init__(self, payload):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def read(self):
        return json.dumps(self.payload).encode()


def test_broker_endpoints_are_paper_only_and_data_is_read_only():
    captured = []

    def opener(req, timeout):
        captured.append(req)
        return FakeResponse({"ok": True})

    broker = AlpacaPaperBroker(key_id="test-key", secret_key="test-secret", opener=opener)
    broker.account()
    broker.submit_order(symbol="SPY", qty=1, side="buy", client_order_id="qt-test")
    broker.snapshot("SPY")
    assert all(req.full_url.startswith("https://paper-api.alpaca.markets/") for req in captured[:2])
    payload = json.loads(captured[1].data)
    assert payload["extended_hours"] is False and payload["time_in_force"] == "day" and payload["qty"] == "1"
    assert captured[2].full_url.startswith("https://data.alpaca.markets/")
    with pytest.raises(ValueError):
        broker.request("POST", "/v2/orders", payload={}, data_api=True)
    with pytest.raises(ValueError):
        broker.request("GET", "https://api.alpaca.markets/v2/account")


def test_broker_only_404_means_order_absent_and_errors_are_sanitized():
    def missing(req, timeout):
        raise HTTPError(req.full_url, 404, "secret-key-is-sensitive", {}, None)

    broker = AlpacaPaperBroker(key_id="test-key", secret_key="test-secret", opener=missing)
    assert broker.get_order("test-order") is None

    def forbidden(req, timeout):
        raise HTTPError(req.full_url, 403, "secret-key-is-sensitive", {}, None)

    broker._opener = forbidden
    with pytest.raises(BrokerHTTPError) as caught:
        broker.get_order("test-order")
    assert "sensitive" not in str(caught.value)

    def timeout(req, timeout):
        raise URLError("secret-key-is-sensitive")

    broker._opener = timeout
    with pytest.raises(BrokerUnavailable) as caught:
        broker.account()
    assert "sensitive" not in str(caught.value)


def test_broker_refuses_live_host_even_if_constants_are_changed(monkeypatch):
    import quant_trading.broker as module

    called = False

    def opener(req, timeout):
        nonlocal called
        called = True
        return FakeResponse({})

    broker = AlpacaPaperBroker(key_id="test-key", secret_key="test-secret", opener=opener)
    monkeypatch.setattr(module, "PAPER_BASE_URL", "https://api.alpaca.markets")
    with pytest.raises(BrokerError, match="Unexpected API host"):
        broker.submit_order(symbol="SPY", qty=1, side="buy", client_order_id="qt-test")
    assert not called

def test_missing_credentials_never_attempt_network(monkeypatch):
    monkeypatch.delenv("APCA_API_KEY_ID", raising=False)
    monkeypatch.delenv("APCA_API_SECRET_KEY", raising=False)
    with pytest.raises(BrokerError, match="PAPER"):
        AlpacaPaperBroker()
