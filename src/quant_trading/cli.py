"""Command-line entry points; paper execution defaults to preview."""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd

from . import __version__
from .backtest import run_backtest
from .config import load_config
from .data import create_demo, fetch_alpaca, load_bars, save_bars
from .reporting import write_report


def _config_option(parser):
    parser.add_argument("--config", default=argparse.SUPPRESS, help="YAML 設定檔（預設 configs/demo.yaml）")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="qt", description="美股日線回測與模擬交易工具")
    parser.add_argument("--version", action="version", version=f"quant-trading {__version__}")
    _config_option(parser)
    parser.set_defaults(config="configs/demo.yaml")
    commands = parser.add_subparsers(dest="command", required=True)
    demo = commands.add_parser("demo", help="建立合成行情並產生範例回測報告")
    _config_option(demo)
    demo.add_argument("--output", type=Path)
    data = commands.add_parser("data", help="行情下載、匯入與驗證")
    sub = data.add_subparsers(dest="data_command", required=True)
    validate = sub.add_parser("validate", help="檢查設定中的資料檔")
    _config_option(validate)
    imp = sub.add_parser("import", help="匯入 raw CSV／Parquet 與公司行動欄位")
    _config_option(imp)
    imp.add_argument("--input", required=True, type=Path)
    imp.add_argument("--actions-complete", action="store_true", help="宣告已核對此區間所有拆股／股息與其他公司行動")
    fetch = sub.add_parser("fetch", help="由 Alpaca 下載已完成日線與支援的公司行動")
    _config_option(fetch)
    fetch.add_argument("--start", required=True, help="YYYY-MM-DD，含此日")
    fetch.add_argument("--end", required=True, help="YYYY-MM-DD，含此日，須早於紐約今日")
    bt = commands.add_parser("backtest", help="回測設定中的策略與資料")
    _config_option(bt)
    bt.add_argument("--start")
    bt.add_argument("--end")
    bt.add_argument("--output", type=Path)
    paper = commands.add_parser("paper", help="Alpaca 模擬交易；無實盤功能")
    paper_sub = paper.add_subparsers(dest="paper_command", required=True)
    run = paper_sub.add_parser("run", help="預覽或執行一次模擬交易決策")
    _config_option(run)
    run.add_argument("--submit", action="store_true", help="將訂單送到 Alpaca paper 環境；省略時只預覽")
    run.add_argument("--loop", action="store_true", help="持續運行，每次重新讀取資料並檢查狀態")
    run.add_argument("--interval", type=int, default=60, help="迴圈秒數，最少 30（預設 60）")
    run.add_argument("--refresh", action="store_true", help="跨交易日自動更新近一年行情；須有行情及公司行動 API 權限")
    status = commands.add_parser("status", help="查看本地模擬交易狀態")
    _config_option(status)
    stop = commands.add_parser("stop", help="持久保存停止旗標；封鎖新增訂單")
    _config_option(stop)
    resume = commands.add_parser("resume", help="解除停止旗標；下次執行仍需通過風控")
    _config_option(resume)
    return parser


def _print(payload):
    print(json.dumps(payload, ensure_ascii=False, indent=2, default=str, allow_nan=False))


def _backtest(config, output=None, start=None, end=None):
    bars = load_bars(config.data_path, expected_symbol=config.symbol)
    if start:
        start_date = pd.Timestamp(start)
        if start_date.tz is not None or start_date != start_date.normalize():
            raise ValueError("start 必須是 YYYY-MM-DD。")
        bars = bars.loc[bars.index >= start_date]
    if end:
        end_date = pd.Timestamp(end)
        if end_date.tz is not None or end_date != end_date.normalize():
            raise ValueError("end 必須是 YYYY-MM-DD。")
        bars = bars.loc[bars.index <= end_date]
    if bars.empty:
        raise ValueError("選定期間沒有行情資料。")
    result = run_backtest(bars, config)
    report = write_report(result, config, bars, output)
    _print({"report": str(report), "metrics": result.metrics, "warnings": result.warnings})
    return report


