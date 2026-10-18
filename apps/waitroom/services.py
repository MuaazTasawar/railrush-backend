"""Waiting-room queue logic. All queue state lives in Redis.

How it works (a leaky-bucket scheduler):
  * Each run has one "next free slot", a timestamp.
  * Joining takes the slot max(next free slot, now, booking_opens_at) and moves the pointer
    forward by 1 / WAITROOM_ADMIT_PER_SECOND. Admissions are therefore spaced evenly however
    bursty the joins are, and an idle queue never stockpiles free admissions.
  * A user's record is "ticket:admit_at". The client sleeps until admit_at and then asks for its
    pass, so no background worker and almost no polling are needed.
  * The whole join is one Lua script, so it is atomic, and joining twice returns the same ticket.
"""

import math
import time
from dataclasses import dataclass
from datetime import datetime
from datetime import timezone as dt_timezone

from django.conf import settings
from django.core.cache import cache
from django.core.exceptions import ImproperlyConfigured
from rest_framework.exceptions import NotFound

from apps.catalog.models import TrainRun
from apps.common.exceptions import DependencyUnavailable
from apps.common.redis import get_redis

from . import passes

OPENING_SOON = "opening_soon"
OPEN = "open"
CLOSED = "closed"

WAITING = "waiting"
ADMITTED = "admitted"
EXPIRED = "expired"

RUN_INFO_TTL_SECONDS = 30
MISSING_RUN_TTL_SECONDS = 15
RUN_KEY_GRACE_SECONDS = 86400
RECORD_GRACE_SECONDS = 60
# The Lua script stores slots with 6 decimals, so a stored slot can sit up to half a microsecond
# after "now". A person whose slot is that close counts as admitted.
TIME_EPSILON = 1e-6

# KEYS: issued counter, next free slot, this user's record.
# ARGV: now, interval, opens_at, max_queue, pass_ttl, record_grace, run_key_ttl.
# Returns {1, record} for a new ticket, {0, record} for an existing one, {-1, ''} when full.
JOIN_LUA = """
local existing = redis.call('GET', KEYS[3])
if existing then return {0, existing} end
local issued = tonumber(redis.call('GET', KEYS[1]) or '0')
if issued >= tonumber(ARGV[4]) then return {-1, ''} end
local now = tonumber(ARGV[1])
local interval = tonumber(ARGV[2])
local opens = tonumber(ARGV[3])
local nxt = tonumber(redis.call('GET', KEYS[2]) or '0')
local slot = math.max(nxt, now, opens)
local ticket = redis.call('INCR', KEYS[1])
redis.call('SET', KEYS[2], string.format('%.6f', slot + interval))
local record = ticket .. ':' .. string.format('%.6f', slot)
local ttl = math.ceil(slot - now + tonumber(ARGV[5]) + tonumber(ARGV[6]))
redis.call('SET', KEYS[3], record, 'EX', ttl)
redis.call('EXPIRE', KEYS[1], ARGV[7])
redis.call('EXPIRE', KEYS[2], ARGV[7])
return {1, record}
"""


@dataclass(frozen=True)
class RunInfo:
    run_id: int
    opens_at: float
    departs_at: float
    cancelled: bool


@dataclass(frozen=True)
class Ticket:
    number: int
    admit_at: float


def now():
    return time.time()


def iso(timestamp):
    moment = datetime.fromtimestamp(timestamp, tz=dt_timezone.utc)
    return moment.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def admit_interval():
    rate = float(settings.WAITROOM_ADMIT_PER_SECOND)
    if rate <= 0:
        raise ImproperlyConfigured("WAITROOM_ADMIT_PER_SECOND must be greater than zero.")
    return 1.0 / rate


