from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

import plaud_transcribe
import visuals

# Two meetings on 14.10: 13:30-14:00 and 16:00-16:30 UTC (tail 30 min each).
MEETING_A = {"id": "plaud:a", "source": "plaud", "filename": "A",
             "start_time": int(datetime(2026, 10, 14, 13, 30, tzinfo=timezone.utc).timestamp() * 1000),
             "duration": 30 * 60 * 1000}
MEETING_B = {"id": "plaud:b", "source": "plaud", "filename": "B",
             "start_time": int(datetime(2026, 10, 14, 16, 0, tzinfo=timezone.utc).timestamp() * 1000),
             "duration": 30 * 60 * 1000}
CONFIG = {"visuals": {"enabled": True, "phone_dirs": ["/sdcard/DCIM/Camera"]}}
PHONE = ["PXL_20261014_134000000.jpg",   # in A
         "PXL_20261014_161500000.jpg",   # in B
         "PXL_20261014_090000000.jpg"]   # in neither


class FakeAdb:
    """Records every adb call; `pull` copies the requested names into the target dir."""

    def __init__(self, listing):
        self.listing = listing
        self.pulls = []

    def run(self, cmd, **kw):
        if cmd[1] == "shell":
            return SimpleNamespace(stdout="\n".join(self.listing), returncode=0)
        if cmd[1] == "pull":
            *remotes, dest = cmd[2:]
            self.pulls.append([r.rsplit("/", 1)[-1] for r in remotes])
            for r in remotes:
                (plaud_transcribe.Path(dest) / r.rsplit("/", 1)[-1]).write_bytes(b"jpg")
            return SimpleNamespace(stdout="", returncode=0)
        raise AssertionError(cmd)


@pytest.fixture
def phone(monkeypatch):
    adb = FakeAdb(PHONE)
    asked = []
    monkeypatch.setattr(visuals, "_prefetched", set())
    monkeypatch.setattr(plaud_transcribe, "local_tz", lambda c: timezone.utc)
    monkeypatch.setattr(visuals.shutil, "which", lambda b: "/usr/bin/adb")
    monkeypatch.setattr(visuals.subprocess, "run", adb.run)
    monkeypatch.setattr(visuals, "_await_phone", lambda a: asked.append(a) or True)
    adb.asked = asked
    return adb


class TestOnePhonePhaseForTheWholeSelection:
    """Processing several recordings used to need the phone again in each one's
    visuals step, after its transcription — so it stayed plugged in all run."""

    def test_pulls_every_meetings_photos_with_a_single_phone_prompt(self, tmp_path, phone):
        groups = [([MEETING_A], tmp_path / "a"), ([MEETING_B], tmp_path / "b")]

        used, staged = visuals.prefetch_phone(groups, CONFIG)

        assert (used, staged) == (True, 2)
        assert len(phone.asked) == 1
        assert [p.name for p in (tmp_path / "a").iterdir()] == ["PXL_20261014_134000000.jpg"]
        assert [p.name for p in (tmp_path / "b").iterdir()] == ["PXL_20261014_161500000.jpg"]

    def test_lists_each_phone_dir_once_and_pulls_a_meetings_photos_in_one_adb_call(self, tmp_path, phone, monkeypatch):
        phone.listing = PHONE + ["PXL_20261014_134500000.jpg"]
        calls = []
        orig = phone.run
        monkeypatch.setattr(visuals.subprocess, "run", lambda cmd, **kw: calls.append(cmd[1]) or orig(cmd, **kw))

        visuals.prefetch_phone([([MEETING_A], tmp_path / "a"), ([MEETING_B], tmp_path / "b")], CONFIG)

        assert calls.count("shell") == 1
        assert phone.pulls[0] == ["PXL_20261014_134000000.jpg", "PXL_20261014_134500000.jpg"]

    def test_does_not_pull_again_what_an_earlier_run_already_staged(self, tmp_path, phone):
        (tmp_path / "a").mkdir()
        (tmp_path / "a" / "PXL_20261014_134000000.jpg").write_bytes(b"jpg")

        visuals.prefetch_phone([([MEETING_A], tmp_path / "a")], CONFIG)

        assert phone.pulls == []


