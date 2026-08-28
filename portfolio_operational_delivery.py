from __future__ import annotations

import os
from datetime import datetime
from typing import Any, Mapping

import portfolio_operational_alerts as operational


_INSTALLED = False
_ORIGINAL_DUE = operational._weekly_heartbeat_due


def deployment_aware_heartbeat_due(
    operations: Mapping[str, Any], now_kst: datetime
) -> bool:
    current_period = operational._week_key(now_kst)
    if operations.get("last_weekly_heartbeat_period") == current_period:
        return False
    event_name = os.getenv("GITHUB_EVENT_NAME", "")
    if event_name in {"push", "workflow_dispatch"}:
        return True
    return _ORIGINAL_DUE(operations, now_kst)


def install() -> None:
    global _INSTALLED
    if _INSTALLED:
        return
    operational._weekly_heartbeat_due = deployment_aware_heartbeat_due
    _INSTALLED = True
