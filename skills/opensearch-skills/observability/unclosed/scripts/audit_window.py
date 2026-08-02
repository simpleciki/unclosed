#!/usr/bin/env python3
"""Fetch the facts a premise audit needs, then run Gate 1 over them.

Retrieval is kept out of `premise_audit.py` on purpose: judgment must be
testable without a cluster, and a probe must not be able to quietly widen its
own evidence by issuing another query while deciding.

Portability note: no inline Painless scripting is used anywhere. Managed
OpenSearch deployments frequently disable it, and the skill has to work across
distributions. Ingest lag is measured by sampling documents and differencing
the two clocks locally -- slower, but it runs everywhere.

Standard library only.
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

sys.path.insert(0, __file__.rsplit("audit_window.py", 1)[0])
from premise_audit import Observation, audit  # noqa: E402

DEFAULT_ENDPOINT = "http://127.0.0.1:9250"
SAMPLE_SIZE = 200


def _get(endpoint, path, body=None):
    url = f"{endpoint}{path}"
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data, method="POST" if data else "GET")
    if data:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raise SystemExit(f"OpenSearch returned {exc.code} for {path}: {exc.read().decode('utf-8')[:300]}")
    except urllib.error.URLError as exc:
        raise SystemExit(f"Cannot reach OpenSearch at {endpoint}: {exc.reason}")


def _range(field, gte, lt):
    return {"range": {field: {"gte": gte, "lt": lt}}}


def _iso(dt):
    return dt.isoformat().replace("+00:00", "Z")


def discover_fields(endpoint, index, metric):
    """Which concrete indices does this pattern resolve to, and what are the clocks?"""
    mapping = _get(endpoint, f"/{index}/_mapping")
    resolved = sorted(mapping.keys())
    metric_types, date_fields = {}, set()
    for name, spec in mapping.items():
        props = spec.get("mappings", {}).get("properties", {})
        metric_types[name] = props.get(metric, {}).get("type")
        date_fields |= {f for f, p in props.items() if p.get("type") == "date"}
    return resolved, metric_types, sorted(date_fields)


def bucket_stats(endpoint, index, time_field, metric, bucket_minutes, lookback_hours):
    now = datetime.now(timezone.utc)
    body = {
        "size": 0,
        "query": _range(time_field, _iso(now - timedelta(hours=lookback_hours)), _iso(now)),
        "aggs": {
            "per_bucket": {
                "date_histogram": {"field": time_field, "fixed_interval": f"{bucket_minutes}m", "min_doc_count": 1},
                "aggs": {"p99": {"percentiles": {"field": metric, "percents": [99]}}},
            }
        },
    }
    res = _get(endpoint, f"/{index}/_search", body)
    out = []
    for b in res["aggregations"]["per_bucket"]["buckets"]:
        p99 = list(b["p99"]["values"].values())[0]
        if p99 is not None:
            out.append({"start": b["key_as_string"], "n": b["doc_count"], "p99": round(p99, 2)})
    return out


def composition(endpoint, index, time_field, dim, gte, lt):
    body = {
        "size": 0,
        "query": _range(time_field, gte, lt),
        "aggs": {"c": {"terms": {"field": dim, "size": 50}}},
    }
    res = _get(endpoint, f"/{index}/_search", body)
    buckets = res["aggregations"]["c"]["buckets"]
    total = sum(b["doc_count"] for b in buckets)
    if not total:
        return None
    return {b["key"]: b["doc_count"] / total for b in buckets}


def ingest_lag_p50(endpoint, index, time_field, second_clock, gte, lt):
    body = {
        "size": SAMPLE_SIZE,
        "query": _range(time_field, gte, lt),
        "_source": [time_field, second_clock],
    }
    res = _get(endpoint, f"/{index}/_search", body)
    lags = []
    for hit in res["hits"]["hits"]:
        src = hit["_source"]
        if time_field not in src or second_clock not in src:
            continue
        t0 = datetime.fromisoformat(src[time_field].replace("Z", "+00:00"))
        t1 = datetime.fromisoformat(src[second_clock].replace("Z", "+00:00"))
        lags.append((t1 - t0).total_seconds())
    return statistics.median(lags) if lags else None


def build_observation(endpoint, index, metric, time_field, dim, bucket_minutes, lookback_hours):
    resolved, metric_types, date_fields = discover_fields(endpoint, index, metric)
    buckets = bucket_stats(endpoint, index, time_field, metric, bucket_minutes, lookback_hours)
    if len(buckets) < 2:
        raise SystemExit(f"Not enough data in {index} over the last {lookback_hours}h to compare anything.")

    focus = max(buckets, key=lambda b: b["p99"])
    others = [b for b in buckets if b is not focus]
    baseline_p99 = statistics.median(b["p99"] for b in others)
    baseline_n = int(statistics.median(b["n"] for b in others))

    f_start = focus["start"]
    f_end = _iso(datetime.fromisoformat(f_start.replace("Z", "+00:00")) + timedelta(minutes=bucket_minutes))
    b_start = others[0]["start"]
    b_end = _iso(datetime.fromisoformat(others[-1]["start"].replace("Z", "+00:00")) + timedelta(minutes=bucket_minutes))

    second_clock = next((f for f in date_fields if f != time_field), None)

    return Observation(
        metric=metric,
        focus_value=focus["p99"],
        baseline_value=baseline_p99,
        focus_count=focus["n"],
        baseline_typical_count=baseline_n,
        focus_composition=composition(endpoint, index, time_field, dim, f_start, f_end) if dim else None,
        baseline_composition=composition(endpoint, index, time_field, dim, b_start, b_end) if dim else None,
        second_clock_field=second_clock,
        focus_ingest_lag_p50_s=ingest_lag_p50(endpoint, index, time_field, second_clock, f_start, f_end) if second_clock else None,
        baseline_ingest_lag_p50_s=ingest_lag_p50(endpoint, index, time_field, second_clock, b_start, b_end) if second_clock else None,
        resolved_indices=resolved,
        metric_field_types=metric_types,
    ), focus


def main() -> int:
    ap = argparse.ArgumentParser(description="Run Gate 1 (premise audit) over an OpenSearch index.")
    ap.add_argument("--index", required=True)
    ap.add_argument("--endpoint", default=DEFAULT_ENDPOINT)
    ap.add_argument("--metric", default="latency_ms")
    ap.add_argument("--time-field", default="@timestamp")
    ap.add_argument("--dimension", default="endpoint", help="Categorical field for the population check")
    ap.add_argument("--bucket-minutes", type=int, default=10)
    ap.add_argument("--lookback-hours", type=int, default=6)
    args = ap.parse_args()

    obs, focus = build_observation(args.endpoint, args.index, args.metric, args.time_field,
                                   args.dimension, args.bucket_minutes, args.lookback_hours)
    report = audit(obs)
    print(f"INDEX: {args.index}    FOCUS WINDOW: {focus['start']} (+{args.bucket_minutes}m)")
    print(report.to_text())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
