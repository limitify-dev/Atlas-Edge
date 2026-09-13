"""Atlas API client — mocked with httpx.MockTransport (no network).

Two separate credentials are in play: the admin JWT (login, /cards, device
provisioning) and the device's own API key (device-api/register,
school-entry/scan). Covers: login + automatic device self-registration
(fresh, and the name-conflict/regenerate fallback), token reuse, 401 ->
refresh -> retry, proactive refresh via the JWT's own exp claim, push using
the device key (not the JWT), and card-assignment parsing/flattening.
"""

import base64
import json as jsonlib

import httpx
import pytest

from atlas_edge.atlas_client import (
    AtlasAuthError,
    AtlasClient,
    AtlasError,
    AtlasPushError,
    MemoryTokenStore,
)


def make_client(handler, store=None, clock=None):
    store = store or MemoryTokenStore()
    kwargs = dict(
        api_root="https://atlas.test",
        device_id="edge-pi-01",
        token_store=store,
        transport=httpx.MockTransport(handler),
    )
    if clock is not None:
        kwargs["clock"] = clock
    return AtlasClient(**kwargs), store


def _fake_jwt(exp) -> str:
    def seg(obj):
        return base64.urlsafe_b64encode(jsonlib.dumps(obj).encode()).rstrip(b"=").decode()

    return f"{seg({'alg': 'none'})}.{seg({'exp': exp})}.sig"


def _body(request) -> dict:
    return jsonlib.loads(request.content)


# A handler wrapper that answers the device self-registration calls the same
# way every time, so individual tests only need to handle the calls they
# actually care about.
def with_device_bootstrap(inner, *, device_key="dkey-1"):
    def handler(request):
        p = request.url.path
        if p == "/devices/register":
            return httpx.Response(200, json={"apiKeyPlain": device_key})
        if p == "/device-api/register":
            assert request.headers["authorization"] == f"Bearer {device_key}"
            return httpx.Response(200, json={"message": "ok"})
        return inner(request)

    return handler


# ── login ──────────────────────────────────────────────────────────────
def test_login_stores_token_and_self_registers_device():
    def handler(request):
        if request.url.path == "/auth/login":
            return httpx.Response(200, json={"accessToken": "tok-1", "user": {}})
        raise AssertionError(f"unexpected call: {request.url.path}")

    client, store = make_client(with_device_bootstrap(handler))
    client.login("t@school.io", "pw")
    assert store.get_token() == "tok-1"
    assert store.get_device_key() == "dkey-1"
    assert client.is_device_registered() is True


def test_login_bad_credentials_raises_auth_error():
    def handler(request):
        return httpx.Response(401, json={"message": "nope"})

    client, _ = make_client(handler)
    with pytest.raises(AtlasAuthError):
        client.login("t@school.io", "wrong")


def test_login_network_failure_raises_atlas_error_not_raw_httpx_error():
    # A misconfigured/unreachable ATLAS_EDGE_ATLAS_BASE_URL must surface as
    # the login page's existing "Could not reach Atlas" branch, not crash
    # the request with an unhandled httpx exception (a raw 500).
    def handler(request):
        raise httpx.ConnectError("Connection refused", request=request)

    client, _ = make_client(handler)
    with pytest.raises(AtlasError) as exc_info:
        client.login("t@school.io", "pw")
    assert not isinstance(exc_info.value, AtlasAuthError)


def test_login_response_without_access_token_raises():
    def handler(request):
        if request.url.path == "/auth/login":
            return httpx.Response(200, json={"nope": True})
        raise AssertionError("should not get past the failed login")

    client, _ = make_client(handler)
    with pytest.raises(AtlasError):
        client.login("t@school.io", "pw")


def test_login_sends_identifier_not_email():
    captured = {}

    def handler(request):
        if request.url.path == "/auth/login":
            captured.update(_body(request))
            return httpx.Response(200, json={"accessToken": "tok"})
        raise AssertionError(f"unexpected call: {request.url.path}")

    client, _ = make_client(with_device_bootstrap(handler))
    client.login("someone@school.io", "pw")
    assert captured == {"identifier": "someone@school.io", "password": "pw"}


# ── device self-registration ─────────────────────────────────────────
def test_device_registration_failure_does_not_break_login():
    def handler(request):
        p = request.url.path
        if p == "/auth/login":
            return httpx.Response(200, json={"accessToken": "tok"})
        if p == "/devices/register":
            return httpx.Response(500, text="boom")
        raise AssertionError(f"unexpected call: {p}")

    client, store = make_client(handler)
    client.login("t@school.io", "pw")  # must not raise
    assert store.get_token() == "tok"
    assert client.is_device_registered() is False


