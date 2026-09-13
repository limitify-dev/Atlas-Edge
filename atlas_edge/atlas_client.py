"""The one place that knows how to talk to Atlas.

Everything else (listener, web UI) goes through :class:`AtlasClient`. Endpoint
contracts below were confirmed against the real Atlas-API source — no global
prefix (``ATLAS_EDGE_ATLAS_API_PREFIX=""``), routes at the bare root.

Two separate credentials, for two separate things
--------------------------------------------------
* The **admin session** (email/username + password -> JWT, from the web UI
  login form) can read ``/cards`` — the school's card <-> student list.
* A **device API key**, scoped to this Atlas-Edge install, is required to
  push gate scans to ``/school-entry/*``. It's never typed in by hand: the
  first successful admin login also self-registers this device (or, if a
  device with this name already exists, regenerates its key) and stores the
  key locally. From then on scan pushes work independently of whether anyone
  is currently logged in through the web UI.

Endpoint contracts
-------------------
``POST /auth/login``            (public)
    req  : {"identifier": str, "password": str}   # email or username
    resp : {"accessToken": str, "refreshToken": str, "user": {...}}

``POST /auth/refresh``          (public)
    req  : {"refreshToken": str}
    resp : same shape as /auth/login

``GET /cards``                  (Authorization: Bearer <accessToken>)
    resp : [{"cardNumber": str, "studentId": str | null,
              "student": {"firstName": str, "lastName": str} | null, ...}, ...]
    Cards with no ``student`` (unassigned, or assigned to a teacher) are
    skipped — only cards assigned to a student in atlas.ui are enrolled.

``POST /devices/register``      (Authorization: Bearer <accessToken>)
    req  : {"name": str, "deviceType": "EDGE_DEVICE"}
    resp : {..., "apiKeyPlain": str}     # shown once; 400 if name taken
``GET /devices``                (Authorization: Bearer <accessToken>)
    resp : [{"id": str, "name": str, ...}, ...]
``POST /devices/{id}/regenerate-key``   (Authorization: Bearer <accessToken>)
    resp : {..., "apiKeyPlain": str}     # used when this device already
                                          # exists but we don't hold its key

``POST /device-api/register``   (Authorization: Bearer <device api key>)
    req  : {"device_id": str, "device_name": str}
    Activates a freshly (re)generated key — both device-register paths above
    create/leave the device INACTIVE until this is called with the new key.

``POST /school-entry/scan``     (Authorization: Bearer <device api key>)
    req  : {"cardNumber": str, "at": str, "deviceId": str}   # at = ISO8601
    resp : any 2xx. The server looks up the student by card and decides
    check-in vs check-out itself — Atlas-Edge never sends a direction.

``POST /school-entry/batch``    (Authorization: Bearer <device api key>)
    req  : {"records": [{"cardNumber": str, "at": str}, ...]}
    resp : {"processed": int, "failed": int, "errors": [str, ...]}
    Not used by the flush loop: a partial failure here can't be mapped back
    to which record failed, and retrying the whole batch risks re-recording
    (and re-toggling check-in/out for) events Atlas already accepted. Taps
    are still buffered locally and pushed periodically — just one HTTP call
    per event once the interval fires, not one call per tap as it happens.
"""

from __future__ import annotations

import base64
import json
import logging
import threading
import time
from typing import Optional, Protocol

import httpx

log = logging.getLogger("atlas_edge.atlas_client")


# ── errors ──────────────────────────────────────────────────────────────────
class AtlasError(Exception):
    """Base class for anything that went wrong talking to Atlas."""


class AtlasAuthError(AtlasError):
    """No usable session — the caller must (re-)login through the web UI."""


class AtlasPushError(AtlasError):
    """A request failed in a way that should be retried later (network / 5xx)."""


class AtlasCardUnknownError(AtlasError):
    """Atlas doesn't recognise this card at all (not found, inactive, or not
    assigned to a student) — retrying won't change that, so the caller
    should drop the event instead of scheduling another attempt."""


