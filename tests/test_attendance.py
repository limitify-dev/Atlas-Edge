"""Tap direction classification — purely local, never sent to Atlas."""

from atlas_edge.attendance import classify_direction

WINDOWS = dict(
    tz="",
    checkin_start="06:00",
    checkin_end="09:00",
    checkout_start="14:00",
    checkout_end="18:00",
)


def test_morning_tap_is_checkin():
    assert classify_direction("2026-01-01T07:30:00+00:00", **WINDOWS) == "check_in"


def test_afternoon_tap_is_checkout():
    assert classify_direction("2026-01-01T15:00:00+00:00", **WINDOWS) == "check_out"


def test_midday_tap_is_unclassified():
    assert classify_direction("2026-01-01T11:00:00+00:00", **WINDOWS) is None


def test_window_start_is_inclusive_end_is_exclusive():
    assert classify_direction("2026-01-01T06:00:00+00:00", **WINDOWS) == "check_in"
    assert classify_direction("2026-01-01T09:00:00+00:00", **WINDOWS) is None


def test_unparsable_timestamp_returns_none():
    assert classify_direction("not-a-timestamp", **WINDOWS) is None


def test_converts_to_configured_timezone():
    # 07:00 UTC is 09:00 in Africa/Kigali (UTC+2) — past the check-in window there.
    kwargs = dict(WINDOWS, tz="Africa/Kigali")
    assert classify_direction("2026-01-01T07:00:00+00:00", **kwargs) is None
    assert classify_direction("2026-01-01T04:30:00+00:00", **kwargs) == "check_in"
