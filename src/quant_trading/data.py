"""Raw daily OHLCV with explicit US session dates and corporate-action fields."""

from __future__ import annotations

import hashlib
import json
import math
import re
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import exchange_calendars as xcals
import numpy as np
import pandas as pd

PRICE_COLUMNS = ["open", "high", "low", "close"]
REQUIRED_COLUMNS = PRICE_COLUMNS + ["volume"]


def sessions(start, end) -> pd.DatetimeIndex:
    calendar = xcals.get_calendar("XNYS", start=pd.Timestamp(start) - pd.Timedelta(days=7),
                                 end=pd.Timestamp(end) + pd.Timedelta(days=7))
    result = calendar.sessions_in_range(pd.Timestamp(start), pd.Timestamp(end))
    return result.tz_localize(None) if result.tz is not None else result


def validate_bars(frame: pd.DataFrame, *, validate_calendar: bool = True) -> pd.DataFrame:
    if not isinstance(frame, pd.DataFrame) or frame.empty:
        raise ValueError("行情資料不得為空。")
    frame = frame.copy()
    frame.columns = [str(x).strip().lower() for x in frame.columns]
    if len(set(frame.columns)) != len(frame.columns):
        raise ValueError("行情欄位不得重複。")
    if "date" in frame.columns:
        values = frame.pop("date")
        if not all(re.fullmatch(r"\d{4}-\d{2}-\d{2}", str(x)) for x in values):
            raise ValueError("date 必須為 YYYY-MM-DD 美股交易日，不接受時間戳。")
        try:
            frame.index = pd.DatetimeIndex(pd.to_datetime(values, format="%Y-%m-%d", errors="raise"))
        except (ValueError, TypeError) as exc:
            raise ValueError("date 含有無效日期。") from exc
    if not isinstance(frame.index, pd.DatetimeIndex) or frame.index.tz is not None:
        raise ValueError("行情索引必須是無時區的美股交易日。")
    if frame.index.hasnans or not (frame.index == frame.index.normalize()).all():
        raise ValueError("行情索引必須是完整交易日，不能包含時間或空值。")
    if not frame.index.is_unique:
        raise ValueError("行情包含重複交易日。")
    if not frame.index.is_monotonic_increasing:
        raise ValueError("行情日期必須嚴格遞增；不會自動排序。")
    frame.index.name = "date"
    if missing := set(REQUIRED_COLUMNS) - set(frame.columns):
        raise ValueError(f"行情缺少欄位：{', '.join(sorted(missing))}")
    for col in REQUIRED_COLUMNS:
        frame[col] = pd.to_numeric(frame[col], errors="coerce")
        if not np.isfinite(frame[col].to_numpy(dtype=float)).all():
            raise ValueError(f"{col} 含有空值、無限值或非數字。")
    if (frame[PRICE_COLUMNS] <= 0).any().any():
        raise ValueError("OHLC 價格必須大於 0。")
    if ((frame.high < frame[PRICE_COLUMNS].max(axis=1)) |
            (frame.low > frame[PRICE_COLUMNS].min(axis=1))).any():
        raise ValueError("OHLC 範圍錯誤：high 必須最高、low 必須最低。")
    if (frame.volume < 0).any() or (frame.volume % 1 != 0).any():
        raise ValueError("volume 必須是非負整數。")
    for col, default in (("split_ratio", 1.0), ("dividend", 0.0)):
        if col not in frame:
            frame[col] = default
        frame[col] = pd.to_numeric(frame[col], errors="coerce")
        if not np.isfinite(frame[col].to_numpy(dtype=float)).all():
            raise ValueError(f"{col} 必須是有限數值。")
    if (frame.split_ratio <= 0).any() or (frame.dividend < 0).any():
        raise ValueError("split_ratio 必須大於 0；dividend 不得為負數。")
    if "dividend_pay_date" not in frame:
        frame["dividend_pay_date"] = ""
    frame["dividend_pay_date"] = frame.dividend_pay_date.fillna("").astype(str)
    warnings = list(frame.attrs.get("warnings", []))
    for session, row in frame.iterrows():
        value = row.dividend_pay_date
        if value:
            try:
                if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
                    raise ValueError()
                pay = pd.Timestamp(date.fromisoformat(value))
            except ValueError as exc:
                raise ValueError("dividend_pay_date 必須是 YYYY-MM-DD 或空白。") from exc
            if pay < session:
                raise ValueError("股息支付日不能早於除息日。")
        elif row.dividend > 0:
            warnings.append(f"{session.date()} 股息缺少支付日，僅列應收股息，不提早作為可用現金。")
    if validate_calendar:
        expected = sessions(frame.index[0], frame.index[-1])
        invalid = frame.index.difference(expected)
        if len(invalid):
            raise ValueError(f"資料包含非 XNYS 交易日：{', '.join(str(x.date()) for x in invalid[:5])}")
        absent = expected.difference(frame.index)
        if len(absent):
            warnings.append(f"區間內缺少 {len(absent)} 個交易日；可能為停牌或資料缺漏，不自動補值。")
    frame.attrs["warnings"] = list(dict.fromkeys(warnings))
    return frame


