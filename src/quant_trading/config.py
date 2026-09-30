"""Validated settings; paths are relative to the YAML file, not the shell."""

from __future__ import annotations

import math
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import yaml


@dataclass(frozen=True)
class StrategyConfig:
    fast_window: int = 20
    slow_window: int = 60


@dataclass(frozen=True)
class CostConfig:
    commission_bps: float = 1.0
    slippage_bps: float = 5.0
    min_commission: float = 0.0


@dataclass(frozen=True)
class RiskConfig:
    target_fraction: float = 0.5
    max_position_fraction: float = 0.8
    max_order_notional: float = 5000.0
    cash_buffer_fraction: float = 0.1
    max_drawdown: float = 0.15


@dataclass(frozen=True)
class AppConfig:
    symbol: str = "DEMO"
    initial_cash: float = 10000.0
    strategy: StrategyConfig = field(default_factory=StrategyConfig)
    costs: CostConfig = field(default_factory=CostConfig)
    risk: RiskConfig = field(default_factory=RiskConfig)
    data_path: Path = Path("data/demo.csv")
    output_dir: Path = Path("reports/demo")
    state_path: Path = Path("state/paper.sqlite3")
    feed: str = "iex"

    def validate(self) -> None:
        if not isinstance(self.symbol, str) or not re.fullmatch(r"[A-Z][A-Z0-9.\-]{0,14}", self.symbol):
            raise ValueError("symbol 必須是單一美股代碼（大寫英文字母、數字、點或連字號）。")
        if self.feed not in {"iex", "sip"}:
            raise ValueError("feed 必須是 iex 或 sip。")
        _positive("initial_cash", self.initial_cash)
        fast, slow = self.strategy.fast_window, self.strategy.slow_window
        if any(isinstance(x, bool) or not isinstance(x, int) for x in (fast, slow)) or not 1 <= fast < slow:
            raise ValueError("均線參數必須是整數，且 1 <= fast_window < slow_window。")
        for name, value in asdict(self.costs).items():
            _finite(name, value)
            if value < 0:
                raise ValueError(f"{name} 不得為負數。")
        if self.costs.slippage_bps >= 10000 or self.costs.commission_bps >= 10000:
            raise ValueError("成本 bps 必須小於 10000。")
        for name in ("target_fraction", "max_position_fraction", "max_drawdown"):
            value = getattr(self.risk, name)
            _finite(name, value)
            if not 0 < value <= 1:
                raise ValueError(f"{name} 必須在 (0, 1]。")
        _finite("cash_buffer_fraction", self.risk.cash_buffer_fraction)
        if not 0 <= self.risk.cash_buffer_fraction < 1:
            raise ValueError("cash_buffer_fraction 必須在 [0, 1)。")
        if self.risk.target_fraction > self.risk.max_position_fraction:
            raise ValueError("target_fraction 不得大於 max_position_fraction。")
        _positive("max_order_notional", self.risk.max_order_notional)


def _finite(name: str, value: Any) -> None:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError(f"{name} 必須是有限數值。")


def _positive(name: str, value: Any) -> None:
    _finite(name, value)
    if value <= 0:
        raise ValueError(f"{name} 必須大於 0。")


def load_config(path: str | Path) -> AppConfig:
    path = Path(path).resolve()
    with path.open(encoding="utf-8") as handle:
        raw = yaml.safe_load(handle)
    if not isinstance(raw, dict):
        raise ValueError("設定檔必須是 YAML mapping。")
    allowed = set(AppConfig.__dataclass_fields__)
    if unknown := set(raw) - allowed:
        raise ValueError(f"不認識的設定項目：{', '.join(sorted(unknown))}")
    kwargs = dict(raw)
    for name, cls in (("strategy", StrategyConfig), ("costs", CostConfig), ("risk", RiskConfig)):
        section = kwargs.get(name, {})
        if not isinstance(section, dict):
            raise ValueError(f"{name} 必須是 YAML mapping。")
        if unknown := set(section) - set(cls.__dataclass_fields__):
            raise ValueError(f"{name} 中有不認識的設定：{', '.join(sorted(unknown))}")
        kwargs[name] = cls(**section)
    defaults = AppConfig()
    for name in ("data_path", "output_dir", "state_path"):
        value = kwargs.get(name, getattr(defaults, name))
        if not isinstance(value, (str, Path)) or not str(value):
            raise ValueError(f"{name} 必須是檔案路徑。")
        candidate = Path(value).expanduser()
        kwargs[name] = candidate.resolve() if candidate.is_absolute() else (path.parent / candidate).resolve()
    config = AppConfig(**kwargs)
    config.validate()
    return config


def config_dict(config: AppConfig) -> dict:
    result = asdict(config)
    for key in ("data_path", "output_dir", "state_path"):
        result[key] = str(result[key])
    return result

