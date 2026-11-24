"""Stage 1 end to end: enqueue -> runner -> transcript pushed -> work handed to cc-tree.

Only the outside world is stubbed: Plaud's API, the transcription call (process_recording,
which still writes its files with the pipeline's real write_outputs), notifications and
cc-tree itself. The repo, origin, worktree commit, push and job files are all real."""

import json
from types import SimpleNamespace

import pytest

import jobs
import jobs_cli
import plaud_transcribe as pt
import runner
import visuals
from test_deliver import MESSAGED, HELD, cc_tree, no_real_cc_tree, paths_in_text  # noqa: F401
from test_publish_main import classification, repo, sh, isolated_git  # noqa: F401


@pytest.fixture
def world(tmp_path, monkeypatch, repo, cc_tree):
    monkeypatch.setattr(jobs, "JOBS_DIR", tmp_path / "jobs")
    monkeypatch.setattr(runner, "LOCK_FILE", tmp_path / "runner.lock")
    monkeypatch.setattr(pt, "CONFIG_DIR", tmp_path / "cfg")
    monkeypatch.setattr(pt, "STATE_FILE", tmp_path / "cfg" / "state.json")
    monkeypatch.setattr(pt, "CONFIG_FILE", tmp_path / "cfg" / "config.json")
    monkeypatch.setattr(visuals, "_interactive", True)
    monkeypatch.setattr(runner, "load_runner_env", lambda *a, **k: "env: stubbed")
    monkeypatch.setattr(runner, "preflight", lambda *a, **k: None)
    sent = []
    monkeypatch.setattr(runner, "notify", lambda title, msg, **k: sent.append((title, msg)))
    (tmp_path / "cfg").mkdir()
    (tmp_path / "cfg" / "config.json").write_text(json.dumps({
        "projects": {"Alpha": {"dir": str(repo.user / "transcripts"), "push": "main",
                               "node": "tree/1", "deliver_ask": "file the decisions"}}}))

    recs = [{"id": "plaud:aaa", "source": "plaud", "filename": "Weekly", "start_time": 1},
            {"id": "plaud:bbb", "source": "plaud", "filename": "Retro", "start_time": 2}]
    monkeypatch.setattr(pt, "get_token", lambda: "tok")
    monkeypatch.setattr(pt, "list_all_recordings", lambda t, c, d: recs)
    monkeypatch.setattr(pt, "fetch_folders", lambda t, c: {})
    monkeypatch.setattr(pt, "prefetch_phone_files", lambda *a: None)

    def process(rec, token, config, folders, projects, state, language, **kw):
        assert kw["forced_project"] == "Alpha" and kw["interactive"] is False
        name = rec["filename"]
        out = repo.user / "transcripts" / f"2026-11-18-{name}"
        meta = {"date": "2026-11-18", "file_id": rec["id"], "duration": 20 * 60 * 1000,
                "source": "plaud", "filename": name}
        pt.write_outputs(classification(name), meta, "raw", out)
        return {"file_id": rec["id"], "name": name, "title": name, "project": "Alpha",
                "date": "2026-11-18", "duration": 20 * 60 * 1000,
                "sources": [{"id": rec["id"], "source": "plaud", "filename": name}],
                "files": sorted(p.name for p in out.iterdir() if p.is_file()),
                "output_dir": str(out)}

    monkeypatch.setattr(pt, "process_recording", process)
    return SimpleNamespace(sent=sent, repo=repo, cc=cc_tree)


def enqueue(*extra):
    assert jobs_cli.main(["enqueue", "--recording", "plaud:aaa", "--recording", "plaud:bbb",
                          "--project", "Alpha", *extra], host=pt) == 0
    return jobs.list_jobs()[-1]["id"]


class TestRecordingToSessionWithoutAHuman:
    """The point of stage 1: say "transcribe these" once and the node's session is told
    where the finished transcripts are, with nobody at the keyboard."""

    def test_enqueued_recordings_are_pushed_and_the_session_is_told_about_both(self, world):
        jid = enqueue("--note", "decisions only")

        assert runner.run_queue(pt) == [jid]

        job = jobs.load_job(jid)
        assert job["state"] == "done"
        assert [s["state"] for s in job["steps"]] == ["done"] * 4
        subjects = sh(world.repo.origin, "log", "--format=%s", "main").splitlines()
        assert "Add transcript: Weekly (2026-11-18)" in subjects
        assert "Add transcript: Retro (2026-11-18)" in subjects
        (argv, stdin), = world.cc.calls()
        assert argv == ["deliver", "tree/1", "-"]
        assert "New transcript: Weekly" in stdin and "New transcript: Retro" in stdin
        assert stdin.rstrip().endswith("Process it now: decisions only")
        for p in paths_in_text(stdin):
            sh(world.repo.origin, "cat-file", "-e", f"main:{p}")
        assert job["result"]["delivered"]["outcome"] == "message"
        assert world.sent == []   # the node's session takes over; no desktop notification

    def test_the_project_ask_is_used_when_the_job_carries_no_note(self, world):
        enqueue()
        runner.run_queue(pt)
        assert world.cc.calls()[0][1].rstrip().endswith("Process it now: file the decisions")

    def test_a_held_hand_off_leaves_a_held_job_and_retry_redelivers_only(self, world):
        world.cc.say(HELD)
        jid = enqueue()
        runner.run_queue(pt)
        assert jobs.load_job(jid)["state"] == "held"
        assert world.sent == [("Plaud job held", f"held for you: 9xq1af ({jid})")]
        pushed_head = sh(world.repo.origin, "rev-parse", "main")

        world.cc.say(MESSAGED)
        jobs_cli.main(["jobs", "retry", jid], host=pt)
        runner.run_queue(pt)

        assert jobs.load_job(jid)["state"] == "done"
        assert sh(world.repo.origin, "rev-parse", "main") == pushed_head
        assert len(world.cc.calls()) == 2

    def test_cc_tree_down_fails_the_job_at_deliver_with_the_transcripts_already_pushed(self, world):
        world.cc.say("", rc=1, stderr="no such node")
        jid = enqueue()
        runner.run_queue(pt)

        job = jobs.load_job(jid)
        assert job["state"] == "failed" and job["failed_step"] == "deliver"
        assert sh(world.repo.origin, "log", "--format=%s", "main").splitlines()[0].startswith("Add transcript")

    def test_deliver_plan_dry_runs_the_whole_queue_without_messaging(self, world):
        jid = enqueue()
        runner.run_queue(pt, deliver_plan=True)

        assert [a for a, _ in world.cc.calls()] == [["deliver", "tree/1", "--plan"]]
        assert jobs.load_job(jid)["result"]["delivered"]["outcome"] == "plan"
