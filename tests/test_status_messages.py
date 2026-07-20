"""The status panel must only re-edit a message when its content actually changed — otherwise a
big watch list machine-guns Discord edits every poll and gets rate-limited (429s)."""

import asyncio

import discord

from bot import state
from bot.discord_bot import MugBot


class _FakeMsg:
    def __init__(self, mid):
        self.id = mid
        self.edits = 0

    async def edit(self, **kwargs):
        self.edits += 1


class _FakeChannel:
    def __init__(self):
        self.sends = 0
        self._msgs = {}

    async def send(self, **kwargs):
        self.sends += 1
        m = _FakeMsg(1000 + self.sends)
        self._msgs[m.id] = m
        return m

    async def fetch_message(self, mid):
        return self._msgs[mid]


def test_status_message_skips_unchanged(tmp_path, monkeypatch):
    monkeypatch.setattr(state, "STATE_PATH", str(tmp_path / "state.json"))
    bot = MugBot()
    ch = _FakeChannel()

    async def run():
        e1 = [discord.Embed(title="Armour — 5/5", description="rows v1")]
        assert await bot._upsert_status_message(ch, "watch_Defensive", e1) is True   # first send
        assert ch.sends == 1
        # Same content (even a new embed object) -> skipped, no Discord call.
        again = [discord.Embed(title="Armour — 5/5", description="rows v1")]
        assert await bot._upsert_status_message(ch, "watch_Defensive", again) is False
        # Footer-only change is ignored (footer carries a per-render timestamp).
        with_footer = discord.Embed(title="Armour — 5/5", description="rows v1")
        with_footer.set_footer(text="Last update: later")
        assert await bot._upsert_status_message(ch, "watch_Defensive", [with_footer]) is False
        # Real content change -> edit.
        e2 = [discord.Embed(title="Armour — 6/6", description="rows v2")]
        assert await bot._upsert_status_message(ch, "watch_Defensive", e2) is True
        assert ch._msgs[1001].edits == 1

    asyncio.run(run())


def test_commands_registered_including_help():
    from discord import app_commands
    bot = MugBot()
    bot._register_commands()
    names = {c.name for c in bot.tree.get_commands()}
    assert {"help", "mugmin", "watch", "keys", "faction", "gearbonus"} <= names
    watch = next(c for c in bot.tree.get_commands() if c.name == "watch")
    assert isinstance(watch, app_commands.Group)
    assert {"add", "remove", "enable", "disable", "clear", "list"} <= {s.name for s in watch.commands}
