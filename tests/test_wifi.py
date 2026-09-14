"""nmcli-driven WiFi selection for wlan0 — parsing, dedup, connect outcomes.
Every test stubs subprocess.run; nothing here touches a real radio."""

import subprocess

import pytest

from atlas_edge import wifi

# Captured before any test/fixture runs, so tests that want to exercise the
# real pause/resume logic (instead of the autouse no-op patch below) can
# restore the true originals rather than re-reading an already-patched
# lambda from wifi's own __dict__.
_REAL_PAUSE_HOTSPOT = wifi._pause_hotspot
_REAL_RESUME_HOTSPOT = wifi._resume_hotspot


class FakeCompleted:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


@pytest.fixture(autouse=True)
def _no_real_rescan(monkeypatch):
    # scan_networks() always rescans first — skip the real subprocess call
    # and the 2s settle sleep so tests stay fast and offline.
    monkeypatch.setattr(wifi, "_maybe_rescan", lambda iface: None)
    # connect() also does its own unconditional pre-connect rescan + sleep —
    # tests that stub subprocess.run already control what that "rescan"
    # returns, so just skip the real sleep to keep the suite fast.
    monkeypatch.setattr(wifi.time, "sleep", lambda *_: None)
    # connect() always pauses/resumes the ap0 hotspot around the real nmcli
    # work (sudo systemctl calls) — most tests here are about the nmcli
    # sequence itself, not this. Dedicated tests below call
    # _pause_hotspot/_resume_hotspot directly to exercise them for real.
    monkeypatch.setattr(wifi, "_pause_hotspot", lambda *a, **k: None)
    monkeypatch.setattr(wifi, "_resume_hotspot", lambda *a, **k: None)


HOTSPOT_KWARGS = dict(
    hotspot_hostapd_unit="atlas-ap0-hostapd.service",
    hotspot_dnsmasq_unit="atlas-ap0-dnsmasq.service",
    hotspot_watchdog_timer="atlas-ap0-watchdog.timer",
)


def test_split_terse_handles_escaped_colon():
    assert wifi._split_terse(r"yes:Some\:Network:78:WPA2") == ["yes", "Some:Network", "78", "WPA2"]


def test_split_terse_handles_escaped_backslash():
    assert wifi._split_terse(r"no:Back\\slash:50:") == ["no", "Back\\slash", "50", ""]


def test_scan_networks_parses_and_sorts_by_signal(monkeypatch):
    stdout = "no:Weak:20:WPA2\nno:Strong:90:WPA2\nno:Open:50:\n"
    monkeypatch.setattr(
        wifi.subprocess, "run", lambda *a, **k: FakeCompleted(0, stdout, "")
    )
    nets = wifi.scan_networks("wlan0")
    assert [n.ssid for n in nets] == ["Strong", "Open", "Weak"]
    assert nets[0].secured is True
    assert next(n for n in nets if n.ssid == "Open").secured is False


def test_scan_networks_puts_connected_network_first_even_if_weaker(monkeypatch):
    stdout = "no:Strong:95:WPA2\nyes:MySchool:40:WPA2\n"
    monkeypatch.setattr(
        wifi.subprocess, "run", lambda *a, **k: FakeCompleted(0, stdout, "")
    )
    nets = wifi.scan_networks("wlan0")
    assert nets[0].ssid == "MySchool"
    assert nets[0].connected is True
    assert nets[1].connected is False


def test_scan_networks_dedupes_by_ssid_keeping_strongest(monkeypatch):
    stdout = "no:CANALBOX:30:WPA2\nno:CANALBOX:70:WPA2\n"
    monkeypatch.setattr(
        wifi.subprocess, "run", lambda *a, **k: FakeCompleted(0, stdout, "")
    )
    nets = wifi.scan_networks("wlan0")
    assert len(nets) == 1
    assert nets[0].signal == 70


def test_scan_networks_skips_hidden_ssids(monkeypatch):
    stdout = "no::60:WPA2\nno:Visible:60:\n"
    monkeypatch.setattr(
        wifi.subprocess, "run", lambda *a, **k: FakeCompleted(0, stdout, "")
    )
    nets = wifi.scan_networks("wlan0")
    assert [n.ssid for n in nets] == ["Visible"]


