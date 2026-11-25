"""Part D: after the push the runner hands the work to cc-tree, which decides between
messaging a live session, starting a fresh one or holding the work for Tomas.

`cc-tree` is never run for real: every test points CC_TREE_BIN at a stub script that
records its argv and stdin and prints canned output (the formats are the ones
cc_tree_deliver.py prints). Nothing resolves `cc-tree` from PATH."""

import json
import os
import stat
import sys
from types import SimpleNamespace

import pytest

import deliver
import jobs
import runner
from test_publish_main import (ctx_for, make_job, make_transcript, publish_job,  # noqa: F401
                               repo, sh, isolated_git)
from test_publish_pr import gh, URL  # noqa: F401

MESSAGED = "delivered 1/1\ntree:#1: messaged · session is healthy\n"
STARTED = "tree:#1: no session: a fresh one started with the work in its brief · nothing running\n"
RESEEDED = ("tree:#1: fresh session started from its checkpoint, with the work in its brief"
            " · heavy and refresh allowed\n")
HELD = ("tree:#1: held for Tomas (he's told) · Tomas is in the tab"
        " · cc-tree deliver --release 9xq1af\n")


@pytest.fixture(autouse=True)
def no_real_cc_tree(monkeypatch):
    monkeypatch.delenv("PLAUD_DELIVER_PLAN", raising=False)
    monkeypatch.delenv("PLAUD_DELIVER_TIMEOUT", raising=False)
    monkeypatch.setenv("CC_TREE_BIN", "/nonexistent/cc-tree")   # a test that forgets the stub fails loudly


@pytest.fixture
def cc_tree(tmp_path, monkeypatch):
    """A stub cc-tree. `calls` = [(argv, stdin)]; `say(stdout, rc, stderr)` sets the answer."""
    state = tmp_path / "cc-tree-state"
    state.mkdir()
    script = tmp_path / "ccbin" / "cc-tree"
    script.parent.mkdir()
    script.write_text(f"""#!{sys.executable}
import json, os, sys
st = {str(state)!r}
stdin = "" if "--plan" in sys.argv else sys.stdin.read()
with open(os.path.join(st, "calls.log"), "a") as f:
    f.write(json.dumps([sys.argv[1:], stdin]) + "\\n")
ans = json.load(open(os.path.join(st, "answer.json")))
sys.stdout.write(ans["stdout"]); sys.stderr.write(ans["stderr"])
sys.exit(ans["rc"])
""")
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    monkeypatch.setenv("CC_TREE_BIN", str(script))

    class Stub:
        path = script

        def say(self, stdout="", rc=0, stderr=""):
            (state / "answer.json").write_text(json.dumps({"stdout": stdout, "rc": rc, "stderr": stderr}))

        def calls(self):
            p = state / "calls.log"
            return [tuple(json.loads(l)) for l in p.read_text().splitlines()] if p.exists() else []

    stub = Stub()
    stub.say(MESSAGED)
    return stub


def deliver_job(results, mode="now", note=None, ask=None, **fields):
    job = make_job(results, deliver={"node": "tree/1", "mode": mode, "note": note, "ask": ask}, **fields)
    return job


def pushed(repo, names=("Weekly",), mode="now", note=None, push="main", **fields):
    """A job whose transcripts are really committed and pushed to the tmp origin."""
    job = deliver_job([make_transcript(repo.user, n) for n in names], mode=mode, note=note,
                      push=push, **fields)
    ctx = ctx_for(repo.user, push=push)
    publish_job(job, ctx)
    return job, ctx


def paths_in_text(text):
    line = [l for l in text.splitlines() if l.startswith("Files: ")]
    return [p for l in line for p in l[len("Files: "):].split(", ")]


