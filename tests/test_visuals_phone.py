from datetime import datetime, timezone

import plaud_transcribe
import visuals

LO = datetime(2026, 10, 14, 13, 30, tzinfo=timezone.utc)
HI = datetime(2026, 10, 14, 14, 30, tzinfo=timezone.utc)
CONFIG = {"visuals": {"phone_dirs": ["/sdcard/DCIM/Camera"]}}


def _staging(tmp_path, *names):
    for n in names:
        (tmp_path / n).write_bytes(b"jpg")
    return tmp_path


class TestReRunWithoutThePhone:
    """14.10 backfill: re-processing a meeting with the phone unplugged dropped the
    photo of the printed margin table that the first run had already pulled."""

    def test_reuses_the_photos_an_earlier_run_pulled_so_a_reprocess_keeps_them(self, tmp_path, monkeypatch):
        staging = _staging(tmp_path, "PXL_20261014_133756423.jpg", "PXL_20261014_141120246.jpg",
                           "PXL_20261014_090000000.jpg")  # last one is outside the meeting
        monkeypatch.setattr(plaud_transcribe, "local_tz", lambda c: timezone.utc)
        monkeypatch.setattr(visuals, "_device_state", lambda adb: "absent")
        monkeypatch.setattr(visuals.shutil, "which", lambda b: "/usr/bin/adb")
        monkeypatch.setattr(visuals, "_await_phone", lambda adb: (_ for _ in ()).throw(AssertionError("prompted")))

        got = visuals._pull_phone(CONFIG, LO, HI, staging)

        assert [p.name for p in got] == ["PXL_20261014_133756423.jpg", "PXL_20261014_141120246.jpg"]

    def test_still_pulls_from_a_connected_phone_so_photos_taken_after_the_first_run_arrive(self, tmp_path, monkeypatch):
        staging = _staging(tmp_path, "PXL_20261014_133756423.jpg")
        monkeypatch.setattr(plaud_transcribe, "local_tz", lambda c: timezone.utc)
        monkeypatch.setattr(visuals, "_device_state", lambda adb: "ready")
        monkeypatch.setattr(visuals.shutil, "which", lambda b: "/usr/bin/adb")
        monkeypatch.setattr(visuals, "_await_phone", lambda adb: False)  # reached = phone path taken

        assert visuals._pull_phone(CONFIG, LO, HI, staging) == []

    def test_first_run_without_staged_photos_keeps_asking_for_the_phone(self, tmp_path, monkeypatch):
        asked = []
        monkeypatch.setattr(plaud_transcribe, "local_tz", lambda c: timezone.utc)
        monkeypatch.setattr(visuals.shutil, "which", lambda b: "/usr/bin/adb")
        monkeypatch.setattr(visuals, "_await_phone", lambda adb: asked.append(adb) or False)

        visuals._pull_phone(CONFIG, LO, HI, tmp_path)

        assert asked
