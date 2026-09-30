"""Reject ambiguous inputs before they can produce plausible backtest results."""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd
import pytest
import yaml

from quant_trading.config import load_config
from quant_trading.data import load_bars, validate_bars


@pytest.fixture
def valid_bars() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "open": [100.0, 101.0, 102.0],
            "high": [102.0, 103.0, 104.0],
            "low": [99.0, 100.0, 101.0],
            "close": [101.0, 102.0, 103.0],
            "volume": [1000, 1500, 1200],
        },
        index=pd.DatetimeIndex(["2024-07-01", "2024-07-02", "2024-07-03"], name="date"),
    )


def write_bars(tmp_path: Path, bars: pd.DataFrame, *, metadata: dict | None = None) -> Path:
    path = tmp_path / "bars.csv"
    bars.to_csv(path, index_label="date", date_format="%Y-%m-%d")
    if metadata is not None:
        Path(str(path) + ".meta.json").write_text(json.dumps(metadata), encoding="utf-8")
    return path


@pytest.fixture
def raw_metadata() -> dict:
    return {
        "symbol": "TEST",
        "source": "test-fixture",
        "feed": "iex",
        "adjustment": "raw",
        "synthetic": False,
    }


def write_config(tmp_path: Path, values: dict) -> Path:
    path = tmp_path / "settings.yaml"
    path.write_text(yaml.safe_dump(values), encoding="utf-8")
    return path


def test_valid_daily_bars_preserve_prices_and_have_neutral_action_defaults(valid_bars):
    bars = validate_bars(valid_bars)

    pd.testing.assert_frame_equal(bars[list(valid_bars.columns)], valid_bars, check_dtype=False)
    assert isinstance(bars.index, pd.DatetimeIndex)
    assert bars.index.tz is None
    assert (bars["split_ratio"] == 1).all()
    assert (bars["dividend"] == 0).all()


def test_csv_with_raw_metadata_loads_for_requested_symbol(tmp_path, valid_bars, raw_metadata):
    path = write_bars(tmp_path, valid_bars, metadata=raw_metadata)
    bars = load_bars(path, expected_symbol="TEST")

    pd.testing.assert_index_equal(bars.index, valid_bars.index, check_names=False)
    np.testing.assert_allclose(bars["close"], [101, 102, 103])


def test_offline_csv_without_metadata_warns_about_unknown_provenance(tmp_path, valid_bars):
    bars = load_bars(write_bars(tmp_path, valid_bars), expected_symbol="TEST")
    assert bars.attrs.get("warnings")


def test_symbol_mismatch_is_not_silently_accepted(tmp_path, valid_bars, raw_metadata):
    path = write_bars(tmp_path, valid_bars, metadata={**raw_metadata, "symbol": "OTHER"})
    with pytest.raises(ValueError):
        load_bars(path, expected_symbol="TEST")


@pytest.mark.parametrize("adjustment", ["split", "dividend", "all"])
def test_adjusted_prices_are_rejected_before_action_accounting(
    tmp_path, valid_bars, raw_metadata, adjustment
):
    path = write_bars(tmp_path, valid_bars, metadata={**raw_metadata, "adjustment": adjustment})
    with pytest.raises(ValueError):
        load_bars(path, expected_symbol="TEST")


def test_duplicate_sessions_are_rejected_instead_of_deduplicated(valid_bars):
    duplicate = pd.concat([valid_bars.iloc[:1], valid_bars])
    with pytest.raises(ValueError):
        validate_bars(duplicate)


def test_out_of_order_sessions_are_rejected_instead_of_sorted(valid_bars):
    with pytest.raises(ValueError):
        validate_bars(valid_bars.iloc[[1, 0, 2]])


@pytest.mark.parametrize("column", ["open", "high", "low", "close"])
@pytest.mark.parametrize("value", [0, -1, np.nan, np.inf, -np.inf])
def test_invalid_ohlc_cannot_enter_backtest(valid_bars, column, value):
    valid_bars.loc[valid_bars.index[0], column] = value
    with pytest.raises(ValueError):
        validate_bars(valid_bars)