class TestTextListsEverythingTheJobProduced:
    """D1: one job can carry several transcripts; the session must be told about all of
    them, with paths it can open at origin/main."""

    def test_every_transcript_is_listed_and_its_paths_exist_on_origin_main(self, repo):
        job, _ = pushed(repo, ("Alpha-sync", "Beta-sync"))

        text = deliver.build_work_text(job)

        assert "New transcript: Alpha-sync (2026-11-18, 20m)" in text
        assert "New transcript: Beta-sync (2026-11-18, 20m)" in text
        paths = paths_in_text(text)
        assert paths and len(paths) >= 2 * 3
        for p in paths:
            sh(repo.origin, "cat-file", "-e", f"main:{p}")   # raises when the path is not on origin/main
        assert not any(os.path.isabs(p) for p in paths)

    def test_the_commit_line_names_the_sha_that_is_on_origin_main(self, repo):
        job, _ = pushed(repo, ("Alpha-sync", "Beta-sync"))

        text = deliver.build_work_text(job)

        commits = [l for l in text.splitlines() if l.startswith("Commit: ")]
        assert len(commits) == 2
        for line, sha in zip(commits, job["result"]["commits"]):
            assert line.split()[1] == sha[:10]
            sh(repo.origin, "merge-base", "--is-ancestor", sha, "main")

    def test_the_header_names_the_repo_so_a_session_knows_where_to_look(self, repo):
        job, _ = pushed(repo)
        assert "— user\n" in deliver.build_work_text(job)   # the clone's directory name

    def test_a_pr_job_gives_the_pr_url_and_paths_that_exist_on_the_pr_branch(self, repo, gh):
        job, _ = pushed(repo, ("Weekly",), push="pr")

        text = deliver.build_work_text(job)

        assert f"PR: {URL}" in text
        paths = paths_in_text(text)
        assert paths
        for p in paths:
            sh(repo.origin, "cat-file", "-e", f"transcripts/{job['id']}:{p}")
            assert sh(repo.origin, "cat-file", "-e", f"main:{p}", check=False) == ""
            assert sh(repo.origin, "rev-parse", "--verify", "-q", f"main:{p}", check=False) == ""

    def test_a_main_job_has_no_pr_mention(self, repo):
        job, _ = pushed(repo)
        assert "PR:" not in deliver.build_work_text(job)


class TestWhatTheSessionIsAskedToDo:
    """D7 and mode later: a note typed for this job beats the project's standing ask,
    which beats a built-in default; `later` asks for nothing."""

    def test_the_note_wins_over_the_project_ask(self, repo):
        job, _ = pushed(repo, note="only the action items", ask="process it")
        assert deliver.build_work_text(job).rstrip().endswith("Process it now: only the action items")

    def test_the_project_ask_wins_over_the_default(self, repo):
        job = deliver_job([make_transcript(repo.user, "Weekly")], ask="file the decisions")
        assert deliver.build_work_text(job).rstrip().endswith("Process it now: file the decisions")

    def test_with_neither_a_built_in_default_still_asks_for_processing(self, repo):
        job = deliver_job([make_transcript(repo.user, "Weekly")])
        text = deliver.build_work_text(job)
        assert "Process it now: " + deliver.DEFAULT_ASK in text

    def test_later_mode_asks_for_nothing_even_when_a_note_exists(self, repo):
        job = deliver_job([make_transcript(repo.user, "Weekly")], mode="later", note="ignored",
                          ask="ignored too")
        text = deliver.build_work_text(job)
        assert text.rstrip().endswith("No action needed yet — it is here when you need it.")
        assert "Process it now" not in text and "ignored" not in text


class TestOutcomeIsRecordedFromWhatCcTreePrints:
    """The runner must know whether a session was messaged, a fresh one started, or Tomas
    has to pick; the format is cc-tree's, so an unfamiliar one is kept, not rejected."""

    @pytest.mark.parametrize("out,outcome,hid", [
        (MESSAGED, "message", None),
        ("tree:#1: messaged · healthy\n", "message", None),
        (STARTED, "fresh", None),
        (RESEEDED, "fresh", None),
        (HELD, "held", "9xq1af"),
        ("all fine, something new\n", "unknown", None),
    ])
    def test_parse(self, out, outcome, hid):
        assert deliver.parse_outcome(out) == (outcome, hid)

    def test_unrecognised_success_is_still_done_with_the_raw_text_kept(self, repo, cc_tree):
        cc_tree.say("brand new wording " + "x" * 700)
        job, ctx = pushed(repo)

        assert deliver.step_deliver(job, ctx) == "done"

        d = job["result"]["delivered"]
        assert d["outcome"] == "unknown" and d["raw"].startswith("brand new wording")
        assert len(d["raw"]) == 500

    def test_the_work_text_arrives_on_stdin_for_the_configured_node(self, repo, cc_tree):
        job, ctx = pushed(repo)

        deliver.step_deliver(job, ctx)

        (argv, stdin), = cc_tree.calls()
        assert argv == ["deliver", "tree/1", "-"]
        assert stdin == deliver.build_work_text(job)
        assert job["result"]["delivered"]["outcome"] == "message"

    def test_the_binary_may_come_from_config_when_the_env_is_unset(self, repo, cc_tree, monkeypatch):
        monkeypatch.delenv("CC_TREE_BIN")
        job, _ = pushed(repo)
        ctx = ctx_for(repo.user)
        cfg = ctx.host.load_config()
        cfg["deliver"] = {"bin": str(cc_tree.path)}

        assert deliver.step_deliver(job, ctx) == "done"
        assert len(cc_tree.calls()) == 1


