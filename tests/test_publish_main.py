"""Part C (push = main): transcripts reach origin/main through a temporary worktree,
so whatever the user is doing in their checkout is never touched (D4: no stash).

Every test builds its own bare origin and clones under tmp_path, with the dev's git
config shut out; nothing here reaches a real remote or this repo's git."""

import os
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

import jobs
import plaud_transcribe as pt
import publish


def sh(path, *args, check=True):
    proc = subprocess.run(["git", "-C", str(path), *args], capture_output=True, text=True,
                          env={**os.environ, "GIT_TERMINAL_PROMPT": "0"})
    if check and proc.returncode != 0:
        raise AssertionError(f"git {args}: {proc.stderr}")
    return proc.stdout.strip()


@pytest.fixture(autouse=True)
def isolated_git(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", "/dev/null")
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.delenv("PLAUD_PUSH_RETRIES", raising=False)
    for who in ("AUTHOR", "COMMITTER"):
        monkeypatch.setenv(f"GIT_{who}_NAME", "Test")
        monkeypatch.setenv(f"GIT_{who}_EMAIL", "test@example.invalid")


def clone(origin, dest):
    subprocess.run(["git", "clone", "-q", str(origin), str(dest)], check=True,
                   capture_output=True, env={**os.environ, "GIT_TERMINAL_PROMPT": "0"})
    sh(dest, "checkout", "-q", "-B", "main", "origin/main", check=False)
    return dest


@pytest.fixture
def repo(tmp_path):
    """A bare origin with one commit on main, the user's clone, and a second
    clone standing for another machine / session."""
    origin = tmp_path / "origin.git"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(origin)], check=True)
    seed = tmp_path / "seed"
    seed.mkdir()
    sh(seed, "init", "-q", "-b", "main")
    (seed / "README.md").write_text("hello\n")
    (seed / "notes.md").write_text("notes v1\n")
    (seed / "staged.md").write_text("staged v1\n")
    sh(seed, "add", ".")
    sh(seed, "commit", "-q", "-m", "seed")
    sh(seed, "remote", "add", "origin", str(origin))
    sh(seed, "push", "-q", "origin", "main")
    return SimpleNamespace(origin=origin, user=clone(origin, tmp_path / "user"),
                           other=clone(origin, tmp_path / "other"), tmp=tmp_path)


def classification(name, project="Alpha"):
    return {"name": name, "type": "meeting", "project": project, "tags": ["x"], "language": "en",
            "summary": f"# {name}\n\nThey agreed to ship Friday.\n", "clean_transcript": "hello"}


def make_transcript(root, name, date="2026-11-18", with_visual=False):
    """A real output directory, written by the pipeline's own write_outputs."""
    out = root / "transcripts" / f"{date}-{name}"
    meta = {"date": date, "file_id": f"plaud:{name}", "duration": 20 * 60 * 1000,
            "source": "plaud", "filename": name}
    pt.write_outputs(classification(name), meta, "raw text", out)
    if with_visual:
        (out / "visuals").mkdir()
        (out / "visuals" / "slide1.png").write_bytes(b"\x89PNG\x00\x01")
    return {"file_id": f"plaud:{name}", "name": name, "title": name, "project": "Alpha",
            "date": date, "duration": 20 * 60 * 1000,
            "sources": [{"id": f"plaud:{name}", "source": "plaud", "filename": name}],
            "summary_first_line": "They agreed to ship Friday.",
            "files": sorted(p.name for p in out.iterdir() if p.is_file()), "output_dir": str(out)}


def make_job(results, **fields):
    job = jobs.new_job(recordings=["plaud:aaa"], **fields)
    job["result"]["recordings"] = results
    job["result"]["paths"] = [r["output_dir"] for r in results]
    return job


