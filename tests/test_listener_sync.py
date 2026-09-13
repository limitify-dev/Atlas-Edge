"""Orchestration layer: the listener turning a plan into device writes,
including partial failure and a clean idempotent re-run.

Needs pydantic-settings (pulled in by `pip install -r requirements.txt` or
`-r requirements-dev.txt`); skipped otherwise so the minimal test env still runs.
"""

import types

import pytest

pytest.importorskip("pydantic_settings")

from atlas_edge.atlas_client import CardAssignment  # noqa: E402
from atlas_edge.listener import Listener  # noqa: E402
from atlas_edge.storage import Storage  # noqa: E402


class FakeUser:
    def __init__(self, uid, user_id, name):
        self.uid = uid
        self.user_id = user_id
        self.name = name
        self.card = int(user_id) if str(user_id).isdigit() else 0


class FakeDevice:
    """Minimal stand-in for F18Device: an in-memory user table."""

    def __init__(self, users=None, fail_cards=frozenset()):
        self.users = list(users or [])
        self.fail_cards = set(fail_cards)
        self.writes = []

    def get_users(self):
        return list(self.users)

    def build_name_map(self):
        return {str(u.user_id): u.name for u in self.users}

    def set_user(self, *, uid, card_number, name):
        if card_number in self.fail_cards:
            from atlas_edge.device import DeviceUnavailable

            raise DeviceUnavailable(f"write rejected for {card_number}")
        self.writes.append((uid, card_number, name))
        self.users = [u for u in self.users if str(u.user_id) != str(card_number)]
        self.users.append(FakeUser(uid, str(card_number), name))

    def bulk_write(self, writes, on_progress=None):
        ok, failed = [], []
        for i, w in enumerate(list(writes), start=1):
            try:
                self.set_user(uid=w.uid, card_number=w.card_number, name=w.name)
                ok.append(w.card_number)
            except Exception as exc:  # noqa: BLE001
                failed.append((w.card_number, str(exc)))
            if on_progress:
                on_progress(i, 0, w.card_number)
        return ok, failed


class FakeAtlas:
    def __init__(self, assignments=None, unknown_cards=frozenset()):
        self._assignments = assignments or []
        self._unknown_cards = set(unknown_cards)
        self.heartbeats = 0
        self.pushed = []

    def get_card_assignments(self):
        return self._assignments

    def is_authenticated(self):
        return True

    def is_device_registered(self):
        return True

    def heartbeat(self):
        self.heartbeats += 1

    def push_attendance_event(self, *, card_number, name, timestamp):
        if card_number in self._unknown_cards:
            from atlas_edge.atlas_client import AtlasCardUnknownError

            raise AtlasCardUnknownError(f"Card {card_number} is not known to Atlas.")
        self.pushed.append((card_number, name, timestamp))

    def close(self):
        pass


def make_listener(tmp_path, device, atlas):
    storage = Storage(tmp_path / "l.sqlite3")
    storage.init_db()
    settings = types.SimpleNamespace(
        f18_host="x", api_root="http://x", name_map_refresh_seconds=300,
        reconcile_interval_seconds=3600,
    )
    return Listener(settings, storage, atlas, device), storage


def test_bulk_sync_enrolls_updates_and_skips_bad_rows(tmp_path):
    device = FakeDevice(users=[FakeUser(1, "1001", "Alice")])
    atlas = FakeAtlas(
        [
            CardAssignment("s1", "Alice", "1001"),        # unchanged
            CardAssignment("s2", "Bob", "1002"),          # add
            CardAssignment("s3", "", "1003"),             # skipped (no name)
            CardAssignment("s4", "Dee", "1002"),          # dup card -> last wins
        ]
    )
    listener, _ = make_listener(tmp_path, device, atlas)

    result = listener._cmd_bulk_sync(command_id=1)

    assert result["ok"] is True
    assert result["enrolled"] == 1
    assert result["updated"] == 0
    assert result["unchanged"] == 1
    assert len(result["skipped"]) == 2          # empty name + duplicate
    assert result["failed"] == []
    # "Dee" (last write for card 1002) is what landed on the device
    assert ("1002", "Dee") in [(c, n) for _, c, n in device.writes]

    # ── second run is a no-op ──
    again = listener._cmd_bulk_sync(command_id=2)
    assert again["enrolled"] == 0 and again["updated"] == 0
    assert again["ok"] is True
    assert "already match" in again["message"]


def test_bulk_sync_reports_partial_failure(tmp_path):
    device = FakeDevice(fail_cards={"2002"})
    atlas = FakeAtlas(
        [CardAssignment("s1", "A", "1001"), CardAssignment("s2", "B", "2002")]
    )
    listener, _ = make_listener(tmp_path, device, atlas)

    result = listener._cmd_bulk_sync(command_id=1)

    assert result["ok"] is False
    assert result["enrolled"] == 1
    assert [f["card_number"] for f in result["failed"]] == ["2002"]
    assert "1 failed" in result["message"]