@pytest.mark.parametrize(
    "changes",
    [
        {"high": 100.5},  # Below the closing trade.
        {"low": 100.5},  # Above the opening trade.
        {"low": 104.0},  # Entire range is inverted.
    ],
)
def test_inconsistent_price_ranges_are_rejected(valid_bars, changes):
    for column, value in changes.items():
        valid_bars.loc[valid_bars.index[0], column] = value
    with pytest.raises(ValueError):
        validate_bars(valid_bars)


@pytest.mark.parametrize("value", [-1, 1.5, np.nan, np.inf])
def test_volume_must_be_finite_nonnegative_whole_shares(valid_bars, value):
    valid_bars["volume"] = valid_bars["volume"].astype(float)
    valid_bars.loc[valid_bars.index[0], "volume"] = value
    with pytest.raises(ValueError):
        validate_bars(valid_bars)


def test_zero_volume_is_valid_data(valid_bars):
    valid_bars.loc[valid_bars.index[0], "volume"] = 0
    assert validate_bars(valid_bars).iloc[0]["volume"] == 0


def test_missing_required_price_is_rejected(valid_bars):
    with pytest.raises(ValueError):
        validate_bars(valid_bars.drop(columns="close"))


def test_independence_day_is_not_a_us_equity_session(valid_bars):
    valid_bars.index = pd.DatetimeIndex(["2024-07-01", "2024-07-02", "2024-07-04"], name="date")
    with pytest.raises(ValueError):
        validate_bars(valid_bars)

    assert len(validate_bars(valid_bars, validate_calendar=False)) == 3


@pytest.mark.parametrize(
    "dates",
    [
        pd.DatetimeIndex(["2024-07-01 12:00", "2024-07-02 12:00", "2024-07-03 12:00"]),
        pd.DatetimeIndex(["2024-07-01", "2024-07-02", "2024-07-03"], tz="UTC"),
    ],
)
def test_normalized_bar_dates_do_not_silently_discard_time_or_timezone(valid_bars, dates):
    valid_bars.index = dates
    with pytest.raises(ValueError):
        validate_bars(valid_bars)


@pytest.mark.parametrize("date_value", ["2024-07-01T16:00:00", "2024-07-01T00:00:00Z"])
def test_csv_dates_must_be_dates_not_timestamps(tmp_path, valid_bars, date_value):
    rows = valid_bars.reset_index()
    rows["date"] = rows["date"].dt.strftime("%Y-%m-%d")
    rows.loc[0, "date"] = date_value
    path = tmp_path / "timestamps.csv"
    rows.to_csv(path, index=False)
    with pytest.raises(ValueError):
        load_bars(path)


@pytest.mark.parametrize("value", [0, -2, np.nan, np.inf])
def test_split_ratio_must_be_finite_and_positive(valid_bars, value):
    valid_bars["split_ratio"] = [1.0, value, 1.0]
    with pytest.raises(ValueError):
        validate_bars(valid_bars)


@pytest.mark.parametrize("value", [-1, np.nan, np.inf])
def test_dividend_must_be_finite_and_nonnegative(valid_bars, value):
    valid_bars["dividend"] = [0.0, value, 0.0]
    with pytest.raises(ValueError):
        validate_bars(valid_bars)


def test_unknown_dividend_payment_date_is_explicit_warning(
    tmp_path, valid_bars, raw_metadata
):
    valid_bars["dividend"] = [0.0, 0.5, 0.0]
    path = write_bars(tmp_path, valid_bars, metadata=raw_metadata)
    bars = load_bars(path, expected_symbol="TEST")

    assert bars.loc["2024-07-02", "dividend"] == 0.5
    assert bars.attrs.get("warnings")


