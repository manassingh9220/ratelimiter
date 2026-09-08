from rate_limiter import allow

LIMIT = 10
WINDOW = 60


def test_allows_up_to_limit_then_denies(rdb):
    now = 1_700_000_000
    results = [allow(rdb, "alice", LIMIT, WINDOW, now=now) for _ in range(12)]

    allowed = [ok for ok, _ in results]
    assert allowed[:LIMIT] == [True] * LIMIT
    assert allowed[LIMIT:] == [False, False]


def test_remaining_counts_down(rdb):
    now = 1_700_000_000
    remaining = [allow(rdb, "alice", LIMIT, WINDOW, now=now)[1] for _ in range(LIMIT)]
    assert remaining == list(range(LIMIT - 1, -1, -1))      # 9, 8, ... 0


def test_remaining_never_negative(rdb):
    now = 1_700_000_000
    for _ in range(LIMIT + 5):
        _, remaining = allow(rdb, "alice", LIMIT, WINDOW, now=now)
        assert remaining >= 0


def test_separate_users_have_separate_limits(rdb):
    now = 1_700_000_000
    for _ in range(LIMIT):
        allow(rdb, "alice", LIMIT, WINDOW, now=now)

    ok, _ = allow(rdb, "alice", LIMIT, WINDOW, now=now)
    assert ok is False, "alice should be exhausted"

    ok, _ = allow(rdb, "bob", LIMIT, WINDOW, now=now)
    assert ok is True, "bob's limit must be independent of alice's"


def test_window_resets_in_next_bucket(rdb):
    now = 1_700_000_000
    for _ in range(LIMIT):
        allow(rdb, "alice", LIMIT, WINDOW, now=now)
    assert allow(rdb, "alice", LIMIT, WINDOW, now=now)[0] is False

    # jump a full window forward — new bucket, fresh counter
    ok, remaining = allow(rdb, "alice", LIMIT, WINDOW, now=now + WINDOW)
    assert ok is True
    assert remaining == LIMIT - 1


def test_ttl_is_set_on_the_counter(rdb):
    """Keys must expire on their own — no cleanup job."""
    allow(rdb, "alice", LIMIT, WINDOW, now=1_700_000_000)
    keys = rdb.keys("*")
    assert len(keys) == 1
    ttl = rdb.ttl(keys[0])
    assert 0 < ttl <= WINDOW, f"expected a TTL up to {WINDOW}s, got {ttl}"


def test_boundary_burst(rdb):
    """
    Fixed window's known flaw: a client can send 2x the limit by straddling
    a window reset. This test documents the behaviour — it is expected to pass.
    """
    base = 1_700_000_000
    end_of_window = (base // WINDOW) * WINDOW + WINDOW - 1

    # adding 2s must cross into the next bucket, or the test proves nothing
    assert end_of_window // WINDOW != (end_of_window + 2) // WINDOW

    before = sum(
        allow(rdb, "bob", LIMIT, WINDOW, now=end_of_window)[0]
        for _ in range(LIMIT)
    )
    after = sum(
        allow(rdb, "bob", LIMIT, WINDOW, now=end_of_window + 2)[0]
        for _ in range(LIMIT)
    )

    total = before + after
    assert before == LIMIT
    assert after == LIMIT
    assert total == 2 * LIMIT, (
        f"{total} requests allowed within 2 seconds "
        f"against a limit of {LIMIT} per {WINDOW}s"
    )