def test_scan_networks_raises_wifi_error_on_nonzero_exit(monkeypatch):
    monkeypatch.setattr(
        wifi.subprocess, "run", lambda *a, **k: FakeCompleted(1, "", "nmcli: device not found")
    )
    with pytest.raises(wifi.WifiError, match="device not found"):
        wifi.scan_networks("wlan0")


def test_scan_networks_never_mentions_ap0_or_hotspot(monkeypatch):
    captured = {}

    def fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        return FakeCompleted(0, "", "")

    monkeypatch.setattr(wifi.subprocess, "run", fake_run)
    wifi.scan_networks("wlan0")
    assert "ap0" not in captured["cmd"]
    assert "wlan0" in captured["cmd"]


def test_connect_rescans_deletes_stale_profile_adds_then_brings_up(monkeypatch):
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        if cmd[:4] == ["nmcli", "connection", "up", "MySchool"]:
            return FakeCompleted(0, "Device 'wlan0' successfully activated.", "")
        return FakeCompleted(0, "", "")

    monkeypatch.setattr(wifi.subprocess, "run", fake_run)
    ok, message = wifi.connect("wlan0", "MySchool", "hunter22", **HOTSPOT_KWARGS)
    assert ok is True
    assert "successfully activated" in message
    assert calls[0] == ["nmcli", "device", "wifi", "rescan", "ifname", "wlan0"]
    assert calls[1] == ["nmcli", "connection", "delete", "MySchool"]
    assert calls[2] == [
        "nmcli", "connection", "add", "type", "wifi", "ifname", "wlan0",
        "con-name", "MySchool", "ssid", "MySchool",
        "--", "wifi-sec.key-mgmt", "wpa-psk", "wifi-sec.psk", "hunter22",
    ]
    assert calls[3] == ["nmcli", "connection", "up", "MySchool", "ifname", "wlan0"]
    # Password only ever appears as its own argv element in the "add" step —
    # never concatenated into another arg, never in the "up" step.
    assert "hunter22" not in " ".join(calls[3])


def test_connect_open_network_omits_security_fields(monkeypatch):
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        return FakeCompleted(0, "ok", "")

    monkeypatch.setattr(wifi.subprocess, "run", fake_run)
    wifi.connect("wlan0", "OpenNet", "", **HOTSPOT_KWARGS)
    add_call = calls[2]
    assert "wifi-sec.key-mgmt" not in add_call
    assert "--" not in add_call


def test_connect_add_failure_is_reported_without_attempting_up(monkeypatch):
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        if cmd[:3] == ["nmcli", "connection", "add"]:
            return FakeCompleted(1, "", "Error: failed to add connection")
        return FakeCompleted(0, "unreachable — up should never be called", "")

    monkeypatch.setattr(wifi.subprocess, "run", fake_run)
    ok, message = wifi.connect("wlan0", "MySchool", "hunter22", **HOTSPOT_KWARGS)
    assert ok is False
    assert "failed to add connection" in message
    assert not any(c[:3] == ["nmcli", "connection", "up"] for c in calls)


def test_connect_wrong_password_returns_failure_with_nmcli_message(monkeypatch):
    def fake_run(cmd, **kwargs):
        if cmd[:3] == ["nmcli", "connection", "up"]:
            return FakeCompleted(4, "", "Error: Secrets were required, but not provided.")
        return FakeCompleted(0, "", "")

    monkeypatch.setattr(wifi.subprocess, "run", fake_run)
    ok, message = wifi.connect("wlan0", "MySchool", "wrongpass", **HOTSPOT_KWARGS)
    assert ok is False
    assert "Secrets were required" in message
    assert "wrongpass" not in message


def test_connect_timeout_on_up_is_reported_as_failure_not_raised(monkeypatch):
    def fake_run(cmd, **kwargs):
        if cmd[:3] == ["nmcli", "connection", "up"]:
            raise subprocess.TimeoutExpired(cmd, kwargs.get("timeout", 0))
        return FakeCompleted(0, "", "")

    monkeypatch.setattr(wifi.subprocess, "run", fake_run)
    ok, message = wifi.connect("wlan0", "OutOfRange", "somepass", **HOTSPOT_KWARGS)
    assert ok is False
    assert "somepass" not in message


