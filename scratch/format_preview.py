"""
Track A runner: post (or edit) MOCK embeds in a test channel so we can iterate on the
formatter without a live server.

Usage:
    cd mug-bot
    python3 -m venv .venv && source .venv/bin/activate
    pip install -r requirements.txt
    cp .env.example .env            # set MUG_BOT_DISCORD_TOKEN + MUG_PREVIEW_CHANNEL_ID
    python scratch/format_preview.py

First run sends one message per embed type and records their ids in bot/state.json;
reruns edit those messages in place — re-run after each formatter.py tweak to see the
change without spamming the channel.
"""

import asyncio
import logging
import os
import sys

# Make `import config` / `from bot import ...` work when run as a script, and load env
# BEFORE importing config (its constants are read at import time).
_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from dotenv import load_dotenv

load_dotenv(os.path.join(_ROOT, ".env"))   # self-contained: only this folder's .env

import discord  # noqa: E402

import config  # noqa: E402
from bot import formatter, mock_data, state  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("format_preview")


async def _upsert(channel, key, embeds):
    existing = state.get_message_id(key)
    if existing:
        try:
            msg = await channel.fetch_message(existing)
            await msg.edit(embeds=embeds)
            log.info("edited %s (message %s)", key, existing)
            return
        except discord.NotFound:
            log.warning("stored %s message %s gone — sending a new one", key, existing)
    msg = await channel.send(embeds=embeds)
    state.set_message_id(key, msg.id)
    log.info("sent %s (message %s)", key, msg.id)


def main() -> None:
    token = os.environ.get("MUG_BOT_DISCORD_TOKEN")
    channel_id = config.PREVIEW_CHANNEL_ID or config.LOGS_CHANNEL_ID
    if not token or not channel_id:
        raise SystemExit("MUG_BOT_DISCORD_TOKEN and MUG_PREVIEW_CHANNEL_ID must be set in mug-bot/.env")

    intents = discord.Intents.default()
    client = discord.Client(intents=intents)

    @client.event
    async def on_ready():
        try:
            channel = client.get_channel(channel_id) or await client.fetch_channel(channel_id)
            await _upsert(channel, "preview_mug",
                          [formatter.build_mug_alert_embed(mock_data.mock_mug_event())])
            await _upsert(channel, "preview_hospital",
                          [formatter.build_mug_alert_embed(mock_data.mock_hospital_mug_event())])
            await _upsert(channel, "preview_status",
                          formatter.build_status_embeds(mock_data.mock_status()))
            await _upsert(channel, "preview_deal",
                          [formatter.build_deal_embed(mock_data.mock_deal())])
        finally:
            await client.close()

    client.run(token, log_handler=None)


if __name__ == "__main__":
    main()
