# Rate Limiter

Four rate limiting algorithms behind one API, backed by Redis, with the counter
updates done atomically in Lua so concurrent requests cannot race.

The point of the project is not the algorithms — they are well documented
everywhere. It is measuring what actually separates them, and finding that the
usual framing is wrong.

---

## Quick start

```bash
docker compose up
```

Or without Docker, against a local Redis:

```bash
pip install -r requirements.txt
uvicorn app:app --reload
```

```bash
curl -i -X POST localhost:8000/check \
  -H 'Content-Type: application/json' \
  -d '{"key": "user-123", "limit": 100, "window": 60, "algorithm": "token_bucket"}'
```

```
HTTP/1.1 200 OK
x-ratelimit-limit: 100
x-ratelimit-remaining: 99
x-ratelimit-reset: 1788857940

{"allowed": true, "remaining": 99, "limit": 100,
 "reset_at": 1788857940, "retry_after": null, "algorithm": "token_bucket"}
```

Denied requests return `429` with a `Retry-After` header. Interactive docs at
`/docs`.

---

## The problem every rate limiter has

Deciding whether to allow a request is a read-modify-write cycle: read the
current count, decide, write the new count. Split across three round trips from
application code, two clients interleave between the read and the write, both see
the same count, and both admit a request that should have been refused.

```
client A: read counter -> 9        client B: read counter -> 9
client A: 9 < 10, allow            client B: 9 < 10, allow
client A: write 10                 client B: write 10
```

Two requests admitted against a limit with room for one, and the counter says 10
when it should say 11.

Every algorithm here solves it the same way: the entire decision runs inside a
Lua script. Redis executes scripts atomically — nothing else runs on its command
thread for the duration — so the gap between read and write does not exist.

---

## The four algorithms

| | Fixed window | Sliding counter | Sliding log | Token bucket |
|---|---|---|---|---|
| Redis type | String | Hash | Sorted set | Hash |
| Memory per key | 48 B | 88 B | ≤ limit × 48 B | 84 B |
| Latency (mean) | 44 µs | 47 µs | 50 µs | 47 µs |
| Boundary burst | **2× limit** | limit + 1 | exact | exact |
| Recovery after idle | at clock boundary | lagged | step at `window` | linear |
| Permits bursts | accidentally | no | no | **by design** |
| Redis round trips | 1 | 1 | 1 | 1 |

**Fixed window** counts requests per clock-aligned window. One integer, cheapest
possible, and wrong at the edges — see below.

**Sliding window log** stores a timestamp per accepted request and looks back
exactly `window` from this instant. Exact, and pays for it in memory.

**Sliding window counter** keeps two integers — this window's count and the
previous one's — weighting the previous by how much of it still falls inside the
sliding view. Approximates the log at fixed window's memory cost.

**Token bucket** accrues tokens continuously at `limit / window` per second up to
a capacity of `limit`. No windows at all. Deliberately permits bursts.

---

## What the benchmarks actually showed

Full numbers and methodology in [NOTES.md](NOTES.md).

### Fixed window admits 2× the limit, and that is the whole motivation

Send the full quota at the last second of a window, then the full quota again two
seconds later:

| Offset into next window | Fixed | Counter | Log | Bucket |
|---:|---:|---:|---:|---:|
| +2s | **20** | 11 | 10 | 10 |
| +10s | **20** | 12 | 10 | 11 |
| +30s | **20** | 15 | 10 | 15 |
| +59s | 20 | 20 | 10 | 19 |

Limit of 10 per 60s. Fixed window jumps to 20 regardless of offset — the key
changed, so the old count is simply gone.

The counter and the bucket converge on 20 by +59s, and that is correct rather
than a defect: a full window really has elapsed, so a fresh quota is legitimately
available. `tests/test_fixed_window.py::test_boundary_burst` documents the flaw;
`tests/test_sliding_log.py` proves the log eliminates it.

### Latency is not a differentiator — 6 µs separates all four

The received wisdom is that the sliding log is too slow because sorted set
operations are O(log n). Measured against set size:

| Entries in window | mean µs | p99 µs |
|---:|---:|---:|
| 10 | 51 | 70 |
| 100 | 52 | 86 |
| 1,000 | 50 | 62 |
| 10,000 | 49 | 61 |

Flat across three orders of magnitude. log₂(10,000) = 13 against log₂(10) = 3 —
ten extra pointer hops, invisible next to a ~45 µs round trip. The 6 µs spread
between algorithms is command count, not data structure.

**The choice is memory and behaviour, not speed.**

### One design decision cut worst-case memory 250×

The log does not write denied requests to the sorted set. Same 10,000 requests
against the same 60-second window, differing only in the limit:

| Limit | Key memory | Entries stored |
|---:|---:|---:|
| 100,000 (nothing denied) | 1,211,647 B | 10,000 |
| 100 (denials dropped) | **4,847 B** | 100 |

Without that choice, memory scales with request volume — the client hammering
hardest becomes the most expensive to defend against, and the rate limiter turns
into a memory amplifier for exactly the traffic it exists to block.

With it, worst-case memory per key is bounded at `limit` entries. An attacker
sending a million requests against a limit of 100 still costs 4.8 KB.

### Four mechanisms, four completely different recovery curves

Exhaust the limit, idle, then fire a full quota at once:

| Idle | Fixed | Counter | Log | Bucket |
|---:|---:|---:|---:|---:|
| 0s | 0 | 0 | 0 | 0 |
| 15s | 0 | 0 | 0 | 2 |
| 30s | 0 | 0 | 0 | 5 |
| 45s | **10** | 1 | 0 | 7 |
| 60s | 10 | 4 | **10** | 10 |