class TestNothingToDeliver:
    @pytest.mark.parametrize("mode,node", [("none", "tree/1"), ("now", None)])
    def test_mode_none_or_no_node_never_calls_cc_tree(self, repo, cc_tree, mode, node):
        job = deliver_job([make_transcript(repo.user, "Weekly")], mode=mode)
        job["deliver"]["node"] = node
        assert deliver.step_deliver(job, ctx_for(repo.user)) == "skipped"
        assert cc_tree.calls() == []


class TestHeldWorkWaitsForTomas:
    """D2: cc-tree keeps the work and tells Tomas; the job has to say so too, once."""

    @pytest.fixture
    def env(self, tmp_path, monkeypatch, repo):
        monkeypatch.setattr(jobs, "JOBS_DIR", tmp_path / "jobs")
        monkeypatch.setattr(runner, "preflight", lambda *a, **k: None)
        sent = []
        monkeypatch.setattr(runner, "notify", lambda title, msg, **k: sent.append((title, msg)))
        return SimpleNamespace(sent=sent)

    def run_deliver_only(self, job, ctx_host):
        job["steps"] = [{"name": n, "state": "done"} for n in ("transcribe", "commit", "push")]
        jobs.save_job(job)
        c = SimpleNamespace(host=ctx_host, days=30, deliver_plan=False, notify=runner.notify)
        return runner.run_job(job, c)

    def test_held_work_makes_the_job_held_with_its_id_and_one_notification(self, repo, cc_tree, env):
        cc_tree.say(HELD)
        job, ctx = pushed(repo)

        final = self.run_deliver_only(job, ctx.host)

        saved = jobs.load_job(job["id"])
        assert final == "held" and saved["state"] == "held"
        assert saved["result"]["delivered"]["id"] == "9xq1af"
        assert saved["result"]["delivered"]["outcome"] == "held"
        assert "9xq1af" in saved["error"]
        assert env.sent == [("Plaud job held", f"held for you: 9xq1af ({job['id']})")]

    def test_retry_from_held_delivers_again_and_nothing_else(self, repo, cc_tree, env):
        cc_tree.say(HELD)
        job, ctx = pushed(repo)
        self.run_deliver_only(job, ctx.host)
        commits = sh(repo.origin, "rev-list", "--count", "main")
        cc_tree.say(MESSAGED)   # Tomas released it; the node is healthy now
        jobs.retry_job(job["id"])

        again = jobs.load_job(job["id"])
        assert [s["name"] for s in again["steps"]] == ["transcribe", "commit", "push"]
        c = SimpleNamespace(host=ctx.host, days=30, deliver_plan=False, notify=runner.notify)
        assert runner.run_job(again, c) == "done"

        assert len(cc_tree.calls()) == 2
        assert sh(repo.origin, "rev-list", "--count", "main") == commits
        assert jobs.load_job(job["id"])["result"]["delivered"]["outcome"] == "message"
        assert len(env.sent) == 1   # only the hold was announced; a good delivery stays silent

    def test_a_delivered_job_sends_no_notification_because_the_node_takes_over(self, repo, cc_tree, env):
        job, ctx = pushed(repo)
        assert self.run_deliver_only(job, ctx.host) == "done"
        assert env.sent == []


