"""Horizon table shared by every forecast tier.

Extracted from `future_mode_service` so tests and future refactors can
import the horizon spec without pulling in the entire forecast module.
"""
from __future__ import annotations

from typing import Tuple

# (key, units, is_intraday)
#   key:        block name in forward_metrics / forward_metrics_garch
#   units:      # of "bars" — 1h/5h for intraday, days for the daily set
#   is_intraday: True → the drift-scaling helper treats `units` as hours
Horizon = Tuple[str, int, bool]

ALL_HORIZONS: tuple[Horizon, ...] = (
    ('forward_1h',  1,  True),
    ('forward_5h',  5,  True),
    ('forward_1d',  1,  False),
    ('forward_5d',  5,  False),
    ('forward_20d', 20, False),
)


def horizon_labels() -> list[str]:
    return [h[0] for h in ALL_HORIZONS]
