"""Runtime configuration.

Everything that changes between schools or environments (staging vs prod) lives
here and is read from environment variables / a ``.env`` file — nothing is
hardcoded. Atlas login credentials are deliberately *not* config: they are
entered once through the web UI and the resulting session is persisted in the
local SQLite store (see :mod:`atlas_edge.storage`).
"""

from __future__ import annotations

import functools
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        env_prefix="ATLAS_EDGE_",
        extra="ignore",
    )

    # ── Drivers ────────────────────────────────────────────────────────────
    # "f18"  = talk to a real ZKTeco F18 over the network (default)
    # "mock" = in-process simulator, no hardware — for testing on a laptop.
    # "auto" resolves to "mock" when f18_host is "mock"/empty, else "f18".
    device_driver: str = "auto"
    # "http" = real Atlas API (default). "mock" = in-process fake Atlas.
    # "auto" resolves to "mock" when atlas_base_url is empty or contains "mock".
    atlas_driver: str = "auto"

    # ── ZKTeco F18 terminal ────────────────────────────────────────────────
    f18_host: str = Field("192.168.1.201", description="F18 IP on the school LAN")
    f18_port: int = 4370
    f18_password: int = Field(0, description="Device comm key (usually 0)")
    f18_timeout: int = Field(15, description="Socket timeout (seconds)")
    f18_force_udp: bool = False
    # If the Pi clock/timezone doesn't match the school, set this (IANA name,
    # e.g. "Africa/Kigali") to stamp naive device timestamps correctly.
    device_timezone: str = ""

    # ── Atlas API ──────────────────────────────────────────────────────────
    atlas_base_url: str = Field(
        "https://api.atlas.example", description="Atlas API root, no trailing slash"
    )
    # Real Atlas-API mounts every route at the bare root (no /api/v1 or
    # similar) — leave this empty unless a future Atlas-API version adds one.
    atlas_api_prefix: str = ""
    http_timeout_seconds: int = 20

    # ── This gateway / school identity ─────────────────────────────────────
    school_id: str = Field("SCHOOL_ID_HERE", description="Atlas school identifier")
    device_id: str = Field(
        "edge-pi-01", description="Stable id for this Pi/gateway, sent with every event"
    )

    # ── WiFi (wlan0 — the station radio; see atlas_edge/wifi.py) ───────────
    # Drives the /wifi-setup admin page, which picks which network wlan0
    # joins. Same variable name the ap0_setup.sh/ap0_watchdog.sh shell
    # scripts already read straight from .env for the same interface — kept
    # in sync deliberately, one name for one physical adapter.
    wifi_iface: str = "wlan0"
    # This radio can't reliably scan for a new network while ap0 is
    # actively beaconing (confirmed live — wlan0 scanning silently fails to
    # find an in-range AP whenever ap0 is up). wifi.connect() briefly stops
    # these units for the duration of a connect attempt, then re-runs
    # ap0_setup.sh to bring ap0 back (which also realigns its channel to
    # wherever wlan0 ends up). Needs a matching sudoers NOPASSWD grant for
    # this unprivileged process — see systemd/ap0-wifi-sudoers.
    hotspot_hostapd_unit: str = "atlas-ap0-hostapd.service"
    hotspot_dnsmasq_unit: str = "atlas-ap0-dnsmasq.service"
    hotspot_watchdog_timer: str = "atlas-ap0-watchdog.timer"

    # ── Local state ────────────────────────────────────────────────────────
    db_path: Path = Path("./data/atlas_edge.sqlite3")

    # ── Web UI ─────────────────────────────────────────────────────────────
    web_host: str = "0.0.0.0"
    web_port: int = 8080
    # Signs the browser session cookie only. The Atlas bearer token is never
    # placed in the cookie — it stays server-side in SQLite.
    web_secret_key: str = Field(
        "change-me-please", description="Random string; generate with `openssl rand -hex 32`"
    )
    session_max_age_seconds: int = 60 * 60 * 12

    # ── Attendance windows (local to Atlas-Edge only) ───────────────────────
    # Classifies each tap as check-in / check-out purely for the Recent taps
    # view on this device — never sent to Atlas, never changes what's pushed.
    # "HH:MM" 24h, read in device_timezone; a tap outside both windows just
    # shows with no direction. Also collapses an accidental double-tap (the
    # same card again within tap_debounce_seconds) into a single event.
    checkin_window_start: str = "06:00"
    checkin_window_end: str = "09:00"
    checkout_window_start: str = "14:00"
    checkout_window_end: str = "18:00"
    tap_debounce_seconds: int = Field(
        30, description="A repeat tap from the same card within this many seconds is dropped as a double-tap, not logged again"
    )

    # ── Timers (seconds) ───────────────────────────────────────────────────
    flush_interval_seconds: int = 15
    reconcile_interval_seconds: int = 3600
    housekeeping_tick_seconds: int = 10
    reconnect_backoff_start_seconds: int = 3
    reconnect_backoff_max_seconds: int = 60
    push_backoff_max_seconds: int = 900
    name_map_refresh_seconds: int = 300

    log_level: str = "INFO"

    # ── Mock-mode knobs (only used when a driver resolves to "mock") ───────
    mock_card_assignments_path: Path = Path("./data/mock_card_assignments.json")
    mock_atlas_fail_rate: float = Field(
        0.0, ge=0.0, le=1.0, description="0..1 — fraction of pushes the mock Atlas rejects"
    )

    @property
    def api_root(self) -> str:
        return f"{self.atlas_base_url.rstrip('/')}{self.atlas_api_prefix}"

    @property
    def effective_device_driver(self) -> str:
        if self.device_driver in ("f18", "mock"):
            return self.device_driver
        return "mock" if self.f18_host.strip().lower() in ("", "mock") else "f18"

    @property
    def effective_atlas_driver(self) -> str:
        if self.atlas_driver in ("http", "mock"):
            return self.atlas_driver
        low = self.atlas_base_url.strip().lower()
        return "mock" if (not low or "mock" in low) else "http"


@functools.lru_cache
def get_settings() -> Settings:
    """Process-wide singleton. Cached so every module sees the same object."""
    return Settings()
