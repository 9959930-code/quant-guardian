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
    stock_scores,
)


ROOT = Path(__file__).resolve().parent
STATIC_ASSETS = ["manifest.webmanifest", "service-worker.js", "icon.svg"]


ETF_GUIDE = [
    {
        "ticker": "SPY",
        "role": "미국 대형주 기준선",
        "tracks": "S&P 500",
        "what": "미국 대표 대형주 약 500개에 분산 투자하는 ETF입니다.",
        "use": "QG-Core가 SPY보다 의미 있는지 비교하는 기본 벤치마크입니다.",
        "note": "장기 핵심 자산으로는 VOO, IVV, SPLG 같은 저비용 대안도 비교할 수 있습니다.",
    },
    {
        "ticker": "QQQ",
        "role": "나스닥100 신호 기준",
        "tracks": "Nasdaq-100",
        "what": "나스닥 상장 대형 비금융 100개 기업을 추종합니다.",
        "use": "성장주와 기술주 베타가 강한지 판단하는 핵심 기준입니다.",
        "note": "장기 매수 후보로는 비용이 낮은 QQQM을 함께 봅니다.",
    },
    {
        "ticker": "QQQM",
        "role": "QQQ 장기매수 대안",
        "tracks": "Nasdaq-100",
        "what": "QQQ와 같은 나스닥100 지수를 추종하는 Invesco ETF입니다.",
        "use": "QG-Core에서 QQQ 신호가 나올 때 실제 장기 매수 대안으로 검토합니다.",
        "note": "QQQ보다 거래량은 작지만 장기 보유 관점에서는 비용과 주당 가격이 유리할 수 있습니다.",
    },
    {
        "ticker": "SPYG",
        "role": "S&P500 성장주",
        "tracks": "S&P 500 Growth",
        "what": "S&P500 안에서 성장 성향이 강한 종목을 담습니다.",
        "use": "나스닥100에만 쏠리지 않는 성장주 노출로 비교합니다.",
        "note": "QQQ와 완전히 같은 상품이 아닙니다. 구성 방식과 업종 비중이 다릅니다.",
    },
    {
        "ticker": "XLK",
        "role": "기술 섹터",
        "tracks": "Technology Select Sector",
        "what": "S&P500의 기술 섹터에 집중 투자합니다.",
        "use": "기술주 주도장이 강할 때 QQQ와 함께 비교합니다.",
        "note": "소수 대형 기술주 비중이 높을 수 있어 집중 리스크가 있습니다.",
    },
    {
        "ticker": "SMH",
        "role": "반도체",
        "tracks": "Semiconductor",
        "what": "글로벌 반도체 기업에 집중 투자하는 ETF입니다.",
        "use": "AI/반도체 주도장이 이어지는지 확인하는 공격형 후보입니다.",
        "note": "변동성이 매우 클 수 있어 QG-Core에서는 코어 전체가 아니라 후보 중 하나로만 봅니다.",
    },
    {
        "ticker": "SGOV",
        "role": "초단기 국채 대기자금",
        "tracks": "0-3개월 미국 T-Bill",
        "what": "만기가 매우 짧은 미국 국채 ETF로 현금 대기처에 가깝습니다.",
        "use": "방어/중립 국면에서 위험자산 비중을 줄일 때 사용합니다.",
        "note": "SHY보다 금리 변동에 덜 민감한 편이라 실제 대기자금 후보로 둡니다.",
    },
    {
        "ticker": "SHY",
        "role": "백테스트용 단기채",
        "tracks": "1-3년 미국 국채",
        "what": "짧은 만기의 미국 국채에 투자합니다.",
        "use": "SGOV의 역사가 짧아 장기 백테스트에서는 SHY를 방어자산 프록시로 씁니다.",
        "note": "SGOV보다 금리 변화에 더 흔들릴 수 있습니다.",
    },
    {
        "ticker": "GLD",
        "role": "금 대체자산",
        "tracks": "금 가격",
        "what": "금 가격 흐름에 노출되는 ETF입니다.",
        "use": "주식과 채권이 함께 불안할 때 일부 분산 역할을 기대합니다.",
        "note": "수익을 만드는 엔진이라기보다 위기 분산 후보입니다.",
    },
    {
        "ticker": "TLT",
        "role": "장기채 방어자산",
        "tracks": "20년 이상 미국 국채",
        "what": "미국 장기 국채에 투자합니다.",
        "use": "금리 하락기나 경기 둔화 국면의 방어 보조 후보입니다.",
        "note": "금리가 오르면 가격이 크게 흔들릴 수 있습니다.",
    },
]


def clean_value(value):
    if pd.isna(value):
        return None
    if hasattr(value, "item"):
        return value.item()
    return value


def json_ready(value):
    if isinstance(value, dict):
        return {k: json_ready(v) for k, v in value.items()}
    if isinstance(value, list):
        return [json_ready(v) for v in value]
    if isinstance(value, tuple):
        return [json_ready(v) for v in value]
    try:
        if pd.isna(value):
            return None
    except (TypeError, ValueError):
        pass
    if hasattr(value, "item"):
        return value.item()
    return value


def records(df: pd.DataFrame, limit: int | None = None) -> list[dict]:
    if limit:
        df = df.head(limit)
    return [{k: clean_value(v) for k, v in row.items()} for row in df.to_dict(orient="records")]


