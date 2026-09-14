"""WiFi network selection for wlan0 (the station radio), driven via nmcli.

Only ever targets ``wlan0`` — never touches ``ap0``, the permanent admin
hotspot riding the same physical radio (brought up independently by
``atlas_edge/scripts/ap0_setup.sh``, driven by hostapd/dnsmasq, not
NetworkManager). ``scan_networks()`` never mentions ap0. ``connect()`` is the
one exception: confirmed live that this radio (rtl8xxxu / RTL8188FTV) can't
reliably scan for and associate to a *new* network while ap0 is actively
beaconing — wlan0's own scan silently fails to find an in-range AP the whole
time ap0 is up, even though normal data traffic on both works fine once
wlan0 is already associated. So a connect attempt briefly pauses ap0 for its
duration, then brings it back via ap0_setup.sh (which also realigns ap0's
channel to wherever wlan0 ends up — they share one radio and must match).

Known limitation: nmcli takes the password as a plain command-line argument,
so it's briefly visible to anything reading the process list (``ps``) on the
Pi while the call is in flight. That's the standard way to drive this
non-interactively; avoiding it entirely would mean writing a temporary
NetworkManager keyfile instead, which is more machinery than this LAN-only,
already-authenticated admin tool warrants.
"""

from __future__ import annotations

import logging
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger("atlas_edge.wifi")

_SCAN_TIMEOUT_SECONDS = 10
_ADD_TIMEOUT_SECONDS = 10
_CONNECT_TIMEOUT_SECONDS = 45
_HOTSPOT_STOP_TIMEOUT_SECONDS = 15
_HOTSPOT_RESUME_TIMEOUT_SECONDS = 90
_RESCAN_MIN_INTERVAL_SECONDS = 15  # avoid hammering the radio on every page load

_last_rescan_at = 0.0
_rescan_lock = threading.Lock()

_SUDO = "/usr/bin/sudo"
_SYSTEMCTL = "/usr/bin/systemctl"
_NMCLI = "/usr/bin/nmcli"
# Absolute, not derived from ATLAS_EDGE_* env at runtime — this must match
# exactly what the sudoers grant on the Pi allows (see
# systemd/ap0-wifi-sudoers), and the script's own location is stable
# relative to this file regardless of where the repo is checked out.
_AP0_SETUP_SCRIPT = str(Path(__file__).parent / "scripts" / "ap0_setup.sh")


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


