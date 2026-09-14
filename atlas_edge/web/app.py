"""Technician web UI for the Pi, on the school LAN.

* Login authenticates against Atlas (``/auth/login``). The returned bearer
  token is stored server-side in SQLite (shared with the listener) — the
  browser only gets a signed session cookie that says "this browser logged in".
* Device writes (single enroll, bulk sync) are *not* done here — the web
  process never opens the F18 socket. It drops a row in ``device_commands`` and
  the listener (sole device owner) executes it. The UI polls the command row.

No TLS here by design (LAN only). It's a plain ASGI app, so putting it behind
an HTTPS reverse proxy later is config, not a rewrite.
"""

from __future__ import annotations

import json
import logging
import threading
from pathlib import Path

from fastapi import FastAPI, File, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

from .. import wifi
from ..atlas_client import AtlasAuthError, AtlasError
from ..config import get_settings
from ..drivers import build_atlas_client
from ..enrollment import AtlasAssignment, DeviceUser, compute_sync_plan, parse_card_rows
from ..formatting import shorttime, timeago
from ..storage import Storage, utcnow_iso

log = logging.getLogger("atlas_edge.web")

_HERE = Path(__file__).parent
_STATIC_DIR = _HERE / "static"
templates = Jinja2Templates(directory=str(_HERE / "templates"))
templates.env.filters["timeago"] = timeago
templates.env.filters["shorttime"] = shorttime


def _asset_version(filename: str) -> int:
    """Mtime of a static file, used as a cache-busting query param so the
    browser can't serve a stale tailwind.css/app.js after we edit it."""
    try:
        return int((_STATIC_DIR / filename).stat().st_mtime)
    except OSError:
        return 0


templates.env.globals["asset_version"] = _asset_version

settings = get_settings()
storage = Storage(settings.db_path)
storage.init_db()

atlas = build_atlas_client(settings, storage)
MOCK_MODE = settings.effective_device_driver == "mock"

app = FastAPI(title="Atlas-Edge", docs_url=None, redoc_url=None)
app.add_middleware(
    SessionMiddleware,
    secret_key=settings.web_secret_key,
    max_age=settings.session_max_age_seconds,
    same_site="lax",
    https_only=False,  # LAN only; flip to True behind a TLS proxy
)
app.mount("/static", StaticFiles(directory=str(_HERE / "static")), name="static")


@app.middleware("http")
async def _no_cache_html(request: Request, call_next):
    """Every page here is generated per-request from live device/queue state,
    so a browser (or an intermediate proxy) must never serve a stale copy —
    that's what made CSS/markup changes look like they "weren't applying"
    after a plain refresh. Static assets are exempt: they're already
    cache-busted via ``asset_version()`` in the templates, so they're safe
    (and desirable) to cache hard."""
    response = await call_next(request)
    if not request.url.path.startswith("/static/"):
        response.headers["Cache-Control"] = "no-store, must-revalidate"
    return response


# ── helpers ───────────────────────────────────────────────────────────────
def _logged_in(request: Request) -> bool:
    return bool(request.session.get("email")) and atlas.is_authenticated()


def _ctx(request: Request, **extra) -> dict:
    return {
        "request": request,
        "email": request.session.get("email"),
        "school_id": settings.school_id,
        "device_id": settings.device_id,
        "atlas_base_url": settings.atlas_base_url,
        "mock_mode": MOCK_MODE,
        "device_driver": settings.effective_device_driver,
        "atlas_driver": settings.effective_atlas_driver,
        **extra,
    }


def _redirect(path: str) -> RedirectResponse:
    return RedirectResponse(path, status_code=303)


# ── auth ──────────────────────────────────────────────────────────────────
@app.get("/login", response_class=HTMLResponse)
def login_form(request: Request):
    if _logged_in(request):
        return _redirect("/")
    return templates.TemplateResponse("login.html", _ctx(request, error=None))


@app.post("/login", response_class=HTMLResponse)
def login_submit(
    request: Request, email: str = Form(...), password: str = Form(...)
):
    try:
        atlas.login(email.strip(), password)
    except AtlasAuthError:
        return templates.TemplateResponse(
            "login.html", _ctx(request, error="Wrong email or password."), status_code=401
        )
    except AtlasError as exc:
        return templates.TemplateResponse(
            "login.html",
            _ctx(request, error=f"Could not reach Atlas: {exc}"),
            status_code=502,
        )
    request.session["email"] = email.strip()
    return _redirect("/")


@app.post("/logout")
def logout(request: Request):
    request.session.clear()
    atlas.logout()
    return _redirect("/login")


# ── status ────────────────────────────────────────────────────────────────
@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    if not _logged_in(request):
        return _redirect("/login")
    return templates.TemplateResponse("status.html", _ctx(request, **_status_data()))


