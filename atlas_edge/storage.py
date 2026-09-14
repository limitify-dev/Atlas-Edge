"""Local SQLite store, shared by the listener and the web process.

Design notes
------------
* WAL mode + a generous ``busy_timeout`` let the two processes read/write
  concurrently at this (very low) event rate without a heavier database.
* Every call opens a short-lived connection. Simpler and safer across threads
  and processes than juggling one shared handle.
* ``event_queue.dedup_key`` is ``"<card_number>|<iso_timestamp>"`` and UNIQUE,
  so the live stream and the hourly onboard-log reconciliation can both insert
  the same tap and the second one is a harmless no-op.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Optional

SCHEMA = """
CREATE TABLE IF NOT EXISTS kv (
    key   TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS event_queue (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    card_number    TEXT NOT NULL,
    name           TEXT,
    occurred_at    TEXT NOT NULL,          -- ISO8601, as reported by the device
    source         TEXT NOT NULL,          -- 'live' | 'reconcile'
    dedup_key      TEXT NOT NULL UNIQUE,
    direction      TEXT,                   -- 'check_in' | 'check_out' | NULL (local-only, never sent to Atlas)
    status         TEXT NOT NULL DEFAULT 'pending',   -- pending | sent | failed
    attempts       INTEGER NOT NULL DEFAULT 0,
    next_attempt_at TEXT NOT NULL,
    last_error     TEXT,
    created_at     TEXT NOT NULL,
    sent_at        TEXT
);
CREATE INDEX IF NOT EXISTS idx_event_queue_due
    ON event_queue (status, next_attempt_at);

CREATE TABLE IF NOT EXISTS device_commands (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    kind        TEXT NOT NULL,             -- 'enroll_one' | 'bulk_sync'
    payload     TEXT NOT NULL DEFAULT '{}',
    status      TEXT NOT NULL DEFAULT 'queued',  -- queued | running | done | error
    progress    TEXT,                     -- human-readable running status
    result      TEXT,                     -- JSON summary once finished
    requested_by TEXT,
    created_at  TEXT NOT NULL,
    started_at  TEXT,
    finished_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_device_commands_status
    ON device_commands (status, id);

-- ── local-test mode only (ATLAS_EDGE_DEVICE_DRIVER=mock) ───────────────
-- Stand-ins for the F18's own tables so the whole pipeline runs on a laptop
-- with no hardware. Ignored entirely in real mode.
CREATE TABLE IF NOT EXISTS mock_device_users (
    uid     INTEGER PRIMARY KEY,
    user_id TEXT UNIQUE,               -- == card number
    name    TEXT,
    card    INTEGER
);
CREATE TABLE IF NOT EXISTS mock_taps (             -- inbox of simulated taps
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    card_number TEXT NOT NULL,
    occurred_at TEXT NOT NULL,
    consumed    INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS mock_attendance_log (   -- the "onboard log" (reconcile)
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    card_number TEXT NOT NULL,
    occurred_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS mock_atlas_events (     -- what the mock Atlas "received"
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    payload     TEXT NOT NULL,
    received_at TEXT NOT NULL
);
"""


def utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _within_debounce(
    conn: sqlite3.Connection, card_number: str, occurred_at: str, debounce_seconds: int
) -> bool:
    """True if this card's most recent few taps include one within
    ``debounce_seconds`` of ``occurred_at``. Compared as parsed instants, not
    strings, since ISO offsets aren't guaranteed to match byte-for-byte."""
    try:
        new_ts = datetime.fromisoformat(occurred_at)
    except ValueError:
        return False  # can't compare — let it through rather than silently drop it
    rows = conn.execute(
        "SELECT occurred_at FROM event_queue WHERE card_number=? "
        "ORDER BY id DESC LIMIT 5",
        (card_number,),
    ).fetchall()
    for row in rows:
        try:
            prev_ts = datetime.fromisoformat(row["occurred_at"])
            close = abs((new_ts - prev_ts).total_seconds()) < debounce_seconds
        except (ValueError, TypeError):
            continue  # unparsable, or one of the two timestamps is naive
        if close:
            return True
    return False


def _taps_today(conn: sqlite3.Connection, card_number: str, occurred_at: str) -> int:
    """How many of this card's recent taps fall on the same calendar day as
    ``occurred_at`` (compared in whatever offset it already carries — device
    taps all share one consistent offset, so this doesn't need its own
    timezone conversion). Atlas already treats a card's 3rd+ tap in a day as
    a no-op (first tap = check-in, next = check-out, rest ignored) — this
    just avoids spending a request finding that out."""
    try:
        day = datetime.fromisoformat(occurred_at).date()
    except ValueError:
        return 0
    rows = conn.execute(
        "SELECT occurred_at FROM event_queue WHERE card_number=? "
        "ORDER BY id DESC LIMIT 20",
        (card_number,),
    ).fetchall()
    count = 0
    for row in rows:
        try:
            if datetime.fromisoformat(row["occurred_at"]).date() == day:
                count += 1
        except ValueError:
            continue
    return count


class Storage:
    def __init__(self, db_path: Path | str) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)

    # ── connection plumbing ──────────────────────────────────────────────
    @contextmanager
    def _conn(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(
            self.db_path, timeout=30, isolation_level=None, check_same_thread=False
        )
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA busy_timeout=30000")
            conn.execute("PRAGMA foreign_keys=ON")
            yield conn
        finally:
            conn.close()

    def init_db(self) -> None:
        with self._conn() as conn:
            conn.executescript(SCHEMA)
            self._migrate(conn)

    def _migrate(self, conn: sqlite3.Connection) -> None:
        """Additive, idempotent column additions for databases created before
        a given field existed. ``CREATE TABLE IF NOT EXISTS`` above only
        covers a brand-new database — an existing one needs an ALTER TABLE."""
        cols = {row["name"] for row in conn.execute("PRAGMA table_info(event_queue)")}
        if "direction" not in cols:
            conn.execute("ALTER TABLE event_queue ADD COLUMN direction TEXT")

    # ── key/value (session token, device status, timers) ────────────────
    def get_kv(self, key: str, default: Optional[str] = None) -> Optional[str]:
        with self._conn() as conn:
            row = conn.execute("SELECT value FROM kv WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else default

    def set_kv(self, key: str, value: Optional[str]) -> None:
        with self._conn() as conn:
            if value is None:
                conn.execute("DELETE FROM kv WHERE key = ?", (key,))
            else:
                conn.execute(
                    "INSERT INTO kv (key, value) VALUES (?, ?) "
                    "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                    (key, value),
                )

    def get_json(self, key: str, default: Any = None) -> Any:
        raw = self.get_kv(key)
        return json.loads(raw) if raw is not None else default

    def set_json(self, key: str, value: Any) -> None:
        self.set_kv(key, json.dumps(value, separators=(",", ":")))

    # ── device status (written by the listener, read by the web UI) ─────
    def set_device_status(
        self, *, connected: bool, last_seen: Optional[str] = None, note: str = ""
    ) -> None:
        self.set_json(
            "device_status",
            {
                "connected": connected,
                "last_seen": last_seen,
                "note": note,
                "updated_at": utcnow_iso(),
            },
        )

    def get_device_status(self) -> dict:
        return self.get_json(
            "device_status", {"connected": False, "last_seen": None, "note": ""}
        )

    # ── wifi connect attempt (written by the /wifi-setup background thread,
    # polled by its own page) — never holds the submitted password. ───────
    def set_wifi_connect_state(self, state: dict) -> None:
        self.set_json("wifi_connect_state", state)

    def get_wifi_connect_state(self) -> dict:
        return self.get_json(
            "wifi_connect_state",
            {"ssid": None, "status": "idle", "message": "", "started_at": None, "finished_at": None},
        )

    # ── device users snapshot (populated by the "list_users" command) ──
    def set_device_users_snapshot(self, users: list[dict]) -> None:
        self.set_json("device_users_snapshot", users)
        self.set_kv("device_users_snapshot_at", utcnow_iso())

    def get_device_users_snapshot(self) -> tuple[list[dict], Optional[str]]:
        return self.get_json("device_users_snapshot", []), self.get_kv(
            "device_users_snapshot_at"
        )

    # ── event queue ────────────────────────────────────────────────────
    def enqueue_event(
        self,
        *,
        card_number: str,
        name: Optional[str],
        occurred_at: str,
        source: str,
        direction: Optional[str] = None,
        debounce_seconds: int = 0,
        max_taps_per_day: int = 0,
    ) -> bool:
        """Insert a tap. Returns True if it was new, False if it's a dupe —
        either the exact same (card, timestamp) already queued/sent (the live
        stream and reconciliation both reporting one tap), the same card
        tapping again too soon after its own last tap to be a real second
        event when ``debounce_seconds`` > 0 (a fumbled double-tap), or,
        when ``max_taps_per_day`` > 0, a tap beyond that many already
        recorded for this card today — Atlas only ever acts on the first two
        (check-in, check-out) and no-ops the rest, so there's nothing to gain
        from sending them."""
        dedup_key = f"{card_number}|{occurred_at}"
        now = utcnow_iso()
        with self._conn() as conn:
            if debounce_seconds > 0 and _within_debounce(
                conn, card_number, occurred_at, debounce_seconds
            ):
                return False
            if max_taps_per_day > 0 and _taps_today(conn, card_number, occurred_at) >= max_taps_per_day:
                return False
            cur = conn.execute(
                "INSERT OR IGNORE INTO event_queue "
                "(card_number, name, occurred_at, source, direction, dedup_key, "
                " next_attempt_at, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (card_number, name, occurred_at, source, direction, dedup_key, now, now),
            )
            return cur.rowcount == 1

    def due_events(self, limit: int = 100) -> list[sqlite3.Row]:
        now = utcnow_iso()
        with self._conn() as conn:
            return conn.execute(
                "SELECT * FROM event_queue "
                "WHERE status IN ('pending', 'failed') AND next_attempt_at <= ? "
                "ORDER BY id ASC LIMIT ?",
                (now, limit),
            ).fetchall()

    def mark_event_sent(self, event_id: int) -> None:
        with self._conn() as conn:
            conn.execute(
                "UPDATE event_queue SET status='sent', sent_at=?, last_error=NULL "
                "WHERE id=?",
                (utcnow_iso(), event_id),
            )

    def mark_event_failed(self, event_id: int, error: str, next_attempt_at: str) -> None:
        with self._conn() as conn:
            conn.execute(
                "UPDATE event_queue SET status='failed', attempts=attempts+1, "
                "last_error=?, next_attempt_at=? WHERE id=?",
                (error[:500], next_attempt_at, event_id),
            )

    def delete_event(self, event_id: int) -> None:
        """Drop an event outright — no retry, no record kept. For a tap that
        will never succeed no matter how many times it's retried (e.g. the
        card is unknown to Atlas), rather than one that just failed once."""
        with self._conn() as conn:
            conn.execute("DELETE FROM event_queue WHERE id=?", (event_id,))

    def queue_stats(self) -> dict:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT status, COUNT(*) c FROM event_queue GROUP BY status"
            ).fetchall()
        stats = {r["status"]: r["c"] for r in rows}
        stats["pending_total"] = stats.get("pending", 0) + stats.get("failed", 0)
        return stats

    def recent_events(self, limit: int = 20) -> list[sqlite3.Row]:
        with self._conn() as conn:
            return conn.execute(
                "SELECT * FROM event_queue ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()

    # ── device command queue (web enqueues, listener executes) ─────────
    def enqueue_command(self, kind: str, payload: dict, requested_by: str = "") -> int:
        with self._conn() as conn:
            cur = conn.execute(
                "INSERT INTO device_commands (kind, payload, requested_by, created_at) "
                "VALUES (?, ?, ?, ?)",
                (kind, json.dumps(payload), requested_by, utcnow_iso()),
            )
            return int(cur.lastrowid)

    def claim_next_command(self) -> Optional[sqlite3.Row]:
        """Atomically move the oldest queued command to 'running' and return it."""
        with self._conn() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT * FROM device_commands WHERE status='queued' "
                "ORDER BY id ASC LIMIT 1"
            ).fetchone()
            if row is None:
                conn.execute("COMMIT")
                return None
            conn.execute(
                "UPDATE device_commands SET status='running', started_at=? WHERE id=?",
                (utcnow_iso(), row["id"]),
            )
            conn.execute("COMMIT")
        return self.get_command(int(row["id"]))

    def update_command_progress(self, command_id: int, progress: str) -> None:
        with self._conn() as conn:
            conn.execute(
                "UPDATE device_commands SET progress=? WHERE id=?",
                (progress[:500], command_id),
            )

    def finish_command(
        self, command_id: int, *, status: str, result: dict, progress: str = ""
    ) -> None:
        with self._conn() as conn:
            conn.execute(
                "UPDATE device_commands SET status=?, result=?, progress=?, "
                "finished_at=? WHERE id=?",
                (status, json.dumps(result), progress, utcnow_iso(), command_id),
            )

    def get_command(self, command_id: int) -> Optional[sqlite3.Row]:
        with self._conn() as conn:
            return conn.execute(
                "SELECT * FROM device_commands WHERE id=?", (command_id,)
            ).fetchone()

    def recent_commands(self, limit: int = 15) -> list[sqlite3.Row]:
        with self._conn() as conn:
            return conn.execute(
                "SELECT * FROM device_commands ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()

    def requeue_stale_running_commands(self) -> int:
        """On listener startup, a command left 'running' means we crashed mid-run.

        Bulk sync / enroll are idempotent, so it's safe to run it again.
        """
        with self._conn() as conn:
            cur = conn.execute(
                "UPDATE device_commands SET status='queued', progress='re-queued after "
                "restart' WHERE status='running'"
            )
            return cur.rowcount

    # ── local-test mode: mock device + mock Atlas state ─────────────────
    def mock_get_users(self) -> list[sqlite3.Row]:
        with self._conn() as conn:
            return conn.execute(
                "SELECT uid, user_id, name, card FROM mock_device_users ORDER BY uid"
            ).fetchall()

    def mock_upsert_user(self, *, uid: int, user_id: str, name: str, card: int) -> None:
        """Write by uid — the real identity (pyzk keys on uid too), so this
        also handles editing an existing slot onto a different card number,
        not just the common "same card, new name" update."""
        with self._conn() as conn:
            cur = conn.execute(
                "UPDATE mock_device_users SET user_id=?, name=?, card=? WHERE uid=?",
                (user_id, name, card, uid),
            )
            if cur.rowcount == 0:
                conn.execute(
                    "INSERT INTO mock_device_users (uid, user_id, name, card) "
                    "VALUES (?, ?, ?, ?)",
                    (uid, user_id, name, card),
                )

    def mock_delete_user(self, uid: int) -> bool:
        with self._conn() as conn:
            cur = conn.execute("DELETE FROM mock_device_users WHERE uid=?", (uid,))
            return cur.rowcount > 0

    def mock_enqueue_tap(self, *, card_number: str, occurred_at: str) -> None:
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO mock_taps (card_number, occurred_at) VALUES (?, ?)",
                (card_number, occurred_at),
            )
            conn.execute(
                "INSERT INTO mock_attendance_log (card_number, occurred_at) VALUES (?, ?)",
                (card_number, occurred_at),
            )

    def mock_pop_taps(self, limit: int = 50) -> list[sqlite3.Row]:
        with self._conn() as conn:
            conn.execute("BEGIN IMMEDIATE")
            rows = conn.execute(
                "SELECT id, card_number, occurred_at FROM mock_taps "
                "WHERE consumed = 0 ORDER BY id ASC LIMIT ?",
                (limit,),
            ).fetchall()
            if rows:
                ids = tuple(r["id"] for r in rows)
                conn.execute(
                    f"UPDATE mock_taps SET consumed = 1 WHERE id IN "
                    f"({','.join('?' * len(ids))})",
                    ids,
                )
            conn.execute("COMMIT")
        return rows

    def mock_attendance_since(self, since_iso: Optional[str]) -> list[sqlite3.Row]:
        with self._conn() as conn:
            if since_iso:
                return conn.execute(
                    "SELECT card_number, occurred_at FROM mock_attendance_log "
                    "WHERE occurred_at > ? ORDER BY occurred_at",
                    (since_iso,),
                ).fetchall()
            return conn.execute(
                "SELECT card_number, occurred_at FROM mock_attendance_log "
                "ORDER BY occurred_at"
            ).fetchall()

    def mock_record_atlas_event(self, payload: dict) -> None:
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO mock_atlas_events (payload, received_at) VALUES (?, ?)",
                (json.dumps(payload), utcnow_iso()),
            )

    def mock_recent_atlas_events(self, limit: int = 20) -> list[sqlite3.Row]:
        with self._conn() as conn:
            return conn.execute(
                "SELECT * FROM mock_atlas_events ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()

    def mock_atlas_event_count(self) -> int:
        with self._conn() as conn:
            return int(
                conn.execute("SELECT COUNT(*) c FROM mock_atlas_events").fetchone()["c"]
            )
