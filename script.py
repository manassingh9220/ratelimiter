LUA_FIXED_WINDOW = """
local c = redis.call('INCR', KEYS[1])
if c == 1 then
    redis.call('EXPIRE', KEYS[1], ARGV[1])
end
return c
"""

LUA_SLIDING_WINDOW = """
local key = KEYS[1]
local now = tonumber(ARGV[1])
local window = tonumber(ARGV[2])
local limit = tonumber(ARGV[3])
local member = ARGV[4]

redis.call('ZREMRANGEBYSCORE', key, '-inf', now - window)
local count = redis.call('ZCARD', key)

local allowed = 0
if count < limit then
    redis.call('ZADD', key, now, member)
    allowed = 1
    count = count + 1
end

redis.call('PEXPIRE', key, window)

local remaining = limit - count

if remaining < 0 then
    remaining = 0
end

return {allowed, remaining}
"""