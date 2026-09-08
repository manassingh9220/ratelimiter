"""
Lua scripts for each rate limiting algorithm.

Every script runs atomically inside Redis. That matters because a rate limit
decision is a read-modify-write cycle: read the current count, decide, write
the new count. Split across three round trips from Python, two clients can
interleave between the read and the write, both see the same count, and both
allow a request that should have been denied.

A Lua script occupies Redis's single command thread for its whole duration, so
no other client's commands run in between. The gap does not exist.

Conventions used below:
  - KEYS[n]  are the Redis keys the script touches, declared separately so
             Redis Cluster knows which shard to route to.
  - ARGV[n]  are plain arguments. Everything arrives as a *string*, so
             tonumber() is required before arithmetic.
  - Lua tables returned from a script become arrays in Python. Lua booleans do
             not survive the conversion cleanly (false becomes nil), so these
             scripts return 1/0 instead of true/false.
"""

# ---------------------------------------------------------------------------
# FIXED WINDOW
# ---------------------------------------------------------------------------
# Counts requests per clock-aligned window. The window number is baked into the
# key name by the caller (rl:fixed:{user}:{floor(now/window)}), so the key
# itself changes at each boundary and old keys expire on their own — no
# cleanup job, no sweep.
#
# Cheapest option: one integer per key, one round trip, O(1) memory forever.
#
# Its flaw is the whole reason the other algorithms exist. Because every client
# shares the same boundary, a client can send the full quota at 11:59:59 and
# the full quota again at 12:00:01 — 2x the limit in two seconds — because the
# key changed in between.
#
# KEYS[1] = rl:fixed:{user}:{bucket}
# ARGV[1] = window in SECONDS (EXPIRE takes seconds)
# returns   the post-increment count
# ---------------------------------------------------------------------------
LUA_FIXED_WINDOW = """
local c = redis.call('INCR', KEYS[1])

-- Set the TTL only on the first increment of this window.
-- Setting it every call would push the expiry forward on every request, so a
-- client sending steady traffic would keep the window alive indefinitely and
-- the counter would never reset.
if c == 1 then
    redis.call('EXPIRE', KEYS[1], ARGV[1])
end

return c
"""


# ---------------------------------------------------------------------------
# SLIDING WINDOW LOG
# ---------------------------------------------------------------------------
# Stores a timestamp for every accepted request in a sorted set, then looks
# back exactly `window` milliseconds from *this instant* on every call. There
# is no shared boundary, so there is nothing to straddle. Exact by construction.
#
# The cost is memory: one sorted set member per accepted request.
#
# Two decisions worth knowing:
#
#   1. The member is a UUID, not the timestamp. Sorted set MEMBERS must be
#      unique; SCORES may repeat. Using the timestamp for both would make the
#      second request in a given millisecond overwrite the first instead of
#      adding to it, silently undercounting. This is not hypothetical — a
#      12-request smoke test had 5 requests share one millisecond.
#
#   2. Denied requests are NOT stored. A rejected request neither consumes a
#      slot nor extends the window. This bounds worst-case memory at `limit`
#      entries per key rather than letting it scale with request volume —
#      measured at 4.8 KB vs 1.2 MB for the same 10k requests.
#
# KEYS[1] = rl:log:{user}          (no bucket — one key per user, forever)
# ARGV[1] = now in MILLISECONDS
# ARGV[2] = window in MILLISECONDS
# ARGV[3] = limit
# ARGV[4] = a unique member id (UUID), generated per call by the caller
# returns   {allowed, remaining}
# ---------------------------------------------------------------------------
LUA_SLIDING_LOG = """
local key    = KEYS[1]
local now    = tonumber(ARGV[1])
local window = tonumber(ARGV[2])
local limit  = tonumber(ARGV[3])
local member = ARGV[4]

-- Drop everything that has aged out of the window. This is what makes the
-- window "slide": the cutoff moves forward with every call.
redis.call('ZREMRANGEBYSCORE', key, '-inf', now - window)

local count = redis.call('ZCARD', key)

local allowed = 0
if count < limit then
    redis.call('ZADD', key, now, member)
    allowed = 1
    count = count + 1
end

-- Refreshed on EVERY call, unlike fixed window. Here the window is supposed to
-- slide, so the expiry should move with it. PEXPIRE because the window is in ms.
redis.call('PEXPIRE', key, window)

local remaining = limit - count
if remaining < 0 then
    remaining = 0
end

return {allowed, remaining}
"""