class TestAfterThePhoneWasAnnouncedUnplugged:
    """Once the run said 'you can unplug the phone now', no later step may ask for it."""

    def test_a_meeting_uses_its_staged_photos_without_touching_the_phone(self, tmp_path, phone, monkeypatch):
        visuals.prefetch_phone([([MEETING_A], tmp_path / "a")], CONFIG)
        monkeypatch.setattr(visuals, "_device_state", lambda a: (_ for _ in ()).throw(AssertionError("phone touched")))
        monkeypatch.setattr(visuals, "_await_phone", lambda a: (_ for _ in ()).throw(AssertionError("prompted")))
        lo, hi = visuals.window([MEETING_A], CONFIG)

        got = visuals._pull_phone(CONFIG, lo, hi, tmp_path / "a")

        assert [p.name for p in got] == ["PXL_20261014_134000000.jpg"]

    def test_a_meeting_without_any_phone_photos_does_not_prompt_either(self, tmp_path, phone, monkeypatch):
        phone.listing = []
        visuals.prefetch_phone([([MEETING_A], tmp_path / "a")], CONFIG)
        monkeypatch.setattr(visuals, "_await_phone", lambda a: (_ for _ in ()).throw(AssertionError("prompted")))
        lo, hi = visuals.window([MEETING_A], CONFIG)

        assert visuals._pull_phone(CONFIG, lo, hi, tmp_path / "a") == []

    def test_declining_the_phone_once_is_not_asked_again_for_the_next_meeting(self, tmp_path, phone, monkeypatch):
        monkeypatch.setattr(visuals, "_await_phone", lambda a: phone.asked.append(a) or False)

        assert visuals.prefetch_phone([([MEETING_A], tmp_path / "a")], CONFIG) == (False, 0)
        lo, hi = visuals.window([MEETING_A], CONFIG)
        visuals._pull_phone(CONFIG, lo, hi, tmp_path / "a")

        assert len(phone.asked) == 1


class TestRecorderAudioAndPhotosComeOffThePhoneBeforeTranscription:
    """Google Recorder: photos were pulled only after ElevenLabs had finished, so
    the phone had to stay connected through the whole transcription."""

    def test_selection_copies_audio_and_photos_before_any_recording_is_processed(self, tmp_path, monkeypatch):
        rec = {**MEETING_A, "id": "recorder:1", "source": "recorder",
               "_recorder": {"path": "/sdcard/Recordings/a.m4a", "size": 3}}
        order = []
        monkeypatch.setattr(plaud_transcribe, "WORKING_DIR", tmp_path)
        monkeypatch.setattr(plaud_transcribe.google_recorder, "pull",
                            lambda r, c, d: order.append("audio") or _file(tmp_path / "a.m4a"))
        monkeypatch.setattr(visuals, "prefetch_phone",
                            lambda groups, c, on_ready=None: order.append(("photos", [str(g[1]) for g in groups])) or (True, 0))
        monkeypatch.setattr(plaud_transcribe, "process_recording",
                            lambda *a, **k: order.append("process") or None)
        monkeypatch.setattr(plaud_transcribe, "git_commit_working", lambda *a: None)

        plaud_transcribe.execute_selection([rec], False, None, CONFIG, {}, {}, {}, "cs")

        assert order == ["audio", ("photos", [str(tmp_path / "recorder_1" / "phone")]), "process"]

    def test_a_merged_session_stages_photos_where_its_visuals_step_reads_them(self, tmp_path, monkeypatch):
        seen = []
        monkeypatch.setattr(plaud_transcribe, "WORKING_DIR", tmp_path)
        monkeypatch.setattr(visuals, "prefetch_phone", lambda groups, c, on_ready=None: seen.extend(groups) or (False, 0))

        plaud_transcribe.prefetch_phone_files([MEETING_B, MEETING_A], True, CONFIG)

        assert [g[1] for g in seen] == [plaud_transcribe.merged_work_dir_for([MEETING_A, MEETING_B]) / "phone"]

    def test_plaud_only_run_without_visuals_never_looks_for_a_phone(self, monkeypatch):
        monkeypatch.setattr(visuals, "prefetch_phone", lambda *a: (_ for _ in ()).throw(AssertionError("phone")))

        plaud_transcribe.prefetch_phone_files([MEETING_A], False, {})


def _file(path):
    path.write_bytes(b"abc")
    return path


class TestRunWithoutAPhone:
    """With no phone attached, the run printed 'Phone: copying…' and then nothing,
    which reads like a hang or a silent failure."""

    def test_prints_no_copying_header_when_the_phone_never_showed_up(self, tmp_path, phone, monkeypatch, capsys):
        monkeypatch.setattr(plaud_transcribe, "WORKING_DIR", tmp_path)
        monkeypatch.setattr(visuals, "_await_phone", lambda a: False)

        plaud_transcribe.prefetch_phone_files([MEETING_A], False, CONFIG)

        assert "Phone: copying" not in capsys.readouterr().out

    def test_announces_the_copying_once_the_phone_is_there(self, tmp_path, phone, monkeypatch, capsys):
        monkeypatch.setattr(plaud_transcribe, "WORKING_DIR", tmp_path)

        plaud_transcribe.prefetch_phone_files([MEETING_A, MEETING_B], False, CONFIG)

        out = capsys.readouterr().out
        assert out.count("Phone: copying") == 1
        assert out.index("Phone: copying") < out.index("you can unplug")
