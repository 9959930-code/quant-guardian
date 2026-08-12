from __future__ import annotations

import argparse
import json
from pathlib import Path

import pandas as pd

from equity_v2_dca_engine import DcaSimulator, StrategySpec, prepare_market, synthetic_tqqq_from_qqq
from equity_v2_engine import load_market_data
from quant_guardian import DEFAULT_CONFIG, load_config, resolve_paths


SPECS = {
    "always_tqqq": StrategySpec("always_tqqq", "always_tqqq"),
    "always_qqq_contrib": StrategySpec("always_qqq_contrib", "always_qqq"),
    "split_50_50": StrategySpec("split_50_50", "split_50_50"),
    "optimized_conversion": StrategySpec(
        "convert_dd15_ma0_f100_s3_weekly_nm3",
        "qqq_convert",
        drawdown=-0.15,
        recovery_ma=None,
        conversion_fraction=1.0,
        conversion_stages=3,
        stage_frequency="weekly",
        switch_months=3,
    ),
}


def run(*, refresh: bool, config_path: Path) -> pd.DataFrame:
    cfg = load_config(config_path)
    paths = resolve_paths(cfg)
    frames, _ = load_market_data(cfg=cfg, paths=paths, refresh=refresh)
    synthetic = synthetic_tqqq_from_qqq(frames["QQQ"], annual_drag=0.025)
    constant_fx = frames["QQQ"][["Open", "Close"]].copy()
    constant_fx.loc[:, "Open"] = 1000.0
    constant_fx.loc[:, "Close"] = 1000.0
    stress_frames = dict(frames)
    stress_frames["KRW=X"] = constant_fx
    periods = {
        "dotcom_2000_2006": (pd.Timestamp("2000-01-03"), pd.Timestamp("2006-12-29")),
        "gfc_2006_2012": (pd.Timestamp("2006-07-03"), pd.Timestamp("2012-12-31")),
    }
    rows: list[dict] = []
    for period, (start, end) in periods.items():
        market = prepare_market(stress_frames, start, end, synthetic_tqqq=synthetic)
        for name, spec in SPECS.items():
            result = DcaSimulator(market, spec).run()
            rows.append(
                {
                    "period": period,
                    "strategy": name,
                    **result.metrics,
                }
            )
    frame = pd.DataFrame(rows)
    csv_path = paths.output / "equity_v2_dca_synthetic_constant_fx.csv"
    json_path = paths.output / "equity_v2_dca_synthetic_constant_fx.json"
    frame.to_csv(csv_path, index=False, encoding="utf-8-sig")
    json_path.write_text(
        json.dumps(frame.to_dict(orient="records"), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(frame[["period", "strategy", "after_tax_xirr", "mdd", "minimum_vs_contributions", "after_tax_liquidation_value_krw"]].to_string(index=False))
    return frame


def main() -> int:
    parser = argparse.ArgumentParser(description="Constant-FX synthetic DCA stress")
    parser.add_argument("--refresh", action="store_true")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    args = parser.parse_args()
    run(refresh=args.refresh, config_path=args.config)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
