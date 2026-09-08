
from scratch import allow, allow_sliding_log

LIMIT = 10
WINDOW = 60

def test_allows_upto_limit_then_denies(rdb):
    now = 1_700_000_000
    results = [allow_sliding_log(rdb, "alice", limit=LIMIT, window=WINDOW, now=now) for _ in range(12)]

    allowed = [ok for ok, _ in results]
    assert allowed[:LIMIT] == [True] * LIMIT
    assert allowed[LIMIT:] == [False,False]

def test_denied_request_are_not_recorded(rdb):
    """Design choice: a rejected request must not consume a slot in the window."""
    now = 1_700_000_000
    for _ in range(LIMIT + 5):
        allow_sliding_log(rdb, "alice", LIMIT, WINDOW, now=now)

    assert rdb.zcard("rl:log:alice") == LIMIT, (
        "only accepted requests should be stored in the sorted set"
    )

def test_same_millisecond_requests_all_counted(rdb):
    """
    Sorted set members must be unique; scores may repeat. Using the timestamp
    as the member would silently collapse same-millisecond requests.
    """
    now = 1_700_000_000            # every call shares this exact timestamp
    for _ in range(LIMIT):
        allow_sliding_log(rdb, "alice", LIMIT, WINDOW, now=now)

    assert rdb.zcard("rl:log:alice") == LIMIT, (
        "requests sharing a timestamp were collapsed — member is not unique"
    )

def test_ttl_is_set_and_refreshed(rdb):
    now = 1_700_000_000
    allow_sliding_log(rdb, "alice", LIMIT, WINDOW, now=now)
    ttl = rdb.pttl("rl:log:alice")
    assert 0 < ttl <= WINDOW * 1000

def test_separate_users_have_separate_windows(rdb):
    now = 1_700_000_000
    for _ in range(LIMIT):
        allow_sliding_log(rdb, "alice", LIMIT, WINDOW, now=now)

    assert allow_sliding_log(rdb, "alice", LIMIT, WINDOW, now=now)[0] is False
    assert allow_sliding_log(rdb, "bob", LIMIT, WINDOW, now=now)[0] is True

def test_recovers_after_full_window(rdb):
    """Old entries must be pruned once they fall outside the window."""
    now = 1_700_000_000
    for _ in range(LIMIT):
        allow_sliding_log(rdb, "alice", LIMIT, WINDOW, now=now)
    assert allow_sliding_log(rdb, "alice", LIMIT, WINDOW, now=now)[0] is False

    later = now + WINDOW + 1                      # entire first batch now stale
    ok, remaining = allow_sliding_log(rdb, "alice", LIMIT, WINDOW, now=later)
    assert ok is True
    assert remaining == LIMIT - 1
    assert rdb.zcard("rl:log:alice") == 1, "stale entries were not pruned"

def test_partial_recovery_mid_window(rdb):
    """Half the window elapsing should free exactly the requests that aged out."""
    now = 1_700_000_000

    for _ in range(LIMIT // 2):                   # 5 at t=0
        allow_sliding_log(rdb, "alice", LIMIT, WINDOW, now=now)
    for _ in range(LIMIT // 2):                   # 5 at t=+40s
        allow_sliding_log(rdb, "alice", LIMIT, WINDOW, now=now + 40)

    assert allow_sliding_log(rdb, "alice", LIMIT, WINDOW, now=now + 40)[0] is False

    # at t=+61s the first five have aged out, the second five have not
    ok, remaining = allow_sliding_log(rdb, "alice", LIMIT, WINDOW, now=now + 61)
    assert ok is True
    assert rdb.zcard("rl:log:alice") == 6         # 5 survivors + this one


def test_sliding_log_prevents_boundary_burst(rdb):
    """
    The same scenario that lets fixed window through 2x the limit.
    Sliding window must cap it at exactly the limit.
    """
    base = 1_700_000_000
    end_of_window = (base // WINDOW) * WINDOW + WINDOW - 1

    assert end_of_window // WINDOW != (end_of_window + 2) // WINDOW

    before = sum(
        allow_sliding_log(rdb, "bob", LIMIT, WINDOW, now=end_of_window)[0]
        for _ in range(LIMIT)
    )
    after = sum(
        allow_sliding_log(rdb, "bob", LIMIT, WINDOW, now=end_of_window + 2)[0]
        for _ in range(LIMIT)
    )

    total = before + after
    assert before == LIMIT
    assert after == 0, "the second batch is still inside the sliding window"
    assert total == LIMIT, (
        f"{total} requests allowed within 2 seconds against a limit of "
        f"{LIMIT} per {WINDOW}s — sliding window should cap at {LIMIT}"
    )

def test_fixed_and_sliding_disagree_at_the_boundary(rdb):
    """
    Side-by-side: same traffic pattern, same limit, different outcomes.
    This is the whole argument for sliding window.
    """
    base = 1_700_000_000
    boundary = (base // WINDOW) * WINDOW + WINDOW - 1

    fixed_total = (
        sum(allow(rdb, "f", LIMIT, WINDOW, now=boundary)[0] for _ in range(LIMIT))
        + sum(allow(rdb, "f", LIMIT, WINDOW, now=boundary + 2)[0] for _ in range(LIMIT))
    )
    sliding_total = (
        sum(allow_sliding_log(rdb, "s", LIMIT, WINDOW, now=boundary)[0] for _ in range(LIMIT))
        + sum(allow_sliding_log(rdb, "s", LIMIT, WINDOW, now=boundary + 2)[0] for _ in range(LIMIT))
    )

    assert fixed_total == 2 * LIMIT
    assert sliding_total == LIMIT
    assert fixed_total == 2 * sliding_total