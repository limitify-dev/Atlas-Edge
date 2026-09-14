"""WiFi network selection for wlan0 (the station radio), driven via nmcli.

Only ever targets ``wlan0`` — never touches ``ap0``, the permanent admin
hotspot riding the same physical radio (brought up independently by
``atlas_edge/scripts/ap0_setup.sh``, driven by hostapd/dnsmasq, not
NetworkManager). Nothing here starts, stops, or reconfigures that hotspot;
it's simply never mentioned in any nmcli call this module makes. wlan0's own
association can be dropped and rejoined freely without affecting it — that
independence is the whole reason ap0 exists.

Known limitation: ``nmcli device wifi connect`` takes the password as a
plain command-line argument, so it's briefly visible to anything reading the
process list (``ps``) on the Pi while the call is in flight. That's the
standard way to drive this non-interactively; avoiding it entirely would mean
writing a temporary NetworkManager keyfile instead, which is more machinery
than this LAN-only, already-authenticated admin tool warrants.
"""

from __future__ import annotations

import logging
import subprocess
import threading
import time
from dataclasses import dataclass

log = logging.getLogger("atlas_edge.wifi")

_SCAN_TIMEOUT_SECONDS = 10
_CONNECT_TIMEOUT_SECONDS = 45
_RESCAN_MIN_INTERVAL_SECONDS = 15  # avoid hammering the radio on every page load

_last_rescan_at = 0.0
_rescan_lock = threading.Lock()


class WifiError(RuntimeError):
    """nmcli couldn't be run, or came back in a shape we can't parse."""


@dataclass(frozen=True)
class WifiNetwork:
    ssid: str
    signal: int  # 0-100
    secured: bool
    connected: bool


def _split_terse(line: str) -> list[str]:
    """Split one line of ``nmcli -t`` output on unescaped ':' — nmcli escapes
    a literal ':' within a field's value as '\\:' (and '\\' itself as
    '\\\\'), so a naive ``line.split(':')`` would corrupt any SSID that
    happens to contain a colon."""
    fields: list[str] = []
    current: list[str] = []
    i = 0
    while i < len(line):
        ch = line[i]
        if ch == "\\" and i + 1 < len(line):
            current.append(line[i + 1])
            i += 2
            continue
        if ch == ":":
            fields.append("".join(current))
            current = []
            i += 1
            continue
        current.append(ch)
        i += 1
    fields.append("".join(current))
    return fields


def _maybe_rescan(iface: str) -> None:
    global _last_rescan_at
    with _rescan_lock:
        now = time.monotonic()
        if now - _last_rescan_at < _RESCAN_MIN_INTERVAL_SECONDS:
            return
        _last_rescan_at = now
    try:
        subprocess.run(
            ["nmcli", "device", "wifi", "rescan", "ifname", iface],
            capture_output=True,
            timeout=_SCAN_TIMEOUT_SECONDS,
            check=False,
        )
        time.sleep(2)  # give the radio a moment to populate fresh results
    except (OSError, subprocess.SubprocessError) as exc:
        log.warning("wifi rescan on %s failed: %s", iface, exc)


def scan_networks(iface: str) -> list[WifiNetwork]:
    """Networks ``iface`` can currently see, deduped by SSID (the strongest
    signal — or whichever copy is marked ACTIVE — wins), sorted with the
    currently-connected network first, then strongest signal first. Hidden
    networks (empty SSID) are dropped — there's nothing to click."""
    _maybe_rescan(iface)
    try:
        proc = subprocess.run(
            [
                "nmcli", "-t", "-f", "ACTIVE,SSID,SIGNAL,SECURITY",
                "device", "wifi", "list", "ifname", iface,
            ],
            capture_output=True,
            text=True,
            timeout=_SCAN_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise WifiError(f"Could not scan for networks: {exc}") from exc
    if proc.returncode != 0:
        raise WifiError(proc.stderr.strip() or f"nmcli exited {proc.returncode}")

    by_ssid: dict[str, WifiNetwork] = {}
    for line in proc.stdout.splitlines():
        if not line.strip():
            continue
        fields = _split_terse(line)
        if len(fields) < 4:
            continue
        active_raw, ssid, signal_raw, security = fields[0], fields[1], fields[2], fields[3]
        if not ssid:
            continue
        try:
            signal = int(signal_raw)
        except ValueError:
            signal = 0
        net = WifiNetwork(
            ssid=ssid,
            signal=signal,
            secured=bool(security.strip() and security.strip() != "--"),
            connected=(active_raw == "yes"),
        )
        existing = by_ssid.get(ssid)
        if existing is None or net.connected or (not existing.connected and net.signal > existing.signal):
            by_ssid[ssid] = net

    return sorted(by_ssid.values(), key=lambda n: (not n.connected, -n.signal))


def connect(iface: str, ssid: str, password: str) -> tuple[bool, str]:
    """Blocking — always call from a background thread, never from a request
    handler. Returns ``(ok, message)``; ``message`` is nmcli's own
    stdout/stderr and never contains ``password`` (nmcli doesn't echo
    submitted secrets back), so it's always safe to surface or log."""
    cmd = ["nmcli", "device", "wifi", "connect", ssid, "ifname", iface]
    if password:
        cmd += ["password", password]
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=_CONNECT_TIMEOUT_SECONDS, check=False
        )
    except subprocess.TimeoutExpired:
        return False, "Timed out trying to connect — the network may be out of range."
    except (OSError, subprocess.SubprocessError) as exc:
        return False, f"Could not run nmcli: {exc}"
    if proc.returncode == 0:
        return True, proc.stdout.strip()
    return False, (proc.stderr.strip() or proc.stdout.strip() or f"nmcli exited {proc.returncode}")
