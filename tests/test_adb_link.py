import subprocess

import adb_link
import google_recorder
import visuals

MDNS = ("List of discovered mdns services\n"
        "adb-4C07-Yg3Vw9\t_adb-tls-connect._tcp\t10.0.0.107:38467\n"
        "adb-4C07-Yg3Vw9\t_adb._tcp\t10.0.0.107:5555\n")


import pytest


@pytest.fixture(autouse=True)
def no_waiting(monkeypatch):
    monkeypatch.setattr(adb_link.time, "sleep", lambda s: None)


def fake_adb(monkeypatch, devices, mdns=MDNS, connect="connected to 10.0.0.107:38467"):
    """`devices` is a list of successive `adb devices` bodies; the last one repeats.
    Returns the list of adb subcommands that were run."""
    calls, seen = [], []

    def run(cmd, **kw):
        sub = cmd[1]
        calls.append(sub)
        if sub == "devices":
            body = devices[min(calls.count("devices") - 1, len(devices) - 1)]
            out = "List of devices attached\n" + body
        elif sub == "mdns":
            out = mdns
        else:
            out = connect
        return subprocess.CompletedProcess(cmd, 0, out, "")

    monkeypatch.setattr(adb_link.subprocess, "run", run)
    return calls


class TestPhoneOnlyOverWifi:
    """Cable unplugged, wireless debugging on: the port changes on every toggle,
    so the phone has to be discovered, not configured."""

    def test_connects_to_the_advertised_port_so_no_cable_is_needed(self, monkeypatch):
        calls = fake_adb(monkeypatch, ["", "10.0.0.107:38467\tdevice\n"])

        assert adb_link.find_phone("adb") == "ready"
        assert calls == ["devices", "mdns", "connect", "devices", "devices"]

    def test_cable_wins_without_touching_the_network(self, monkeypatch):
        calls = fake_adb(monkeypatch, ["ABC\tdevice\n"])

        assert adb_link.find_phone("adb") == "ready"
        assert calls == ["devices"]

    def test_unauthorised_phone_is_reported_so_the_user_is_told_to_tap_accept(self, monkeypatch):
        fake_adb(monkeypatch, ["", "10.0.0.107:38467\tunauthorized\n"])

        assert adb_link.find_phone("adb") == "unauthorized"

    def test_nothing_advertised_means_absent(self, monkeypatch):
        fake_adb(monkeypatch, [""], mdns="List of discovered mdns services\n")

        assert adb_link.find_phone("adb") == "absent"


class TestAdbAlreadyAutoConnected:
    """adb's mDNS auto-connect can win the race; the same phone on two transports
    breaks every un-targeted `adb shell` / `adb pull` ("more than one device")."""

    def test_drops_the_duplicate_transport_we_just_added(self, monkeypatch):
        both = "adb-4C07._adb-tls-connect._tcp\tdevice\n10.0.0.107:38467\tdevice\n"
        calls = fake_adb(monkeypatch, ["", both])

        assert adb_link.find_phone("adb") == "ready"
        assert "disconnect" in calls


class TestUnpairedMac:
    """A phone that is advertised but refuses `adb connect` has never been paired
    with this Mac; asking for the code mid-run would hang a pipeline."""

    def test_prints_pairing_steps_instead_of_prompting(self, monkeypatch):
        fake_adb(monkeypatch, [""], connect="failed to connect to 10.0.0.107:38467")
        said = []

        assert adb_link.find_phone("adb", said.append) == "absent"
        assert len(said) == 1 and "adb pair" in said[0]

    def test_pairing_steps_are_said_once_per_run_however_often_the_user_retries(self, monkeypatch):
        monkeypatch.setattr(visuals, "_pair_hint_shown", False)
        fake_adb(monkeypatch, [""], connect="failed to connect to 10.0.0.107:38467")

        visuals._device_state("adb")
        visuals._device_state("adb")

        assert visuals._pair_hint_shown


class TestRecorderUsesTheSameLink:
    def test_recordings_are_listed_over_wifi_with_the_cable_unplugged(self, monkeypatch):
        fake_adb(monkeypatch, ["", "10.0.0.107:38467\tdevice\n"])

        assert google_recorder._device_ready("adb") is True
