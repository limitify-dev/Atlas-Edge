# Atlas-Edge

A small gateway service that runs on a **Raspberry Pi 4** and bridges a
**ZKTeco F18** card terminal (TCP/IP, port 4370) to the **Atlas** school
platform.

It does three jobs:

1. **Listens** to the F18 for card taps and forwards each one to Atlas
   (buffered locally and retried until it lands — nothing is ever dropped).
2. Serves a small **web UI** on the school LAN for a technician to enroll
   cards and watch status.
3. **Syncs** the card ↔ student list *down* from Atlas and bulk-writes it to
   the F18, instead of hand-enrolling every card.

---

## Architecture

Two independent processes share one SQLite file:

```
                    ┌─────────────────────────── Raspberry Pi ───────────────────────────┐
                    │                                                                    │
 ZKTeco F18 ◄──TCP──┼──►  atlas-edge-listener   ──────────────►  Atlas API               │
   (4370)           │      • owns the device socket             (POST /attendance-events)│
                    │      • live tap stream + hourly reconcile                           │
                    │      • executes enroll / bulk-sync jobs                             │
                    │              │        ▲                                             │
                    │        SQLite (WAL)   │  queued jobs / session token               │
                    │              ▲        │                                             │
                    │      atlas-edge-web  ─┘  ──────────────►  Atlas API                 │
                    │      • login, status, enroll, sync         (POST /auth/login, …)    │
                    └────────────────────────────────────────────────────────────────────┘
     Technician's browser ──HTTP (LAN)──► atlas-edge-web  :8080
```