class TestCcTreeFailureDoesNotUndoThePush:
    """D3: the transcript is already on origin; a missing or failing cc-tree must not
    cost a second commit or a second transcription."""

    @pytest.fixture
    def env(self, tmp_path, monkeypatch):
        monkeypatch.setattr(jobs, "JOBS_DIR", tmp_path / "jobs")
        monkeypatch.setattr(runner, "preflight", lambda *a, **k: None)
        monkeypatch.setattr(runner, "notify", lambda *a, **k: None)

    def run(self, job, host):
        c = SimpleNamespace(host=host, days=30, deliver_plan=False, notify=lambda *a, **k: None)
        return runner.run_job(job, c)

    def prepared(self, repo):
        job, ctx = pushed(repo)
        job["steps"] = [{"name": n, "state": "done"} for n in ("transcribe", "commit", "push")]
        jobs.save_job(job)
        return job, ctx

    def test_a_non_zero_exit_fails_only_deliver_and_retry_delivers_without_a_new_commit(
            self, repo, cc_tree, env):
        cc_tree.say("", rc=9, stderr="deliver tree:#1: not delivered · node is closed")
        job, ctx = self.prepared(repo)
        before = sh(repo.origin, "rev-parse", "main")
        count = sh(repo.origin, "rev-list", "--count", "main")

        assert self.run(job, ctx.host) == "failed"

        saved = jobs.load_job(job["id"])
        assert saved["failed_step"] == "deliver" and "exited 9" in saved["error"]
        assert "node is closed" in saved["error"]
        assert [s["state"] for s in saved["steps"] if s["name"] != "deliver"] == ["done"] * 3
        assert sh(repo.origin, "rev-parse", "main") == before

        cc_tree.say(MESSAGED)
        jobs.retry_job(job["id"])
        assert self.run(jobs.load_job(job["id"]), ctx.host) == "done"

        assert sh(repo.origin, "rev-list", "--count", "main") == count
        assert sh(repo.origin, "rev-parse", "main") == before
        assert jobs.load_job(job["id"])["result"]["delivered"]["outcome"] == "message"

    def test_a_missing_binary_names_what_to_set_and_keeps_the_transcript_pushed(
            self, repo, cc_tree, env, monkeypatch):
        monkeypatch.setenv("CC_TREE_BIN", "/nonexistent/cc-tree")
        job, ctx = self.prepared(repo)
        before = sh(repo.origin, "rev-parse", "main")

        assert self.run(job, ctx.host) == "failed"

        err = jobs.load_job(job["id"])["error"]
        assert "CC_TREE_BIN" in err and "not found" in err
        assert sh(repo.origin, "rev-parse", "main") == before

    def test_a_hung_cc_tree_fails_the_step_instead_of_blocking_the_queue(self, repo, tmp_path, env,
                                                                         monkeypatch):
        slow = tmp_path / "slow"
        slow.write_text("#!/bin/sh\nsleep 5\n")
        slow.chmod(0o755)
        monkeypatch.setenv("CC_TREE_BIN", str(slow))
        monkeypatch.setenv("PLAUD_DELIVER_TIMEOUT", "1")
        job, ctx = self.prepared(repo)

        assert self.run(job, ctx.host) == "failed"
        assert "timed out after 1s" in jobs.load_job(job["id"])["error"]


class TestDryRunNeverMessagesASession:
    """D4: tests and cautious first runs ask cc-tree what it WOULD do."""

    def test_plan_mode_calls_dash_dash_plan_and_never_the_plain_deliver(self, repo, cc_tree, monkeypatch):
        cc_tree.say("message\tsession is healthy\n")
        monkeypatch.setenv("PLAUD_DELIVER_PLAN", "1")
        job, ctx = pushed(repo)

        assert deliver.step_deliver(job, ctx) == "done"

        (argv, stdin), = cc_tree.calls()
        assert argv == ["deliver", "tree/1", "--plan"] and stdin == ""
        assert job["result"]["delivered"] == {"outcome": "plan", "id": None,
                                              "raw": "message\tsession is healthy"}

    def test_the_runner_flag_does_the_same_as_the_env_var(self, repo, cc_tree):
        job, ctx = pushed(repo)
        ctx.deliver_plan = True

        deliver.step_deliver(job, ctx)

        assert [a for a, _ in cc_tree.calls()] == [["deliver", "tree/1", "--plan"]]

    def test_the_flag_is_parsed_and_reaches_run_queue(self, monkeypatch, tmp_path):
        seen = {}
        monkeypatch.setattr(runner, "run_queue", lambda *a, **k: seen.update(k))
        monkeypatch.setattr(runner, "load_runner_env", lambda *a, **k: "env: stubbed")
        host = SimpleNamespace(load_config=lambda validate=True: {})
        runner.main(["--deliver-plan", "--lock-file", str(tmp_path / "l")], host=host)
        assert seen["deliver_plan"] is True