def ctx_for(repo_dir, push="main", auto_commit=False, committed=None):
    config = {"projects": {"Alpha": {"dir": str(repo_dir / "transcripts"), "push": push,
                                     "node": "tree/1", "deliver_ask": "process it"}},
              "auto_commit": auto_commit}
    host = SimpleNamespace(
        load_config=lambda validate=True: config, project_specs=pt.project_specs,
        git_commit_working=lambda results, cfg: (committed if committed is not None else []).append(
            [r["output_dir"] for r in results]))
    return SimpleNamespace(host=host)


def publish_job(job, ctx):
    assert publish.step_commit(job, ctx) == "done"
    assert publish.step_push(job, ctx) == "done"


def origin_subjects(repo):
    return sh(repo.origin, "log", "--format=%s", "main").splitlines()


class TestUserKeepsWorkingWhileTranscriptsAreFiled:
    """Tomas keeps Claude sessions running in the transcript repo. A stash would pull
    their files out from under them, so the job must leave every local change alone (C1)."""

    def test_modified_staged_and_untracked_files_survive_byte_for_byte(self, repo):
        user = repo.user
        res = make_transcript(user, "Weekly")
        (user / "notes.md").write_text("notes v1\nmy unsaved thought\n")
        (user / "staged.md").write_text("staged v2\n")
        sh(user, "add", "staged.md")
        (user / "scratch.txt").write_bytes(b"\x00untracked\xff")
        # origin moves on, so the checkout is behind and a fast-forward is attempted
        (repo.other / "other.md").write_text("from elsewhere\n")
        sh(repo.other, "add", "other.md")
        sh(repo.other, "commit", "-q", "-m", "other work")
        sh(repo.other, "push", "-q", "origin", "main")
        before = {p: (user / p).read_bytes() for p in ("notes.md", "staged.md", "scratch.txt")}
        before_status = [l for l in sh(user, "status", "--porcelain", "--untracked-files=all").splitlines()
                         if "transcripts/" not in l]
        job = make_job([res])

        publish_job(job, ctx_for(user))

        assert {p: (user / p).read_bytes() for p in before} == before
        after_status = sh(user, "status", "--porcelain", "--untracked-files=all").splitlines()
        assert after_status == before_status
        assert sh(user, "stash", "list") == ""
        assert job["result"]["checkout"].startswith("fast-forwarded")
        assert (user / "other.md").exists()
        assert "Add transcript: Weekly (2026-11-18)" in origin_subjects(repo)

    def test_a_local_edit_to_a_file_the_merge_would_change_keeps_the_checkout_behind(self, repo):
        user = repo.user
        res = make_transcript(user, "Weekly")
        (repo.other / "notes.md").write_text("notes v2 from elsewhere\n")
        sh(repo.other, "commit", "-q", "-am", "edit notes")
        sh(repo.other, "push", "-q", "origin", "main")
        (user / "notes.md").write_text("my local edit\n")
        job = make_job([res])

        publish_job(job, ctx_for(user))

        assert (user / "notes.md").read_text() == "my local edit\n"
        assert job["result"]["checkout"].startswith("left behind: local changes in notes.md")

    def test_the_transcript_files_written_into_the_checkout_make_way_for_the_merge(self, repo):
        """The pipeline writes the transcript into the checkout as untracked files; git
        refuses to fast-forward over them unless the identical copies are moved aside."""
        user = repo.user
        res = make_transcript(user, "Weekly", with_visual=True)
        job = make_job([res])

        publish_job(job, ctx_for(user))

        assert job["result"]["checkout"].startswith("fast-forwarded")
        assert sh(user, "status", "--porcelain") == ""
        assert (Path(res["output_dir"]) / "summary.md").exists()


