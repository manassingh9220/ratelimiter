# Implementation Notes

Running log of design decisions and measurements. Numbers here feed the README.

Benchmarks run against a local Redis 8.10 on Apple Silicon, single client,
loopback connection. All figures from `bench_marking.py`.

**Status:** fixed window, sliding window log, and sliding window counter are
implemented and tested. Token bucket and the HTTP layer are next.

---

## Fixed window

**How it works.** One counter per `(user, window)`. The window number is baked
into the key by the caller, derived from `floor(now / window)`, so the key
itself changes at each boundary and old keys expire on their own. `INCR` plus a
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
  unique; scores may repeat. Using the timestamp for both would silently
  collapse same-millisecond requests into one entry. Not hypothetical — in a
  12-request smoke test, 5 requests shared the millisecond `1788845957506`.
  Guarded by `test_same_millisecond_requests_all_counted`.
- **Denied requests are not recorded.** A rejected request neither consumes a
  slot nor extends the window. This bounds worst-case memory; see the memory
  finding below.
- **`PEXPIRE` on every call**, not just the first. Unlike fixed window, this
  window is meant to slide, so the expiry should move with it.

**Boundary behaviour.** The traffic pattern that gets 20 through fixed window
gets exactly 10 through the log. The second batch still falls inside the sliding
view, so all of it is denied.

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
  after an idle period. `test_gap_of_two_or_more_windows_resets_both` is the
  only test that catches its absence.

- **`floor(estimate) < limit`, compared before the increment.** Flooring errs
  very slightly permissive — an estimate of 9.6 counts as 9 used. That is the
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

## Memory

10,000 requests per scenario, measured with `MEMORY USAGE`.

| Scenario | Key memory | Entries | B/entry |
|---|---:|---:|---:|
| Fixed window, any volume | 48 B | 1 | 48 |
| Sliding counter, any volume | 88 B | 3 | 29 |
| Log — 10k reqs over a 3600s window | 397,871 B | 3,600 | 111 |
| Log — 10k reqs in 60s, limit 100k (uncapped) | 1,212,255 B | 10,000 | 121 |
| Log — 10k reqs in 60s, limit 100 (capped) | 4,847 B | 100 | 48 |

Fixed window and the counter are **constant** — 48 B and 88 B no matter how much
traffic arrives. The log's per-entry cost is a 36-character UUID member, an
8-byte double score, and skiplist plus dict overhead.

**Row 3 note.** Only 3,600 of 10,000 entries survived — that loop advanced time
one second per request across a 3600s window, so 6,400 aged out and were pruned.
Memory tracks window occupancy, not lifetime traffic.

### Finding 1: not recording denied requests bounds worst-case memory

Rows 4 and 5 are the same 10,000 requests against the same 60-second window. The
only difference is the limit, and therefore how many were accepted and written.

**4,847 B versus 1,212,255 B — a 250x difference from one design decision.**

Without it, memory scales with request volume, which means the client hammering
hardest is the most expensive to defend against. The rate limiter becomes a
memory amplifier for exactly the traffic it exists to block. With it, worst-case
memory per key is bounded at `limit` entries no matter how much arrives.

At a limit of 100 over a 60s window, an attacker sending a million requests
still costs 4.8 KB.

---

## Latency

2,000 calls per algorithm, after warm-up (the first call sends the full script
body; subsequent calls use `EVALSHA`).

| Algorithm | mean µs | p50 | p95 | p99 | calls/sec |
|---|---:|---:|---:|---:|---:|
| Fixed window | 44 | 43 | 49 | 54 | 22,830 |
| Sliding counter | 47 | 46 | 53 | 61 | 21,386 |
| Sliding log (under limit) | 50 | 48 | 59 | 67 | 20,035 |
| Sliding log (at limit) | 48 | 47 | 56 | 64 | 20,726 |

### Latency vs entries in the window (sliding log)

| Entries | mean µs | p99 µs |
|---:|---:|---:|
| 10 | 51 | 71 |
| 100 | 51 | 71 |
| 1,000 | 50 | 68 |
| 10,000 | 49 | 61 |

