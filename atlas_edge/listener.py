"""The background service — the only process that touches the F18.

Threads
-------
``_device_loop``
    Owns the F18 connection. Streams taps into the local queue; between taps
    (every housekeeping tick) it refreshes the name map when it's stale or the
    web UI flagged an enrollment change, runs the hourly onboard-log
    reconciliation, and drains the device-command queue (single enroll / bulk
    sync requested from the web UI). Reconnects with capped backoff whenever the
    stream or a device call dies.

``_flush_loop``
    Drains the local event queue to Atlas with exponential backoff. Never
    touches the device. If the Atlas session is gone it simply waits — the
    events stay buffered until someone logs in again through the web UI.

Run:  ``python -m atlas_edge.listener``   (systemd unit: atlas-edge-listener)
"""

from __future__ import annotations

import json
import logging
import signal
import threading
import time
from datetime import datetime, timezone

from .atlas_client import AtlasAuthError, AtlasCardUnknownError, AtlasError, AtlasPushError
from .attendance import classify_direction
from .config import Settings, get_settings
from .device import DeviceUnavailable
from .drivers import build_atlas_client, build_device
from .enrollment import compute_sync_plan, DeviceUser, normalize_assignments
from .storage import Storage, utcnow_iso

log = logging.getLogger("atlas_edge.listener")


