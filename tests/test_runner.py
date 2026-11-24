import fcntl
import json
from types import SimpleNamespace

import pytest

import jobs
import jobs_cli
import plaud_transcribe as pt
import runner
import visuals


@pytest.fixture
def env(monkeypatch, tmp_path):
    """Jobs, lock, state and config all in tmp_path; notifications recorded;
    the step functions replaced by recorders unless a test swaps them."""
    monkeypatch.setattr(jobs, "JOBS_DIR", tmp_path / "jobs")
    monkeypatch.setattr(runner, "LOCK_FILE", tmp_path / "runner.lock")
    monkeypatch.setattr(pt, "CONFIG_DIR", tmp_path / "cfg")
    monkeypatch.setattr(pt, "STATE_FILE", tmp_path / "cfg" / "state.json")
    monkeypatch.setattr(pt, "CONFIG_FILE", tmp_path / "cfg" / "config.json")
    monkeypatch.setattr(visuals, "_interactive", True)
    sent = []
    monkeypatch.setattr(runner, "notify", lambda title, msg, **k: sent.append((title, msg)))
    calls = []

    def recorder(name):
        def fn(job, ctx, checkpoint=None):
            calls.append(name)
            return "done"
        return fn

    monkeypatch.setattr(runner, "STEP_FUNCS", {n: recorder(n) for n in runner.STEPS})
    return SimpleNamespace(tmp=tmp_path, sent=sent, calls=calls)


def make_job(**fields):
    fields.setdefault("recordings", ["plaud:aaa"])
    job = jobs.new_job(**fields)
    jobs.save_job(job)
    return job


def run_all(**kw):
    return runner.run_queue(pt, **kw)


class TestRebootMidJob:
    """A reboot used to throw away a long transcription; the work-dir cache only
    helps if the runner knows which steps already finished (B2)."""

    def test_resume_skips_done_steps_so_the_paid_transcription_is_not_repeated(self, env):
        job = make_job()
        jobs.set_step(job, "transcribe", "done")
        jobs.set_step(job, "commit", "skipped")
        jobs.save_job(job)

        run_all()

        assert env.calls == ["push", "deliver"]
        assert jobs.load_job(job["id"])["state"] == "done"

    def test_a_job_left_running_by_a_crash_is_resumed_not_orphaned(self, env):
        job = make_job(state="running")
        jobs.set_step(job, "transcribe", "running")
        jobs.save_job(job)

        run_all()

        assert env.calls[0] == "transcribe"
        assert jobs.load_job(job["id"])["state"] == "done"

    def test_steps_are_recorded_in_the_job_file_before_and_after_running(self, env, monkeypatch):
        job = make_job()
        seen = []

        def fn(j, ctx, checkpoint=None):
            seen.append([s["state"] for s in jobs.load_job(job["id"])["steps"]])
            return "done"

        monkeypatch.setattr(runner, "STEP_FUNCS", {**runner.STEP_FUNCS, "transcribe": fn})
        run_all()

        assert seen == [["running"]]
        assert [s["state"] for s in jobs.load_job(job["id"])["steps"]] == ["done"] * 4

    def test_oldest_queued_job_goes_first(self, env):
        a = make_job(created="2026-11-24T10:00:00+01:00")
        b = make_job(created="2026-11-24T09:00:00+01:00")
        assert run_all() == [b["id"], a["id"]]


