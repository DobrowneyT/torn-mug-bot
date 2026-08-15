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


def split_banked(sales, last_action_ts, *, grace=None) -> tuple:
    """Split a seller's window into (on_hand, banked) by their last action.

    The bank is two clicks away, so a sale is only muggable until the seller next touches the game.
    Anything they sold BEFORE their last action is assumed already deposited and is dropped
    outright — not discounted. There is no partial credit here on purpose: the old linear decay
    handed a sale from an hour ago some fraction of its value and kept firing $0 mugs on sellers who
    had plainly been online since.

    `grace` covers the seconds either side where "acted after the sale" is really clock skew — our
    `sold_at` is when the poller SAW the listing vanish, not when the buyer clicked. A sale inside
    that of the last action still counts.

    No `last_action_ts` means we know nothing about whether they've been online, and an assumption
    that deletes value needs evidence — so nothing is dropped.

    The split is purely by time, so every banked sale is strictly older than every kept one.
    """
    grace = config.MUG_BANK_GRACE_SECONDS if grace is None else grace
    if not last_action_ts:
        return list(sales), []
    cutoff = int(last_action_ts) - int(grace)
    on_hand = [s for s in sales if int(s.get("sold_at") or 0) >= cutoff]
    banked = [s for s in sales if int(s.get("sold_at") or 0) < cutoff]
    return on_hand, banked


def bank_adjusted_total(sales, last_action_ts, *, grace=None) -> int:
    """What of the window is still in the seller's wallet. See `split_banked`."""
    on_hand, _ = split_banked(sales, last_action_ts, grace=grace)
    return sum(int(s.get("total_value") or 0) for s in on_hand)
