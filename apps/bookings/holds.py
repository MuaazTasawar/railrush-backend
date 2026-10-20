"""Seat holds: a short, exclusive claim on a seat while its holder pays.

State lives in Redis, three keys per run (the braces are a hash tag that keeps them together):
  hold:{run}:z          sorted set  seat -> expiry time
  hold:{run}:o          hash        seat -> user id of the holder
  hold:{run}:u:<user>   sorted set  that user's seats -> expiry time

Holding and releasing are each one Lua script, so they are atomic: a request for several seats
gets all of them or none, two people can never hold the same seat, and one user can never exceed
the per-user limit however many requests race. Expired holds are purged by the next hold on that
run, and every key expires by itself once the last hold is over.
"""

from dataclasses import dataclass

from django.conf import settings

from apps.common.redis import get_redis
from apps.waitroom import services as waitroom

HELD = "held"
UNAVAILABLE = "unavailable"
LIMIT = "limit"
KEY_GRACE_SECONDS = 60

# KEYS: all holds of the run, owner hash, this user's holds.
# ARGV: now, ttl, max seats per user, user id, key grace seconds, then the seats wanted.
# Returns {1, {seat, expiry, ...}, number newly held}, {-1, {taken seats}} or {-2, seats already held}.
HOLD_LUA = """
local now = tonumber(ARGV[1])
local ttl = tonumber(ARGV[2])
local max_per_user = tonumber(ARGV[3])
local user = ARGV[4]
local grace = tonumber(ARGV[5])

local expired = redis.call('ZRANGEBYSCORE', KEYS[1], '-inf', now)
for i = 1, #expired, 500 do
  local chunk = {}
  for j = i, math.min(i + 499, #expired) do chunk[#chunk + 1] = expired[j] end
  redis.call('HDEL', KEYS[2], unpack(chunk))
  redis.call('ZREM', KEYS[1], unpack(chunk))
end
redis.call('ZREMRANGEBYSCORE', KEYS[3], '-inf', now)

local mine = redis.call('ZCARD', KEYS[3])
local conflicts = {}
local fresh = {}
for i = 6, #ARGV do
  local owner = redis.call('HGET', KEYS[2], ARGV[i])
  if not owner then
    fresh[#fresh + 1] = ARGV[i]
  elseif owner ~= user then
    conflicts[#conflicts + 1] = ARGV[i]
  end
end
if #conflicts > 0 then return {-1, conflicts} end
if mine + #fresh > max_per_user then return {-2, mine} end

local expires = now + ttl
for _, seat in ipairs(fresh) do
  redis.call('ZADD', KEYS[1], expires, seat)
  redis.call('HSET', KEYS[2], seat, user)
  redis.call('ZADD', KEYS[3], expires, seat)
end
local flat = {}
for i = 6, #ARGV do
  flat[#flat + 1] = ARGV[i]
  flat[#flat + 1] = redis.call('ZSCORE', KEYS[3], ARGV[i])
end
for k = 1, 3 do redis.call('EXPIRE', KEYS[k], ttl + grace) end
return {1, flat, #fresh}
"""

# KEYS as above. ARGV: now, user id, then the seats to release (none means all of the user's).
# Returns the seats that were released while still live.
RELEASE_LUA = """
local now = tonumber(ARGV[1])
local user = ARGV[2]
local targets = {}
if #ARGV >= 3 then
  for i = 3, #ARGV do targets[#targets + 1] = ARGV[i] end
else
  targets = redis.call('ZRANGE', KEYS[3], 0, -1)
end
local released = {}
for _, seat in ipairs(targets) do
  local owner = redis.call('HGET', KEYS[2], seat)
  if owner == user then
    local score = tonumber(redis.call('ZSCORE', KEYS[1], seat) or '0')
    redis.call('HDEL', KEYS[2], seat)
    redis.call('ZREM', KEYS[1], seat)
    redis.call('ZREM', KEYS[3], seat)
    if score > now then released[#released + 1] = seat end
  end
end
return released
"""


@dataclass(frozen=True)
class HoldResult:
    status: str
    seats: dict
    created: int = 0
    unavailable: tuple = ()
    held_now: int = 0


def now():
    # One clock for the whole app; tests replace it in one place.
    return waitroom.now()


def _run_keys(run_id):
    base = f"{settings.HOLD_KEY_PREFIX}:{{{run_id}}}"
    return f"{base}:z", f"{base}:o"


def _user_key(run_id, user_id):
    return f"{settings.HOLD_KEY_PREFIX}:{{{run_id}}}:u:{user_id}"


def hold(run_id, user_id, labels, at):
    """Hold every seat in labels for the user, or none of them."""
    z_key, owner_key = _run_keys(run_id)
    script = get_redis().register_script(HOLD_LUA)
    reply = script(
        keys=[z_key, owner_key, _user_key(run_id, user_id)],
        args=[
            at,
            settings.HOLD_SECONDS,
            settings.HOLD_MAX_SEATS_PER_USER,
            str(user_id),
            KEY_GRACE_SECONDS,
            *labels,
        ],
    )
    if reply[0] == -1:
        return HoldResult(UNAVAILABLE, {}, unavailable=tuple(reply[1]))
    if reply[0] == -2:
        return HoldResult(LIMIT, {}, held_now=int(reply[1]))
    flat = reply[1]
    seats = {flat[i]: float(flat[i + 1]) for i in range(0, len(flat), 2)}
    return HoldResult(HELD, seats, created=int(reply[2]))


def release(run_id, user_id, labels, at):
    """Release the user's own seats (all of them when labels is empty). Returns those released."""
    z_key, owner_key = _run_keys(run_id)
    script = get_redis().register_script(RELEASE_LUA)
    reply = script(
        keys=[z_key, owner_key, _user_key(run_id, user_id)],
        args=[at, str(user_id), *labels],
    )
    return list(reply)


def holds_for_user(run_id, user_id, at):
    """The user's live holds as {seat: expiry timestamp}."""
    rows = get_redis().zrangebyscore(
        _user_key(run_id, user_id), f"({at}", "+inf", withscores=True
    )
    return dict(sorted(rows))


def active_labels(run_id, at):
    """Every seat currently held on the run, by anyone."""
    z_key, _ = _run_keys(run_id)
    return get_redis().zrangebyscore(z_key, f"({at}", "+inf")


def verify_holds(run_id, user_id, labels, at):
    """True when the user holds every one of these seats right now (used by Phase 7 bookings)."""
    mine = holds_for_user(run_id, user_id, at)
    return bool(labels) and all(label in mine for label in labels)