def metadata_path(path: str | Path) -> Path:
    return Path(str(path) + ".meta.json")


def load_bars(path: str | Path, expected_symbol: str | None = None,
              validate_calendar: bool = True) -> pd.DataFrame:
    path = Path(path)
    if path.suffix.lower() == ".csv":
        frame = pd.read_csv(path)
    elif path.suffix.lower() in {".parquet", ".pq"}:
        frame = pd.read_parquet(path)
    else:
        raise ValueError("行情格式必須是 .csv 或 .parquet。")
    meta_file = metadata_path(path)
    if meta_file.exists():
        metadata = json.loads(meta_file.read_text(encoding="utf-8"))
        if not isinstance(metadata, dict):
            raise ValueError("行情 metadata 必須是 JSON object。")
        if metadata.get("adjustment", "raw") != "raw":
            raise ValueError("回測只接受 raw OHLC；復權價格不能用作實際成交價。")
        if expected_symbol and metadata.get("symbol") != expected_symbol:
            raise ValueError("設定的 symbol 與資料 metadata 不一致。")
        if checksum := metadata.get("sha256"):
            if hashlib.sha256(path.read_bytes()).hexdigest() != checksum:
                raise ValueError("資料檔雜湊與 metadata 不一致，請重新匯入資料。")
    else:
        metadata = {"symbol": expected_symbol, "source": "unverified_csv", "adjustment": "raw",
                    "corporate_actions_complete": False, "synthetic": False}
        frame.attrs["warnings"] = ["資料沒有來源 metadata；原始價格與公司行動完整性尚未驗證。"]
    source_warnings = metadata.get("warnings", [])
    if not isinstance(source_warnings, list) or not all(isinstance(value, str) for value in source_warnings):
        raise ValueError("metadata warnings 必須是文字陣列。")
    frame.attrs["warnings"] = list(frame.attrs.get("warnings", [])) + source_warnings
    if "symbol" in frame:
        supplied = set(frame.symbol.dropna().astype(str))
        if len(supplied) != 1 or (expected_symbol and supplied != {expected_symbol}):
            raise ValueError("資料包含多個標的或與設定的 symbol 不一致。")
    frame = validate_bars(frame, validate_calendar=validate_calendar)
    frame.attrs["metadata"] = metadata
    frame.attrs["sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
    if metadata.get("synthetic"):
        frame.attrs["warnings"].append("合成行情：只驗證程式流程，不代表真實市場績效。")
    if not metadata.get("corporate_actions_complete", False):
        frame.attrs["warnings"].append("公司行動未完整驗證，績效可能缺少股息、拆股或其他事件。")
    return frame


def save_bars(frame: pd.DataFrame, path: str | Path, metadata: dict) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    frame = validate_bars(frame)
    if path.suffix.lower() == ".csv":
        frame.to_csv(path, date_format="%Y-%m-%d", index=True)
    elif path.suffix.lower() in {".parquet", ".pq"}:
        frame.to_parquet(path, index=True)
    else:
        raise ValueError("輸出必須是 .csv 或 .parquet。")
    metadata = dict(metadata)
    metadata["sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
    metadata_path(path).write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def create_demo(path: str | Path) -> Path:
    dates = sessions("2024-01-02", "2025-12-31")
    rows = []
    for i, session in enumerate(dates):
        divisor = 2.0 if i >= 250 else 1.0
        base = 100 + 0.05 * i + 10 * math.sin(i / 23) + 3 * math.sin(i / 7)
        close = round(base / divisor, 4)
        opening = round((base + 0.4 * math.sin(i * 1.7)) / divisor, 4)
        dividend = 0.4 if i in (180, 400) else 0.0
        rows.append({"date": str(session.date()), "open": opening,
                     "high": round(max(opening, close) + 0.9 / divisor, 4),
                     "low": round(min(opening, close) - 0.9 / divisor, 4), "close": close,
                     "volume": 1000000 + (i * 7919) % 500000,
                     "split_ratio": 2.0 if i == 250 else 1.0,
                     "dividend": dividend,
                     "dividend_pay_date": str(dates[i + 5].date()) if dividend else ""})
    frame = validate_bars(pd.DataFrame(rows))
    return save_bars(frame, path, {"symbol": "DEMO", "source": "deterministic_synthetic",
                                 "feed": "synthetic", "adjustment": "raw", "synthetic": True,
                                 "corporate_actions_complete": True,
                                 "description": "Engineering fixture with two dividends and a 2:1 split; not market data."})


def fetch_alpaca(broker, *, symbol: str, start: str, end: str,
                 feed: str = "iex", output: str | Path) -> Path:
    """Download completed raw daily bars; never silently promise CA completeness."""
    if not isinstance(symbol, str) or not re.fullmatch(r"[A-Z][A-Z0-9.\-]{0,14}", symbol):
        raise ValueError("下載必須指定單一美股代碼。")
    if feed not in {"iex", "sip"}:
        raise ValueError("feed 必須是 iex 或 sip。")
    start_day, end_day = date.fromisoformat(start), date.fromisoformat(end)
    if start_day > end_day:
        raise ValueError("start 不得晚於 end。")
    # Exclude the current New York session even if it has closed: avoid evolving daily aggregates.
    from zoneinfo import ZoneInfo
    today_ny = datetime.now(ZoneInfo("America/New_York")).date()
    if end_day >= today_ny:
        raise ValueError("下載 end 必須早於紐約今日，避免使用尚未穩定的當日 K 線。")
    params = {"symbols": symbol, "timeframe": "1Day", "start": start,
              "end": f"{end}T23:59:59-04:00", "feed": feed, "adjustment": "raw",
              "sort": "asc", "limit": 10000}
    # A date-only end is midnight; use the next UTC day to include all bars for end session.
    params["end"] = f"{end_day + timedelta(days=1)}T00:00:00Z"
    rows, seen_tokens = [], set()
    while True:
        response = broker.market_data("/v2/stocks/bars", params)
        for bar in response.get("bars", {}).get(symbol, []):
            session = pd.Timestamp(bar["t"]).tz_convert("America/New_York").date()
            if start_day <= session <= end_day:
                rows.append({"date": str(session), "open": bar["o"], "high": bar["h"],
                             "low": bar["l"], "close": bar["c"], "volume": bar["v"]})
        token = response.get("next_page_token")
        if not token:
            break
        if token in seen_tokens:
            raise ValueError("行情 API 重複回傳分頁 token。")
        seen_tokens.add(token)
        params["page_token"] = token
    frame = validate_bars(pd.DataFrame(rows))
    actions = []
    action_params = {"symbols": symbol, "start": str(start_day - timedelta(days=366)),
                     "end": str(today_ny), "limit": 1000, "sort": "asc"}
    seen_tokens = set()
    while True:
        response = broker.market_data("/v1/corporate-actions", action_params)
        grouped = response.get("corporate_actions")
        if not isinstance(grouped, dict):
            raise ValueError("公司行動 API 回應格式無效，無法建立可核對的行情資料。")
        for kind, events in grouped.items():
            actions.extend((kind, event) for event in events)
        token = response.get("next_page_token")
        if not token:
            break
        if token in seen_tokens:
            raise ValueError("公司行動 API 重複回傳分頁 token。")
        seen_tokens.add(token)
        action_params["page_token"] = token
    frame = overlay_actions(frame, actions, start_day=start_day, end_day=end_day,
                            expected_symbol=symbol)
    return save_bars(frame, output, {"symbol": symbol, "source": "alpaca", "feed": feed,
                                  "adjustment": "raw", "synthetic": False,
                                  "corporate_actions_complete": True,
                                  "corporate_action_count": len(actions),
                                  "retrieved_at": datetime.now(timezone.utc).isoformat(),
                                  "start": start, "end": end,
                                  "warnings": ["Provider corporate actions may arrive late; completeness is limited to this snapshot."]})


def overlay_actions(frame: pd.DataFrame, actions: list, *, start_day: date,
                    end_day: date, expected_symbol: str | None = None) -> pd.DataFrame:
    """Overlay supported events; reject rather than silently ignore unsupported economics."""
    frame = frame.copy()
    seen = set()
    for kind, event in actions:
        identity = (kind, event.get("id") or json.dumps(event, sort_keys=True))
        if identity in seen:
            continue
        seen.add(identity)
        value = event.get("ex_date") or event.get("effective_date") or event.get("process_date")
        if not value:
            raise ValueError("公司行動缺少生效日期。")
        effective = date.fromisoformat(str(value))
        if not start_day <= effective <= end_day:
            continue
        if kind not in {"forward_splits", "reverse_splits", "cash_dividends"}:
            raise ValueError(f"第一版尚未支援公司行動 {kind}（{effective}）；請選擇其他測試區間。")
        if expected_symbol is not None and event.get("symbol") != expected_symbol:
            raise ValueError("公司行動的 symbol 與下載標的不一致。")
        effective_stamp = pd.Timestamp(effective)
        if effective_stamp not in frame.index:
            raise ValueError(f"公司行動 {effective} 缺少當日行情，請補齊資料後回測。")
        if kind in {"forward_splits", "reverse_splits"}:
            old, new = float(event["old_rate"]), float(event["new_rate"])
            if not (math.isfinite(old) and math.isfinite(new) and old > 0 and new > 0):
                raise ValueError("公司行動拆股比例無效。")
            frame.loc[effective_stamp, "split_ratio"] *= new / old
        else:
            if event.get("foreign", False):
                raise ValueError("第一版尚未支援外幣股息與預扣稅模型。")
            rate = float(event["rate"])
            if not math.isfinite(rate) or rate < 0:
                raise ValueError("公司行動股息金額無效。")
            payout = str(event.get("payable_date") or "")
            old_payout = frame.loc[effective_stamp, "dividend_pay_date"]
            if frame.loc[effective_stamp, "dividend"] > 0 and old_payout != payout:
                raise ValueError("同日多筆股息有不同支付日，第一版無法以單列資料表達。")
            frame.loc[effective_stamp, "dividend"] += rate
            frame.loc[effective_stamp, "dividend_pay_date"] = payout
    return validate_bars(frame)
