"""Local queue behaviour: dedup between the live stream and reconciliation,
retry/backoff bookkeeping, and the web<->listener command hand-off."""

from atlas_edge.storage import Storage, utcnow_iso


def fresh(tmp_path) -> Storage:
    s = Storage(tmp_path / "t.sqlite3")
    s.init_db()
    return s


def test_enqueue_dedupes_same_card_and_timestamp(tmp_path):
    s = fresh(tmp_path)
    assert s.enqueue_event(
        card_number="1001", name="Ada", occurred_at="2026-01-01T08:00:00", source="live"
    ) is True
    # reconciliation later re-reports the exact same tap -> no-op
    assert s.enqueue_event(
        card_number="1001", name=None, occurred_at="2026-01-01T08:00:00", source="reconcile"
    ) is False
    assert s.queue_stats()["pending_total"] == 1


def test_debounce_drops_a_quick_repeat_tap_from_the_same_card(tmp_path):
    s = fresh(tmp_path)
    assert s.enqueue_event(
        card_number="1001", name="Ada", occurred_at="2026-01-01T08:00:00+00:00",
        source="live", debounce_seconds=30,
    ) is True
    # fumbled a second tap 5s later — same physical arrival, not a new one
    assert s.enqueue_event(
        card_number="1001", name="Ada", occurred_at="2026-01-01T08:00:05+00:00",
        source="live", debounce_seconds=30,
    ) is False
    assert s.queue_stats()["pending_total"] == 1


def test_debounce_does_not_swallow_a_later_genuine_tap(tmp_path):
    s = fresh(tmp_path)
    assert s.enqueue_event(
        card_number="1001", name="Ada", occurred_at="2026-01-01T08:00:00+00:00",
        source="live", debounce_seconds=30,
    ) is True
    # well outside the debounce window -> a real second event (e.g. checkout)
    assert s.enqueue_event(
        card_number="1001", name="Ada", occurred_at="2026-01-01T15:00:00+00:00",
        source="live", debounce_seconds=30,
    ) is True
    assert s.queue_stats()["pending_total"] == 2


def test_debounce_is_per_card_not_global(tmp_path):
    s = fresh(tmp_path)
    assert s.enqueue_event(
        card_number="1001", name="Ada", occurred_at="2026-01-01T08:00:00+00:00",
        source="live", debounce_seconds=30,
    ) is True
    assert s.enqueue_event(
        card_number="1002", name="Bob", occurred_at="2026-01-01T08:00:01+00:00",
        source="live", debounce_seconds=30,
    ) is True
    assert s.queue_stats()["pending_total"] == 2


def test_a_card_can_tap_more_than_twice_in_a_day(tmp_path):
    # Whether a tap "counts" for attendance is Atlas's call, not Edge's —
    # Edge forwards every non-debounced tap and lets Atlas decide.
    s = fresh(tmp_path)
    for hour in (7, 12, 15, 18):
        assert s.enqueue_event(
            card_number="1001", name="Ada", occurred_at=f"2026-01-01T{hour:02d}:00:00+00:00",
            source="live",
        ) is True
    assert s.queue_stats()["pending_total"] == 4


def test_direction_is_stored_and_defaults_to_none(tmp_path):
    s = fresh(tmp_path)
    s.enqueue_event(
        card_number="1001", name="Ada", occurred_at="2026-01-01T07:00:00+00:00",
        source="live", direction="check_in",
    )
    s.enqueue_event(
        card_number="1002", name="Bob", occurred_at="2026-01-01T11:00:00+00:00",
        source="live",
    )
    by_card = {r["card_number"]: r["direction"] for r in s.recent_events(10)}
    assert by_card["1001"] == "check_in"
    assert by_card["1002"] is None


def test_due_events_respects_next_attempt_at(tmp_path):
    s = fresh(tmp_path)
    s.enqueue_event(card_number="1", name="x", occurred_at="t1", source="live")
    (row,) = s.due_events()
    s.mark_event_failed(int(row["id"]), "boom", next_attempt_at="2999-01-01T00:00:00")
    assert s.due_events() == []  # backed off into the future
    stats = s.queue_stats()
    assert stats.get("failed") == 1 and stats["pending_total"] == 1


def test_mark_sent_removes_from_pending(tmp_path):
    s = fresh(tmp_path)
    s.enqueue_event(card_number="1", name="x", occurred_at="t1", source="live")
    (row,) = s.due_events()
    s.mark_event_sent(int(row["id"]))
    assert s.due_events() == []
    assert s.queue_stats().get("sent") == 1


def test_delete_event_removes_it_outright_not_as_failed(tmp_path):
    s = fresh(tmp_path)
    s.enqueue_event(card_number="1", name="x", occurred_at="t1", source="live")
    (row,) = s.due_events()
    s.delete_event(int(row["id"]))
    assert s.due_events() == []
    stats = s.queue_stats()
    assert stats.get("failed", 0) == 0
    assert stats.get("pending_total", 0) == 0
    assert stats.get("sent", 0) == 0  # not recorded as sent either — just gone


def test_command_queue_claim_is_one_shot(tmp_path):
    s = fresh(tmp_path)
    cid = s.enqueue_command("bulk_sync", {}, requested_by="t@x.io")
    claimed = s.claim_next_command()
    assert claimed["id"] == cid and claimed["status"] == "running"
    assert s.claim_next_command() is None  # already claimed
    s.finish_command(cid, status="done", result={"message": "ok"})
    assert s.get_command(cid)["status"] == "done"


def test_requeue_stale_running_commands(tmp_path):
    s = fresh(tmp_path)
    cid = s.enqueue_command("enroll_one", {"card_number": "5"})
    s.claim_next_command()  # -> running
    assert s.requeue_stale_running_commands() == 1
    assert s.claim_next_command()["id"] == cid  # available again


def test_device_status_roundtrip(tmp_path):
    s = fresh(tmp_path)
    assert s.get_device_status()["connected"] is False
    now = utcnow_iso()
    s.set_device_status(connected=True, last_seen=now, note="ok")
    got = s.get_device_status()
    assert got["connected"] is True and got["last_seen"] == now


def test_device_users_snapshot_roundtrip(tmp_path):
    s = fresh(tmp_path)
    users, at = s.get_device_users_snapshot()
    assert users == [] and at is None
    rows = [{"uid": 1, "card_number": "1001", "name": "Alice"}]
    s.set_device_users_snapshot(rows)
    users, at = s.get_device_users_snapshot()
    assert users == rows and at is not None