class TestHandOffOnAnotherMachine:
    """cc-tree on the Mac starts a fresh session on the Mac. On 2026-11-25 a node whose
    session lives on the HP got a new Mac session instead; a job can now name the host the
    hand-off runs on, and cc-tree runs there over ssh (a stub ssh here, nothing leaves)."""

    @pytest.fixture
    def ssh(self, tmp_path, monkeypatch):
        log = tmp_path / "ssh.log"
        script = tmp_path / "ssh-stub"
        script.write_text(f"""#!{sys.executable}
import json, sys
stdin = "" if "--plan" in sys.argv[-1] else sys.stdin.read()
open({str(log)!r}, "a").write(json.dumps([sys.argv[1:], stdin]) + "\\n")
sys.stdout.write({STARTED!r})
""")
        script.chmod(0o755)
        monkeypatch.setenv("PLAUD_DELIVER_SSH", str(script))
        return lambda: [tuple(json.loads(l)) for l in log.read_text().splitlines()] if log.exists() else []

    def test_cc_tree_runs_on_the_host_in_its_login_shell_with_the_work_on_stdin(self, repo, ssh, cc_tree):
        job, ctx = pushed(repo)
        job["deliver"]["host"] = "hp-ubuntu"

        assert deliver.step_deliver(job, ctx) == "done"

        (argv, stdin), = ssh()
        assert argv[:4] == ["-o", "BatchMode=yes", "-o", "ConnectTimeout=15"]
        assert argv[4] == "hp-ubuntu"
        assert argv[5] == "zsh -lc 'cc-tree deliver tree/1 -'"   # ~/.dotfiles/bin is on PATH only there
        assert "New transcript: Weekly" in stdin
        assert cc_tree.calls() == []                              # nothing ran on this machine
        assert job["result"]["delivered"]["host"] == "hp-ubuntu"
        assert job["result"]["delivered"]["outcome"] == "fresh"

    def test_the_session_there_is_told_to_pull_because_the_files_are_on_origin_only(self, repo):
        job, _ = pushed(repo)
        job["deliver"]["host"] = "hp-ubuntu"
        assert "pull" in deliver.build_work_text(job)
        job["deliver"]["host"] = None
        assert "pull" not in deliver.build_work_text(job)

    def test_nothing_pushed_means_no_remote_hand_off_the_session_could_not_read_it(self, repo, ssh):
        job, ctx = pushed(repo)
        job["push"] = "none"           # e.g. a classifier-routed job whose project pushes nothing
        job["deliver"]["host"] = "hp-ubuntu"

        with pytest.raises(deliver.DeliverError, match="needs the transcript on origin"):
            deliver.step_deliver(job, ctx)
        assert ssh() == []

    def test_this_machines_own_name_or_local_runs_cc_tree_here(self, monkeypatch):
        monkeypatch.setattr(deliver.socket, "gethostname", lambda: "Toms-MBP.local")
        for h in (None, "", "local", "localhost", "toms-mbp", "Toms-MBP.local"):
            assert not deliver.is_remote(h), h
        assert deliver.is_remote("hp-ubuntu")

    def test_an_unreachable_host_fails_the_step_with_a_retry_hint(self, repo, tmp_path, monkeypatch):
        bad = tmp_path / "ssh-down"
        bad.write_text("#!/bin/sh\necho 'ssh: connect to host hp-ubuntu: timed out' >&2\nexit 255\n")
        bad.chmod(0o755)
        monkeypatch.setenv("PLAUD_DELIVER_SSH", str(bad))
        job, ctx = pushed(repo)
        job["deliver"]["host"] = "hp-ubuntu"

        with pytest.raises(deliver.DeliverError, match="ssh to hp-ubuntu failed.*jobs retry"):
            deliver.step_deliver(job, ctx)

    def test_remote_shell_and_binary_are_config_knobs(self):
        argv = deliver.remote_argv("box", ["deliver", "a:b", "-"],
                                   {"deliver": {"remote_shell": "bash -lc", "remote_bin": "/opt/cc-tree"}})
        assert argv[-1] == "bash -lc '/opt/cc-tree deliver a:b -'"