# ── token persistence ──────────────────────────────────────────────────────
class TokenStore(Protocol):
    """How the client persists its session + device key. Backed by SQLite in
    production, by a dict in tests."""

    def get_token(self) -> Optional[str]: ...
    def get_refresh(self) -> Optional[str]: ...
    def get_expiry(self) -> Optional[float]: ...
    def save(
        self, *, token: Optional[str], refresh: Optional[str], expires_at: Optional[float]
    ) -> None: ...
    def clear(self) -> None: ...
    def get_device_key(self) -> Optional[str]: ...
    def save_device_key(self, key: Optional[str]) -> None: ...


class MemoryTokenStore:
    def __init__(self) -> None:
        self._token: Optional[str] = None
        self._refresh: Optional[str] = None
        self._expires_at: Optional[float] = None
        self._device_key: Optional[str] = None

    def get_token(self) -> Optional[str]:
        return self._token

    def get_refresh(self) -> Optional[str]:
        return self._refresh

    def get_expiry(self) -> Optional[float]:
        return self._expires_at

    def save(self, *, token, refresh, expires_at) -> None:
        self._token, self._refresh, self._expires_at = token, refresh, expires_at

    def clear(self) -> None:
        self.save(token=None, refresh=None, expires_at=None)

    def get_device_key(self) -> Optional[str]:
        return self._device_key

    def save_device_key(self, key: Optional[str]) -> None:
        self._device_key = key


class SqliteTokenStore:
    """Persists the session + device key in the shared ``kv`` table so the
    listener and the web process reuse the same ones."""

    def __init__(self, storage) -> None:
        self._s = storage

    def get_token(self) -> Optional[str]:
        return self._s.get_kv("atlas_access_token")

    def get_refresh(self) -> Optional[str]:
        return self._s.get_kv("atlas_refresh_token")

    def get_expiry(self) -> Optional[float]:
        raw = self._s.get_kv("atlas_token_expires_at")
        return float(raw) if raw else None

    def save(self, *, token, refresh, expires_at) -> None:
        self._s.set_kv("atlas_access_token", token)
        self._s.set_kv("atlas_refresh_token", refresh)
        self._s.set_kv(
            "atlas_token_expires_at", str(expires_at) if expires_at is not None else None
        )

    def clear(self) -> None:
        self.save(token=None, refresh=None, expires_at=None)

    def get_device_key(self) -> Optional[str]:
        return self._s.get_kv("atlas_device_api_key")

    def save_device_key(self, key: Optional[str]) -> None:
        self._s.set_kv("atlas_device_api_key", key)


def _jwt_expiry(token: str) -> Optional[float]:
    """Best-effort read of a JWT's own ``exp`` claim (unix seconds) — not
    verified, purely so we can refresh proactively instead of waiting for a
    401. The server is the actual authority either way."""
    try:
        payload_b64 = token.split(".")[1]
        padded = payload_b64 + "=" * (-len(payload_b64) % 4)
        payload = json.loads(base64.urlsafe_b64decode(padded))
        exp = payload.get("exp")
        return float(exp) if exp is not None else None
    except Exception:  # noqa: BLE001 — malformed/unexpected token shape
        return None


# ── the client ─────────────────────────────────────────────────────────────
class CardAssignment:
    __slots__ = ("student_id", "name", "card_number")

    def __init__(self, student_id: str, name: str, card_number: str) -> None:
        self.student_id = student_id
        self.name = name
        self.card_number = card_number

    def as_dict(self) -> dict:
        return {
            "student_id": self.student_id,
            "name": self.name,
            "card_number": self.card_number,
        }