@app.get("/api/status")
def api_status(request: Request):
    if not _logged_in(request):
        return {"error": "unauthenticated"}
    return _status_data()


def _status_data() -> dict:
    dev = storage.get_device_status()
    data = {
        "device": dev,
        "queue": storage.queue_stats(),
        "last_reconcile_at": storage.get_kv("last_reconcile_at"),
        "recent_events": [dict(r) for r in storage.recent_events(10)],
        "recent_commands": [dict(r) for r in storage.recent_commands(10)],
        "atlas_authenticated": atlas.is_authenticated(),
        "device_registered": atlas.is_device_registered(),
    }
    if MOCK_MODE:
        data["mock_atlas_events"] = storage.mock_atlas_event_count()
    return data


# ── manual single enroll ─────────────────────────────────────────────────
@app.get("/enroll", response_class=HTMLResponse)
def enroll_form(request: Request):
    if not _logged_in(request):
        return _redirect("/login")
    return templates.TemplateResponse(
        "enroll.html", _ctx(request, error=None, bulk_error=None)
    )


@app.post("/enroll")
def enroll_submit(
    request: Request, card_number: str = Form(...), label: str = Form("")
):
    if not _logged_in(request):
        return _redirect("/login")
    card = card_number.strip()
    if not card:
        return templates.TemplateResponse(
            "enroll.html",
            _ctx(request, error="Card number is required.", bulk_error=None),
            status_code=400,
        )
    cmd_id = storage.enqueue_command(
        "enroll_one",
        {"card_number": card, "label": label.strip()},
        requested_by=request.session.get("email", ""),
    )
    return _redirect(f"/commands/{cmd_id}")


@app.post("/enroll/bulk")
async def enroll_bulk_submit(
    request: Request,
    rows: str = Form(""),
    file: UploadFile = File(None),
):
    if not _logged_in(request):
        return _redirect("/login")

    text = rows or ""
    if file is not None and getattr(file, "filename", ""):
        try:
            text = (await file.read()).decode("utf-8-sig", errors="replace")
        except Exception:  # noqa: BLE001
            text = ""

    parsed = parse_card_rows(text)
    if not parsed:
        return templates.TemplateResponse(
            "enroll.html",
            _ctx(
                request,
                error=None,
                bulk_error="Paste at least one line (card number, then name) or choose a CSV file.",
            ),
            status_code=400,
        )
    cmd_id = storage.enqueue_command(
        "enroll_bulk",
        {"rows": parsed},
        requested_by=request.session.get("email", ""),
    )
    return _redirect(f"/commands/{cmd_id}")


def _sync_summary(row: dict) -> str:
    """Condense a bulk_sync command's progress/result into one status line."""
    if row["status"] in ("queued", "running"):
        return row["progress"] or ("waiting for the listener…" if row["status"] == "queued" else "running…")
    try:
        result = json.loads(row["result"]) if row["result"] else {}
    except (TypeError, ValueError):
        return row["progress"] or "—"
    if result.get("error"):
        return result["error"]
    parts = []
    if result.get("enrolled"):
        parts.append(f"{result['enrolled']} enrolled")
    if result.get("updated"):
        parts.append(f"{result['updated']} updated")
    if result.get("unchanged"):
        parts.append(f"{result['unchanged']} unchanged")
    if result.get("failed"):
        parts.append(f"{len(result['failed'])} failed")
    if parts:
        return " · ".join(parts)
    return result.get("message") or row["progress"] or "—"


def _missing_enrollments() -> tuple[list[dict], str | None]:
    """Cards assigned in Atlas that this F18 doesn't have enrolled yet — a
    read-only preview, computed the same way a sync would, but never writes
    anything. Returns (rows, error)."""
    try:
        assignments = atlas.get_card_assignments()
    except (AtlasAuthError, AtlasError) as exc:
        return [], str(exc)
    student_ids = {a.card_number: a.student_id for a in assignments}
    device_rows, _ = storage.get_device_users_snapshot()
    device_users = [
        DeviceUser(
            uid=int(r.get("uid") or 0),
            card_number=str(r.get("card_number") or ""),
            name=r.get("name") or "",
        )
        for r in device_rows
    ]
    plan = compute_sync_plan(
        device_users,
        [AtlasAssignment(a.student_id, a.name, a.card_number) for a in assignments],
    )
    missing = [
        {"student_id": student_ids.get(w.card_number, ""), "name": w.name, "card_number": w.card_number}
        for w in plan.to_write
        if w.action == "add"
    ]
    return missing, None