def test_device_name_conflict_falls_back_to_regenerating_the_existing_key():
    def handler(request):
        p = request.url.path
        if p == "/auth/login":
            return httpx.Response(200, json={"accessToken": "tok"})
        if p == "/devices/register":
            return httpx.Response(400, text="name taken")
        if p == "/devices":
            return httpx.Response(
                200, json=[{"id": "dev-9", "name": "edge-pi-01"}, {"id": "dev-2", "name": "other"}]
            )
        if p == "/devices/dev-9/regenerate-key":
            return httpx.Response(200, json={"apiKeyPlain": "regen-key"})
        if p == "/device-api/register":
            assert request.headers["authorization"] == "Bearer regen-key"
            return httpx.Response(200)
        raise AssertionError(f"unexpected call: {p}")

    client, store = make_client(handler)
    client.login("t@school.io", "pw")
    assert store.get_device_key() == "regen-key"


def test_login_is_a_no_op_bootstrap_once_a_device_key_is_already_stored():
    store = MemoryTokenStore()
    store.save_device_key("already-have-one")
    calls = {"register_attempts": 0}

    def handler(request):
        if request.url.path == "/auth/login":
            return httpx.Response(200, json={"accessToken": "tok"})
        calls["register_attempts"] += 1
        raise AssertionError("should never call the device endpoints again")

    client, _ = make_client(handler, store=store)
    client.login("t@school.io", "pw")
    assert calls["register_attempts"] == 0
    assert store.get_device_key() == "already-have-one"


# ── heartbeat (keeps the device from being swept OFFLINE when quiet) ───
def test_heartbeat_uses_the_device_key():
    seen_auth = []

    def handler(request):
        assert request.url.path == "/device-api/heartbeat"
        seen_auth.append(request.headers.get("authorization"))
        return httpx.Response(200, json={"status": "ACTIVE"})

    store = MemoryTokenStore()
    store.save_device_key("dkey")
    client, _ = make_client(handler, store=store)
    client.heartbeat()
    assert seen_auth == ["Bearer dkey"]


def test_heartbeat_error_status_raises_pusherror():
    store = MemoryTokenStore()
    store.save_device_key("dkey")
    client, _ = make_client(lambda r: httpx.Response(500, text="boom"), store=store)
    with pytest.raises(AtlasPushError):
        client.heartbeat()


def test_heartbeat_without_device_key_raises_auth_error():
    client, _ = make_client(lambda r: httpx.Response(200))
    with pytest.raises(AtlasAuthError):
        client.heartbeat()


# ── push events (device key, not the admin JWT) ────────────────────────
def test_push_uses_the_device_key_not_the_admin_token():
    seen_auth = []

    def handler(request):
        if request.url.path == "/school-entry/scan":
            seen_auth.append(request.headers.get("authorization"))
            return httpx.Response(200)
        raise AssertionError(f"unexpected call: {request.url.path}")

    store = MemoryTokenStore()
    store.save(token="admin-jwt", refresh=None, expires_at=None)
    store.save_device_key("device-key-1")
    client, _ = make_client(handler, store=store)
    client.push_attendance_event(card_number="7", name="Ada", timestamp="2026-01-01T08:00:00")
    assert seen_auth == ["Bearer device-key-1"]


def test_push_payload_shape():
    captured = {}

    def handler(request):
        assert request.url.path == "/school-entry/scan"
        captured.update(_body(request))
        return httpx.Response(200)

    store = MemoryTokenStore()
    store.save_device_key("dkey")
    client, _ = make_client(handler, store=store)
    client.push_attendance_event(card_number="123", name="Zoe", timestamp="2026-02-02T07:30:00")
    assert captured == {
        "cardNumber": "123",
        "at": "2026-02-02T07:30:00",
        "deviceId": "edge-pi-01",
    }


def test_push_without_device_key_raises_auth_error():
    client, _ = make_client(lambda r: httpx.Response(200))
    with pytest.raises(AtlasAuthError):
        client.push_attendance_event(card_number="1", name="x", timestamp="t")


def test_push_5xx_is_retryable_pusherror():
    store = MemoryTokenStore()
    store.save_device_key("dkey")
    client, _ = make_client(lambda r: httpx.Response(503), store=store)
    with pytest.raises(AtlasPushError):
        client.push_attendance_event(card_number="1", name="x", timestamp="t")


