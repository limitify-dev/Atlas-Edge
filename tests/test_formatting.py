from datetime import datetime, timedelta, timezone

from atlas_edge.formatting import shorttime, timeago


def iso(delta: timedelta) -> str:
    return (datetime.now(timezone.utc) - delta).isoformat()


def test_timeago_missing_value():
    assert timeago(None) == "—"
    assert timeago("") == "—"


def test_timeago_buckets():
    assert timeago(iso(timedelta(seconds=2))) == "just now"
    assert timeago(iso(timedelta(seconds=30))) == "30s ago"
    assert timeago(iso(timedelta(minutes=5))) == "5m ago"
    assert timeago(iso(timedelta(hours=3))) == "3h ago"
    assert timeago(iso(timedelta(days=2))) == "2d ago"


def test_timeago_older_than_a_week_is_an_absolute_date():
    old = datetime(2020, 1, 15, tzinfo=timezone.utc)
    assert timeago(old.isoformat()) == "Jan 15, 2020"


def test_timeago_clock_skew_does_not_go_negative():
    future = iso(timedelta(seconds=-10))  # a timestamp "in the future"
    assert timeago(future) == "just now"


def test_timeago_malformed_value_falls_back_to_raw_string():
    assert timeago("not-a-timestamp") == "not-a-timestamp"


def test_timeago_naive_datetime_is_treated_as_utc():
    naive = (datetime.utcnow() - timedelta(minutes=2)).isoformat()
    assert timeago(naive) == "2m ago"


def test_shorttime_formats_without_microseconds_or_offset():
    dt = datetime(2026, 9, 11, 15, 44, 24, 41232, tzinfo=timezone.utc)
    assert shorttime(dt.isoformat()) == "Sep 11, 15:44:24"


def test_shorttime_missing_and_malformed():
    assert shorttime(None) == "—"
    assert shorttime("garbage") == "garbage"