class TestOneCommitPerTranscript:
    """Part C: a transcript is one reviewable unit in history, and a merged recording is one."""

    def test_two_transcripts_give_two_commits_each_with_only_its_own_files(self, repo):
        a = make_transcript(repo.user, "Alpha-sync", with_visual=True)
        b = make_transcript(repo.user, "Beta-sync", date="2026-11-19")
        job = make_job([a, b])

        publish_job(job, ctx_for(repo.user))

        subjects = origin_subjects(repo)
        assert subjects[:2] == ["Add transcript: Beta-sync (2026-11-19)",
                                "Add transcript: Alpha-sync (2026-11-18)"]
        a_files = sh(repo.origin, "show", "--name-only", "--format=", "main~1").splitlines()
        b_files = sh(repo.origin, "show", "--name-only", "--format=", "main").splitlines()
        assert a_files and all("2026-11-18-Alpha-sync" in f for f in a_files)
        assert b_files and all("2026-11-19-Beta-sync" in f for f in b_files)
        assert "transcripts/2026-11-18-Alpha-sync/visuals/slide1.png" in a_files
        assert len(job["result"]["commits"]) == 2
        assert job["result"]["branch"] == f"transcripts/{job['id']}"

    def test_the_message_body_carries_source_duration_and_the_summary_line(self, repo):
        res = make_transcript(repo.user, "Weekly")
        job = make_job([res])

        publish_job(job, ctx_for(repo.user))

        body = sh(repo.origin, "log", "-1", "--format=%b", "main")
        assert "plaud:Weekly (plaud)" in body and "20m" in body
        assert "They agreed to ship Friday." in body

    def test_the_worktree_is_gone_and_the_branch_is_kept(self, repo):
        job = make_job([make_transcript(repo.user, "Weekly")])

        publish_job(job, ctx_for(repo.user))

        assert sh(repo.user, "worktree", "list").count("\n") == 0
        assert sh(repo.user, "branch", "--list", f"transcripts/{job['id']}")


class TestAConcurrentPushIsAbsorbed:
    """Someone else pushes to main between our fetch and our push (C3); their commit
    and ours must both survive."""

    def test_rebase_and_retry_keeps_both_commits(self, repo, monkeypatch):
        job = make_job([make_transcript(repo.user, "Weekly")])
        fired = []

        def race(root, branch):
            if fired:
                return
            fired.append(1)
            (repo.other / "race.md").write_text("won the race\n")
            sh(repo.other, "add", "race.md")
            sh(repo.other, "commit", "-q", "-m", "someone else")
            sh(repo.other, "push", "-q", "origin", "main")

        monkeypatch.setattr(publish, "_before_push", race)

        publish_job(job, ctx_for(repo.user))

        subjects = origin_subjects(repo)
        assert "someone else" in subjects and "Add transcript: Weekly (2026-11-18)" in subjects
        assert job["result"]["commits"] == [sh(repo.origin, "rev-parse", "main")]

    def test_a_main_that_never_stops_moving_fails_after_the_retry_limit(self, repo, monkeypatch):
        monkeypatch.setenv("PLAUD_PUSH_RETRIES", "2")
        job = make_job([make_transcript(repo.user, "Weekly")])
        n = []

        def race(root, branch):
            n.append(1)
            (repo.other / f"race{len(n)}.md").write_text("x\n")
            sh(repo.other, "add", ".")
            sh(repo.other, "commit", "-q", "-m", f"race {len(n)}")
            sh(repo.other, "push", "-q", "origin", "main")

        monkeypatch.setattr(publish, "_before_push", race)
        ctx = ctx_for(repo.user)
        publish.step_commit(job, ctx)

        with pytest.raises(publish.PublishError, match="kept moving"):
            publish.step_push(job, ctx)
        assert len(n) == 3   # first try + 2 retries
        assert sh(repo.user, "branch", "--list", f"transcripts/{job['id']}")


class TestOtherBranchIsNeverDisturbed:
    """Tomas is often on a feature branch in the transcript repo (C5)."""

    def test_head_and_files_are_untouched_and_nothing_is_fast_forwarded(self, repo):
        user = repo.user
        sh(user, "checkout", "-q", "-b", "feature")
        (user / "wip.md").write_text("wip\n")
        res = make_transcript(user, "Weekly")
        head = sh(user, "rev-parse", "HEAD")
        job = make_job([res])

        publish_job(job, ctx_for(user))

        assert sh(user, "symbolic-ref", "--short", "HEAD") == "feature"
        assert sh(user, "rev-parse", "HEAD") == head
        assert (user / "wip.md").read_text() == "wip\n"
        assert job["result"]["checkout"] == "left behind: checkout is on feature, not main"
        assert "Add transcript: Weekly (2026-11-18)" in origin_subjects(repo)


