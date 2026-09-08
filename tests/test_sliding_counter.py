"""
Tests for the sliding window counter.

The counter keeps two integers — the count in the current fixed window and the
count in the previous one — and weights the previous by how much of it still
falls inside the sliding view:

    elapsed  = (now % window) / window
    estimate = current + previous * (1 - elapsed)

It is an approximation, so several assertions here allow a small tolerance.
Where they do, the reason is stated.
"""

from scratch import allow, allow_sliding_counter, allow_sliding_log

LIMIT = 10
WINDOW = 60
BASE = 1_700_000_000

# the last second of a window — adding a few seconds crosses into the next one
BOUNDARY = (BASE // WINDOW) * WINDOW + WINDOW - 1


def key(user):
    return f"rl:swc:{user}"


# --------------------------------------------------------------- basics ----

def test_allows_up_to_limit_then_denies(rdb):
    results = [allow_sliding_counter(rdb, "alice", LIMIT, WINDOW, now=BASE)
               for _ in range(12)]
    allowed = [ok for ok, _ in results]
    assert allowed[:LIMIT] == [True] * LIMIT
    assert allowed[LIMIT:] == [False, False]


def test_remaining_counts_down(rdb):
    remaining = [allow_sliding_counter(rdb, "alice", LIMIT, WINDOW, now=BASE)[1]
                 for _ in range(LIMIT)]
    assert remaining == list(range(LIMIT - 1, -1, -1))      # 9, 8, ... 0


def test_remaining_never_negative(rdb):
    for _ in range(LIMIT + 5):
        _, remaining = allow_sliding_counter(rdb, "alice", LIMIT, WINDOW, now=BASE)
        assert remaining >= 0


def test_separate_users_have_separate_limits(rdb):
    for _ in range(LIMIT):
        allow_sliding_counter(rdb, "alice", LIMIT, WINDOW, now=BASE)

    assert allow_sliding_counter(rdb, "alice", LIMIT, WINDOW, now=BASE)[0] is False
    assert allow_sliding_counter(rdb, "bob", LIMIT, WINDOW, now=BASE)[0] is True


def test_uses_a_single_key_per_user(rdb):
    """No bucket in the key — rollover is handled inside the script."""
    allow_sliding_counter(rdb, "alice", LIMIT, WINDOW, now=BASE)
    allow_sliding_counter(rdb, "alice", LIMIT, WINDOW, now=BASE + WINDOW)
    allow_sliding_counter(rdb, "alice", LIMIT, WINDOW, now=BASE + WINDOW * 2)
    assert rdb.keys("rl:swc:*") == [key("alice")]


# ------------------------------------------------------------- rollover ----

def test_same_bucket_accumulates(rdb):
    for _ in range(5):
        allow_sliding_counter(rdb, "eve", LIMIT, WINDOW, now=BASE)

    h = rdb.hgetall(key("eve"))
    assert int(h["current"]) == 5
    assert int(h["previous"]) == 0


def test_rollover_by_one_shifts_current_to_previous(rdb):
    for _ in range(5):
        allow_sliding_counter(rdb, "eve", LIMIT, WINDOW, now=BASE)
    allow_sliding_counter(rdb, "eve", LIMIT, WINDOW, now=BASE + WINDOW)

    h = rdb.hgetall(key("eve"))
    assert int(h["current"]) == 1, "the new window starts fresh"
    assert int(h["previous"]) == 5, "the old current becomes previous"


def test_gap_of_two_or_more_windows_resets_both(rdb):
    """
    The case people forget. After an idle period longer than two windows,
    nothing stored still overlaps the sliding view — both counters must clear.
    Without this branch, counts from long ago get weighted into the estimate.
    """
    for _ in range(5):
        allow_sliding_counter(rdb, "eve", LIMIT, WINDOW, now=BASE)
    allow_sliding_counter(rdb, "eve", LIMIT, WINDOW, now=BASE + WINDOW * 5)

    h = rdb.hgetall(key("eve"))
    assert int(h["current"]) == 1
    assert int(h["previous"]) == 0, "stale counters were carried forward"


def test_full_quota_available_after_long_idle(rdb):
    """Behavioural consequence of the reset branch above."""
    for _ in range(LIMIT):
        allow_sliding_counter(rdb, "eve", LIMIT, WINDOW, now=BASE)
    assert allow_sliding_counter(rdb, "eve", LIMIT, WINDOW, now=BASE)[0] is False

    later = BASE + WINDOW * 5
    allowed = sum(allow_sliding_counter(rdb, "eve", LIMIT, WINDOW, now=later)[0]
                  for _ in range(LIMIT))
    assert allowed == LIMIT


def test_ttl_spans_two_windows(rdb):
    """
    `previous` must survive into the current window to be usable. A one-window
    TTL would delete the key exactly when the previous count starts mattering.
    """
    allow_sliding_counter(rdb, "alice", LIMIT, WINDOW, now=BASE)
    ttl = rdb.ttl(key("alice"))
    assert WINDOW < ttl <= WINDOW * 2, f"expected a TTL near {WINDOW * 2}s, got {ttl}"


# --------------------------------------------------- boundary behaviour ----

def test_boundary_burst_is_bounded(rdb):
    """
    The scenario that lets 20 through fixed window. The counter charges ~97%
    of the previous window's count against you at +2s, so at most one extra
    request slips through.
    """
    assert BOUNDARY // WINDOW != (BOUNDARY + 2) // WINDOW

    before = sum(allow_sliding_counter(rdb, "bob", LIMIT, WINDOW, now=BOUNDARY)[0]
                 for _ in range(LIMIT))
    after = sum(allow_sliding_counter(rdb, "bob", LIMIT, WINDOW, now=BOUNDARY + 2)[0]
                for _ in range(LIMIT))

    assert before == LIMIT
    assert after <= 1, f"{after} extra allowed just past the boundary"
    assert before + after <= LIMIT + 1, (
        f"{before + after} allowed across the boundary against a limit of {LIMIT}"
    )


def test_recovery_is_proportional_to_elapsed(rdb):
    """
    Quota returns in proportion to how much of the previous window has slid
    out of view. Expected extra ~= LIMIT * elapsed; allow +/-1 for the floor()
    in the comparison and for integer counts.

    Note the +59s row: by the end of the next window the previous one has
    almost fully aged out, so a near-full fresh quota is correct, not a defect.
    The sliding log behaves identically. Accuracy should be judged per sliding
    window, not by summing across two.
    """
    for offset in (10, 20, 30, 45, 59):
        rdb.flushdb()
        for _ in range(LIMIT):
            allow_sliding_counter(rdb, "carol", LIMIT, WINDOW, now=BOUNDARY)

        extra = sum(
            allow_sliding_counter(rdb, "carol", LIMIT, WINDOW, now=BOUNDARY + offset)[0]
            for _ in range(LIMIT)
        )
        elapsed = ((BOUNDARY + offset) % WINDOW) / WINDOW
        expected = LIMIT * elapsed

        assert abs(extra - expected) <= 1.5, (
            f"+{offset}s (elapsed={elapsed:.2f}): allowed {extra}, "
            f"expected about {expected:.1f}"
        )


def test_no_recovery_within_the_same_window(rdb):
    """Time passing inside one window releases nothing — only rollover does."""
    for _ in range(LIMIT):
        allow_sliding_counter(rdb, "carol", LIMIT, WINDOW, now=BASE)

    for delta in (1, 10, 30):
        ok, _ = allow_sliding_counter(rdb, "carol", LIMIT, WINDOW, now=BASE + delta)
        assert ok is False, f"quota should not return {delta}s into the same window"


# ------------------------------------------ three-way comparison ----------

def test_counter_sits_between_fixed_and_log_at_the_boundary(rdb):
    """
    The whole argument for this algorithm, in one assertion.
    Same traffic, same limit, three implementations.
    """
    fixed = (
        sum(allow(rdb, "x", LIMIT, WINDOW, now=BOUNDARY)[0] for _ in range(LIMIT))
        + sum(allow(rdb, "x", LIMIT, WINDOW, now=BOUNDARY + 2)[0] for _ in range(LIMIT))
    )
    log = (
        sum(allow_sliding_log(rdb, "y", LIMIT, WINDOW, now=BOUNDARY)[0] for _ in range(LIMIT))
        + sum(allow_sliding_log(rdb, "y", LIMIT, WINDOW, now=BOUNDARY + 2)[0] for _ in range(LIMIT))
    )
    counter = (
        sum(allow_sliding_counter(rdb, "z", LIMIT, WINDOW, now=BOUNDARY)[0] for _ in range(LIMIT))
        + sum(allow_sliding_counter(rdb, "z", LIMIT, WINDOW, now=BOUNDARY + 2)[0] for _ in range(LIMIT))
    )

    assert fixed == 2 * LIMIT, "fixed window should burst to 2x"
    assert log == LIMIT, "the log is exact"
    assert log <= counter <= LIMIT + 1, (
        f"counter allowed {counter}; expected between {log} (exact) "
        f"and {LIMIT + 1} (one request of approximation slack)"
    )
    assert counter < fixed, "the counter must be strictly better than fixed window"


def test_counter_uses_less_memory_than_the_log(rdb):
    """Two integers in a hash versus one sorted set member per request."""
    for _ in range(LIMIT):
        allow_sliding_counter(rdb, "m", LIMIT, WINDOW, now=BASE)
        allow_sliding_log(rdb, "m", LIMIT, WINDOW, now=BASE)

    counter_bytes = rdb.memory_usage("rl:swc:m")
    log_bytes = rdb.memory_usage("rl:log:m")
    assert counter_bytes < log_bytes, (
        f"counter {counter_bytes} B vs log {log_bytes} B"
    )