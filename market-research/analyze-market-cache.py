import os
import json
from pathlib import Path
from datetime import datetime, timezone, timedelta
import statistics

import requests
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
from matplotlib.lines import Line2D

# ---------- CONFIG ----------

# Manual override (leave as None to use auto window)
TIME_START_UTC = None   # e.g. "2026-06-22T21:03:00"
TIME_END_UTC   = None   # e.g. "2026-06-22T21:08:00"

# Auto window padding (only used if both TIME_START_UTC and TIME_END_UTC are None)
AUTO_PAD_BEFORE_SECONDS = 60    # 1 minute before first sample
AUTO_PAD_AFTER_SECONDS  = 60    # 1 minute after last sample

# Torn API key that can access /user/log for YOUR account
USER_LOG_API_KEY = os.environ.get("USER_LOG_API_KEY", "")  # was hardcoded; set via env

# Item you are testing
ITEM_ID = 682            # EOD Pants
TEST_UID = 9632834426    # your specific test listing UID
TEST_PRICE = 20_000_000_000
PRIMARY_ITEM_ID = 682  # keep in sync with probe script

# Polling JSON files to aggregate (can be globs or explicit list)
POLL_FILES = [
    # "market_presence_682-1.json",
    # "market_presence_682-2.json",
    # "market_presence_682_multi_key.json",
    "market_cache_probe_eod.json"
]

# Where to save raw Torn log fetch (for debugging/inspection)
TORN_LOG_OUTFILE = "torn_logs_raw_new.json"

# User log endpoint + params
USER_LOG_URL = "https://api.torn.com/v2/user/log"

LEGEND_LOC = "upper left"  # e.g. 'upper right', 'lower left', 'best', etc.


# ---------- HELPERS: LOAD / FETCH / PARSE ----------

def _auto_time_window(polls, actions):
    """
    Compute automatic [start, end] window from poll + action timestamps,
    with configurable padding before/after.
    """
    times = [p["ts"] for p in polls]
    times += [a["ts"] for a in actions]

    if not times:
        return None, None

    first = min(times)
    last = max(times)

    start = first - timedelta(seconds=AUTO_PAD_BEFORE_SECONDS)
    end = last + timedelta(seconds=AUTO_PAD_AFTER_SECONDS)

    return start, end

def _parse_time_utc(ts_str):
    """
    Parse an ISO string like '2026-06-22T21:03:00' as UTC.
    Returns datetime or None if ts_str is None.
    """
    if ts_str is None:
        return None
    # allow a trailing 'Z' too
    s = ts_str.rstrip("Z")
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _within_window(ts, start_dt, end_dt):
    if start_dt is not None and ts < start_dt:
        return False
    if end_dt is not None and ts > end_dt:
        return False
    return True

def safe_load_json(path: Path):
    text = path.read_text(encoding="utf-8").strip()
    if not text:
        return []
    if not text.lstrip().startswith("["):
        idx = text.find("[")
        if idx != -1:
            text = text[idx:]
    return json.loads(text)


def load_poll_logs(files):
    """
    Load poll samples for PRIMARY_ITEM_ID (682) from one or more JSON files
    produced by market_cache_probe. Each entry must contain:

      timestamp_utc, timestamp_unix, item_id,
      cache_timestamp, cache_delay, on_market
    """
    rows = []
    for fname in files:
        p = Path(fname)
        if not p.exists():
            print(f"Warning: polling file not found: {p}")
            continue

        data = safe_load_json(p)
        for entry in data:
            if entry.get("item_id") != PRIMARY_ITEM_ID:
                continue
            ts = datetime.fromisoformat(entry["timestamp_utc"])
            rows.append(
                {
                    "ts": ts,
                    "unix": entry["timestamp_unix"],
                    "on_market": bool(entry.get("on_market")),
                    "cache_timestamp": entry.get("cache_timestamp"),
                    "cache_delay": entry.get("cache_delay"),
                    "file": str(p),
                }
            )
    rows.sort(key=lambda r: r["unix"])
    return rows


def fetch_torn_logs():
    """
    Fetch recent item market add/remove logs (1110, 1111) from Torn /user/log
    for your own account, then save to TORN_LOG_OUTFILE and return the JSON.
    """
    params = {
        "log": "1110,1111",
        "cat": 11,
        "limit": 100,
        "key": USER_LOG_API_KEY,
    }
    resp = requests.get(USER_LOG_URL, params=params, timeout=15)
    resp.raise_for_status()
    data = resp.json()
    Path(TORN_LOG_OUTFILE).write_text(json.dumps(data, indent=2), encoding="utf-8")
    print(f"Fetched Torn logs, saved to {TORN_LOG_OUTFILE}")
    return data


