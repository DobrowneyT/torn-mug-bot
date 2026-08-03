"""
Cron entrypoint for the auction house scraper.

Exists as a sibling of main.py rather than as `python -m server.ah_scraper` for the same reason
main.py does: .env has to be loaded BEFORE config is imported, because config reads the environment
at import time. A `-m` invocation imports the package first, so the env would already be missed.

    ./.venv/bin/python ah_scrape.py forward     # the cron job
    ./.venv/bin/python ah_scrape.py backfill    # walk history backwards, resumable
    ./.venv/bin/python ah_scrape.py status      # is it healthy, how much have we got
    ./.venv/bin/python ah_scrape.py probe       # dump raw API rows, store nothing
"""

import asyncio
import logging
import os
import sys

_ROOT = os.path.dirname(os.path.abspath(__file__))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from dotenv import load_dotenv

load_dotenv(os.path.join(_ROOT, ".env"))

import config  # noqa: E402
from server import logging_utils  # noqa: E402
from server.ah_scraper import cli  # noqa: E402

logging.basicConfig(
    level=getattr(logging, config.LOG_LEVEL, logging.INFO),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
# Same reasoning as main.py: httpx logs full request URLs at INFO and Torn takes the API key as a
# query parameter, so INFO here would write live credentials into the cron log on every call.
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
logging_utils.install_redaction()


if __name__ == "__main__":
    sys.exit(asyncio.run(cli()))