class TestOneBadJobDoesNotStopTheQueue:
    """B6: a failure the night before must not leave the next day's meeting
    untranscribed, and Tomas needs to hear about it exactly once."""

    def test_failing_job_notifies_once_and_the_next_job_still_runs(self, env, monkeypatch):
        bad = make_job(created="2026-11-24T09:00:00+01:00")
        good = make_job(created="2026-11-24T10:00:00+01:00")

        def fn(job, ctx, checkpoint=None):
            if job["id"] == bad["id"]:
                raise RuntimeError("x" * 600)
            return "done"

        monkeypatch.setattr(runner, "STEP_FUNCS", {**runner.STEP_FUNCS, "transcribe": fn})
        run_all()

        failed = jobs.load_job(bad["id"])
        assert failed["state"] == "failed" and failed["failed_step"] == "transcribe"
        assert len(failed["error"]) == 500
        assert jobs.load_job(good["id"])["state"] == "done"
        assert [t for t, _ in env.sent].count("Plaud job failed") == 1

    def test_ctrl_c_is_not_turned_into_a_failed_job(self, env, monkeypatch):
        job = make_job()

        def fn(j, ctx, checkpoint=None):
            raise KeyboardInterrupt

        monkeypatch.setattr(runner, "STEP_FUNCS", {**runner.STEP_FUNCS, "transcribe": fn})
        with pytest.raises(KeyboardInterrupt):
            run_all()
        assert jobs.load_job(job["id"])["state"] == "running"  # resumable, not failed


class TestRetry:
    """`jobs retry` must not pay for the transcription again after a failed push."""

    def test_retry_keeps_done_steps_and_clears_the_error(self, env, monkeypatch, capsys):
        job = make_job()
        failing = {"on": True}

        def push(j, ctx, checkpoint=None):
            if failing["on"]:
                raise RuntimeError("remote rejected")
            return "done"

        monkeypatch.setattr(runner, "STEP_FUNCS", {**runner.STEP_FUNCS, "push": push})
        run_all()
        assert jobs.load_job(job["id"])["failed_step"] == "push"
        env.calls.clear()

        assert jobs_cli.main(["jobs", "retry", job["id"]], host=pt) == 0
        retried = jobs.load_job(job["id"])
        assert retried["state"] == "queued" and retried["error"] is None
        assert "failed_step" not in retried
        assert [s["name"] for s in retried["steps"]] == ["transcribe", "commit"]

        failing["on"] = False
        run_all()
        assert env.calls == ["deliver"]  # transcribe and commit were not redone
        assert jobs.load_job(job["id"])["state"] == "done"

    def test_retry_refuses_a_job_that_is_not_failed_or_held(self, env, capsys):
        job = make_job()
        assert jobs_cli.main(["jobs", "retry", job["id"]], host=pt) == 2
        assert "only failed/held" in capsys.readouterr().err


class TestCancelMidRun:
    """A job cancelled while it runs must not be resurrected by the runner's
    own next write."""

    def test_cancel_between_steps_stops_the_job_and_keeps_it_cancelled(self, env, monkeypatch):
        job = make_job()

        def transcribe(j, ctx, checkpoint=None):
            on_disk = jobs.load_job(job["id"])
            on_disk["state"] = "cancelled"
            jobs.save_job(on_disk)
            return "done"

        monkeypatch.setattr(runner, "STEP_FUNCS", {**runner.STEP_FUNCS, "transcribe": transcribe})
        run_all()

        assert env.calls == []  # commit/push/deliver never ran
        final = jobs.load_job(job["id"])
        assert final["state"] == "cancelled"
        assert final["steps"][0]["state"] == "done"  # progress kept
        assert env.sent == []


class TestOnlyOneRunner:
    """B9: launchd re-triggers on every job-file write, including the runner's own."""

    def test_second_runner_exits_zero_at_once_while_the_lock_is_held(self, env, capsys):
        make_job()
        held = open(env.tmp / "other.lock", "a+")
        fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            code = runner.main(["--lock-file", str(env.tmp / "other.lock")], host=pt)
        finally:
            held.close()
        assert code == 0
        assert "holds the lock" in capsys.readouterr().out
        assert env.calls == []

    def test_lock_is_free_again_after_the_runner_ends(self, env):
        make_job()
        lock = env.tmp / "x.lock"
        assert runner.main(["--lock-file", str(lock)], host=pt) == 0
        assert env.calls == list(runner.STEPS)
        assert runner.acquire_lock(lock) is not None