### Finding 2: latency is not a differentiator

Flat across three orders of magnitude, and only 6 µs separates the three
algorithms. The sorted set operations are O(log n), but log₂(10,000) = 13 against
log₂(10) = 3 — ten extra pointer hops, invisible next to a ~45 µs round trip.

The 6 µs spread is command count (two versus four Redis calls inside the script),
not data structure. **The common claim that the log is "too slow at scale" does
not hold for realistic set sizes. The choice is memory versus accuracy, not
speed.**

---

## Boundary burst — all three, side by side

Full quota at the last second of a window, then a full quota again at increasing
offsets into the next. Limit 10 per 60s.

| Offset | elapsed | Fixed | Counter | Log |
|---:|---:|---:|---:|---:|
| +2s | 0.02 | 20 | 11 | 10 |
| +10s | 0.15 | 20 | 12 | 10 |
| +20s | 0.32 | 20 | 14 | 10 |
| +30s | 0.48 | 20 | 15 | 10 |
| +45s | 0.73 | 20 | 18 | 10 |
| +59s | 0.97 | 20 | 20 | 10 |

Fixed window jumps straight to 2x regardless of offset — the key changed, so the
old count is simply gone.

The counter releases quota in proportion to `elapsed`, converging on fixed window
only once the previous window has aged out. **That convergence is correct, not a
defect.** By +59s the previous window genuinely has slid out of view and a fresh
quota is legitimately available; the log would allow the same thing within its
own sliding view.

### Finding 3: the right accuracy metric is per sliding window

Summing across two windows is misleading — every algorithm looks permissive that
way, because a full window really has elapsed. Measured as "requests admitted
within any single sliding 60-second view," the counter is within **one request**
of exact.

---

## Steady-state accuracy

5,000 requests at a constant 5 req/s against a limit of 10 per 60s.

| Algorithm | Admitted | vs log |
|---|---:|---:|
| Fixed window | 170 | +0.0% |
| Sliding counter | 170 | +0.0% |
| Sliding log | 170 | +0.0% |

### Finding 4: under sustained overload all three are identical

At 5 req/s against 10/60s the limiter is saturated for the entire run, so every
algorithm simply gates at capacity. The differences only surface with **bursty
traffic near window edges**, which is exactly what the boundary table measures.

Worth stating plainly, because it bounds the practical impact of the choice: if
your traffic is steady, pick fixed window and stop thinking about it.

---

## Three-way comparison

| | Fixed window | Sliding counter | Sliding log |
|---|---|---|---|
| Redis type | String | Hash | Sorted set |
| Keys | 1 per (user, window) | 1 per user | 1 per user |
| Memory | 48 B constant | 88 B constant | ≤ `limit` × 48 B |
| Latency (mean) | 44 µs | 47 µs | 50 µs |
| Boundary burst | 2x limit | limit + 1 | exact |
| Redis round trips | 1 | 1 | 1 |
| Scales with | nothing | nothing | limit, not traffic |

**The counter is the default choice.** 55x less memory than the capped log
(88 B vs 4,847 B) for one extra admitted request at the worst boundary, and
memory that does not grow with the limit at all.

Use the log when the limit must be exact — billing, quota enforcement, anything
where admitting one extra request has a real cost. Use fixed window when traffic
is steady and boundary bursts do not matter.

---

## Next

**Token bucket.** Structurally different from the other three: store current
tokens and a last-refill timestamp, compute the refill lazily on each request
rather than on a timer. Deliberately permits bursts — save tokens while idle,
spend them at once — which is the right behaviour for APIs where occasional
spikes are normal.

Two things to work out:
- Both values must be read and written atomically. A hash again, or a single
  string holding both?
- Tokens are fractional. How is float drift avoided across thousands of requests?

**Then the HTTP layer.** One `POST /check` endpoint dispatching on an
`algorithm` parameter via the `STRATEGIES` dict, returning standard
`X-RateLimit-Limit`, `X-RateLimit-Remaining`, `X-RateLimit-Reset` headers and a
`429` with `Retry-After` when denied.