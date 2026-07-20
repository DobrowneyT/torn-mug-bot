import json
import argparse
from pathlib import Path
from collections import defaultdict


def load_probe_data(path: Path):
    if not path.exists():
        raise FileNotFoundError(f"Probe data file not found: {path}")

    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def extract_item_data(rows, item_id):
    """Filter and sort rows for a specific item by timestamp."""
    item_rows = [r for r in rows if r.get("item_id") == item_id and r.get("error") is None]
    item_rows.sort(key=lambda r: r.get("timestamp_unix", 0))
    return item_rows


def find_cache_flips(item_data):
    """Detect every time cache_timestamp changes."""
    flips = []
    last_cache_ts = None

    for row in item_data:
        cache_ts = row.get("cache_timestamp")
        if cache_ts is not None and cache_ts != last_cache_ts:
            flips.append({
                "unix": row.get("timestamp_unix"),
                "cache_ts": cache_ts,
                "cache_ts_prev": last_cache_ts,
            })
            last_cache_ts = cache_ts

    return flips


def compute_edges(flips):
    """For each flip, compute the edge_second (unix_ts % 60)."""
    edges = []
    edge_seconds = []

    for flip in flips:
        unix_ts = flip.get("unix")
        if unix_ts is not None:
            edge_second = unix_ts % 60
            edges.append({
                "unix": unix_ts,
                "edge_second": edge_second,
                "cache_ts": flip.get("cache_ts"),
                "cache_ts_prev": flip.get("cache_ts_prev"),
            })
            edge_seconds.append(edge_second)

    return edges, edge_seconds


def summarize_edges(edge_seconds):
    """Aggregate statistics on edge seconds."""
    if not edge_seconds:
        return {
            "edges": [],
            "edge_frequency": {},
            "total_flips": 0,
            "confidence": "none (no flips detected)",
        }

    freq = defaultdict(int)
    for sec in edge_seconds:
        freq[sec] += 1

    unique_edges = sorted(set(edge_seconds))
    max_freq = max(freq.values())
    min_freq = min(freq.values())
    avg_freq = sum(freq.values()) / len(freq)

    if min_freq >= max_freq * 0.7:
        confidence = "high (edges consistent)"
    elif max_freq >= 3:
        confidence = "medium (primary edges seen multiple times)"
    else:
        confidence = "low (edges sparse)"

    return {
        "edges": unique_edges,
        "edge_frequency": {str(k): v for k, v in sorted(freq.items())},
        "total_flips": len(edge_seconds),
        "min_freq": min_freq,
        "max_freq": max_freq,
        "avg_freq": f"{avg_freq:.2f}",
        "confidence": confidence,
    }


def main():
    parser = argparse.ArgumentParser(description="Analyze market cache edges from probe data")
    parser.add_argument("--probe", type=str, default="market_cache_probe_full.json", help="Probe data file")
    parser.add_argument("--output", type=str, default="cache_edges_analysis.json", help="Output analysis file")
    parser.add_argument("--summary-only", action="store_true", help="Only print console summary (skip JSON output)")

    args = parser.parse_args()

    probe_data = load_probe_data(Path(args.probe))
    print(f"Loaded {len(probe_data)} probe entries")

    # Extract unique items
    item_ids = set()
    item_names = {}
    for row in probe_data:
        item_id = row.get("item_id")
        if item_id is not None:
            item_ids.add(item_id)
            item_names[item_id] = row.get("item_name", f"Item {item_id}")

    item_ids = sorted(item_ids)
    print(f"Found {len(item_ids)} unique items\n")

    summary = {}
    detailed = []

    for item_id in item_ids:
        item_name = item_names[item_id]
        item_data = extract_item_data(probe_data, item_id)

        if not item_data:
            continue

        flips = find_cache_flips(item_data)
        edges, edge_seconds = compute_edges(flips)
        stats = summarize_edges(edge_seconds)

        summary[item_id] = {
            "name": item_name,
            **stats,
        }

        detailed.append({
            "item_id": item_id,
            "name": item_name,
            "total_samples": len(item_data),
            "total_flips": len(flips),
            "flips": edges,
        })

        # Print console output
        edges_str = "{" + ", ".join(map(str, stats["edges"])) + "}"
        print(f"item_{item_id:4d} ({item_name:30s}): edges at {edges_str:20s} | {stats['total_flips']:3d} flips | {stats['confidence']}")

    # Save JSON output if not summary-only
    if not args.summary_only:
        analysis_output = {
            "summary": {k: v for k, v in summary.items()},
            "detailed": detailed,
            "stats": {
                "total_items": len(item_ids),
                "items_with_flips": sum(1 for s in summary.values() if s["total_flips"] > 0),
            },
        }

        with open(args.output, "w", encoding="utf-8") as f:
            json.dump(analysis_output, f, ensure_ascii=False, indent=2)

        print(f"\nDetailed analysis saved to: {args.output}")


if __name__ == "__main__":
    main()