def test_push_network_error_is_retryable_pusherror():
    def handler(request):
        raise httpx.ConnectError("down", request=request)

    store = MemoryTokenStore()
    store.save_device_key("dkey")
    client, _ = make_client(handler, store=store)
    with pytest.raises(AtlasPushError):
        client.push_attendance_event(card_number="1", name="x", timestamp="t")


def test_push_4xx_is_not_retryable_pusherror():
    # e.g. an unknown/inactive card — retrying the same payload won't help,
    # but the event still must not be silently dropped.
    store = MemoryTokenStore()
    store.save_device_key("dkey")
    client, _ = make_client(lambda r: httpx.Response(404, text="card not found"), store=store)
    with pytest.raises(AtlasPushError):
        client.push_attendance_event(card_number="1", name="x", timestamp="t")


# ── 401 -> refresh -> retry (admin JWT, used by /cards) ────────────────
def test_401_on_cards_triggers_refresh_and_retries():
    calls = {"cards": 0}

    def handler(request):
        p = request.url.path
        if p == "/auth/refresh":
            assert _body(request) == {"refreshToken": "r1"}
            return httpx.Response(200, json={"accessToken": "new", "refreshToken": "r2"})
        if p == "/cards":
            calls["cards"] += 1
            if request.headers["authorization"] == "Bearer old":
                return httpx.Response(401)
            return httpx.Response(200, json=[])
        raise AssertionError(f"unexpected call: {p}")

    store = MemoryTokenStore()
    store.save(token="old", refresh="r1", expires_at=None)
    client, _ = make_client(handler, store=store)
    client.get_card_assignments()
    assert calls["cards"] == 2
    assert store.get_token() == "new"
    assert store.get_refresh() == "r2"


def test_401_without_refresh_token_clears_session_and_raises_auth_error():
    store = MemoryTokenStore()
    store.save(token="old", refresh=None, expires_at=None)
    client, _ = make_client(lambda r: httpx.Response(401), store=store)
    with pytest.raises(AtlasAuthError):
        client.get_card_assignments()
    assert store.get_token() is None  # session cleared -> web UI must re-login


def test_refresh_failure_clears_session():
    def handler(request):
        if request.url.path == "/auth/refresh":
            return httpx.Response(403)
        return httpx.Response(401)

    store = MemoryTokenStore()
    store.save(token="old", refresh="r1", expires_at=None)
    client, _ = make_client(handler, store=store)
    with pytest.raises(AtlasAuthError):
        client.get_card_assignments()
    assert store.get_token() is None


def test_proactive_refresh_from_the_jwt_exp_claim():
    now = [1000.0]
    new_token = _fake_jwt(exp=3000)

    def handler(request):
        p = request.url.path
        if p == "/auth/refresh":
            return httpx.Response(200, json={"accessToken": new_token, "refreshToken": "r1"})
        assert p == "/cards"
        assert request.headers["authorization"] == f"Bearer {new_token}"
        return httpx.Response(200, json=[])

    store = MemoryTokenStore()
    store.save(token=_fake_jwt(exp=1050), refresh="r1", expires_at=1050.0)
    client, _ = make_client(handler, store=store, clock=lambda: now[0])
    now[0] += 100  # past the 1050 expiry (minus the 30s margin)
    client.get_card_assignments()
    assert store.get_token() == new_token


# ── card assignments ────────────────────────────────────────────────
def test_get_card_assignments_flattens_student_and_skips_unassigned_cards():
    def handler(request):
        assert request.url.path == "/cards"
        return httpx.Response(
            200,
            json=[
                {
                    "cardNumber": "1001",
                    "studentId": "s1",
                    "student": {"firstName": "Alice", "lastName": "Uwase"},
                },
                {"cardNumber": "1002", "studentId": None, "student": None},  # unassigned
                {
                    "cardNumber": "1003",
                    "teacherId": "t1",
                    "teacher": {"firstName": "Mr", "lastName": "Smith"},
                    "student": None,
                },  # a teacher's card
            ],
        )

    store = MemoryTokenStore()
    store.save(token="tok", refresh=None, expires_at=None)
    client, _ = make_client(handler, store=store)
    rows = client.get_card_assignments()
    assert [(r.student_id, r.name, r.card_number) for r in rows] == [
        ("s1", "Alice Uwase", "1001"),
    ]


def test_get_card_assignments_error_status_raises():
    store = MemoryTokenStore()
    store.save(token="tok", refresh=None, expires_at=None)
    client, _ = make_client(lambda r: httpx.Response(500, text="boom"), store=store)
    with pytest.raises(AtlasError):
        client.get_card_assignments()