class AtlasClient:
    def __init__(
        self,
        *,
        api_root: str,
        device_id: str,
        token_store: TokenStore,
        timeout: float = 20.0,
        transport: Optional[httpx.BaseTransport] = None,
        clock=time.time,
    ) -> None:
        self._api_root = api_root.rstrip("/")
        self._device_id = device_id
        self._store = token_store
        self._clock = clock
        # One connection pool for the whole process lifetime, reused by every
        # call (the listener's flush loop can push dozens of small POSTs a
        # minute — a fresh TCP+TLS handshake per event would be wasteful).
        # Split connect/read timeouts so a dead network fails fast instead of
        # hanging for the full read timeout; keep-alive so those repeated
        # POSTs reuse one socket. `transport` is an injection point for tests
        # (httpx.MockTransport).
        self._http = httpx.Client(
            base_url=self._api_root,
            timeout=httpx.Timeout(timeout, connect=min(timeout, 5.0)),
            limits=httpx.Limits(max_keepalive_connections=5, max_connections=10, keepalive_expiry=30.0),
            transport=transport,
        )
        self._lock = threading.Lock()

    def close(self) -> None:
        self._http.close()

    # ── admin session (JWT) ──────────────────────────────────────────────
    def is_authenticated(self) -> bool:
        return self._store.get_token() is not None

    def login(self, identifier: str, password: str) -> None:
        try:
            resp = self._http.post(
                "/auth/login", json={"identifier": identifier, "password": password}
            )
        except httpx.HTTPError as exc:
            raise AtlasError(f"Could not reach Atlas at {self._api_root}: {exc}") from exc
        if resp.status_code == 401:
            raise AtlasAuthError("Atlas rejected those credentials.")
        if resp.status_code >= 400:
            raise AtlasError(f"Login failed: HTTP {resp.status_code} {resp.text[:200]}")
        self._absorb_token(resp.json())
        self._ensure_device_registered()

    def logout(self) -> None:
        self._store.clear()

    def _absorb_token(self, body: dict) -> None:
        token = body.get("accessToken")
        if not token:
            raise AtlasError("Login response had no accessToken.")
        self._store.save(
            token=token,
            refresh=body.get("refreshToken"),
            expires_at=_jwt_expiry(token),
        )

    def _refresh(self) -> bool:
        """Try the refresh-token flow. Returns True if we now hold a fresh token."""
        refresh = self._store.get_refresh()
        if not refresh:
            return False
        try:
            resp = self._http.post("/auth/refresh", json={"refreshToken": refresh})
        except httpx.HTTPError:
            return False
        if resp.status_code >= 400:
            return False
        try:
            self._absorb_token(resp.json())
        except AtlasError:
            return False
        return True

    # ── request helper: attaches the bearer token, refreshes on 401 ───────
    def _request(self, method: str, path: str, **kwargs) -> httpx.Response:
        token = self._store.get_token()
        if not token:
            raise AtlasAuthError("Not logged in to Atlas.")

        # Proactive refresh if we know the token has expired.
        expiry = self._store.get_expiry()
        if expiry is not None and self._clock() >= expiry - 30:
            with self._lock:
                if not self._refresh():
                    self._store.clear()
                    raise AtlasAuthError("Atlas session expired; log in again.")
                token = self._store.get_token()

        headers = {**kwargs.pop("headers", {}), "Authorization": f"Bearer {token}"}
        try:
            resp = self._http.request(method, path, headers=headers, **kwargs)
        except httpx.HTTPError as exc:
            raise AtlasPushError(f"Network error calling Atlas: {exc}") from exc

        if resp.status_code == 401:
            with self._lock:
                refreshed = self._refresh()
            if not refreshed:
                self._store.clear()
                raise AtlasAuthError("Atlas session expired; log in again.")
            headers["Authorization"] = f"Bearer {self._store.get_token()}"
            try:
                resp = self._http.request(method, path, headers=headers, **kwargs)
            except httpx.HTTPError as exc:
                raise AtlasPushError(f"Network error calling Atlas: {exc}") from exc
            if resp.status_code == 401:
                self._store.clear()
                raise AtlasAuthError("Atlas rejected the refreshed session.")
        return resp

    # ── device registration (bootstraps the scan-push credential) ─────────
    def is_device_registered(self) -> bool:
        return self._store.get_device_key() is not None

    def _ensure_device_registered(self) -> None:
        """Best-effort, called right after a successful admin login. A no-op
        once a key is stored. Never raises — a failure here must not break
        login; scan pushes just keep waiting until it succeeds on a later
        login."""
        if self.is_device_registered():
            return
        try:
            key = self._register_device()
            self._activate_device(key)
            self._store.save_device_key(key)
            log.info("Registered this device with Atlas as %r", self._device_id)
        except Exception as exc:  # noqa: BLE001
            log.warning("Could not self-register this device with Atlas: %s", exc)

    def _register_device(self) -> str:
        resp = self._request(
            "POST", "/devices/register",
            json={"name": self._device_id, "deviceType": "EDGE_DEVICE"},
        )
        if resp.status_code < 400:
            return resp.json()["apiKeyPlain"]
        if resp.status_code == 400:
            # Most likely a name conflict — this device was already
            # registered before (e.g. re-provisioned Pi). We don't have its
            # original key, so reuse the device by regenerating it.
            existing = self._request("GET", "/devices")
            if existing.status_code < 400:
                for d in existing.json() or []:
                    if d.get("name") == self._device_id:
                        regen = self._request(
                            "POST", f"/devices/{d['id']}/regenerate-key"
                        )
                        if regen.status_code < 400:
                            return regen.json()["apiKeyPlain"]
        raise AtlasError(
            f"Device registration failed: HTTP {resp.status_code} {resp.text[:200]}"
        )

    def _activate_device(self, device_api_key: str) -> None:
        """A (re)generated key starts INACTIVE — this confirms it's really
        in use, which the API treats as activation."""
        resp = self._http.post(
            "/device-api/register",
            json={"device_id": self._device_id, "device_name": self._device_id},
            headers={"Authorization": f"Bearer {device_api_key}"},
        )
        if resp.status_code >= 400:
            raise AtlasError(
                f"Device activation failed: HTTP {resp.status_code} {resp.text[:200]}"
            )

    def _device_request(self, method: str, path: str, **kwargs) -> httpx.Response:
        key = self._store.get_device_key()
        if not key:
            raise AtlasAuthError(
                "This device isn't registered with Atlas yet — log in through "
                "the web UI once to register it."
            )
        headers = {**kwargs.pop("headers", {}), "Authorization": f"Bearer {key}"}
        try:
            return self._http.request(method, path, headers=headers, **kwargs)
        except httpx.HTTPError as exc:
            raise AtlasPushError(f"Network error calling Atlas: {exc}") from exc

    # ── feature calls ───────────────────────────────────────────────────
    def heartbeat(self) -> None:
        """Proves this device is alive even during a quiet stretch with no
        taps to push. Atlas-API flips a device ACTIVE -> OFFLINE after ~2.5
        heartbeat intervals of silence (a scan push also counts as contact,
        but taps aren't frequent enough on their own to rely on)."""
        resp = self._device_request("POST", "/device-api/heartbeat", json={})
        if resp.status_code >= 400:
            raise AtlasPushError(f"Heartbeat failed: HTTP {resp.status_code} {resp.text[:200]}")

    def push_attendance_event(
        self, *, card_number: str, name: Optional[str], timestamp: str
    ) -> None:
        # `name` isn't sent — the server identifies the student by card and
        # decides check-in vs check-out itself. Kept in the signature so this
        # client is interchangeable with MockAtlasClient.
        payload = {
            "cardNumber": card_number,
            "at": timestamp,
            "deviceId": self._device_id,
        }
        resp = self._device_request("POST", "/school-entry/scan", json=payload)
        if resp.status_code == 404:
            # school-entry.service.ts's recordScanByCard raises NotFoundException
            # for exactly this: no such card, an inactive one, or one never
            # assigned to a student. The card isn't going to become known by
            # retrying, so the caller should drop the event outright.
            raise AtlasCardUnknownError(f"Card {card_number} is not known to Atlas.")
        if resp.status_code >= 500 or resp.status_code == 429:
            raise AtlasPushError(f"Atlas returned HTTP {resp.status_code}; will retry.")
        if resp.status_code >= 400:
            # Other 4xx — e.g. a malformed request — retrying won't help
            # either, but unlike an unknown card this isn't expected/routine,
            # so keep surfacing it instead of silently dropping it.
            raise AtlasPushError(
                f"Atlas rejected the event: HTTP {resp.status_code} {resp.text[:200]}"
            )

    def get_card_assignments(self) -> list[CardAssignment]:
        resp = self._request("GET", "/cards")
        if resp.status_code >= 400:
            raise AtlasError(
                f"Could not fetch card assignments: HTTP {resp.status_code} "
                f"{resp.text[:200]}"
            )
        out: list[CardAssignment] = []
        for row in resp.json() or []:
            student = row.get("student")
            card_number = row.get("cardNumber")
            if not student or not card_number:
                continue  # unassigned, or a teacher's card — nothing to enrol
            name = f"{student.get('firstName', '')} {student.get('lastName', '')}".strip()
            out.append(
                CardAssignment(
                    student_id=str(row.get("studentId") or ""),
                    name=name,
                    card_number=str(card_number),
                )
            )
        return out
