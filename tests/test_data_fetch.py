"""Offline provider-response tests; no credentials, network, or orders are used."""

from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path

import pandas as pd
import pytest
import yaml

from quant_trading import cli
from quant_trading.data import fetch_alpaca, load_bars, metadata_path, save_bars


class ScriptedMarketData:
    """Return independently authored provider pages and retain request snapshots."""

    def __init__(self, *, bar_pages: list, action_pages: list):
        self.pages = {
            "/v2/stocks/bars": copy.deepcopy(bar_pages),
            "/v1/corporate-actions": copy.deepcopy(action_pages),
        }
        self.calls = []

    def market_data(self, path, params):
        self.calls.append((path, copy.deepcopy(params)))
        assert path in self.pages, f"Unexpected endpoint: {path}"
        assert self.pages[path], f"Unexpected extra request: {path}"
        return self.pages[path].pop(0)


def provider_bar(day: str, price: float = 100.0) -> dict:
    # July daily bars are timestamped at midnight New York / 04:00 UTC.
    return {"t": f"{day}T04:00:00Z", "o": price, "h": price + 2,
            "l": price - 1, "c": price + 1, "v": 1000}


def normal_bar_page() -> dict:
    return {"bars": {"TEST": [provider_bar(day) for day in
                              ["2024-07-01", "2024-07-02", "2024-07-03"]]},
            "next_page_token": None}


def download(broker, output: Path, **kwargs) -> Path:
    return fetch_alpaca(broker, symbol="TEST", start="2024-07-01", end="2024-07-03",
                        output=output, **kwargs)


def test_paginated_raw_bars_and_actions_make_a_reproducible_dataset(tmp_path):
    split = {"id": "split-1", "symbol": "TEST", "ex_date": "2024-07-02",
             "old_rate": 1, "new_rate": 2}
    dividend = {"id": "dividend-1", "symbol": "TEST", "ex_date": "2024-07-03",
                "rate": 0.5, "payable_date": "2024-07-08", "foreign": False}
    broker = ScriptedMarketData(
        bar_pages=[
            {"bars": {"TEST": [provider_bar("2024-06-28"), provider_bar("2024-07-01")]},
             "next_page_token": "bars-2"},
            {"bars": {"TEST": [provider_bar("2024-07-02", 50), provider_bar("2024-07-03", 51)]},
             "next_page_token": None},
        ],
        action_pages=[
            {"corporate_actions": {"forward_splits": [split], "cash_dividends": [dividend]},
             "next_page_token": "actions-2"},
            # The same event ID repeated across pages must not double the dividend.
            {"corporate_actions": {"cash_dividends": [dividend]}, "next_page_token": None},
        ],
    )
    path = download(broker, tmp_path / "download.csv", feed="sip")
    bars = load_bars(path, expected_symbol="TEST")

    assert list(bars.index.strftime("%Y-%m-%d")) == ["2024-07-01", "2024-07-02", "2024-07-03"]
    assert bars["open"].tolist() == [100.0, 50.0, 51.0]  # Raw execution prices stay raw.
    assert bars["split_ratio"].tolist() == [1.0, 2.0, 1.0]
    assert bars["dividend"].tolist() == [0.0, 0.0, 0.5]
    assert bars.loc["2024-07-03", "dividend_pay_date"] == "2024-07-08"
    bar_calls = [params for endpoint, params in broker.calls if endpoint == "/v2/stocks/bars"]
    action_calls = [params for endpoint, params in broker.calls if endpoint == "/v1/corporate-actions"]
    assert len(bar_calls) == len(action_calls) == 2
    assert all(params["adjustment"] == "raw" and params["feed"] == "sip" for params in bar_calls)
    assert bar_calls[0]["timeframe"] == "1Day"
    assert bar_calls[0]["end"] == "2024-07-04T00:00:00Z"
    assert "page_token" not in bar_calls[0]
    assert bar_calls[1]["page_token"] == "bars-2"
    assert action_calls[1]["page_token"] == "actions-2"
    metadata = json.loads(metadata_path(path).read_text(encoding="utf-8"))
    assert metadata["symbol"] == "TEST"
    assert metadata["feed"] == "sip"
    assert metadata["adjustment"] == "raw"
    assert metadata["synthetic"] is False
    assert metadata["sha256"] == hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.mark.parametrize("endpoint", ["bars", "actions"])
def test_repeated_pagination_token_fails_without_overwriting_previous_dataset(tmp_path, endpoint):
    bars = normal_bar_page()
    actions = {"corporate_actions": {}, "next_page_token": None}
    if endpoint == "bars":
        bars["next_page_token"] = "cycle"
        bar_pages, action_pages = [bars, bars], []
    else:
        actions["next_page_token"] = "cycle"
        bar_pages, action_pages = [bars], [actions, actions]
    broker = ScriptedMarketData(bar_pages=bar_pages, action_pages=action_pages)
    path = tmp_path / "existing.csv"
    path.write_text("previous dataset", encoding="utf-8")
    metadata_path(path).write_text("previous metadata", encoding="utf-8")

    with pytest.raises(ValueError):
        download(broker, path)
    assert path.read_text(encoding="utf-8") == "previous dataset"
    assert metadata_path(path).read_text(encoding="utf-8") == "previous metadata"


