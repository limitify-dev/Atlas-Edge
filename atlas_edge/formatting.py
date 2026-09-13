"""Small display helpers shared by the web templates. Pure functions, no I/O —
kept separate from `web/app.py` so they're easy to unit test."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional


def timeago(value: Optional[str]) -> str:
    """``"2026-09-11T15:44:24+00:00"`` -> ``"3m ago"``.

    Falls back to the raw value (or "—") if it isn't a timestamp we recognise,
    so a malformed/legacy marker never crashes a page — it just looks a little
    plain instead of pretty.
    """
    if not value:
        return "—"
    try:
        dt = datetime.fromisoformat(value)
    except (ValueError, TypeError):
        return str(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)

    seconds = (datetime.now(timezone.utc) - dt).total_seconds()
    if seconds < 0:
        seconds = 0  # clock skew — don't show "in -3s"

    if seconds < 5:
        return "just now"
    if seconds < 60:
        return f"{int(seconds)}s ago"
    if seconds < 3600:
        return f"{int(seconds // 60)}m ago"
    if seconds < 86400:
        return f"{int(seconds // 3600)}h ago"
    if seconds < 86400 * 7:
        return f"{int(seconds // 86400)}d ago"
    try:
        return dt.strftime("%b %-d, %Y")  # "Sep 11, 2026" — no leading zero (BSD/Linux)
    except ValueError:
        return dt.strftime("%b %d, %Y")  # Windows strftime doesn't support %-d


def shorttime(value: Optional[str]) -> str:
    """``"2026-09-11T15:44:24.041232+00:00"`` -> ``"Sep 11, 15:44:24"``.

    Unlike :func:`timeago`, this keeps the exact moment — for attendance
    records, where "a few minutes ago" isn't precise enough to matter, but the
    microseconds and UTC offset in the raw ISO string are just noise.
    """
    if not value:
        return "—"
    try:
        dt = datetime.fromisoformat(value)
    except (ValueError, TypeError):
        return str(value)
    try:
        return dt.strftime("%b %-d, %H:%M:%S")
    except ValueError:
        return dt.strftime("%b %d, %H:%M:%S")
