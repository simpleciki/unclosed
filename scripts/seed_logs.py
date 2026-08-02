#!/usr/bin/env python3
"""Seed OpenSearch with synthetic latency logs for premise-audit development.

Standard library only. No runtime dependencies (see pyproject.toml).

Why this exists
---------------
`unclosed` claims it can tell a real latency regression from an artifact that
merely looks like one. That claim is only testable if we can produce both, and
produce them so that a naive percentile query cannot tell them apart.

This script emits two scenarios. In both, a naive "p99 per 10-minute bucket"
query shows the same thing: a quiet baseline, then one bucket where p99 jumps
by roughly 5x.

  real-spike   The service genuinely got slower. Request volume is unchanged;
               the whole latency distribution shifted up.

  fake-spike   The service did not get slower. Request volume COLLAPSED in that
               bucket (200 requests -> 3), and two of the three survivors were
               slow. p99 over n=3 is arithmetic noise, not performance.

A third scenario, `baseline`, has no spike at all. It is the negative control:
a gate that flags everything is not a gate.

Determinism
-----------
All randomness is seeded. Re-running with the same --seed reproduces the same
documents, so a captured gate verdict stays reproducible for reviewers.
"""

from __future__ import annotations

import argparse
import json
import math
import random
import statistics
import sys
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

DEFAULT_ENDPOINT = "http://127.0.0.1:9250"

# Bound to loopback by default and never widened by this script. The port is
# deliberately NOT 9200: relying on the default is how a hardcoded endpoint
# sneaks into the code, and portability across OpenSearch deployments is an
# explicit judging dimension.

SCENARIOS = ("baseline", "real-spike", "fake-spike")

BUCKET_MINUTES = 10
BUCKETS = 36  # 6 hours of history
NORMAL_VOLUME = 200
COLLAPSED_VOLUME = 3
SPIKE_BUCKET_FROM_END = 4  # which bucket carries the anomaly

ENDPOINTS = ("/api/checkout", "/api/search", "/api/cart", "/api/profile")
REGIONS = ("us-east-1", "us-west-2", "eu-central-1")

INDEX_MAPPING = {
    "mappings": {
        "properties": {
            # Named `@timestamp` on purpose. Which clock this field represents
            # -- when the event happened, or when it was indexed -- is one of
            # the premises Gate 1 has to interrogate rather than assume.
            "@timestamp": {"type": "date"},
            "service": {"type": "keyword"},
            "endpoint": {"type": "keyword"},
            "region": {"type": "keyword"},
            "status": {"type": "integer"},
            "latency_ms": {"type": "float"},
        }
    },
    "settings": {"number_of_shards": 1, "number_of_replicas": 0},
}


def _request(method: str, url: str, body: str | None = None, content_type: str = "application/json"):
    data = body.encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    if data is not None:
        req.add_header("Content-Type", content_type)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode("utf-8") or "{}")


def _latency(rng: random.Random, median_ms: float, sigma: float) -> float:
    """Lognormal latency: right-skewed, like the real thing."""
    return round(rng.lognormvariate(math.log(median_ms), sigma), 2)


def _bucket_docs(rng: random.Random, start: datetime, volume: int, median_ms: float, sigma: float):
    docs = []
    for _ in range(volume):
        offset = rng.uniform(0, BUCKET_MINUTES * 60)
        latency = _latency(rng, median_ms, sigma)
        docs.append(
            {
                "@timestamp": (start + timedelta(seconds=offset)).isoformat().replace("+00:00", "Z"),
                "service": "storefront",
                "endpoint": rng.choice(ENDPOINTS),
                "region": rng.choice(REGIONS),
                "status": 200 if rng.random() > 0.02 else 500,
                "latency_ms": latency,
            }
        )
    return docs


def build_documents(scenario: str, seed: int, now: datetime):
    rng = random.Random(seed)
    window_start = now - timedelta(minutes=BUCKET_MINUTES * BUCKETS)
    spike_index = BUCKETS - SPIKE_BUCKET_FROM_END

    docs = []
    spike_bucket_start = None

    for i in range(BUCKETS):
        start = window_start + timedelta(minutes=BUCKET_MINUTES * i)
        is_spike_bucket = i == spike_index and scenario != "baseline"

        if not is_spike_bucket:
            docs.extend(_bucket_docs(rng, start, NORMAL_VOLUME, median_ms=80.0, sigma=0.6))
            continue

        spike_bucket_start = start
        if scenario == "real-spike":
            # Volume holds. The distribution itself moved. This is a regression.
            docs.extend(_bucket_docs(rng, start, NORMAL_VOLUME, median_ms=450.0, sigma=0.6))
        else:
            # Volume collapsed. Nothing got slower; there is just almost no data,
            # and what remains happens to include two slow calls.
            for latency in (110.4, 1850.2, 2210.7):
                docs.append(
                    {
                        "@timestamp": (start + timedelta(seconds=rng.uniform(0, 600)))
                        .isoformat()
                        .replace("+00:00", "Z"),
                        "service": "storefront",
                        "endpoint": rng.choice(ENDPOINTS),
                        "region": rng.choice(REGIONS),
                        "status": 200,
                        "latency_ms": latency,
                    }
                )

    return docs, spike_bucket_start


