import json
import time
import argparse
from datetime import datetime, timezone
from pathlib import Path
import requests
from concurrent.futures import ThreadPoolExecutor, as_completed


def get_timestamp_pair():
    now = datetime.now(timezone.utc)
    return now.isoformat(), int(now.timestamp())


def load_api_keys(path: Path):
    if not path.exists():
        raise FileNotFoundError(f"Keys file not found: {path}")

    with path.open("r", encoding="utf-8") as f:
        data = json.load(f)

    keys = []
    for entry in data:
        key = entry.get("cpr_api_key")
        if key:
            keys.append(str(key).strip())

    seen = set()
    unique_keys = []
    for k in keys:
        if k and k not in seen:
            seen.add(k)
            unique_keys.append(k)

    if not unique_keys:
        raise RuntimeError(f"No cpr_api_key values found in {path}")

    return unique_keys


def load_items(path: Path):
    if not path.exists():
        raise FileNotFoundError(f"Items file not found: {path}")

    with path.open("r", encoding="utf-8") as f:
        data = json.load(f)

    items = []
    for item in data.get("items", []):
        items.append({"id": item["id"], "name": item["name"]})

    if not items:
        raise RuntimeError(f"No items found in {path}")

    return items


def load_existing_results(path: Path):
    if not path.exists():
        return []

    try:
        with path.open("r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, IOError):
        return []


def get_completed_batches(results):
    batches = set()
    for entry in results:
        if "batch_num" in entry:
            batches.add(entry["batch_num"])
    return batches


def make_request(item, api_key, unix_ts, iso_ts, second, key_index, batch_num):
    """Make a single market request and return the result."""
    item_id = item["id"]
    item_name = item["name"]
    url = f"https://api.torn.com/v2/market/{item_id}/itemmarket"
    params = {
        "limit": 100,
        "offset": 0,
        "timestamp": unix_ts,
        "key": api_key,
    }

    cache_timestamp = None
    cache_delay = None
    error = None

    try:
        resp = requests.get(url, params=params, timeout=10)
        status = resp.status_code
        if status == 200:
            data = resp.json()
            im = data.get("itemmarket", {})
            cache_timestamp = im.get("cache_timestamp")
            cache_delay = im.get("cache_delay")
        else:
            error = f"HTTP {status}"
    except Exception as e:
        error = str(e)

    return {
        "timestamp_utc": iso_ts,
        "timestamp_unix": unix_ts,
        "item_id": item_id,
        "item_name": item_name,
        "batch_num": batch_num,
        "key_index": key_index,
        "cache_timestamp": cache_timestamp,
        "cache_delay": cache_delay,
        "error": error,
    }


def run_batch(items, api_keys, batch_num, output_file, resume=False, max_workers=10):
    duration_seconds = 120
    interval_seconds = 1

    results = []
    if resume:
        results = load_existing_results(Path(output_file))

    completed = get_completed_batches(results)
    if batch_num in completed:
        print(f"Batch {batch_num} already completed, skipping")
        return results

    key_count = len(api_keys)
    print(f"\n=== Batch {batch_num}: {len(items)} items for {duration_seconds}s ===")
    print(f"Starting at {datetime.now(timezone.utc).isoformat()}")

    batch_results = []
    start_time = time.time()

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        for second in range(duration_seconds):
            iso_ts, unix_ts = get_timestamp_pair()

            # Submit all items for this second concurrently
            futures = {}
            for item in items:
                key_index = (second + batch_num * key_count) % key_count
                api_key = api_keys[key_index]

                future = executor.submit(
                    make_request,
                    item,
                    api_key,
                    unix_ts,
                    iso_ts,
                    second,
                    key_index,
                    batch_num,
                )
                futures[future] = item

            # Collect results as they complete
            for future in as_completed(futures):
                try:
                    entry = future.result()
                    batch_results.append(entry)
                except Exception as e:
                    item = futures[future]
                    print(f"[{iso_ts}] sec {second+1} item[{item['id']}] FUTURE ERROR: {e}")

            # Sleep to maintain per-second timing
            elapsed = time.time() - start_time
            target = (second + 1) * interval_seconds
            sleep_for = target - elapsed
            if sleep_for > 0:
                time.sleep(sleep_for)

    results.extend(batch_results)

    with open(output_file, "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=2)

    print(f"Batch {batch_num} done. Saved {len(batch_results)} entries. Total: {len(results)}")
    return results


def main():
    parser = argparse.ArgumentParser(description="Audit Torn market cache timing across all items")
    parser.add_argument("--items", type=str, default="items.json", help="Items JSON file")
    parser.add_argument("--keys", type=str, default=".secrets/member_api_keys.json", help="API keys JSON file")
    parser.add_argument("--batch-size", type=int, default=20, help="Items per batch")
    parser.add_argument("--item-limit", type=int, default=None, help="Limit total items (for testing)")
    parser.add_argument("--output", type=str, default="market_cache_probe_full.json", help="Output file")
    parser.add_argument("--resume", action="store_true", help="Resume from existing output (skip completed batches)")
    parser.add_argument("--workers", type=int, default=10, help="Max concurrent requests per second")

    args = parser.parse_args()

    api_keys = load_api_keys(Path(args.keys))
    print(f"Loaded {len(api_keys)} API keys")

    all_items = load_items(Path(args.items))
    if args.item_limit:
        all_items = all_items[:args.item_limit]
    print(f"Loaded {len(all_items)} items to scan")

    num_batches = (len(all_items) + args.batch_size - 1) // args.batch_size
    print(f"Will run {num_batches} batches of ~{args.batch_size} items")
    print(f"Total runtime: ~{num_batches * 120 / 60:.0f} minutes")

    results = []
    if args.resume:
        results = load_existing_results(Path(args.output))
        completed = get_completed_batches(results)
        print(f"Resuming: {len(completed)} batches already completed")

    for batch_num in range(num_batches):
        start_idx = batch_num * args.batch_size
        end_idx = min(start_idx + args.batch_size, len(all_items))
        batch_items = all_items[start_idx:end_idx]

        results = run_batch(batch_items, api_keys, batch_num, args.output, resume=(batch_num > 0 or args.resume), max_workers=args.workers)

    print(f"\n=== Audit complete ===")
    print(f"Total entries: {len(results)}")
    print(f"Saved to: {args.output}")


if __name__ == "__main__":
    main()