def test_enroll_one_is_update_when_card_exists(tmp_path):
    device = FakeDevice(users=[FakeUser(7, "555", "Old Label")])
    listener, _ = make_listener(tmp_path, device, FakeAtlas([]))

    r1 = listener._cmd_enroll_one(1, {"card_number": "555", "label": "New Label"})
    assert r1 == {
        "ok": True,
        "action": "updated",
        "card_number": "555",
        "name": "New Label",
        "uid": 7,
    }

    r2 = listener._cmd_enroll_one(2, {"card_number": "999", "label": "Fresh"})
    assert r2["action"] == "enrolled" and r2["uid"] != 7


def test_enroll_one_requires_card_number(tmp_path):
    listener, _ = make_listener(tmp_path, FakeDevice(), FakeAtlas([]))
    r = listener._cmd_enroll_one(1, {"card_number": "  ", "label": "x"})
    assert r["ok"] is False and "required" in r["error"]


def test_enroll_bulk_from_pasted_rows(tmp_path):
    device = FakeDevice(users=[FakeUser(1, "1001", "Alice")])
    listener, _ = make_listener(tmp_path, device, FakeAtlas([]))
    rows = [
        {"card_number": "1001", "name": "Alice"},        # unchanged
        {"card_number": "1002", "name": "Bob"},          # add
        {"card_number": "1003", "name": ""},             # skipped (no name)
    ]
    r = listener._cmd_enroll_bulk(1, {"rows": rows})
    assert r["ok"] is True
    assert r["enrolled"] == 1 and r["updated"] == 0 and r["unchanged"] == 1
    assert len(r["skipped"]) == 1
    assert ("1002", "Bob") in [(c, n) for _, c, n in device.writes]

    # re-run = nothing to write
    again = listener._cmd_enroll_bulk(2, {"rows": rows})
    assert again["enrolled"] == 0 and "already match" in again["message"]


def test_enroll_bulk_rejects_empty_and_all_bad(tmp_path):
    listener, _ = make_listener(tmp_path, FakeDevice(), FakeAtlas([]))
    assert listener._cmd_enroll_bulk(1, {"rows": []})["ok"] is False
    r = listener._cmd_enroll_bulk(2, {"rows": [{"card_number": "", "name": "x"}]})
    assert r["ok"] is False and "usable" in r["error"]


def test_list_users_reads_device_and_caches_snapshot(tmp_path):
    device = FakeDevice(
        users=[FakeUser(2, "1002", "Bob"), FakeUser(1, "1001", "Alice")]
    )
    listener, storage = make_listener(tmp_path, device, FakeAtlas([]))
    r = listener._cmd_list_users(1)
    assert r == {"ok": True, "count": 2, "message": "2 user(s) on the device."}
    snapshot, fetched_at = storage.get_device_users_snapshot()
    assert fetched_at is not None
    # sorted by uid
    assert snapshot == [
        {"uid": 1, "card_number": "1001", "name": "Alice"},
        {"uid": 2, "card_number": "1002", "name": "Bob"},
    ]
    assert device.writes == []  # a pure read never writes to the device


def test_flush_sends_a_heartbeat_while_the_device_is_connected(tmp_path):
    atlas = FakeAtlas()
    listener, storage = make_listener(tmp_path, FakeDevice(), atlas)
    storage.set_device_status(connected=True)
    listener._flush_once()
    assert atlas.heartbeats == 1


def test_flush_skips_the_heartbeat_while_the_device_is_disconnected(tmp_path):
    atlas = FakeAtlas()
    listener, storage = make_listener(tmp_path, FakeDevice(), atlas)
    storage.set_device_status(connected=False)
    listener._flush_once()
    assert atlas.heartbeats == 0


def test_flush_drops_a_tap_for_a_card_unknown_to_atlas_without_retrying(tmp_path):
    atlas = FakeAtlas(unknown_cards={"9999"})
    listener, storage = make_listener(tmp_path, FakeDevice(), atlas)
    storage.set_device_status(connected=True)
    storage.enqueue_event(
        card_number="9999", name=None, occurred_at="2026-01-01T08:00:00+00:00",
        source="live",
    )
    assert storage.queue_stats()["pending_total"] == 1

    listener._flush_once()

    # gone outright — not marked failed/pending for another attempt
    assert storage.queue_stats().get("pending_total", 0) == 0
    assert storage.queue_stats().get("failed", 0) == 0
    assert atlas.pushed == []  # the fake never recorded it as sent either