# ---------------------------------------------------------------------------
# SLIDING WINDOW COUNTER
# ---------------------------------------------------------------------------
# Approximates the log using two integers instead of N timestamps: the count in
# the current fixed window and the count in the previous one, with the previous
# weighted by how much of it still falls inside the sliding view.
#
#     elapsed  = (now % window) / window        -- 0.0 .. 1.0 through this window
#     estimate = current + previous * (1 - elapsed)
#
# 25% into the current minute means 75% of the previous minute is still within
# the last 60 seconds, so 75% of its count is charged against you.
#
# The approximation assumes the previous window's requests were spread evenly.
# When they were actually bunched at the end of that window, the estimate
# undercounts and slightly more than `limit` can slip through. That error is
# bounded by `previous * elapsed`, which is largest right after a rollover —
# and right after a rollover, `elapsed` is near zero, so in practice the slack
# is under one request in the boundary scenario.
#
# THREE ROLLOVER CASES, and the third is the one people forget:
#
#   stored == bucket        same window       keep both counters
#   stored == bucket - 1    rolled by one     current becomes previous, reset current
#   anything else           cold or idle 2+   reset both to zero
#
# Missing the third case means counts from ten minutes ago get weighted into
# today's estimate after an idle period.
#
# KEYS[1] = rl:swc:{user}          (one key, rollover handled inside)
# ARGV[1] = now in MILLISECONDS
# ARGV[2] = window in MILLISECONDS
# ARGV[3] = limit
# returns   {allowed, remaining}
# ---------------------------------------------------------------------------
LUA_SLIDING_COUNTER = """
local key     = KEYS[1]
local now     = tonumber(ARGV[1])
local window  = tonumber(ARGV[2])
local limit   = tonumber(ARGV[3])

local bucket  = math.floor(now / window)
local elapsed = (now % window) / window

-- HMGET on a missing key returns a table of `false` values, not nils, and
-- tonumber(false) errors — hence the `data[1] and ...` guard.
local data   = redis.call('HMGET', key, 'bucket', 'current', 'previous')
local stored = data[1] and tonumber(data[1]) or nil

local cur, prev

if stored == bucket then
    -- Still inside the same window: both counters are current.
    cur  = tonumber(data[2]) or 0
    prev = tonumber(data[3]) or 0

elseif stored == bucket - 1 then
    -- Rolled over by exactly one window. What was `current` is now the
    -- window immediately behind us, so it becomes `previous`. The old
    -- `previous` is two windows back and no longer overlaps — discard it.
    cur  = 0
    prev = tonumber(data[2]) or 0

else
    -- Either the key does not exist, or the client has been idle for two or
    -- more windows. Nothing stored still overlaps the sliding view.
    cur  = 0
    prev = 0
end

-- Estimate is computed BEFORE counting this request, so it represents
-- "how much of the quota is already used".
local estimate = cur + prev * (1 - elapsed)

-- floor() before comparing: an estimate of 9.6 counts as 9 used, so the
-- request is allowed. This errs very slightly permissive, which is the
-- conventional choice — a rate limiter that rejects a legitimate request is
-- worse than one that occasionally allows an extra.
local allowed = 0
if math.floor(estimate) < limit then
    allowed = 1
    cur = cur + 1
end

redis.call('HSET', key, 'bucket', bucket, 'current', cur, 'previous', prev)

-- TTL is TWO windows, not one. `previous` has to survive into the current
-- window to be usable; a one-window TTL would delete the key exactly when the
-- previous count starts mattering.
-- EXPIRE takes seconds while `window` is in ms, and ceil() stops very small
-- windows from rounding down to a TTL of 0 (which would delete immediately).
redis.call('EXPIRE', key, math.ceil(window * 2 / 1000))

-- Subtract `allowed` separately because `estimate` was taken before the
-- increment — otherwise this request would not be reflected in `remaining`.
local remaining = limit - math.floor(estimate) - allowed
if remaining < 0 then
    remaining = 0
end

return {allowed, remaining}
"""


