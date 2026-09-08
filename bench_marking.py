"""
Memory, latency and accuracy benchmarks for the rate limiter algorithms.

Requires a local Redis. Uses db 14 so it never touches dev (db 0) or
test (db 15) data.

    python bench_marking.py
"""

import statistics
import time

import redis

from rate_limiter import (
    allow,
    allow_sliding_counter,
    allow_sliding_log,
    allow_token_bucket,
)

r = redis.Redis(host="localhost", port=6379, db=14, decode_responses=True)

BASE = 1_700_000_000
N = 10_000
TIMING_N = 2_000
LIMIT, WINDOW = 10, 60

# the last second of a window — adding a few seconds crosses into the next one
BOUNDARY = (BASE // WINDOW) * WINDOW + WINDOW - 1

ALGORITHMS = (
    ("fixed", allow),
    ("counter", allow_sliding_counter),
    ("log", allow_sliding_log),
    ("bucket", allow_token_bucket),
)


def entry_count(key: str) -> int:
    """Number of stored items, whatever Redis type the key holds."""
    kind = r.type(key)
    if kind == "zset":
        return r.zcard(key)
    if kind == "hash":
        return r.hlen(key)
    if kind == "list":
        return r.llen(key)
    return 1                                    # string, or anything scalar


# ---------------------------------------------------------------- memory ----

def bench_memory(label, key, fn, user, limit, window, clock):
    r.flushdb()
    for i in range(N):
        fn(r, user, limit, window, now=clock(i))

    mem = r.memory_usage(key)
    if mem is None:
        print(f"{label:<48}{'key not found: ' + key:>31}")
        return

    entries = entry_count(key)
    print(f"{label:<48}{mem:>11,} B{entries:>9,}{mem / entries:>11.0f}")


print(f"\nMEMORY  ({N:,} requests per scenario)")
print(f"{'scenario':<48}{'key size':>13}{'entries':>9}{'B/entry':>11}")
print("-" * 81)

bench_memory(
    "fixed window, any volume",
    f"rl:heavy_fixed:{BASE // 3600}",
    allow, "heavy_fixed", 100_000, 3600, lambda i: BASE,
)
bench_memory(
    "token bucket, any volume",
    "rl:tb:heavy_tb",
    allow_token_bucket, "heavy_tb", 100_000, 3600, lambda i: BASE + i,
)
bench_memory(
    "sliding counter, any volume",
    "rl:swc:heavy_swc",
    allow_sliding_counter, "heavy_swc", 100_000, 3600, lambda i: BASE + i,
)
bench_memory(
    "log, 10k reqs spread over a 3600s window",
    "rl:log:heavy",
    allow_sliding_log, "heavy", 100_000, 3600, lambda i: BASE + i,
)
bench_memory(
    "log, 10k reqs in 60s, limit 100k (uncapped)",
    "rl:log:burst",
    allow_sliding_log, "burst", 100_000, 60, lambda i: BASE + i * 0.001,
)
bench_memory(
    "log, 10k reqs in 60s, limit 100 (capped)",
    "rl:log:burst2",
    allow_sliding_log, "burst2", 100, 60, lambda i: BASE + i * 0.001,
)


# --------------------------------------------------------------- latency ----

def bench_latency(label, fn, user, limit, window):
    r.flushdb()

    for i in range(200):                        # warm up — caches the script sha
        fn(r, user, limit, window, now=BASE + i * 0.001)

    r.flushdb()
    samples = []
    for i in range(TIMING_N):
        t0 = time.perf_counter()
        fn(r, user, limit, window, now=BASE + i * 0.001)
        samples.append((time.perf_counter() - t0) * 1e6)      # microseconds

    samples.sort()
    mean = statistics.mean(samples)
    p50 = samples[len(samples) // 2]
    p95 = samples[int(len(samples) * 0.95)]
    p99 = samples[int(len(samples) * 0.99)]
    print(f"{label:<34}{mean:>9.0f}{p50:>8.0f}{p95:>8.0f}{p99:>8.0f}{1e6 / mean:>12,.0f}")


print(f"\n\nLATENCY  ({TIMING_N:,} calls, single client, localhost)")
print(f"{'algorithm':<34}{'mean µs':>9}{'p50':>8}{'p95':>8}{'p99':>8}{'calls/sec':>12}")
print("-" * 79)

bench_latency("fixed window", allow, "t_fixed", 100_000, 60)
bench_latency("token bucket", allow_token_bucket, "t_tb", 100_000, 60)
bench_latency("sliding counter", allow_sliding_counter, "t_swc", 100_000, 60)
bench_latency("sliding log (under limit)", allow_sliding_log, "t_log", 100_000, 60)
bench_latency("sliding log (at limit, capped)", allow_sliding_log, "t_capped", 100, 60)


# ------------------------------------------------------- latency vs size ----

print("\n\nLATENCY vs ENTRIES IN WINDOW  (sliding log only)")
print(f"{'entries':<14}{'mean µs':>10}{'p99 µs':>10}")
print("-" * 34)

for size in (10, 100, 1_000, 10_000):
    r.flushdb()
    for i in range(size):                       # pre-fill the window
        allow_sliding_log(r, "grow", size + 1_000, 60, now=BASE + i * 0.001)

    samples = []
    for i in range(500):
        t0 = time.perf_counter()
        allow_sliding_log(r, "grow", size + 1_000, 60, now=BASE + 10 + i * 0.001)
        samples.append((time.perf_counter() - t0) * 1e6)

    samples.sort()
    p99 = samples[int(len(samples) * 0.99)]
    print(f"{size:<14,}{statistics.mean(samples):>10.0f}{p99:>10.0f}")


# ------------------------------------------------------- boundary burst ----
# Full quota at the last second of a window, then a full quota again at
# increasing offsets into the next one.

print("\n\nBOUNDARY BURST  (full quota at the last second, then again after)")
print(f"{'offset':<12}{'elapsed':>9}{'fixed':>8}{'counter':>9}{'log':>7}{'bucket':>9}")
print("-" * 54)

for offset in (2, 10, 20, 30, 45, 59):
    totals = {}
    for name, fn in ALGORITHMS:
        r.flushdb()
        first = sum(fn(r, f"a_{name}", LIMIT, WINDOW, now=BOUNDARY)[0]
                    for _ in range(LIMIT))
        second = sum(fn(r, f"a_{name}", LIMIT, WINDOW, now=BOUNDARY + offset)[0]
                     for _ in range(LIMIT))
        totals[name] = first + second

    elapsed = ((BOUNDARY + offset) % WINDOW) / WINDOW
    print(f"{'+' + str(offset) + 's':<12}{elapsed:>9.2f}"
          f"{totals['fixed']:>8}{totals['counter']:>9}"
          f"{totals['log']:>7}{totals['bucket']:>9}")

print("\nfixed jumps straight to 2x regardless of offset — the key changed, so the")
print("old count is simply gone. the counter and the bucket both release quota in")
print("proportion to elapsed time, by completely different mechanisms.")


# ------------------------------------------------------ burst tolerance ----
# Exhaust the limit, go idle for a while, then fire a full quota at once.
# This is the axis the boundary test does not capture: how each algorithm
# RECOVERS, rather than how it behaves at a window edge.

print("\n\nBURST TOLERANCE AFTER IDLE  (exhaust, wait, then fire 10 at once)")
print(f"{'idle':<12}{'fixed':>8}{'counter':>9}{'log':>7}{'bucket':>9}")
print("-" * 45)

for idle in (0, 15, 30, 45, 60, 120):
    row = {}
    for name, fn in ALGORITHMS:
        r.flushdb()
        for _ in range(LIMIT):                              # exhaust the quota
            fn(r, f"b_{name}", LIMIT, WINDOW, now=BASE)
        row[name] = sum(fn(r, f"b_{name}", LIMIT, WINDOW, now=BASE + idle)[0]
                        for _ in range(LIMIT))

    print(f"{str(idle) + 's':<12}{row['fixed']:>8}{row['counter']:>9}"
          f"{row['log']:>7}{row['bucket']:>9}")

print("\nthe log recovers all at once when the oldest entries age out. the counter")
print("and bucket recover gradually. fixed window recovers at its next boundary,")
print("which depends on where BASE happens to fall rather than on the idle time.")


# ------------------------------------------------------ steady-state ------
# Over a long run at a constant rate, how far does each drift from exact?

print(f"\n\nSTEADY-STATE  (5,000 requests at 1 req / 200ms, limit {LIMIT}/{WINDOW}s)")
print(f"{'algorithm':<20}{'admitted':>10}{'vs log':>10}")
print("-" * 40)

admitted = {}
for name, fn in ALGORITHMS:
    r.flushdb()
    admitted[name] = sum(
        fn(r, f"s_{name}", LIMIT, WINDOW, now=BASE + i * 0.2)[0] for i in range(5_000)
    )

exact = admitted["log"]
for name, count in admitted.items():
    drift = f"{(count - exact) / exact * 100:+.1f}%" if exact else "n/a"
    print(f"{name:<20}{count:>10,}{drift:>10}")

print("\nunder sustained overload every algorithm simply gates at capacity — the")
print("differences only surface with bursty traffic near window edges.")


# ------------------------------------------------------ below-limit rate ---
# The saturated case above hides differences. This runs BELOW the limit, where
# the algorithms have room to disagree.

RATE_S = 8.0                                     # 1 request every 8s = 7.5/min
print(f"\n\nBELOW-LIMIT RATE  (1 req / {RATE_S:.0f}s = 7.5/min against {LIMIT}/{WINDOW}s)")
print(f"{'algorithm':<20}{'admitted':>10}{'of':>6}{'denied':>9}")
print("-" * 45)

TOTAL = 500
for name, fn in ALGORITHMS:
    r.flushdb()
    ok = sum(fn(r, f"u_{name}", LIMIT, WINDOW, now=BASE + i * RATE_S)[0]
             for i in range(TOTAL))
    print(f"{name:<20}{ok:>10,}{TOTAL:>6}{TOTAL - ok:>9}")

print("\nall four should admit everything — traffic is comfortably under the limit.")
print("any denials here would be a false positive, which is the worse failure mode.")

r.flushdb()
print()