class TestHrisetFrontMatterReachesTheRepo:
    """hriste's doc gate fails CI on a transcript without class/type/event_date (C7)."""

    def test_committed_blob_has_the_governance_keys(self, repo):
        res = make_transcript(repo.user, "Weekly")
        job = make_job([res])

        publish_job(job, ctx_for(repo.user))

        blob = sh(repo.origin, "show", "main:transcripts/2026-11-18-Weekly/summary.md")
        head = blob.split("---")[1]
        assert "class: immutable" in head and "type: transcript" in head
        assert "event_date: 2026-11-18" in head

    def test_a_gitignored_transcript_dir_is_still_committed(self, repo):
        """plaud-sync's own docs/transcripts is gitignored; the worktree add must be forced."""
        user = repo.user
        (user / ".gitignore").write_text("transcripts/\n")
        sh(user, "add", ".gitignore")
        sh(user, "commit", "-q", "-m", "ignore")
        sh(user, "push", "-q", "origin", "main")
        res = make_transcript(user, "Weekly")
        job = make_job([res])

        publish_job(job, ctx_for(user))

        assert "transcripts/2026-11-18-Weekly/summary.md" in sh(
            repo.origin, "ls-tree", "-r", "--name-only", "main")


class TestRetryAfterACrash:
    """A reboot mid-commit leaves a half-built branch; the retry must finish it, not
    duplicate the first transcript."""

    def test_a_crash_after_the_first_commit_does_not_double_commit(self, repo, monkeypatch):
        a = make_transcript(repo.user, "Alpha-sync")
        b = make_transcript(repo.user, "Beta-sync", date="2026-11-19")
        job = make_job([a, b])
        ctx = ctx_for(repo.user)
        monkeypatch.setattr(publish, "_after_commit",
                            lambda root, rel: (_ for _ in ()).throw(RuntimeError("power cut")))
        with pytest.raises(RuntimeError):
            publish.step_commit(job, ctx)
        monkeypatch.undo()

        publish_job(job, ctx)

        assert [s for s in origin_subjects(repo) if s.startswith("Add transcript")] == [
            "Add transcript: Beta-sync (2026-11-19)", "Add transcript: Alpha-sync (2026-11-18)"]

    def test_running_the_commit_step_twice_adds_nothing(self, repo):
        job = make_job([make_transcript(repo.user, "Weekly")])
        ctx = ctx_for(repo.user)
        publish.step_commit(job, ctx)
        first = list(job["result"]["commits"])

        publish.step_commit(job, ctx)

        assert job["result"]["commits"] == first
        assert sh(repo.user, "rev-list", "--count", f"origin/main..transcripts/{job['id']}") == "1"


