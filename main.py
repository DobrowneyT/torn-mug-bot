"""
Single-process entrypoint: runs the FastAPI ingest server and the discord.py bot on one
asyncio event loop, so the server can `await bot.post_mug_alert(...)` directly with no IPC.

Env is loaded from mug-bot/.env (wins) then ../.secrets/.env (fallback) BEFORE importing
config. If MUG_BOT_DISCORD_TOKEN is unset, the ingest server still runs (Phase 0 / discovery)
without a Discord connection.

    cd mug-bot && python main.py
"""

import asyncio
import logging
import os
import sys

_ROOT = os.path.dirname(os.path.abspath(__file__))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from dotenv import load_dotenv

# Self-contained: all secrets/config come from mug-bot/.env (this folder is deployed
# standalone to the box and must not depend on anything else in the repo).
load_dotenv(os.path.join(_ROOT, ".env"))

import uvicorn  # noqa: E402

import config  # noqa: E402
from server import db, logging_utils  # noqa: E402
from server.app import create_app  # noqa: E402

logging.basicConfig(
    level=getattr(logging, config.LOG_LEVEL, logging.INFO),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
# httpx logs the full request URL at INFO, and Torn takes the API key as a query parameter — so
# leaving this at INFO wrote a live credential into the journal on every single API call (thousands
# an hour), and buried our own logs while doing it. WARNING keeps genuine transport failures.
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
# Backstop: even a WARNING from httpx can carry a URL, and future code may log one. Redact at the
# handler so nothing reaches the journal with a key in it.
logging_utils.install_redaction()

log = logging.getLogger("mug.main")


async def _validate_keys_bg() -> None:
    import httpx

    from server import keys as keypool

    try:
        async with httpx.AsyncClient() as client:
            await keypool.validate_all(client)
        log.info("API key validation done: %d active", keypool.active_count())
    except Exception:
        log.exception("API key validation failed")


async def _prune_raw_events_daily() -> None:
    """Startup + daily retention pass. raw_events shares a database with the poller and the alert
    path, so an unbounded Phase 0 catch-all table is not free."""
    while True:
        try:
            removed = await db.prune_raw_events()
            if removed:
                log.info("pruned %d raw_events older than %d days", removed, config.RAW_RETENTION_DAYS)
        except Exception:
            log.exception("raw_events prune failed")
        await asyncio.sleep(24 * 3600)


async def amain() -> None:
    await db.init_db()
    log.info("SQLite ready at %s", db.db_path())
    prune_task = asyncio.create_task(_prune_raw_events_daily())

    from server import keys as keypool

    await db.seed_api_keys_from_env(config.TORN_API_KEYS)
    await keypool.refresh()
    log.info("API keys: %d active (validating in background)", keypool.active_count())
    asyncio.create_task(_validate_keys_bg())

    bot = None
    bot_task = None
    if config.DISCORD_TOKEN:
        from bot.discord_bot import MugBot

        bot = MugBot()
        bot_task = asyncio.create_task(bot.start(config.DISCORD_TOKEN))
        log.info("Discord bot starting")
    else:
        log.warning("MUG_BOT_DISCORD_TOKEN not set — running ingest server only (no Discord)")

    poller_task = None
    if config.detection_api_enabled():
        from server import api_poller

        poller_task = asyncio.create_task(api_poller.run_gear_poller(bot))
        log.info("API gear poller enabled (DETECTION_MODE=%s)", config.DETECTION_MODE)
    log.info("Detection mode: %s", config.DETECTION_MODE)

    # The read API serves the AH ledger, which lives in a separate file the SCRAPER owns. Opened
    # read-only: a bug on the request path then cannot write to, migrate or lock the scraper's data,
    # and the two processes can never contend for the write lock.
    if config.API_TOKEN:
        from server import ah_db
        try:
            await ah_db.init_db(read_only=True)
            log.info("read API enabled — AH ledger (read-only) at %s", ah_db.db_path())
        except Exception:
            log.exception("could not open the AH ledger read-only — /api/auctions/* will fail")
    else:
        log.info("read API disabled (MUG_API_TOKEN not set)")

    app = create_app(bot)
    server = uvicorn.Server(
        uvicorn.Config(
            app,
            host=config.SERVER_HOST,
            port=config.SERVER_PORT,
            log_level="info",
            loop="asyncio",
        )
    )
    log.info("Ingest server on http://%s:%s", config.SERVER_HOST, config.SERVER_PORT)
    try:
        await server.serve()
    finally:
        prune_task.cancel()
        if poller_task is not None:
            poller_task.cancel()
        if bot is not None:
            await bot.close()
        if bot_task is not None:
            try:
                await bot_task
            except Exception:
                pass
        await db.close_db()
        if config.API_TOKEN:
            from server import ah_db
            await ah_db.close_db()


def main() -> None:
    try:
        asyncio.run(amain())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
