## Fixed window
- 20 requests allowed in 2s against a limit of 10/60s (test_boundary_burst)
- 1 Redis round trip per decision
- 1 key per (user, window), TTL = window, self-cleaning
- Memory: 1 integer per active key

## Sliding window log
- Boundary burst: 10 allowed (vs 20 for fixed window), same traffic pattern
- 10 members stored for 12 requests — denied requests are not recorded
- 5 of 10 requests shared a millisecond timestamp; UUID members prevent
  ZADD from overwriting (scores may repeat, members may not)
- 1 key per user, no bucket
- Memory: one sorted-set member per accepted request