@pytest.mark.parametrize("pay_date", ["2024-07-01", "not-a-date", "2024-07-08T12:00:00"])
def test_dividend_pay_date_cannot_precede_ex_date_or_contain_a_timestamp(valid_bars, pay_date):
    valid_bars["dividend"] = [0.0, 0.5, 0.0]
    valid_bars["dividend_pay_date"] = ["", pay_date, ""]
    with pytest.raises(ValueError):
        validate_bars(valid_bars)


def test_explicit_split_and_future_dividend_payment_are_preserved(valid_bars):
    valid_bars["split_ratio"] = [1.0, 2.0, 1.0]
    valid_bars["dividend"] = [0.0, 0.5, 0.0]
    valid_bars["dividend_pay_date"] = ["", "2024-07-08", ""]
    bars = validate_bars(valid_bars)

    assert bars.loc["2024-07-02", "split_ratio"] == 2.0
    assert bars.loc["2024-07-02", "dividend"] == 0.5
    assert pd.Timestamp(bars.loc["2024-07-02", "dividend_pay_date"]) == pd.Timestamp("2024-07-08")


def test_config_paths_are_relative_to_yaml_directory(tmp_path, monkeypatch):
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    path = write_config(
        config_dir,
        {
            "symbol": "TEST",
            "data_path": "../input/bars.csv",
            "output_dir": "../output",
            "state_path": "../state/paper.sqlite3",
        },
    )
    other_directory = tmp_path / "unrelated-working-directory"
    other_directory.mkdir()
    monkeypatch.chdir(other_directory)
    config = load_config(path)

    assert config.data_path == (tmp_path / "input/bars.csv").resolve()
    assert config.output_dir == (tmp_path / "output").resolve()
    assert config.state_path == (tmp_path / "state/paper.sqlite3").resolve()


@pytest.mark.parametrize(
    "values",
    [
        {"unexpected": 1},
        {"strategy": {"lookahead": True}},
        {"costs": {"fee": 1}},
        {"risk": {"leverage": 2}},
    ],
)
def test_unknown_config_fields_fail_instead_of_using_a_typo(values, tmp_path):
    with pytest.raises(ValueError):
        load_config(write_config(tmp_path, values))


@pytest.mark.parametrize(
    "values",
    [
        {"strategy": {"fast_window": 2.5}},
        {"strategy": {"fast_window": True}},
        {"strategy": {"slow_window": 60.0}},
        {"strategy": {"fast_window": 0}},
        {"strategy": {"fast_window": 60, "slow_window": 60}},
        {"strategy": {"fast_window": 61, "slow_window": 60}},
        {"initial_cash": 0},
        {"initial_cash": np.nan},
        {"initial_cash": np.inf},
        {"initial_cash": True},
        {"costs": {"commission_bps": -1}},
        {"costs": {"slippage_bps": np.nan}},
        {"costs": {"slippage_bps": 10000}},
        {"risk": {"max_order_notional": np.inf}},
        {"risk": {"max_order_notional": 0}},
        {"risk": {"max_drawdown": np.nan}},
        {"risk": {"max_drawdown": 0}},
        {"risk": {"max_position_fraction": 1.1}},
        {"risk": {"target_fraction": 0.9, "max_position_fraction": 0.8}},
        {"risk": {"cash_buffer_fraction": 1}},
        {"feed": "unrecognized"},
        {"symbol": "test"},
        {"data_path": ""},
        {"risk": []},
    ],
)
def test_invalid_settings_fail_before_backtest(tmp_path, values):
    with pytest.raises(ValueError):
        load_config(write_config(tmp_path, values))


def test_yaml_root_must_be_a_mapping(tmp_path):
    path = tmp_path / "settings.yaml"
    path.write_text("- TEST\n- OTHER\n", encoding="utf-8")
    with pytest.raises(ValueError):
        load_config(path)


def test_valid_config_retains_feed_and_strategy(tmp_path):
    config = load_config(
        write_config(
            tmp_path,
            {"symbol": "TEST", "feed": "sip", "strategy": {"fast_window": 3, "slow_window": 8}},
        )
    )
    assert config.feed == "sip"
    assert config.strategy.fast_window == 3
    assert config.strategy.slow_window == 8
