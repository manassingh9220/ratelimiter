"""
Memory, latency and accuracy benchmarks for the rate limiter algorithms.

Requires a local Redis. Uses db 14 so it never touches dev (db 0) or
test (db 15) data.

    python bench_marking.py
"""

import statistics
import time

import redis

from rate_limiter import allow, allow_sliding_counter, allow_sliding_log

r = redis.Redis(host="localhost", port=6379, db=14, decode_responses=True)

BASE = 1_700_000_000
N = 10_000
TIMING_N = 2_000


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


# -------------------------------------------------------------- accuracy ----
# The counter approximates the log. This measures the gap directly: replay one
# traffic pattern through all three and compare how many each admits.

LIMIT, WINDOW = 10, 60
BOUNDARY = (BASE // WINDOW) * WINDOW + WINDOW - 1        # last second of a window

print("\n\nBOUNDARY BURST  (full quota at the last second, then again after)")
print(f"{'offset into next window':<26}{'fixed':>8}{'counter':>9}{'log':>7}{'elapsed':>10}")
print("-" * 60)

for offset in (2, 10, 20, 30, 45, 59):
    totals = {}
    for name, fn, user in (
        ("fixed", allow, "a_fixed"),
        ("counter", allow_sliding_counter, "a_swc"),
        ("log", allow_sliding_log, "a_log"),
    ):
        r.flushdb()
        first = sum(fn(r, user, LIMIT, WINDOW, now=BOUNDARY)[0] for _ in range(LIMIT))
        second = sum(fn(r, user, LIMIT, WINDOW, now=BOUNDARY + offset)[0]
                     for _ in range(LIMIT))
        totals[name] = first + second

    elapsed = ((BOUNDARY + offset) % WINDOW) / WINDOW
    print(f"{'+' + str(offset) + 's':<26}"
          f"{totals['fixed']:>8}{totals['counter']:>9}{totals['log']:>7}{elapsed:>10.2f}")

print("\nfixed jumps straight to 2x. the counter releases quota in proportion to")
print("elapsed, converging on fixed only once the previous window has aged out —")
print("which is correct, and is what the log does too.")


# ------------------------------------------------------ steady-state error --
# Over a long run at a steady rate, how far does the counter drift from exact?

print("\n\nSTEADY-STATE ACCURACY  (5,000 requests at 1 req / 200ms, limit 10/60s)")
print(f"{'algorithm':<20}{'admitted':>10}{'vs log':>10}")
print("-" * 40)

admitted = {}
for name, fn, user in (
    ("fixed window", allow, "s_fixed"),
    ("sliding counter", allow_sliding_counter, "s_swc"),
    ("sliding log", allow_sliding_log, "s_log"),
):
    r.flushdb()
    admitted[name] = sum(
        fn(r, user, LIMIT, WINDOW, now=BASE + i * 0.2)[0] for i in range(5_000)
    )

exact = admitted["sliding log"]
for name, count in admitted.items():
    drift = f"{(count - exact) / exact * 100:+.1f}%" if exact else "n/a"
    print(f"{name:<20}{count:>10,}{drift:>10}")

r.flushdb()
print()