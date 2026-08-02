"""
Seller enrichment: on first sight (or when stale) fill clothing-store (job), status (for the
hospital-accumulation timing + embed), and TBS (BSP). Gated by freshness TTLs so a busy seller
isn't re-fetched on every sale. Never raises — enrichment is best-effort.
"""

import asyncio
import logging
import time
from typing import Dict, Optional

import httpx

import config
from server import bsp, db, torn

log = logging.getLogger("mug.enrich")


def _fresh(ts: Optional[int], ttl: int) -> bool:
    return bool(ts) and (int(time.time()) - int(ts)) < ttl


async def enrich_seller(user_id: int, client: httpx.AsyncClient, *, force: bool = False) -> Optional[Dict]:
    seller = await db.get_seller(user_id)
    now = int(time.time())

    need_full = force or not seller or not _fresh(
        seller.get("clothing_checked_at"), config.SELLER_ENRICH_TTL_SECONDS
    )
    need_status = force or not seller or not _fresh(
        seller.get("status_checked_at"), config.SELLER_STATUS_TTL_SECONDS
    )
    if not need_full and not need_status:
        return seller

    fields: Dict = {}

    # These three sources are independent, so fetch them CONCURRENTLY. Run in series this was up to
    # three round trips (each with its own timeout) sitting between "we know it sold" and the alert
    # reaching Discord — pure latency on the one thing that has to be fast.
    #
    # return_exceptions: a failing source must not take the others down with it. Enrichment is
    # best-effort by contract, and a partial result still produces a usable alert.
    tasks = {}
    if need_status or need_full:
        tasks["profile"] = torn.get_profile(client, user_id)
    if need_full:
        tasks["job"] = torn.get_job(client, user_id)
        tasks["tbs"] = bsp.get_tbs(client, user_id)
    results = dict(zip(tasks, await asyncio.gather(*tasks.values(), return_exceptions=True)))

    def _ok(name):
        """The source's value, or None if it errored — never raise out of enrichment."""
        value = results.get(name)
        if isinstance(value, BaseException):
            log.warning("enrich %s failed for %s: %r", name, user_id, value)
            return None
        return value

    prof = _ok("profile")
    if prof is not None:
        st = prof.get("status") or {}
        fields["status_state"] = st.get("state")
        fields["status_until"] = st.get("until")
        fields["status_description"] = st.get("description")
        fields["status_details"] = st.get("details")   # "Mugged by …" / "Hospitalized by …" / medical
        fields["status_checked_at"] = now
        la = prof.get("last_action") or {}
        fields["last_action_ts"] = la.get("timestamp")
        fields["online_status"] = la.get("status")   # Online / Idle / Offline
        # v2 profile exposes faction_id as a FLAT scalar on the profile (no nested "faction"
        # object, and no faction name in this endpoint). 0 = not in a faction.
        fields["faction_id"] = prof.get("faction_id") or 0

    job = _ok("job")
    if job is not None:
        fields["job_type_id"] = job.get("type_id")
        fields["job_rating"] = job.get("rating")
        fields["job_company_name"] = job.get("name")
        fields["in_clothing_store"] = 1 if torn.is_clothing_store(job) else 0
        fields["clothing_checked_at"] = now

    tbs = _ok("tbs")
    if tbs is not None:
        fields["bsp_total"] = tbs.get("total")
        fields["bsp_str"] = tbs.get("str")
        fields["bsp_spd"] = tbs.get("spd")
        fields["bsp_def"] = tbs.get("def")
        fields["bsp_dex"] = tbs.get("dex")
        fields["bsp_fetched_at"] = now

    await db.update_seller_enrichment(user_id, fields)
    return await db.get_seller(user_id)


async def enrich_clothing(user_id: int, client: httpx.AsyncClient) -> Optional[Dict]:
    """Cheap, per-sale: ensure the clothing-store flag is fresh (24h) so the mug calc is right,
    WITHOUT fetching status/BSP every sale. Full status+TBS is fetched only at threshold-cross."""
    seller = await db.get_seller(user_id)
    if seller and _fresh(seller.get("clothing_checked_at"), config.SELLER_ENRICH_TTL_SECONDS):
        return seller
    job = await torn.get_job(client, user_id)
    if job is not None:
        await db.update_seller_enrichment(user_id, {
            "job_type_id": job.get("type_id"),
            "job_rating": job.get("rating"),
            "job_company_name": job.get("name"),
            "in_clothing_store": 1 if torn.is_clothing_store(job) else 0,
            "clothing_checked_at": int(time.time()),
        })
    return await db.get_seller(user_id)
