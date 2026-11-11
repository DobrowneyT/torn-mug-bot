"""API keys must never reach a log handler in full.

Regression for live Torn keys appearing in the systemd journal on every API call, via httpx's
INFO-level request logging (Torn takes the key as a URL query parameter).
"""

import io
import logging

from server.logging_utils import RedactSecretsFilter, install_redaction, redact

URL = ("https://api.torn.com/v2/market/249/itemmarket?key=FAKEkey0TESTabcd"
       "&comment=MonChoon+Market+Tracker")


def _capture(logger_name="httpx"):
    """A logger wired to a string buffer through the same handler-level filter main.py installs."""
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.addFilter(RedactSecretsFilter())
    logger = logging.getLogger(f"test.{logger_name}")
    logger.handlers = [handler]
    logger.setLevel(logging.INFO)
    logger.propagate = False
    return logger, stream


def test_redacts_lazy_args():
    """The case that actually leaked: httpx logs the URL as an ARG, not in the format string."""
    logger, stream = _capture()
    logger.info("HTTP Request: %s %s", "GET", URL)
    out = stream.getvalue()
    assert "FAKEkey0TESTabcd" not in out
    assert "key=…abcd" in out
    assert "market/249/itemmarket" in out   # the useful part of the URL survives


def test_redacts_preformatted_message():
    logger, stream = _capture()
    logger.info(f"fetching {URL}")
    out = stream.getvalue()
    assert "FAKEkey0TESTabcd" not in out
    assert "key=…abcd" in out


def test_redacts_dict_args():
    logger, stream = _capture()
    logger.info("%(method)s %(url)s", {"method": "GET", "url": URL})
    assert "FAKEkey0TESTabcd" not in stream.getvalue()


def test_multiple_keys_in_one_line_all_redacted():
    logger, stream = _capture()
    logger.info("a=%s b=%s", "x?key=AAAABBBBCCCCDDDD", "y?key=EEEEFFFFGGGGHHHH")
    out = stream.getvalue()
    assert "AAAABBBBCCCCDDDD" not in out and "EEEEFFFFGGGGHHHH" not in out
    assert "key=…DDDD" in out and "key=…HHHH" in out


def test_messages_without_keys_pass_through_unchanged():
    logger, stream = _capture("mug.poller")
    msg = "reconcile item=438: 16 listings -> tracked=15 uid=0 price=0 anon=0 unmatched=1"
    logger.info(msg)
    assert stream.getvalue().strip() == msg


def test_non_string_args_are_left_alone():
    """Guards against the filter coercing types and breaking %-formatting."""
    logger, stream = _capture("mug.poller")
    logger.info("item=%s count=%d ratio=%.1f", 399, 42, 1.5)
    assert stream.getvalue().strip() == "item=399 count=42 ratio=1.5"


def test_redact_is_idempotent():
    once = redact(URL)
    assert redact(once) == once


def test_install_redaction_is_idempotent():
    root = logging.getLogger()
    original = list(root.handlers)
    try:
        root.handlers = [logging.StreamHandler(io.StringIO())]
        install_redaction()
        install_redaction()
        filters = [f for f in root.handlers[0].filters if isinstance(f, RedactSecretsFilter)]
        assert len(filters) == 1      # re-running must not stack duplicate filters
    finally:
        root.handlers = original