def _refresh(config, broker):
    today = datetime.now(ZoneInfo("America/New_York")).date()
    expected_days = broker.calendar(str(today - timedelta(days=14)), str(today - timedelta(days=1)))
    if not expected_days:
        raise ValueError("券商日曆沒有前一個已完成交易日。")
    latest = max(str(day["date"]) for day in expected_days)
    if config.data_path.exists():
        bars = load_bars(config.data_path, expected_symbol=config.symbol)
        if str(bars.index[-1].date()) == latest:
            return
    lookback = max(366, config.strategy.slow_window * 3)
    fetch_alpaca(broker, symbol=config.symbol, start=str(today - timedelta(days=lookback)),
                 end=str(today - timedelta(days=1)), feed=config.feed, output=config.data_path)


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    try:
        config = load_config(args.config)
        if args.command == "demo":
            if config.symbol != "DEMO":
                raise ValueError("demo 只能搭配 symbol: DEMO 的設定，避免覆蓋真實行情。")
            create_demo(config.data_path)
            _backtest(config, args.output)
        elif args.command == "data":
            if args.data_command == "validate":
                bars = load_bars(config.data_path, expected_symbol=config.symbol)
                _print({"symbol": config.symbol, "rows": len(bars), "start": bars.index[0],
                        "end": bars.index[-1], "metadata": bars.attrs["metadata"], "warnings": bars.attrs["warnings"]})
            elif args.data_command == "import":
                bars = load_bars(args.input, expected_symbol=config.symbol)
                metadata = dict(bars.attrs["metadata"])
                old_feed = metadata.get("feed")
                if old_feed and not metadata.get("synthetic") and old_feed != config.feed:
                    raise ValueError("匯入檔案的 feed 與設定不一致；請使用符合來源的設定。")
                metadata.update({"symbol": config.symbol, "source": "user_import", "feed": config.feed,
                                 "adjustment": "raw", "corporate_actions_complete": args.actions_complete,
                                 "imported_from": str(args.input.resolve())})
                save_bars(bars, config.data_path, metadata)
                _print({"data": str(config.data_path), "warnings": bars.attrs["warnings"]})
            else:
                from .broker import AlpacaPaperBroker
                path = fetch_alpaca(AlpacaPaperBroker(), symbol=config.symbol, start=args.start,
                                    end=args.end, feed=config.feed, output=config.data_path)
                _print({"data": str(path.resolve())})
        elif args.command == "backtest":
            _backtest(config, args.output, args.start, args.end)
        elif args.command == "paper":
            from .broker import AlpacaPaperBroker
            from .execution import paper_step
            if args.interval < 30:
                raise ValueError("interval 必須至少 30 秒。")
            broker = AlpacaPaperBroker()
            while True:
                try:
                    if args.refresh:
                        _refresh(config, broker)
                    bars = load_bars(config.data_path, expected_symbol=config.symbol)
                    _print(paper_step(bars, config, broker, submit=args.submit))
                except (ValueError, OSError, RuntimeError) as exc:
                    if not args.loop:
                        raise
                    _print({"action": "blocked", "reason": str(exc), "orders_submitted": False})
                if not args.loop:
                    break
                time.sleep(args.interval)
        elif args.command in {"status", "stop", "resume"}:
            from .execution import get_status, set_stopped
            _print(get_status(config.state_path) if args.command == "status" else
                   set_stopped(config.state_path, stopped=args.command == "stop"))
        return 0
    except KeyboardInterrupt:
        print("已結束程式；既有券商訂單與持倉不會自動撤銷或平倉。", file=sys.stderr)
        return 130
    except (ValueError, OSError, RuntimeError, KeyError, TypeError) as exc:
        print(f"錯誤：{exc}", file=sys.stderr)
        return 2