- **Log** is a step function — every entry ages out at the same instant, because
  they were all written at the same instant.
- **Bucket** is perfectly linear: 2, 5, 7 tokens matches `idle × 0.167 tokens/s`
  exactly.
- **Counter** lags until a window rollover, then decays.
- **Fixed** recovers at 45s but not 30s — not because of the idle duration, but
  because the clock boundary happened to fall at +40s. Its recovery depends on
  where you are in the clock rather than how long you waited.

The boundary table measures how an algorithm *fails*. This measures how it
*recovers*, and the four are far more different on that axis.

### Token bucket admits 3.5% more, and it is right to

5,000 requests at a steady 5/s against a limit of 10 per 60s:

| Algorithm | Admitted |
|---|---:|
| Fixed / counter / log | 170 |
| Token bucket | **176** |

Derivable exactly — 1,000 seconds elapsed, starting full (10) plus
1,000 × 10/60 = 166.67 accrued, giving 176.67.

Window-based algorithms quantise to whole windows and discard the partial one at
the end. The bucket accrues continuously and captures it. The six-request gap is
the window algorithms slightly **under**-admitting.

---

## Which to use

**Token bucket** for general API limiting. Constant memory, continuous recovery,
and a burst allowance that matches how real clients behave — idle for a while,
then a flurry. This is why it is the most common choice in practice.

**Sliding window counter** when bursts must be smoothed rather than banked. Same
constant memory, quota released gradually instead of saved up.

**Sliding window log** when the limit must be exact — billing, quota enforcement,
anything where one extra admitted request has a real cost. Pay in memory
proportional to the limit.

**Fixed window** when traffic is steady and boundary bursts do not matter. Under
sustained overload all four admit identical counts, so the cheapest option wins.

---

## Design decisions

The parts that look arbitrary until you know what they prevent.

**The log's sorted set members are UUIDs, not timestamps.** Members must be
unique; scores may repeat. Using the timestamp for both would make the second
request in a given millisecond overwrite the first instead of adding to it,
silently undercounting. Not hypothetical — a 12-request smoke test had 5 requests
share one millisecond. Guarded by
`test_same_millisecond_requests_all_counted`.

**The counter handles three rollover cases, not two.** Same window, rolled by
one, and *everything else*. The third covers a cold key and an idle period longer
than two windows; without it, counts from ten minutes ago get weighted into the
current estimate. `test_gap_of_two_or_more_windows_resets_both` is the only test
that catches its absence.

**The counter's TTL is two windows, not one.** `previous` has to survive into the
current window to be usable; a one-window TTL would delete the key exactly when
the previous count starts mattering.

**A cold token bucket starts full, not empty.** A first-time client should get its
full burst allowance rather than being throttled by a limiter it has never
touched.

**Fixed window sets its TTL only on the first increment.** Setting it every call
would push the expiry forward on every request, so steady traffic would keep the
window alive indefinitely and the counter would never reset.

---

## Testing

```bash
createdb-equivalent: none needed — tests use Redis db 15
pytest tests/ -v
```

Tests are organised around what each one catches rather than by module. The ones
that carry weight:

| Test | What it proves |
|---|---|
| `test_boundary_burst` | Fixed window admits 2× the limit across a reset |
| `test_sliding_log_prevents_boundary_burst` | The log caps it at exactly the limit |
| `test_counter_sits_between_fixed_and_log_at_the_boundary` | All three, same traffic, one assertion |
| `test_same_millisecond_requests_all_counted` | UUID members are load-bearing |
| `test_gap_of_two_or_more_windows_resets_both` | The rollover case that gets missed |
| `test_recovery_is_proportional_to_elapsed` | The counter's recovery curve, not just one point |

Benchmarks:

```bash
python bench_marking.py
```

Runs against Redis db 14. Prints memory, latency, latency-vs-set-size, boundary
burst, recovery after idle, steady state, and a below-limit sanity check.

---

## API

```
POST /check      { key, limit, window, algorithm }  ->  200 or 429
GET  /health     Redis connectivity
GET  /algorithms Supported algorithms and when to use each
```

Response headers follow the conventional names — `X-RateLimit-Limit`,
`X-RateLimit-Remaining`, `X-RateLimit-Reset`, and `Retry-After` on a 429 — rather
than anything invented here, because clients already parse those.

`algorithm` is validated against a literal type, so an unknown value returns a
422 listing the valid options before any code runs.

---

## Limitations

- **`reset_at` is an upper bound, not exact.** Fixed window and the counter
  genuinely reset at the next boundary, but the log frees its oldest entry at an
  arbitrary moment and the bucket accrues continuously — both recover sooner than
  reported. Computing it exactly means an extra read for the log and returning
  the unfloored token count for the bucket. A conservative `Retry-After` is safe,
  just occasionally pessimistic.
- **Single Redis, no cluster support.** The counter and bucket use one key each so
  they would work in a cluster unmodified; a multi-key design would need hash
  tags.
- **Algorithm is chosen per request**, not configured per key pattern. Realistic
  deployments would want the latter.
- **No distributed clock handling.** Two application servers with skewed clocks
  will disagree about window boundaries. Redis's `TIME` command would fix this at
  the cost of a non-deterministic script.

---

## Layout

```
rate_limiter.py     the four algorithms, one signature each
script.py           every Lua script, annotated
app.py              FastAPI layer
bench_marking.py    memory, latency, accuracy benchmarks
tests/              organised by what each test catches
NOTES.md            full measurements and the reasoning behind each decision
```

## License

MIT