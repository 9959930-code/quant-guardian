from __future__ import annotations

import btc_clock_hybrid_core as hybrid_core
import btc_clock_hybrid_telegram as hybrid_telegram

_INSTALLED = False


def install() -> None:
    global _INSTALLED
    if _INSTALLED:
        return
    hybrid_core.install()
    hybrid_telegram.install()
    _INSTALLED = True
