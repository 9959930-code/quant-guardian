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
    etf_backtest,
    full_report,
    latest_etf_signal,
    load_config,
    market_regime,
    portfolio_plan,
    read_price,
    resolve_paths,
    stock_rotation_backtest,
    stock_scores,
)


ROOT = Path(__file__).resolve().parent
STATIC_ASSETS = ["manifest.webmanifest", "service-worker.js", "icon.svg"]


ETF_GUIDE = [
    {
        "ticker": "SPY",
        "role": "미국 대형주 기준",
        "tracks": "S&P 500",
        "what": "미국 대표 대형주 약 500개에 분산 투자하는 ETF입니다.",
        "use": "미국 주식시장 전체 분위기를 볼 때 씁니다.",
        "note": "장기투자는 VOO, IVV, SPLG 같은 저비용 대안도 비교할 수 있습니다.",
    },
    {
        "ticker": "QQQ",
        "role": "성장주 코어 신호",
        "tracks": "Nasdaq-100",
        "what": "나스닥에 상장된 대형 비금융 성장주 100개를 추종합니다.",
        "use": "기술주와 성장주가 강한 장세인지 판단할 때 씁니다.",
        "note": "가격이 부담되면 장기 매수용으로 QQQM을 같이 검토합니다.",
    },
    {
        "ticker": "TLT",
        "role": "장기채 방어/금리 민감 자산",
        "tracks": "미국 20년 이상 장기 국채",
        "what": "미국 장기 국채에 투자합니다. 금리 변화에 민감합니다.",
        "use": "주식이 약하고 장기채 모멘텀이 강할 때 후보가 됩니다.",
        "note": "금리가 오르면 가격이 크게 흔들릴 수 있습니다.",
    },
    {
        "ticker": "GLD",
        "role": "금/대체자산",
        "tracks": "금 현물 가격",
        "what": "금 가격 움직임에 노출되는 대표 ETF입니다.",
        "use": "주식과 채권이 모두 불안할 때 대체자산 후보로 봅니다.",
        "note": "장기 비용을 중시하면 GLDM 같은 저비용 금 ETF도 비교할 수 있습니다.",
    },
    {
        "ticker": "SHY",
        "role": "안전자산 대기처",
        "tracks": "미국 1-3년 단기 국채",
        "what": "짧은 만기의 미국 국채에 투자합니다.",
        "use": "위험자산 모멘텀이 모두 약할 때 피난처로 씁니다.",
        "note": "수익을 크게 노리는 자산이 아니라 변동성을 낮추는 역할입니다.",
    },
    {
        "ticker": "SGOV",
        "role": "초단기 현금 대기 대안",
        "tracks": "미국 0-3개월 초단기 국채",
        "what": "만기가 매우 짧은 미국 T-Bill에 투자하는 현금성 ETF입니다.",
        "use": "매수 대기 현금이나 리스크를 낮춘 대기처를 찾을 때 봅니다.",
        "note": "SHY보다 금리 변화에 덜 흔들립니다. 백테스트 신호는 긴 히스토리의 SHY를 유지하고, 실제 대기자금 후보로 SGOV를 같이 봅니다.",
    },
    {
        "ticker": "QQQM",
        "role": "QQQ 장기매수 대안",
        "tracks": "Nasdaq-100",
        "what": "QQQ와 같은 Nasdaq-100 지수를 추종하는 Invesco ETF입니다.",
        "use": "QQQ 신호가 나왔을 때 실제 장기 매수 대안으로 검토할 수 있습니다.",
        "note": "QQQ보다 역사가 짧아 백테스트 신호는 QQQ를 쓰고, 실행 후보로 QQQM을 봅니다.",
    },
    {
        "ticker": "SPYG",
        "role": "성장주 ETF 참고",
        "tracks": "S&P 500 Growth Index",
        "what": "S&P 500 안의 성장주 성향 종목을 담는 ETF입니다.",
        "use": "미국 성장주 노출을 원할 때 참고할 수 있습니다.",
        "note": "QQQ와 동일한 ETF는 아닙니다. Nasdaq-100과 구성 방식이 다릅니다.",
    },
]


def clean_value(value):
    if pd.isna(value):
        return None
    if hasattr(value, "item"):
        return value.item()
    return value


def records(df: pd.DataFrame, limit: int | None = None) -> list[dict]:
    if limit:
        df = df.head(limit)
    return [{k: clean_value(v) for k, v in row.items()} for row in df.to_dict(orient="records")]


