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
from premise_audit import Observation, Provenance, audit  # noqa: E402

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


def _parse_iso(ts):
    return datetime.fromisoformat(ts.replace("Z", "+00:00"))


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


#: Two estimators of the same percentile, both core to every OpenSearch
#: distribution, neither an extra dependency and neither an extra round trip --
#: they are sub-aggregations of the same request. Which one a dashboard uses is
#: a choice that is almost never written down, and at incident-window sample
#: sizes it changes the number.
PRIMARY_ESTIMATOR = "tdigest"
ALT_ESTIMATOR = "hdr"
HDR_SIGNIFICANT_DIGITS = 3


def _percentile_aggs(metric, percents=(99,)):
    return {
        PRIMARY_ESTIMATOR: {"percentiles": {"field": metric, "percents": list(percents)}},
        ALT_ESTIMATOR: {"percentiles": {"field": metric, "percents": list(percents),
                                        "hdr": {"number_of_significant_value_digits": HDR_SIGNIFICANT_DIGITS}}},
    }


def _read(agg_block, estimator):
    vals = agg_block[estimator]["values"]
    return list(vals.values())[0]


def index_max_timestamp(endpoint, index, time_field):
    """The newest event the index actually holds. `None` when it holds none.

    This is the clock the scan window hangs from. See `resolve_scan_window`.
    """
    body = {"size": 0, "aggs": {"newest": {"max": {"field": time_field}}}}
    res = _get(endpoint, f"/{index}/_search", body)
    epoch_ms = res["aggregations"]["newest"]["value"]
    if epoch_ms is None:
        return None
    return datetime.fromtimestamp(epoch_ms / 1000.0, tz=timezone.utc)


def _floor_to_bucket(dt, bucket_minutes):
    """`fixed_interval` buckets are aligned to the epoch, not to the query range."""
    interval = bucket_minutes * 60
    epoch = dt.timestamp()
    return datetime.fromtimestamp(epoch - (epoch % interval), tz=timezone.utc)


def _ceil_to_bucket(dt, bucket_minutes):
    floored = _floor_to_bucket(dt, bucket_minutes)
    return floored if floored == dt else floored + timedelta(minutes=bucket_minutes)


def resolve_scan_window(endpoint, index, time_field, bucket_minutes, lookback_hours, as_of=None):
    """Fix the scan range to a clock that can be named, and to whole buckets.

    Two defects live in the obvious version of this, `now - lookback .. now`,
    and the evaluation hit both.

    **The wall clock is not a property of the data.** Run the same command
    against the same static index twice, ten minutes apart, and the range moves
    with the typing. Different buckets are compared, and the verdict can change
    with nothing about the system having changed. A tool whose whole position is
    that a premise must be auditable cannot leave its own premise -- *which
    window* -- selected by an input nobody records.

    **An unaligned edge slices a bucket in half.** `now` almost never lands on a
    bucket boundary, so the oldest bucket in range is returned holding whatever
    fraction of its documents happened to fall to the right of the cut. Read
    whole it may be n=200 with a p99 *below* baseline; read sliced it is n=40,
    and `sample_size_collapse` refutes the observation on volume the query
    removed. The collapse is manufactured by the reader.

    So: anchor on `--as-of` when the caller names a moment, otherwise on the
    newest document in the index; then snap both edges outward to bucket
    boundaries, so every bucket returned is queried whole. What remains partial
    is partial in the data, which is a fact about the system and is allowed to
    be judged as one.

    Returns `(gte, lt, anchor, anchor_source)` -- three datetimes and a string
    naming the clock, which travels into the report.
    """
    if as_of:
        anchor = _parse_iso(as_of)
        source = "--as-of, supplied by the caller"
    else:
        anchor = index_max_timestamp(endpoint, index, time_field)
        if anchor is None:
            raise SystemExit(f"{index} holds no `{time_field}` values, so there is no window to scan.")
        source = f"newest `{time_field}` in {index}"
    lt = _ceil_to_bucket(anchor, bucket_minutes)
    gte = _floor_to_bucket(lt - timedelta(hours=lookback_hours), bucket_minutes)
    return gte, lt, anchor, source


def bucket_stats(endpoint, index, time_field, metric, bucket_minutes, gte, lt):
    body = {
        "size": 0,
        "query": _range(time_field, _iso(gte), _iso(lt)),
        "aggs": {
            "per_bucket": {
                "date_histogram": {"field": time_field, "fixed_interval": f"{bucket_minutes}m", "min_doc_count": 1},
                "aggs": _percentile_aggs(metric),
            }
        },
    }
    res = _get(endpoint, f"/{index}/_search", body)
    out = []
    for b in res["aggregations"]["per_bucket"]["buckets"]:
        p99 = _read(b, PRIMARY_ESTIMATOR)
        if p99 is None:
            continue
        alt = _read(b, ALT_ESTIMATOR)
        out.append({"start": b["key_as_string"], "n": b["doc_count"], "p99": round(p99, 2),
                    "p99_alt": round(alt, 2) if alt is not None else None})
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


