from __future__ import annotations

import traceback

from equity_v2_engine import FeatureStore, close_panel, simulate_close_to_close
from equity_v2_modern_engine import build_candidate_grid, generate_targets
from equity_v2_modern_research import (
    MODERN_START,
    build_primary_frames,
    metric_columns,
    valid_start,
)
from quant_guardian import DEFAULT_CONFIG, load_config, resolve_paths


def main() -> int:
    cfg = load_config(DEFAULT_CONFIG)
    paths = resolve_paths(cfg)
    frames, _, _, _ = build_primary_frames(cfg=cfg, paths=paths, refresh=True)
    start = valid_start(frames, MODERN_START)
    latest = min(frame["Close"].dropna().index.max() for frame in frames.values())
    store = FeatureStore(close_panel(frames))
    candidates = build_candidate_grid()
    print(f"start={start} latest={latest} candidates={len(candidates)}")
    print({name: (frame.index.min(), frame.index.max(), int(frame["Close"].notna().sum())) for name, frame in frames.items()})
    failures = 0
    for candidate in candidates[:12]:
        print("CANDIDATE", candidate.candidate_id, candidate.family, candidate.params)
        try:
            targets = generate_targets(candidate, store=store, start=start, end=latest)
            print("TARGETS", targets.head().to_dict(), targets.tail().to_dict(), len(targets))
            simulation = simulate_close_to_close(
                targets=targets,
                frames=frames,
                start=start,
                end=latest,
                cost_bps=10.0,
            )
            print("METRICS", simulation.metrics)
            print("PERIODS", metric_columns(simulation, latest))
        except Exception:
            failures += 1
            traceback.print_exc()
    return 1 if failures == 12 else 0


if __name__ == "__main__":
    raise SystemExit(main())
