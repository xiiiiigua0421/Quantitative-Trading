"""Self-contained research reports with reproducibility metadata."""

from __future__ import annotations

import html
import json
import math
import platform
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

from . import __version__
from .config import AppConfig, config_dict


def _safe(value):
    if isinstance(value, dict):
        return {str(key): _safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_safe(item) for item in value]
    if isinstance(value, (np.integer, np.floating)):
        value = value.item()
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if isinstance(value, (Path, pd.Timestamp, datetime)):
        return str(value)
    return value


def write_report(result, config: AppConfig, bars: pd.DataFrame,
                 output_dir: str | Path | None = None) -> Path:
    output = Path(output_dir or config.output_dir)
    output.mkdir(parents=True, exist_ok=True)
    metrics = _safe(result.metrics)
    (output / "metrics.json").write_text(json.dumps(metrics, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
    for name in ("equity", "trades", "signals"):
        getattr(result, name).to_csv(output / f"{name}.csv", index=True, date_format="%Y-%m-%d")
    manifest = {"version": __version__, "generated_at": datetime.now(timezone.utc).isoformat(),
                "python_version": platform.python_version(), "pandas_version": pd.__version__,
                "numpy_version": np.__version__, "data_sha256": bars.attrs.get("sha256"),
                "data_metadata": bars.attrs.get("metadata", {}), "config": config_dict(config),
                "warnings": list(dict.fromkeys(result.warnings)),
                "model": "Daily t-close signal -> next observed session open with slippage; no forced terminal liquidation"}
    (output / "manifest.json").write_text(json.dumps(_safe(manifest), ensure_ascii=False, indent=2), encoding="utf-8")
    metadata = bars.attrs.get("metadata", {})
    synthetic = bool(metadata.get("synthetic"))
    caution = "合成行情 · 功能驗證" if synthetic else "歷史回測 · 成交模型估計"
    cards = []
    labels = [("total_return", "累積報酬", "pct"), ("max_drawdown", "最大回撤", "pct"),
              ("sharpe", "Sharpe（無風險利率 0）", "number"),
              ("trade_count", "買賣成交筆數", "integer"),
              ("ending_equity", "期末淨值", "money"),
              ("benchmark_total_return", "買入持有報酬", "pct")]
    for key, label, kind in labels:
        value = metrics.get(key)
        formatted = "—" if value is None else (f"{value:.2%}" if kind == "pct" else
                    f"${value:,.2f}" if kind == "money" else
                    str(int(value)) if kind == "integer" else f"{value:.2f}")
        cards.append(f'<div class="card"><span>{label}</span><strong>{formatted}</strong></div>')
    warnings = list(dict.fromkeys(result.warnings))
    warning_html = "".join(f"<li>{html.escape(str(x))}</li>" for x in warnings)
    notes = metrics.get("metric_notes", [])
    notes_html = "".join(f"<li>{html.escape(str(x))}</li>" for x in notes)
    trades = result.trades.copy()
    if not trades.empty:
        trades_html = trades.to_html(index=False, escape=True, float_format=lambda x: f"{x:,.4f}", classes="ledger", border=0)
    else:
        trades_html = "<p>這個區間沒有交易。</p>"
    meta_text = html.escape(json.dumps(_safe(metadata), ensure_ascii=False, indent=2))
    title = html.escape(f"{config.symbol} · 美股量化回測")
    chart = equity_svg(result.equity)
    page = f'''<!doctype html>
<html lang="zh-Hant"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{title}</title><style>
*{{box-sizing:border-box}}body{{margin:0;background:#f4f7fa;color:#193048;font:15px/1.65 system-ui,-apple-system,"Noto Sans TC",sans-serif}}
main{{max-width:1160px;margin:40px auto;padding:0 24px 48px}}h1{{font-size:30px;margin:10px 0}}h2{{font-size:19px;margin:0 0 18px}}
.eyebrow{{color:#39716f;font-weight:600;letter-spacing:.08em}}.muted{{color:#647789}}.badge{{display:inline-block;background:#fff1cc;color:#785400;padding:5px 12px;border-radius:6px}}
.cards{{display:grid;grid-template-columns:repeat(3,1fr);gap:14px;margin:24px 0}}.card,section{{background:white;border:1px solid #e1e8ef;border-radius:10px;padding:22px}}
.card span{{display:block;color:#647789;font-size:13px}}.card strong{{display:block;font-size:27px;margin-top:5px;font-variant-numeric:tabular-nums}}section{{margin-top:18px}}
.chart-wrap{{overflow:auto}}.chart{{width:100%;min-width:700px;height:auto;display:block}}.legend{{display:flex;gap:24px;font-size:13px}}.key::before{{content:"";display:inline-block;width:18px;height:3px;margin:0 8px 3px 0;background:#16877e}}.benchmark::before{{background:#8093aa}}
ul{{padding-left:22px}}.scroll{{overflow:auto}}table{{border-collapse:collapse;width:100%;font-size:12px;white-space:nowrap}}th,td{{padding:10px 12px;text-align:right;border-bottom:1px solid #e7edf3}}th{{color:#647789;text-align:right}}pre{{white-space:pre-wrap;overflow-wrap:anywhere;font-size:12px;background:#f4f7fa;padding:18px;border-radius:8px}}
footer{{color:#647789;font-size:12px;margin-top:22px}}@media(max-width:650px){{main{{margin-top:20px;padding:0 14px 30px}}.cards{{grid-template-columns:repeat(2,1fr)}}.card,section{{padding:16px}}h1{{font-size:25px}}.card strong{{font-size:23px}}}}
</style></head><body><main>
<div class="eyebrow">QUANT TRADING / v{__version__}</div><h1>{title}</h1>
<span class="badge">{caution}</span><p class="muted">資料區間 {bars.index[0].date()} — {bars.index[-1].date()} · {len(bars)} 個交易日 · 均線 {config.strategy.fast_window}/{config.strategy.slow_window} · 初始資金 ${config.initial_cash:,.0f}</p>
<div class="cards">{''.join(cards)}</div>
<section><h2>帳戶淨值（USD）</h2><div class="legend"><span class="key">策略</span><span class="key benchmark">買入持有基準</span></div><div class="chart-wrap">{chart}</div>
<p class="muted">橫軸為資料交易日順序；淨值包含持倉與應收股息。成交採次交易日開盤近似，與券商成交可能有差異。</p></section>
<section><h2>資料與模型限制</h2><ul>{warning_html or '<li>仍需樣本外與前向模擬驗證。</li>'}</ul><ul>{notes_html}</ul></section>
<section><h2>交易與公司行動紀錄</h2><div class="scroll">{trades_html}</div></section>
<section><h2>資料來源</h2><pre>{meta_text}</pre><p class="muted">完整設定、資料雜湊與版本保存於 manifest.json；另有 equity.csv、trades.csv、signals.csv 與 metrics.json。</p></section>
<footer>此報告用於程式與策略研究。合成資料的報酬不代表真實市場表現；回測與模擬結果不構成獲利保證。</footer>
</main></body></html>'''
    path = output / "report.html"
    path.write_text(page, encoding="utf-8")
    return path.resolve()


def equity_svg(frame: pd.DataFrame) -> str:
    columns = [col for col in ("equity", "benchmark_equity") if col in frame]
    if frame.empty or not columns:
        return "<p>沒有淨值資料。</p>"
    width, height = 1000, 350
    left, right, top, bottom = 80, 20, 25, 45
    values = frame[columns].to_numpy(dtype=float)
    lo, hi = float(np.nanmin(values)), float(np.nanmax(values))
    pad = max((hi - lo) * .12, 1)
    lo, hi = lo - pad, hi + pad
    def x(index):
        return left + index / max(len(frame) - 1, 1) * (width - left - right)
    def y(value):
        return top + (hi - value) / (hi - lo) * (height - top - bottom)
    parts = [f'<svg class="chart" role="img" aria-label="策略與買入持有淨值曲線" viewBox="0 0 {width} {height}" xmlns="http://www.w3.org/2000/svg">']
    for tick in np.linspace(lo, hi, 5):
        yy = y(tick)
        parts.append(f'<line x1="{left}" y1="{yy:.2f}" x2="{width-right}" y2="{yy:.2f}" stroke="#e7edf3"/><text x="{left-10}" y="{yy+4:.2f}" text-anchor="end" font-size="16" fill="#647789">{tick:,.0f}</text>')
    for col, color in zip(columns, ("#16877e", "#8093aa")):
        points = " ".join(f"{x(i):.2f},{y(value):.2f}" for i, value in enumerate(frame[col]))
        parts.append(f'<polyline points="{points}" fill="none" stroke="{color}" stroke-width="2.2"/>')
    for index in sorted(set([0, len(frame) // 2, len(frame) - 1])):
        anchor = "start" if index == 0 else "end" if index == len(frame) - 1 else "middle"
        parts.append(f'<text x="{x(index):.2f}" y="{height-12}" text-anchor="{anchor}" font-size="16" fill="#647789">{frame.index[index].date()}</text>')
    parts.append("</svg>")
    return "".join(parts)