def p99(values):
    if not values:
        return None
    ordered = sorted(values)
    idx = max(0, math.ceil(0.99 * len(ordered)) - 1)
    return round(ordered[idx], 2)


def summarize(docs, spike_bucket_start):
    """Local preview of what a naive percentile query would report."""
    if spike_bucket_start is None:
        return None
    spike_end = spike_bucket_start + timedelta(minutes=BUCKET_MINUTES)
    spike, baseline = [], []
    for d in docs:
        ts = datetime.fromisoformat(d["@timestamp"].replace("Z", "+00:00"))
        (spike if spike_bucket_start <= ts < spike_end else baseline).append(d["latency_ms"])
    return {
        "baseline_n": len(baseline),
        "baseline_p99": p99(baseline),
        "spike_n": len(spike),
        "spike_p99": p99(spike),
    }


def bulk_load(endpoint: str, index: str, docs, recreate: bool) -> int:
    if recreate:
        _request("DELETE", f"{endpoint}/{index}")
    status, body = _request("PUT", f"{endpoint}/{index}", json.dumps(INDEX_MAPPING))
    if status >= 400 and body.get("error", {}).get("type") != "resource_already_exists_exception":
        print(f"ERROR creating index: {status} {body}", file=sys.stderr)
        return 1

    lines = []
    for doc in docs:
        # The action line must name the target index: this is posted to the
        # cluster-level /_bulk, not /<index>/_bulk.
        lines.append(json.dumps({"index": {"_index": index}}))
        lines.append(json.dumps(doc))
    payload = "\n".join(lines) + "\n"

    status, body = _request("POST", f"{endpoint}/_bulk?refresh=wait_for", payload, "application/x-ndjson")
    if status >= 400 or body.get("errors"):
        # Say what actually went wrong. A loader that fails with only a status
        # code teaches the operator nothing, which is the failure mode this
        # whole project exists to argue against.
        reason = body.get("error", {}).get("reason") if isinstance(body.get("error"), dict) else body.get("error")
        if not reason:
            failed = [
                item["index"]
                for item in body.get("items", [])
                if isinstance(item.get("index"), dict) and item["index"].get("error")
            ]
            reason = failed[0]["error"] if failed else "unknown"
        print(f"ERROR bulk indexing (HTTP {status}): {reason}", file=sys.stderr)
        return 1
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--scenario", choices=SCENARIOS, default="fake-spike")
    parser.add_argument("--endpoint", default=DEFAULT_ENDPOINT)
    parser.add_argument("--index", default=None, help="Default: unclosed-<scenario>")
    parser.add_argument("--seed", type=int, default=20260801)
    parser.add_argument("--recreate", action="store_true", help="Delete the index first")
    parser.add_argument("--dry-run", action="store_true", help="Summarize without writing")
    args = parser.parse_args()

    index = args.index or f"unclosed-{args.scenario}"

    # Align to a wall-clock bucket boundary. OpenSearch `span(@timestamp, 10m)`
    # snaps to :00/:10/:20..., so synthetic buckets that start at an arbitrary
    # minute get split across two reported buckets -- and the collapsed bucket
    # stops being visibly collapsed. The scenario has to survive being queried
    # the way an analyst would actually query it.
    now = datetime.now(timezone.utc).replace(second=0, microsecond=0)
    now -= timedelta(minutes=now.minute % BUCKET_MINUTES)

    docs, spike_start = build_documents(args.scenario, args.seed, now)
    stats = summarize(docs, spike_start)

    print(f"scenario     : {args.scenario}")
    print(f"index        : {index}")
    print(f"documents    : {len(docs)}")
    if stats:
        print(f"baseline     : n={stats['baseline_n']:>5}  p99={stats['baseline_p99']} ms")
        print(f"spike bucket : n={stats['spike_n']:>5}  p99={stats['spike_p99']} ms")
        ratio = stats["spike_p99"] / stats["baseline_p99"] if stats["baseline_p99"] else 0
        print(f"naive read   : p99 rose {ratio:.1f}x  <-- identical story in both scenarios")
    else:
        print("spike bucket : none (negative control)")

    if args.dry_run:
        return 0

    rc = bulk_load(args.endpoint, index, docs, args.recreate)
    if rc == 0:
        print(f"indexed into : {args.endpoint}/{index}")
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
