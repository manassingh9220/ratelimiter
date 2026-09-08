"""
Rate limiter algorithms, each backed by an atomic Lua script.

Every function has the same signature so they are interchangeable:

    allow_*(client, user, limit, window, now=None) -> (allowed, remaining)

    client  a redis.Redis instance (passed in so tests can use a separate db)
    user    opaque identifier for whatever is being limited
    limit   requests permitted per window
    window  window length in SECONDS
    now     unix timestamp; defaults to time.time(). Injectable so boundary
            behaviour can be tested without waiting for a real clock rollover.
"""

import time
import uuid

import redis

from script import LUA_FIXED_WINDOW, LUA_SLIDING_COUNTER, LUA_SLIDING_LOG, LUA_TOKEN_BUCKET

r = redis.Redis(host="localhost", port=6379, decode_responses=True)

# register_script sends the body once and calls it by SHA thereafter (EVALSHA).
# The returned object is bound to `r`, but accepts a `client=` override per call
# so tests can point it at a different database.
FIXED_WINDOW = r.register_script(LUA_FIXED_WINDOW)
SLIDING_LOG = r.register_script(LUA_SLIDING_LOG)
SLIDING_COUNTER = r.register_script(LUA_SLIDING_COUNTER)
TOKEN_BUCKET = r.register_script(LUA_TOKEN_BUCKET)


def allow(client, user, limit, window, now=None):
    """
    Fixed window.

    The window number is part of the key, so the key itself changes at each
    clock-aligned boundary and old keys expire on their own.

    Cheapest option — one integer, one round trip, constant memory. Its flaw is
    that a client can spend a full quota either side of a boundary and get 2x
    the limit within seconds.
    """
    if now is None:
        now = time.time()

    bucket = int(now) // window
    key = f"rl:{user}:{bucket}"

    count = FIXED_WINDOW(keys=[key], args=[window], client=client)

    if count <= limit:
        return True, limit - count
    return False, 0


def allow_sliding_log(client, user, limit, window, now=None):
    """
    Sliding window log.

    Stores a timestamp per accepted request in a sorted set and looks back
    exactly `window` from this instant. Exact, with no boundary to exploit.

    The UUID member is required: sorted set members must be unique while scores
    may repeat, so using the timestamp for both would let two requests in the
    same millisecond overwrite each other.
    """
    if now is None:
        now = time.time()

    key = f"rl:log:{user}"
    allowed, remaining = SLIDING_LOG(
        keys=[key],
        args=[int(now * 1000), window * 1000, limit, str(uuid.uuid4())],
        client=client,
    )
    return bool(allowed), int(remaining)


def allow_sliding_counter(client, user, limit, window, now=None):
    """
    Sliding window counter.

    Two integers — the count in the current window and the previous one —
    with the previous weighted by how much of it still falls inside the
    sliding view. Approximates the log at fixed window's memory cost.

    One key per user; rollover is handled inside the script.
    """
    if now is None:
        now = time.time()

    key = f"rl:swc:{user}"
    allowed, remaining = SLIDING_COUNTER(
        keys=[key],
        args=[int(now * 1000), window * 1000, limit],
        client=client,
    )
    return bool(allowed), int(remaining)


def allow_token_bucket(client, user, limit, window, now=None):
    """
    Token bucket.

    Stores tokens remaining and a last-refill timestamp, and refills lazily
    on each call at `limit` tokens per `window` seconds — no fixed or sliding
    window at all. A cold bucket starts full, so it deliberately permits a
    burst up to `limit` right away; a client idle for a whole window earns
    back the full quota to spend at once.

    `limit / window` converts the (limit, window) shape shared by every
    strategy here into the tokens-per-second rate the Lua script wants.
    """
    if now is None:
        now = time.time()

    key = f"rl:tb:{user}"
    allowed, remaining = TOKEN_BUCKET(
        keys=[key],
        args=[int(now * 1000), limit, limit / window],
        client=client,
    )
    return bool(allowed), int(remaining)


STRATEGIES = {
    "fixed": allow, 
    "sliding_log": allow_sliding_log,
    "sliding_counter": allow_sliding_counter,
    "token_bucket": allow_token_bucket,
}


if __name__ == "__main__":
    now = time.time()          # one timestamp for all four, so they compare fairly
    r.delete("rl:log:demo", "rl:swc:demo", "rl:tb:demo", f"rl:demo:{int(now) // 60}")

    print(f"{'#':>3}  {'fixed':<16}{'log':<16}{'counter':<16}{'bucket':<16}")
    print("-" * 71)
    for i in range(12):
        f = allow(r, "demo", 10, 60, now=now)
        l = allow_sliding_log(r, "demo", 10, 60, now=now)
        c = allow_sliding_counter(r, "demo", 10, 60, now=now)
        b = allow_token_bucket(r, "demo", 10, 60, now=now)
        fmt = lambda t: f"{'allow' if t[0] else 'DENY':<6} rem={t[1]:<4}"
        print(f"{i + 1:>3}  {fmt(f):<16}{fmt(l):<16}{fmt(c):<16}{fmt(b):<16}")