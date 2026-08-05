"""Given a uid, who owns it — from which source, with what confidence, and whether that is worth
a ping.

This was three data structures and a lookup consulted inline in `reconcile_gear`. It reads better
here, but that is not the reason it moved. The reason is that the precedence rules were only
reachable by driving a full poll against two databases, and one of them — the **upgrade** rule — is
invisible when it breaks: the alert still fires, on time, naming the wrong player.

Two rules, and they are not the same rule:

1. **Precedence** decides which source wins when a uid resolves from scratch. A direct capture beats
   an auction inference, because the matcher saw the listing and the auction house is only saying
   nobody has taken it off this person since.
2. **Upgrade** decides what happens when a uid we already attributed resolves differently later. It
   exists because the tracked branch in `reconcile_gear` short-circuits the chain: without an
   explicit upgrade the FIRST answer wins for the life of the listing, and the first answer is
   usually the weaker one.

Adding a source (a bazaar scraper, say) means adding it to `_PRECEDENCE` and writing a resolver.
Both rules then hold for it without being restated, which is the property the inline version did
not have.
"""

import logging
from dataclasses import dataclass, replace
from typing import Any, Dict, Optional

import config
from server import db

log = logging.getLogger("mug.attrib")

MATCHER = "matcher"      # the item-matcher userscript saw this uid in a named listing
AUCTION = "auction"      # inferred: they won this uid at auction and nothing since says otherwise

# Strongest first. A source may only be replaced by one that appears EARLIER in this tuple —
# stated once, so a new source cannot silently acquire the right to overwrite a stronger one.
_PRECEDENCE = (MATCHER, AUCTION)


def _rank(source: Optional[str]) -> int:
    """Lower is stronger. An unknown or absent source ranks below everything."""
    try:
        return _PRECEDENCE.index(source)
    except ValueError:
        return len(_PRECEDENCE)


@dataclass(frozen=True)
class Attribution:
    """Who owns a uid, and how well we know it.

    Frozen because this is evidence, not state: a caller that wants a different answer has to
    produce a new one through the rules below rather than editing this in place.
    """

    seller_id: Optional[int] = None
    seller_name: Optional[str] = None
    source: Optional[str] = None
    item_name: Optional[str] = None
    confidence: Optional[float] = None
    silent: bool = False              # post the alert, but without a ping
    reason: str = ""
    # What the claim rests on, captured NOW. The alert fires hours later against a table that is
    # recomputed wholesale on every scrape, so a uid that has since left the candidate set no longer
    # has a row to look up — and the alert would then describe its own reasoning as "this player won
    # this item", with no price, no date and no confidence. Evidence is a snapshot; only the POLICY
    # (mode, threshold) is re-read at alert time.
    evidence: Optional[Dict[str, Any]] = None

    @property
    def matched(self) -> bool:
        return self.seller_id is not None

    def beats(self, other: Optional[str]) -> bool:
        return _rank(self.source) < _rank(other)


UNMATCHED = Attribution(reason="no direct capture and no AH win")


def from_capture(uid_map_row: Dict[str, Any]) -> Attribution:
    """A uid the item-matcher captured: the strongest thing we have, and not probabilistic."""
    return Attribution(
        seller_id=uid_map_row["seller_id"],
        source=MATCHER,
        item_name=uid_map_row.get("item_name"),
        confidence=1.0,
        reason="item-matcher captured this uid in a named listing",
    )


async def resolve(uid, uid_map: Dict[str, Any], *, item_label: Optional[str] = None,
                  anonymous: bool = False) -> Attribution:
    """Who owns this uid, consulting every source in precedence order.

    Gear is attributed by **exact uid** only. There is deliberately no price fallback: matching on
    price alone pins one player's weapon onto another who listed at the same round number (the
    $300M Swiss Army Knife mis-match). An uncaptured uid staying unmatched is the correct outcome.

    `anonymous` is a HARD GATE on the auction-house inference, not a hint. The pipeline is:

        item market API  ->  uid, no seller
        item-matcher     ->  the seller, definitively — or "this listing is anonymous"
        anon cache       ->  the uids the matcher could not name
        auction house    ->  a guess, for those and ONLY those

    A uid that has not been through the matcher yet is not a dead end, it is a QUEUE ENTRY. Guessing
    at it is wrong twice over: a ~77% inference pre-empts an answer that would have been certain,
    and — because attributing a uid removes it from `unmatched_gear` — the guess deletes the very
    queue entry that would have produced the certain answer. The inference cannibalises the
    pipeline that grounds it.
    """
    if uid in uid_map:
        return from_capture(uid_map[uid])

    if not anonymous:
        return replace(UNMATCHED, reason="not yet seen by the item-matcher — capture it first")

    # The matcher has seen this listing and could not name the seller. NOW the auction house is the
    # best evidence available: if this uid was won at auction and nothing since says otherwise, the
    # person listing it anonymously is the winner.
    match = await db.get_anon_match(uid)
    decision = await decide_anon(match)
    if not decision["use"]:
        return replace(UNMATCHED, reason=decision["reason"])
    return Attribution(
        seller_id=match["buyer_id"],
        seller_name=match.get("buyer_name"),
        source=AUCTION,
        item_name=item_label,
        confidence=float(match.get("confidence") or 0.0),
        silent=decision["silent"],
        reason=decision["reason"],
        evidence=dict(match),
    )