class TestRejectedPushIsAFailureNotAFallback:
    """D8: a protected main fails the job; the commit stays on its branch for a later retry."""

    def test_a_hook_rejection_fails_the_push_with_git_s_words_and_keeps_the_branch(self, repo):
        hook = repo.origin / "hooks" / "pre-receive"
        hook.write_text("#!/bin/sh\necho 'main is protected, open a PR' >&2\nexit 1\n")
        hook.chmod(0o755)
        job = make_job([make_transcript(repo.user, "Weekly")])
        ctx = ctx_for(repo.user)
        publish.step_commit(job, ctx)

        with pytest.raises(publish.PublishError) as e:
            publish.step_push(job, ctx)

        assert "main is protected" in str(e.value) and len(str(e.value)) <= 500
        assert sh(repo.user, "branch", "--list", f"transcripts/{job['id']}")
        assert "Add transcript: Weekly (2026-11-18)" not in origin_subjects(repo)

    def test_retry_after_the_hook_is_lifted_pushes_the_same_commit(self, repo):
        hook = repo.origin / "hooks" / "pre-receive"
        hook.write_text("#!/bin/sh\nexit 1\n")
        hook.chmod(0o755)
        job = make_job([make_transcript(repo.user, "Weekly")])
        ctx = ctx_for(repo.user)
        publish.step_commit(job, ctx)
        sha = job["result"]["commits"][0]
        with pytest.raises(publish.PublishError):
            publish.step_push(job, ctx)
        hook.unlink()

        publish.step_push(job, ctx)

        assert sh(repo.origin, "rev-parse", "main") == sha

    def test_a_push_step_rerun_after_success_is_a_no_op(self, repo):
        job = make_job([make_transcript(repo.user, "Weekly")])
        ctx = ctx_for(repo.user)
        publish_job(job, ctx)
        sh(repo.other, "pull", "-q", "--ff-only", "origin", "main")
        (repo.other / "later.md").write_text("later\n")
        sh(repo.other, "add", ".")
        sh(repo.other, "commit", "-q", "-m", "later")
        sh(repo.other, "push", "-q", "origin", "main")

        publish.step_push(job, ctx)

        assert origin_subjects(repo)[0] == "later"


class TestWhatTheCommitStepRefuses:
    def test_a_repo_without_origin_fails_with_a_message_naming_it(self, tmp_path):
        solo = tmp_path / "solo"
        solo.mkdir()
        sh(solo, "init", "-q", "-b", "main")
        job = make_job([make_transcript(solo, "Weekly")])

        with pytest.raises(publish.PublishError, match="no 'origin' remote"):
            publish.step_commit(job, ctx_for(solo))

    def test_pr_mode_is_not_built_yet_and_says_so(self, repo):
        job = make_job([make_transcript(repo.user, "Weekly")], push="pr")
        ctx = ctx_for(repo.user, push="pr")
        publish.step_commit(job, ctx)

        with pytest.raises(publish.PublishError, match="pr lands in K7"):
            publish.step_push(job, ctx)
        assert origin_subjects(repo) == ["seed"]


class TestPushModeComesFromTheProject:
    """The job's push/node/ask are null when the classifier routes; they resolve from the
    final project at the commit step."""

    def test_a_routed_job_takes_push_node_and_ask_from_its_project(self, repo):
        job = make_job([make_transcript(repo.user, "Weekly")])

        publish_job(job, ctx_for(repo.user))

        assert job["push"] == "main"
        assert job["deliver"]["node"] == "tree/1" and job["deliver"]["ask"] == "process it"
        assert job["result"]["project"] == "Alpha"

    def test_push_none_skips_both_steps_and_keeps_the_auto_commit_behaviour(self, repo):
        res = make_transcript(repo.user, "Weekly")
        job = make_job([res])
        committed = []
        ctx = ctx_for(repo.user, push="none", auto_commit=True, committed=committed)

        assert publish.step_commit(job, ctx) == "skipped"
        assert publish.step_push(job, ctx) == "skipped"

        assert committed == [[res["output_dir"]]]
        assert origin_subjects(repo) == ["seed"]

    def test_the_default_output_is_never_pushed_and_its_delivery_is_dropped(self, repo):
        res = make_transcript(repo.user, "Weekly")
        res["project"] = "default"
        job = make_job([res], deliver={"node": None, "mode": "now", "note": None, "ask": None})

        assert publish.step_commit(job, ctx_for(repo.user)) == "skipped"

        assert job["push"] == "none" and job["deliver"]["mode"] == "none"

    def test_an_explicit_job_push_overrides_the_project(self, repo):
        job = make_job([make_transcript(repo.user, "Weekly")], push="none")

        assert publish.step_commit(job, ctx_for(repo.user, push="main")) == "skipped"
