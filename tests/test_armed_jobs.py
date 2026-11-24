"""Pre-arming a transcript on a meeting that is still being recorded (spec Part E).

Everything runs against a fixture Wispr data dir built in tmp_path (same table shape
wispr_flow and autocapture.wispr_live_meeting read): never the real Wispr store,
~/.plaud-sync or ~/.cache. Only the paid pipeline call and notifications are stubbed."""

import json
import os
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

import autocapture
import jobs
import jobs_cli
import plaud_transcribe as pt
import runner
import visuals

UUID = "9bf98b25-0000-0000-0000-000000000000"
PAUSED = {"id": "p", "marker": "paused", "segment": 0, "epochMs": 1}
RESUMED = {"id": "r", "marker": "resumed", "segment": 1, "epochMs": 2}
STOPPED = {"id": "s", "marker": "stopped", "segment": 1, "epochMs": 3}


def _said(segment, text, start=1000, end=5000):
    return {"id": text, "text": text, "segment": segment, "startRecordingMs": start,
            "endRecordingMs": end}


class FakeWispr:
    """A Wispr Flow data dir with one meeting that started 30 minutes ago."""

    def __init__(self, root, uuid=UUID):
        self.root, self.uuid = root, uuid
        self.dir = root / "meetings" / uuid
        self.dir.mkdir(parents=True)
        created = (datetime.now(timezone.utc) - timedelta(minutes=30)).strftime(
            "%Y-%m-%d %H:%M:%S.000 +00:00")
        conn = sqlite3.connect(root / "flow.sqlite")
        conn.execute("CREATE TABLE Meetings (id TEXT, title TEXT, summary TEXT, notes TEXT, "
                     "speakerMap TEXT, participantNames TEXT, createdAt TEXT, endedAt INTEGER, "
                     "isDeleted INTEGER, finalized INTEGER)")
        conn.execute("INSERT INTO Meetings VALUES (?, 'Weekly sync', NULL, NULL, NULL, NULL, ?, NULL, 0, 0)",
                     (uuid, created))
        conn.commit()
        conn.close()
        self.live = []
        self.say(_said(0, "Zaciname."))

    def _sql(self, sql, *args):
        conn = sqlite3.connect(self.root / "flow.sqlite")
        conn.execute(sql, args)
        conn.commit()
        conn.close()

    def say(self, *lines):
        self.live += list(lines)
        (self.dir / "live.ndjson").write_text(
            "\n".join(json.dumps(o) for o in [{"meta": {"v": 3}}] + self.live) + "\n")

    def live_silent_for(self, seconds):
        t = time.time() - seconds
        os.utime(self.dir / "live.ndjson", (t, t))

    def end(self, minutes_ago=1, finalized=True):
        self._sql("UPDATE Meetings SET endedAt = ?, finalized = ?",
                  int((time.time() - minutes_ago * 60) * 1000), int(finalized))

    def refine(self):
        (self.dir / "refined.ndjson").write_text(json.dumps(
            {"id": "1", "timestamp": "00:01", "text": "Zaciname.", "speaker": {"id": 1}}) + "\n")

    def delete(self):
        self._sql("UPDATE Meetings SET isDeleted = 1")

    def config(self):
        return {"wispr": {"enabled": True, "data_dir": str(self.root)}}


@pytest.fixture
def world(tmp_path, monkeypatch):
    monkeypatch.setattr(jobs, "JOBS_DIR", tmp_path / "jobs")
    monkeypatch.setattr(runner, "LOCK_FILE", tmp_path / "runner.lock")
    monkeypatch.setattr(pt, "CONFIG_DIR", tmp_path / "cfg")
    monkeypatch.setattr(pt, "STATE_FILE", tmp_path / "cfg" / "state.json")
    monkeypatch.setattr(pt, "CONFIG_FILE", tmp_path / "cfg" / "config.json")
    monkeypatch.setattr(autocapture, "STATE_DIR", tmp_path / "ac")
    monkeypatch.setattr(autocapture, "LOG_FILE", tmp_path / "ac" / "autocapture.log")
    monkeypatch.setattr(visuals, "_interactive", True)
    monkeypatch.setattr(runner, "load_runner_env", lambda *a, **k: "env: stubbed")
    monkeypatch.setattr(runner, "preflight", lambda *a, **k: None)
    monkeypatch.delenv(runner.ARM_ENV, raising=False)
    sent = []
    monkeypatch.setattr(runner, "notify", lambda title, msg, **k: sent.append((title, msg)))
    wispr = FakeWispr(tmp_path / "wispr")
    (tmp_path / "cfg").mkdir()
    (tmp_path / "cfg" / "config.json").write_text(json.dumps(wispr.config()))

    processed = []

    def process(rec, token, config, folders, projects, state, language, **kw):
        processed.append(rec)
        out = tmp_path / "out" / rec["filename"]
        out.mkdir(parents=True, exist_ok=True)
        return {"file_id": rec["id"], "name": rec["filename"], "output_dir": str(out),
                "project": "default", "sources": [], "files": []}

    monkeypatch.setattr(pt, "process_recording", process)
    monkeypatch.setattr(pt, "prefetch_phone_files", lambda *a: None)
    monkeypatch.setattr(pt.google_recorder, "list_recordings", lambda config, days: [])
    skipped = lambda job, ctx, checkpoint=None: "skipped"  # noqa: E731  git steps are covered elsewhere
    monkeypatch.setattr(runner, "STEP_FUNCS", dict(runner.STEP_FUNCS, commit=skipped, push=skipped))
    return SimpleNamespace(wispr=wispr, sent=sent, processed=processed, tmp=tmp_path)


