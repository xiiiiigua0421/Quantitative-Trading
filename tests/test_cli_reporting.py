"""End-to-end local workflows remain usable without brokerage credentials."""

import json

import pandas as pd
import pytest
import yaml

from quant_trading.backtest import run_backtest
from quant_trading.cli import main
from quant_trading.config import load_config
from quant_trading.data import load_bars, save_bars


@pytest.fixture
def settings(tmp_path):
    path = tmp_path / "demo.yaml"
    path.write_text(yaml.safe_dump({"symbol": "DEMO", "data_path": "demo.csv",
                                   "output_dir": "report", "state_path": "paper.sqlite3"}), encoding="utf-8")
    return path


def test_demo_builds_traceable_outputs_without_credentials(settings, monkeypatch, capsys):
    monkeypatch.delenv("APCA_API_KEY_ID", raising=False)
    monkeypatch.delenv("APCA_API_SECRET_KEY", raising=False)
    assert main(["demo", "--config", str(settings)]) == 0
    result = json.loads(capsys.readouterr().out)
    report = settings.parent / "report"
    assert result["metrics"]["trade_count"] > 0
    assert {p.name for p in report.iterdir()} == {"report.html", "metrics.json", "manifest.json",
                                               "equity.csv", "trades.csv", "signals.csv"}
    text = (report / "report.html").read_text(encoding="utf-8")
    assert "合成行情 · 功能驗證" in text
    assert "<svg" in text
    assert "nan," not in text.lower()
    manifest = json.loads((report / "manifest.json").read_text())
    assert manifest["data_sha256"] == load_bars(settings.parent / "demo.csv").attrs["sha256"]
    assert "APCA_API_SECRET_KEY" not in json.dumps(manifest)


def test_csv_parquet_round_trip_preserves_backtest_cash_and_fills(settings, capsys):
    assert main(["demo", "--config", str(settings)]) == 0
    capsys.readouterr()
    config = load_config(settings)
    original = load_bars(config.data_path, expected_symbol="DEMO")
    parquet = settings.parent / "demo.parquet"
    save_bars(original, parquet, original.attrs["metadata"])
    restored = load_bars(parquet, expected_symbol="DEMO")
    a, b = run_backtest(original, config), run_backtest(restored, config)
    pd.testing.assert_frame_equal(a.equity, b.equity)
    pd.testing.assert_frame_equal(a.trades, b.trades)
    assert a.metrics == b.metrics


def test_stop_resume_state_works_without_account_and_persists(settings, capsys):
    assert main(["stop", "--config", str(settings)]) == 0
    assert json.loads(capsys.readouterr().out)["stopped"] is True
    assert main(["status", "--config", str(settings)]) == 0
    assert json.loads(capsys.readouterr().out)["stopped"] is True
    assert main(["resume", "--config", str(settings)]) == 0
    assert json.loads(capsys.readouterr().out)["stopped"] is False


def test_missing_credentials_returns_clean_error_without_traceback(settings, monkeypatch, capsys):
    monkeypatch.delenv("APCA_API_KEY_ID", raising=False)
    monkeypatch.delenv("APCA_API_SECRET_KEY", raising=False)
    assert main(["paper", "run", "--config", str(settings)]) == 2
    error = capsys.readouterr().err
    assert "PAPER" in error and "Traceback" not in error


def test_demo_rejects_real_symbol_before_overwriting_source(settings, capsys):
    settings.write_text(yaml.safe_dump({"symbol": "SPY", "data_path": "real.csv"}), encoding="utf-8")
    source = settings.parent / "real.csv"
    source.write_text("user data", encoding="utf-8")
    assert main(["demo", "--config", str(settings)]) == 2
    assert source.read_text(encoding="utf-8") == "user data"
