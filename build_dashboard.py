from __future__ import annotations

import argparse
import json
import shutil
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd

from quant_guardian import (
    DEFAULT_CONFIG,
    execution_ticker,
    full_report,
    load_config,
    market_regime,
    qg_core_backtest,
    qg_core_etf_scores,
    qg_core_portfolio_plan,
    read_price,
    resolve_paths,
)


ROOT = Path(__file__).resolve().parent
STATIC_ASSETS = ["manifest.webmanifest", "service-worker.js", "icon.svg"]

ETF_GUIDE = [
    {
        "ticker": "SPY",
        "role": "S&P 500 신호 기준",
        "tracks": "S&P 500",
        "what": "미국 대표 대형주 약 500개를 추종하는 가장 널리 쓰이는 기준 ETF입니다.",
        "use": "시장 전체 추세와 백테스트 비교 기준으로 사용합니다.",
        "note": "실제 장기 매수 대안은 같은 지수를 추종하는 SPYM을 표시합니다.",
    },
    {
        "ticker": "SPYM",
        "role": "S&P 500 장기매수 대안",
        "tracks": "S&P 500",
        "what": "SPY와 같은 S&P 500 지수를 추종하는 State Street ETF입니다.",
        "use": "SPY 신호가 선택되면 실제 매수 후보로 표시합니다.",
        "note": "신호 계산은 역사가 긴 SPY, 실행 표시는 SPYM을 사용합니다.",
    },
    {
        "ticker": "QQQ",
        "role": "나스닥100 신호 기준",
        "tracks": "Nasdaq-100",
        "what": "나스닥 상장 대형 비금융 기업 100개를 추종합니다.",
        "use": "성장주와 기술주 흐름을 판단하는 핵심 지수로 사용합니다.",
        "note": "실제 장기 매수 대안은 같은 지수를 추종하는 QQQM을 표시합니다.",
    },
    {
        "ticker": "QQQM",
        "role": "나스닥100 장기매수 대안",
        "tracks": "Nasdaq-100",
        "what": "QQQ와 같은 나스닥100 지수를 추종하는 Invesco ETF입니다.",
        "use": "QQQ 신호가 선택되면 실제 매수 후보로 표시합니다.",
        "note": "QQQ보다 거래량은 작지만 장기 보유용 대안으로 비교합니다.",
    },
    {
        "ticker": "SPYG",
        "role": "S&P 500 성장주",
        "tracks": "S&P 500 Growth",
        "what": "S&P 500 구성 종목 중 성장 특성이 강한 기업을 담습니다.",
        "use": "나스닥100과 다른 방식의 성장주 지수 노출을 비교합니다.",
        "note": "QQQ와 구성 지수와 업종 비중이 다르므로 같은 상품이 아닙니다.",
    },
    {
        "ticker": "XLK",
        "role": "미국 기술 섹터",
        "tracks": "Technology Select Sector",
        "what": "S&P 500 안의 정보기술 기업에 집중합니다.",
        "use": "기술 섹터 흐름이 시장보다 강할 때 보조 비중 후보로 봅니다.",
        "note": "소수 대형 기술주 비중이 높아 핵심 지수보다 변동성이 커질 수 있습니다.",
    },
    {
        "ticker": "SMH",
        "role": "반도체 섹터",
        "tracks": "MVIS US Listed Semiconductor 25",
        "what": "미국 상장 주요 반도체 기업에 집중합니다.",
        "use": "반도체 흐름이 충분히 강할 때만 전체 주식형 비중의 일부로 사용합니다.",
        "note": "집중도와 변동성이 높아 포트폴리오의 핵심 자산으로 단독 사용하지 않습니다.",
    },
    {
        "ticker": "SGOV",
        "role": "초단기 국채 대기자금",
        "tracks": "0-3개월 미국 T-Bill",
        "what": "만기가 매우 짧은 미국 국채에 투자해 현금 대기처에 가깝게 움직입니다.",
        "use": "매수 조건이 약할 때 다음 신호까지 대기하는 비중으로 사용합니다.",
        "note": "환율, ETF 가격, 분배금, 세금 때문에 원화 현금과 완전히 같지는 않습니다.",
    },
    {
        "ticker": "SHY",
        "role": "장기 백테스트용 단기채",
        "tracks": "1-3년 미국 국채",
        "what": "1~3년 만기의 미국 국채에 투자합니다.",
        "use": "SGOV의 상장 역사가 짧아 장기 백테스트의 대기자금 대용으로 사용합니다.",
        "note": "SGOV보다 금리 변화에 더 민감해 실제 성과가 완전히 같지는 않습니다.",
    },
    {
        "ticker": "GLD",
        "role": "금 분산 자산",
        "tracks": "금 현물 가격",
        "what": "금 가격 흐름에 노출되는 ETF입니다.",
        "use": "GLD 자체가 200일선 위일 때만 분산 비중을 둡니다.",
        "note": "항상 주식 하락을 막아주는 자산은 아닙니다.",
    },
    {
        "ticker": "TLT",
        "role": "장기 미국 국채",
        "tracks": "20년 이상 미국 국채",
        "what": "만기가 긴 미국 국채에 투자합니다.",
        "use": "TLT 자체가 200일선 위일 때만 분산 비중을 둡니다.",
        "note": "금리 상승기에는 가격 변동과 손실이 클 수 있습니다.",
    },
]


def clean_value(value):
    if isinstance(value, (list, dict, tuple)):
        return json_ready(value)
    try:
        if pd.isna(value):
            return None
    except (TypeError, ValueError):
        pass
    if hasattr(value, "item"):
        return value.item()
    return value


def json_ready(value):
    if isinstance(value, dict):
        return {key: json_ready(item) for key, item in value.items()}
    if isinstance(value, list):
        return [json_ready(item) for item in value]
    if isinstance(value, tuple):
        return [json_ready(item) for item in value]
    try:
        if pd.isna(value):
            return None
    except (TypeError, ValueError):
        pass
    if hasattr(value, "item"):
        return value.item()
    return value


def records(frame: pd.DataFrame, limit: int | None = None) -> list[dict]:
    if limit is not None:
        frame = frame.head(limit)
    return [{key: clean_value(value) for key, value in row.items()} for row in frame.to_dict(orient="records")]


