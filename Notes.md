# Implementation Notes

Running log of design decisions and measurements. Numbers here feed the README.

Benchmarks run against a local Redis 8.10 on Apple Silicon, single client,
loopback connection. All figures from `bench_marking.py`.

**Status:** all four algorithms implemented, tested, and benchmarked. The HTTP
layer, Docker Compose, and the README are what remain.

---

## Fixed window

**How it works.** One counter per `(user, window)`. The window number is baked
into the key by the caller, derived from `floor(now / window)`, so the key itself
changes at each boundary and old keys expire on their own. `INCR` plus a
conditional `EXPIRE`, in one Lua script.

**Design decisions**

- **TTL is set only when the counter reaches 1.** Setting it every call would
  push the expiry forward on every request, so steady traffic would keep the
  window alive indefinitely and the counter would never reset.
- **The bucket comes from the clock, not from first-request time.** Every client
  therefore shares the same window boundaries. That is what makes the boundary
  burst reproducible, and testable.

**Measured flaw.** `test_boundary_burst`: 20 requests admitted within 2 seconds
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
  unique; scores may repeat. Using the timestamp for both would silently collapse
  same-millisecond requests into one entry. Not hypothetical — in a 12-request
  smoke test, 5 requests shared the millisecond `1788845957506`. Guarded by
  `test_same_millisecond_requests_all_counted`.
- **Denied requests are not recorded.** A rejected request neither consumes a slot
  nor extends the window. This bounds worst-case memory; see Finding 1.
- **`PEXPIRE` on every call**, not just the first. Unlike fixed window, this
  window is meant to slide, so the expiry should move with it.

**Boundary behaviour.** The traffic pattern that gets 20 through fixed window gets
exactly 10 through the log. The second batch still falls inside the sliding view,
so all of it is denied.

---

## Sliding window counter

**How it works.** Two integers in a hash — the count in the current fixed window
and the count in the previous one — with the previous weighted by how much of it
still falls inside the sliding view.

```
elapsed  = (now % window) / window          -- 0.0 .. 1.0 through this window
estimate = current + previous * (1 - elapsed)
```

25% into the current minute means 75% of the previous minute is still within the
last 60 seconds, so 75% of its count is charged against you.

**Design decisions**

- **A hash, not two bucketed keys.** One key per user keeps it cluster-safe
  without hash tags, and makes rollover explicit rather than implicit in key
  naming. The cost is having to detect rollover in the script.
- **Three rollover cases**, and the third is the one that gets missed:

  | Stored bucket | Meaning | Action |
  |---|---|---|
  | `== bucket` | same window | keep both counters |
  | `== bucket - 1` | rolled by one | current becomes previous, current resets |
  | anything else | cold key, or idle 2+ windows | reset both to zero |

  Without the third case, counts from long ago get weighted into the estimate
  after an idle period. `test_gap_of_two_or_more_windows_resets_both` is the only
  test that catches its absence.

- **`floor(estimate) < limit`, compared before the increment.** Flooring errs
  slightly permissive — an estimate of 9.6 counts as 9 used. That is the
  conventional choice: rejecting a legitimate request is worse than occasionally
  admitting one extra.
- **TTL of two windows, not one.** `previous` has to survive into the current
  window to be usable; a one-window TTL would delete the key exactly when the
  previous count starts mattering.

**The approximation.** The formula assumes the previous window's requests were
spread evenly. When they were bunched at its end, the estimate undercounts and
slightly more than `limit` slips through. Bounded by `previous * elapsed`, which
is largest right after a rollover — and right after a rollover `elapsed` is near
zero, so in practice the slack is one request.

---

## Token bucket

**How it works.** Tokens accrue continuously at `limit / window` per second up to
a capacity of `limit`. Each request spends one. Refill is computed lazily from
elapsed time on each call — no timer, no background job, no windows at all.

**Design decisions**

- **A cold key starts full**, not empty. A first-time client should get its full
  burst allowance, not be throttled by a limiter it has never touched.
- **Fractional tokens are stored**, formatted with `%.17g` so the remainder
  round-trips exactly through the string form Redis stores it in. Drift does not
  accumulate, because tokens are recomputed from `last_refill` each call rather
  than incremented repeatedly.
- **Capacity is tied to `limit`.** They are conceptually independent — capacity
  controls burst size, rate controls sustained throughput — but tying them keeps
  the signature uniform with the other three.
- **TTL is `capacity / rate`**, the time a full refill takes. Past that, the
  stored state is indistinguishable from a cold key.
- **`elapsed` is clamped at zero.** A clock going backwards must not drain the
  bucket.