class Listener:
    def __init__(
        self,
        settings: Settings,
        storage: Storage,
        atlas,          # AtlasClient | MockAtlasClient (same surface)
        device,         # F18Device | MockF18 (same surface)
    ) -> None:
        self.s = settings
        self.db = storage
        self.atlas = atlas
        self.dev = device
        self._stop = threading.Event()
        self._name_map: dict = {}
        self._name_map_built_at = 0.0
        # Read from the DB in run(), after init_db() — __init__ must not assume
        # the schema exists (a first run on a fresh machine gets here first).
        self._last_users_version = "0"

    # ── public ─────────────────────────────────────────────────────────
    def run(self) -> None:
        self.db.init_db()
        self._last_users_version = self.db.get_kv("device_users_version", "0")
        requeued = self.db.requeue_stale_running_commands()
        if requeued:
            log.info("Re-queued %d command(s) left running after a restart", requeued)

        threads = [
            threading.Thread(target=self._device_loop, name="device", daemon=True),
            threading.Thread(target=self._flush_loop, name="flush", daemon=True),
        ]
        for t in threads:
            t.start()
        if self.s.effective_device_driver == "mock":
            log.info("Listener started in MOCK mode — no F18, Atlas events recorded locally")
        else:
            log.info(
                "Listener started (F18 %s:%s, atlas=%s)",
                self.s.f18_host, self.s.f18_port, self.s.api_root,
            )
        try:
            while not self._stop.is_set():
                self._stop.wait(1.0)
        finally:
            for t in threads:
                t.join(timeout=5)
            self.dev.disconnect()
            self.atlas.close()
            log.info("Listener stopped")

    def stop(self, *_a) -> None:
        self._stop.set()

    # ── device loop ────────────────────────────────────────────────────
    def _device_loop(self) -> None:
        backoff = self.s.reconnect_backoff_start_seconds
        while not self._stop.is_set():
            try:
                self.dev.connect()
                self._rebuild_name_map(force=True)
                self.db.set_device_status(connected=True, last_seen=utcnow_iso())
                backoff = self.s.reconnect_backoff_start_seconds

                for tap in self.dev.live_events(self.s.housekeeping_tick_seconds):
                    if self._stop.is_set():
                        break
                    if tap is None:
                        self._housekeeping()
                        continue
                    self._handle_tap(tap)
            except DeviceUnavailable as exc:
                self.db.set_device_status(
                    connected=False, last_seen=self.db.get_device_status().get("last_seen"),
                    note=str(exc),
                )
                log.warning("Device unavailable: %s — reconnecting in %ds", exc, backoff)
            except Exception:  # keep the loop alive no matter what
                log.exception("Unexpected error in device loop — reconnecting in %ds", backoff)
                self.db.set_device_status(connected=False, note="internal error")
            finally:
                self.dev.disconnect()

            if self._stop.wait(backoff):
                break
            backoff = min(backoff * 2, self.s.reconnect_backoff_max_seconds)

    def _classify(self, occurred_at: str) -> str | None:
        return classify_direction(
            occurred_at,
            tz=self.s.device_timezone,
            checkin_start=self.s.checkin_window_start,
            checkin_end=self.s.checkin_window_end,
            checkout_start=self.s.checkout_window_start,
            checkout_end=self.s.checkout_window_end,
        )

    def _handle_tap(self, tap) -> None:
        name = self._name_map.get(tap.card_number)  # may be None — that's OK
        is_new = self.db.enqueue_event(
            card_number=tap.card_number,
            name=name,
            occurred_at=tap.timestamp,
            source="live",
            direction=self._classify(tap.timestamp),
            debounce_seconds=self.s.tap_debounce_seconds,
            max_taps_per_day=self.s.max_taps_per_day_per_card,
        )
        self.db.set_device_status(connected=True, last_seen=utcnow_iso())
        if is_new:
            log.info("Tap: %s (%s) @ %s", tap.card_number, name or "?", tap.timestamp)
        else:
            log.info("Tap: %s ignored (duplicate or double-tap) @ %s", tap.card_number, tap.timestamp)

    def _housekeeping(self) -> None:
        # live_capture()'s recv just times out (yields None) when the socket
        # is silently dead — it doesn't raise. A real round-trip is the only
        # way to notice a pulled cable / powered-off terminal promptly.
        if not self.dev.is_alive():
            raise DeviceUnavailable("liveness check failed (get_time())")
        self.db.set_device_status(connected=True, last_seen=utcnow_iso())
        # Name map: rebuild if the web UI bumped the version, or it's just stale.
        version = self.db.get_kv("device_users_version", "0")
        stale = (time.monotonic() - self._name_map_built_at) > self.s.name_map_refresh_seconds
        if version != self._last_users_version or stale:
            self._rebuild_name_map()
            self._last_users_version = version
        self._maybe_reconcile()
        self._drain_commands()

    def _rebuild_name_map(self, force: bool = False) -> None:
        try:
            self._name_map = self.dev.build_name_map()
            self._name_map_built_at = time.monotonic()
            log.info("Name map rebuilt: %d cards", len(self._name_map))
        except DeviceUnavailable:
            if force:
                raise
            log.warning("Could not rebuild name map this tick")

    # ── reconciliation ────────────────────────────────────────────────
    def _maybe_reconcile(self) -> None:
        last_iso = self.db.get_kv("last_reconcile_at")
        if last_iso is not None:
            try:
                last_dt = datetime.fromisoformat(last_iso)
                age = (datetime.now(timezone.utc) - last_dt).total_seconds()
                if age < self.s.reconcile_interval_seconds:
                    return
            except ValueError:
                pass  # malformed marker from an older build — just re-reconcile
        since = None
        marker = self.db.get_kv("reconcile_since")
        if marker:
            try:
                since = datetime.fromisoformat(marker)
            except ValueError:
                since = None
        try:
            taps = self.dev.get_attendance_since(since)
        except DeviceUnavailable as exc:
            log.warning("Reconcile skipped: %s", exc)
            return
        added = 0
        newest = since
        for tap in taps:
            if self.db.enqueue_event(
                card_number=tap.card_number,
                name=self._name_map.get(tap.card_number),
                occurred_at=tap.timestamp,
                source="reconcile",
                direction=self._classify(tap.timestamp),
                debounce_seconds=self.s.tap_debounce_seconds,
            max_taps_per_day=self.s.max_taps_per_day_per_card,
            ):
                added += 1
            try:
                ts = datetime.fromisoformat(tap.timestamp)
                if newest is None or ts > newest:
                    newest = ts
            except ValueError:
                pass
        self.db.set_kv("last_reconcile_at", utcnow_iso())
        if newest is not None:
            self.db.set_kv("reconcile_since", newest.isoformat())
        log.info("Reconcile: %d record(s) checked, %d new", len(taps), added)

    # ── device-command queue (enroll / bulk sync from the web UI) ──────
    def _drain_commands(self) -> None:
        while not self._stop.is_set():
            cmd = self.db.claim_next_command()
            if cmd is None:
                return
            log.info("Running command #%s (%s)", cmd["id"], cmd["kind"])
            writes_device = cmd["kind"] in (
                "enroll_one", "enroll_bulk", "bulk_sync", "delete_users", "edit_user",
            )
            try:
                payload = json.loads(cmd["payload"] or "{}")
                if cmd["kind"] == "enroll_one":
                    result = self._cmd_enroll_one(int(cmd["id"]), payload)
                elif cmd["kind"] == "enroll_bulk":
                    result = self._cmd_enroll_bulk(int(cmd["id"]), payload)
                elif cmd["kind"] == "bulk_sync":
                    result = self._cmd_bulk_sync(int(cmd["id"]))
                elif cmd["kind"] == "list_users":
                    result = self._cmd_list_users(int(cmd["id"]))
                elif cmd["kind"] == "delete_users":
                    result = self._cmd_delete_users(int(cmd["id"]), payload)
                elif cmd["kind"] == "edit_user":
                    result = self._cmd_edit_user(int(cmd["id"]), payload)
                else:
                    self.db.finish_command(
                        int(cmd["id"]), status="error",
                        result={"error": f"unknown command kind {cmd['kind']}"},
                    )
                    continue
                self.db.finish_command(int(cmd["id"]), status="done", result=result)
            except Exception as exc:  # never let a bad command kill the loop
                log.exception("Command #%s failed", cmd["id"])
                self.db.finish_command(
                    int(cmd["id"]), status="error", result={"error": str(exc)}
                )
            finally:
                # Only writes actually change the device's user table — a plain
                # read (list_users) doesn't need a version bump / map rebuild.
                if writes_device:
                    self._bump_users_version()
                    self._rebuild_name_map()
                    # Keep the Device Users page's cached snapshot self-healing —
                    # without this, every enroll/edit/delete leaves the visible
                    # list stale until someone clicks "Refresh from device".
                    try:
                        self._refresh_device_users_snapshot()
                    except DeviceUnavailable:
                        log.warning("Could not refresh device users snapshot after write")

    def _cmd_enroll_one(self, command_id: int, payload: dict) -> dict:
        card = str(payload.get("card_number", "")).strip()
        label = str(payload.get("label", "")).strip() or f"Card {card}"
        if not card:
            return {"ok": False, "error": "card_number is required"}
        existing = {
            str(getattr(u, "user_id", "")).strip(): u for u in self.dev.get_users()
        }
        if card in existing:
            uid = int(getattr(existing[card], "uid", 0)) or _next_uid(existing.values())
            action = "updated"
        else:
            uid = _next_uid(existing.values())
            action = "enrolled"
        self.dev.set_user(uid=uid, card_number=card, name=label)
        return {"ok": True, "action": action, "card_number": card, "name": label, "uid": uid}

    def _cmd_edit_user(self, command_id: int, payload: dict) -> dict:
        """Rewrite one existing device slot (by uid) — the only kind that
        targets a specific uid directly instead of matching by card_number,
        so renaming or renumbering a row can't create a duplicate."""
        uid = int(payload.get("uid", 0) or 0)
        card = str(payload.get("card_number", "")).strip()
        label = str(payload.get("label", "")).strip()
        if not uid:
            return {"ok": False, "error": "uid is required"}
        if not card:
            return {"ok": False, "error": "card_number is required"}
        holders = {
            str(getattr(u, "user_id", "")).strip(): int(getattr(u, "uid", 0))
            for u in self.dev.get_users()
        }
        holder = holders.get(card)
        if holder is not None and holder != uid:
            return {
                "ok": False,
                "error": f"Card {card} is already assigned to another user (uid {holder}).",
            }
        name = label or f"Card {card}"
        self.dev.set_user(uid=uid, card_number=card, name=name)
        return {
            "ok": True,
            "uid": uid,
            "card_number": card,
            "name": name,
            "message": f"Updated uid {uid}.",
        }

    def _refresh_device_users_snapshot(self) -> list[dict]:
        """Read the device's user table and cache it for the Device Users
        page. A plain read — never writes to the F18."""
        users = [
            {
                "uid": int(getattr(u, "uid", 0) or 0),
                "card_number": str(getattr(u, "user_id", "")).strip(),
                "name": getattr(u, "name", "") or "",
            }
            for u in self.dev.get_users()
        ]
        users.sort(key=lambda u: u["uid"])
        self.db.set_device_users_snapshot(users)
        return users

    def _cmd_list_users(self, command_id: int) -> dict:
        self.db.update_command_progress(command_id, "reading device users…")
        users = self._refresh_device_users_snapshot()
        return {
            "ok": True,
            "count": len(users),
            "message": f"{len(users)} user(s) on the device.",
        }

    def _cmd_delete_users(self, command_id: int, payload: dict) -> dict:
        uids = [int(u) for u in (payload.get("uids") or [])]
        if not uids:
            return {"ok": False, "error": "No users were selected."}

        def _progress(i, total, uid):
            self.db.update_command_progress(command_id, f"deleting {i}/{total} (uid {uid})")

        ok, failed = self.dev.bulk_delete(uids, on_progress=_progress)
        return {
            "ok": len(failed) == 0,
            "deleted": len(ok),
            "failed": [{"card_number": str(uid), "error": e} for uid, e in failed],
            "message": (
                f"Deleted {len(ok)} user(s)."
                if not failed
                else f"Deleted {len(ok)} user(s), {len(failed)} failed."
            ),
        }

    def _cmd_bulk_sync(self, command_id: int) -> dict:
        self.db.update_command_progress(command_id, "fetching card list from Atlas…")
        try:
            raw = self.atlas.get_card_assignments()
        except AtlasAuthError as exc:
            return {"ok": False, "error": f"Not logged in to Atlas: {exc}"}
        except AtlasError as exc:
            return {"ok": False, "error": str(exc)}
        assignments, skipped = normalize_assignments(raw)
        return self._apply_assignments(command_id, assignments, skipped)

    def _cmd_enroll_bulk(self, command_id: int, payload: dict) -> dict:
        """Enrol a list supplied directly (pasted / uploaded), not from Atlas."""
        rows = payload.get("rows") or []
        if not rows:
            return {"ok": False, "error": "No card rows were provided."}
        self.db.update_command_progress(command_id, f"parsed {len(rows)} row(s)…")
        assignments, skipped = normalize_assignments(rows)
        if not assignments:
            return {
                "ok": False,
                "error": "None of the rows were usable.",
                "skipped": [s.__dict__ for s in skipped],
            }
        return self._apply_assignments(command_id, assignments, skipped)

    def _apply_assignments(self, command_id: int, assignments, skipped) -> dict:
        """Shared tail of bulk_sync / enroll_bulk: diff against the device and
        write the additions + changes."""
        self.db.update_command_progress(command_id, "reading current device users…")
        device_users = [
            DeviceUser(
                uid=int(getattr(u, "uid", 0) or 0),
                card_number=str(getattr(u, "user_id", "")).strip(),
                name=getattr(u, "name", "") or "",
            )
            for u in self.dev.get_users()
        ]
        plan = compute_sync_plan(device_users, assignments)

        if not plan.to_write:
            return {
                "ok": True,
                "enrolled": 0,
                "updated": 0,
                "unchanged": len(plan.unchanged),
                "failed": [],
                "skipped": [s.__dict__ for s in skipped],
                "orphans": plan.orphan_card_numbers,
                "message": f"Nothing to write — {len(plan.unchanged)} card(s) already match.",
            }

        def _progress(i, total, card):
            self.db.update_command_progress(
                command_id, f"writing {i}/{total} to the F18 (card {card})"
            )

        ok, failed = self.dev.bulk_write(plan.to_write, on_progress=_progress)
        ok_set = set(ok)
        enrolled = sum(1 for w in plan.to_write if w.action == "add" and w.card_number in ok_set)
        updated = sum(1 for w in plan.to_write if w.action == "update" and w.card_number in ok_set)
        return {
            "ok": len(failed) == 0,
            "enrolled": enrolled,
            "updated": updated,
            "unchanged": len(plan.unchanged),
            "failed": [{"card_number": c, "error": e} for c, e in failed],
            "skipped": [s.__dict__ for s in skipped],
            "orphans": plan.orphan_card_numbers,
            "message": (
                f"{enrolled} enrolled, {updated} updated, {len(plan.unchanged)} unchanged"
                + (f", {len(failed)} failed" if failed else "")
            ),
        }

    def _bump_users_version(self) -> None:
        cur = int(self.db.get_kv("device_users_version", "0") or "0")
        self.db.set_kv("device_users_version", str(cur + 1))

    # ── flush loop (queue -> Atlas) ───────────────────────────────────
    def _flush_loop(self) -> None:
        while not self._stop.is_set():
            try:
                self._flush_once()
            except Exception:
                log.exception("flush loop error")
            self._stop.wait(self.s.flush_interval_seconds)

    def _flush_once(self) -> None:
        # Pushing a scan needs the device's own API key, not the admin's
        # login session — it works even while nobody is logged into the web
        # UI, as long as this device has registered with Atlas at least once.
        if not self.atlas.is_device_registered():
            return
        # A heartbeat claims "this gateway is alive and taps are flowing" —
        # not true while the F18 itself is unreachable, so don't send one.
        # Atlas is left to notice the silence on its own (same as it would
        # for a genuinely dead gateway) instead of being told everything's
        # fine when the terminal isn't.
        if self.db.get_device_status().get("connected"):
            try:
                self.atlas.heartbeat()
            except AtlasError as exc:
                log.warning("Heartbeat failed: %s", exc)
        batch = self.db.due_events(limit=100)
        for row in batch:
            if self._stop.is_set():
                return
            try:
                self.atlas.push_attendance_event(
                    card_number=row["card_number"],
                    name=row["name"],
                    timestamp=row["occurred_at"],
                )
                self.db.mark_event_sent(int(row["id"]))
            except AtlasAuthError:
                log.warning("Device not registered with Atlas — pausing flush (%d events buffered)",
                            self.db.queue_stats().get("pending_total", 0))
                return
            except AtlasCardUnknownError as exc:
                self.db.delete_event(int(row["id"]))
                log.warning("Event #%s dropped — %s (not retrying)", row["id"], exc)
            except AtlasPushError as exc:
                delay = min(
                    self.s.push_backoff_max_seconds,
                    self.s.flush_interval_seconds * (2 ** min(int(row["attempts"]), 8)),
                )
                nxt = _iso_in(delay)
                self.db.mark_event_failed(int(row["id"]), str(exc), nxt)
                log.warning("Event #%s push failed (%s) — retry after %ds",
                            row["id"], exc, delay)


def _next_uid(users) -> int:
    used = {int(getattr(u, "uid", 0) or 0) for u in users}
    uid = 1
    while uid in used:
        uid += 1
    return uid


def _iso_in(seconds: float) -> str:
    return datetime.fromtimestamp(time.time() + seconds, tz=timezone.utc).isoformat()


def build_listener(settings: Settings, storage: Storage) -> Listener:
    return Listener(
        settings,
        storage,
        build_atlas_client(settings, storage),
        build_device(settings, storage),
    )


def main() -> None:
    settings = get_settings()
    logging.basicConfig(
        level=getattr(logging, settings.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )
    storage = Storage(settings.db_path)
    storage.init_db()
    listener = build_listener(settings, storage)
    signal.signal(signal.SIGTERM, listener.stop)
    signal.signal(signal.SIGINT, listener.stop)
    listener.run()


if __name__ == "__main__":
    main()
