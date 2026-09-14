"""nmcli-driven WiFi selection for wlan0 — parsing, dedup, connect outcomes.
Every test stubs subprocess.run; nothing here touches a real radio."""

import subprocess

import pytest

from atlas_edge import wifi


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


def test_connect_success_returns_ok_and_message(monkeypatch):
    captured = {}

    def fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        return FakeCompleted(0, "Device 'wlan0' successfully activated.", "")

    monkeypatch.setattr(wifi.subprocess, "run", fake_run)
    ok, message = wifi.connect("wlan0", "MySchool", "hunter22")
    assert ok is True
    assert "successfully activated" in message
    assert "ap0" not in captured["cmd"]
    assert "hunter22" not in " ".join(captured["cmd"][:-1])  # password is its own arg, not concatenated


def test_connect_wrong_password_returns_failure_with_nmcli_message(monkeypatch):
    monkeypatch.setattr(
        wifi.subprocess,
        "run",
        lambda *a, **k: FakeCompleted(4, "", "Error: Secrets were required, but not provided."),
    )
    ok, message = wifi.connect("wlan0", "MySchool", "wrongpass")
    assert ok is False
    assert "Secrets were required" in message
    assert "wrongpass" not in message


def test_connect_open_network_omits_password_flag(monkeypatch):
    captured = {}

    def fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        return FakeCompleted(0, "ok", "")

    monkeypatch.setattr(wifi.subprocess, "run", fake_run)
    wifi.connect("wlan0", "OpenNet", "")
    assert "password" not in captured["cmd"]


def test_connect_timeout_is_reported_as_failure_not_raised(monkeypatch):
    def fake_run(cmd, **kwargs):
        raise subprocess.TimeoutExpired(cmd, kwargs.get("timeout", 0))

    monkeypatch.setattr(wifi.subprocess, "run", fake_run)
    ok, message = wifi.connect("wlan0", "OutOfRange", "somepass")
    assert ok is False
    assert "somepass" not in message


def test_connect_rescans_then_deletes_stale_profile_then_connects(monkeypatch):
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        if cmd[:3] == ["nmcli", "connection", "delete"]:
            return FakeCompleted(0, "", "")
        return FakeCompleted(0, "Device 'wlan0' successfully activated.", "")

    monkeypatch.setattr(wifi.subprocess, "run", fake_run)
    ok, _ = wifi.connect("wlan0", "MySchool", "hunter22")
    assert ok is True
    assert calls[0] == ["nmcli", "device", "wifi", "rescan", "ifname", "wlan0"]
    assert calls[1] == ["nmcli", "connection", "delete", "MySchool"]
    assert calls[2][:4] == ["nmcli", "device", "wifi", "connect"]


def test_connect_delete_failure_does_not_block_the_real_connect_attempt(monkeypatch):
    def fake_run(cmd, **kwargs):
        if cmd[:3] == ["nmcli", "connection", "delete"]:
            raise OSError("nmcli not found")
        return FakeCompleted(0, "Device 'wlan0' successfully activated.", "")

    monkeypatch.setattr(wifi.subprocess, "run", fake_run)
    ok, message = wifi.connect("wlan0", "MySchool", "hunter22")
    assert ok is True
    assert "successfully activated" in message