def parse_item_actions(log_data, item_id: int, item_uid: int):
    """
    From Torn /user/log payload, extract add/remove events for the given item+uid.

    Log structure:
    {
      "log": [
        {
          "timestamp": 1782162321,
          "details": {"id": 1110, "title": "Item market add", ...},
          "data": {
            "items": [{"id": 682, "uid": 9632834426, "qty": 1}],
            "price": 20000000000,
            ...
          }
        },
        ...
      ]
    }
    """
    actions = []
    for entry in log_data.get("log", []):
        ts_unix = entry.get("timestamp")
        details = entry.get("details", {})
        data = entry.get("data", {})
        detail_id = details.get("id")  # 1110 add, 1111 remove

        items = data.get("items") or []
        if not items:
            continue

        for it in items:
            if it.get("id") == item_id and it.get("uid") == item_uid:
                if detail_id == 1110:
                    action_type = "add"
                elif detail_id == 1111:
                    action_type = "remove"
                else:
                    continue

                ts = datetime.fromtimestamp(ts_unix, tz=timezone.utc)
                actions.append(
                    {
                        "type": action_type,
                        "ts": ts,
                        "unix": ts_unix,
                        "id": entry.get("id"),
                        "price": data.get("price"),
                    }
                )
                break

    actions.sort(key=lambda a: a["unix"])
    print(f"Found {len(actions)} item actions for item {item_id}, uid {item_uid}")
    return actions


def compute_deltas(actions, polls):
    """
    For each add/remove action, find the first poll sample >= action time
    where on_market matches the logical state (True for add, False for remove).
    """
    deltas = []
    for act in actions:
        target = True if act["type"] == "add" else False
        t0 = act["unix"]

        match = None
        for p in polls:
            if p["unix"] >= t0 and p["on_market"] == target:
                match = p
                break

        if not match:
            print(f"No matching poll sample found after {act['type']} at {act['ts']}")
            continue

        delta = match["unix"] - t0
        deltas.append(
            {
                "type": act["type"],
                "action_ts": act["ts"],
                "action_unix": act["unix"],
                "api_ts": match["ts"],
                "api_unix": match["unix"],
                "delta_seconds": delta,
            }
        )

    return deltas


def print_delta_summary(deltas):
    if not deltas:
        print("No deltas computed.")
        return

    adds = [d["delta_seconds"] for d in deltas if d["type"] == "add"]
    rems = [d["delta_seconds"] for d in deltas if d["type"] == "remove"]

    def summarize(name, values):
        if not values:
            print(f"{name}: no events")
            return
        print(f"{name}:")
        print(f"  count = {len(values)}")
        print(f"  min   = {min(values)} s")
        print(f"  max   = {max(values)} s")
        print(f"  mean  = {statistics.mean(values):.2f} s")
        if len(values) > 1:
            print(f"  median= {statistics.median(values):.2f} s")

    print("\n=== Delay summary (Torn action -> API visibility) ===")
    summarize("ADD events", adds)
    summarize("REMOVE events", rems)

    print("\nPer-event detail:")
    for d in deltas:
        print(
            f"  {d['type'].upper():6} | "
            f"action {d['action_ts'].isoformat()}  ->  "
            f"API {d['api_ts'].isoformat()}  |  Δ = {d['delta_seconds']} s"
        )


# ---------- PLOTTING WITH MATPLOTLIB ----------

def build_cache_toggle_series(polls):
    """
    Given poll_rows (sorted by unix), derive a binary cache state that flips
    every time cache_timestamp changes.

    Returns (xs_cache, ys_cache) lists.
    """
    xs = []
    ys = []

    last_cache_ts = None
    state = 0  # start "low"

    for p in polls:
        cache_ts = p.get("cache_timestamp")
        if cache_ts is not None and cache_ts != last_cache_ts:
            # cache timestamp changed -> toggle
            state = 1 - state
            last_cache_ts = cache_ts

        xs.append(p["ts"])
        # put the cache line below the False line but visible
        ys.append(-0.3 if state == 0 else -0.5)

    return xs, ys

