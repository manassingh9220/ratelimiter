import pytest
import redis


@pytest.fixture
def rdb():
    """A clean Redis on db 15 for each test."""
    r = redis.Redis(host="localhost", port=6379, db=15, decode_responses=True)
    r.flushdb()
    yield r
    r.flushdb()