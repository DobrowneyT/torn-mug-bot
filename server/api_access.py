"""
Access control and access logging for the read API.

Two jobs, kept out of app.py because both are security-relevant and worth reading on their own.

The token is a bearer token over plain HTTP. It is not security in any strong sense — anyone on the
network path can read it — and it is not presented as such. What it buys is that the endpoint is not
usable by whoever finds the port, and that a leak can be revoked by changing one value.

The access log exists so "who has been using this" is answerable after the fact rather than
guessed at.
"""

import hmac
import logging
import logging.handlers
import os
import time
from typing import Dict, Optional, Tuple

import config

log = logging.getLogger("mug.api")

_access_log: Optional[logging.Logger] = None
# (token_prefix, ip) -> [tokens, last_refill]. In-memory on purpose: a restart clearing it is fine,
# and a shared counter across processes would be a database write on every request.
_buckets: Dict[Tuple[str, str], list] = {}


def _now() -> float:
    """Indirection so tests can drive the clock instead of sleeping."""
    return time.time()


def enabled() -> bool:
    return bool(config.API_TOKEN)


def get_access_log() -> logging.Logger:
    """A dedicated rotating logger, separate from the application log.

    Separate because the two are read for different reasons and have different retention needs —
    and because an access log buried in the service journal is one nobody actually greps. Rotating
    because a log that grows forever eventually becomes the outage.
    """
    global _access_log
    if _access_log is not None:
        return _access_log
    logger = logging.getLogger("mug.api.access")
    logger.propagate = False        # don't duplicate every request into the service log
    logger.setLevel(logging.INFO)
    try:
        os.makedirs(os.path.dirname(config.API_ACCESS_LOG) or ".", exist_ok=True)
        handler = logging.handlers.RotatingFileHandler(
            config.API_ACCESS_LOG,
            maxBytes=config.API_ACCESS_LOG_MAX_BYTES,
            backupCount=config.API_ACCESS_LOG_BACKUPS,
        )
    except OSError as e:
        # /var/log is not writable by the service user on some boxes. Falling back to the service
        # log keeps the record rather than losing it silently, and says where it went.
        log.warning("cannot open API access log at %s (%s) — falling back to the service log",
                    config.API_ACCESS_LOG, e)
        logger.propagate = True
        _access_log = logger
        return logger
    handler.setFormatter(logging.Formatter("%(asctime)s %(message)s"))
    logger.addHandler(handler)
    _access_log = logger
    return logger


def client_ip(request) -> str:
    """The caller's address.

    X-Forwarded-For is only consulted when API_TRUST_PROXY is set, because a client can send that
    header itself. Trusting it unconditionally means the access log faithfully records whichever IP
    the caller chose to claim — worse than having no log, because it looks authoritative.
    """
    if config.API_TRUST_PROXY:
        fwd = request.headers.get("x-forwarded-for")
        if fwd:
            return fwd.split(",")[0].strip()
    return getattr(getattr(request, "client", None), "host", None) or "?"


def _presented_token(request) -> str:
    auth = request.headers.get("authorization") or ""
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    # The userscript already sends `apikey` alongside Authorization (Supabase's convention), so
    # accepting it too means the fork needs one line changed rather than three.
    return (request.headers.get("apikey") or "").strip()


def authorise(request) -> bool:
    """Constant-time token check.

    compare_digest rather than ==: a plain comparison returns as soon as two bytes differ, which
    leaks the token's prefix to anyone willing to time enough requests. Cheap to do correctly.
    """
    if not enabled():
        return False
    return hmac.compare_digest(_presented_token(request), config.API_TOKEN)


def rate_limited(request) -> bool:
    """True if this caller has spent its budget. Continuous refill, per token+IP."""
    cap = float(config.API_RATE_PER_MIN)
    if cap <= 0:
        return False
    key = (_presented_token(request)[:8], client_ip(request))
    now = _now()
    tokens, last = _buckets.get(key, (cap, now))
    tokens = min(cap, tokens + (now - last) * cap / 60.0)
    if tokens < 1.0:
        _buckets[key] = [tokens, now]
        return True
    _buckets[key] = [tokens - 1.0, now]
    return False


def record(request, *, path: str, status: int, detail: str = "", elapsed_ms: float = 0.0) -> None:
    """One line per request: who, from where, asking what, and what they got."""
    get_access_log().info(
        "ip=%s status=%s path=%s ua=%r %s took=%.0fms",
        client_ip(request), status, path,
        (request.headers.get("user-agent") or "")[:120], detail, elapsed_ms,
    )


def reset_for_tests() -> None:
    global _access_log
    _buckets.clear()
    _access_log = None
