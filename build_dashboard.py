from __future__ import annotations

import argparse
import json
from datetime import datetime

import pandas as pd

from quant_guardian import (
    DEFAULT_CONFIG,
    etf_backtest,
    full_report,
    latest_etf_signal,
    load_config,
    market_regime,
    portfolio_plan,
    resolve_paths,
    stock_rotation_backtest,
    stock_scores,
)


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


def pct(value):
    if value is None or pd.isna(value):
        return None
    return round(float(value) * 100, 2)


def num(value):
    if value is None or pd.isna(value):
        return None
    return round(float(value), 4)


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

    return {
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
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
    }


HTML_TEMPLATE = r"""<!doctype html>
<html lang="ko">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
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
    .button { border:1px solid var(--line); background:#fff; color:var(--ink); border-radius:8px; padding:9px 12px; font-size:13px; font-weight:800; text-decoration:none; }
    main { max-width:1180px; margin:0 auto; padding:22px 20px 48px; }
    .notice { background:#fff7ed; border:1px solid #fed7aa; color:#7c2d12; border-radius:8px; padding:12px 14px; font-size:13px; line-height:1.55; margin-bottom:18px; }
    .grid { display:grid; gap:14px; }
    .summary { grid-template-columns:repeat(4,minmax(0,1fr)); }
    .card { background:var(--panel); border:1px solid var(--line); border-radius:8px; box-shadow:var(--shadow); padding:16px; }
    .label { color:var(--muted); font-size:12px; font-weight:800; }
    .value { margin-top:8px; font-size:26px; font-weight:900; }
    .hint { margin-top:6px; color:var(--muted); font-size:12px; }
    .negative { color:var(--red); }
    .positive { color:var(--green); }
    .tabs { display:flex; gap:6px; margin:20px 0 12px; flex-wrap:wrap; }
    .tab { border:1px solid var(--line); background:#fff; border-radius:8px; padding:9px 12px; font-weight:800; cursor:pointer; }
    .tab.active { background:var(--brand-soft); color:var(--brand); border-color:#bfdbfe; }
    .view { display:none; }
    .view.active { display:block; }
    .head { margin:14px 0 10px; display:flex; align-items:end; justify-content:space-between; gap:12px; }
    .head h2 { margin:0; font-size:18px; }
    .head p { margin:4px 0 0; color:var(--muted); font-size:13px; }
    .split { display:grid; grid-template-columns:minmax(0,1fr) 350px; gap:14px; }
    .table-wrap { overflow:auto; background:#fff; border:1px solid var(--line); border-radius:8px; box-shadow:var(--shadow); }
    table { width:100%; min-width:760px; border-collapse:collapse; }
    th,td { padding:11px 12px; border-bottom:1px solid var(--line); text-align:left; font-size:13px; vertical-align:middle; }
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
    @media (max-width:900px) {
      .summary,.split { grid-template-columns:1fr; }
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
      <a class="button" href="report.md">리포트 원문</a>
    </div>
  </header>
  <main>
    <div class="notice">투자 추천이 아니라 후보 발굴과 리스크 점검용 화면입니다. 자동 주문은 없고, 실제 매매 전에는 반드시 본인이 검토해야 합니다.</div>
    <div class="grid summary">
      <div class="card"><div class="label">시장 모드</div><div class="value" id="regime"></div><div class="hint" id="regimeHint"></div></div>
      <div class="card"><div class="label">ETF 코어 신호</div><div class="value" id="signal"></div><div class="hint" id="signalHint"></div></div>
      <div class="card"><div class="label">ETF CAGR</div><div class="value positive" id="etfCagr"></div><div class="hint">월간 모멘텀 백테스트</div></div>
      <div class="card"><div class="label">주식 로테이션 Sharpe</div><div class="value" id="rotSharpe"></div><div class="hint">상위 모멘텀 종목 월간 교체</div></div>
    </div>
    <div class="tabs">
      <button class="tab active" data-view="overview">요약</button>
      <button class="tab" data-view="scanner">종목 스캐너</button>
      <button class="tab" data-view="portfolio">포트폴리오 제안</button>
      <button class="tab" data-view="backtest">백테스트</button>
    </div>
    <section id="overview" class="view active">
      <div class="split">
        <div>
          <div class="head"><div><h2>상위 퀀트 후보</h2><p>모멘텀, 추세, 리스크, 타이밍을 합산합니다.</p></div></div>
          <div class="table-wrap"><table id="topScores"></table></div>
        </div>
        <div class="card">
          <div class="head"><div><h2>시장 체크</h2><p>공격/중립/방어 판단 근거입니다.</p></div></div>
          <div id="regimeFacts"></div>
        </div>
      </div>
    </section>
    <section id="scanner" class="view"><div class="head"><div><h2>종목 스캐너</h2><p>매수후보와 과열주의를 분리해서 봅니다.</p></div></div><div class="table-wrap"><table id="scoreTable"></table></div></section>
    <section id="portfolio" class="view"><div class="head"><div><h2>포트폴리오 제안</h2><p>ETF 코어와 개별주 위성 비중입니다.</p></div></div><div class="table-wrap"><table id="planTable"></table></div></section>
    <section id="backtest" class="view"><div class="head"><div><h2>백테스트</h2><p>ETF 코어와 개별주 로테이션 결과입니다.</p></div></div><div class="grid summary" id="backtestCards"></div></section>
  </main>
  <script>
    const DATA = __DATA__;
    const $ = id => document.getElementById(id);
    const pct = v => v === null || v === undefined || Number.isNaN(Number(v)) ? "-" : `${Number(v).toFixed(2)}%`;
    const num = (v,d=2) => v === null || v === undefined || Number.isNaN(Number(v)) ? "-" : Number(v).toFixed(d);
    const pill = v => {
      const cls = v === "매수후보" ? "good" : v === "과열주의" || v === "관찰" ? "warn" : v === "방어" ? "bad" : "";
      return `<span class="pill ${cls}">${v || "-"}</span>`;
    };
    function table(el, cols, rows) {
      el.innerHTML = `<thead><tr>${cols.map(c=>`<th>${c.label}</th>`).join("")}</tr></thead><tbody>${rows.map(r=>`<tr>${cols.map(c=>`<td>${c.render ? c.render(r[c.key], r) : (r[c.key] ?? "-")}</td>`).join("")}</tr>`).join("")}</tbody>`;
    }
    $("generatedAt").textContent = `마지막 계산 ${DATA.generated_at}`;
    $("regime").textContent = DATA.regime.regime;
    $("regimeHint").textContent = `${DATA.regime.as_of} 기준, ${DATA.regime.score}점`;
    $("signal").textContent = DATA.signal.current_signal;
    $("signalHint").textContent = `${DATA.signal.as_of} 기준, 12개월 모멘텀 ${pct(DATA.signal.latest_momentum_pct)}`;
    $("etfCagr").textContent = pct(DATA.etf_metrics.cagr_pct);
    $("rotSharpe").textContent = num(DATA.rotation_metrics.sharpe, 3);
    $("regimeFacts").innerHTML = [
      ["SPY 200일선 위", DATA.regime.market_above_200d === true ? "예" : DATA.regime.market_above_200d === false ? "아니오" : "없음"],
      ["QQQ 200일선 위", DATA.regime.growth_above_200d === true ? "예" : DATA.regime.growth_above_200d === false ? "아니오" : "없음"],
      ["SPY 6개월", pct((DATA.regime.market_6m_return ?? 0) * 100)],
      ["VIX", num(DATA.regime.vix, 2)]
    ].map(([k,v])=>`<div class="kv"><span>${k}</span><span>${v}</span></div>`).join("");
    const scoreCols = [
      {key:"ticker", label:"티커"},
      {key:"quant_score", label:"총점", render:v=>num(v,1)},
      {key:"status", label:"상태", render:v=>pill(v)},
      {key:"mom_12_1", label:"12-1 모멘텀", render:v=>pct(Number(v)*100)},
      {key:"ret_6m", label:"6개월", render:v=>pct(Number(v)*100)},
      {key:"rsi14", label:"RSI", render:v=>num(v,1)},
      {key:"drawdown_252d", label:"1년 낙폭", render:v=>pct(Number(v)*100)},
      {key:"reason", label:"이유"}
    ];
    table($("topScores"), scoreCols, DATA.scores.slice(0,8));
    table($("scoreTable"), scoreCols, DATA.scores);
    table($("planTable"), [
      {key:"asset", label:"자산"},
      {key:"type", label:"구분"},
      {key:"weight", label:"비중", render:v=>pct(Number(v)*100)},
      {key:"reason", label:"이유"}
    ], DATA.plan);
    $("backtestCards").innerHTML = [
      ["ETF CAGR", pct(DATA.etf_metrics.cagr_pct)],
      ["ETF MDD", pct(DATA.etf_metrics.mdd_pct)],
      ["ETF Sharpe", num(DATA.etf_metrics.sharpe, 3)],
      ["주식 로테이션 CAGR", pct(DATA.rotation_metrics.cagr_pct)],
      ["주식 로테이션 MDD", pct(DATA.rotation_metrics.mdd_pct)],
      ["주식 로테이션 Sharpe", num(DATA.rotation_metrics.sharpe, 3)]
    ].map(([k,v])=>`<div class="card"><div class="label">${k}</div><div class="value">${v}</div></div>`).join("");
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
    print(out.resolve())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