def _sudo_systemctl(action: str, unit: str, timeout: int) -> None:
    try:
        subprocess.run(
            [_SUDO, "-n", _SYSTEMCTL, action, unit],
            capture_output=True, timeout=timeout, check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        log.warning("sudo systemctl %s %s failed: %s", action, unit, exc)


def _pause_hotspot(hostapd_unit: str, dnsmasq_unit: str, watchdog_timer: str) -> None:
    """Stop ap0's services, and the watchdog that would otherwise silently
    bring them back mid-attempt (it treats a stopped hostapd/dnsmasq as
    unhealthy and auto-recovers within a few minutes). Best-effort — if this
    fails, still attempt the connect rather than blocking on it; a failed
    pause just means the original scan-contention problem might recur."""
    log.info("Pausing ap0 hotspot for a wlan0 reconnect attempt")
    _sudo_systemctl("stop", watchdog_timer, _HOTSPOT_STOP_TIMEOUT_SECONDS)
    _sudo_systemctl("stop", hostapd_unit, _HOTSPOT_STOP_TIMEOUT_SECONDS)
    _sudo_systemctl("stop", dnsmasq_unit, _HOTSPOT_STOP_TIMEOUT_SECONDS)


def _resume_hotspot(watchdog_timer: str) -> None:
    """Bring ap0 back — always, even if the connect attempt failed or
    raised (never leave the admin hotspot down). Re-runs the full setup
    script rather than just restarting hostapd/dnsmasq, since it also
    realigns ap0's channel to wherever wlan0 ended up."""
    log.info("Resuming ap0 hotspot after wlan0 reconnect attempt")
    try:
        subprocess.run(
            [_SUDO, "-n", _AP0_SETUP_SCRIPT],
            capture_output=True, timeout=_HOTSPOT_RESUME_TIMEOUT_SECONDS, check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        log.warning("ap0 resume via setup script failed: %s", exc)
    _sudo_systemctl("start", watchdog_timer, _HOTSPOT_STOP_TIMEOUT_SECONDS)


def connect(
    iface: str,
    ssid: str,
    password: str,
    *,
    hotspot_hostapd_unit: str,
    hotspot_dnsmasq_unit: str,
    hotspot_watchdog_timer: str,
) -> tuple[bool, str]:
    """Blocking — always call from a background thread, never from a request
    handler. Returns ``(ok, message)``; ``message`` is nmcli's own
    stdout/stderr and never contains ``password`` (nmcli doesn't echo
    submitted secrets back), so it's always safe to surface or log."""
    _pause_hotspot(hotspot_hostapd_unit, hotspot_dnsmasq_unit, hotspot_watchdog_timer)
    try:
        return _connect_wlan0(iface, ssid, password)
    finally:
        _resume_hotspot(hotspot_watchdog_timer)


def _connect_wlan0(iface: str, ssid: str, password: str) -> tuple[bool, str]:
    # Force a fresh scan now that ap0 is out of the way — nmcli/wpa_supplicant
    # need a current scan result for this AP to build a correct profile from.
    try:
        subprocess.run(
            ["nmcli", "device", "wifi", "rescan", "ifname", iface],
            capture_output=True, timeout=_SCAN_TIMEOUT_SECONDS, check=False,
        )
        time.sleep(2)
    except (OSError, subprocess.SubprocessError) as exc:
        log.warning("pre-connect rescan on %s failed: %s", iface, exc)

    # Best-effort: drop any existing profile with this name first (nmcli
    # names one after the SSID by default) so we always start from a clean
    # slate — fine if there's nothing to delete.
    # From here on, every nmcli call modifies a system connection profile —
    # unlike scanning/listing (unprivileged, works fine as-is) or the old
    # one-shot `device wifi connect` convenience command, NetworkManager's
    # polkit policy treats persisting a named connection as a privileged
    # operation ("Insufficient privileges" otherwise for a non-root,
    # non-active-session process like this one). Routed through the
    # narrowly-scoped sudoers grant in systemd/ap0-wifi-sudoers.
    try:
        subprocess.run(
            [_SUDO, "-n", _NMCLI, "connection", "delete", ssid],
            capture_output=True, timeout=10, check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        log.warning("pre-connect delete of stale profile %r failed: %s", ssid, exc)

    # Build the profile explicitly instead of nmcli's one-shot `device wifi
    # connect <ssid> password <pw>`, which auto-detects security from the
    # scan and has proven unreliable on this system — it's produced
    # "802-11-wireless-security.key-mgmt: property is missing" even
    # immediately after a fresh rescan showed the AP with correct security
    # info. Telling nmcli directly what key-mgmt/psk to use sidesteps that
    # detection step entirely (the same approach used for the ap0 hotspot's
    # own profile, which has never hit this problem).
    add_cmd = [
        _SUDO, "-n", _NMCLI, "connection", "add",
        "type", "wifi",
        "ifname", iface,
        "con-name", ssid,
        "ssid", ssid,
    ]
    if password:
        add_cmd += ["--", "wifi-sec.key-mgmt", "wpa-psk", "wifi-sec.psk", password]
    try:
        proc = subprocess.run(
            add_cmd, capture_output=True, text=True, timeout=_ADD_TIMEOUT_SECONDS, check=False
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return False, f"Could not create connection profile: {exc}"
    if proc.returncode != 0:
        return False, (proc.stderr.strip() or proc.stdout.strip() or f"nmcli exited {proc.returncode}")

    up_cmd = [_SUDO, "-n", _NMCLI, "connection", "up", ssid, "ifname", iface]
    try:
        proc = subprocess.run(
            up_cmd, capture_output=True, text=True, timeout=_CONNECT_TIMEOUT_SECONDS, check=False
        )
    except subprocess.TimeoutExpired:
        return False, "Timed out trying to connect — the network may be out of range."
    except (OSError, subprocess.SubprocessError) as exc:
        return False, f"Could not run nmcli: {exc}"
    if proc.returncode == 0:
        return True, proc.stdout.strip()
    return False, (proc.stderr.strip() or proc.stdout.strip() or f"nmcli exited {proc.returncode}")