class TestFinishedJobNotification:
    """B8: with no hand-off nobody else will say the transcript is ready."""

    def test_mode_none_sends_one_quiet_notification(self, env):
        make_job()
        run_all()
        assert [t for t, _ in env.sent] == ["Plaud transcript ready"]

    def test_a_handed_over_job_stays_silent_because_the_node_takes_over(self, env):
        make_job(deliver={"node": "t:1", "mode": "now", "note": None, "ask": None},
                 project="P")
        run_all()
        assert env.sent == []


class TestTranscribeStep:
    """The step wires the job into the K3 pipeline hooks without prompting."""

    @pytest.fixture
    def pipe(self, env, monkeypatch):
        order = []
        recs = [{"id": "plaud:aaa", "source": "plaud", "filename": "A", "start_time": 1},
                {"id": "plaud:bbb", "source": "plaud", "filename": "B", "start_time": 2}]
        monkeypatch.setattr(runner, "STEP_FUNCS", {**runner.STEP_FUNCS, "transcribe": runner.step_transcribe})
        monkeypatch.setattr(pt, "get_token", lambda: "tok")
        monkeypatch.setattr(pt, "list_all_recordings", lambda t, c, d: order.append(("list", d)) or recs)
        monkeypatch.setattr(pt, "fetch_folders", lambda t, c: {})
        monkeypatch.setattr(pt, "get_projects", lambda c: {"P": "/tmp/p"})
        monkeypatch.setattr(pt, "prefetch_phone_files",
                            lambda sel, merge, cfg: order.append(("prefetch", visuals.is_interactive())))
        (env.tmp / "cfg").mkdir()
        return SimpleNamespace(order=order, recs=recs)

    def test_phone_prompt_is_switched_off_before_prefetch_can_reach_it(self, env, pipe, monkeypatch):
        make_job()
        monkeypatch.setattr(pt, "process_recording", lambda *a, **k: None)
        run_all()
        assert ("prefetch", False) in pipe.order

    def test_state_is_reloaded_per_recording_so_two_recordings_do_not_overwrite_each_other(
            self, env, pipe, monkeypatch):
        job = make_job(recordings=["plaud:aaa", "plaud:bbb"], project="P")

        def fake(rec, token, config, folders, projects, state, language, **kw):
            assert kw["forced_project"] == "P" and kw["interactive"] is False
            state.setdefault("transcribed", {})[rec["id"]] = {"status": "done"}
            pt.save_state(state)   # what process_recording does after each recording
            return {"file_id": rec["id"], "name": rec["filename"], "output_dir": f"/o/{rec['id']}"}

        monkeypatch.setattr(pt, "process_recording", fake)
        run_all()

        assert set(pt.load_state()["transcribed"]) == {"plaud:aaa", "plaud:bbb"}
        saved = jobs.load_job(job["id"])
        assert [r["file_id"] for r in saved["result"]["recordings"]] == ["plaud:aaa", "plaud:bbb"]
        assert saved["result"]["paths"] == ["/o/plaud:aaa", "/o/plaud:bbb"]

    def test_state_written_by_another_process_between_recordings_survives(self, env, pipe, monkeypatch):
        make_job(recordings=["plaud:aaa", "plaud:bbb"])

        def fake(rec, token, config, folders, projects, state, language, **kw):
            state.setdefault("transcribed", {})[rec["id"]] = {"status": "done"}
            pt.save_state(state)
            if rec["id"] == "plaud:aaa":   # an interactive session saves between our recordings
                other = pt.load_state()
                other["transcribed"]["other"] = {"status": "done"}
                pt.save_state(other)
            return {"file_id": rec["id"], "output_dir": "/o"}

        monkeypatch.setattr(pt, "process_recording", fake)
        run_all()
        assert "other" in pt.load_state()["transcribed"]

    def test_resume_after_a_failed_second_recording_does_not_redo_the_first(self, env, pipe, monkeypatch):
        job = make_job(recordings=["plaud:aaa", "plaud:bbb"])
        done = []

        def fake(rec, *a, **k):
            if rec["id"] == "plaud:bbb" and "boom" not in done:
                done.append("boom")
                raise RuntimeError("elevenlabs down")
            done.append(rec["id"])
            return {"file_id": rec["id"], "output_dir": "/o"}

        monkeypatch.setattr(pt, "process_recording", fake)
        run_all()
        assert jobs.load_job(job["id"])["state"] == "failed"
        jobs.retry_job(job["id"])
        run_all()
        assert done == ["plaud:aaa", "boom", "plaud:bbb"]

    def test_cuts_instructions_and_days_come_from_the_job_and_flag(self, env, pipe, monkeypatch):
        make_job(recordings=["plaud:aaa"], cuts={"plaud:aaa": ["1:00-2:00"]}, instructions="focus")
        seen = {}

        def fake(rec, *a, **k):
            seen.update(k)
            return None

        monkeypatch.setattr(pt, "process_recording", fake)
        run_all(days=7)
        assert seen["cuts"] == [pt.parse_cut("1:00-2:00")] and seen["extra_instructions"] == "focus"
        assert ("list", 7) in pipe.order

    def test_merge_job_goes_through_the_merged_pipeline(self, env, pipe, monkeypatch):
        job = make_job(recordings=["plaud:aaa", "plaud:bbb"], merge=True)
        monkeypatch.setattr(pt, "process_merged_recordings",
                            lambda recs, *a, **k: {"file_id": "m", "output_dir": "/o",
                                                   "n": len(recs)})
        run_all()
        assert jobs.load_job(job["id"])["result"]["recordings"][0]["n"] == 2

    def test_an_unknown_recording_fails_the_job_with_a_message_naming_it(self, env, pipe):
        job = make_job(recordings=["plaud:zzz"])
        run_all()
        saved = jobs.load_job(job["id"])
        assert saved["state"] == "failed" and "plaud:zzz" in saved["error"]

    def test_local_ids_fail_clearly_because_they_cannot_be_listed(self, env, pipe):
        job = make_job(recordings=["local:abc123"])
        run_all()
        assert "file <audio>" in jobs.load_job(job["id"])["error"]