**The listener is the only process that talks to the F18.** The web UI never
opens the device socket — when you enroll a card or start a sync it writes a
row into the `device_commands` table and the listener performs it on its next
housekeeping tick, reporting progress back through the same table. This keeps a
single owner of the reverse-engineered protocol and keeps the web app light
(it doesn't even need `pyzk`).

A crashed web UI can't stop taps being recorded; a crashed listener can't stop
you reading status. They only share the SQLite file.

### Why the name is attached on the Pi

An F18 attendance record is a compact log line — `user_id` (kept equal to the
card number) + timestamp + status. It does **not** contain the person's name,
even though the name *is* stored on the device against that `user_id` in a
separate users table. So the listener builds a `{card_number: name}` map from
the device's own `get_users()` at startup and after every enrollment sync, and
uses that to fill in `name` on each tap. A tap for a card that isn't in the map
yet (e.g. right after a sync, before the rebuild) is still forwarded with
`name: null` — Atlas can reconcile it later.

---

## Project layout

```
atlas_edge/
  config.py         env / .env settings (nothing hardcoded); driver selection
  storage.py        SQLite: event queue, device-command queue, kv, mock_* tables
  atlas_client.py   the ONLY module that knows Atlas's HTTP contract — swappable
  device.py         pyzk wrapper for the F18 (lazy import; listener-only)
  mock.py           MockF18 + MockAtlasClient — local testing, no hardware
  drivers.py        build_device() / build_atlas_client() — real vs mock
  enrollment.py     pure diff/sync logic (no I/O) — device users vs Atlas list
  listener.py       the background service   →  python -m atlas_edge.listener
  web/
    app.py          FastAPI app               →  python -m atlas_edge.web
    templates/      Jinja2, Tailwind utility classes — the same design system
                    as atlas.ui (sidebar + topbar from Sidebar.tsx/TopBar.tsx,
                    Cabinet Grotesk / DM Sans, the Atlas logo, MetricStrip-style
                    tiles, status badges, inlined lucide SVGs)
    static/         tailwind.css (compiled, served — check it in), tailwind-src.css
                    (source — see "Rebuilding the CSS" below), app.js, atlas-icon.png
systemd/            two unit files
tests/              atlas_client (mocked), enrollment, storage, listener, mock
```

### Rebuilding the CSS

The UI's CSS is Tailwind, compiled ahead of time — the Pi never runs Node,
it only ever serves the checked-in `atlas_edge/web/static/tailwind.css`.
After changing any class name in a template, or `tailwind-src.css` itself,
rebuild it on your laptop (needs Node — a one-time `npm install`):

```bash
npm install                 # once, pulls the Tailwind CLI (devDependency only)
npm run build:css           # one-shot rebuild
npm run watch:css           # or keep it rebuilding while you edit templates
```

The web process cache-busts `tailwind.css`/`app.js` by file mtime
(`asset_version()` in `app.py`), and every page response is sent with
`Cache-Control: no-store` — a browser refresh always picks up a rebuilt
bundle, no hard-reload needed.

---

## Requirements mapped to code

| Requirement | Where |
| --- | --- |
| Live tap capture + reconnect/watchdog | `listener._device_loop` (capped backoff), `device.live_events` (raises on a dead stream) |
| Attach name from the device's own user table | `device.build_name_map`, rebuilt in `listener._housekeeping` on version-bump or staleness |
| Forward `{name, card_number, timestamp}`; missing name ⇒ still send | `listener._handle_tap` (name may be `None`), `atlas_client.push_attendance_event` |
| Buffer failed POSTs in SQLite + retry with backoff | `storage.event_queue`, `listener._flush_loop` (exponential, capped) |
| Hourly onboard-log reconciliation, de-duplicated | `listener._maybe_reconcile` + `event_queue.dedup_key` UNIQUE |
| Runs as a resilient service, restarts on crash/boot | `systemd/atlas-edge-listener.service` (`Restart=always`) |
| Web login against Atlas; token kept server-side, reused everywhere | `web.app` login → `atlas_client.login`; `SqliteTokenStore` shared by both processes; 401 → refresh in `atlas_client._request` |
| Status page (connection, queue size, last sync) | `GET /` + `GET /api/status` |
| Enrol one card | `POST /enroll` → `device_commands(kind=enroll_one)` → `listener._cmd_enroll_one` |
| Bulk enrol from a pasted/CSV list | `POST /enroll/bulk` → `enrollment.parse_card_rows` → `device_commands(kind=enroll_bulk)` → `listener._cmd_enroll_bulk` (same diff/`bulk_write` as sync) |
| Bulk sync from Atlas with diff + progress + summary | `POST /sync` → `listener._cmd_bulk_sync` → `enrollment.compute_sync_plan` → `device.bulk_write` |
| Browse who's enrolled on the device | `GET /device-users` (client-side search over the cached snapshot) → `POST /device-users/refresh` → `device_commands(kind=list_users)` → `listener._cmd_list_users` (read-only) → `storage.set_device_users_snapshot` |
| Idempotent re-runs | `enrollment.compute_sync_plan` (add / update / unchanged), `device.set_user` updates in place |
| All config in `.env`, no hardcoded creds | `config.Settings`, `.env.example`; credentials entered once via web login |
| Easy to repoint at another F18 / Atlas env | edit `.env` only |

---

## Local development (on your laptop)

```bash
cd Atlas-Edge
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt        # or: pip install -e ".[dev]"
cp .env.example .env                        # edit ATLAS_EDGE_ATLAS_BASE_URL, SCHOOL_ID
pytest -q                                   # run the suite
```

### Run the whole thing on your laptop — no F18, no real Atlas

There's a **mock mode**: an in-process simulator for the F18 and for Atlas, so
the full pipeline (tap → queue → push, plus enrol/sync) runs on a laptop.

```bash
cp .env.local.example .env          # sets DEVICE_DRIVER=mock, ATLAS_DRIVER=mock

# terminal 1 — the listener (executes syncs, processes taps)
python -m atlas_edge.listener

# terminal 2 — the web UI
python -m atlas_edge.web            # http://localhost:8080
```

Then in the browser:

1. **Sign in** with any email + password (mock Atlas accepts anything).
2. **Sync from Atlas** — enrols the cards in
   [`data/mock_card_assignments.json`](data/mock_card_assignments.json) onto the
   mock device.
3. **Test tools** (nav item, mock mode only) — *Simulate a tap* for card
   `1001`; watch it appear on **Status** with the name attached and, a second
   later, land in "events the mock Atlas received".

`ATLAS_EDGE_MOCK_ATLAS_FAIL_RATE=0.5` makes the mock Atlas reject half the
pushes so you can watch the local buffer + backoff/retry do their job.

`device_driver` / `atlas_driver` are `f18` / `http` in production (the default
`auto` picks `mock` only when `F18_HOST` is `mock`/empty or `ATLAS_BASE_URL`
is empty/contains "mock").

The web UI also runs standalone against a real Atlas without a device — device
status just shows "Offline" and enrol/sync jobs wait for a listener.

### Real F18, still on localhost

Once you have the terminal wired up but before deploying to a Pi, run against
the **real device** with Atlas still mocked — the Atlas API doesn't implement
the four endpoints in `atlas_client.py` yet (`auth/login`, `auth/refresh`,
`attendance-events`, `schools/{id}/card-assignments`), so pointing
`ATLAS_DRIVER` at it now would just 404.

```bash
cp .env.real-device.example .env
# edit ATLAS_EDGE_F18_HOST to the terminal's LAN IP

python -m atlas_edge.listener   # terminal 1 — connects to the real F18
python -m atlas_edge.web        # terminal 2 — http://localhost:8080
```

Sign in with anything (Atlas is mocked) and **Sync from Atlas** — it reads
`data/mock_card_assignments.json` and writes those cards onto the *real*
terminal via `pyzk`. Tap a card and it should show up on **Status** with the
enrolled name attached. The topbar's two chips (`F18` / `Mock Atlas`) always
show which side is real vs simulated, so it's never ambiguous which mode
you're in. Switch `ATLAS_DRIVER` to `http` and point `ATLAS_BASE_URL` at the
real API the day those endpoints ship — nothing else changes.

---

## Deploying to the Raspberry Pi

Assumes Raspberry Pi OS (Debian). The F18 must be on the same LAN and reachable
on `tcp/4370` (`nc -vz <F18_IP> 4370`).

```bash
# 1. Code + venv under /opt/atlas-edge
sudo useradd --system --home /opt/atlas-edge --shell /usr/sbin/nologin atlas-edge
sudo mkdir -p /opt/atlas-edge && sudo chown atlas-edge:atlas-edge /opt/atlas-edge
sudo -u atlas-edge git clone <this-repo> /opt/atlas-edge        # or rsync the folder
cd /opt/atlas-edge
sudo -u atlas-edge python3 -m venv .venv
sudo -u atlas-edge .venv/bin/pip install -r requirements.txt
sudo -u atlas-edge mkdir -p data

# 2. Configuration
sudo -u atlas-edge cp .env.example .env
sudo -u atlas-edge nano .env
#   ATLAS_EDGE_F18_HOST=192.168.1.201
#   ATLAS_EDGE_ATLAS_BASE_URL=https://api.atlas.yourdomain
#   ATLAS_EDGE_SCHOOL_ID=<school id>
#   ATLAS_EDGE_DEVICE_ID=edge-pi-<location>
#   ATLAS_EDGE_WEB_SECRET_KEY=$(openssl rand -hex 32)

# 3. systemd units
sudo cp systemd/atlas-edge-listener.service /etc/systemd/system/
sudo cp systemd/atlas-edge-web.service      /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now atlas-edge-listener atlas-edge-web

# 4. Check
systemctl status atlas-edge-listener atlas-edge-web
journalctl -u atlas-edge-listener -f
```

Then browse to `http://<pi-ip>:8080`, sign in with an Atlas account, and run
**Sync from Atlas** once to push the card list to the F18.

Updating later:

```bash
cd /opt/atlas-edge && sudo -u atlas-edge git pull
sudo -u atlas-edge .venv/bin/pip install -r requirements.txt
sudo systemctl restart atlas-edge-listener atlas-edge-web
```

---

## Configuration reference

Everything is read from environment variables (prefix `ATLAS_EDGE_`) or a
`.env` file next to the app. See [`.env.example`](.env.example) for the full
list with defaults. The ones you must set per school:

| Variable | Meaning |
| --- | --- |
| `ATLAS_EDGE_F18_HOST` | F18 IP on the LAN |
| `ATLAS_EDGE_ATLAS_BASE_URL` | Atlas API root (no trailing slash) — swap for staging/prod |
| `ATLAS_EDGE_SCHOOL_ID` | this school's Atlas identifier (sent with every event) |
| `ATLAS_EDGE_DEVICE_ID` | stable id for this Pi/gateway |
| `ATLAS_EDGE_WEB_SECRET_KEY` | random 32-byte hex — signs the browser session cookie only |

Atlas login credentials are **not** config. They're entered once in the web UI;
the resulting session (access + optional refresh token) is stored in SQLite and
reused by both processes. Expiry is handled automatically (refresh-token flow if
Atlas returns one, otherwise the UI re-prompts).

---

## Atlas API — the four stubs to implement

All contracts live in [`atlas_edge/atlas_client.py`](atlas_edge/atlas_client.py)
(top-of-file docstring). Replace that one module when the real API is ready.

```
POST {prefix}/auth/login
  → {"email","password"}                 ⇒ {"access_token", "refresh_token"?, "expires_in"?}
POST {prefix}/auth/refresh   (optional)
  → {"refresh_token"}                     ⇒ same shape as /auth/login
POST {prefix}/attendance-events
  Authorization: Bearer <token>
  → {"device_id","school_id","card_number","name"|null,"timestamp"}   ⇒ any 2xx
GET  {prefix}/schools/{school_id}/card-assignments
  Authorization: Bearer <token>
  ⇒ {"assignments":[{"student_id","name","card_number"}, …]}   (a bare list also works)
```

`prefix` defaults to `/api/v1` (`ATLAS_EDGE_ATLAS_API_PREFIX`).

---

## Testing

```bash
pytest -q
```

* `test_atlas_client.py` — mocked with `httpx.MockTransport`: token reuse across
  calls, `401 → refresh → retry`, refresh failure ⇒ `AtlasAuthError` + session
  cleared, `5xx`/network ⇒ retryable `AtlasPushError`, proactive refresh on a
  known-expired token, both card-assignment response shapes.
* `test_enrollment.py` — the diff/sync core: malformed rows skipped with
  reasons, duplicate card numbers (last wins), add/update/unchanged
  classification, distinct uid assignment for multiple adds, idempotent
  re-run = no-op, orphans reported but never deleted.
* `test_storage.py` — queue dedup between live stream and reconciliation,
  retry/backoff bookkeeping, one-shot command claim, stale-command re-queue.
* `test_listener_sync.py` — orchestration with a fake device + fake Atlas:
  enroll/update/skip in one run, partial failure summary
  ("1 enrolled … 1 failed"), clean second run, single-card enroll vs update.
* `test_mock.py` — the local-test simulators (`MockF18`, `MockAtlasClient`)
  honour the same contracts as the real ones: injected taps stream out,
  onboard-log reconciliation, set_user is update-in-place; mock login accepts
  anything, `fail_rate` raises the retryable error, card list read from file
  with a sample fallback.

The minimal env (`pip install pytest httpx`) runs everything except
`test_listener_sync.py`, which is skipped unless `pydantic-settings` is present.

---

## Operational notes

* **Not in sync** if the listener is down: taps aren't recorded and queued
  enroll/sync jobs wait. That's intentional — the listener owns the hardware.
  The web UI still shows status and lets you queue jobs for when it's back.
* **Timezones**: the F18 reports naive local timestamps. If the Pi's clock/zone
  matches the school, nothing to do. Otherwise set
  `ATLAS_EDGE_DEVICE_TIMEZONE=Africa/Kigali` (IANA name) so events carry the
  right offset.
* **Local data** lives in `data/atlas_edge.sqlite3` (WAL). Safe to back up while
  running; deleting it loses only the local queue/history, not device data.
* **Multiple F18s per Pi** is out of scope but nothing hardcodes "one device" in
  a way that blocks it: `device_id` is already per-gateway config, and the
  device layer is a single class you could instantiate per unit.
* **HTTPS** is not terminated here (LAN only). It's a plain ASGI app — front it
  with nginx/Caddy and flip `https_only=True` in `web/app.py` when you do.
* **Atlas connection efficiency**: `AtlasClient` opens one `httpx.Client` for
  the whole process lifetime and reuses it for every call — the flush loop can
  push dozens of small events a minute and none of them pay for a fresh
  TCP/TLS handshake. It uses a keep-alive pool (`httpx.Limits`, 5 idle
  connections) and split connect/read timeouts (`httpx.Timeout`, 5s to
  connect) so a dead network fails fast instead of hanging for the full read
  timeout.
* **UI auto-refresh is not `<meta refresh>`**: live pages (Status, a running
  job, Test tools) re-fetch just their own URL and swap in the changed region
  (`static/app.js`, `data-autorefresh`) instead of reloading the whole page —
  no flash, scroll position is kept, and it stops polling entirely while the
  browser tab is hidden.
