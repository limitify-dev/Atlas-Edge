"""Pure tap-classification logic — no device, no network, no I/O.

Purely local to Atlas-Edge: this only labels a tap for the Recent taps view
so whoever's watching this device can tell arrivals from departures at a
glance. It never changes what gets pushed to Atlas.
"""

from __future__ import annotations

from datetime import datetime, time
from typing import Optional
from zoneinfo import ZoneInfo


def _parse_hhmm(value: str) -> time:
    hour, _, minute = value.partition(":")
    return time(int(hour), int(minute))


def classify_direction(
    occurred_at: str,
    *,
    tz: str,
    checkin_start: str,
    checkin_end: str,
    checkout_start: str,
    checkout_end: str,
) -> Optional[str]:
    """"check_in" / "check_out" / None — outside both windows, or the
    timestamp/window strings couldn't be parsed."""
    try:
        ts = datetime.fromisoformat(occurred_at)
    except ValueError:
        return None
    if tz:
        try:
            zone = ZoneInfo(tz)
            ts = ts.astimezone(zone) if ts.tzinfo else ts.replace(tzinfo=zone)
        except Exception:  # noqa: BLE001 — unknown/bad tz name, fall back to as-is
            pass
    local_time = ts.time()  # naive wall-clock time, regardless of ts's tzinfo

    try:
        checkin = (_parse_hhmm(checkin_start), _parse_hhmm(checkin_end))
        checkout = (_parse_hhmm(checkout_start), _parse_hhmm(checkout_end))
    except (ValueError, IndexError):
        return None

    if checkin[0] <= local_time < checkin[1]:
        return "check_in"
    if checkout[0] <= local_time < checkout[1]:
        return "check_out"
    return None
