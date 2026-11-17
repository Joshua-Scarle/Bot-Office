from __future__ import annotations

from datetime import date, datetime, timedelta
from itertools import pairwise
from zoneinfo import ZoneInfo

# Routine schedules are written in Joshua's zone, not UTC and not the host's.
CRON_TZ = ZoneInfo("America/New_York")


def to_cron_time(moment: datetime) -> datetime:
    """Wall-clock time in America/New_York (EST/EDT). Naive input is taken as already ET."""
    if moment.tzinfo is None:
        return moment.replace(tzinfo=CRON_TZ)
    return moment.astimezone(CRON_TZ)

FIELDS = (("minute", 0, 59), ("hour", 0, 23), ("day-of-month", 1, 31), ("month", 1, 12), ("day-of-week", 0, 7))


def _parse_field(text: str, name: str, low: int, high: int) -> frozenset[int]:
    values: set[int] = set()
    for part in text.split(","):
        base, _, step_text = part.partition("/")
        if step_text and not step_text.isdigit():
            raise ValueError(f"invalid step in {name}")
        step = int(step_text) if step_text else 1
        if step < 1:
            raise ValueError(f"invalid step in {name}")
        if base == "*":
            start, end = low, high
        elif "-" in base:
            first, _, last = base.partition("-")
            if not (first.isdigit() and last.isdigit()):
                raise ValueError(f"invalid range in {name}")
            start, end = int(first), int(last)
        elif base.isdigit():
            start = int(base)
            end = high if step_text else start
        else:
            raise ValueError(f"invalid value in {name}")
        if not (low <= start <= end <= high):
            raise ValueError(f"{name} out of range")
        values.update(range(start, end + 1, step))
    return frozenset(values)


def parse_cron(expr: str) -> tuple[frozenset[int], ...]:
    parts = expr.split()
    if len(parts) != 5:
        raise ValueError("cron must have 5 fields")
    parsed = [_parse_field(part, name, low, high) for part, (name, low, high) in zip(parts, FIELDS)]
    parsed[4] = frozenset(day % 7 for day in parsed[4])
    return tuple(parsed)


def cron_matches(expr: str, moment: datetime) -> bool:
    return _matches(expr, parse_cron(expr), moment)


def min_gap(expr: str, start: datetime | None = None) -> timedelta | None:
    """Shortest real time between two fires of *expr* over every date it can fire on (a full
    28-year calendar cycle from *start*, so a burst on a rare day is seen too). The hour skipped
    when DST starts counts: 01:59 and 03:00 fire one minute apart. None: it never fires twice."""
    minute, hour, dom, month, dow = parse_cron(expr)
    fields = expr.split()
    times = sorted(h * 60 + m for h in hour for m in minute)
    first = to_cron_time(start or datetime.now(CRON_TZ)).date()

    def fires_on(day: date) -> bool:
        if day.month not in month:
            return False
        dom_ok, dow_ok = day.day in dom, (day.weekday() + 1) % 7 in dow
        if fields[2] == "*" or fields[4] == "*":
            return dom_ok and dow_ok
        return dom_ok or dow_ok

    days = [day for day in (first + timedelta(days=i) for i in range(CYCLE_DAYS)) if fires_on(day)]
    if not days or not times:
        return None
    gaps = []
    if len(times) > 1:
        gaps += [b - a for a, b in pairwise(times)]
        if any(_dst_starts(day) for day in days):
            kept = [t for t in times if not 120 <= t < 180]
            gaps += [b - a - (60 if a < 120 <= b else 0) for a, b in pairwise(kept)]
    gaps += [(b - a).days * 1440 + times[0] - times[-1] for a, b in pairwise(days)]
    return timedelta(minutes=min(gaps)) if gaps else None


CYCLE_DAYS = 28 * 366  # weekdays and leap years repeat within 28 years (2001..2099)


def _dst_starts(day: date) -> bool:
    """The clocks spring forward (02:00 -> 03:00) on *day* in Joshua's zone."""
    midnight = datetime(day.year, day.month, day.day, tzinfo=CRON_TZ)
    return midnight.utcoffset() < (midnight + timedelta(hours=12)).utcoffset()


def _matches(expr: str, parsed: tuple[frozenset[int], ...], moment: datetime) -> bool:
    minute, hour, dom, month, dow = parsed
    moment = to_cron_time(moment)
    if moment.minute not in minute or moment.hour not in hour or moment.month not in month:
        return False
    dom_ok = moment.day in dom
    dow_ok = (moment.weekday() + 1) % 7 in dow
    dom_any = expr.split()[2] == "*"
    dow_any = expr.split()[4] == "*"
    if dom_any or dow_any:
        return dom_ok and dow_ok
    return dom_ok or dow_ok
