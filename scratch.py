import redis
import time
import uuid
from script import LUA_FIXED_WINDOW, LUA_SLIDING_WINDOW

r = redis.Redis(host='localhost', port=6379, decode_responses=True)

SLIDING_WINDOW = r.register_script(LUA_SLIDING_WINDOW)
FIXED_WINDOW = r.register_script(LUA_FIXED_WINDOW)

def allow(client, user, limit, window,now=None):
    if now is None:
        now = time.time()
    bucket = int(now) // window
    key = f"rl:{user}:{bucket}"
    count = FIXED_WINDOW(keys=[key], args=[window], client=client)
    if count <= limit:
        ok = True
        remaining = limit - count
    else:
        ok = False
        remaining = 0

    return ok, remaining

def allow_sliding_log(client, user, limit, window, now=None):
    if now is None:
        now = time.time()
    now_ms = int(now * 1000)
    window_ms = window * 1000
    key = f"rl:log:{user}"
    member = str(uuid.uuid4())
    allowed, remaining = SLIDING_WINDOW(
        keys=[key],
        args=[now_ms, window_ms, limit, member],
        client=client,
    )
    return bool(allowed), int(remaining)

if __name__ == "__main__":
    for i in range(12):
        # ok, remaining = allow(r, "alice", limit=10, window=60)
        ok_sliding, remaining_sliding = allow_sliding_log(r, "alice", limit=10, window=60)
        print(i+1, ok_sliding, remaining_sliding)
        # print(i+1, ok, remaining)
