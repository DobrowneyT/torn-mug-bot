import json
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

# Config
ITEM_IDS = [682]
# ITEM_IDS = [682]
PRIMARY_ITEM_ID = 682
PRIMARY_UID = 9632834426        # your test EOD Pants uid
PRIMARY_PRICE = 20_000_000_000  # optional extra check

DURATION_SECONDS = 300          # total time to run
INTERVAL_SECONDS = 1            # one request per second

KEYS_FILE = Path(".secrets/member_api_keys.json")
OUTFILE = "market_cache_probe_eod.json"


def get_timestamp_pair():
    now = datetime.now(timezone.utc)
    return now.isoformat(), int(now.timestamp())


def listing_is_present(data: dict) -> bool:
    """
    Returns True if the test listing is present in itemmarket.listings
    for the PRIMARY_UID/PRIMARY_PRICE.
    """
    try:
        listings = data["itemmarket"]["listings"]
    except (TypeError, KeyError):
        return False

    for l in listings:
        try:
            uid = l["item_details"]["uid"]
            price = l["price"]
        except KeyError:
            continue

        if uid == PRIMARY_UID and price == PRIMARY_PRICE:
            return True
    return False


def load_api_keys(path: Path):
    """
    Load all cpr_api_key values from the given JSON file.

    Expected schema:
    [
      {
        "member_name": "...",
        "member_id": "...",
        "cpr_api_key": "KEY1"
      },
      ...
    ]
    """
    if not path.exists():
        raise FileNotFoundError(f"Keys file not found: {path}")

    with path.open("r", encoding="utf-8") as f:
        data = json.load(f)

    keys = []
    for entry in data:
        key = entry.get("cpr_api_key")
        if key:
            keys.append(str(key).strip())

    # Remove duplicates while preserving order
    seen = set()
    unique_keys = []
    for k in keys:
        if k and k not in seen:
            seen.add(k)
            unique_keys.append(k)

    if not unique_keys:
        raise RuntimeError(f"No cpr_api_key values found in {path}")

    return unique_keys


def main():
    api_keys = load_api_keys(KEYS_FILE)
    key_count = len(api_keys)
    print(f"Loaded {key_count} API keys from {KEYS_FILE}")

    results = []
    print(
        f"Starting multi-key market presence probe for items {ITEM_IDS}, "
        f"primary item {PRIMARY_ITEM_ID}, uid={PRIMARY_UID}..."
    )
    start = time.time()

    for i in range(DURATION_SECONDS):
        iso_ts, unix_ts = get_timestamp_pair()

        # Rotate through the loaded keys once per second
        key_index = i % key_count
        api_key = api_keys[key_index]

        for item_id in ITEM_IDS:
            url = f"https://api.torn.com/v2/market/{item_id}/itemmarket"
            params = {
                "limit": 100,
                "offset": 0,
                "timestamp": unix_ts,  # per-call timestamp
                "key": api_key,
            }

            on_market = None
            cache_timestamp = None
            cache_delay = None

            try:
                resp = requests.get(url, params=params, timeout=10)
                status = resp.status_code
                if status == 200:
                    data = resp.json()
                    im = data.get("itemmarket", {})
                    cache_timestamp = im.get("cache_timestamp")
                    cache_delay = im.get("cache_delay")

                    # Only check presence for the primary item
                    if item_id == PRIMARY_ITEM_ID:
                        on_market = listing_is_present(data)
                else:
                    print(
                        f"[{iso_ts}] #{i+1} key[{key_index}] "
                        f"item[{item_id}] status={status} (on_market=None)"
                    )
            except Exception as e:
                print(
                    f"[{iso_ts}] #{i+1} key[{key_index}] "
                    f"item[{item_id}] ERROR {e} (on_market=None)"
                )

            entry = {
                "timestamp_utc": iso_ts,
                "timestamp_unix": unix_ts,
                "item_id": item_id,
                "key_index": key_index,
                "on_market": on_market,          # only meaningful for PRIMARY_ITEM_ID
                "cache_timestamp": cache_timestamp,
                "cache_delay": cache_delay,
            }
            results.append(entry)

            print(
                f"[{iso_ts}] #{i+1} key[{key_index}] item[{item_id}] "
                f"unix={unix_ts}, cache_ts={cache_timestamp}, "
                f"delay={cache_delay}, on_market={on_market}"
            )

        elapsed = time.time() - start
        target = (i + 1) * INTERVAL_SECONDS
        sleep_for = target - elapsed
        if sleep_for > 0:
            time.sleep(sleep_for)

    with open(OUTFILE, "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=2)

    print(f"Done. Saved {len(results)} entries to {OUTFILE}")


if __name__ == "__main__":
    main()