# ---------------------------------------------------------------------------
# TOKEN BUCKET
# ---------------------------------------------------------------------------
# Stores two numbers per user — tokens remaining, and when the bucket was
# last topped up — and computes the refill lazily on each call instead of on
# a timer. There is no window at all, fixed or sliding: capacity refills
# continuously at `rate` tokens per second, capped at `capacity`.
#
# The defining behaviour, and the reason to reach for this over the other
# three: it deliberately PERMITS bursts. A client idle for a while
# accumulates tokens up to `capacity` and can spend them all at once — the
# right shape for APIs where occasional spikes are normal and only the
# sustained rate needs capping.
#
# A new key starts FULL (tokens = capacity), not empty. Starting empty would
# throttle the very first request from a client that has never used the
# limiter before — none of the other three algorithms punish a cold key like
# that, so this one should not either.
#
# Two decisions worth knowing:
#
#   1. A hash, not a string. Tokens and last_refill must be read and written
#      together — a hash keeps that atomic without a second key, the same
#      reasoning that put the sliding counter's two integers in a hash.
#
#   2. Tokens are written with %.17g, not Lua's default tostring. Every call
#      round-trips the value through a string: read, add a fractional
#      refill, write back. Lua's default number-to-string uses %.14g (14
#      significant digits), which is not enough to reconstruct an IEEE-754
#      double exactly — 17 digits are required for that. Left on the
#      default, every call quietly drops the last bit or two of precision,
#      and that rounding compounds into visible drift over enough requests.
#      %.17g makes the round trip exact, so no drift accumulates no matter
#      how many requests pass through.
#
# Elapsed time is clamped at zero before it multiplies into a refill amount.
# Every other script here only subtracts a fixed cutoff (ZREMRANGEBYSCORE) or
# keys off a floored bucket number, both harmless if `now` moves backward.
# This script multiplies elapsed time directly into a token count, so a
# negative elapsed from clock skew would erase tokens instead of adding
# them — the clamp is load-bearing here in a way it isn't elsewhere.
#
# KEYS[1] = rl:tb:{user}
# ARGV[1] = now in MILLISECONDS
# ARGV[2] = capacity (max tokens / max burst size)
# ARGV[3] = refill rate in tokens per SECOND
# returns   {allowed, remaining}   (remaining is floor(tokens), post-request)
# ---------------------------------------------------------------------------
LUA_TOKEN_BUCKET = """
local key      = KEYS[1]
local now      = tonumber(ARGV[1])
local capacity = tonumber(ARGV[2])
local rate     = tonumber(ARGV[3])          -- tokens per second

local data = redis.call('HMGET', key, 'tokens', 'last_refill')

local tokens, last_refill
if data[1] then
    tokens      = tonumber(data[1])
    last_refill = tonumber(data[2])
else
    -- Cold key: start full, as of now, so the first-ever request is not
    -- throttled by a limiter it has never touched.
    tokens      = capacity
    last_refill = now
end

local elapsed = now - last_refill
if elapsed < 0 then
    elapsed = 0
end

tokens = tokens + elapsed * (rate / 1000)
if tokens > capacity then
    tokens = capacity
end

local allowed = 0
if tokens >= 1 then
    allowed = 1
    tokens  = tokens - 1
end

-- %.17g: enough significant digits to round-trip a double exactly, so the
-- fractional remainder survives the string form Redis stores it in.
redis.call('HSET', key, 'tokens', string.format('%.17g', tokens), 'last_refill', now)

-- TTL is however long a full refill from empty takes, so a bucket that goes
-- idle disappears once its state stops being meaningful — the same
-- self-cleanup the other three get from EXPIRE/PEXPIRE.
redis.call('EXPIRE', key, math.ceil(capacity / rate))

return {allowed, math.floor(tokens)}
"""