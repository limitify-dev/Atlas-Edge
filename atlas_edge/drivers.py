"""Pick the real or the mock implementation based on config.

Everywhere else just calls ``build_device(...)`` / ``build_atlas_client(...)``
and doesn't care which one it got.
"""

from __future__ import annotations

import logging

from .atlas_client import AtlasClient, SqliteTokenStore
from .config import Settings
from .storage import Storage

log = logging.getLogger("atlas_edge.drivers")


def build_device(settings: Settings, storage: Storage):
    if settings.effective_device_driver == "mock":
        from .mock import MockF18

        log.info("Device driver: MOCK (no F18 hardware)")
        return MockF18(storage)

    from .device import F18Device

    log.info("Device driver: F18 @ %s:%s", settings.f18_host, settings.f18_port)
    return F18Device(
        host=settings.f18_host,
        port=settings.f18_port,
        password=settings.f18_password,
        timeout=settings.f18_timeout,
        force_udp=settings.f18_force_udp,
        device_timezone=settings.device_timezone,
    )


def build_atlas_client(settings: Settings, storage: Storage):
    if settings.effective_atlas_driver == "mock":
        from .mock import MockAtlasClient

        log.info("Atlas driver: MOCK (events recorded locally)")
        return MockAtlasClient(
            storage,
            assignments_path=settings.mock_card_assignments_path,
            fail_rate=settings.mock_atlas_fail_rate,
        )

    log.info("Atlas driver: HTTP @ %s", settings.api_root)
    return AtlasClient(
        api_root=settings.api_root,
        device_id=settings.device_id,
        token_store=SqliteTokenStore(storage),
        timeout=settings.http_timeout_seconds,
    )