def arm(*extra):
    assert jobs_cli.main(["enqueue", "--arm-wispr", UUID, *extra], host=pt) == 0
    return next(j["id"] for j in jobs.list_jobs() if j.get("trigger"))


def state(jid):
    return jobs.load_job(jid)["state"]


class TestArmDuringALiveMeetingAndWalkAway:
    """D1/E1: Tomas plans the transcript while the meeting is still running and wants
    the delivered transcript afterwards without touching anything."""

    def test_the_job_is_saved_armed_with_no_recording_yet(self, world):
        jid = arm()

        job = jobs.load_job(jid)
        assert job["state"] == "armed" and job["recordings"] == []
        assert job["trigger"]["wispr"] == UUID and job["trigger"]["armed_at"]

    def test_a_meeting_that_is_still_running_is_not_transcribed_half_finished(self, world):
        jid = arm()

        assert runner.run_queue(pt) == []

        assert state(jid) == "armed" and world.processed == []

    def test_when_the_meeting_ends_and_is_refined_the_same_runner_pass_delivers_it(self, world):
        jid = arm()
        world.wispr.end()
        world.wispr.refine()

        assert runner.run_queue(pt) == [jid]

        job = jobs.load_job(jid)
        assert job["state"] == "done"
        assert job["recordings"] == [f"wispr:{UUID}"]
        assert [r["id"] for r in world.processed] == [f"wispr:{UUID}"]
        assert [s["state"] for s in job["steps"]] == ["done", "skipped", "skipped", "skipped"]
        assert any(t == "Plaud transcript ready" for t, _ in world.sent)

    def test_the_meeting_may_end_while_a_long_job_runs_and_is_still_picked_up(self, world, monkeypatch):
        other = jobs.new_job(recordings=["wispr:other"])
        jobs.save_job(other)
        jid = arm()

        def process(rec, *a, **k):
            if rec["id"] == "wispr:other":
                world.wispr.end()
                world.wispr.refine()
            return {"file_id": rec["id"], "name": "x", "output_dir": str(world.tmp / "o"),
                    "project": "default", "sources": [], "files": []}

        monkeypatch.setattr(pt, "process_recording", process)
        monkeypatch.setattr(pt.wispr_flow, "list_meetings", lambda c, d: [
            {"id": "wispr:other", "filename": "o", "source": "wispr"},
            {"id": f"wispr:{UUID}", "filename": "w", "source": "wispr"}] if world.wispr.live and
            (world.wispr.dir / "refined.ndjson").exists() else
            [{"id": "wispr:other", "filename": "o", "source": "wispr"}])

        ran = runner.run_queue(pt)

        assert ran == [other["id"], jid]


