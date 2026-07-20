"""
Does the `timestamp` request param affect itemmarket cache freshness?

Every INTERVAL seconds, for one item, fire three near-simultaneous reads:
  A) WITH    timestamp=<now>     (what mug-bot currently sends)
  B) WITHOUT timestamp           (control)
  C) WITH    a changing comment  (cache-bust attempt)
and record each response's cache_timestamp + the observed lag (now - cache_timestamp).

If the timestamp param caused stale reads, A's lag would exceed B's. If nothing beats the lag,
the ~30s staleness is Torn's cache architecture and is unavoidable. Pure stdlib (urllib) so it
needs no venv. Single key, ~60 req/min — under the 100/min/key budget.
"""

import os

import json
import time
import urllib.parse
import urllib.request

KEY = os.environ.get("TORN_API_KEY", "")  # was hardcoded; set via env
ITEM = 682
DURATION_SECONDS = 240
INTERVAL_SECONDS = 3
OUT = "timestamp_experiment_results.json"


def call(extra: dict) -> dict:
    params = {**extra, "key": KEY, "limit": 100, "offset": 0}
    url = f"https://api.torn.com/v2/market/{ITEM}/itemmarket?" + urllib.parse.urlencode(params)
    t0 = time.time()
    try:
        req = urllib.request.Request(
            url,
            headers={"User-Agent": "Mozilla/5.0 (compatible; TornExperiment/1.0)"}
        )
        with urllib.request.urlopen(req, timeout=10) as r:
            raw = r.read()
            data = json.loads(raw)
        rtt = round(time.time() - t0, 3)

        if "error" in data:
            return {"err": f"API error {data['error']}", "rtt": round(time.time() - t0, 3)}

        im = data.get("itemmarket") or {}
        return {"cache_ts": im.get("cache_timestamp"), "cache_delay": im.get("cache_delay"),
                "n": len(im.get("listings") or []), "rtt": rtt}
    except Exception as e:
        err = str(e)[:300]
        print(f"EXCEPTION: {err}")
        return {"err": err, "rtt": round(time.time() - t0, 3)}

def lag(now: int, d: dict):
    return (now - d["cache_ts"]) if d.get("cache_ts") else None

COMMENT = "MonChoon Market Tracker"   # mirrors the bot's API_COMMENT
# The four decisive conditions: does the bot's OLD ms+comment differ from the NEW sec+comment, and
# does the comment matter at all vs a bare seconds timestamp / no timestamp?
CONDITIONS = [
    ("ms_comment", lambda now: {"timestamp": now * 1000, "comment": COMMENT}),   # EXACT OLD bot
    ("sec_comment", lambda now: {"timestamp": now, "comment": COMMENT}),         # EXACT NEW bot
    ("sec_only", lambda now: {"timestamp": now}),                                # proven-fresh control
    ("none", lambda now: {}),                                                    # stale baseline
]


def main():
    results = []
    start = time.time()
    i = 0
    while time.time() - start < DURATION_SECONDS:
        now = int(time.time())
        row = {"unix": now}
        for name, build in CONDITIONS:
            row[name] = call(build(now))
        results.append(row)
        print(f"#{i:3d} unix={now} | " + " | ".join(
            f"{name} cache={row[name].get('cache_ts')} lag={lag(now, row[name])}"
            for name, _ in CONDITIONS), flush=True)
        i += 1
        target = i * INTERVAL_SECONDS
        slack = target - (time.time() - start)
        if slack > 0:
            time.sleep(slack)

    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(results, f, indent=2)

    def lags(name):
        return [lag(r["unix"], r[name]) for r in results if lag(r["unix"], r[name]) is not None]

    for name, _ in CONDITIONS:
        ls = lags(name)
        if ls:
            print(f"{name:12s}: n={len(ls)} lag min={min(ls)} max={max(ls)} mean={sum(ls)/len(ls):.1f}")
    # Headline: is the OLD bot (ms_comment) staler than the NEW bot (sec_comment)?
    pairs = [(lag(r["unix"], r["ms_comment"]), lag(r["unix"], r["sec_comment"]))
             for r in results if r["ms_comment"].get("cache_ts") and r["sec_comment"].get("cache_ts")]
    if pairs:
        fresher = sum(1 for m, s in pairs if s < m)
        print(f"sec_comment was FRESHER than ms_comment in {fresher}/{len(pairs)} cycles "
              f"(mean lag improvement {sum(m - s for m, s in pairs) / len(pairs):.1f}s)")
    print(f"saved {len(results)} rows to {OUT}")


if __name__ == "__main__":
    main()
