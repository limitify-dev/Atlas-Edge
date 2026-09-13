"""In-process simulators for local testing without a Pi or an F18.

Selected via ``ATLAS_EDGE_DEVICE_DRIVER=mock`` / ``ATLAS_EDGE_ATLAS_DRIVER=mock``
(or the ``auto`` heuristics in :class:`atlas_edge.config.Settings`). Both mirror
the exact public surface the listener/web use, so nothing else changes.

* :class:`MockF18` keeps its "device user table" and "onboard log" in the same
  SQLite file (``mock_*`` tables). Taps are injected from the web UI
  ("Simulate a tap") — they land in ``mock_taps`` and the listener picks them up
  just like a real ``live_capture()`` stream.
* :class:`MockAtlasClient` accepts any non-empty login, records pushed events in
  ``mock_atlas_events`` (visible in the UI), and reads the card list from a
  JSON file so "Sync from Atlas" does something real.
"""

from __future__ import annotations

import json
import logging
import random
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Iterator, Optional

from .atlas_client import AtlasAuthError, AtlasError, CardAssignment, SqliteTokenStore
from .device import DeviceUnavailable, Tap

log = logging.getLogger("atlas_edge.mock")


@dataclass
class MockUser:
    uid: int
    user_id: str
    name: str
    card: int


_SAMPLE_ASSIGNMENTS = [
    {"student_id": "stu-001", "name": "Aline Uwase", "card_number": "1001"},
    {"student_id": "stu-002", "name": "Bosco Habimana", "card_number": "1002"},
    {"student_id": "stu-003", "name": "Chantal Ingabire", "card_number": "1003"},
    {"student_id": "stu-004", "name": "David Nshuti", "card_number": "1004"},
]


# ── mock device ───────────────────────────────────────────────────────────
class MockF18:
    def __init__(self, storage, *, poll_seconds: float = 0.5) -> None:
        self._s = storage
        self._poll = poll_seconds
        self._connected = False

    # lifecycle — always "reachable"
    def connect(self) -> None:
        self._connected = True
        log.info("MockF18 connected (no hardware)")

    def disconnect(self) -> None:
        self._connected = False

    def is_alive(self) -> bool:
        return self._connected

    # users
    def get_users(self) -> list:
        return [
            MockUser(
                uid=int(r["uid"]),
                user_id=str(r["user_id"]),
                name=r["name"] or "",
                card=int(r["card"] or 0),
            )
            for r in self._s.mock_get_users()
        ]

    def build_name_map(self) -> dict:
        return {u.user_id: u.name for u in self.get_users() if u.user_id}

    def set_user(self, *, uid: int, card_number: str, name: str) -> None:
        card_int = int(card_number) if str(card_number).isdigit() else 0
        self._s.mock_upsert_user(
            uid=int(uid), user_id=str(card_number), name=name[:24], card=card_int
        )

    def bulk_write(self, writes, on_progress=None) -> tuple:
        succeeded: list = []
        failed: list = []
        writes = list(writes)
        for i, w in enumerate(writes, start=1):
            try:
                self.set_user(uid=w.uid, card_number=w.card_number, name=w.name)
                succeeded.append(w.card_number)
            except Exception as exc:  # noqa: BLE001
                failed.append((w.card_number, str(exc)))
            if on_progress:
                on_progress(i, len(writes), w.card_number)
        return succeeded, failed

    def delete_user(self, uid: int) -> None:
        if not self._s.mock_delete_user(int(uid)):
            raise DeviceUnavailable(f"delete_user({uid}) failed: no such uid")

    def bulk_delete(self, uids, on_progress=None) -> tuple:
        succeeded: list = []
        failed: list = []
        uids = list(uids)
        for i, uid in enumerate(uids, start=1):
            try:
                self.delete_user(uid)
                succeeded.append(uid)
            except Exception as exc:  # noqa: BLE001
                failed.append((uid, str(exc)))
            if on_progress:
                on_progress(i, len(uids), uid)
        return succeeded, failed

    # attendance
    def live_events(self, tick_seconds: int = 10) -> Iterator[Optional[Tap]]:
        if not self._connected:
            raise DeviceUnavailable("MockF18 not connected")
        last_tick = time.monotonic()
        while self._connected:
            for r in self._s.mock_pop_taps():
                yield Tap(card_number=str(r["card_number"]), timestamp=str(r["occurred_at"]))
            now = time.monotonic()
            if now - last_tick >= tick_seconds:
                last_tick = now
                yield None
            time.sleep(self._poll)

    def get_attendance_since(self, since: Optional[datetime]) -> list:
        since_iso = since.isoformat() if since is not None else None
        return [
            Tap(card_number=str(r["card_number"]), timestamp=str(r["occurred_at"]))
            for r in self._s.mock_attendance_since(since_iso)
        ]


# ── mock Atlas ────────────────────────────────────────────────────────────
class MockAtlasClient:
    def __init__(self, storage, *, assignments_path, fail_rate: float = 0.0) -> None:
        self._s = storage
        self._store = SqliteTokenStore(storage)
        self._assignments_path = assignments_path
        self._fail_rate = max(0.0, min(1.0, fail_rate))

    def close(self) -> None:  # parity with AtlasClient
        pass

    def is_authenticated(self) -> bool:
        return self._store.get_token() is not None

    def is_device_registered(self) -> bool:
        # No real Atlas to register with in mock mode — pushes just work.
        return True

    def heartbeat(self) -> None:  # parity with AtlasClient — no-op in mock mode
        pass

    def login(self, identifier: str, password: str) -> None:
        if not identifier.strip() or not password:
            raise AtlasAuthError("Mock Atlas: enter any email and password.")
        self._store.save(token=f"mock-{identifier.strip()}", refresh=None, expires_at=None)
        log.info("MockAtlas login accepted for %s", identifier)

    def logout(self) -> None:
        self._store.clear()

    def push_attendance_event(
        self, *, card_number: str, name: Optional[str], timestamp: str
    ) -> None:
        if not self.is_authenticated():
            raise AtlasAuthError("Mock Atlas: not logged in.")
        from .atlas_client import AtlasPushError

        if self._fail_rate and random.random() < self._fail_rate:
            raise AtlasPushError("Mock Atlas: simulated transient failure (will retry).")
        self._s.mock_record_atlas_event(
            {
                "card_number": card_number,
                "name": name,
                "timestamp": timestamp,
                "received_at": _now_iso(),
            }
        )

    def get_card_assignments(self) -> list:
        if not self.is_authenticated():
            raise AtlasAuthError("Mock Atlas: not logged in.")
        path = self._assignments_path
        try:
            raw = json.loads(path.read_text()) if path.exists() else None
        except (OSError, ValueError) as exc:
            raise AtlasError(f"Mock Atlas: bad {path}: {exc}") from exc
        if raw is None:
            log.warning(
                "Mock Atlas: %s not found — returning a 4-row sample. "
                "Create that file to control the card list.",
                path,
            )
            raw = _SAMPLE_ASSIGNMENTS
        rows = raw["assignments"] if isinstance(raw, dict) else raw
        return [
            CardAssignment(
                student_id=str(r.get("student_id", "")),
                name=r.get("name"),
                card_number=r.get("card_number"),
            )
            for r in (rows or [])
        ]


def _now_iso() -> str:
    from datetime import timezone

    return datetime.now(timezone.utc).isoformat()
