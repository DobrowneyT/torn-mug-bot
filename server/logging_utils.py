"""Keep Torn API keys out of the logs.

Torn takes the key as a URL query parameter, and httpx logs the full request URL at INFO — so with
the root logger at INFO, every API call wrote a live credential into the journal, thousands of times
an hour. Silencing httpx (see main) is the primary fix; this filter is the backstop, so that any
future code path or library that logs a URL can't quietly reintroduce the leak.

Masking matches the convention already used by torn._mask / keys._mask: the last four characters,
which is enough to tell two keys apart when debugging without disclosing either.
"""

import logging
import re

# key=<value> in a URL or query string. Torn keys are alphanumeric; the length floor avoids mangling
# unrelated text that happens to contain "key=".
_KEY_RE = re.compile(r"(key=)([A-Za-z0-9]{6,})")


def redact(text: str) -> str:
    return _KEY_RE.sub(lambda m: f"{m.group(1)}…{m.group(2)[-4:]}", text)


def _maybe(value):
    return redact(value) if isinstance(value, str) and "key=" in value else value


class RedactSecretsFilter(logging.Filter):
    """Rewrites `key=<secret>` to `key=…abcd` in both the format string and its arguments.

    Both matter: httpx logs lazily (``log.info("HTTP Request: %s %s", method, url)``), so the secret
    lives in ``record.args``, while hand-written f-strings put it in ``record.msg``. A filter that
    inspected only one of the two would miss the case that actually leaks.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.msg, str):
            record.msg = _maybe(record.msg)
        if record.args:
            if isinstance(record.args, dict):
                record.args = {k: _maybe(v) for k, v in record.args.items()}
            else:
                record.args = tuple(_maybe(a) for a in record.args)
        return True


def install_redaction() -> None:
    """Attach the filter to every root HANDLER.

    Deliberately not ``logging.getLogger().addFilter(...)``: a filter on a logger only sees records
    logged *directly* to it, not ones propagated up from children — so a logger-level filter would
    miss every httpx record, which is precisely what we're guarding against. Handler-level filters
    see everything that reaches the handler.
    """
    for handler in logging.getLogger().handlers:
        if not any(isinstance(f, RedactSecretsFilter) for f in handler.filters):
            handler.addFilter(RedactSecretsFilter())
