from __future__ import annotations

import equity_v2_phase2_research as research


_original_buy_hold_comparison = research.buy_hold_comparison


def _buy_hold_with_multiple(*args, **kwargs):
    frame, simulations = _original_buy_hold_comparison(*args, **kwargs)
    if "capital_multiple" not in frame:
        frame["capital_multiple"] = 1.0 + frame["total_return"]
    return frame, simulations


research.buy_hold_comparison = _buy_hold_with_multiple


if __name__ == "__main__":
    raise SystemExit(research.main())
