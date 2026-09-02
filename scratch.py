import redis
import time
from script import LUA_FIXED_WINDOW

r = redis.Redis(host='localhost', port=6379, decode_responses=True)

FIXED_WINDOW = r.register_script(LUA_FIXED_WINDOW)

def allow(client, user, limit, window, now=None):
    if now is None:
        now= time.time()
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


if __name__ == "__main__":
    for i in range(12):
        ok, remaining = allow(r, "alice", limit=10, window=60, now=time.time())
        print(i+1, ok, remaining)