def curve_records(series: pd.Series, limit: int = 240) -> list[dict]:
    series = series.dropna()
    if series.empty:
        return []
    if len(series) > limit:
        step = max(1, len(series) // limit)
        series = series.iloc[::step]
    return [{"date": index.date().isoformat(), "value": round(float(value), 4)} for index, value in series.items()]


def metric_pct(value):
    if value is None or pd.isna(value):
        return None
    return round(float(value) * 100, 2)


def metric_num(value, digits: int = 4):
    if value is None or pd.isna(value):
        return None
    return round(float(value), digits)


def build_etf_guide(cfg: dict, paths, refresh: bool) -> list[dict]:
    source = cfg["settings"].get("data_source", "yahoo")
    rows = []
    for item in ETF_GUIDE:
        row = dict(item)
        try:
            price = read_price(item["ticker"], paths, refresh=refresh, source=source)
            row["last"] = round(float(price["Close"].dropna().iloc[-1]), 2)
            row["avg_volume_60d"] = int(price["Volume"].dropna().tail(60).mean()) if "Volume" in price else None
            row["as_of"] = price.index[-1].date().isoformat()
            row["data_error"] = None
        except Exception as exc:
            row["last"] = None
            row["avg_volume_60d"] = None
            row["as_of"] = None
            row["data_error"] = str(exc)
        rows.append(row)
    return rows


def index_chart_payload(cfg: dict, paths) -> dict[str, list[dict]]:
    source = cfg["settings"].get("data_source", "yahoo")
    charts: dict[str, list[dict]] = {}
    for ticker in [str(item).upper() for item in cfg["qg_core"]["equity_etfs"]]:
        try:
            close = read_price(ticker, paths, refresh=False, source=source)["Close"].dropna().tail(540)
            sma20 = close.rolling(20).mean()
            sma50 = close.rolling(50).mean()
            sma200 = close.rolling(200).mean()
            charts[ticker] = [
                {
                    "date": index.date().isoformat(),
                    "close": round(float(value), 2),
                    "sma20": round(float(sma20.loc[index]), 2) if pd.notna(sma20.loc[index]) else None,
                    "sma50": round(float(sma50.loc[index]), 2) if pd.notna(sma50.loc[index]) else None,
                    "sma200": round(float(sma200.loc[index]), 2) if pd.notna(sma200.loc[index]) else None,
                }
                for index, value in close.items()
            ]
        except Exception:
            charts[ticker] = []
    return charts


def build_daily_advice(decision: dict, etfs: pd.DataFrame, plan: pd.DataFrame, cfg: dict) -> dict:
    top = {}
    if not plan.empty and not etfs.empty:
        equity_plan = plan[plan["type"].isin(["핵심 지수", "섹터 보조"])]
        if not equity_plan.empty:
            signal_ticker = str(equity_plan.iloc[0]["signal_asset"])
            matched = etfs[etfs["ticker"] == signal_ticker]
            if not matched.empty:
                top = matched.iloc[0].to_dict()
    if not top and not etfs.empty:
        top = etfs.iloc[0].to_dict()
    top_signal = str(top.get("ticker", "-"))
    top_execution = execution_ticker(cfg, top_signal) if top_signal != "-" else "-"
    market_action = str(decision.get("action", "보유·관찰"))
    top_action = str(top.get("action", "보유·관찰"))
    if market_action in {"비중축소", "매도·대기"}:
        action = market_action
        tone = "bad"
    elif "추격매수 대기" in top_action:
        action = "보유·추격매수 대기"
        tone = "warn"
    elif market_action == "보유·관찰":
        action = market_action
        tone = "warn"
    else:
        action = top_action
        tone = str(top.get("tone", "good"))
    equity_weight = float(decision.get("target_equity_weight", 0))
    summary = (
        f"주식형 ETF 목표는 전체 투자금의 {equity_weight * 100:.0f}%입니다. "
        f"우선 확인할 지수는 {top_signal}, 실제 매수 표시 종목은 {top_execution}입니다. "
        f"{top.get('timing', '다음 미국장 마감 데이터까지 기다립니다.')}"
    )
    return {
        "action": action,
        "tone": tone,
        "summary": summary,
        "data_mode": "미국장 마감 종가 기준·장중 실시간 아님",
        "refresh_rule": "미국 정규장 마감 뒤 한국 오전 자동 갱신",
        "as_of": decision.get("as_of"),
        "market_score": round(float(decision.get("score", 0)), 1),
        "target_equity_weight": equity_weight,
        "top_etf_signal": top_signal,
        "top_etf_execution": top_execution,
        "top_etf_score": round(float(top.get("score", 0)), 1) if top else None,
        "top_etf_action": top_action,
        "top_etf_timing": top.get("timing"),
        "top_etf_last": round(float(top.get("last", 0)), 2) if top else None,
        "positives": top.get("positives", []),
        "cautions": top.get("cautions", []),
        "steps": [
            "목표 비중은 현재 보유량을 모르는 상태의 목표값입니다.",
            "실제 주문액은 목표금액에서 현재 보유 평가액을 뺀 값으로 계산합니다.",
            "신호는 매일 확인해도 주문은 주 1회 이내로 모아 과도한 매매를 줄입니다.",
        ],
        "plan": records(plan),
    }


def write_static_assets(paths) -> None:
    for asset in STATIC_ASSETS:
        source = ROOT / asset
        if source.exists():
            shutil.copyfile(source, paths.output / asset)


def write_daily_payload(paths, payload: dict) -> None:
    daily = {
        "generated_at": payload["generated_at"],
        "daily_advice": payload["daily_advice"],
        "market_decision": payload["market_decision"],
        "regime": payload["market_decision"],
        "qg_core_metrics": payload["qg_core_metrics"],
        "benchmarks": payload["benchmarks"],
        "top_etfs": payload["qg_core_etfs"],
        "plan": payload["plan"],
    }
    (paths.output / "daily.json").write_text(
        json.dumps(json_ready(daily), ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8"
    )


def write_output_assets(paths, payload: dict) -> None:
    write_static_assets(paths)
    write_daily_payload(paths, payload)


def build_payload(refresh: bool = False) -> dict:
    cfg = load_config(DEFAULT_CONFIG)
    paths = resolve_paths(cfg)
    etfs = qg_core_etf_scores(cfg, paths, refresh=refresh)
    decision = market_regime(cfg, paths, refresh=False)
    plan = qg_core_portfolio_plan(cfg, paths, refresh=False)
    backtest = qg_core_backtest(cfg, paths, refresh=False)
    full_report(
        cfg,
        paths,
        refresh=False,
        prepared={"scores": etfs, "decision": decision, "plan": plan, "backtest": backtest},
    )
    returns = backtest["returns"]
    qg_metrics = backtest["metrics"].get("QG_CORE", {})
    spy_metrics = backtest["metrics"].get("SPY", {})
    qqq_metrics = backtest["metrics"].get("QQQ", {})
    payload = {
        "generated_at": datetime.now(ZoneInfo("Asia/Seoul")).strftime("%Y-%m-%d %H:%M:%S KST"),
        "market_decision": decision,
        "daily_advice": build_daily_advice(decision, etfs, plan, cfg),
        "qg_core_metrics": {
            "cagr_pct": metric_pct(qg_metrics.get("cagr")),
            "mdd_pct": metric_pct(qg_metrics.get("mdd")),
            "sharpe": metric_num(qg_metrics.get("sharpe"), 3),
            "sortino": metric_num(qg_metrics.get("sortino"), 3),
            "calmar": metric_num(qg_metrics.get("calmar"), 3),
            "win_rate_pct": metric_pct(qg_metrics.get("win_rate")),
        },
        "benchmarks": {
            "spy_cagr_pct": metric_pct(spy_metrics.get("cagr")),
            "spy_mdd_pct": metric_pct(spy_metrics.get("mdd")),
            "qqq_cagr_pct": metric_pct(qqq_metrics.get("cagr")),
            "qqq_mdd_pct": metric_pct(qqq_metrics.get("mdd")),
        },
        "qg_core_etfs": records(etfs),
        "plan": records(plan),
        "etf_guide": build_etf_guide(cfg, paths, refresh=False),
        "index_charts": index_chart_payload(cfg, paths),
        "charts": {
            "qg_core": curve_records((1 + returns["QG_CORE"].fillna(0)).cumprod()),
            "spy": curve_records((1 + returns["SPY"].fillna(0)).cumprod()),
            "qqq": curve_records((1 + returns["QQQ"].fillna(0)).cumprod()),
        },
        "methodology": {
            "signal_frequency": "매 거래일 종가로 계산",
            "trade_frequency": "주 1회 이내 실행 권장",
            "backtest_rule": "월말 신호를 다음 달에 적용",
            "transaction_cost_bps": float(cfg["qg_core"].get("transaction_cost_bps", 10)),
        },
    }
    return json_ready(payload)


HTML_TEMPLATE = r"""<!doctype html>
<html lang="ko">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta name="theme-color" content="#176b5b">
  <link rel="manifest" href="manifest.webmanifest">
  <link rel="icon" href="icon.svg" type="image/svg+xml">
  <title>Quant Guardian 지수 타이밍</title>
  <style>
    :root { --bg:#f4f6f8; --panel:#fff; --ink:#17212b; --muted:#667585; --line:#dce3e8; --brand:#176b5b; --brand-soft:#e6f3ef; --blue:#2463a7; --green:#067647; --red:#b42318; --amber:#a15c07; --shadow:0 5px 18px rgba(23,33,43,.07); }
    * { box-sizing:border-box; }
    body { margin:0; background:var(--bg); color:var(--ink); font-family:Arial,"Malgun Gothic",sans-serif; letter-spacing:0; }
    header { position:sticky; top:0; z-index:10; background:#fff; border-bottom:1px solid var(--line); }
    .topbar { max-width:1240px; margin:0 auto; padding:14px 20px; display:flex; align-items:center; justify-content:space-between; gap:14px; }
    h1 { margin:0; font-size:21px; }
    .sub { margin-top:4px; color:var(--muted); font-size:12px; }
    .actions { display:flex; align-items:center; gap:8px; flex-wrap:wrap; }
    .button { border:1px solid var(--line); background:#fff; color:var(--ink); border-radius:7px; padding:9px 12px; font-weight:800; font-size:13px; text-decoration:none; cursor:pointer; }
    .button.primary { background:var(--brand); color:#fff; border-color:var(--brand); }
    .button[hidden] { display:none; }
    main { max-width:1240px; margin:0 auto; padding:20px 20px 48px; }
    .notice { background:#fff8e8; border:1px solid #f2d18a; color:#70420b; border-radius:7px; padding:12px 14px; font-size:13px; line-height:1.55; margin-bottom:14px; }
    .card { min-width:0; background:var(--panel); border:1px solid var(--line); border-radius:7px; box-shadow:var(--shadow); padding:16px; }
    .daily { display:grid; grid-template-columns:minmax(0,1.25fr) minmax(280px,.75fr); gap:20px; margin-bottom:14px; }
    .daily-title { display:flex; align-items:center; gap:10px; flex-wrap:wrap; margin-bottom:8px; }
    .daily h2 { margin:0; font-size:20px; }
    .daily-copy { margin:0; color:var(--muted); font-size:13px; line-height:1.65; }
    .badge,.pill { display:inline-flex; align-items:center; min-height:25px; padding:4px 8px; border-radius:999px; background:#edf2f5; color:#344454; font-weight:900; font-size:12px; white-space:nowrap; }
    .good { color:var(--green); }
    .bad { color:var(--red); }
    .warn { color:var(--amber); }
    .badge.good,.pill.good { background:#dcfce7; color:#166534; }
    .badge.warn,.pill.warn { background:#fef3c7; color:#92400e; }
    .badge.bad,.pill.bad { background:#fee2e2; color:#991b1b; }
    .steps { margin:10px 0 0; padding-left:20px; color:var(--muted); font-size:13px; line-height:1.55; }
    .chips { display:flex; gap:7px; flex-wrap:wrap; margin-top:10px; }
    .chip { border:1px solid var(--line); background:#f8fafb; border-radius:999px; padding:6px 9px; font-size:12px; font-weight:800; }
    .grid { display:grid; gap:14px; }
    .summary { grid-template-columns:repeat(4,minmax(0,1fr)); }
    .label { color:var(--muted); font-size:12px; font-weight:800; }
    .value { margin-top:8px; font-size:25px; font-weight:900; overflow-wrap:anywhere; }
    .hint { margin-top:6px; color:var(--muted); font-size:12px; line-height:1.45; }
    .chart-grid { grid-template-columns:repeat(2,minmax(0,1fr)); margin-top:14px; }
    .chart-card h3 { margin:0 0 4px; font-size:15px; }
    .chart-card p { margin:0 0 12px; color:var(--muted); font-size:13px; line-height:1.5; }
    .bar-list { display:grid; gap:9px; }
    .bar-row { display:grid; grid-template-columns:92px minmax(0,1fr) 70px; gap:10px; align-items:center; font-size:13px; }
    .bar-track { height:10px; background:#edf1f4; border-radius:999px; overflow:hidden; }
    .bar-fill { height:100%; background:var(--brand); border-radius:999px; }
    .tabs { display:flex; gap:6px; flex-wrap:wrap; margin:20px 0 12px; }
    .tab { border:1px solid var(--line); background:#fff; border-radius:7px; padding:9px 12px; font-weight:800; cursor:pointer; }
    .tab.active { background:var(--brand-soft); color:var(--brand); border-color:#acd0c7; }
    .view { display:none; }
    .view.active { display:block; }
    .head { display:flex; align-items:end; justify-content:space-between; gap:12px; margin:14px 0 10px; }
    .head h2 { margin:0; font-size:18px; }
    .head p { margin:4px 0 0; color:var(--muted); font-size:13px; line-height:1.5; }
    .split { display:grid; grid-template-columns:minmax(0,1.45fr) minmax(290px,.55fr); gap:14px; }
    .split > * { min-width:0; }
    .table-wrap { width:100%; overflow:auto; background:#fff; border:1px solid var(--line); border-radius:7px; box-shadow:var(--shadow); }
    table { width:100%; min-width:1120px; border-collapse:collapse; }
    th,td { padding:10px 11px; border-bottom:1px solid var(--line); text-align:left; vertical-align:top; font-size:12px; line-height:1.45; word-break:keep-all; }
    th { background:#f7f9fa; color:var(--muted); position:sticky; top:0; }
    tr:last-child td { border-bottom:0; }
    .kv { display:grid; grid-template-columns:minmax(0,1fr) auto; gap:10px; padding:9px 0; border-bottom:1px solid var(--line); font-size:13px; }
    .kv:last-child { border-bottom:0; }
    .kv span:first-child { color:var(--muted); }
    .kv span:last-child { font-weight:900; text-align:right; }
    .explain { margin-top:10px; color:var(--muted); font-size:13px; line-height:1.55; }
    .controls { display:flex; align-items:center; gap:8px; flex-wrap:wrap; margin-bottom:10px; }
    select,input { border:1px solid var(--line); border-radius:7px; background:#fff; padding:9px 10px; color:var(--ink); font-weight:700; }
    input { width:min(280px,100%); }
    .period { border:1px solid var(--line); background:#fff; border-radius:7px; padding:8px 10px; font-weight:800; cursor:pointer; }
    .period.active { background:var(--brand-soft); color:var(--brand); border-color:#acd0c7; }
    .chart { display:block; width:100%; height:270px; overflow:visible; }
    .chart-legend { display:flex; flex-wrap:wrap; gap:11px; margin-top:8px; color:var(--muted); font-size:12px; }
    .legend-dot { display:inline-block; width:10px; height:10px; border-radius:50%; margin-right:5px; }
    .indicator-summary { grid-template-columns:repeat(4,minmax(0,1fr)); margin-top:14px; }
    .indicator-grid { grid-template-columns:repeat(3,minmax(0,1fr)); margin-top:14px; }
    .indicator-item { background:#fff; border:1px solid var(--line); border-radius:7px; padding:13px; }
    .indicator-item strong { display:block; margin-bottom:5px; font-size:13px; }
    .indicator-item p { margin:0; color:var(--muted); font-size:12px; line-height:1.5; }
    .capital-box { display:flex; align-items:end; gap:9px; flex-wrap:wrap; margin-bottom:12px; }
    .capital-box label { display:grid; gap:5px; color:var(--muted); font-size:12px; font-weight:800; }
    .privacy { color:var(--muted); font-size:12px; line-height:1.5; margin:0 0 12px; }
    .etf-cards { display:grid; grid-template-columns:repeat(2,minmax(0,1fr)); gap:14px; }
    .etf-title { display:flex; align-items:baseline; justify-content:space-between; gap:10px; }
    .etf-title h3 { margin:0; font-size:18px; }
    .etf-role { color:var(--brand); font-size:12px; font-weight:900; }
    .etf-meta { display:grid; grid-template-columns:repeat(3,minmax(0,1fr)); gap:8px; }
    .mini { background:#f7f9fa; border:1px solid var(--line); border-radius:7px; padding:8px; }
    .mini span { display:block; color:var(--muted); font-size:11px; margin-bottom:4px; }
    .mini strong { font-size:12px; }
    .etf-card p { margin:0; color:var(--muted); font-size:13px; line-height:1.55; }
    .term-grid { grid-template-columns:repeat(3,minmax(0,1fr)); }
    .term { min-height:128px; }
    .term strong { display:block; margin-bottom:8px; }
    .term p { margin:0; color:var(--muted); font-size:13px; line-height:1.55; }
    .tip { display:inline-flex; align-items:center; gap:4px; border-bottom:1px dotted #8b99a6; cursor:help; white-space:nowrap; }
    .tip::after { content:"?"; display:inline-grid; place-items:center; width:16px; height:16px; border-radius:50%; background:#e8f2f0; color:var(--brand); font-size:11px; font-weight:900; }
    .global-tooltip { display:none; position:fixed; z-index:1000; width:max-content; max-width:min(640px,calc(100vw - 28px)); background:#17212b; color:#fff; border-radius:7px; padding:10px 12px; box-shadow:var(--shadow); font-size:12px; line-height:1.55; white-space:normal; overflow-wrap:anywhere; pointer-events:none; }
    .global-tooltip.active { display:block; }
    @media (max-width:920px) { .summary,.daily,.chart-grid,.split,.indicator-summary,.indicator-grid,.etf-cards,.term-grid { grid-template-columns:1fr; } .topbar { align-items:flex-start; flex-direction:column; } }
  </style>
</head>
<body>
  <header>
    <div class="topbar">
      <div><h1>Quant Guardian 지수 타이밍</h1><div class="sub" id="generatedAt"></div></div>
      <div class="actions">
        <span class="tip" tabindex="0" data-tip="화면 계산 결과를 텍스트로 남긴 검산용 파일입니다. 평소 사용에는 열지 않아도 됩니다.">계산 원문</span>
        <a class="button" href="report.html">열기</a>
        <button class="button" id="installApp" hidden>앱 설치</button>
      </div>
    </div>
  </header>
  <main>
    <div class="notice">이 화면은 미래 가격을 맞히는 도구가 아닙니다. 미국장 마감 가격에서 여러 지표가 같은 방향을 가리키는지 확인해 매수 시점과 목표 비중을 정합니다. 장중 실시간 신호가 아니며 자동주문도 하지 않습니다.</div>
    <section class="card daily" id="dailyAdvice"></section>
    <div class="grid summary">
      <div class="card"><div class="label" id="labelAction"></div><div class="value" id="marketAction"></div><div class="hint" id="actionHint"></div></div>
      <div class="card"><div class="label" id="labelSignal"></div><div class="value" id="signal"></div><div class="hint" id="signalHint"></div></div>
      <div class="card"><div class="label" id="labelEquity"></div><div class="value" id="equityTarget"></div><div class="hint">나머지는 대기자금과 조건부 분산 자산</div></div>
      <div class="card"><div class="label" id="labelMdd"></div><div class="value negative" id="qgMdd"></div><div class="hint">과거 최대 낙폭·작을수록 양호</div></div>
    </div>
    <div class="grid chart-grid">
      <div class="card chart-card"><h3>현재 목표 비중</h3><p>총 투자금에 적용할 목표 포트폴리오입니다.</p><div id="weightBars" class="bar-list"></div></div>
      <div class="card chart-card"><h3>지수 ETF 종합점수</h3><p>100점은 수익 확률이 아니라 네 지표 묶음의 조건 충족 정도입니다.</p><div id="etfScoreBars" class="bar-list"></div></div>
    </div>

    <nav class="tabs" aria-label="대시보드 보기 선택">
      <button class="tab active" data-view="today">오늘 판단</button>
      <button class="tab" data-view="indicators">차트·보조지표</button>
      <button class="tab" data-view="portfolio">목표 비중</button>
      <button class="tab" data-view="etfs">ETF 설명</button>
      <button class="tab" data-view="backtest">백테스트</button>
      <button class="tab" data-view="terms">용어</button>
    </nav>

    <section id="today" class="view active">
      <div class="split">
        <div>
          <div class="head"><div><h2>ETF별 매수·보유 판단</h2><p>점수보다 행동과 실행 기준을 먼저 보세요.</p></div></div>
          <div class="table-wrap"><table id="timingTable"></table></div>
        </div>
        <aside class="card">
          <div class="head"><div><h2>시장 확인</h2><p>SPY·QQQ·VIX에서 확인한 객관적 상태입니다.</p></div></div>
          <div id="marketFacts"></div>
          <p class="explain" id="marketExplain"></p>
        </aside>
      </div>
    </section>

    <section id="indicators" class="view">
      <div class="head"><div><h2>가격 차트와 보조지표</h2><p>한 지표만 믿지 않고 추세·모멘텀·타이밍·위험을 나눠 봅니다.</p></div></div>
      <div class="card chart-card">
        <div class="controls">
          <select id="indexSelect" aria-label="차트로 볼 ETF 선택"></select>
          <button class="period" data-days="63">3개월</button>
          <button class="period active" data-days="126">6개월</button>
          <button class="period" data-days="252">1년</button>
          <button class="period" data-days="520">2년</button>
        </div>
        <svg id="indexChart" class="chart" role="img" aria-label="ETF 가격과 이동평균선"></svg>
        <div class="chart-legend">
          <span><i class="legend-dot" style="background:#17212b"></i>종가</span>
          <span><i class="legend-dot" style="background:#176b5b"></i>20일선</span>
          <span><i class="legend-dot" style="background:#2463a7"></i>50일선</span>
          <span><i class="legend-dot" style="background:#b42318"></i>200일선</span>
        </div>
        <div class="hint" id="indexChartMeta"></div>
      </div>
      <div class="grid indicator-summary" id="indicatorScores"></div>
      <div class="grid indicator-grid" id="indicatorGrid"></div>
    </section>

    <section id="portfolio" class="view">
      <div class="head"><div><h2>목표 비중과 금액</h2><p>현재 보유 평가액을 뺀 차액이 실제 매수·매도 검토 금액입니다.</p></div></div>
      <div class="capital-box">
        <label>총 투자 가능 금액(원)<input id="capitalInput" type="number" min="0" step="100000" inputmode="numeric" placeholder="예: 10000000"></label>
        <button class="button" id="clearCapital">금액 지우기</button>
      </div>
      <p class="privacy">입력 금액은 이 브라우저의 로컬 저장소에만 보관되며 사이트나 GitHub로 전송되지 않습니다. 보유 종목을 입력하지 않으므로 표시 금액은 주문액이 아니라 목표 평가액입니다.</p>
      <div class="table-wrap"><table id="planTable"></table></div>
    </section>

    <section id="etfs" class="view">
      <div class="head"><div><h2>사용 ETF 설명</h2><p>신호용 ETF와 실제 매수 표시 종목이 다른 이유도 함께 적었습니다.</p></div></div>
      <div id="etfCards" class="etf-cards"></div>
    </section>

    <section id="backtest" class="view">
      <div class="head"><div><h2>과거 규칙 적용 결과</h2><p>월말에 계산한 신호를 다음 달에 적용하고 거래비용을 차감했습니다. 미래 성과 보장이 아닙니다.</p></div></div>
      <div class="card chart-card">
        <h3>누적 수익 비교</h3>
        <svg id="equityChart" class="chart" role="img" aria-label="전략과 지수의 누적 수익 비교"></svg>
        <div class="chart-legend">
          <span><i class="legend-dot" style="background:#176b5b"></i>QG Index Timing</span>
          <span><i class="legend-dot" style="background:#2463a7"></i>SPY</span>
          <span><i class="legend-dot" style="background:#a15c07"></i>QQQ</span>
        </div>
      </div>
      <div class="grid summary" id="backtestCards"></div>
      <p class="explain" id="backtestExplain"></p>
    </section>

    <section id="terms" class="view">
      <div class="head"><div><h2>처음 보는 사람을 위한 설명</h2><p>각 숫자가 무엇을 뜻하고 어떻게 사용되는지 정리했습니다.</p></div></div>
      <div class="grid term-grid" id="termGrid"></div>
    </section>
  </main>
  <div id="globalTooltip" class="global-tooltip"></div>
  <script>
    const DATA = __DATA__;
    const $ = id => document.getElementById(id);
    const esc = value => String(value ?? "").replace(/[&<>"']/g, char => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[char]));
    const pct = value => value === null || value === undefined || Number.isNaN(Number(value)) ? "-" : `${Number(value).toFixed(2)}%`;
    const num = (value, digits=2) => value === null || value === undefined || Number.isNaN(Number(value)) ? "-" : Number(value).toFixed(digits);
    const usd = value => value === null || value === undefined || Number.isNaN(Number(value)) ? "-" : `$${Number(value).toFixed(2)}`;
    const formatVolume = value => value === null || value === undefined || Number.isNaN(Number(value)) ? "-" : Number(value).toLocaleString("ko-KR");
    const krw = value => `${Math.round(Number(value) || 0).toLocaleString("ko-KR")}원`;
    const tip = (label,text) => `<span class="tip" tabindex="0" data-tip="${esc(text)}">${esc(label)}</span>`;
    const HELP = {
      action:"SPY와 QQQ의 기술적 상태를 합쳐 신규매수 검토, 분할매수, 보유·관찰, 비중축소, 매도·대기 중 하나로 표시합니다.",
      score:"추세 40점, 모멘텀 25점, 진입 타이밍 20점, 위험 15점의 합입니다. 80점이 수익확률 80%라는 뜻은 아닙니다.",
      equity:"전체 투자 가능 금액 중 주식형 지수 ETF가 차지할 목표 비중입니다.",
      mdd:"과거 고점에서 저점까지 가장 크게 줄어든 비율입니다. 백테스트 구간에만 해당합니다.",
      cagr:"연평균 복리수익률입니다. 백테스트 값이며 미래 수익률 예측값이 아닙니다.",
      rsi:"최근 14거래일 상승과 하락 속도입니다. 이 전략은 45~65를 비교적 편안한 진입 구간, 72 초과를 추격주의로 봅니다.",
      ichimoku:"9·26·52기간 고가와 저가 중간값으로 추세와 지지·저항을 보는 일목균형표입니다. 구름대 위를 상승 추세 확인으로 사용합니다.",
      adx:"추세의 강도를 보는 지표입니다. 20 이상이면서 +DI가 -DI보다 크면 상승 방향에 가점을 줍니다.",
      atr:"최근 가격 변동 폭입니다. 가격 대비 ATR이 높으면 같은 금액을 투자해도 흔들림이 커 위험 점수가 낮아집니다.",
      macd:"12일과 26일 지수이동평균의 차이입니다. MACD가 신호선 위면 단기 추세 개선으로 봅니다.",
      bb:"20일 평균과 표준편차로 만든 밴드 안에서 현재 가격 위치를 나타냅니다. 위쪽을 크게 벗어나면 추격매수를 경계합니다.",
      stochastic:"최근 14일 고가·저가 범위에서 현재 가격 위치를 봅니다. 90을 넘으면 여러 단기 과열 조건 중 하나로 확인합니다.",
      momentum:"최근 1개월을 제외한 12개월 수익률입니다. 단기 반전 노이즈를 줄이면서 중기 추세 지속성을 봅니다.",
      data:"미국 정규장 마감 후 확정된 일봉을 사용합니다. 장중 가격 변화는 다음 갱신 전까지 반영되지 않습니다."
    };
    const TERMS = [
      ["오늘의 판단", HELP.action],
      ["종합점수", HELP.score],
      ["이동평균선", "20·50·100·200일 평균 가격입니다. 짧은 선은 빠르지만 신호가 자주 바뀌고, 200일선은 느리지만 큰 추세를 확인하는 데 사용합니다."],
      ["일목균형표", HELP.ichimoku],
      ["MACD", HELP.macd],
      ["RSI", HELP.rsi],
      ["스토캐스틱", HELP.stochastic],
      ["볼린저밴드", HELP.bb],
      ["ADX와 DI", HELP.adx],
      ["ATR", HELP.atr],
      ["12-1 모멘텀", HELP.momentum],
      ["CAGR", HELP.cagr],
      ["MDD", HELP.mdd],
      ["백테스트", "현재 규칙을 과거 데이터에 적용한 가상 결과입니다. 월말 신호를 다음 달에 적용해 미래 데이터를 미리 보는 오류를 줄였지만, 세금·환율·실제 체결 차이는 남습니다."],
      ["왜 모든 지표를 더하지 않나", "RSI, MACD, 이동평균선 등은 대부분 같은 가격을 다른 방식으로 가공합니다. 비슷한 지표를 많이 더하면 확신이 커 보일 뿐이므로 네 묶음별 점수 상한을 둡니다."]
    ];
    function toneFor(value) {
      if (["신규매수","분할매수","신규매수 검토"].includes(value)) return "good";
      if (["비중축소","매도·대기"].includes(value)) return "bad";
      return "warn";
    }
    const pill = value => `<span class="pill ${toneFor(value)}">${esc(value || "-")}</span>`;
    function table(element, columns, rows) {
      element.innerHTML = `<thead><tr>${columns.map(column => `<th>${column.help ? tip(column.label,column.help) : esc(column.label)}</th>`).join("")}</tr></thead><tbody>${rows.map(row => `<tr>${columns.map(column => `<td>${column.render ? column.render(row[column.key],row) : esc(row[column.key] ?? "-")}</td>`).join("")}</tr>`).join("")}</tbody>`;
    }
    function barList(element, rows, valueKey, maxValue, labelKey, formatter) {
      element.innerHTML = rows.map(row => {
        const value = Number(row[valueKey] || 0);
        const width = Math.max(1,Math.min(100,value / maxValue * 100));
        return `<div class="bar-row"><strong>${esc(row[labelKey])}</strong><div class="bar-track"><div class="bar-fill" style="width:${width}%"></div></div><span>${esc(formatter(value))}</span></div>`;
      }).join("");
    }
    function lineChart(svg, seriesList, formatter=value=>value.toFixed(2)) {
      const width = 900, height = 270, left = 54, right = 16, top = 18, bottom = 34;
      svg.setAttribute("viewBox",`0 0 ${width} ${height}`);
      const all = seriesList.flatMap(series => series.data.map(point => Number(point.value))).filter(Number.isFinite);
      if (!all.length) { svg.innerHTML = `<text x="20" y="40" fill="#667585">표시할 데이터가 없습니다.</text>`; return; }
      let min = Math.min(...all), max = Math.max(...all);
      if (min === max) { min *= .99; max *= 1.01; }
      const x = (index,count) => left + (count <= 1 ? 0 : index / (count - 1) * (width-left-right));
      const y = value => top + (max-value)/(max-min)*(height-top-bottom);
      let markup = "";
      for (let index=0; index<=4; index++) {
        const value = max - (max-min)*index/4;
        const yy = y(value);
        markup += `<line x1="${left}" y1="${yy}" x2="${width-right}" y2="${yy}" stroke="#e2e8ec"/><text x="${left-8}" y="${yy+4}" text-anchor="end" fill="#667585" font-size="11">${esc(formatter(value))}</text>`;
      }
      seriesList.forEach(series => {
        const data = series.data.filter(point => Number.isFinite(Number(point.value)));
        if (!data.length) return;
        const points = data.map((point,index) => `${x(index,data.length)},${y(Number(point.value))}`).join(" ");
        markup += `<polyline fill="none" stroke="${series.color}" stroke-width="${series.width || 2.5}" points="${points}"/>`;
      });
      const first = seriesList.find(series=>series.data.length)?.data[0]?.date || "";
      const lastSeries = seriesList.find(series=>series.data.length)?.data || [];
      const last = lastSeries[lastSeries.length-1]?.date || "";
      markup += `<text x="${left}" y="${height-8}" fill="#667585" font-size="11">${esc(first)}</text><text x="${width-right}" y="${height-8}" text-anchor="end" fill="#667585" font-size="11">${esc(last)}</text>`;
      svg.innerHTML = markup;
    }
    function renderEtfCards() {
      $("etfCards").innerHTML = DATA.etf_guide.map(item => `<article class="card etf-card"><div class="etf-title"><h3>${esc(item.ticker)}</h3><span class="etf-role">${esc(item.role)}</span></div><div class="etf-meta"><div class="mini"><span>추종 대상</span><strong>${esc(item.tracks)}</strong></div><div class="mini"><span>최근 가격</span><strong>${usd(item.last)}</strong></div><div class="mini"><span>60일 평균 거래량</span><strong>${formatVolume(item.avg_volume_60d)}</strong></div></div><p><strong>무엇인가:</strong> ${esc(item.what)}</p><p><strong>프로그램에서:</strong> ${esc(item.use)}</p><p><strong>주의:</strong> ${esc(item.note)}</p></article>`).join("");
    }
    function renderDailyAdvice() {
      const advice = DATA.daily_advice || {};
      const positives = advice.positives || [];
      const cautions = advice.cautions || [];
      $("dailyAdvice").innerHTML = `<div><div class="daily-title"><h2>오늘 할 일: ${esc(advice.action || "보유·관찰")}</h2><span class="badge ${esc(advice.tone || "warn")}">${esc(advice.data_mode || "미국장 마감 종가 기준")}</span></div><p class="daily-copy">${esc(advice.summary || "다음 갱신을 기다립니다.")}</p><div class="chips">${positives.map(value=>`<span class="chip good">${esc(value)}</span>`).join("")}${cautions.map(value=>`<span class="chip warn">${esc(value)}</span>`).join("")}</div></div><div><div class="label">실행 순서</div><ol class="steps">${(advice.steps || []).map(step=>`<li>${esc(step)}</li>`).join("")}</ol><div class="hint" style="margin-top:10px">${esc(advice.refresh_rule || "")}</div></div>`;
    }
    function renderTimingTable() {
      table($("timingTable"),[
        {key:"ticker",label:"신호 지수"},
        {key:"execution_ticker",label:"매수 표시"},
        {key:"action",label:"지금 행동",help:HELP.action,render:value=>pill(value)},
        {key:"score",label:"종합점수",help:HELP.score,render:value=>num(value,1)},
        {key:"last",label:"종가",render:value=>usd(value)},
        {key:"mom_12_1",label:"12-1 모멘텀",help:HELP.momentum,render:value=>pct(Number(value)*100)},
        {key:"ichimoku_state",label:"일목",help:HELP.ichimoku},
        {key:"rsi14",label:"RSI",help:HELP.rsi,render:value=>num(value,1)},
        {key:"timing",label:"실행 기준"}
      ],DATA.qg_core_etfs);
    }
    let chartPeriod = 126;
    function selectedEtf() {
      return DATA.qg_core_etfs.find(row=>row.ticker===$("indexSelect").value) || DATA.qg_core_etfs[0] || {};
    }
    function renderIndexDetails() {
      const row = selectedEtf();
      const raw = (DATA.index_charts && DATA.index_charts[row.ticker]) || [];
      const data = raw.slice(-chartPeriod);
      const makeSeries = (key,color,width) => ({name:key,color,width,data:data.filter(point=>point[key]!==null).map(point=>({date:point.date,value:point[key]}))});
      lineChart($("indexChart"),[makeSeries("close","#17212b",2.8),makeSeries("sma20","#176b5b",1.8),makeSeries("sma50","#2463a7",1.8),makeSeries("sma200","#b42318",1.8)],value=>`$${value.toFixed(0)}`);
      $("indexChartMeta").textContent = data.length ? `${row.ticker} · ${data[0].date} ~ ${data[data.length-1].date} · ${row.action}` : "데이터 없음";
      const componentRows = [
        ["추세",row.trend_score,40,"20/50/100/200일선, EMA, MACD, 일목, ADX"],
        ["모멘텀",row.momentum_score,25,"1·3·6·12개월 수익률과 SPY 대비 상대강도"],
        ["진입 타이밍",row.timing_score,20,"RSI, 스토캐스틱, 볼린저, ATR 이격, 돌파·거래량"],
        ["위험",row.risk_score,15,"변동성, 낙폭, ATR 비율, VIX"]
      ];
      $("indicatorScores").innerHTML = componentRows.map(([name,value,max,description])=>`<div class="card"><div class="label">${esc(name)} · ${num(value,1)}/${max}</div><div class="value" style="font-size:20px">${pct(Number(value)/max*100)}</div><div class="hint">${esc(description)}</div></div>`).join("");
      const indicatorRows = [
        ["이동평균선",`20일 ${usd(row.sma20)} · 50일 ${usd(row.sma50)} · 200일 ${usd(row.sma200)}`,row.above_200d ? "종가가 200일선 위입니다." : "종가가 200일선 아래여서 신규매수를 제한합니다."],
        ["일목균형표",`${row.ichimoku_state} · 전환선 ${usd(row.tenkan)} · 기준선 ${usd(row.kijun)}`,HELP.ichimoku],
        ["MACD",`MACD ${num(row.macd,3)} · 신호선 ${num(row.macd_signal,3)} · 히스토그램 ${num(row.macd_hist,3)}`,HELP.macd],
        ["RSI·스토캐스틱",`RSI ${num(row.rsi14,1)} · %K ${num(row.stochastic_k,1)} · %D ${num(row.stochastic_d,1)}`,`${HELP.rsi} ${HELP.stochastic}`],
        ["볼린저·ATR",`밴드 위치 ${num(Number(row.bb_percent)*100,1)}% · ATR ${pct(Number(row.atr_pct)*100)}`,`${HELP.bb} ${HELP.atr}`],
        ["ADX·방향",`ADX ${num(row.adx14,1)} · +DI ${num(row.plus_di,1)} · -DI ${num(row.minus_di,1)}`,HELP.adx],
        ["중기 모멘텀",`3개월 ${pct(Number(row.ret_3m)*100)} · 6개월 ${pct(Number(row.ret_6m)*100)} · 12-1 ${pct(Number(row.mom_12_1)*100)}`,HELP.momentum],
        ["위험",`연환산 변동성 ${pct(Number(row.vol63)*100)} · 1년 낙폭 ${pct(Number(row.drawdown_252d)*100)}`,"변동성과 과거 낙폭이 클수록 같은 투자금에서도 손실 폭이 커질 수 있습니다."],
        ["거래량·돌파",`20/60일 거래량 비율 ${num(row.volume_ratio,2)} · 20일 돌파 ${row.breakout20 ? "예" : "아니오"}`,"가격 돌파가 평균 이상의 거래량과 함께 나오는지 확인합니다. 단독 매수 신호로 쓰지는 않습니다."]
      ];
      $("indicatorGrid").innerHTML = indicatorRows.map(([name,value,description])=>`<div class="indicator-item"><strong>${esc(name)}</strong><p>${esc(value)}</p><p style="margin-top:6px">${esc(description)}</p></div>`).join("");
    }
    function setupIndexChart() {
      $("indexSelect").innerHTML = DATA.qg_core_etfs.map(row=>`<option value="${esc(row.ticker)}">${esc(row.ticker)} · ${esc(row.action)}</option>`).join("");
      $("indexSelect").addEventListener("change",renderIndexDetails);
      document.querySelectorAll(".period").forEach(button=>button.addEventListener("click",()=>{
        document.querySelectorAll(".period").forEach(item=>item.classList.remove("active"));
        button.classList.add("active");
        chartPeriod = Number(button.dataset.days);
        renderIndexDetails();
      }));
      renderIndexDetails();
    }
    function renderPlan() {
      const capital = Math.max(0,Number($("capitalInput").value || 0));
      table($("planTable"),[
        {key:"asset",label:"목표 자산"},
        {key:"signal_asset",label:"신호 기준"},
        {key:"type",label:"역할"},
        {key:"action",label:"지금 행동",render:value=>pill(value)},
        {key:"weight",label:"목표 비중",render:value=>pct(Number(value)*100)},
        {key:"weight",label:"목표 평가액",render:value=>capital ? krw(capital*Number(value)) : `총 투자금 × ${pct(Number(value)*100)}`},
        {key:"timing",label:"실행 기준"}
      ],DATA.plan);
    }
    function setupCapital() {
      const stored = localStorage.getItem("quantGuardianCapitalKrw");
      if (stored) $("capitalInput").value = stored;
      $("capitalInput").addEventListener("input",()=>{
        const value = $("capitalInput").value;
        if (value) localStorage.setItem("quantGuardianCapitalKrw",value); else localStorage.removeItem("quantGuardianCapitalKrw");
        renderPlan();
      });
      $("clearCapital").addEventListener("click",()=>{ $("capitalInput").value=""; localStorage.removeItem("quantGuardianCapitalKrw"); renderPlan(); });
      renderPlan();
    }
    function showTooltip(target) {
      const tooltip = $("globalTooltip");
      if (!target.dataset.tip) return;
      tooltip.textContent = target.dataset.tip;
      tooltip.classList.add("active");
      const targetBox = target.getBoundingClientRect();
      const tipBox = tooltip.getBoundingClientRect();
      const margin = 12;
      let left = targetBox.left;
      let top = targetBox.bottom + 8;
      if (left + tipBox.width > window.innerWidth-margin) left = window.innerWidth-tipBox.width-margin;
      if (top + tipBox.height > window.innerHeight-margin) top = targetBox.top-tipBox.height-8;
      tooltip.style.left = `${Math.max(margin,left)}px`;
      tooltip.style.top = `${Math.max(margin,top)}px`;
    }
    function hideTooltip() { $("globalTooltip").classList.remove("active"); }
    function setupInstallPrompt() {
      let deferredPrompt = null;
      window.addEventListener("beforeinstallprompt",event=>{ event.preventDefault(); deferredPrompt=event; $("installApp").hidden=false; });
      $("installApp").addEventListener("click",async()=>{ if(!deferredPrompt)return; deferredPrompt.prompt(); await deferredPrompt.userChoice; deferredPrompt=null; $("installApp").hidden=true; });
      if ("serviceWorker" in navigator) navigator.serviceWorker.register("service-worker.js").catch(()=>{});
    }
    renderDailyAdvice();
    renderTimingTable();
    setupIndexChart();
    setupCapital();
    renderEtfCards();
    setupInstallPrompt();
    const decision = DATA.market_decision || {};
    const topEtf = DATA.qg_core_etfs.find(row=>row.ticker===DATA.daily_advice.top_etf_signal) || DATA.qg_core_etfs[0] || {};
    $("labelAction").innerHTML = tip("오늘의 판단",HELP.action);
    $("labelSignal").innerHTML = tip("우선 확인 ETF",HELP.score);
    $("labelEquity").innerHTML = tip("주식형 목표 비중",HELP.equity);
    $("labelMdd").innerHTML = tip("전략 백테스트 MDD",HELP.mdd);
    $("generatedAt").textContent = `마지막 계산 ${DATA.generated_at}`;
    $("marketAction").textContent = decision.action || "-";
    $("marketAction").className = `value ${toneFor(decision.action)}`;
    $("actionHint").textContent = `${decision.as_of || "-"} 기준 · 시장점수 ${num(decision.score,1)}/100`;
    $("signal").textContent = DATA.daily_advice.top_etf_execution || topEtf.execution_ticker || topEtf.ticker || "-";
    $("signalHint").textContent = topEtf.ticker ? `신호 ${topEtf.ticker} · ${topEtf.action} · ${num(topEtf.score,1)}점` : "-";
    $("equityTarget").textContent = pct(Number(decision.target_equity_weight || 0)*100);
    $("qgMdd").textContent = pct(DATA.qg_core_metrics.mdd_pct);
    barList($("weightBars"),DATA.plan,"weight",1,"asset",value=>pct(value*100));
    barList($("etfScoreBars"),DATA.qg_core_etfs,"score",100,"ticker",value=>num(value,1));
    $("marketFacts").innerHTML = [
      [tip("SPY 200일선","S&P 500 장기 추세 확인"),decision.market_above_200d ? "위" : "아래"],
      [tip("QQQ 200일선","나스닥100 장기 추세 확인"),decision.growth_above_200d ? "위" : "아래"],
      [tip("SPY 일목",HELP.ichimoku),decision.market_cloud || "-"],
      [tip("QQQ 일목",HELP.ichimoku),decision.growth_cloud || "-"],
      ["SPY 6개월",pct(Number(decision.market_6m_return || 0)*100)],
      ["QQQ 6개월",pct(Number(decision.growth_6m_return || 0)*100)],
      [tip("VIX","주식시장의 예상 변동성 지수입니다. 높을수록 가격 흔들림이 커질 가능성을 반영합니다."),num(decision.vix,1)]
    ].map(([key,value])=>`<div class="kv"><span>${key}</span><span>${esc(value)}</span></div>`).join("");
    $("marketExplain").textContent = `${decision.reason || ""}. 이 결과를 전체 투자금의 주식형 목표 비중으로 번역했습니다.`;
    lineChart($("equityChart"),[
      {name:"QG Index Timing",color:"#176b5b",data:DATA.charts.qg_core || []},
      {name:"SPY",color:"#2463a7",data:DATA.charts.spy || []},
      {name:"QQQ",color:"#a15c07",data:DATA.charts.qqq || []}
    ]);
    $("backtestCards").innerHTML = [
      [tip("전략 CAGR",HELP.cagr),pct(DATA.qg_core_metrics.cagr_pct)],
      [tip("전략 MDD",HELP.mdd),pct(DATA.qg_core_metrics.mdd_pct)],
      ["전략 Sharpe",num(DATA.qg_core_metrics.sharpe,3)],
      ["월 승률",pct(DATA.qg_core_metrics.win_rate_pct)],
      ["SPY CAGR / MDD",`${pct(DATA.benchmarks.spy_cagr_pct)} / ${pct(DATA.benchmarks.spy_mdd_pct)}`],
      ["QQQ CAGR / MDD",`${pct(DATA.benchmarks.qqq_cagr_pct)} / ${pct(DATA.benchmarks.qqq_mdd_pct)}`]
    ].map(([key,value])=>`<div class="card"><div class="label">${key}</div><div class="value" style="font-size:21px">${esc(value)}</div></div>`).join("");
    $("backtestExplain").textContent = `백테스트는 ${DATA.methodology.backtest_rule}, 매매 회전율에 거래비용 ${num(DATA.methodology.transaction_cost_bps,0)}bp를 적용했습니다. 현재 선정 ETF를 과거에도 그대로 사용한 결과라 상품 선택 편향과 환율·세금·체결 오차가 남습니다.`;
    $("termGrid").innerHTML = TERMS.map(([name,description])=>`<article class="card term"><strong>${esc(name)}</strong><p>${esc(description)}</p></article>`).join("");
    document.querySelectorAll(".tab").forEach(button=>button.addEventListener("click",()=>{
      document.querySelectorAll(".tab").forEach(item=>item.classList.remove("active"));
      document.querySelectorAll(".view").forEach(item=>item.classList.remove("active"));
      button.classList.add("active");
      $(button.dataset.view).classList.add("active");
    }));
    document.addEventListener("mouseover",event=>{ const target=event.target.closest(".tip"); if(target)showTooltip(target); });
    document.addEventListener("focusin",event=>{ const target=event.target.closest(".tip"); if(target)showTooltip(target); });
    document.addEventListener("mouseout",event=>{ if(event.target.closest(".tip"))hideTooltip(); });
    document.addEventListener("focusout",event=>{ if(event.target.closest(".tip"))hideTooltip(); });
    window.addEventListener("scroll",hideTooltip,true);
  </script>
</body>
</html>"""


def main() -> int:
    parser = argparse.ArgumentParser(description="Quant Guardian 지수 타이밍 HTML 대시보드 생성")
    parser.add_argument("--refresh", action="store_true", help="무료 가격 데이터를 새로 받아 생성")
    args = parser.parse_args()
    cfg = load_config(DEFAULT_CONFIG)
    paths = resolve_paths(cfg)
    payload = build_payload(refresh=args.refresh)
    html_text = HTML_TEMPLATE.replace("__DATA__", json.dumps(payload, ensure_ascii=False, allow_nan=False))
    output = paths.output / "dashboard.html"
    output.write_text(html_text, encoding="utf-8-sig")
    write_output_assets(paths, payload)
    print(output.resolve())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
