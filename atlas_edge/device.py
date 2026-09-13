"""Thin wrapper around :mod:`pyzk` for one ZKTeco F18.

Only the listener process imports this — it is the sole owner of the device
connection. ``pyzk`` is imported lazily so the rest of the codebase (and the
test suite) doesn't need it installed.

Why the name is looked up here and not taken from the tap
--------------------------------------------------------
An F18 attendance record (`live_capture()` / `get_attendance()`) is a compact
log line: ``user_id`` (which we keep equal to the card number) + timestamp +
status. It does **not** carry the person's name, even though the name is stored
on the device against that same ``user_id`` in a separate users table. So we
build a ``{card_number: name}`` map from ``get_users()`` once, and rebuild it
after every enrollment sync (that's the only time the device users change).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Iterator, Optional
from zoneinfo import ZoneInfo

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Tap:
    card_number: str
    timestamp: str  # ISO8601 string, ready to send to Atlas


class DeviceUnavailable(RuntimeError):
    """The F18 could not be reached / the socket is dead."""


class F18Device:
    def __init__(
        self,
        *,
        host: str,
        port: int = 4370,
        password: int = 0,
        timeout: int = 15,
        force_udp: bool = False,
        device_timezone: str = "",
    ) -> None:
        self._host = host
        self._port = port
        self._password = password
        self._timeout = timeout
        self._force_udp = force_udp
        self._tz = ZoneInfo(device_timezone) if device_timezone else None
        self._zk = None
        self._conn = None

    # ── lifecycle ──────────────────────────────────────────────────────
    def connect(self) -> None:
        try:
            from zk import ZK  # lazy: pyzk only needed on the Pi
        except ImportError as exc:  # pragma: no cover
            raise DeviceUnavailable(
                "pyzk is not installed. Run `pip install pyzk` on the Pi."
            ) from exc

        self.disconnect()
        self._zk = ZK(
            self._host,
            port=self._port,
            timeout=self._timeout,
            password=self._password,
            force_udp=self._force_udp,
            ommit_ping=True,
        )
        try:
            self._conn = self._zk.connect()
        except Exception as exc:  # pyzk raises a grab-bag of exception types
            self._conn = None
            raise DeviceUnavailable(f"F18 connect failed: {exc}") from exc
        log.info("Connected to F18 at %s:%s", self._host, self._port)

    def disconnect(self) -> None:
        if self._conn is not None:
            try:
                self._conn.disconnect()
            except Exception:  # pragma: no cover - best effort
                pass
        self._conn = None

    def is_alive(self) -> bool:
        if self._conn is None:
            return False
        try:
            # get_time() is a cheap round-trip; if the socket is dead it raises.
            self._conn.get_time()
            return True
        except Exception:
            return False

    def _require(self):
        if self._conn is None:
            raise DeviceUnavailable("Not connected to the F18.")
        return self._conn

    # ── users ──────────────────────────────────────────────────────────
    def get_users(self) -> list:
        """Returns pyzk ``User`` objects (uid, user_id, name, card, ...)."""
        try:
            return list(self._require().get_users())
        except Exception as exc:
            raise DeviceUnavailable(f"get_users failed: {exc}") from exc

    def build_name_map(self) -> dict:
        """``{card_number(str): name(str)}`` from the device's own user table."""
        out: dict = {}
        for u in self.get_users():
            key = str(getattr(u, "user_id", "") or getattr(u, "card", "") or "").strip()
            if key:
                out[key] = getattr(u, "name", "") or ""
        return out

    def set_user(self, *, uid: int, card_number: str, name: str) -> None:
        card_int = int(card_number) if str(card_number).isdigit() else 0
        try:
            self._require().set_user(
                uid=int(uid),
                name=name[:24],           # F18 name field is short
                privilege=0,
                password="",
                group_id="",
                user_id=str(card_number),  # keep user_id == card number
                card=card_int,
            )
        except Exception as exc:
            raise DeviceUnavailable(f"set_user({card_number}) failed: {exc}") from exc

    def bulk_write(self, writes, on_progress=None) -> tuple:
        """Apply an iterable of objects with ``.uid/.card_number/.name``.

        Returns ``(succeeded: list[str], failed: list[(card_number, error)])``.
        Idempotent: ``set_user`` on an existing uid updates it.
        """
        conn = self._require()
        succeeded: list = []
        failed: list = []
        try:
            conn.disable_device()  # steadier during a batch of writes
        except Exception:  # pragma: no cover
            pass
        try:
            total = len(list(writes)) if hasattr(writes, "__len__") else 0
            for i, w in enumerate(writes, start=1):
                try:
                    self.set_user(uid=w.uid, card_number=w.card_number, name=w.name)
                    succeeded.append(w.card_number)
                except DeviceUnavailable as exc:
                    failed.append((w.card_number, str(exc)))
                if on_progress:
                    on_progress(i, total, w.card_number)
        finally:
            try:
                conn.enable_device()
            except Exception:  # pragma: no cover
                pass
        return succeeded, failed

    def delete_user(self, uid: int) -> None:
        try:
            self._require().delete_user(uid=int(uid))
        except Exception as exc:
            raise DeviceUnavailable(f"delete_user({uid}) failed: {exc}") from exc

    def bulk_delete(self, uids, on_progress=None) -> tuple:
        """Delete an iterable of device uids.

        Returns ``(succeeded: list[int], failed: list[(uid, error)])``.
        """
        conn = self._require()
        succeeded: list = []
        failed: list = []
        try:
            conn.disable_device()
        except Exception:  # pragma: no cover
            pass
        try:
            uids = list(uids)
            total = len(uids)
            for i, uid in enumerate(uids, start=1):
                try:
                    self.delete_user(uid)
                    succeeded.append(uid)
                except DeviceUnavailable as exc:
                    failed.append((uid, str(exc)))
                if on_progress:
                    on_progress(i, total, uid)
        finally:
            try:
                conn.enable_device()
            except Exception:  # pragma: no cover
                pass
        return succeeded, failed

    # ── attendance ─────────────────────────────────────────────────────
    def _to_iso(self, ts: datetime) -> str:
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=self._tz) if self._tz else ts.replace(
                tzinfo=timezone.utc
            )
        return ts.isoformat()

    def live_events(self, tick_seconds: int = 10) -> Iterator[Optional[Tap]]:
        """Yield a :class:`Tap` per card tap. Yields ``None`` roughly every
        ``tick_seconds`` so the caller can do housekeeping (heartbeat, name-map
        refresh, command queue) between taps. Raises :class:`DeviceUnavailable`
        if the stream dies — the caller reconnects."""
        conn = self._require()
        try:
            for att in conn.live_capture(new_timeout=tick_seconds):
                if att is None:
                    yield None
                    continue
                yield Tap(
                    card_number=str(att.user_id).strip(),
                    timestamp=self._to_iso(att.timestamp),
                )
        except Exception as exc:
            raise DeviceUnavailable(f"live_capture ended: {exc}") from exc

    def get_attendance_since(self, since: Optional[datetime]) -> list:
        """Read the F18's onboard log for reconciliation. Returns Taps newer
        than ``since`` (device-local comparison)."""
        try:
            records = self._require().get_attendance() or []
        except Exception as exc:
            raise DeviceUnavailable(f"get_attendance failed: {exc}") from exc
        out: list = []
        for att in records:
            ts = att.timestamp
            if since is not None:
                cmp_since = since
                if ts.tzinfo is None and since.tzinfo is not None:
                    cmp_since = since.replace(tzinfo=None)
                if ts.tzinfo is not None and since.tzinfo is None:
                    ts_cmp = ts.replace(tzinfo=None)
                else:
                    ts_cmp = ts
                if ts_cmp <= cmp_since:
                    continue
            out.append(
                Tap(card_number=str(att.user_id).strip(), timestamp=self._to_iso(ts))
            )
        return out