def test_connect_delete_failure_does_not_block_the_real_connect_attempt(monkeypatch):
    def fake_run(cmd, **kwargs):
        if cmd[:3] == ["nmcli", "connection", "delete"]:
            raise OSError("nmcli not found")
        if cmd[:3] == ["nmcli", "connection", "up"]:
            return FakeCompleted(0, "Device 'wlan0' successfully activated.", "")
        return FakeCompleted(0, "", "")

    monkeypatch.setattr(wifi.subprocess, "run", fake_run)
    ok, message = wifi.connect("wlan0", "MySchool", "hunter22", **HOTSPOT_KWARGS)
    assert ok is True
    assert "successfully activated" in message


def test_connect_pauses_and_resumes_the_hotspot_around_the_attempt(monkeypatch):
    # Unlike the tests above, exercise the real _pause_hotspot/_resume_hotspot
    # instead of the autouse no-op patch.
    monkeypatch.setattr(wifi, "_pause_hotspot", _REAL_PAUSE_HOTSPOT)
    monkeypatch.setattr(wifi, "_resume_hotspot", _REAL_RESUME_HOTSPOT)
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        if cmd[:4] == ["nmcli", "connection", "up", "MySchool"]:
            return FakeCompleted(0, "Device 'wlan0' successfully activated.", "")
        return FakeCompleted(0, "", "")

    monkeypatch.setattr(wifi.subprocess, "run", fake_run)
    ok, _ = wifi.connect("wlan0", "MySchool", "hunter22", **HOTSPOT_KWARGS)
    assert ok is True

    sudo_calls = [c for c in calls if c[:2] == ["/usr/bin/sudo", "-n"]]
    # Watchdog timer stopped first (so it can't fight the pause), then
    # hostapd/dnsmasq stopped, then — after the connect logic — ap0_setup.sh
    # re-run and the watchdog timer started again.
    assert sudo_calls[0] == [
        "/usr/bin/sudo", "-n", "/usr/bin/systemctl", "stop", "atlas-ap0-watchdog.timer",
    ]
    assert sudo_calls[1] == [
        "/usr/bin/sudo", "-n", "/usr/bin/systemctl", "stop", "atlas-ap0-hostapd.service",
    ]
    assert sudo_calls[2] == [
        "/usr/bin/sudo", "-n", "/usr/bin/systemctl", "stop", "atlas-ap0-dnsmasq.service",
    ]
    assert sudo_calls[-2][:3] == ["/usr/bin/sudo", "-n", wifi._AP0_SETUP_SCRIPT]
    assert sudo_calls[-1] == [
        "/usr/bin/sudo", "-n", "/usr/bin/systemctl", "start", "atlas-ap0-watchdog.timer",
    ]


def test_connect_resumes_the_hotspot_even_if_the_attempt_raises(monkeypatch):
    monkeypatch.setattr(wifi, "_pause_hotspot", _REAL_PAUSE_HOTSPOT)
    monkeypatch.setattr(wifi, "_resume_hotspot", _REAL_RESUME_HOTSPOT)
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        if cmd[:4] == ["nmcli", "device", "wifi", "rescan"]:
            raise RuntimeError("boom — something unexpected blew up mid-connect")
        return FakeCompleted(0, "", "")

    monkeypatch.setattr(wifi.subprocess, "run", fake_run)
    with pytest.raises(RuntimeError):
        wifi.connect("wlan0", "MySchool", "hunter22", **HOTSPOT_KWARGS)

    # The hotspot must still come back even though the attempt itself blew up.
    assert any(c[:3] == ["/usr/bin/sudo", "-n", wifi._AP0_SETUP_SCRIPT] for c in calls)
    assert any(
        c == ["/usr/bin/sudo", "-n", "/usr/bin/systemctl", "start", "atlas-ap0-watchdog.timer"]
        for c in calls
    )