def curve_records(series: pd.Series, limit: int = 220) -> list[dict]:
    series = series.dropna()
    if series.empty:
        return []
    if len(series) > limit:
        step = max(1, len(series) // limit)
        series = series.iloc[::step]
    return [{"date": idx.date().isoformat(), "value": round(float(value), 4)} for idx, value in series.items()]


def pct(value):
    if value is None or pd.isna(value):
        return None
    return round(float(value) * 100, 2)


def num(value, digits: int = 4):
    if value is None or pd.isna(value):
        return None
    return round(float(value), digits)


def build_etf_guide(cfg: dict, paths, refresh: bool) -> list[dict]:
    rows = []
    source = cfg["settings"].get("data_source", "yahoo")
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


def stock_chart_payload(scores: pd.DataFrame, cfg: dict, paths, limit: int = 25) -> dict[str, list[dict]]:
    if scores.empty or "ticker" not in scores:
        return {}
    source = cfg["settings"].get("data_source", "yahoo")
    charts = {}
    for ticker in scores["ticker"].head(limit):
        try:
            price = read_price(ticker, paths, refresh=False, source=source)
            close = price["Close"].dropna().tail(260)
            charts[ticker] = [
                {"date": idx.date().isoformat(), "value": round(float(value), 2)}
                for idx, value in close.items()
            ]
        except Exception:
            charts[ticker] = []
    return charts


def build_daily_advice(regime: dict, etfs: pd.DataFrame, scores: pd.DataFrame, plan: pd.DataFrame, cfg: dict) -> dict:
    top_etf = etfs.iloc[0].to_dict() if not etfs.empty else {}
    top_signal = str(top_etf.get("ticker", "-"))
    top_exec = execution_ticker(cfg, top_signal) if top_signal != "-" else "-"
    candidates = scores[scores["status"] == "매수후보"].head(5) if not scores.empty else pd.DataFrame()
    top_candidates = [
        {
            "ticker": row["ticker"],
            "sector": row.get("sector", "기타"),
            "score": round(float(row["quant_score"]), 1),
            "mom_12_1_pct": round(float(row["mom_12_1"]) * 100, 1),
            "ret_6m_pct": round(float(row["ret_6m"]) * 100, 1),
            "rsi14": round(float(row["rsi14"]), 1),
            "reason": row.get("reason", ""),
        }
        for _, row in candidates.iterrows()
    ]
    if regime["regime"] == "공격":
        action = "성장 노출 유지 / 분할편입 검토"
        tone = "good"
        summary = f"공격 국면입니다. 성장 ETF 1순위는 {top_exec}이며, 개별주는 상위 후보만 소량으로 얹는 구조가 맞습니다."
    elif regime["regime"] == "중립":
        action = "비중 조절 / 신규매수 신중"
        tone = "warn"
        summary = f"중립 국면입니다. {top_exec} 신호가 있더라도 전체 위험자산 비중은 낮추고 SGOV/GLD/TLT 비중을 함께 봅니다."
    else:
        action = "위험 축소 / 현금성 자산 우선"
        tone = "bad"
        summary = "방어 국면입니다. 신규 성장주 매수보다 SGOV 중심의 대기자금과 손실 방어가 우선입니다."
    steps = [
        f"기준일 {regime.get('as_of', '-')} 미국장 마감 데이터로 계산했습니다.",
        "매일 신호는 확인하되 실제 매매 판단은 주 1회 또는 월 1회로 제한합니다.",
        "QG-Core가 SPY/QQQ 단순보유보다 나은지 백테스트와 실전 기록으로 계속 비교합니다.",
    ]
    return {
        "action": action,
        "tone": tone,
        "summary": summary,
        "data_mode": "미국장 마감 종가 기준",
        "refresh_rule": "미국장 거래일 다음 한국 오전 자동 갱신",
        "top_etf_signal": top_signal,
        "top_etf_execution": top_exec,
        "top_etf_score": round(float(top_etf.get("score", 0)), 1) if top_etf else None,
        "top_candidates": top_candidates,
        "candidate_rule": "매수후보는 총점 75점 이상, RSI 72 이하, 가격이 200일선 위인 대형주입니다.",
        "score_rule": "ETF는 12-1개월/6개월/3개월 모멘텀, 200일선, RSI를 합산합니다. 개별주는 모멘텀, 추세, 리스크, 거래대금, RSI를 함께 봅니다.",
        "steps": steps,
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
        "regime": payload["regime"],
        "qg_core_metrics": payload["qg_core_metrics"],
        "benchmarks": payload["benchmarks"],
        "top_etfs": payload["qg_core_etfs"][:4],
        "top_scores": payload["scores"][:5],
        "plan": payload["plan"],
    }
    (paths.output / "daily.json").write_text(
        json.dumps(json_ready(daily), ensure_ascii=False, indent=2, allow_nan=False),
        encoding="utf-8",
    )


def write_output_assets(paths, payload: dict) -> None:
    write_static_assets(paths)
    write_daily_payload(paths, payload)


def build_payload(refresh: bool = False) -> dict:
    cfg = load_config(DEFAULT_CONFIG)
    paths = resolve_paths(cfg)
    full_report(cfg, paths, refresh=refresh)
    regime = market_regime(cfg, paths, refresh=False)
    etfs = qg_core_etf_scores(cfg, paths, refresh=False)
    scores = stock_scores(cfg, paths, refresh=False)
    plan = qg_core_portfolio_plan(cfg, paths, refresh=False)
    qg = qg_core_backtest(cfg, paths, refresh=False)
    returns = qg["returns"]
    qg_metrics = qg["metrics"].get("QG_CORE", {})
    spy_metrics = qg["metrics"].get("SPY", {})
    qqq_metrics = qg["metrics"].get("QQQ", {})

    payload = {
        "generated_at": datetime.now(ZoneInfo("Asia/Seoul")).strftime("%Y-%m-%d %H:%M:%S KST"),
        "regime": regime,
        "daily_advice": build_daily_advice(regime, etfs, scores, plan, cfg),
        "qg_core_metrics": {
            "cagr_pct": pct(qg_metrics.get("cagr")),
            "mdd_pct": pct(qg_metrics.get("mdd")),
            "sharpe": num(qg_metrics.get("sharpe"), 3),
            "sortino": num(qg_metrics.get("sortino"), 3),
            "calmar": num(qg_metrics.get("calmar"), 3),
            "win_rate_pct": pct(qg_metrics.get("win_rate")),
        },
        "benchmarks": {
            "spy_cagr_pct": pct(spy_metrics.get("cagr")),
            "spy_mdd_pct": pct(spy_metrics.get("mdd")),
            "qqq_cagr_pct": pct(qqq_metrics.get("cagr")),
            "qqq_mdd_pct": pct(qqq_metrics.get("mdd")),
        },
        "qg_core_etfs": records(etfs),
        "scores": records(scores, limit=60),
        "plan": records(plan),
        "etf_guide": build_etf_guide(cfg, paths, refresh=refresh),
        "stock_charts": stock_chart_payload(scores, cfg, paths),
        "charts": {
            "qg_core": curve_records((1 + returns["QG_CORE"].fillna(0)).cumprod()) if "QG_CORE" in returns else [],
            "spy": curve_records((1 + returns["SPY"].fillna(0)).cumprod()) if "SPY" in returns else [],
            "qqq": curve_records((1 + returns["QQQ"].fillna(0)).cumprod()) if "QQQ" in returns else [],
        },
    }
    return json_ready(payload)


HTML_TEMPLATE = r"""<!doctype html>
<html lang="ko">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta name="theme-color" content="#2563eb">
  <link rel="manifest" href="manifest.webmanifest">
  <link rel="icon" href="icon.svg" type="image/svg+xml">
  <title>Quant Guardian QG-Core</title>
  <style>
    :root { --bg:#f4f6f9; --panel:#fff; --ink:#172033; --muted:#687589; --line:#dfe5ef; --brand:#2563eb; --brand-soft:#e8f1ff; --green:#059669; --red:#dc2626; --amber:#b45309; --shadow:0 6px 20px rgba(23,32,51,.07); }
    * { box-sizing:border-box; }
    body { margin:0; background:var(--bg); color:var(--ink); font-family:Arial,"Malgun Gothic",sans-serif; letter-spacing:0; }
    header { background:#fff; border-bottom:1px solid var(--line); position:sticky; top:0; z-index:5; }
    .topbar { max-width:1200px; margin:0 auto; padding:14px 20px; display:flex; gap:14px; align-items:center; justify-content:space-between; }
    h1 { margin:0; font-size:22px; }
    .sub { color:var(--muted); font-size:13px; margin-top:4px; }
    .actions { display:flex; gap:8px; align-items:center; flex-wrap:wrap; }
    .button { border:1px solid var(--line); background:#fff; color:var(--ink); border-radius:8px; padding:9px 12px; font-size:13px; font-weight:800; text-decoration:none; cursor:pointer; }
    .button[hidden] { display:none; }
    main { max-width:1200px; margin:0 auto; padding:22px 20px 48px; }
    .notice { background:#fff7ed; border:1px solid #fed7aa; color:#7c2d12; border-radius:8px; padding:12px 14px; font-size:13px; line-height:1.55; margin-bottom:18px; }
    .grid { display:grid; gap:14px; }
    .summary { grid-template-columns:repeat(4,minmax(0,1fr)); }
    .card { min-width:0; background:var(--panel); border:1px solid var(--line); border-radius:8px; box-shadow:var(--shadow); padding:16px; }
    .daily { display:grid; grid-template-columns:minmax(0,1.25fr) minmax(260px,.75fr); gap:18px; margin-bottom:16px; }
    .daily h2 { margin:0; font-size:19px; }
    .daily-title { display:flex; align-items:center; gap:10px; flex-wrap:wrap; margin-bottom:8px; }
    .badge { display:inline-flex; align-items:center; min-height:26px; padding:4px 9px; border-radius:999px; background:#eef2ff; color:var(--brand); font-size:12px; font-weight:900; }
    .badge.good { background:#dcfce7; color:#166534; }
    .badge.warn { background:#fef3c7; color:#92400e; }
    .badge.bad { background:#fee2e2; color:#991b1b; }
    .daily-copy { margin:0; color:var(--muted); line-height:1.6; font-size:13px; }
    .steps { margin:10px 0 0; padding-left:18px; color:var(--muted); font-size:13px; line-height:1.55; }
    .chips { display:flex; flex-wrap:wrap; gap:8px; margin-top:10px; }
    .chip { display:inline-flex; gap:6px; align-items:center; border:1px solid var(--line); border-radius:999px; padding:6px 9px; background:#f8fafc; font-size:12px; font-weight:900; }
    .chip span { color:var(--muted); font-weight:700; }
    .label { color:var(--muted); font-size:12px; font-weight:800; }
    .value { margin-top:8px; font-size:26px; font-weight:900; }
    .hint { margin-top:6px; color:var(--muted); font-size:12px; line-height:1.45; }
    .positive { color:var(--green); }
    .negative { color:var(--red); }
    .tabs { display:flex; gap:6px; margin:20px 0 12px; flex-wrap:wrap; }
    .tab { border:1px solid var(--line); background:#fff; border-radius:8px; padding:9px 12px; font-weight:800; cursor:pointer; }
    .tab.active { background:var(--brand-soft); color:var(--brand); border-color:#bfdbfe; }
    .view { display:none; }
    .view.active { display:block; }
    .head { margin:14px 0 10px; display:flex; align-items:end; justify-content:space-between; gap:12px; }
    .head h2 { margin:0; font-size:18px; }
    .head p { margin:4px 0 0; color:var(--muted); font-size:13px; line-height:1.45; }
    .split { display:grid; grid-template-columns:minmax(0,1fr) 360px; gap:14px; }
    .split > * { min-width:0; }
    .table-wrap { max-width:100%; min-width:0; overflow:auto; background:#fff; border:1px solid var(--line); border-radius:8px; box-shadow:var(--shadow); }
    table { width:100%; min-width:960px; border-collapse:collapse; }
    th,td { padding:11px 12px; border-bottom:1px solid var(--line); text-align:left; font-size:13px; vertical-align:top; word-break:keep-all; }
    th { background:#f8fafc; color:var(--muted); font-size:12px; }
    tr:last-child td { border-bottom:0; }
    .pill { display:inline-flex; min-height:24px; align-items:center; padding:3px 8px; border-radius:999px; background:#f1f5f9; color:#334155; font-weight:900; font-size:12px; white-space:nowrap; }
    .pill.good { background:#dcfce7; color:#166534; }
    .pill.warn { background:#fef3c7; color:#92400e; }
    .pill.bad { background:#fee2e2; color:#991b1b; }
    .kv { display:grid; grid-template-columns:1fr auto; gap:10px; padding:9px 0; border-bottom:1px solid var(--line); }
    .kv:last-child { border-bottom:0; }
    .kv span:first-child { color:var(--muted); }
    .kv span:last-child { font-weight:900; }
    .explain { margin-top:10px; color:var(--muted); font-size:13px; line-height:1.55; }
    .chart-grid { grid-template-columns:repeat(2,minmax(0,1fr)); margin:14px 0; }
    .chart-card h3 { margin:0 0 4px; font-size:15px; }
    .chart-card p { margin:0 0 12px; color:var(--muted); font-size:13px; line-height:1.45; }
    .chart { width:100%; height:240px; display:block; overflow:visible; }
    .chart-legend { display:flex; flex-wrap:wrap; gap:10px; margin-top:8px; color:var(--muted); font-size:12px; }
    .legend-dot { display:inline-block; width:10px; height:10px; border-radius:50%; margin-right:5px; }
    .stock-controls { display:flex; flex-wrap:wrap; gap:8px; align-items:center; margin-bottom:10px; }
    .stock-controls select { min-width:140px; border:1px solid var(--line); border-radius:8px; padding:9px 10px; background:#fff; font-weight:800; }
    .period { border:1px solid var(--line); background:#fff; border-radius:8px; padding:8px 10px; font-weight:800; cursor:pointer; }
    .period.active { background:var(--brand-soft); color:var(--brand); border-color:#bfdbfe; }
    .bar-list { display:grid; gap:9px; }
    .bar-row { display:grid; grid-template-columns:82px minmax(0,1fr) 60px; align-items:center; gap:10px; font-size:13px; }
    .bar-track { height:10px; background:#eef2f7; border-radius:999px; overflow:hidden; }
    .bar-fill { height:100%; background:var(--brand); border-radius:999px; }
    .etf-cards { display:grid; grid-template-columns:repeat(2,minmax(0,1fr)); gap:14px; margin-top:14px; }
    .etf-card { display:grid; gap:10px; }
    .etf-title { display:flex; align-items:baseline; justify-content:space-between; gap:10px; }
    .etf-title h3 { margin:0; font-size:18px; }
    .etf-role { color:var(--brand); font-weight:900; font-size:13px; }
    .etf-meta { display:grid; grid-template-columns:repeat(3,minmax(0,1fr)); gap:8px; }
    .mini { background:#f8fafc; border:1px solid var(--line); border-radius:8px; padding:8px; }
    .mini span { display:block; color:var(--muted); font-size:11px; margin-bottom:4px; }
    .mini strong { font-size:13px; }
    .etf-card p { margin:0; color:var(--muted); font-size:13px; line-height:1.55; }
    .tip { position:relative; display:inline-flex; align-items:center; gap:4px; cursor:help; border-bottom:1px dotted #94a3b8; white-space:nowrap; }
    .tip::after { content:"?"; display:inline-grid; place-items:center; width:16px; height:16px; border-radius:50%; background:#eef2ff; color:var(--brand); font-size:11px; font-weight:900; }
    .global-tooltip { display:none; position:fixed; z-index:1000; max-width:min(620px, calc(100vw - 32px)); width:max-content; background:#172033; color:#fff; padding:10px 12px; border-radius:8px; box-shadow:var(--shadow); font-size:12px; line-height:1.5; font-weight:500; white-space:normal; overflow-wrap:anywhere; pointer-events:none; }
    .global-tooltip.active { display:block; }
    .term-grid { grid-template-columns:repeat(3,minmax(0,1fr)); }
    .term { min-height:120px; }
    .term strong { display:block; margin-bottom:8px; }
    .term p { margin:0; color:var(--muted); font-size:13px; line-height:1.55; }
    @media (max-width:900px) { .summary,.daily,.split,.chart-grid,.etf-cards,.term-grid { grid-template-columns:1fr; } .topbar { flex-direction:column; align-items:flex-start; } }
  </style>
</head>
<body>
  <header>
    <div class="topbar">
      <div>
        <h1>Quant Guardian QG-Core</h1>
        <div class="sub" id="generatedAt"></div>
      </div>
      <div class="actions">
        <span class="tip" tabindex="0" data-tip="화면에 보이는 내용을 텍스트로 저장한 백업본입니다. 검산, 기록, 공유가 필요할 때만 열면 됩니다.">리포트 원문</span>
        <a class="button" href="report.md">열기</a>
        <button class="button" id="installApp" hidden>앱 설치</button>
      </div>
    </div>
  </header>
  <main>
    <div class="notice">투자 추천이 아닙니다. 이 화면은 무료 데이터 기반의 퀀트 점검 도구이며 자동주문을 하지 않습니다. 실제 매매 전에는 계좌 비중, 세금, 수수료, 환율, 실적 발표 일정을 직접 확인해야 합니다.</div>
    <section class="card daily" id="dailyAdvice"></section>
    <div class="grid summary">
      <div class="card"><div class="label" id="labelRegime"></div><div class="value" id="regime"></div><div class="hint" id="regimeHint"></div></div>
      <div class="card"><div class="label" id="labelSignal"></div><div class="value" id="signal"></div><div class="hint" id="signalHint"></div></div>
      <div class="card"><div class="label" id="labelQGCagr"></div><div class="value positive" id="qgCagr"></div><div class="hint">QG-Core 월간 백테스트</div></div>
      <div class="card"><div class="label" id="labelQGMdd"></div><div class="value negative" id="qgMdd"></div><div class="hint">최대 낙폭. 작을수록 좋습니다.</div></div>
    </div>
    <div class="card explain" id="currentRead"></div>
    <div class="grid chart-grid">
      <div class="card chart-card">
        <h3>QG-Core 추천 비중</h3>
        <p>현재 시장국면을 기준으로 성장 ETF, 대형주 위성, 방어자산을 나눠 봅니다.</p>
        <div id="weightBars" class="bar-list"></div>
      </div>
      <div class="card chart-card">
        <h3>성장 ETF 점수</h3>
        <p>QQQ, SPYG, XLK, SMH를 모멘텀과 추세 기준으로 비교합니다.</p>
        <div id="etfScoreBars" class="bar-list"></div>
      </div>
    </div>
    <div class="tabs">
      <button class="tab active" data-view="core">QG-Core</button>
      <button class="tab" data-view="scanner">종목 스캐너</button>
      <button class="tab" data-view="portfolio">비중 제안</button>
      <button class="tab" data-view="etfs">ETF 설명</button>
      <button class="tab" data-view="backtest">백테스트</button>
      <button class="tab" data-view="terms">용어</button>
    </div>
    <section id="core" class="view active">
      <div class="split">
        <div>
          <div class="head"><div><h2>상위 대형주 후보</h2><p>개별주는 포트폴리오의 전부가 아니라 25% 이하 위성으로만 봅니다.</p></div></div>
          <div class="table-wrap"><table id="topScores"></table></div>
        </div>
        <div class="card">
          <div class="head"><div><h2>시장 체크</h2><p>공격/중립/방어 판단 근거입니다.</p></div></div>
          <div id="regimeFacts"></div>
          <div class="explain" id="regimeExplain"></div>
        </div>
      </div>
    </section>
    <section id="scanner" class="view">
      <div class="head"><div><h2>종목 스캐너</h2><p>나스닥100/S&P100급 대형주 후보를 점수화합니다. 점수는 매수 명령이 아니라 검토 우선순위입니다.</p></div></div>
      <div class="card chart-card">
        <h3>종목 최근 차트</h3>
        <p>상위 후보의 최근 흐름을 기간별로 봅니다.</p>
        <div class="stock-controls">
          <select id="stockSelect" aria-label="차트로 볼 종목 선택"></select>
          <button class="period" data-days="21">1M</button>
          <button class="period active" data-days="63">3M</button>
          <button class="period" data-days="126">6M</button>
          <button class="period" data-days="252">1Y</button>
        </div>
        <svg id="stockChart" class="chart" role="img" aria-label="종목 최근 차트"></svg>
        <div class="hint" id="stockChartMeta"></div>
      </div>
      <div class="table-wrap"><table id="scoreTable"></table></div>
    </section>
    <section id="portfolio" class="view">
      <div class="head"><div><h2>QG-Core 비중 제안</h2><p>공격 국면은 성장 ETF 60% + 대형주 25% + SGOV 15%, 중립/방어는 위험자산을 줄입니다.</p></div></div>
      <div class="table-wrap"><table id="planTable"></table></div>
    </section>
    <section id="etfs" class="view">
      <div class="head"><div><h2>ETF 설명</h2><p>QG-Core에서 쓰는 ETF의 역할입니다. QQQ 신호는 장기 매수 대안으로 QQQM을 함께 봅니다.</p></div></div>
      <div id="etfCards" class="etf-cards"></div>
    </section>
    <section id="backtest" class="view">
      <div class="head"><div><h2>QG-Core 백테스트</h2><p>과거 규칙 적용 결과입니다. 미래 수익 보장이 아니며 SPY/QQQ와 비교해야 의미가 있습니다.</p></div></div>
      <div class="card chart-card">
        <h3>누적 수익 곡선</h3>
        <svg id="equityChart" class="chart" role="img" aria-label="백테스트 누적 수익 곡선"></svg>
        <div class="chart-legend">
          <span><i class="legend-dot" style="background:#2563eb"></i>QG-Core</span>
          <span><i class="legend-dot" style="background:#059669"></i>SPY</span>
          <span><i class="legend-dot" style="background:#b45309"></i>QQQ</span>
        </div>
      </div>
      <div class="grid summary" id="backtestCards"></div>
    </section>
    <section id="terms" class="view">
      <div class="head"><div><h2>용어 설명</h2><p>처음 보는 사람이 이해할 수 있도록 주요 항목을 풀었습니다.</p></div></div>
      <div class="grid term-grid" id="termGrid"></div>
    </section>
  </main>
  <div id="globalTooltip" class="global-tooltip"></div>
  <script>
    const DATA = __DATA__;
    const $ = id => document.getElementById(id);
    const HELP = {
      regime:"SPY와 QQQ의 200일선, 6개월 수익률, VIX를 합산해 공격/중립/방어를 정합니다.",
      signal:"성장 ETF 후보 중 모멘텀과 추세 점수가 가장 높은 후보입니다. QQQ가 1순위면 장기 매수 대안으로 QQQM도 함께 봅니다.",
      cagr:"연평균 복리수익률입니다. 높을수록 좋지만 백테스트 수익은 미래를 보장하지 않습니다.",
      mdd:"Max Drawdown. 과거 고점 대비 가장 크게 빠진 낙폭입니다.",
      score:"ETF는 12-1개월, 6개월, 3개월 모멘텀과 200일선, RSI를 합산합니다. 개별주는 모멘텀, 추세, 리스크, 거래대금, RSI를 함께 봅니다.",
      status:"매수후보, 관찰, 과열주의, 제외 중 하나입니다. 매수후보도 즉시 매수 명령은 아닙니다.",
      mom:"최근 1개월을 제외한 12개월 수익률입니다. 단기 급등 노이즈를 줄이기 위한 모멘텀 지표입니다.",
      rsi:"최근 상승/하락 속도를 보는 지표입니다. 45~65는 무난, 65~72는 추격주의, 72 초과는 과열로 봅니다.",
      volume:"최근 60거래일 평균 거래량과 가격을 곱한 대략적인 거래대금입니다.",
      shySgov:"SGOV는 0~3개월 초단기 국채라 현금 대기처에 가깝고, SHY는 1~3년 국채라 금리 변화에 더 민감합니다. 장기 백테스트는 SHY, 실제 대기자금 후보는 SGOV를 봅니다."
    };
    const TERMS = [
      ["QG-Core", "성장 ETF를 핵심 엔진으로 쓰되 시장이 나쁠 때 SGOV/GLD/TLT로 후퇴하는 공격형 퀀트 모델입니다."],
      ["시장국면", HELP.regime],
      ["성장 ETF 코어", "QQQ/QQQM, SPYG, XLK, SMH 같은 성장주·기술주 ETF 후보입니다."],
      ["대형주 위성", "전체 포트폴리오의 25% 이하로만 쓰는 개별주 후보입니다. 5개 내외로 시작합니다."],
      ["12-1 모멘텀", HELP.mom],
      ["RSI", HELP.rsi],
      ["CAGR", HELP.cagr],
      ["MDD", HELP.mdd],
      ["SGOV와 SHY", HELP.shySgov]
    ];
    const esc = v => String(v ?? "").replace(/[&<>"']/g, ch => ({ "&":"&amp;", "<":"&lt;", ">":"&gt;", '"':"&quot;", "'":"&#39;" }[ch]));
    const pct = v => v === null || v === undefined || Number.isNaN(Number(v)) ? "-" : `${Number(v).toFixed(2)}%`;
    const num = (v,d=2) => v === null || v === undefined || Number.isNaN(Number(v)) ? "-" : Number(v).toFixed(d);
    const volume = v => v === null || v === undefined || Number.isNaN(Number(v)) ? "-" : Number(v).toLocaleString("ko-KR");
    const tip = (label, text) => `<span class="tip" tabindex="0" data-tip="${esc(text)}">${esc(label)}</span>`;
    const pill = v => {
      const cls = v === "매수후보" ? "good" : v === "과열주의" || v === "관찰" ? "warn" : v === "방어" || v === "제외" ? "bad" : "";
      return `<span class="pill ${cls}">${esc(v || "-")}</span>`;
    };
    function table(el, cols, rows) {
      el.innerHTML = `<thead><tr>${cols.map(c=>`<th>${c.help ? tip(c.label, c.help) : esc(c.label)}</th>`).join("")}</tr></thead><tbody>${rows.map(r=>`<tr>${cols.map(c=>`<td>${c.render ? c.render(r[c.key], r) : esc(r[c.key] ?? "-")}</td>`).join("")}</tr>`).join("")}</tbody>`;
    }
    function barList(el, rows, valueKey, maxValue, labelKey, formatter) {
      el.innerHTML = rows.map(row => {
        const value = Number(row[valueKey] ?? 0);
        const width = Math.max(2, Math.min(100, maxValue ? value / maxValue * 100 : 0));
        return `<div class="bar-row"><strong>${esc(row[labelKey])}</strong><div class="bar-track"><div class="bar-fill" style="width:${width}%"></div></div><span>${formatter(value)}</span></div>`;
      }).join("");
    }
    function lineChart(el, seriesList, labelFormatter = v => `${v.toFixed(1)}x`) {
      const width = 720, height = 240, pad = 30;
      const points = seriesList.flatMap(s => s.data.map((p, i) => ({ x:i, y:Number(p.value) })));
      if (!points.length) {
        el.innerHTML = `<text x="20" y="40" fill="#687589">표시할 그래프 데이터가 없습니다.</text>`;
        return;
      }
      const maxLen = Math.max(...seriesList.map(s => s.data.length));
      const minY = Math.min(...points.map(p => p.y));
      const maxY = Math.max(...points.map(p => p.y));
      const yRange = maxY - minY || 1;
      const path = data => data.map((p, i) => {
        const x = pad + (maxLen <= 1 ? 0 : i / (maxLen - 1) * (width - pad * 2));
        const y = height - pad - ((Number(p.value) - minY) / yRange * (height - pad * 2));
        return `${i === 0 ? "M" : "L"}${x.toFixed(1)},${y.toFixed(1)}`;
      }).join(" ");
      const lines = seriesList.map(s => `<path d="${path(s.data)}" fill="none" stroke="${s.color}" stroke-width="2.7" stroke-linecap="round" stroke-linejoin="round"></path>`).join("");
      el.setAttribute("viewBox", `0 0 ${width} ${height}`);
      el.innerHTML = `<line x1="${pad}" y1="${height-pad}" x2="${width-pad}" y2="${height-pad}" stroke="#dfe5ef"></line><line x1="${pad}" y1="${pad}" x2="${pad}" y2="${height-pad}" stroke="#dfe5ef"></line><text x="${pad}" y="18" fill="#687589" font-size="12">${labelFormatter(maxY)}</text><text x="${pad}" y="${height-6}" fill="#687589" font-size="12">${labelFormatter(minY)}</text>${lines}`;
    }
    function renderEtfCards(el, rows) {
      el.innerHTML = rows.map(row => `<div class="card etf-card"><div class="etf-title"><h3>${esc(row.ticker)}</h3><span class="etf-role">${esc(row.role)}</span></div><div class="etf-meta"><div class="mini"><span>추종/노출</span><strong>${esc(row.tracks)}</strong></div><div class="mini"><span>최근 종가</span><strong>${row.last === null ? "-" : "$" + num(row.last, 2)}</strong></div><div class="mini"><span>60일 평균 거래량</span><strong>${volume(row.avg_volume_60d)}</strong></div></div><p><strong>무엇인가:</strong> ${esc(row.what)}</p><p><strong>언제 보는가:</strong> ${esc(row.use)}</p><p><strong>메모:</strong> ${esc(row.note)}</p></div>`).join("");
    }
    function renderDailyAdvice() {
      const advice = DATA.daily_advice || {};
      const candidates = advice.top_candidates || [];
      $("dailyAdvice").innerHTML = `<div><div class="daily-title"><h2>오늘의 행동: ${esc(advice.action || "유지 / 관찰")}</h2><span class="badge ${esc(advice.tone || "")}">${esc(advice.data_mode || "미국장 마감 종가 기준")}</span></div><p class="daily-copy">${esc(advice.summary || "오늘은 기존 포트폴리오를 유지하고 다음 갱신을 기다립니다.")}</p><div class="chips">${candidates.length ? candidates.map(item => `<span class="chip">${esc(item.ticker)}<span>${esc(item.sector)} · ${num(item.score, 1)}점</span></span>`).join("") : `<span class="chip">신규 강한 후보 없음</span>`}</div></div><div><div class="label">갱신 방식</div><div class="value" style="font-size:18px">${esc(advice.refresh_rule || "미국장 거래일 다음 한국 오전 자동 갱신")}</div><ol class="steps">${(advice.steps || []).map(step => `<li>${esc(step)}</li>`).join("")}</ol></div>`;
    }
    function showTooltip(target) {
      const tooltip = $("globalTooltip");
      const text = target.dataset.tip;
      if (!tooltip || !text) return;
      tooltip.textContent = text;
      tooltip.classList.add("active");
      const targetBox = target.getBoundingClientRect();
      const tipBox = tooltip.getBoundingClientRect();
      const margin = 12;
      let left = targetBox.left;
      let top = targetBox.bottom + 8;
      if (left + tipBox.width > window.innerWidth - margin) left = window.innerWidth - tipBox.width - margin;
      if (top + tipBox.height > window.innerHeight - margin) top = targetBox.top - tipBox.height - 8;
      tooltip.style.left = `${Math.max(margin, left)}px`;
      tooltip.style.top = `${Math.max(margin, top)}px`;
    }
    function hideTooltip() { const tooltip = $("globalTooltip"); if (tooltip) tooltip.classList.remove("active"); }
    document.addEventListener("mouseover", e => { const t = e.target.closest(".tip"); if (t) showTooltip(t); });
    document.addEventListener("focusin", e => { const t = e.target.closest(".tip"); if (t) showTooltip(t); });
    document.addEventListener("mouseout", e => { if (e.target.closest(".tip")) hideTooltip(); });
    document.addEventListener("focusout", e => { if (e.target.closest(".tip")) hideTooltip(); });
    window.addEventListener("scroll", hideTooltip, true);
    let stockPeriod = 63;
    function renderStockChart() {
      const select = $("stockSelect");
      const chart = $("stockChart");
      if (!select || !chart) return;
      const ticker = select.value;
      const raw = (DATA.stock_charts && DATA.stock_charts[ticker]) || [];
      const data = raw.slice(-stockPeriod);
      lineChart(chart, [{name:ticker, color:"#2563eb", data}], v => `$${v.toFixed(2)}`);
      const range = data.length ? `${data[0].date} - ${data[data.length - 1].date}` : "데이터 없음";
      $("stockChartMeta").textContent = `${ticker} ${range}`;
    }
    function setupStockChart() {
      const select = $("stockSelect");
      if (!select) return;
      const tickers = DATA.scores.slice(0, 25).map(row => row.ticker);
      select.innerHTML = tickers.map(ticker => `<option value="${esc(ticker)}">${esc(ticker)}</option>`).join("");
      select.addEventListener("change", renderStockChart);
      document.querySelectorAll(".period").forEach(btn => btn.addEventListener("click", () => {
        document.querySelectorAll(".period").forEach(item => item.classList.remove("active"));
        btn.classList.add("active");
        stockPeriod = Number(btn.dataset.days);
        renderStockChart();
      }));
      renderStockChart();
    }
    function setupInstallPrompt() {
      const installButton = $("installApp");
      let deferredPrompt = null;
      window.addEventListener("beforeinstallprompt", event => {
        event.preventDefault();
        deferredPrompt = event;
        installButton.hidden = false;
      });
      installButton.addEventListener("click", async () => {
        if (!deferredPrompt) return;
        deferredPrompt.prompt();
        await deferredPrompt.userChoice;
        deferredPrompt = null;
        installButton.hidden = true;
      });
      if ("serviceWorker" in navigator) navigator.serviceWorker.register("service-worker.js").catch(() => {});
    }
    renderDailyAdvice();
    setupInstallPrompt();
    $("labelRegime").innerHTML = tip("시장 모드", HELP.regime);
    $("labelSignal").innerHTML = tip("ETF 1순위", HELP.signal);
    $("labelQGCagr").innerHTML = tip("QG-Core CAGR", HELP.cagr);
    $("labelQGMdd").innerHTML = tip("QG-Core MDD", HELP.mdd);
    $("generatedAt").textContent = `마지막 계산 ${DATA.generated_at}`;
    $("regime").textContent = DATA.regime.regime;
    $("regimeHint").textContent = `${DATA.regime.as_of} 기준, ${DATA.regime.score}/${DATA.regime.max_score}점`;
    const topEtf = DATA.qg_core_etfs[0] || {};
    $("signal").textContent = DATA.daily_advice.top_etf_execution || topEtf.ticker || "-";
    $("signalHint").textContent = topEtf.ticker ? `신호 기준 ${topEtf.ticker}, 점수 ${num(topEtf.score, 1)}, 12-1M ${pct(Number(topEtf.mom_12_1)*100)}` : "-";
    $("qgCagr").textContent = pct(DATA.qg_core_metrics.cagr_pct);
    $("qgMdd").textContent = pct(DATA.qg_core_metrics.mdd_pct);
    $("currentRead").textContent = `현재 해석: 시장은 ${DATA.regime.regime} 모드입니다. QG-Core는 성장주 베타를 무조건 크게 가져가는 전략이 아니라, 시장국면이 좋을 때만 QQQM/SPYG/XLK/SMH 같은 성장 ETF 노출을 키우고, 나쁠 때는 SGOV/GLD/TLT 비중을 높이는 전략입니다.`;
    $("regimeFacts").innerHTML = [
      [tip("SPY 200일선 위", "SPY가 200일 평균 가격보다 위이면 미국 대형주 시장의 장기 추세가 살아 있다고 봅니다."), DATA.regime.market_above_200d === true ? "예" : DATA.regime.market_above_200d === false ? "아니오" : "없음"],
      [tip("QQQ 200일선 위", "QQQ가 200일 평균 가격보다 위이면 성장주/기술주의 장기 추세가 살아 있다고 봅니다."), DATA.regime.growth_above_200d === true ? "예" : DATA.regime.growth_above_200d === false ? "아니오" : "없음"],
      [tip("SPY 6개월", "최근 6개월 SPY 수익률입니다."), pct((DATA.regime.market_6m_return ?? 0) * 100)],
      [tip("QQQ 6개월", "최근 6개월 QQQ 수익률입니다."), pct((DATA.regime.growth_6m_return ?? 0) * 100)],
      [tip("VIX", "시장 공포지수입니다. 보통 20 아래면 안정, 20 위면 경계로 봅니다."), num(DATA.regime.vix, 2)]
    ].map(([k,v])=>`<div class="kv"><span>${k}</span><span>${esc(v)}</span></div>`).join("");
    $("regimeExplain").textContent = "5개 조건 중 4개 이상이면 공격, 2~3개면 중립, 그보다 낮으면 방어로 봅니다.";
    const scoreCols = [
      {key:"ticker", label:"티커"},
      {key:"sector", label:"업종"},
      {key:"quant_score", label:"총점", help:HELP.score, render:v=>num(v,1)},
      {key:"status", label:"상태", help:HELP.status, render:v=>pill(v)},
      {key:"mom_12_1", label:"12-1 모멘텀", help:HELP.mom, render:v=>pct(Number(v)*100)},
      {key:"ret_6m", label:"6개월", render:v=>pct(Number(v)*100)},
      {key:"rsi14", label:"RSI", help:HELP.rsi, render:v=>num(v,1)},
      {key:"dollar_volume_m", label:"거래대금($M)", help:HELP.volume, render:v=>num(v,1)},
      {key:"reason", label:"이유"}
    ];
    table($("topScores"), scoreCols, DATA.scores.slice(0,8));
    table($("scoreTable"), scoreCols, DATA.scores);
    setupStockChart();
    table($("planTable"), [
      {key:"asset", label:"자산"},
      {key:"signal_asset", label:"신호 기준"},
      {key:"type", label:"구분"},
      {key:"weight", label:"비중", render:v=>pct(Number(v)*100)},
      {key:"reason", label:"이유"}
    ], DATA.plan);
    barList($("weightBars"), DATA.plan, "weight", 1, "asset", v => pct(v * 100));
    barList($("etfScoreBars"), DATA.qg_core_etfs, "score", 100, "ticker", v => num(v, 1));
    renderEtfCards($("etfCards"), DATA.etf_guide);
    lineChart($("equityChart"), [
      {name:"QG-Core", color:"#2563eb", data:DATA.charts.qg_core || []},
      {name:"SPY", color:"#059669", data:DATA.charts.spy || []},
      {name:"QQQ", color:"#b45309", data:DATA.charts.qqq || []}
    ]);
    $("backtestCards").innerHTML = [
      [tip("QG-Core CAGR", HELP.cagr), pct(DATA.qg_core_metrics.cagr_pct)],
      [tip("QG-Core MDD", HELP.mdd), pct(DATA.qg_core_metrics.mdd_pct)],
      ["QG-Core Sharpe", num(DATA.qg_core_metrics.sharpe, 3)],
      ["SPY CAGR", pct(DATA.benchmarks.spy_cagr_pct)],
      ["QQQ CAGR", pct(DATA.benchmarks.qqq_cagr_pct)],
      ["QQQ MDD", pct(DATA.benchmarks.qqq_mdd_pct)]
    ].map(([k,v])=>`<div class="card"><div class="label">${k}</div><div class="value">${esc(v)}</div></div>`).join("");
    $("termGrid").innerHTML = TERMS.map(([k,v])=>`<div class="card term"><strong>${esc(k)}</strong><p>${esc(v)}</p></div>`).join("");
    document.querySelectorAll(".tab").forEach(btn => btn.addEventListener("click", () => {
      document.querySelectorAll(".tab").forEach(x=>x.classList.remove("active"));
      document.querySelectorAll(".view").forEach(x=>x.classList.remove("active"));
      btn.classList.add("active");
      $(btn.dataset.view).classList.add("active");
    }));
  </script>
</body>
</html>"""


def main() -> int:
    parser = argparse.ArgumentParser(description="Quant Guardian QG-Core HTML 대시보드 생성")
    parser.add_argument("--refresh", action="store_true", help="무료 가격 데이터를 새로 받아 생성")
    args = parser.parse_args()
    cfg = load_config(DEFAULT_CONFIG)
    paths = resolve_paths(cfg)
    payload = build_payload(refresh=args.refresh)
    html = HTML_TEMPLATE.replace("__DATA__", json.dumps(payload, ensure_ascii=False, allow_nan=False))
    out = paths.output / "dashboard.html"
    out.write_text(html, encoding="utf-8-sig")
    write_output_assets(paths, payload)
    print(out.resolve())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
