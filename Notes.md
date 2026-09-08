# Implementation Notes

Running log of design decisions and measurements. Numbers here feed the README.

Benchmarks run against a local Redis 8.10 on Apple Silicon, single client,
loopback connection.

---

## Fixed window

**How it works.** One counter per `(user, window)`. The key embeds a bucket
number derived from `floor(now / window)`, so the key itself changes at each
window boundary and old keys expire on their own. `INCR` plus a conditional
`EXPIRE`, in one Lua script.

**Design decisions**

- **TTL is set only when the counter reaches 1.** Setting it on every call would
  push the expiry forward indefinitely under steady traffic, and the window
  would never close.
- **The bucket comes from the clock, not from first-request time.** Every client
  therefore shares the same window boundaries. That is what makes the boundary
  burst reproducible, and testable.

**Properties**

| | |
|---|---|
| Redis type | String |
| Redis round trips | 1 |
| Keys | 1 per (user, window) |
| Memory | 48 B, constant regardless of volume |
| Accuracy | Allows up to 2x the limit across a boundary |

**Measured flaw.** `test_boundary_burst`: 20 requests allowed within 2 seconds
against a limit of 10 per 60s. A client sending its full quota just before a
reset and again just after gets double.

---

## Sliding window log

**How it works.** One sorted set per user, no bucket. Each accepted request
stores a member scored by its timestamp. On every call: prune everything older
than `now - window`, count what remains, add the new entry if under the limit,
refresh the expiry. Four commands in one Lua script.

**Design decisions**

- **UUID as the member, timestamp as the score.** Sorted set members must be
  unique; scores may repeat. Using the timestamp for both would silently
  collapse same-millisecond requests into a single entry. Not theoretical — in a
  12-request smoke test, 5 requests shared the millisecond `1788845957506`.
  Guarded by `test_same_millisecond_requests_all_counted`.
- **Denied requests are not recorded.** A rejected request neither consumes a
  slot nor extends the window. This turns out to bound worst-case memory; see
  the memory finding below.
- **`PEXPIRE` on every call**, not just the first. Unlike fixed window, this
  window is meant to slide, so the expiry should move with it.

**Properties**

| | |
|---|---|
| Redis type | Sorted set |
| Redis round trips | 1 |
| Keys | 1 per user |
| Memory | ~48 B per stored entry, capped at `limit` entries |
| Accuracy | Exact |

**Boundary behaviour.** The traffic pattern that gets 20 through fixed window
gets exactly 10 through the log. The second batch still falls inside the sliding
view, so all of it is denied.

---

## Memory

10,000 requests per scenario, measured with `MEMORY USAGE`.

| Scenario | Key memory | Entries | B/entry |
|---|---:|---:|---:|
| Fixed window, any volume | 48 B | 1 | 48 |
| Log — 10k reqs spread over a 3600s window | 399,423 B | 3,600 | 111 |
| Log — 10k reqs in 60s, limit 100k (uncapped) | 1,210,623 B | 10,000 | 121 |
| Log — 10k reqs in 60s, limit 100 (capped) | 4,847 B | 100 | 48 |

Per-entry cost is a 36-character UUID member, an 8-byte double score, and
skiplist plus dict overhead. Smaller sets amortise the overhead better, which is
why the capped row is cheaper per entry.

**Row 2.** Only 3,600 of 10,000 entries survived — that loop advanced time one
second per request across a 3600s window, so 6,400 aged out and were pruned.
Memory tracks window occupancy, not lifetime traffic.

### Finding: not recording denied requests bounds worst-case memory

Rows 3 and 4 are the same 10,000 requests against the same 60-second window. The
only difference is the limit, and therefore how many requests were accepted and
written to the set.

**4,847 B versus 1,210,623 B — a 250x difference from one design decision.**

Without it, memory scales with request volume, which means the client hammering
hardest is the most expensive to defend against. The rate limiter becomes a
memory amplifier for exactly the traffic it exists to block. With it, worst-case
memory per key is bounded at `limit` entries no matter how much arrives.

At a limit of 100 over a 60s window, an attacker sending a million requests
still costs 4.8 KB.

---

## Latency

2,000 calls per algorithm, single client, after warm-up (the first call sends the
full script body; subsequent calls use `EVALSHA`).

| Algorithm | mean µs | p50 | p95 | p99 | calls/sec |
|---|---:|---:|---:|---:|---:|
| Fixed window | 44 | 43 | 50 | 57 | 22,617 |
| Sliding log (under limit) | 51 | 49 | 62 | 72 | 19,548 |
| Sliding log (at limit) | 50 | 48 | 60 | 66 | 20,097 |

### Finding: latency does not scale with set size

| Entries in window | mean µs | p99 µs |
|---:|---:|---:|
| 10 | 52 | 76 |
| 100 | 52 | 77 |
| 1,000 | 50 | 66 |
| 10,000 | 50 | 67 |

Flat across three orders of magnitude. The sorted set operations are O(log n),
but log₂(10,000) = 13 against log₂(10) = 3 — ten extra pointer hops, invisible
next to a ~45 µs round trip.

**The sliding window log's cost is memory, not time.** The 7 µs gap versus fixed
window is four Redis commands instead of two, not the data structure. The common
claim that the log is "too slow at scale" does not hold for realistic set sizes;
the real constraint is the memory difference above.

---

## Comparison so far

| | Fixed window | Sliding window log |
|---|---|---|
| Redis type | String | Sorted set |
| Memory per key | 48 B constant | ≤ `limit` × 48 B |
| Latency (mean) | 44 µs | 51 µs |
| Accuracy | 2x burst at boundaries | Exact |
| Redis round trips | 1 | 1 |
| Scales with | Nothing | Limit, not traffic |

Fixed window is 100x cheaper in memory and wrong at the boundaries. The log is
exact and costs proportionally to the limit, at a ~16% latency premium. The
sliding window counter, next, aims for the log's accuracy at fixed window's
memory cost.

---

## Open questions for the next algorithm

Sliding window counter — two counters weighted by position in the window:

```
elapsed  = (now % window) / window
estimate = current + previous * (1 - elapsed)
```

- **Which Redis type?** Both counters must be read and written in one atomic
  operation. A hash, a single string holding both values, or two bucketed keys —
  each has different rollover and expiry behaviour.
- **How is rollover detected?** Compare a stored bucket number against the
  computed one, or let key naming handle it implicitly.
- **The estimate is fractional.** Compare before rounding (slightly strict) or
  floor it first (slightly permissive)? Pick one and say why.
- **What should the boundary test assert?** This is an approximation, so it will
  not land on exactly `LIMIT`. An exact assertion or a tolerance range — and if a
  range, what width is defensible? This decision needs a written justification;
  it is the most interesting property of the algorithm.