"""
Tests for the token bucket.

Unlike the other three, there is no window to align to at all — tokens
refill continuously at `limit / window` per second, capped at `limit`, and a
cold bucket starts full. That last point is a deliberate choice: starting
empty would throttle a client's very first-ever request.
"""

from rate_limiter import allow_token_bucket

LIMIT = 10
WINDOW = 60
BASE = 1_700_000_000


def key(user):
    return f"rl:tb:{user}"


# --------------------------------------------------------------- basics ----

def test_cold_bucket_starts_full(rdb):
    """The defining choice vs. a naive implementation: no penalty for a first-ever call."""
    ok, remaining = allow_token_bucket(rdb, "alice", LIMIT, WINDOW, now=BASE)
    assert ok is True
    assert remaining == LIMIT - 1


def test_allows_up_to_limit_then_denies(rdb):
    results = [allow_token_bucket(rdb, "alice", LIMIT, WINDOW, now=BASE) for _ in range(12)]
    allowed = [ok for ok, _ in results]
    assert allowed[:LIMIT] == [True] * LIMIT
    assert allowed[LIMIT:] == [False, False]


def test_remaining_never_negative(rdb):
    for _ in range(LIMIT + 5):
        _, remaining = allow_token_bucket(rdb, "alice", LIMIT, WINDOW, now=BASE)
        assert remaining >= 0


def test_separate_users_have_separate_buckets(rdb):
    for _ in range(LIMIT):
        allow_token_bucket(rdb, "alice", LIMIT, WINDOW, now=BASE)

    assert allow_token_bucket(rdb, "alice", LIMIT, WINDOW, now=BASE)[0] is False
    assert allow_token_bucket(rdb, "bob", LIMIT, WINDOW, now=BASE)[0] is True


def test_uses_a_single_key_per_user(rdb):
    """No bucket-number in the key at all — refill is computed lazily inside the script."""
    allow_token_bucket(rdb, "alice", LIMIT, WINDOW, now=BASE)
    allow_token_bucket(rdb, "alice", LIMIT, WINDOW, now=BASE + WINDOW)
    allow_token_bucket(rdb, "alice", LIMIT, WINDOW, now=BASE + WINDOW * 2)
    assert rdb.keys("rl:tb:*") == [key("alice")]


# -------------------------------------------------------------- refill -----

def test_refill_rate_is_proportional_to_elapsed(rdb):
    """limit/window per second: 6s refills 1 token, 30s refills 5."""
    for _ in range(LIMIT):
        allow_token_bucket(rdb, "six", LIMIT, WINDOW, now=BASE)
    six_second_refill = sum(
        allow_token_bucket(rdb, "six", LIMIT, WINDOW, now=BASE + 6)[0] for _ in range(3)
    )
    assert six_second_refill == 1

    for _ in range(LIMIT):
        allow_token_bucket(rdb, "thirty", LIMIT, WINDOW, now=BASE)
    thirty_second_refill = sum(
        allow_token_bucket(rdb, "thirty", LIMIT, WINDOW, now=BASE + 30)[0] for _ in range(7)
    )
    assert thirty_second_refill == 5


def test_full_burst_after_idle(rdb):
    """Idle a whole window and the full quota is back — the burst-permitting behaviour."""
    for _ in range(LIMIT):
        allow_token_bucket(rdb, "alice", LIMIT, WINDOW, now=BASE)

    allowed = sum(
        allow_token_bucket(rdb, "alice", LIMIT, WINDOW, now=BASE + WINDOW)[0]
        for _ in range(LIMIT)
    )
    assert allowed == LIMIT


def test_refill_caps_at_capacity(rdb):
    """Idle 10 windows and tokens still cap at `limit` rather than accumulating forever."""
    allow_token_bucket(rdb, "alice", LIMIT, WINDOW, now=BASE)   # spend 1
    allowed = sum(
        allow_token_bucket(rdb, "alice", LIMIT, WINDOW, now=BASE + WINDOW * 10)[0]
        for _ in range(LIMIT + 5)
    )
    assert allowed == LIMIT


def test_sustained_rate_matches_limit_over_window(rdb):
    """
    `limit` requests spread evenly across `window` seconds — one every 6s here —
    is precisely the rate the bucket is sized to sustain. Repeated over three
    windows, none of it should ever be denied.
    """
    interval = WINDOW / LIMIT
    results = [
        allow_token_bucket(rdb, "steady", LIMIT, WINDOW, now=BASE + i * interval)[0]
        for i in range(LIMIT * 3)
    ]
    assert all(results), "sustained rate should never be denied"


# ------------------------------------------------------------ precision ----

def test_fractional_tokens_accumulate(rdb):
    """
    3 separate 2-second gaps must accumulate to 1 full token, not truncate to 0
    each time. Guards against storing tokens as an integer (e.g. `math.floor`
    before HSET): each gap alone refills less than 1 token, so a bucket that
    floors on every write would never climb past 0 no matter how long it waits.

    This is a coarser bug than the float-drift %.17g protects against (that one
    only shows up after thousands of round trips — see Notes.md) — 3 calls
    isn't enough iterations to exercise %.14g vs %.17g rounding.
    """
    for _ in range(LIMIT):
        allow_token_bucket(rdb, "alice", LIMIT, WINDOW, now=BASE)   # drain to empty

    assert allow_token_bucket(rdb, "alice", LIMIT, WINDOW, now=BASE + 2)[0] is False
    assert allow_token_bucket(rdb, "alice", LIMIT, WINDOW, now=BASE + 4)[0] is False
    ok, remaining = allow_token_bucket(rdb, "alice", LIMIT, WINDOW, now=BASE + 6)
    assert ok is True
    assert remaining == 0


def test_clock_going_backwards_does_not_refill(rdb):
    """
    A `now` earlier than the stored last_refill (clock skew) must not subtract
    into a refill — without the elapsed<0 guard in the Lua script, a large
    backward jump multiplies out to a large *negative* token count instead of
    leaving the bucket alone.
    """
    for _ in range(LIMIT):
        allow_token_bucket(rdb, "alice", LIMIT, WINDOW, now=BASE)   # drain to empty

    ok, remaining = allow_token_bucket(rdb, "alice", LIMIT, WINDOW, now=BASE - 100)
    assert ok is False
    assert remaining == 0


# ------------------------------------------------------------- storage -----

def test_ttl_covers_a_full_refill_from_empty(rdb):
    """
    The key must survive at least as long as an empty bucket takes to refill,
    or its state would be discarded while it still means something.
    """
    allow_token_bucket(rdb, "alice", LIMIT, WINDOW, now=BASE)
    ttl = rdb.ttl(key("alice"))
    assert 0 < ttl <= WINDOW


# ------------------------------------------------------------ boundary -----

def test_no_boundary_to_straddle(rdb):
    """
    Fixed window's flaw is a shared clock-aligned reset. The bucket has no
    window to align to at all, so bursting right at what would be a fixed
    window's boundary buys nothing extra.
    """
    boundary = (BASE // WINDOW) * WINDOW + WINDOW - 1

    before = sum(
        allow_token_bucket(rdb, "bob", LIMIT, WINDOW, now=boundary)[0]
        for _ in range(LIMIT)
    )
    after = sum(
        allow_token_bucket(rdb, "bob", LIMIT, WINDOW, now=boundary + 2)[0]
        for _ in range(LIMIT)
    )

    assert before == LIMIT
    assert after == 0, "2 seconds only refills a fraction of one token, not a fresh window"