# ── bulk sync from Atlas ─────────────────────────────────────────────────
@app.get("/sync", response_class=HTMLResponse)
def sync_page(request: Request):
    if not _logged_in(request):
        return _redirect("/login")
    history = [
        dict(r) for r in storage.recent_commands(20) if r["kind"] == "bulk_sync"
    ]
    for row in history:
        row["summary"] = _sync_summary(row)
    running = [row for row in history if row["status"] in ("queued", "running")]
    last_finished = next(
        (row for row in history if row["status"] not in ("queued", "running")), None
    )
    missing, missing_error = _missing_enrollments()
    return templates.TemplateResponse(
        "sync.html",
        _ctx(
            request,
            running=running,
            history=history,
            active=running[0] if running else None,
            last_finished=last_finished,
            missing=missing,
            missing_error=missing_error,
        ),
    )


@app.post("/sync")
def sync_start(request: Request):
    if not _logged_in(request):
        return _redirect("/login")
    cmd_id = storage.enqueue_command(
        "bulk_sync", {}, requested_by=request.session.get("email", "")
    )
    return _redirect(f"/commands/{cmd_id}")


DEVICE_USERS_PAGE_SIZE = 20


# ── device users (who's enrolled on the F18 — view, refresh, delete) ────
@app.get("/device-users", response_class=HTMLResponse)
def device_users_page(request: Request, q: str = "", page: int = 1):
    if not _logged_in(request):
        return _redirect("/login")
    all_users, fetched_at = storage.get_device_users_snapshot()

    q = q.strip()
    if q:
        needle = q.lower()
        matched = [
            u
            for u in all_users
            if needle in (u.get("name") or "").lower()
            or needle in (u.get("card_number") or "").lower()
        ]
    else:
        matched = all_users

    total_matched = len(matched)
    total_pages = max(1, -(-total_matched // DEVICE_USERS_PAGE_SIZE))  # ceil div
    page = min(max(page, 1), total_pages)
    start = (page - 1) * DEVICE_USERS_PAGE_SIZE
    page_users = matched[start : start + DEVICE_USERS_PAGE_SIZE]

    fetching = any(
        r["kind"] == "list_users" and r["status"] in ("queued", "running")
        for r in storage.recent_commands(10)
    )
    return templates.TemplateResponse(
        "device_users.html",
        _ctx(
            request,
            users=page_users,
            total_all=len(all_users),
            total_matched=total_matched,
            fetched_at=fetched_at,
            fetching=fetching,
            q=q,
            page=page,
            total_pages=total_pages,
            page_size=DEVICE_USERS_PAGE_SIZE,
            range_start=start + 1 if page_users else 0,
            range_end=start + len(page_users),
        ),
    )


@app.post("/device-users/refresh")
def device_users_refresh(request: Request):
    if not _logged_in(request):
        return _redirect("/login")
    storage.enqueue_command(
        "list_users", {}, requested_by=request.session.get("email", "")
    )
    return _redirect("/device-users")


@app.post("/device-users/delete")
def device_users_delete(request: Request, uid: list[int] = Form(default=[])):
    if not _logged_in(request):
        return _redirect("/login")
    if not uid:
        return _redirect("/device-users")
    cmd_id = storage.enqueue_command(
        "delete_users", {"uids": uid}, requested_by=request.session.get("email", "")
    )
    return _redirect(f"/commands/{cmd_id}")


@app.post("/device-users/delete-all")
def device_users_delete_all(request: Request):
    if not _logged_in(request):
        return _redirect("/login")
    all_users, _ = storage.get_device_users_snapshot()
    uids = [int(u["uid"]) for u in all_users]
    if not uids:
        return _redirect("/device-users")
    cmd_id = storage.enqueue_command(
        "delete_users", {"uids": uids}, requested_by=request.session.get("email", "")
    )
    return _redirect(f"/commands/{cmd_id}")


@app.get("/device-users/{uid}/edit", response_class=HTMLResponse)
def device_user_edit_form(request: Request, uid: int):
    if not _logged_in(request):
        return _redirect("/login")
    all_users, _ = storage.get_device_users_snapshot()
    user = next((u for u in all_users if int(u.get("uid", 0)) == uid), None)
    if user is None:
        return _redirect("/device-users")
    return templates.TemplateResponse(
        "device_user_edit.html", _ctx(request, user=user, error=None)
    )


@app.post("/device-users/{uid}/edit")
def device_user_edit_submit(
    request: Request, uid: int, card_number: str = Form(...), label: str = Form("")
):
    if not _logged_in(request):
        return _redirect("/login")
    card = card_number.strip()
    if not card:
        all_users, _ = storage.get_device_users_snapshot()
        user = next((u for u in all_users if int(u.get("uid", 0)) == uid), None)
        return templates.TemplateResponse(
            "device_user_edit.html",
            _ctx(request, user=user, error="Card number is required."),
            status_code=400,
        )
    cmd_id = storage.enqueue_command(
        "edit_user",
        {"uid": uid, "card_number": card, "label": label.strip()},
        requested_by=request.session.get("email", ""),
    )
    return _redirect(f"/commands/{cmd_id}")


# ── WiFi network setup (which network wlan0 joins — never touches ap0,
# the separate admin hotspot this page itself is normally reached through).
# Gated the same as every other page: `_logged_in` only checks a local
# session cookie + a locally-stored Atlas token (see atlas_client.py's
# `is_authenticated`), no live call out to Atlas — so it still works from
# the admin hotspot even while wlan0 itself is down/mid-reconfiguration,
# which is exactly when this page is needed. Note this app binds 0.0.0.0
# (see Settings.web_host), so it's technically also reachable over wlan0's
# own address, not just ap0's — the background-thread connect below isn't
# just a nicety, it's what keeps a request arriving over wlan0 from being
# cut off by the very reconnect it triggered. ─────────────────────────────
def _run_wifi_connect(iface: str, ssid: str, password: str) -> None:
    ok, message = wifi.connect(iface, ssid, password)
    log.info("wifi connect to %r on %s: %s", ssid, iface, "ok" if ok else "failed")
    storage.set_wifi_connect_state(
        {
            "ssid": ssid,
            "status": "connected" if ok else "failed",
            "message": message,
            "finished_at": utcnow_iso(),
        }
    )


@app.get("/wifi-setup", response_class=HTMLResponse)
def wifi_setup_page(request: Request):
    if not _logged_in(request):
        return _redirect("/login")
    try:
        networks = wifi.scan_networks(settings.wifi_iface)
        scan_error = None
    except wifi.WifiError as exc:
        networks = []
        scan_error = str(exc)
    connected = next((n for n in networks if n.connected), None)
    others = [n for n in networks if not n.connected]
    return templates.TemplateResponse(
        "wifi_setup.html",
        _ctx(
            request,
            wifi_iface=settings.wifi_iface,
            connected=connected,
            networks=others,
            scan_error=scan_error,
            connect_state=storage.get_wifi_connect_state(),
        ),
    )


@app.post("/wifi-setup")
def wifi_setup_connect(request: Request, ssid: str = Form(...), password: str = Form("")):
    if not _logged_in(request):
        return JSONResponse({"error": "unauthenticated"}, status_code=401)
    ssid = ssid.strip()
    if not ssid:
        return JSONResponse({"error": "Choose a network first."}, status_code=400)
    storage.set_wifi_connect_state(
        {"ssid": ssid, "status": "connecting", "message": "", "started_at": utcnow_iso(), "finished_at": None}
    )
    threading.Thread(
        target=_run_wifi_connect,
        args=(settings.wifi_iface, ssid, password),
        name="wifi-connect",
        daemon=True,
    ).start()
    return JSONResponse({"ok": True, "ssid": ssid, "status": "connecting"})


@app.get("/wifi-setup/status")
def wifi_setup_status(request: Request):
    if not _logged_in(request):
        return JSONResponse({"error": "unauthenticated"}, status_code=401)
    return JSONResponse(storage.get_wifi_connect_state())


# ── command progress ────────────────────────────────────────────────────
@app.get("/commands/{command_id}", response_class=HTMLResponse)
def command_view(request: Request, command_id: int):
    if not _logged_in(request):
        return _redirect("/login")
    row = storage.get_command(command_id)
    if row is None:
        return templates.TemplateResponse(
            "command.html", _ctx(request, command=None, result=None), status_code=404
        )
    data = dict(row)
    result = None
    if data.get("result"):
        try:
            result = json.loads(data["result"])
        except (TypeError, ValueError):
            result = {"message": data["result"]}
    return templates.TemplateResponse(
        "command.html", _ctx(request, command=data, result=result)
    )


@app.get("/api/commands/{command_id}")
def command_api(request: Request, command_id: int):
    if not _logged_in(request):
        return {"error": "unauthenticated"}
    row = storage.get_command(command_id)
    return dict(row) if row else {"error": "not found"}


@app.get("/healthz")
def healthz():
    return {"ok": True}


# ── local test mode: simulate the device + inspect the mock Atlas ────────
@app.get("/dev", response_class=HTMLResponse)
def dev_panel(request: Request):
    if not MOCK_MODE:
        return _redirect("/")
    if not _logged_in(request):
        return _redirect("/login")
    return templates.TemplateResponse(
        "dev.html",
        _ctx(
            request,
            device_users=[dict(r) for r in storage.mock_get_users()],
            atlas_events=[dict(r) for r in storage.mock_recent_atlas_events(25)],
        ),
    )


@app.post("/dev/tap")
def dev_simulate_tap(request: Request, card_number: str = Form(...)):
    if not MOCK_MODE:
        return _redirect("/")
    if not _logged_in(request):
        return _redirect("/login")
    card = card_number.strip()
    if card:
        storage.mock_enqueue_tap(card_number=card, occurred_at=utcnow_iso())
    return _redirect("/dev")