**Bug found during implementation.** The wrapper passed `rate = limit / window_ms`
(tokens per millisecond) while the script divided by 1000 again, treating it as
tokens per second. The result was a refill rate 1000x too slow — six seconds of
idle accrued 0.001 tokens instead of 1, so the bucket never refilled at all. The
first twelve requests behaved correctly, which made it look like a storage
problem rather than an arithmetic one. Units at a language boundary are worth
asserting, not assuming.

---

## Memory

10,000 requests per scenario, measured with `MEMORY USAGE`.

| Scenario | Key memory | Entries | B/entry |
|---|---:|---:|---:|
| Fixed window, any volume | 48 B | 1 | 48 |
| Token bucket, any volume | 84 B | 2 | 42 |
| Sliding counter, any volume | 88 B | 3 | 29 |
| Log — 10k reqs over a 3600s window | 397,839 B | 3,600 | 111 |
| Log — 10k reqs in 60s, limit 100k (uncapped) | 1,211,647 B | 10,000 | 121 |
| Log — 10k reqs in 60s, limit 100 (capped) | 4,847 B | 100 | 48 |

Three of the four are **constant** — 48, 84, and 88 bytes no matter how much
traffic arrives. Only the log grows, and only with the limit. Its per-entry cost
is a 36-character UUID member, an 8-byte double score, and skiplist plus dict
overhead.

**Row 4 note.** Only 3,600 of 10,000 entries survived — that loop advanced time
one second per request across a 3600s window, so 6,400 aged out and were pruned.
Memory tracks window occupancy, not lifetime traffic.

### Finding 1: not recording denied requests bounds worst-case memory

Rows 5 and 6 are the same 10,000 requests against the same 60-second window. The
only difference is the limit, and therefore how many were accepted and written.

**4,847 B versus 1,211,647 B — a 250x difference from one design decision.**

Without it, memory scales with request volume, which means the client hammering
hardest is the most expensive to defend against. The rate limiter becomes a memory
amplifier for exactly the traffic it exists to block. With it, worst-case memory
per key is bounded at `limit` entries no matter how much arrives.

At a limit of 100 over a 60s window, an attacker sending a million requests still
costs 4.8 KB.

---

## Latency

2,000 calls per algorithm, after warm-up (the first call sends the full script
body; subsequent calls use `EVALSHA`).

| Algorithm | mean µs | p50 | p95 | p99 | calls/sec |
|---|---:|---:|---:|---:|---:|
| Fixed window | 44 | 43 | 49 | 54 | 22,980 |
| Token bucket | 47 | 46 | 54 | 62 | 21,203 |
| Sliding counter | 47 | 46 | 53 | 63 | 21,304 |
| Sliding log (under limit) | 50 | 48 | 56 | 65 | 20,197 |
| Sliding log (at limit) | 48 | 47 | 53 | 61 | 20,894 |

### Latency vs entries in the window (sliding log)

| Entries | mean µs | p99 µs |
|---:|---:|---:|
| 10 | 51 | 70 |
| 100 | 52 | 86 |
| 1,000 | 50 | 62 |
| 10,000 | 49 | 61 |

### Finding 2: latency is not a differentiator

Flat across three orders of magnitude, and only 6 µs separates the four
algorithms. The sorted set operations are O(log n), but log₂(10,000) = 13 against
log₂(10) = 3 — ten extra pointer hops, invisible next to a ~45 µs round trip.

The 6 µs spread is command count (two versus four Redis calls inside the script),
not data structure. **The common claim that the log is "too slow at scale" does
not hold for realistic set sizes. The choice is memory and behaviour, not speed.**

---

## Boundary burst

Full quota at the last second of a window, then a full quota again at increasing
offsets into the next. Limit 10 per 60s.

| Offset | elapsed | Fixed | Counter | Log | Bucket |
|---:|---:|---:|---:|---:|---:|
| +2s | 0.02 | 20 | 11 | 10 | 10 |
| +10s | 0.15 | 20 | 12 | 10 | 11 |
| +20s | 0.32 | 20 | 14 | 10 | 13 |
| +30s | 0.48 | 20 | 15 | 10 | 15 |
| +45s | 0.73 | 20 | 18 | 10 | 17 |
| +59s | 0.97 | 20 | 20 | 10 | 19 |

Fixed window jumps straight to 2x regardless of offset — the key changed, so the
old count is simply gone.

The counter and the bucket track each other closely despite working by completely
different mechanisms: one weights a previous-window counter, the other accrues
tokens continuously. Both end up releasing quota in proportion to elapsed time.

### Finding 3: the right accuracy metric is per sliding window

Summing across two windows is misleading — every algorithm looks permissive that
way, because a full window really has elapsed. Measured as "requests admitted
within any single sliding 60-second view," the counter is within **one request**
of exact.

---

## Recovery after idle

Exhaust the limit, wait, then fire a full quota at once. This is the axis the
boundary table does not capture: how each algorithm *recovers*, rather than how
it *fails*.

