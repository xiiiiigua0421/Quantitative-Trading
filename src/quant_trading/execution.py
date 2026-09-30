"""Fail-closed daily paper execution and durable, at-most-once order intents.

The SQLite file is the ownership ledger, not a replaceable cache. Use a dedicated,
initially flat paper account. An unexplained position (including a split changing
share count) requires manual reconciliation; this module never adopts it silently.
Market orders are sized against a recent quote and cost buffer, not a guaranteed
fill price. ``stop`` blocks new submissions; it does not cancel or liquidate.
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import fcntl
import hashlib
import json
import math
from pathlib import Path
import sqlite3
from zoneinfo import ZoneInfo

import pandas as pd

from .broker import BrokerError
from .config import AppConfig, CostConfig, config_dict
from .risk import commission_for, execution_price, size_buy
from .strategy import generate_signals

NEW_YORK = ZoneInfo("America/New_York")
TERMINAL = {"filled", "canceled", "expired", "rejected"}
PENDING = {"new", "accepted", "pending_new", "partially_filled", "pending_cancel",
           "pending_replace", "accepted_for_bidding", "held", "calculated",
           "stopped", "suspended", "done_for_day", "replaced"}


class _Blocked(Exception):
    pass


@contextmanager
def _locked_database(path: str | Path, *, blocking: bool = False):
    path = Path(path).expanduser().resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    with Path(str(path) + ".lock").open("a+b") as lock:
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
        except BlockingIOError:
            raise _Blocked("Another process is checking or submitting this account.") from None
        db = sqlite3.connect(path, timeout=15)
        db.row_factory = sqlite3.Row
        try:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("PRAGMA synchronous=FULL")
            db.executescript("""
                CREATE TABLE IF NOT EXISTS metadata(key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS intents(
                    client_order_id TEXT PRIMARY KEY,
                    account_id TEXT NOT NULL, symbol TEXT NOT NULL, session TEXT NOT NULL,
                    side TEXT NOT NULL, qty INTEGER NOT NULL CHECK(qty > 0),
                    reference_price REAL NOT NULL, status TEXT NOT NULL,
                    filled_qty INTEGER NOT NULL DEFAULT 0, broker_id TEXT,
                    snapshot_sha TEXT NOT NULL, config_json TEXT NOT NULL,
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                    last_error TEXT,
                    UNIQUE(account_id, symbol, session)
                );
            """)
            db.commit()
            yield db
        finally:
            db.close()
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def _meta(db, key, default=None):
    row = db.execute("SELECT value FROM metadata WHERE key=?", (key,)).fetchone()
    return json.loads(row[0]) if row else default


def _put(db, key, value):
    db.execute("INSERT INTO metadata(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
               (key, json.dumps(value, allow_nan=False)))
    db.commit()


def _status(db) -> dict:
    intents = [dict(row) for row in db.execute("SELECT * FROM intents ORDER BY created_at,client_order_id")]
    for intent in intents:
        intent["config"] = json.loads(intent.pop("config_json"))
    return {"stopped": bool(_meta(db, "stopped", False)),
            "risk_halted": bool(_meta(db, "risk_halted", False)),
            "account_id": _meta(db, "account_id"), "symbol": _meta(db, "symbol"),
            "peak_equity": _meta(db, "peak_equity"), "last_result": _meta(db, "last_result"),
            "intents": intents}


def get_status(state_path: str | Path) -> dict:
    """Read the durable stop flag, ledger and last run; no broker requests."""
    with _locked_database(state_path, blocking=True) as db:
        return _status(db)


def set_stopped(state_path: str | Path, stopped: bool = True) -> dict:
    """Set persistent stop/resume flag. Does not cancel orders or liquidate."""
    if not isinstance(stopped, bool):
        raise ValueError("stopped must be a boolean.")
    with _locked_database(state_path, blocking=True) as db:
        _put(db, "stopped", stopped)
        return _status(db)


def _number(value, label, *, positive=False) -> float:
    if isinstance(value, bool):
        raise _Blocked(f"Invalid {label}.")
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise _Blocked(f"Invalid {label}.") from None
    if not math.isfinite(number) or number < 0 or (positive and number <= 0):
        raise _Blocked(f"Invalid {label}.")
    return number


def _shares(value, label) -> int:
    value = _number(value, label)
    if not value.is_integer():
        raise _Blocked(f"Fractional {label} requires manual reconciliation.")
    return int(value)


def _timestamp(value, label) -> pd.Timestamp:
    try:
        result = pd.Timestamp(value)
    except (ValueError, TypeError):
        raise _Blocked(f"Invalid {label} timestamp.") from None
    if pd.isna(result) or result.tzinfo is None:
        raise _Blocked(f"{label} timestamp must have a timezone.")
    return result


def _validate_order(order, intent):
    if not isinstance(order, dict) or order.get("client_order_id") != intent["client_order_id"]:
        raise _Blocked("Broker order identity does not match the durable intent.")
    if order.get("symbol") != intent["symbol"] or order.get("side") != intent["side"]:
        raise _Blocked("Broker order symbol/side does not match the durable intent.")
    if (order.get("type", "market") != "market" or order.get("time_in_force", "day") != "day"
            or order.get("extended_hours", False) is not False or order.get("legs")
            or order.get("order_class", "simple") not in {"", "simple"}):
        raise _Blocked("Broker order type/session differs from the simple paper intent.")
    if _shares(order.get("qty"), "order quantity") != intent["qty"]:
        raise _Blocked("Broker order quantity does not match the durable intent.")
    filled = _shares(order.get("filled_qty"), "order filled quantity")
    if filled < intent["filled_qty"] or filled > intent["qty"]:
        raise _Blocked("Broker order fill total is inconsistent.")
    status = order.get("status")
    if status not in TERMINAL | PENDING or (status == "filled" and filled != intent["qty"]):
        raise _Blocked("Unrecognized or inconsistent broker order status.")
    if not isinstance(order.get("id"), str) or not order["id"]:
        raise _Blocked("Broker order has no identity.")
    return status, filled, order["id"]


def _record_order(db, order, intent, timestamp):
    status, filled, broker_id = _validate_order(order, intent)
    db.execute("UPDATE intents SET status=?,filled_qty=?,broker_id=?,updated_at=?,last_error=NULL WHERE client_order_id=?",
               (status, filled, broker_id, timestamp, intent["client_order_id"]))
    db.commit()


def _reconcile(db, broker, timestamp):
    # Query unresolved POSTs before doing anything else. A 404 is not permission
    # to retry: the original request might still be accepted after a timeout.
    intents = [dict(row) for row in db.execute("SELECT * FROM intents")]
    for intent in intents:
        if intent["status"] not in TERMINAL:
            remote = broker.get_order(intent["client_order_id"])
            if remote is not None:
                _record_order(db, remote, intent, timestamp)
    intents = {row["client_order_id"]: dict(row) for row in db.execute("SELECT * FROM intents")}
    orders = broker.orders(status="open")
    if not isinstance(orders, list):
        raise _Blocked("Broker open-order response is invalid.")
    for order in orders:
        if not isinstance(order, dict) or order.get("client_order_id") not in intents:
            raise _Blocked("Unexpected external open order; use a dedicated paper account.")
        _record_order(db, order, intents[order["client_order_id"]], timestamp)
    return [dict(row) for row in db.execute("SELECT * FROM intents")], orders


def _reserved_buy_cash(intents):
    reserve = 0.0
    for intent in intents:
        if intent["side"] != "buy" or intent["status"] in TERMINAL:
            continue
        costs = CostConfig(**json.loads(intent["config_json"])["costs"])
        notional = (intent["qty"] - intent["filled_qty"]) * execution_price(intent["reference_price"], "BUY", costs)
        reserve += notional + commission_for(notional, costs)
    return reserve


def _account(db, broker, symbol):
    account = broker.account()
    if not isinstance(account, dict) or not isinstance(account.get("id"), str) or not account["id"]:
        raise _Blocked("Broker account identity is invalid.")
    if account.get("status") != "ACTIVE" or any(account.get(key) is not False for key in
                                               ("trading_blocked", "account_blocked")):
        raise _Blocked("Paper account is inactive, trading disabled, or blocked.")
    if account.get("currency", "USD") != "USD" or account.get("trade_suspended_by_user", False):
        raise _Blocked("Account currency or trading permissions are unsuitable.")
    account_id = account["id"]
    if _meta(db, "account_id", account_id) != account_id or _meta(db, "symbol", symbol) != symbol:
        raise _Blocked("State belongs to a different account or symbol; use its matching configuration.")
    cash = _number(account.get("cash"), "account cash")
    equity = _number(account.get("equity"), "account equity", positive=True)
    # Broker margin buying_power is intentionally ignored: use cash only.
    _put(db, "account_id", account_id)
    _put(db, "symbol", symbol)
    return {"id": account_id, "cash": cash, "equity": equity}


def _position(broker, symbol, intents):
    positions = broker.positions()
    if not isinstance(positions, list):
        raise _Blocked("Broker positions response is invalid.")
    quantity = 0
    seen = set()
    for position in positions:
        if not isinstance(position, dict) or position.get("symbol") != symbol or symbol in seen:
            raise _Blocked("Unexpected external holdings; use a dedicated paper account.")
        seen.add(symbol)
        if position.get("side") != "long" or position.get("asset_class", "us_equity") != "us_equity":
            raise _Blocked("Only long US-equity positions are supported.")
        quantity = _shares(position.get("qty"), "position quantity")
    expected = sum(row["filled_qty"] * (1 if row["side"] == "buy" else -1) for row in intents)
    if expected < 0 or expected != quantity:
        raise _Blocked("Holdings do not match recorded fills; reconcile manually (including corporate actions).")
    return quantity


def _calendar_and_signal(bars, config, broker, now, *, risk_halted=False):
    local_now = now.tz_convert(NEW_YORK)
    today = local_now.date()
    if not risk_halted:
        if not isinstance(bars, pd.DataFrame) or bars.empty or not isinstance(bars.index, pd.DatetimeIndex):
            raise _Blocked("Daily bars are empty or malformed.")
        index = bars.index
        if index.tz is not None or not index.is_unique or not index.is_monotonic_increasing or not index.equals(index.normalize()):
            raise _Blocked("Bars must have unique ordered naive US session dates.")
        if index[-1].date() > today:
            raise _Blocked("Daily bars contain future sessions.")
    start = today - timedelta(days=max(45, config.strategy.slow_window * 3 + 20))
    raw_calendar = broker.calendar(start.isoformat(), today.isoformat())
    if not isinstance(raw_calendar, list) or not raw_calendar:
        raise _Blocked("No valid exchange calendar was returned.")
    sessions = {}
    for item in raw_calendar:
        try:
            day = pd.Timestamp(item["date"]).date()
            opening = pd.Timestamp(f"{day.isoformat()} {item['open']}").tz_localize(NEW_YORK)
            closing = pd.Timestamp(f"{day.isoformat()} {item['close']}").tz_localize(NEW_YORK)
        except (TypeError, KeyError, ValueError):
            raise _Blocked("Malformed exchange calendar.") from None
        if day in sessions or day < start or day > today or not opening < closing:
            raise _Blocked("Inconsistent exchange calendar.")
        sessions[day] = (opening, closing)
    completed = sorted(day for day, (_, closing) in sessions.items() if closing <= now)
    if not completed:
        raise _Blocked("Calendar contains no completed session.")
    latest = completed[-1]
    if risk_halted:
        # Risk liquidation is independent of a forecast. Account/position
        # reconciliation, exchange hours, and fresh quote checks still apply.
        return today, sessions.get(today), {
            "date": latest.isoformat(), "target": 0, "fast_ma": None, "slow_ma": None,
            "risk_halt": True,
            "snapshot_sha": hashlib.sha256(f"persistent-risk-halt|{latest}".encode()).hexdigest(),
        }
    usable = bars.loc[bars.index <= pd.Timestamp(latest)].copy()
    if usable.empty or usable.index[-1].date() != latest:
        raise _Blocked("Stale daily bars: latest completed exchange session is missing.")
    recent = completed[-config.strategy.slow_window:]
    if len(recent) < config.strategy.slow_window or [day.isoformat() for day in recent] != [day.date().isoformat() for day in usable.index[-len(recent):]]:
        raise _Blocked("Daily bars have missing or nonexchange sessions in the signal window.")
    columns = ("open", "high", "low", "close", "volume", "split_ratio", "dividend")
    if any(column not in usable for column in columns):
        raise _Blocked("Daily bars lack raw OHLCV/corporate-action columns.")
    for column in columns:
        values = pd.to_numeric(usable[column], errors="coerce")
        if values.isna().any() or not values.map(math.isfinite).all() or (values < 0).any():
            raise _Blocked("Daily bars contain invalid numeric values.")
        if column in {"open", "high", "low", "close", "split_ratio"} and (values <= 0).any():
            raise _Blocked("Daily bars contain nonpositive prices or split ratios.")
    if ((usable["high"] < usable[["open", "close", "low"]].max(axis=1)) |
        (usable["low"] > usable[["open", "close", "high"]].min(axis=1))).any():
        raise _Blocked("Daily OHLC price bounds are inconsistent.")
    signals = generate_signals(usable, config.strategy)
    signal = signals.iloc[-1]
    if not bool(signal["ready"]):
        raise _Blocked("Strategy does not have enough completed history.")
    target = int(signal["target"])
    if target not in {0, 1}:
        raise _Blocked("Strategy returned an unsupported target.")
    sha = hashlib.sha256(pd.util.hash_pandas_object(usable, index=True).values.tobytes()).hexdigest()
    result = {"date": latest.isoformat(), "target": target,
              "fast_ma": float(signal["fast_ma"]), "slow_ma": float(signal["slow_ma"]),
              "snapshot_sha": sha}
    return today, sessions.get(today), result


def _quote(broker, config, now):
    snapshot = broker.snapshot(config.symbol, feed=config.feed)
    quote = snapshot.get("latestQuote") if isinstance(snapshot, dict) else None
    if not isinstance(quote, dict):
        raise _Blocked("No valid market quote snapshot.")
    bid = _number(quote.get("bp"), "quote bid", positive=True)
    ask = _number(quote.get("ap"), "quote ask", positive=True)
    timestamp = _timestamp(quote.get("t"), "quote")
    check_time = now if now is not None else pd.Timestamp.now(tz="UTC")
    age = (check_time - timestamp).total_seconds()
    if ask < bid or not 0 <= age <= 120:
        raise _Blocked("Quote snapshot is stale, in the future, or crossed.")
    return bid, ask, timestamp.isoformat()


def paper_step(bars: pd.DataFrame, config: AppConfig, broker, *, submit: bool = False,
               now=None) -> dict:
    """One idempotent paper iteration, dry-run by default.

    Only the first 15 minutes of the regular exchange session permit submitting
    a market/day order from the prior completed close. Pending/ambiguous orders
    block all further exposure. A dry-run records diagnostics but no order intent.
    """
    config.validate()
    result = {"action": "blocked", "reason": "", "dry_run": not submit,
              "symbol": config.symbol, "session": None}
    try:
        with _locked_database(config.state_path) as db:
            try:
                result = _paper_step_locked(bars, config, broker, submit, now, db, result)
            except _Blocked as exc:
                result["reason"] = str(exc)
            except (BrokerError, TimeoutError, ConnectionError, OSError):
                result["reason"] = "Broker read failed; no new order was submitted."
            _put(db, "last_result", result)
    except _Blocked as exc:
        result["reason"] = str(exc)
    return result


def _paper_step_locked(bars, config, broker, submit, now, db, result):
    if _meta(db, "stopped", False):
        raise _Blocked("Persistent stop flag is active; no new submissions.")
    injected_now = now is not None
    now = _timestamp(now if injected_now else datetime.now(timezone.utc), "local clock")
    clock = broker.clock()
    if not isinstance(clock, dict) or not isinstance(clock.get("is_open"), bool):
        raise _Blocked("Broker clock response is invalid.")
    server_now = _timestamp(clock.get("timestamp"), "broker clock")
    if abs((now - server_now).total_seconds()) > 120:
        raise _Blocked("Local and broker clocks differ by more than 120 seconds.")
    timestamp = now.isoformat()
    account = _account(db, broker, config.symbol)
    result["account"] = account
    intents, open_orders = _reconcile(db, broker, timestamp)
    quantity = _position(broker, config.symbol, intents)
    result["position_qty"] = quantity
    peak = max(account["equity"], _meta(db, "peak_equity", account["equity"]))
    _put(db, "peak_equity", peak)
    drawdown = 1 - account["equity"] / peak
    result["drawdown"] = drawdown
    if drawdown + 1e-12 >= config.risk.max_drawdown:
        _put(db, "risk_halted", True)
    risk_halted = bool(_meta(db, "risk_halted", False))
    result["risk_halted"] = risk_halted
    metadata = bars.attrs.get("metadata", {}) if isinstance(bars, pd.DataFrame) else {}
    metadata_ok = (isinstance(metadata, dict) and metadata.get("symbol") == config.symbol
                   and metadata.get("feed") == config.feed and metadata.get("adjustment") == "raw"
                   and metadata.get("synthetic") is False and metadata.get("corporate_actions_complete") is True)
    if not metadata_ok and not risk_halted:
        if submit:
            raise _Blocked("Submission requires nonsynthetic raw bars with matching symbol/feed and complete corporate actions.")
        result["warnings"] = ["Data provenance is not eligible for submission; this dry-run is diagnostic only."]
    reserve = _reserved_buy_cash(intents)
    result["reserved_buy_cash"] = reserve
    if open_orders or any(intent["status"] not in TERMINAL for intent in intents):
        raise _Blocked("Pending or ambiguous order: reconciliation only, never blindly retry a POST.")
    today, trading_hours, signal = _calendar_and_signal(bars, config, broker, now, risk_halted=risk_halted)
    result.update({"session": today.isoformat(), "signal": signal})
    if trading_hours is None:
        result.update(action="wait", reason="Exchange holiday or weekend.")
        return result
    opening, closing = trading_hours
    if not clock["is_open"] or not opening <= now < min(opening + pd.Timedelta(minutes=15), closing):
        result.update(action="wait", reason="Outside the first 15 minutes of the regular session.")
        return result
    session_intents = [intent for intent in intents if intent["session"] == today.isoformat()]
    if session_intents:
        result.update(action="already_processed", reason="This session already has a durable order intent.",
                      order=session_intents[0])
        return result
    side = "buy" if signal["target"] == 1 and quantity == 0 else "sell" if signal["target"] == 0 and quantity > 0 else None
    if side is None:
        result.update(action="hold", reason="Permanent drawdown halt: flat; no future entries." if risk_halted
                      else "Position already matches the target; no daily rebalance.")
        return result
    bid, ask, quote_time = _quote(broker, config, now if injected_now else None)
    price = ask if side == "buy" else bid
    qty = size_buy(account["cash"], account["equity"], price, config.costs, config.risk,
                   current_shares=quantity, pending_buy_notional=reserve) if side == "buy" else quantity
    # max_order_notional caps risk-adding entries; risk-reducing exits sell the
    # entire reconciled holding, matching the backtest convention.
    if qty <= 0:
        raise _Blocked("Cash buffer, exposure cap, or order cap leaves no whole-share order.")
    if side == "sell" and qty > quantity:
        raise _Blocked("Sell would exceed reconciled holdings.")
    identity = hashlib.sha256(f"{account['id']}|{config.symbol}|{today.isoformat()}".encode()).hexdigest()[:24]
    client_order_id = f"qt-{today:%Y%m%d}-{identity}"
    order = {"client_order_id": client_order_id, "symbol": config.symbol, "side": side,
             "qty": qty, "reference_price": price, "quote_time": quote_time,
             "type": "market", "time_in_force": "day"}
    result["order"] = order
    # Covers restoring an old database or using two different state files. A
    # broker-side identity is never overwritten/adopted without a local ledger.
    if broker.get_order(client_order_id) is not None:
        raise _Blocked("Session order already exists at broker but not in this state; reconcile manually.")
    if not submit:
        result.update(action="dry_run", reason="Proposed PAPER order; no order was submitted.")
        return result
    # Reads may take long enough to leave the opening window. Recheck immediately
    # before persisting/sending, and verify the quote is still fresh.
    final_clock = broker.clock()
    if not isinstance(final_clock, dict) or final_clock.get("is_open") is not True:
        raise _Blocked("Market closed during preparation; no order submitted.")
    final_time = _timestamp(final_clock.get("timestamp"), "final broker clock")
    final_local = now if injected_now else pd.Timestamp.now(tz="UTC")
    if (abs((final_local - final_time).total_seconds()) > 120
            or not opening <= final_time < min(opening + pd.Timedelta(minutes=15), closing)
            or not 0 <= (final_time - pd.Timestamp(quote_time)).total_seconds() <= 120):
        raise _Blocked("Opening window or quote freshness changed during preparation.")
    timestamp = final_time.isoformat()
    db.execute("""INSERT INTO intents(client_order_id,account_id,symbol,session,side,qty,
               reference_price,status,snapshot_sha,config_json,created_at,updated_at)
               VALUES(?,?,?,?,?,?,?,'submitting',?,?,?,?)""",
               (client_order_id, account["id"], config.symbol, today.isoformat(), side, qty,
                price, signal["snapshot_sha"], json.dumps(config_dict(config), allow_nan=False), timestamp, timestamp))
    db.commit()  # Durable BEFORE sending. A crash here leaves query-only recovery.
    intent = dict(db.execute("SELECT * FROM intents WHERE client_order_id=?", (client_order_id,)).fetchone())
    try:
        response = broker.submit_order(symbol=config.symbol, qty=qty, side=side, client_order_id=client_order_id)
        _record_order(db, response, intent, timestamp)
    except Exception:
        # Response loss, malformed acknowledgement, and all POST failures stay
        # ambiguous. Never serialize exception text (it may contain credentials).
        db.execute("UPDATE intents SET status='unknown',last_error=?,updated_at=? WHERE client_order_id=?",
                   ("Submission outcome unknown; query broker before manual resolution.", timestamp, client_order_id))
        db.commit()
        result.update(action="unknown", reason="Order acknowledgement unavailable; saved intent will only be queried.")
        return result
    result.update(action="submitted", reason="PAPER market/day order accepted; later iterations reconcile fills.")
    result["order"]["status"] = response["status"]
    return result
