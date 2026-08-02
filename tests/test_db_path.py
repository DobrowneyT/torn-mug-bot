"""Where the SQLite file lands, and whether the operator is told about it.

The fallback used to be silent, which is how production ended up with its database inside the git
working tree — one `git clean` from losing every capture, seller mapping and API key.
"""

import logging
import os

import config
from server import db


def _force_fallback(monkeypatch, tmp_path, fallback_name="mug_bot.db"):
    """Point DB_PATH at an unwritable location so _resolve_db_path takes the fallback branch."""
    monkeypatch.setattr(config, "DB_PATH", "/proc/definitely-not-writable/mug_bot.db")
    fallback = str(tmp_path / fallback_name)
    monkeypatch.setattr(config, "DB_PATH_FALLBACK", fallback)
    return fallback


def test_writable_db_path_is_used_without_warning(tmp_path, monkeypatch, caplog):
    wanted = str(tmp_path / "data" / "mug_bot.db")
    monkeypatch.setattr(config, "DB_PATH", wanted)
    with caplog.at_level(logging.WARNING, logger="mug.db"):
        assert db._resolve_db_path() == wanted
    assert caplog.records == []


def test_fallback_warns_and_names_the_env_var(tmp_path, monkeypatch, caplog):
    fallback = _force_fallback(monkeypatch, tmp_path)
    with caplog.at_level(logging.WARNING, logger="mug.db"):
        assert db._resolve_db_path() == fallback
    assert len(caplog.records) == 1
    msg = caplog.records[0].getMessage()
    assert "MUG_DB_PATH" in msg          # tells the operator how to fix it
    assert fallback in msg               # ...and where the data actually went


def test_fallback_inside_a_git_checkout_is_escalated(tmp_path, monkeypatch, caplog):
    """The dangerous case: the DB would live in a working tree, where git can destroy it."""
    os.makedirs(tmp_path / ".git")
    _force_fallback(monkeypatch, tmp_path)
    with caplog.at_level(logging.WARNING, logger="mug.db"):
        db._resolve_db_path()
    msg = caplog.records[0].getMessage()
    assert "GIT CHECKOUT" in msg
    assert "DESTROY" in msg


def test_fallback_outside_a_checkout_is_not_escalated(tmp_path, monkeypatch, caplog):
    # tmp_path has no .git at or above it, so this is the benign fallback.
    _force_fallback(monkeypatch, tmp_path)
    with caplog.at_level(logging.WARNING, logger="mug.db"):
        db._resolve_db_path()
    assert "GIT CHECKOUT" not in caplog.records[0].getMessage()


def test_within_git_checkout_walks_upwards(tmp_path):
    os.makedirs(tmp_path / ".git")
    nested = tmp_path / "a" / "b"
    os.makedirs(nested)
    assert db._within_git_checkout(str(nested / "mug_bot.db")) is True
    assert db._within_git_checkout("/proc/mug_bot.db") is False