class TestPausedThenResumedMeetingDoesNotFireAtThePause:
    """E2, the 2026-11-02 case: Wispr sets endedAt at a pause and keeps it after the
    resume, and has already refined the first segment, so the pause looks like an end."""

    def test_at_the_pause_the_job_stays_armed_although_the_first_segment_is_refined(self, world):
        jid = arm()
        world.wispr.say(PAUSED)
        world.wispr.end(minutes_ago=1)
        world.wispr.refine()

        assert runner.meeting_ready(pt.load_config(), UUID) == "waiting"
        assert runner.run_queue(pt) == []
        assert state(jid) == "armed"

    def test_after_the_resume_the_meeting_counts_as_live_again(self, world):
        jid = arm()
        world.wispr.say(PAUSED)
        world.wispr.end(minutes_ago=25)
        world.wispr.refine()
        world.wispr.say(RESUMED, _said(1, "Pokracujeme."))

        assert runner.meeting_ready(pt.load_config(), UUID) == "live"
        assert runner.run_queue(pt) == []
        assert state(jid) == "armed"

    def test_after_the_real_end_it_is_promoted_and_delivered(self, world):
        jid = arm()
        world.wispr.say(PAUSED)
        world.wispr.end(minutes_ago=25)
        world.wispr.refine()
        world.wispr.say(RESUMED, _said(1, "Pokracujeme."))
        assert runner.run_queue(pt) == []

        world.wispr.say(STOPPED)
        world.wispr.end(minutes_ago=1)

        assert runner.run_queue(pt) == [jid]
        assert state(jid) == "done"

    def test_a_pause_nobody_resumed_is_promoted_once_the_fallback_time_has_passed(self, world):
        arm()
        world.wispr.say(PAUSED)
        world.wispr.end(minutes_ago=45)
        world.wispr.refine()
        world.wispr.live_silent_for(45 * 60)

        assert runner.meeting_ready(pt.load_config(), UUID) == "ready"


class TestMeetingEndedButTranscriptNotThereYet:
    """list_meetings holds a meeting back until Wispr refined it or live_fallback_minutes
    passed; the job must neither run on nothing nor be given up on."""

    def test_unrefined_just_after_the_end_the_job_keeps_waiting(self, world):
        jid = arm()
        world.wispr.end(minutes_ago=5)

        assert runner.meeting_ready(pt.load_config(), UUID) == "waiting"
        assert runner.run_queue(pt) == []
        assert state(jid) == "armed" and world.sent == []

    def test_unrefined_after_the_fallback_minutes_the_live_transcript_is_used(self, world):
        jid = arm()
        world.wispr.end(minutes_ago=40)

        assert runner.run_queue(pt) == [jid]
        assert world.processed[0]["_wispr"]["transcript_from"] == "live"

    def test_a_meeting_not_yet_finalized_by_wispr_is_not_ready(self, world):
        world.wispr.end(minutes_ago=40, finalized=False)

        assert runner.meeting_ready(pt.load_config(), UUID) == "waiting"


class TestMeetingThatWillNeverArrive:
    """E3: a job armed on a deleted or never-ending meeting must say so, not wait forever."""

    def test_a_deleted_meeting_fails_the_job_with_one_notification(self, world):
        jid = arm()
        world.wispr.delete()

        assert runner.run_queue(pt) == []
        runner.run_queue(pt)

        job = jobs.load_job(jid)
        assert job["state"] == "failed" and "deleted" in job["error"] and job["failed_step"] == "arm"
        assert [t for t, _ in world.sent] == ["Plaud armed job failed"]

    def test_a_meeting_that_never_ends_fails_the_job_after_max_hours(self, world):
        jid = arm()
        job = jobs.load_job(jid)
        job["trigger"]["armed_at"] = (datetime.now().astimezone() - timedelta(hours=3, minutes=5)).isoformat()
        jobs.save_job(job)

        runner.run_queue(pt)

        job = jobs.load_job(jid)
        assert job["state"] == "failed" and "3 h" in job["error"]
        assert [t for t, _ in world.sent] == ["Plaud armed job failed"]

    def test_inside_max_hours_the_job_is_left_alone(self, world):
        jid = arm()
        job = jobs.load_job(jid)
        job["trigger"]["armed_at"] = (datetime.now().astimezone() - timedelta(hours=2)).isoformat()
        jobs.save_job(job)

        runner.run_queue(pt)

        assert state(jid) == "armed" and world.sent == []

    def test_the_limit_is_tunable_by_flag_env_and_config(self, world, monkeypatch):
        assert runner.arm_max_hours({}) == 3
        assert runner.arm_max_hours({"visuals": {"autocapture": {"max_hours": 4}}}) == 4
        assert runner.arm_max_hours({"arm": {"max_hours": 6}, "visuals": {"autocapture": {"max_hours": 4}}}) == 6
        monkeypatch.setenv(runner.ARM_ENV, "8")
        assert runner.arm_max_hours({"arm": {"max_hours": 6}}) == 8
        assert runner.arm_max_hours({}, flag=1.5) == 1.5

    def test_a_failed_armed_job_can_be_re_armed_with_retry(self, world):
        jid = arm()
        world.wispr.delete()
        runner.run_queue(pt)

        jobs.retry_job(jid)

        job = jobs.load_job(jid)
        assert job["state"] == "armed" and job["recordings"] == [] and job["error"] is None


