"""The local-test simulators must honour the same contracts as the real
device/client so nothing else in the app can tell the difference."""

import json

import pytest

from atlas_edge.atlas_client import AtlasAuthError, AtlasError, AtlasPushError
from atlas_edge.enrollment import PlannedWrite
from atlas_edge.mock import MockAtlasClient, MockF18
from atlas_edge.storage import Storage


def storage(tmp_path):
    s = Storage(tmp_path / "m.sqlite3")
    s.init_db()
    return s


# ── MockF18 ────────────────────────────────────────────────────────────
def test_mock_device_set_and_read_users(tmp_path):
    dev = MockF18(storage(tmp_path))
    dev.connect()
    dev.set_user(uid=1, card_number="1001", name="Alice")
    dev.set_user(uid=2, card_number="1002", name="Bob")
    users = {u.user_id: u.name for u in dev.get_users()}
    assert users == {"1001": "Alice", "1002": "Bob"}
    assert dev.build_name_map() == {"1001": "Alice", "1002": "Bob"}


def test_mock_device_set_user_is_update_in_place(tmp_path):
    dev = MockF18(storage(tmp_path))
    dev.connect()
    dev.set_user(uid=1, card_number="1001", name="Old")
    dev.set_user(uid=1, card_number="1001", name="New")
    assert [(u.uid, u.name) for u in dev.get_users()] == [(1, "New")]


def test_mock_device_bulk_write_returns_ok_and_failed(tmp_path):
    dev = MockF18(storage(tmp_path))
    dev.connect()
    writes = [
        PlannedWrite(uid=1, card_number="1001", name="A", action="add"),
        PlannedWrite(uid=2, card_number="1002", name="B", action="add"),
    ]
    ok, failed = dev.bulk_write(writes)
    assert sorted(ok) == ["1001", "1002"] and failed == []


def test_mock_device_live_events_delivers_injected_taps_then_none(tmp_path):
    s = storage(tmp_path)
    dev = MockF18(s, poll_seconds=0.01)
    dev.connect()
    s.mock_enqueue_tap(card_number="1001", occurred_at="2026-03-03T08:00:00+00:00")
    s.mock_enqueue_tap(card_number="1002", occurred_at="2026-03-03T08:01:00+00:00")

    seen = []
    for item in dev.live_events(tick_seconds=0):  # tick immediately
        seen.append(item)
        if len(seen) >= 3:
            dev.disconnect()
            break
    cards = [t.card_number for t in seen if t is not None]
    assert cards == ["1001", "1002"]
    assert None in seen  # housekeeping tick delivered


def test_mock_device_reconcile_reads_onboard_log(tmp_path):
    from datetime import datetime, timezone

    s = storage(tmp_path)
    dev = MockF18(s)
    s.mock_enqueue_tap(card_number="1001", occurred_at="2026-03-03T08:00:00+00:00")
    s.mock_enqueue_tap(card_number="1002", occurred_at="2026-03-03T09:00:00+00:00")

    all_taps = dev.get_attendance_since(None)
    assert [t.card_number for t in all_taps] == ["1001", "1002"]

    since = datetime(2026, 3, 3, 8, 30, tzinfo=timezone.utc)
    later = dev.get_attendance_since(since)
    assert [t.card_number for t in later] == ["1002"]


# ── MockAtlasClient ───────────────────────────────────────────────────
def make_atlas(tmp_path, *, assignments=None, fail_rate=0.0):
    s = storage(tmp_path)
    path = tmp_path / "cards.json"
    if assignments is not None:
        path.write_text(json.dumps(assignments))
    return MockAtlasClient(s, assignments_path=path, fail_rate=fail_rate), s


def test_mock_atlas_login_accepts_anything_nonempty(tmp_path):
    atlas, _ = make_atlas(tmp_path)
    assert atlas.is_authenticated() is False
    atlas.login("teacher@demo.io", "whatever")
    assert atlas.is_authenticated() is True
    atlas.logout()
    assert atlas.is_authenticated() is False


def test_mock_atlas_login_rejects_blank(tmp_path):
    atlas, _ = make_atlas(tmp_path)
    with pytest.raises(AtlasAuthError):
        atlas.login("", "")


def test_mock_atlas_push_requires_login_then_records(tmp_path):
    atlas, s = make_atlas(tmp_path)
    with pytest.raises(AtlasAuthError):
        atlas.push_attendance_event(card_number="1", name="x", timestamp="t")
    atlas.login("t@demo.io", "pw")
    atlas.push_attendance_event(card_number="1001", name="Alice", timestamp="2026-01-01T08:00:00")
    assert s.mock_atlas_event_count() == 1
    ev = json.loads(s.mock_recent_atlas_events()[0]["payload"])
    assert ev["card_number"] == "1001" and ev["name"] == "Alice"


def test_mock_atlas_fail_rate_1_always_raises_retryable(tmp_path):
    atlas, _ = make_atlas(tmp_path, fail_rate=1.0)
    atlas.login("t@demo.io", "pw")
    with pytest.raises(AtlasPushError):
        atlas.push_attendance_event(card_number="1", name="x", timestamp="t")


def test_mock_atlas_card_assignments_from_file(tmp_path):
    rows = [
        {"student_id": "s1", "name": "Alice", "card_number": "1001"},
        {"student_id": "s2", "name": "Bob", "card_number": "1002"},
    ]
    atlas, _ = make_atlas(tmp_path, assignments=rows)
    atlas.login("t@demo.io", "pw")
    got = atlas.get_card_assignments()
    assert [(a.student_id, a.card_number) for a in got] == [("s1", "1001"), ("s2", "1002")]


def test_mock_atlas_card_assignments_missing_file_falls_back_to_sample(tmp_path):
    atlas, _ = make_atlas(tmp_path)  # no file written
    atlas.login("t@demo.io", "pw")
    got = atlas.get_card_assignments()
    assert len(got) >= 1 and all(a.card_number for a in got)


def test_mock_atlas_card_assignments_bad_json_raises(tmp_path):
    atlas, _ = make_atlas(tmp_path)
    atlas._assignments_path.write_text("{not json")
    atlas.login("t@demo.io", "pw")
    with pytest.raises(AtlasError):
        atlas.get_card_assignments()