def make_plot_matplotlib(polls, actions, deltas):
    if not polls:
        raise SystemExit("No polling data found; nothing to plot.")

    xs = [p["ts"] for p in polls]
    ys = [1 if p["on_market"] else 0 for p in polls]

    # Build cache toggle from cache_timestamp
    xs_cache, ys_cache = build_cache_toggle_series(polls)

    fig, ax = plt.subplots(figsize=(10, 5))

    # --- API presence: blue step line ---
    ax.step(
        xs,
        ys,
        where="post",
        color="blue",
        linewidth=1.5,
        label="API on_market",
        zorder=1,
    )

    # --- Cache toggle: thick black line near bottom ---
    ax.step(
        xs_cache,
        ys_cache,
        where="post",
        color="black",
        linewidth=3.0,
        label="Cache toggle (cache_timestamp)",
        zorder=0,
    )

    # --- Torn logs: red up/down arrows on y-lines ---
    add_ts = [a["ts"] for a in actions if a["type"] == "add"]
    rem_ts = [a["ts"] for a in actions if a["type"] == "remove"]

    if add_ts:
        ax.scatter(
            add_ts,
            [1] * len(add_ts),
            color="red",
            marker="^",
            s=60,
            zorder=3,
        )

    if rem_ts:
        ax.scatter(
            rem_ts,
            [0] * len(rem_ts),
            color="red",
            marker="v",
            s=60,
            zorder=3,
        )

    # --- Delta arrows (unchanged except for ylim tweak) ---
    for d in deltas:
        y = 1 if d["type"] == "add" else 0
        color = "green" if d["type"] == "add" else "purple"

        x0 = d["action_ts"]
        x1 = d["api_ts"]

        ax.annotate(
            "",
            xy=(x1, y),
            xytext=(x0, y),
            arrowprops=dict(
                arrowstyle="<->",
                color=color,
                linewidth=1.2,
            ),
            zorder=4,
        )

        mid_x = x0 + (x1 - x0) / 2
        ax.text(
            mid_x,
            y + (0.06 if d["type"] == "add" else -0.1),
            f"Δ={d['delta_seconds']}s",
            color=color,
            fontsize=8,
            ha="center",
            va="center",
            bbox=dict(
                boxstyle="round,pad=0.2",
                facecolor="white",
                edgecolor=color,
                linewidth=0.8,
                alpha=0.8,
            ),
            zorder=5,
        )

    # --- Axes formatting ---
    ax.set_ylabel("Presence")
    ax.set_yticks([0, 1])
    ax.set_yticklabels(["False", "True"])
    ax.set_ylim(-0.7, 1.3)  # extended down to show cache line

    ax.set_xlabel("Time (UTC)")
    ax.set_title(
        "Torn itemmarket cache behavior\n"
        "Blue: API presence; Red: user logs; Black: cache_timestamp toggle"
    )

    ax.xaxis.set_major_formatter(mdates.DateFormatter("%H:%M:%S"))
    fig.autofmt_xdate()

    ax.grid(True, axis="y", linestyle="--", alpha=0.4)

    # --- Legend including cache line and delta colors ---
    api_handle = Line2D([0], [0], color="blue", linewidth=1.5, label="API on_market")
    cache_handle = Line2D(
        [0],
        [0],
        color="black",
        linewidth=3.0,
        label="Cache toggle (cache_timestamp)",
    )
    log_handle = Line2D(
        [0],
        [0],
        color="red",
        marker="^",
        linestyle="None",
        markersize=7,
        label="Torn log (add/remove)",
    )
    delta_add_handle = Line2D(
        [0], [0], color="green", linewidth=1.2, label="Δ add→rise"
    )
    delta_rem_handle = Line2D(
        [0], [0], color="purple", linewidth=1.2, label="Δ remove→fall"
    )

    ax.legend(
        handles=[api_handle, cache_handle, log_handle, delta_add_handle, delta_rem_handle],
        loc=LEGEND_LOC,
    )

    plt.tight_layout()
    plt.show()
    # fig.savefig("market_vs_logs_with_cache_toggle.png", dpi=150)
# ---------- MAIN ----------

def main():
    # 1) Load polling logs
    poll_rows = load_poll_logs(POLL_FILES)
    print(f"Loaded {len(poll_rows)} polling samples (before time filter).")

    # 2) Fetch and parse Torn logs
    log_data = fetch_torn_logs()
    actions = parse_item_actions(log_data, ITEM_ID, TEST_UID)

    # 3) Decide on time window
    manual_start = _parse_time_utc(TIME_START_UTC)
    manual_end = _parse_time_utc(TIME_END_UTC)

    if manual_start or manual_end:
        # Use manual settings
        start_dt, end_dt = manual_start, manual_end
        print(f"Using manual time window: start={start_dt}, end={end_dt}")
    else:
        # Auto‑detect from data + padding
        start_dt, end_dt = _auto_time_window(poll_rows, actions)
        print(f"Auto time window: start={start_dt}, end={end_dt}")

    if start_dt or end_dt:
        poll_rows = [p for p in poll_rows if _within_window(p["ts"], start_dt, end_dt)]
        actions = [a for a in actions if _within_window(a["ts"], start_dt, end_dt)]
        print(f"After time filter: {len(poll_rows)} polls, {len(actions)} actions.")

    # 4) Compute and print deltas
    deltas = compute_deltas(actions, poll_rows)
    print_delta_summary(deltas)

    # 5) Plot
    make_plot_matplotlib(poll_rows, actions, deltas)


if __name__ == "__main__":
    main()