def build_observation(endpoint, index, metric, time_field, dim, bucket_minutes, lookback_hours,
                      focus_window=None, reported_at=None, as_of=None):
    resolved, metric_types, date_fields = discover_fields(endpoint, index, metric)
    gte, lt, anchor, anchor_source = resolve_scan_window(
        endpoint, index, time_field, bucket_minutes, lookback_hours, as_of)
    buckets = bucket_stats(endpoint, index, time_field, metric, bucket_minutes, gte, lt)
    if len(buckets) < 2:
        raise SystemExit(
            f"Not enough data in {index} between {_iso(gte)} and {_iso(lt)} to compare anything "
            f"({lookback_hours}h ending at {_iso(anchor)}, anchored on {anchor_source})."
        )

    if focus_window:
        # Someone named this window. The audit examines what they claimed.
        target = _parse_iso(focus_window)
        focus = next((b for b in buckets if _parse_iso(b["start"]) == target), None)
        if focus is None:
            raise SystemExit(
                f"No {bucket_minutes}m bucket starts at {focus_window} in the last {lookback_hours}h.\n"
                f"Available bucket starts: {buckets[0]['start']} .. {buckets[-1]['start']}"
            )
        provenance = Provenance.EXTERNAL_REPORT
    else:
        # No window was reported, so this tool picks the worst one -- and says
        # so. Every dataset has a maximum; selecting it and then confirming it
        # is not an artifact would be drawing the target around the arrows.
        focus = max(buckets, key=lambda b: b["p99"])
        provenance = Provenance.SELF_SELECTED

    others = [b for b in buckets if b is not focus]
    baseline_p99 = statistics.median(b["p99"] for b in others)
    baseline_n = int(statistics.median(b["n"] for b in others))

    # The second estimator's baseline is taken the same way as the first, over
    # the same buckets. Mixing methods across focus and baseline would put the
    # divergence between them rather than between the rulers.
    alt_focus = focus.get("p99_alt")
    alt_others = [b["p99_alt"] for b in others if b.get("p99_alt") is not None]
    alt_baseline = statistics.median(alt_others) if alt_others else None
    if alt_focus is None or alt_baseline is None:
        alt_focus = alt_baseline = None

    f_start = focus["start"]
    f_end = _iso(datetime.fromisoformat(f_start.replace("Z", "+00:00")) + timedelta(minutes=bucket_minutes))
    b_start = others[0]["start"]
    b_end = _iso(datetime.fromisoformat(others[-1]["start"].replace("Z", "+00:00")) + timedelta(minutes=bucket_minutes))

    second_clock = next((f for f in date_fields if f != time_field), None)

    return Observation(
        metric=metric,
        focus_value=focus["p99"],
        baseline_value=baseline_p99,
        focus_value_alt=alt_focus,
        baseline_value_alt=alt_baseline,
        estimator=PRIMARY_ESTIMATOR,
        estimator_alt=ALT_ESTIMATOR if alt_focus is not None else None,
        provenance=provenance,
        window_start=f_start,
        window_end=f_end,
        reported_at=reported_at,
        focus_count=focus["n"],
        baseline_typical_count=baseline_n,
        focus_composition=composition(endpoint, index, time_field, dim, f_start, f_end) if dim else None,
        baseline_composition=composition(endpoint, index, time_field, dim, b_start, b_end) if dim else None,
        second_clock_field=second_clock,
        focus_ingest_lag_p50_s=ingest_lag_p50(endpoint, index, time_field, second_clock, f_start, f_end) if second_clock else None,
        baseline_ingest_lag_p50_s=ingest_lag_p50(endpoint, index, time_field, second_clock, b_start, b_end) if second_clock else None,
        resolved_indices=resolved,
        metric_field_types=metric_types,
        scan_window_start=_iso(gte),
        scan_window_end=_iso(lt),
        scan_anchor=_iso(anchor),
        scan_anchor_source=anchor_source,
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
    ap.add_argument("--focus-window", default=None,
                    help="ISO start of the window someone reported, e.g. 2026-08-01T14:20:00Z. "
                         "Without it this tool selects the worst bucket itself and can never "
                         "return SUBSTANTIATED.")
    ap.add_argument("--reported-at", default=None,
                    help="ISO moment the observation was made. An external report without one "
                         "cannot be verified as a report at all.")
    ap.add_argument("--as-of", default=None,
                    help="ISO moment the lookback ends. Without it the window is anchored on the "
                         "newest document in the index -- never on the wall clock, so the same "
                         "command over the same data returns the same window whenever it is run.")
    args = ap.parse_args()

    obs, focus = build_observation(args.endpoint, args.index, args.metric, args.time_field,
                                   args.dimension, args.bucket_minutes, args.lookback_hours,
                                   args.focus_window, args.reported_at, args.as_of)
    report = audit(obs)
    print(f"INDEX: {args.index}    FOCUS WINDOW: {focus['start']} (+{args.bucket_minutes}m)    "
          f"PROVENANCE: {obs.provenance.value}")
    print(report.to_text())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
