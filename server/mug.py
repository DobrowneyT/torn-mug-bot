"""
Mug value calculation — pure functions (no I/O), so they're trivial to unit-test and tune.

    mug range = sale_value * [pct_min, pct_max] * clothing_mult

  - sale_value      : the seller's cumulative sales in the window (cash-on-hand proxy), or
                      their actual money-on-hand when known.
  - clothing_mult   : 0.25 when the seller has the 7*+ clothing-store reduction, else 1.0.
  - qualifies       : mug_min >= min_value (default config.MIN_MUG_VALUE).

Defaults come from config.py but every knob is overridable so the server can pass live values
from the SQLite `config` table (Discord-tunable) and tests can pin exact numbers.
"""

from dataclasses import dataclass
from typing import Optional

import config


@dataclass
class MugResult:
    mug_min: int
    mug_max: int
    clothing_mult: float
    qualifies: bool


def compute_mug(
    sale_value: float,
    *,
    in_clothing_store: bool = False,
    pct_min: Optional[float] = None,
    pct_max: Optional[float] = None,
    clothing_mult: Optional[float] = None,
    min_value: Optional[float] = None,
) -> MugResult:
    pct_min = config.MUG_PCT_MIN if pct_min is None else pct_min
    pct_max = config.MUG_PCT_MAX if pct_max is None else pct_max
    base_clothing = config.CLOTHING_STORE_MULTIPLIER if clothing_mult is None else clothing_mult
    cm = base_clothing if in_clothing_store else 1.0
    min_value = config.MIN_MUG_VALUE if min_value is None else min_value

    sale_value = max(0, int(sale_value or 0))
    mug_min = int(round(sale_value * pct_min * cm))
    mug_max = int(round(sale_value * pct_max * cm))
    return MugResult(mug_min=mug_min, mug_max=mug_max, clothing_mult=cm, qualifies=mug_min >= min_value)


def bank_adjusted_total(sales, last_action_ts, *, grace=None, assume=None) -> int:
    """Discount each sale by how long the seller stayed ACTIVE after it — they've had time to bank
    the cash. Full value if they went quiet within `grace` of the sale; zero once they've been
    active `assume` seconds past it; linear in between. `last_action_ts` None (or assume<=grace) =>
    no discount (count everything). Mirrors the "online since the sale ⇒ probably banked" heuristic.
    """
    grace = config.MUG_BANK_GRACE_SECONDS if grace is None else grace
    assume = config.MUG_BANK_ASSUME_SECONDS if assume is None else assume
    if not last_action_ts or assume <= grace:
        return sum(int(s.get("total_value") or 0) for s in sales)
    total = 0.0
    for s in sales:
        gap = int(last_action_ts) - int(s.get("sold_at") or 0)
        if gap <= grace:
            w = 1.0
        elif gap >= assume:
            w = 0.0
        else:
            w = 1.0 - (gap - grace) / (assume - grace)
        total += (s.get("total_value") or 0) * w
    return int(round(total))
