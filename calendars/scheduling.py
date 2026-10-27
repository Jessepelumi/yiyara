"""Local scheduling rules only. Google busy events are a separate constraint."""
from datetime import timedelta, timezone
from zoneinfo import ZoneInfo

from .models import ProtectedPeriod, SchedulingPreferences


def subtract_periods(windows, blocks):
    for start, end in sorted(blocks):
        remaining = []
        for left, right in windows:
            if end <= left or start >= right:
                remaining.append((left, right))
            else:
                if left < start:
                    remaining.append((left, start))
                if end < right:
                    remaining.append((end, right))
        windows = remaining
    return windows


def permitted_windows(user, start, end):
    """Return exact half-open UTC windows; never assume hours before setup.

    Weekly rules have minute precision. Evaluate UTC minutes against local wall
    time so both occurrences of a fall-back minute are protected, and nonexistent
    spring-forward minutes cannot create work time. One-off blocks retain exact
    timestamp precision and are subtracted afterwards.
    """
    preferences = SchedulingPreferences.objects.filter(user=user).first()
    if not preferences:
        return None
    zone = ZoneInfo(preferences.timezone)
    periods = list(ProtectedPeriod.objects.filter(user=user))
    weekly = [p for p in periods if p.kind == "weekly"]
    working = {}
    for window in preferences.working_hours:
        working.setdefault(window["weekday"], []).append((window["start_time"], window["end_time"]))
    start, end = start.astimezone(timezone.utc), end.astimezone(timezone.utc)
    cursor = start.replace(second=0, microsecond=0)
    windows = []
    while cursor < end:
        local = cursor.astimezone(zone)
        day, clock = local.date(), local.strftime("%H:%M")
        allowed = any(left <= clock < right for left, right in working.get(local.weekday(), []))
        if allowed:
            for period in weekly:
                if (local.weekday() not in period.weekdays
                        or (period.start_date and day < period.start_date)
                        or (period.end_date and day > period.end_date)):
                    continue
                if period.all_day or period.start_time <= local.time().replace(tzinfo=None) < period.end_time:
                    allowed = False
                    break
        right = min(end, cursor + timedelta(minutes=1))
        left = max(start, cursor)
        if allowed and left < right:
            if windows and windows[-1][1] == left:
                windows[-1] = (windows[-1][0], right)
            else:
                windows.append((left, right))
        cursor += timedelta(minutes=1)
    return subtract_periods(windows, [(p.starts_at, p.ends_at) for p in periods if p.kind == "one_off"])