| Idle | Fixed | Counter | Log | Bucket |
|---:|---:|---:|---:|---:|
| 0s | 0 | 0 | 0 | 0 |
| 15s | 0 | 0 | 0 | 2 |
| 30s | 0 | 0 | 0 | 5 |
| 45s | 10 | 1 | 0 | 7 |
| 60s | 10 | 4 | 10 | 10 |
| 120s | 10 | 10 | 10 | 10 |

### Finding 4: four mechanisms, four completely different recovery curves

- **Log — all or nothing.** Every entry ages out at the same instant, because
  they were all written at the same instant. Recovery is a step function at
  exactly `window`.
- **Bucket — perfectly linear.** 2, 5, 7 tokens at 15s, 30s, 45s matches
  `idle × 0.167 tokens/sec` to the token. Continuous accrual, no quantisation.
- **Counter — lagged, then catches up.** Nothing recovers until a window
  rollover, after which `previous` decays.
- **Fixed — recovers at the clock boundary, not after the idle.** `BASE % 60 = 20`
  puts the next boundary at +40s, which is why 45s recovers fully and 30s does
  not. Recovery depends on where you are in the clock rather than how long you
  waited. The boundary flaw, seen from another angle.

---

## Steady state

5,000 requests at a constant 5 req/s against a limit of 10 per 60s.

| Algorithm | Admitted | vs log |
|---|---:|---:|
| Fixed window | 170 | +0.0% |
| Sliding counter | 170 | +0.0% |
| Sliding log | 170 | — |
| Token bucket | 176 | +3.5% |

### Finding 5: window algorithms discard the partial window; the bucket does not

The 176 is derivable exactly:

```
5,000 requests x 0.2s      = 1,000 seconds elapsed
starts full                =    10 tokens
accrues 1,000 x (10/60)    = 166.67 tokens
                             ─────────────
                             176.67 -> 176 admitted
```

Window-based algorithms quantise to whole windows — 1,000 seconds spans about 17
boundaries at 10 each, giving 170 — and discard whatever partial window is left
at the end. The bucket accrues continuously and captures it.

**The 6-request gap is the window algorithms slightly under-admitting, not the
bucket over-admitting.** Worth knowing if you care about not throttling
legitimate traffic.

### Below-limit sanity check

500 requests at 7.5/min against a limit of 10/60s — comfortably under. All four
admitted 500 of 500, zero denials. Any false positive here would be the worse
failure mode: rejecting legitimate traffic is more damaging than occasionally
admitting one extra.

---

## Four-way comparison

| | Fixed window | Sliding counter | Sliding log | Token bucket |
|---|---|---|---|---|
| Redis type | String | Hash | Sorted set | Hash |
| Keys | 1 per (user, window) | 1 per user | 1 per user | 1 per user |
| Memory | 48 B constant | 88 B constant | ≤ `limit` × 48 B | 84 B constant |
| Latency (mean) | 44 µs | 47 µs | 50 µs | 47 µs |
| Boundary burst | 2x limit | limit + 1 | exact | exact |
| Recovery | at clock boundary | lagged | step at `window` | linear |
| Permits bursts | accidentally | no | no | **by design** |
| Redis round trips | 1 | 1 | 1 | 1 |

The last row is the real distinction. Fixed window's burst is a bug. The token
bucket's is a feature.

### Which to use

**Token bucket** for general API rate limiting. Constant memory, continuous
recovery, and a deliberate burst allowance that matches how real clients behave —
idle for a while, then a flurry. This is why it is the most common choice in
practice.

**Sliding window counter** when bursts must be suppressed rather than permitted.
Same constant memory, but quota is released gradually instead of banked.

**Sliding window log** when the limit must be exact — billing, quota enforcement,
anything where admitting one extra request has a real cost. Pay for it in memory
proportional to the limit.

**Fixed window** when traffic is steady and boundary bursts do not matter. It is
the cheapest, and Finding 5 shows that under sustained load it is
indistinguishable from the others.

---

## Next

**HTTP layer.** One `POST /check` endpoint dispatching on an `algorithm`
parameter via the `STRATEGIES` dict, returning the conventional
`X-RateLimit-Limit`, `X-RateLimit-Remaining`, `X-RateLimit-Reset` headers and a
`429` with `Retry-After` when denied.

Open question: none of the four scripts currently computes `reset_at` or
`retry_after`. Each algorithm knows when quota returns — fixed window at the next
bucket boundary, the log when its oldest entry ages out, the counter
proportionally, the bucket when one token accrues. Compute it in Lua (accurate,
more script surface) or approximate in Python (simpler)? Approximating first, and
noting it as a limitation.

**Then** `REDIS_URL` from the environment instead of a hardcoded host, Docker
Compose, and the README.