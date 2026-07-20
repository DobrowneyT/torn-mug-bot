"""
Persist the bot's per-channel message ids to bot/state.json (atomic writes).

The status-tracker embeds are edited in place, so we remember their message ids;
format_preview.py reuses the same store so reruns edit rather than spam. Mirrors the
robust load/save pattern from discord/state.py (tolerate missing/corrupt, write via
.tmp + os.replace).
"""

import json
import logging
import os
from typing import Optional

STATE_PATH = os.path.join(os.path.dirname(__file__), "state.json")

log = logging.getLogger("mug.state")


def load_state() -> dict:
    if not os.path.exists(STATE_PATH):
        return {}
    try:
        with open(STATE_PATH, "r") as f:
            text = f.read()
    except OSError as e:
        log.warning("state.json unreadable (%s) — treating as empty", e)
        return {}
    if not text.strip():
        return {}
    try:
        return json.loads(text)
    except json.JSONDecodeError as e:
        backup = STATE_PATH + ".broken"
        try:
            os.replace(STATE_PATH, backup)
            log.error("state.json invalid JSON (%s) — moved to %s and starting fresh", e, backup)
        except OSError as move_err:
            log.error("state.json invalid (%s) and un-backable (%s)", e, move_err)
        return {}


def save_state(state: dict) -> None:
    tmp = STATE_PATH + ".tmp"
    with open(tmp, "w") as f:
        json.dump(state, f, indent=2)
    os.replace(tmp, STATE_PATH)


def get_message_id(key: str) -> Optional[int]:
    raw = load_state().get("message_ids", {}).get(key)
    return int(raw) if raw else None


def set_message_id(key: str, message_id: int) -> None:
    state = load_state()
    state.setdefault("message_ids", {})[key] = int(message_id)
    save_state(state)


def clear_message_id(key: str) -> None:
    state = load_state()
    if state.get("message_ids", {}).pop(key, None) is not None:
        save_state(state)


def all_message_keys() -> list:
    return list(load_state().get("message_ids", {}).keys())


def get_status_layout() -> list:
    """The ordered list of status message keys last posted (to detect when panels change)."""
    return load_state().get("status_layout", [])


def set_status_layout(keys: list) -> None:
    state = load_state()
    state["status_layout"] = list(keys)
    save_state(state)