def curve_records(series: pd.Series, limit: int = 180) -> list[dict]:
    series = series.dropna()
    if series.empty:
        return []
    if len(series) > limit:
        step = max(1, len(series) // limit)
        series = series.iloc[::step]
    return [
        {"date": idx.date().isoformat(), "value": round(float(value), 4)}
        for idx, value in series.items()
    ]


def pct(value):
    if value is None or pd.isna(value):
        return None
    return round(float(value) * 100, 2)


def num(value):
    if value is None or pd.isna(value):
        return None
    return round(float(value), 4)


def build_etf_guide(cfg: dict, paths, refresh: bool) -> list[dict]:
    rows = []
    source = cfg["settings"].get("data_source", "yahoo")
    for item in ETF_GUIDE:
        row = dict(item)
        try:
            price = read_price(item["ticker"], paths, refresh=refresh, source=source)
            row["last"] = round(float(price["Close"].dropna().iloc[-1]), 2)
            if "Volume" in price:
                row["avg_volume_60d"] = int(price["Volume"].dropna().tail(60).mean())
            else:
                row["avg_volume_60d"] = None
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


def build_daily_advice(regime: dict, signal: dict, scores: pd.DataFrame, plan: pd.DataFrame, etf: dict) -> dict:
    signals = etf["signals"].dropna(subset=["signal"])
    current_signal = signal["current_signal"]
    previous_signal = None
    if len(signals) >= 2:
        previous_signal = str(signals["signal"].iloc[-2])

    candidates = scores[scores["status"] == "매수후보"].head(5) if not scores.empty else pd.DataFrame()
    top_candidates = [
        {
            "ticker": row["ticker"],
            "sector": row.get("sector", "기타"),
            "score": round(float(row["quant_score"]), 1),
        }
        for _, row in candidates.iterrows()
    ]
    cash_rows = plan[plan["type"] == "현금/대기"] if not plan.empty else pd.DataFrame()
    cash_weight = float(cash_rows["weight"].sum()) if not cash_rows.empty else 0.0

    if regime["regime"] == "방어":
        action = "위험 축소"
        tone = "bad"
        summary = "방어 모드입니다. 신규 개별주 편입보다 현금성 대기와 손실 제한을 먼저 봅니다."
    elif previous_signal and current_signal != previous_signal:
        action = "ETF 리밸런싱 검토"
        tone = "warn"
        summary = f"ETF 코어 신호가 {previous_signal}에서 {current_signal}로 바뀌었습니다. 월간 리밸런싱 대상인지 확인합니다."
    elif len(top_candidates) >= 3 and regime["regime"] == "공격":
        action = "분할 편입 검토"
        tone = "good"
        summary = "공격 모드이고 매수후보가 충분합니다. 한 번에 사기보다 후보를 나눠 검토합니다."
    elif len(top_candidates) > 0:
        action = "관찰 후 소액 검토"
        tone = "warn"
        summary = "후보는 있지만 시장 모드가 강하지 않습니다. 기존 보유를 우선 확인하고 소액만 검토합니다."
    else:
        action = "유지/관찰"
        tone = ""
        summary = "강한 신규 후보가 부족합니다. 기존 포트폴리오를 유지하고 다음 갱신을 기다립니다."

    steps = [
        f"데이터 기준일 {signal['as_of']}의 미국장 마감 종가로 계산했습니다.",
        "장중 실시간 가격은 반영하지 않고 다음 자동 갱신 때 반영합니다.",
        "실제 주문 전에는 보유 비중, 환율, 수수료, 실적 발표 일정을 별도로 확인합니다.",
    ]
    if cash_weight >= 0.20:
        steps.append(f"현재 제안에는 현금/SGOV 대기 비중이 {cash_weight * 100:.0f}% 있습니다.")
    if top_candidates:
        steps.append("개별주는 상위 후보를 바로 매수하는 뜻이 아니라 검토 목록으로 봅니다.")

    return {
        "action": action,
        "tone": tone,
        "summary": summary,
        "data_mode": "장 마감 종가 기준",
        "refresh_rule": "미국장 거래일 다음날 07:30 KST 자동 갱신",
        "previous_signal": previous_signal,
        "current_signal": current_signal,
        "top_candidates": top_candidates,
        "steps": steps,
    }


def write_static_assets(paths) -> None:
    for asset in STATIC_ASSETS:
        source = ROOT / asset
        if source.exists():
            shutil.copyfile(source, paths.output / asset)


def build_payload(refresh: bool = False) -> dict:
    cfg = load_config(DEFAULT_CONFIG)
    paths = resolve_paths(cfg)
    full_report(cfg, paths, refresh=refresh)
    regime = market_regime(cfg, paths, refresh=False)
    signal = latest_etf_signal(cfg, paths, refresh=False)
    scores = stock_scores(cfg, paths, refresh=False)
    plan = portfolio_plan(cfg, paths, refresh=False)
    etf = etf_backtest(cfg, paths, refresh=False)
    rotation = stock_rotation_backtest(cfg, paths, refresh=False)
    etf_metrics = etf["metrics"]["STRATEGY"]
    rotation_metrics = rotation["metrics"].get("STOCK_ROTATION", {})
    rotation_returns = rotation["returns"]

    return {
        "generated_at": datetime.now(ZoneInfo("Asia/Seoul")).strftime("%Y-%m-%d %H:%M:%S KST"),
        "regime": regime,
        "signal": {
            "as_of": signal["as_of"],
            "current_signal": signal["current_signal"],
            "latest_momentum_pct": pct(signal["latest_momentum"]),
        },
        "etf_metrics": {
            "cagr_pct": pct(etf_metrics["cagr"]),
            "mdd_pct": pct(etf_metrics["mdd"]),
            "sharpe": num(etf_metrics["sharpe"]),
            "sortino": num(etf_metrics["sortino"]),
            "calmar": num(etf_metrics["calmar"]),
            "win_rate_pct": pct(etf_metrics["win_rate"]),
        },
        "rotation_metrics": {
            "cagr_pct": pct(rotation_metrics.get("cagr")),
            "mdd_pct": pct(rotation_metrics.get("mdd")),
            "sharpe": num(rotation_metrics.get("sharpe")),
            "sortino": num(rotation_metrics.get("sortino")),
            "calmar": num(rotation_metrics.get("calmar")),
            "win_rate_pct": pct(rotation_metrics.get("win_rate")),
        },
        "scores": records(scores, limit=50),
        "plan": records(plan),
        "daily_advice": build_daily_advice(regime, signal, scores, plan, etf),
        "etf_guide": build_etf_guide(cfg, paths, refresh=refresh),
        "stock_charts": stock_chart_payload(scores, cfg, paths),
        "charts": {
            "etf_equity": curve_records(etf["signals"]["equity"]),
            "stock_rotation": curve_records((1 + rotation_returns["STOCK_ROTATION"].fillna(0)).cumprod()),
            "spy": curve_records((1 + rotation_returns["SPY"].fillna(0)).cumprod()) if "SPY" in rotation_returns else [],
            "qqq": curve_records((1 + rotation_returns["QQQ"].fillna(0)).cumprod()) if "QQQ" in rotation_returns else [],
        },
    }


HTML_TEMPLATE = r"""<!doctype html>
<html lang="ko">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta name="theme-color" content="#2563eb">
  <link rel="manifest" href="manifest.webmanifest">
  <link rel="icon" href="icon.svg" type="image/svg+xml">
  <title>퀀트 가디언 v2</title>
  <style>
    :root {
      --bg:#f4f6f9; --panel:#fff; --ink:#172033; --muted:#687589; --line:#dfe5ef;
      --brand:#2563eb; --brand-soft:#e8f1ff; --green:#059669; --red:#dc2626;
      --amber:#b45309; --shadow:0 6px 20px rgba(23,32,51,.07);
    }
    * { box-sizing:border-box; }
    body { margin:0; background:var(--bg); color:var(--ink); font-family:Arial, "Malgun Gothic", sans-serif; letter-spacing:0; }
    header { background:#fff; border-bottom:1px solid var(--line); position:sticky; top:0; z-index:5; }
    .topbar { max-width:1180px; margin:0 auto; padding:14px 20px; display:flex; gap:14px; align-items:center; justify-content:space-between; }
    h1 { margin:0; font-size:22px; }
    .sub { color:var(--muted); font-size:13px; margin-top:4px; }
    .actions { display:flex; gap:8px; align-items:center; flex-wrap:wrap; }
    .button { border:1px solid var(--line); background:#fff; color:var(--ink); border-radius:8px; padding:9px 12px; font-size:13px; font-weight:800; text-decoration:none; }
    .button[hidden] { display:none; }
    main { max-width:1180px; margin:0 auto; padding:22px 20px 48px; }
    .notice { background:#fff7ed; border:1px solid #fed7aa; color:#7c2d12; border-radius:8px; padding:12px 14px; font-size:13px; line-height:1.55; margin-bottom:18px; }
    .grid { display:grid; gap:14px; }
    .summary { grid-template-columns:repeat(4,minmax(0,1fr)); }
    .card { background:var(--panel); border:1px solid var(--line); border-radius:8px; box-shadow:var(--shadow); padding:16px; }
    .daily { display:grid; grid-template-columns:minmax(0,1.2fr) minmax(260px,.8fr); gap:18px; margin-bottom:16px; }
    .daily h2 { margin:0; font-size:18px; }
    .daily-title { display:flex; align-items:center; gap:10px; flex-wrap:wrap; margin-bottom:8px; }
    .mode-badge { display:inline-flex; align-items:center; min-height:26px; padding:4px 9px; border-radius:999px; background:#eef2ff; color:var(--brand); font-size:12px; font-weight:900; }
    .mode-badge.good { background:#dcfce7; color:#166534; }
    .mode-badge.warn { background:#fef3c7; color:#92400e; }
    .mode-badge.bad { background:#fee2e2; color:#991b1b; }
    .daily-copy { margin:0; color:var(--muted); line-height:1.6; font-size:13px; }
    .steps { margin:10px 0 0; padding-left:18px; color:var(--muted); font-size:13px; line-height:1.55; }
    .candidate-list { display:flex; flex-wrap:wrap; gap:8px; margin-top:10px; }
    .candidate-chip { display:inline-flex; gap:6px; align-items:center; border:1px solid var(--line); border-radius:999px; padding:6px 9px; background:#f8fafc; font-size:12px; font-weight:900; }
    .candidate-chip span { color:var(--muted); font-weight:700; }
    .label { color:var(--muted); font-size:12px; font-weight:800; }
    .value { margin-top:8px; font-size:26px; font-weight:900; }
    .hint { margin-top:6px; color:var(--muted); font-size:12px; line-height:1.45; }
    .positive { color:var(--green); }
    .tabs { display:flex; gap:6px; margin:20px 0 12px; flex-wrap:wrap; }
    .tab { border:1px solid var(--line); background:#fff; border-radius:8px; padding:9px 12px; font-weight:800; cursor:pointer; }
    .tab.active { background:var(--brand-soft); color:var(--brand); border-color:#bfdbfe; }
    .view { display:none; }
    .view.active { display:block; }
    .head { margin:14px 0 10px; display:flex; align-items:end; justify-content:space-between; gap:12px; }
    .head h2 { margin:0; font-size:18px; }
    .head p { margin:4px 0 0; color:var(--muted); font-size:13px; line-height:1.45; }
    .split { display:grid; grid-template-columns:minmax(0,1fr) 360px; gap:14px; }
    .table-wrap { overflow:auto; background:#fff; border:1px solid var(--line); border-radius:8px; box-shadow:var(--shadow); }
    table { width:100%; min-width:1060px; border-collapse:collapse; }
    th,td { padding:11px 12px; border-bottom:1px solid var(--line); text-align:left; font-size:13px; vertical-align:top; word-break:keep-all; }
    th { background:#f8fafc; color:var(--muted); font-size:12px; }
    tr:last-child td { border-bottom:0; }
    .pill { display:inline-flex; min-height:24px; align-items:center; padding:3px 8px; border-radius:999px; background:#f1f5f9; color:#334155; font-weight:900; font-size:12px; white-space:nowrap; }
    .good { background:#dcfce7; color:#166534; }
    .warn { background:#fef3c7; color:#92400e; }
    .bad { background:#fee2e2; color:#991b1b; }
    .kv { display:grid; grid-template-columns:1fr auto; gap:10px; padding:9px 0; border-bottom:1px solid var(--line); }
    .kv:last-child { border-bottom:0; }
    .kv span:first-child { color:var(--muted); }
    .kv span:last-child { font-weight:900; }
    .explain { margin-top:10px; color:var(--muted); font-size:13px; line-height:1.55; }
    .chart-grid { grid-template-columns:repeat(2,minmax(0,1fr)); margin:14px 0; }
    .chart-card h3 { margin:0 0 4px; font-size:15px; }
    .chart-card p { margin:0 0 12px; color:var(--muted); font-size:13px; line-height:1.45; }
    .chart { width:100%; height:230px; display:block; overflow:visible; }
    .chart-legend { display:flex; flex-wrap:wrap; gap:10px; margin-top:8px; color:var(--muted); font-size:12px; }
    .legend-dot { display:inline-block; width:10px; height:10px; border-radius:50%; margin-right:5px; }
    .stock-controls { display:flex; flex-wrap:wrap; gap:8px; align-items:center; margin-bottom:10px; }
    .stock-controls select { min-width:140px; border:1px solid var(--line); border-radius:8px; padding:9px 10px; background:#fff; font-weight:800; }
    .period { border:1px solid var(--line); background:#fff; border-radius:8px; padding:8px 10px; font-weight:800; cursor:pointer; }
    .period.active { background:var(--brand-soft); color:var(--brand); border-color:#bfdbfe; }
    .bar-list { display:grid; gap:9px; }
    .bar-row { display:grid; grid-template-columns:74px minmax(0,1fr) 54px; align-items:center; gap:10px; font-size:13px; }
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
    .global-tooltip { display:none; position:fixed; z-index:1000; max-width:min(560px, calc(100vw - 32px)); width:max-content; background:#172033; color:#fff; padding:10px 12px; border-radius:8px; box-shadow:var(--shadow); font-size:12px; line-height:1.5; font-weight:500; white-space:normal; overflow-wrap:anywhere; pointer-events:none; }
    .global-tooltip.active { display:block; }
    .term-grid { grid-template-columns:repeat(3,minmax(0,1fr)); }
    .term { min-height:120px; }
    .term strong { display:block; margin-bottom:8px; }
    .term p { margin:0; color:var(--muted); font-size:13px; line-height:1.55; }
    @media (max-width:900px) {
      .summary,.daily,.split,.chart-grid,.etf-cards,.term-grid { grid-template-columns:1fr; }
      .topbar { flex-direction:column; align-items:flex-start; }
    }
  </style>
</head>
<body>
  <header>
    <div class="topbar">
      <div>
        <h1>퀀트 가디언 v2</h1>
        <div class="sub" id="generatedAt"></div>
      </div>
      <div class="actions">
        <span class="tip" tabindex="0" data-tip="화면에 보이는 내용을 텍스트로 저장한 백업본입니다. CSV/HTML이 불편할 때 기록, 복사, 공유, 검산 용도로 씁니다. 평소에는 대시보드만 봐도 됩니다.">리포트 원문</span>
        <a class="button" href="report.md">열기</a>
        <button class="button" id="installApp" hidden>앱 설치</button>
      </div>
    </div>
  </header>
  <main>
    <div class="notice">투자 추천이 아니라 후보 발굴과 리스크 점검용 화면입니다. 자동 주문은 없고, 실제 매매 전에는 반드시 본인이 검토해야 합니다.</div>
    <section class="card daily" id="dailyAdvice"></section>
    <div class="grid summary">
      <div class="card"><div class="label" id="labelRegime"></div><div class="value" id="regime"></div><div class="hint" id="regimeHint"></div></div>
      <div class="card"><div class="label" id="labelSignal"></div><div class="value" id="signal"></div><div class="hint" id="signalHint"></div></div>
      <div class="card"><div class="label" id="labelEtfCagr"></div><div class="value positive" id="etfCagr"></div><div class="hint">월간 ETF 모멘텀 백테스트</div></div>
      <div class="card"><div class="label" id="labelRotSharpe"></div><div class="value" id="rotSharpe"></div><div class="hint">상위 모멘텀 종목 월간 교체</div></div>
    </div>
    <div class="card explain" id="currentRead"></div>
    <div class="grid chart-grid">
      <div class="card chart-card">
        <h3>상위 후보 점수</h3>
        <p>현재 스캐너가 가장 좋게 보는 종목을 막대로 비교합니다.</p>
        <div id="scoreBars" class="bar-list"></div>
      </div>
      <div class="card chart-card">
        <h3>제안 비중</h3>
        <p>ETF 코어와 개별주 위성 비중을 한눈에 봅니다.</p>
        <div id="weightBars" class="bar-list"></div>
      </div>
    </div>
    <div class="tabs">
      <button class="tab active" data-view="overview">요약</button>
      <button class="tab" data-view="scanner">종목 스캐너</button>
      <button class="tab" data-view="portfolio">포트폴리오 제안</button>
      <button class="tab" data-view="etfs">ETF 설명</button>
      <button class="tab" data-view="backtest">백테스트</button>
      <button class="tab" data-view="terms">용어 설명</button>
    </div>
    <section id="overview" class="view active">
      <div class="split">
        <div>
          <div class="head"><div><h2>상위 퀀트 후보</h2><p>모멘텀, 추세, 리스크, 타이밍을 합산한 종목 후보입니다.</p></div></div>
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
      <div class="head"><div><h2>종목 스캐너</h2><p>점수는 후보를 줄이는 필터입니다. 점수가 높아도 뉴스, 실적, 밸류에이션 확인은 별도입니다.</p></div></div>
      <div class="card chart-card">
        <h3>종목 최근 차트</h3>
        <p>상위 후보의 최근 가격 흐름을 1개월, 3개월, 6개월, 1년으로 나눠 봅니다.</p>
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
      <div class="head"><div><h2>포트폴리오 제안</h2><p>현재 시장 모드에 맞춘 ETF 코어와 개별주 위성 비중입니다.</p></div></div>
      <div class="table-wrap"><table id="planTable"></table></div>
    </section>
    <section id="etfs" class="view">
      <div class="head"><div><h2>ETF 코어 신호</h2><p>신호 계산에는 SPY, QQQ, TLT, GLD를 비교하고, 모두 약하면 SHY로 대기합니다. 실제 현금성 대기 후보로는 SGOV도 함께 봅니다.</p></div></div>
      <div class="card explain">QQQ 신호는 “나스닥 100 성장주 묶음이 가장 강하다”는 뜻입니다. 실제 장기 매수는 QQQ 그대로 할 수도 있고, 단가와 비용을 낮추고 싶다면 QQQM을 대안으로 검토할 수 있습니다. SPYG는 QQQ와 동일 상품이 아니라 S&P 500 성장주 ETF라 별도 후보로 봐야 합니다. SHY는 1-3년 단기국채라 가격 변동이 조금 있고, SGOV는 0-3개월 초단기 국채라 현금 대기에 더 가깝습니다.</div>
      <div id="etfCards" class="etf-cards"></div>
    </section>
    <section id="backtest" class="view">
      <div class="head"><div><h2>백테스트</h2><p>과거에 이 규칙을 적용했을 때의 결과입니다. 미래 수익 보장이 아닙니다.</p></div></div>
      <div class="card chart-card">
        <h3>누적 수익 곡선</h3>
        <p>1에서 시작해 시간이 지나며 자산이 얼마나 커졌는지 보는 그래프입니다. 곡선이 출렁이는 구간이 실제로 버텨야 하는 위험입니다.</p>
        <svg id="equityChart" class="chart" role="img" aria-label="백테스트 누적 수익 곡선"></svg>
        <div class="chart-legend">
          <span><i class="legend-dot" style="background:#2563eb"></i>개별주 로테이션</span>
          <span><i class="legend-dot" style="background:#059669"></i>SPY</span>
          <span><i class="legend-dot" style="background:#b45309"></i>QQQ</span>
        </div>
      </div>
      <div class="grid summary" id="backtestCards"></div>
    </section>
    <section id="terms" class="view">
      <div class="head"><div><h2>용어 설명</h2><p>마우스를 올리지 않아도 주요 항목을 한 번에 볼 수 있습니다.</p></div></div>
      <div class="grid term-grid" id="termGrid"></div>
    </section>
  </main>
  <div id="globalTooltip" class="global-tooltip"></div>
  <script>
    const DATA = __DATA__;
    const $ = id => document.getElementById(id);
    const HELP = {
      regime:"SPY/QQQ 장기추세, SPY 6개월 수익률, VIX를 합산해 공격/중립/방어를 정합니다.",
      signal:"SPY, QQQ, TLT, GLD의 12개월 모멘텀을 비교해 가장 강한 ETF를 고릅니다. 모두 약하면 SHY를 선택합니다.",
      etfCagr:"ETF 코어 전략을 과거에 적용했을 때의 연평균 복리수익률입니다.",
      rotSharpe:"개별주 로테이션 전략의 위험 대비 성과입니다. 1보다 높으면 변동성 대비 성과가 양호한 편으로 봅니다.",
      score:"모멘텀 35%, 추세 25%, 리스크 20%, 타이밍 10%, 시장모드 10%를 합친 점수입니다.",
      status:"매수후보, 관찰, 과열주의, 제외 중 하나입니다. 매수후보도 즉시 매수 뜻은 아닙니다.",
      mom:"최근 1개월을 제외한 12개월 수익률입니다. 단기 급등락 노이즈를 줄이기 위한 모멘텀 지표입니다.",
      ret6:"최근 6개월 수익률입니다. 중기 추세가 살아 있는지 봅니다.",
      rsi:"최근 상승/하락 속도를 보는 지표입니다. 45-65는 무난, 72 이상은 과열로 봅니다.",
      drawdown:"최근 1년 고점 대비 얼마나 내려와 있는지입니다. -20%면 고점보다 20% 낮다는 뜻입니다.",
      spy200:"SPY가 200일 평균가격보다 위면 미국 주식시장의 장기 추세가 살아 있다고 봅니다.",
      qqq200:"QQQ가 200일 평균가격보다 위면 성장주/기술주 장기 추세가 살아 있다고 봅니다.",
      spy6:"SPY의 최근 6개월 수익률입니다. 플러스면 시장 흐름이 우호적입니다.",
      vix:"시장 공포지수입니다. 보통 20 아래면 불안이 낮은 편, 20 위면 경계가 필요합니다.",
      mdd:"Max Drawdown. 과거 최고점 대비 가장 크게 빠진 낙폭입니다.",
      sortino:"하락 변동성만 위험으로 보고 계산한 위험 대비 성과입니다.",
      calmar:"CAGR을 MDD로 나눈 값입니다. 수익률 대비 낙폭이 작은지 봅니다.",
      win:"월별 수익률이 플러스였던 비율입니다."
    };
    const TERMS = [
      ["시장 모드", HELP.regime],
      ["ETF 코어 신호", HELP.signal],
      ["총점", HELP.score],
      ["상태", HELP.status],
      ["12-1 모멘텀", HELP.mom],
      ["RSI", HELP.rsi],
      ["1년 낙폭", HELP.drawdown],
      ["CAGR", HELP.etfCagr],
      ["MDD", HELP.mdd],
      ["Sharpe", HELP.rotSharpe],
      ["Sortino", HELP.sortino],
      ["Calmar", HELP.calmar],
      ["SHY와 SGOV", "SHY는 미국 1-3년 단기국채 ETF라 금리 변화에 어느 정도 흔들립니다. SGOV는 미국 0-3개월 초단기 국채 ETF라 현금 대기 성격이 더 강합니다. 그래서 백테스트 신호는 히스토리가 긴 SHY를 쓰고, 실제 대기자금 후보는 SGOV도 같이 봅니다."]
    ];
    const pct = v => v === null || v === undefined || Number.isNaN(Number(v)) ? "-" : `${Number(v).toFixed(2)}%`;
    const num = (v,d=2) => v === null || v === undefined || Number.isNaN(Number(v)) ? "-" : Number(v).toFixed(d);
    const volume = v => v === null || v === undefined || Number.isNaN(Number(v)) ? "-" : Number(v).toLocaleString("ko-KR");
    const escapeAttr = v => String(v ?? "").replace(/[&<>"']/g, ch => ({
      "&":"&amp;", "<":"&lt;", ">":"&gt;", "\"":"&quot;", "'":"&#39;"
    }[ch]));
    const tip = (label, text) => `<span class="tip" tabindex="0" data-tip="${escapeAttr(text)}">${label}</span>`;
    const pill = v => {
      const cls = v === "매수후보" ? "good" : v === "과열주의" || v === "관찰" ? "warn" : v === "방어" ? "bad" : "";
      return `<span class="pill ${cls}">${v || "-"}</span>`;
    };
    function table(el, cols, rows) {
      el.innerHTML = `<thead><tr>${cols.map(c=>`<th>${c.help ? tip(c.label, c.help) : c.label}</th>`).join("")}</tr></thead><tbody>${rows.map(r=>`<tr>${cols.map(c=>`<td>${c.render ? c.render(r[c.key], r) : (r[c.key] ?? "-")}</td>`).join("")}</tr>`).join("")}</tbody>`;
    }
    function barList(el, rows, valueKey, maxValue, labelKey, formatter) {
      el.innerHTML = rows.map(row => {
        const value = Number(row[valueKey] ?? 0);
        const width = Math.max(2, Math.min(100, maxValue ? value / maxValue * 100 : 0));
        return `<div class="bar-row"><strong>${row[labelKey]}</strong><div class="bar-track"><div class="bar-fill" style="width:${width}%"></div></div><span>${formatter(value)}</span></div>`;
      }).join("");
    }
    function lineChart(el, seriesList, labelFormatter = v => `${v.toFixed(1)}x`) {
      const width = 720, height = 230, pad = 28;
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
      const lines = seriesList.map(s => `<path d="${path(s.data)}" fill="none" stroke="${s.color}" stroke-width="2.6" stroke-linecap="round" stroke-linejoin="round"></path>`).join("");
      el.setAttribute("viewBox", `0 0 ${width} ${height}`);
      el.innerHTML = `
        <line x1="${pad}" y1="${height-pad}" x2="${width-pad}" y2="${height-pad}" stroke="#dfe5ef"></line>
        <line x1="${pad}" y1="${pad}" x2="${pad}" y2="${height-pad}" stroke="#dfe5ef"></line>
        <text x="${pad}" y="18" fill="#687589" font-size="12">${labelFormatter(maxY)}</text>
        <text x="${pad}" y="${height-6}" fill="#687589" font-size="12">${labelFormatter(minY)}</text>
        ${lines}`;
    }
    function renderEtfCards(el, rows) {
      el.innerHTML = rows.map(row => `
        <div class="card etf-card">
          <div class="etf-title"><h3>${row.ticker}</h3><span class="etf-role">${row.role}</span></div>
          <div class="etf-meta">
            <div class="mini"><span>추종/노출</span><strong>${row.tracks}</strong></div>
            <div class="mini"><span>최근 종가</span><strong>${row.last === null ? "-" : "$" + num(row.last, 2)}</strong></div>
            <div class="mini"><span>60일 평균 거래량</span><strong>${volume(row.avg_volume_60d)}</strong></div>
          </div>
          <p><strong>무엇인가:</strong> ${row.what}</p>
          <p><strong>언제 보는가:</strong> ${row.use}</p>
          <p><strong>메모:</strong> ${row.note}</p>
        </div>`).join("");
    }
    function renderDailyAdvice() {
      const advice = DATA.daily_advice || {};
      const candidates = advice.top_candidates || [];
      $("dailyAdvice").innerHTML = `
        <div>
          <div class="daily-title">
            <h2>오늘의 행동: ${advice.action || "유지/관찰"}</h2>
            <span class="mode-badge ${advice.tone || ""}">${advice.data_mode || "장 마감 종가 기준"}</span>
          </div>
          <p class="daily-copy">${advice.summary || "오늘은 기존 포트폴리오를 유지하고 다음 갱신을 기다립니다."}</p>
          <div class="candidate-list">
            ${candidates.length ? candidates.map(item => `<span class="candidate-chip">${item.ticker}<span>${item.sector} · ${num(item.score, 1)}점</span></span>`).join("") : `<span class="candidate-chip">신규 강한 후보 없음</span>`}
          </div>
        </div>
        <div>
          <div class="label">갱신 방식</div>
          <div class="value" style="font-size:18px">${advice.refresh_rule || "미국장 거래일 다음날 07:30 KST 자동 갱신"}</div>
          <ol class="steps">${(advice.steps || []).map(step => `<li>${step}</li>`).join("")}</ol>
        </div>`;
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
    function hideTooltip() {
      const tooltip = $("globalTooltip");
      if (tooltip) tooltip.classList.remove("active");
    }
    document.addEventListener("mouseover", event => {
      const target = event.target.closest(".tip");
      if (target) showTooltip(target);
    });
    document.addEventListener("focusin", event => {
      const target = event.target.closest(".tip");
      if (target) showTooltip(target);
    });
    document.addEventListener("mouseout", event => {
      if (event.target.closest(".tip")) hideTooltip();
    });
    document.addEventListener("focusout", event => {
      if (event.target.closest(".tip")) hideTooltip();
    });
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
      select.innerHTML = tickers.map(ticker => `<option value="${ticker}">${ticker}</option>`).join("");
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
      if ("serviceWorker" in navigator) {
        navigator.serviceWorker.register("service-worker.js").catch(() => {});
      }
    }
    renderDailyAdvice();
    setupInstallPrompt();
    $("labelRegime").innerHTML = tip("시장 모드", HELP.regime);
    $("labelSignal").innerHTML = tip("ETF 코어 신호", HELP.signal);
    $("labelEtfCagr").innerHTML = tip("ETF CAGR", HELP.etfCagr);
    $("labelRotSharpe").innerHTML = tip("주식 로테이션 Sharpe", HELP.rotSharpe);
    $("generatedAt").textContent = `마지막 계산 ${DATA.generated_at}`;
    $("regime").textContent = DATA.regime.regime;
    $("regimeHint").textContent = `${DATA.regime.as_of} 기준, ${DATA.regime.score}점`;
    $("signal").textContent = DATA.signal.current_signal;
    $("signalHint").textContent = `${DATA.signal.as_of} 기준, 12개월 모멘텀 ${pct(DATA.signal.latest_momentum_pct)}`;
    $("etfCagr").textContent = pct(DATA.etf_metrics.cagr_pct);
    $("rotSharpe").textContent = num(DATA.rotation_metrics.sharpe, 3);
    const qqqAlt = DATA.signal.current_signal === "QQQ" ? " 실제 매수는 QQQ뿐 아니라 QQQM도 비교할 수 있습니다." : "";
    $("currentRead").textContent = `현재 해석: 시장 모드는 ${DATA.regime.regime}, ETF 코어 신호는 ${DATA.signal.current_signal}입니다. 이 화면은 실시간 호가가 아니라 ${DATA.signal.as_of} 장 마감 종가로 계산한 일일 판단입니다.${qqqAlt} 상위 종목은 후보일 뿐이며, 개별 뉴스와 실적 확인 없이 바로 매수한다는 뜻은 아닙니다.`;
    $("regimeFacts").innerHTML = [
      [tip("SPY 200일선 위", HELP.spy200), DATA.regime.market_above_200d === true ? "예" : DATA.regime.market_above_200d === false ? "아니오" : "없음"],
      [tip("QQQ 200일선 위", HELP.qqq200), DATA.regime.growth_above_200d === true ? "예" : DATA.regime.growth_above_200d === false ? "아니오" : "없음"],
      [tip("SPY 6개월", HELP.spy6), pct((DATA.regime.market_6m_return ?? 0) * 100)],
      [tip("VIX", HELP.vix), num(DATA.regime.vix, 2)]
    ].map(([k,v])=>`<div class="kv"><span>${k}</span><span>${v}</span></div>`).join("");
    $("regimeExplain").textContent = "4개 조건 중 많이 충족할수록 공격, 적게 충족할수록 방어입니다. 지금처럼 SPY/QQQ가 200일선 위이고 VIX가 낮으면 시장 위험을 비교적 감수할 수 있다고 봅니다.";
    const scoreCols = [
      {key:"ticker", label:"티커"},
      {key:"sector", label:"업종"},
      {key:"quant_score", label:"총점", help:HELP.score, render:v=>num(v,1)},
      {key:"status", label:"상태", help:HELP.status, render:v=>pill(v)},
      {key:"mom_12_1", label:"12-1 모멘텀", help:HELP.mom, render:v=>pct(Number(v)*100)},
      {key:"ret_6m", label:"6개월", help:HELP.ret6, render:v=>pct(Number(v)*100)},
      {key:"rsi14", label:"RSI", help:HELP.rsi, render:v=>num(v,1)},
      {key:"drawdown_252d", label:"1년 낙폭", help:HELP.drawdown, render:v=>pct(Number(v)*100)},
      {key:"reason", label:"이유"}
    ];
    table($("topScores"), scoreCols, DATA.scores.slice(0,8));
    table($("scoreTable"), scoreCols, DATA.scores);
    setupStockChart();
    barList($("scoreBars"), DATA.scores.slice(0, 6), "quant_score", 100, "ticker", v => num(v, 1));
    table($("planTable"), [
      {key:"asset", label:"자산"},
      {key:"type", label:"구분"},
      {key:"weight", label:"비중", render:v=>pct(Number(v)*100)},
      {key:"reason", label:"이유"}
    ], DATA.plan);
    barList($("weightBars"), DATA.plan, "weight", 1, "asset", v => pct(v * 100));
    renderEtfCards($("etfCards"), DATA.etf_guide);
    lineChart($("equityChart"), [
      {name:"개별주 로테이션", color:"#2563eb", data:DATA.charts.stock_rotation || []},
      {name:"SPY", color:"#059669", data:DATA.charts.spy || []},
      {name:"QQQ", color:"#b45309", data:DATA.charts.qqq || []}
    ]);
    $("backtestCards").innerHTML = [
      [tip("ETF CAGR", HELP.etfCagr), pct(DATA.etf_metrics.cagr_pct)],
      [tip("ETF MDD", HELP.mdd), pct(DATA.etf_metrics.mdd_pct)],
      [tip("ETF Sharpe", HELP.rotSharpe), num(DATA.etf_metrics.sharpe, 3)],
      ["주식 로테이션 CAGR", pct(DATA.rotation_metrics.cagr_pct)],
      ["주식 로테이션 MDD", pct(DATA.rotation_metrics.mdd_pct)],
      ["주식 로테이션 Sharpe", num(DATA.rotation_metrics.sharpe, 3)]
    ].map(([k,v])=>`<div class="card"><div class="label">${k}</div><div class="value">${v}</div></div>`).join("");
    $("termGrid").innerHTML = TERMS.map(([k,v])=>`<div class="card term"><strong>${k}</strong><p>${v}</p></div>`).join("");
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
    parser = argparse.ArgumentParser(description="퀀트 가디언 v2 HTML 대시보드 생성")
    parser.add_argument("--refresh", action="store_true", help="무료 가격 데이터를 새로 받은 뒤 생성")
    args = parser.parse_args()
    cfg = load_config(DEFAULT_CONFIG)
    paths = resolve_paths(cfg)
    payload = build_payload(refresh=args.refresh)
    html = HTML_TEMPLATE.replace("__DATA__", json.dumps(payload, ensure_ascii=False))
    out = paths.output / "dashboard.html"
    out.write_text(html, encoding="utf-8-sig")
    write_static_assets(paths)
    print(out.resolve())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