class TestRunnerCommand:
    def test_help_runs_nothing_and_lists_every_flag_and_a_recipe(self, env, monkeypatch, capsys):
        monkeypatch.setattr(runner, "run_queue", lambda *a, **k: pytest.fail("ran"))
        monkeypatch.setattr(runner, "acquire_lock", lambda *a, **k: pytest.fail("locked"))
        with pytest.raises(SystemExit) as e:
            jobs_cli.main(["runner", "--help"], host=pt)
        out = capsys.readouterr().out
        assert e.value.code == 0
        for flag in ("--once", "--job", "--lock-file", "--days", "recipes:"):
            assert flag in out

    def test_jobs_retry_has_help(self, capsys):
        with pytest.raises(SystemExit):
            jobs_cli.main(["jobs", "retry", "--help"], host=pt)
        assert "requeue" in capsys.readouterr().out

    def test_once_runs_a_single_job_and_job_picks_a_specific_one(self, env):
        a = make_job(created="2026-11-24T09:00:00+01:00")
        b = make_job(created="2026-11-24T10:00:00+01:00")
        assert runner.main(["--once", "--lock-file", str(env.tmp / "l")], host=pt) == 0
        assert jobs.load_job(a["id"])["state"] == "done"
        assert jobs.load_job(b["id"])["state"] == "queued"
        assert runner.main(["--job", b["id"], "--lock-file", str(env.tmp / "l")], host=pt) == 0
        assert jobs.load_job(b["id"])["state"] == "done"

    def test_main_dispatches_runner_before_any_network_call(self, env, monkeypatch):
        monkeypatch.setattr(pt, "get_token", lambda: pytest.fail("network"))
        monkeypatch.setattr("sys.argv", ["plaud-transcribe", "runner", "--lock-file", str(env.tmp / "l")])
        with pytest.raises(SystemExit) as e:
            pt.main()
        assert e.value.code == 0