def get_run_info(run_id):
    """The run's schedule, cached briefly so a rush costs the database almost nothing."""
    key = f"wr:runinfo:{run_id}"
    cached = cache.get(key)
    if cached == "missing":
        raise NotFound("Run not found.")
    if cached is not None:
        return RunInfo(*cached)
    row = (
        TrainRun.objects.filter(pk=run_id)
        .values_list("booking_opens_at", "departure_at", "status")
        .first()
    )
    if row is None:
        cache.set(key, "missing", MISSING_RUN_TTL_SECONDS)
        raise NotFound("Run not found.")
    opens_at, departs_at, status = row
    info = RunInfo(
        run_id=run_id,
        opens_at=opens_at.timestamp(),
        departs_at=departs_at.timestamp(),
        cancelled=status == TrainRun.Status.CANCELLED,
    )
    cache.set(
        key,
        (info.run_id, info.opens_at, info.departs_at, info.cancelled),
        RUN_INFO_TTL_SECONDS,
    )
    return info


def room_state(info, at):
    if info.cancelled or at >= info.departs_at:
        return CLOSED
    if at < info.opens_at - settings.WAITROOM_PREQUEUE_SECONDS:
        return OPENING_SOON
    return OPEN


def _run_keys(run_id):
    # The braces are a Redis hash tag: all keys of one run stay together if Redis is ever clustered.
    base = f"{settings.WAITROOM_KEY_PREFIX}:{{{run_id}}}"
    return f"{base}:issued", f"{base}:next"


def _user_key(run_id, user_id):
    return f"{settings.WAITROOM_KEY_PREFIX}:{{{run_id}}}:u:{user_id}"


def _parse(record):
    number, admit_at = record.split(":", 1)
    return Ticket(number=int(number), admit_at=float(admit_at))


def join(info, user_id):
    """Take a place in the queue. Returns (ticket, created)."""
    at = now()
    run_ttl = max(int(info.departs_at - at), 0) + RUN_KEY_GRACE_SECONDS
    issued_key, next_key = _run_keys(info.run_id)
    script = get_redis().register_script(JOIN_LUA)
    outcome, record = script(
        keys=[issued_key, next_key, _user_key(info.run_id, user_id)],
        args=[
            at,
            admit_interval(),
            info.opens_at,
            settings.WAITROOM_MAX_QUEUE,
            settings.WAITROOM_PASS_TTL_SECONDS,
            RECORD_GRACE_SECONDS,
            run_ttl,
        ],
    )
    if outcome == -1:
        raise DependencyUnavailable("The waiting room is full. Please try again shortly.")
    return _parse(record), outcome == 1


def get_ticket(info, user_id):
    record = get_redis().get(_user_key(info.run_id, user_id))
    return _parse(record) if record else None


def ticket_status(info, user_id, ticket, at):
    """What a signed-in user sees about their own place. Includes the pass once admitted."""
    rate = float(settings.WAITROOM_ADMIT_PER_SECOND)
    window_ends = ticket.admit_at + settings.WAITROOM_PASS_TTL_SECONDS
    body = {
        "ticket": ticket.number,
        "admit_at": iso(ticket.admit_at),
        "server_time": iso(at),
    }
    if at + TIME_EPSILON < ticket.admit_at:
        wait = ticket.admit_at - at
        body.update(
            state=WAITING,
            wait_seconds=round(wait, 3),
            people_ahead=max(0, math.ceil(wait * rate) - 1),
        )
    elif at < window_ends:
        body.update(
            state=ADMITTED,
            waitroom_pass=passes.issue_pass(user_id, info.run_id, window_ends),
            pass_expires_at=iso(window_ends),
        )
    else:
        body.update(state=EXPIRED)
    return body


def public_status(info, at):
    """Queue-wide numbers anyone may see. Safe to cache: it contains nothing about a person."""
    rate = float(settings.WAITROOM_ADMIT_PER_SECOND)
    issued_key, next_key = _run_keys(info.run_id)
    issued, next_slot = get_redis().mget([issued_key, next_key])
    next_slot = max(float(next_slot or 0.0), at, info.opens_at)
    waiting = max(0, round((next_slot - max(at, info.opens_at)) * rate))
    return {
        "run_id": info.run_id,
        "state": room_state(info, at),
        "opens_at": iso(info.opens_at),
        "joins_open_at": iso(info.opens_at - settings.WAITROOM_PREQUEUE_SECONDS),
        "admit_per_second": rate,
        "tickets_issued": int(issued or 0),
        "people_waiting": waiting,
        "estimated_wait_seconds": round(max(0.0, next_slot - at), 1),
        "server_time": iso(at),
    }