def upgrade(current: Attribution, uid, uid_map: Dict[str, Any]) -> Attribution:
    """A uid we already attributed, re-checked against the sources that have since spoken.

    Only ever moves UP the precedence order. The case that matters: a uid tracked on an auction
    inference, later captured directly by the item-matcher — the capture must replace the
    inference, and nothing else in the poll will do it, because the tracked branch returns before
    the resolution chain runs.

    Downgrades are not a thing. Evidence does not get weaker by being re-examined, and letting a
    weaker source overwrite a stronger one would make the answer depend on poll ordering.
    """
    if uid not in uid_map:
        return current
    candidate = from_capture(uid_map[uid])
    if not candidate.beats(current.source):
        return current
    log.info("uid=%s: %s captured seller=%s — replacing the %s attribution (%s)",
             uid, MATCHER, candidate.seller_id, current.source or "unknown", current.seller_id)
    return candidate


async def decide_anon(match: Optional[Dict]) -> Dict:
    """Should an AH-inferred seller be alerted on, and should it ping? -> {use, silent, reason}.

    The seller here was never observed — they won this uid at auction and nothing since says they
    parted with it. That is a genuinely weaker claim than an item-matcher capture, so the two must
    not look alike at a glance.

    Measured accuracy by evidence age (13,090 comparisons against directly-observed sellers):
    92.3% under a day, 88.0% within a week, 75.4% at 30-90d, 69.3% at 90-365d, 58.3% beyond.

    The shape of that curve is why `ping` is confidence-gated rather than age-gated. There is no
    cliff to cut at — old evidence is weaker, not worthless — so a hard age cutoff would throw away
    thousands of usable matches to avoid a minority of wrong ones. Weighting keeps them and says how
    much to trust each.
    """
    if not match:
        return {"use": False, "silent": True, "reason": "no AH win for this uid"}
    mode = (await db.get_config("anonmatch_mode", config.ANONMATCH_MODE) or "silent").strip().lower()
    if mode == "off":
        return {"use": False, "silent": True, "reason": "anonmatch off"}
    confidence = float(match.get("confidence") or 0.0)
    if mode != "ping":
        return {"use": True, "silent": True, "reason": f"mode={mode}"}
    threshold = float(await db.get_config("anonmatch_min_confidence", config.ANONMATCH_MIN_CONFIDENCE))
    if confidence >= threshold:
        return {"use": True, "silent": False, "reason": f"confidence {confidence:.2f} >= {threshold:.2f}"}
    return {"use": True, "silent": True,
            "reason": f"confidence {confidence:.2f} < {threshold:.2f} — posting without a ping"}


async def silence_for(source: Optional[str], uid, evidence: Optional[Dict] = None) -> tuple:
    """-> (evidence_row, silent). The alert path's half of the same question.

    Evidence and policy are treated differently on purpose:

    * **Evidence** — who won it, for how much, when, and how likely that is to be right — is
      whatever was true when the uid was attributed. It is passed in, because `anon_matches` is
      rebuilt wholesale by every scrape and the row may simply not exist by the time the sale
      fires. Looking it up again is how the alert ended up saying "this player won this item".
    * **Policy** — the mode and the ping threshold — is re-read, so an operator who just ran
      `/anonmatch silent` sees it take effect on the NEXT alert rather than the one after.

    The lookup is kept only as a fallback for rows attributed before evidence was stored.
    """
    if source != AUCTION or not uid:
        return None, False
    match = evidence or await db.get_anon_match(uid)
    return match, (await decide_anon(match))["silent"]
