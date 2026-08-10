from __future__ import annotations

import btc_v07_three_split_research as research
from btc_v07_split_engine import simulate_three_split


# Keep the report/data-loading code in the research module, but replace its local
# simulator with the independently tested execution engine.
research.simulate_three_split = simulate_three_split


if __name__ == "__main__":
    raise SystemExit(research.main())