def test_unsupported_company_action_cannot_silently_produce_complete_data(tmp_path):
    broker = ScriptedMarketData(
        bar_pages=[normal_bar_page()],
        action_pages=[{"corporate_actions": {"stock_dividends": [
            {"id": "stock-dividend", "symbol": "TEST", "ex_date": "2024-07-02"}
        ]}, "next_page_token": None}],
    )
    path = tmp_path / "unsupported.csv"
    with pytest.raises(ValueError):
        download(broker, path)
    assert not path.exists()
    assert not metadata_path(path).exists()


def test_corporate_action_for_other_symbol_cannot_contaminate_prices(tmp_path):
    broker = ScriptedMarketData(
        bar_pages=[normal_bar_page()],
        action_pages=[{"corporate_actions": {"forward_splits": [
            {"id": "wrong-company", "symbol": "OTHER", "ex_date": "2024-07-02",
             "old_rate": 1, "new_rate": 10}
        ]}, "next_page_token": None}],
    )
    path = tmp_path / "wrong-company.csv"
    with pytest.raises(ValueError):
        download(broker, path)
    assert not path.exists()


def test_empty_bars_fail_without_claiming_successful_download(tmp_path):
    broker = ScriptedMarketData(
        bar_pages=[{"bars": {}, "next_page_token": None}], action_pages=[]
    )
    path = tmp_path / "empty.csv"
    with pytest.raises(ValueError):
        download(broker, path)
    assert not path.exists()


def test_missing_action_response_cannot_be_labeled_complete(tmp_path):
    broker = ScriptedMarketData(bar_pages=[normal_bar_page()], action_pages=[{}])
    path = tmp_path / "missing-actions.csv"
    with pytest.raises(ValueError):
        download(broker, path)
    assert not path.exists()


@pytest.mark.parametrize(
    "start,end",
    [("2024-07-03", "2024-07-01"), ("2999-01-01", "2999-01-02")],
)
def test_invalid_or_future_download_range_is_rejected_before_broker_request(tmp_path, start, end):
    broker = ScriptedMarketData(bar_pages=[], action_pages=[])
    with pytest.raises(ValueError):
        fetch_alpaca(broker, symbol="TEST", start=start, end=end, output=tmp_path / "bad.csv")
    assert broker.calls == []


def test_fetch_rejects_unknown_feed_before_broker_request(tmp_path):
    broker = ScriptedMarketData(bar_pages=[], action_pages=[])
    with pytest.raises(ValueError):
        download(broker, tmp_path / "bad-feed.csv", feed="delayed_sip")
    assert broker.calls == []


def test_fetch_is_single_symbol_even_when_called_without_cli(tmp_path):
    broker = ScriptedMarketData(bar_pages=[], action_pages=[])
    with pytest.raises(ValueError):
        fetch_alpaca(broker, symbol="TEST,OTHER", start="2024-07-01", end="2024-07-03",
                     output=tmp_path / "multiple-symbols.csv")
    assert broker.calls == []


def test_dataset_modification_is_detected_even_when_csv_is_still_valid(tmp_path):
    broker = ScriptedMarketData(
        bar_pages=[normal_bar_page()],
        action_pages=[{"corporate_actions": {}, "next_page_token": None}],
    )
    path = download(broker, tmp_path / "download.csv")
    with path.open("a", encoding="utf-8") as handle:
        handle.write("\n")
    with pytest.raises(ValueError):
        load_bars(path, expected_symbol="TEST")


def test_provider_snapshot_caveat_remains_visible_to_backtest_reporting(tmp_path):
    broker = ScriptedMarketData(
        bar_pages=[normal_bar_page()],
        action_pages=[{"corporate_actions": {}, "next_page_token": None}],
    )
    bars = load_bars(download(broker, tmp_path / "download.csv"), expected_symbol="TEST")
    provider_warnings = bars.attrs["metadata"]["warnings"]
    assert provider_warnings
    assert all(warning in bars.attrs["warnings"] for warning in provider_warnings)


def test_cli_import_rejects_feed_mismatch_and_preserves_matching_source_feed(tmp_path):
    original = tmp_path / "sip.csv"
    bars = pd.DataFrame(
        {"open": [100], "high": [102], "low": [99], "close": [101], "volume": [1000]},
        index=pd.DatetimeIndex(["2024-07-01"], name="date"),
    )
    save_bars(bars, original, {"symbol": "TEST", "source": "alpaca", "feed": "sip",
                               "adjustment": "raw", "synthetic": False,
                               "corporate_actions_complete": True})
    imported = tmp_path / "imported.csv"
    config_path = tmp_path / "settings.yaml"
    config_path.write_text(yaml.safe_dump({"symbol": "TEST", "feed": "iex",
                                           "data_path": str(imported)}), encoding="utf-8")

    args = ["data", "import", "--config", str(config_path),
            "--input", str(original), "--actions-complete"]
    assert cli.main(args) == 2
    assert not imported.exists()
    config_path.write_text(yaml.safe_dump({"symbol": "TEST", "feed": "sip",
                                           "data_path": str(imported)}), encoding="utf-8")
    assert cli.main(args) == 0
    metadata = json.loads(metadata_path(imported).read_text(encoding="utf-8"))
    assert metadata["feed"] == "sip"