class TestRunnerWithoutWispr:
    """Wispr is optional; the runner LaunchAgent fires every minute on machines without it."""

    def test_without_a_wispr_store_armed_jobs_wait_and_nothing_crashes(self, world, tmp_path):
        jid = arm()
        (world.wispr.root / "flow.sqlite").unlink()

        assert runner.run_queue(pt) == []
        assert runner.meeting_ready(pt.load_config(), UUID) == "waiting"
        assert state(jid) == "armed"

    def test_without_wispr_the_wait_ends_at_max_hours_like_any_other(self, world):
        jid = arm()
        (world.wispr.root / "flow.sqlite").unlink()
        job = jobs.load_job(jid)
        job["trigger"]["armed_at"] = (datetime.now().astimezone() - timedelta(hours=4)).isoformat()
        jobs.save_job(job)

        runner.run_queue(pt)

        assert state(jid) == "failed"

    def test_a_store_with_an_unexpected_schema_leaves_the_job_armed(self, world):
        jid = arm()
        (world.wispr.root / "flow.sqlite").unlink()
        sqlite3.connect(world.wispr.root / "flow.sqlite").close()

        assert runner.run_queue(pt) == []
        assert state(jid) == "armed"


class TestJobsPromoteByHand:
    def test_promote_reports_what_changed_and_does_not_run_the_job(self, world, capsys):
        jid = arm()
        world.wispr.end()
        world.wispr.refine()
        capsys.readouterr()

        assert jobs_cli.main(["jobs", "promote"], host=pt) == 0

        assert f"{jid}: queued" in capsys.readouterr().out
        assert state(jid) == "queued" and world.processed == []

    def test_promote_says_so_when_nothing_is_armed(self, world, capsys):
        assert jobs_cli.main(["jobs", "promote"], host=pt) == 0
        assert "no armed jobs" in capsys.readouterr().out

    def test_promote_leaves_a_live_meeting_armed(self, world, capsys):
        jid = arm()

        jobs_cli.main(["jobs", "promote"], host=pt)

        assert f"{jid}: still armed" in capsys.readouterr().out

    def test_promote_does_not_race_a_running_runner(self, world, capsys):
        arm()
        lock = runner.acquire_lock()
        try:
            assert jobs_cli.main(["jobs", "promote"], host=pt) == 0
        finally:
            lock.close()

        assert "a runner is active" in capsys.readouterr().out


class TestEnqueueArmWispr:
    def test_live_arms_the_meeting_being_recorded_now(self, world, capsys):
        assert jobs_cli.main(["enqueue", "--arm-wispr", "live"], host=pt) == 0

        job = jobs.list_jobs()[-1]
        assert job["state"] == "armed" and job["trigger"]["wispr"] == UUID

    def test_live_without_a_live_meeting_is_an_error_not_a_job_that_waits_for_nothing(self, world, capsys):
        world.wispr.end()

        assert jobs_cli.main(["enqueue", "--arm-wispr", "live"], host=pt) == 2

        assert "no Wispr meeting is live" in capsys.readouterr().err
        assert jobs.list_jobs() == []

    @pytest.mark.parametrize("flags", [["--recording", "plaud:aaa"], ["--merge"], ["--cut", "wispr:x=1:00-2:00"]])
    def test_recording_flags_are_refused_because_the_job_has_no_recording_yet(self, world, capsys, flags):
        assert jobs_cli.main(["enqueue", "--arm-wispr", UUID, *flags], host=pt) == 2

        assert "cannot be combined" in capsys.readouterr().err
        assert jobs.list_jobs() == []

    def test_a_wispr_prefix_on_the_uuid_is_accepted(self, world):
        assert jobs_cli.main(["enqueue", "--arm-wispr", f"wispr:{UUID}"], host=pt) == 0
        assert jobs.list_jobs()[-1]["trigger"]["wispr"] == UUID

    def test_dry_run_writes_nothing(self, world, capsys):
        assert jobs_cli.main(["enqueue", "--arm-wispr", UUID, "--dry-run"], host=pt) == 0
        assert jobs.list_jobs() == []
        assert '"armed"' in capsys.readouterr().out

    def test_jobs_list_shows_what_an_armed_job_waits_for(self, world, capsys):
        arm()
        capsys.readouterr()

        jobs_cli.main(["jobs", "list"], host=pt)

        assert f"waiting for wispr:{UUID}" in capsys.readouterr().out
