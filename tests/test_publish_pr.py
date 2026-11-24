"""Part C (push = pr, C4/C6) and D8 for main: a repo that wants review gets a branch and a
PR, never a write to main; a refused push fails the job but loses nothing.

`gh` is never run for real: a stub script records its arguments and prints canned output.
Every repo is a throwaway under tmp_path with the dev's git config shut out."""

import json
import os
import stat

import pytest

import jobs
import publish
import runner
from test_publish_main import (ctx_for, make_job, make_transcript, origin_subjects,  # noqa: F401
                               publish_job, repo, sh, isolated_git)

URL = "https://github.com/acme/transcripts/pull/7"


@pytest.fixture
def gh(tmp_path, monkeypatch):
    """A stub gh. `calls` lists each invocation as (cwd, args); `existing_pr` makes
    `pr list` report an open PR; `fail` makes `pr create` exit 1 with that stderr."""
    log = tmp_path / "gh.log"
    state = tmp_path / "gh.state"
    state.mkdir()
    script = tmp_path / "bin" / "gh"
    script.parent.mkdir()
    script.write_text(f"""#!/bin/sh
python3 - "$@" <<'PY'
import json, os, sys
args = sys.argv[1:]
with open({str(log)!r}, "a") as f:
    f.write(json.dumps([os.getcwd(), args]) + "\\n")
st = {str(state)!r}
if args[:2] == ["pr", "list"]:
    p = os.path.join(st, "existing")
    print(open(p).read() if os.path.exists(p) else "[]")
elif args[:2] == ["pr", "create"]:
    p = os.path.join(st, "fail")
    if os.path.exists(p):
        sys.stderr.write(open(p).read()); sys.exit(1)
    print("Creating pull request for you")
    print({URL!r})
PY
""")
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    monkeypatch.setenv("GH_BIN", str(script))

    class Stub:
        def calls(self):
            if not log.exists():
                return []
            return [tuple(json.loads(l)) for l in log.read_text().splitlines()]

        def creates(self):
            return [a for _, a in self.calls() if a[:2] == ["pr", "create"]]

        def existing_pr(self, url=URL):
            (state / "existing").write_text(json.dumps([{"url": url}]))

        def fail(self, text):
            (state / "fail").write_text(text)

    return Stub()


def arg(args, flag):
    return args[args.index(flag) + 1]


class TestReviewedRepoGetsAPullRequestNotAPush:
    """C4: a project with push = pr is one where nobody pushes to main directly."""

    def test_main_is_untouched_the_branch_is_on_origin_and_the_url_is_recorded(self, repo, gh):
        job = make_job([make_transcript(repo.user, "Weekly")], push="pr")
        before = sh(repo.origin, "rev-parse", "main")

        publish_job(job, ctx_for(repo.user, push="pr"))

        branch = f"transcripts/{job['id']}"
        assert sh(repo.origin, "rev-parse", "main") == before
        assert sh(repo.origin, "rev-parse", branch) == job["result"]["commits"][0]
        assert job["result"]["pr"] == URL
        assert "checkout" not in job["result"]
        assert sh(repo.user, "rev-parse", "HEAD") == before   # no fast-forward in pr mode

    def test_gh_is_told_the_base_the_head_and_a_title_naming_the_transcript(self, repo, gh):
        job = make_job([make_transcript(repo.user, "Weekly")], push="pr")

        publish_job(job, ctx_for(repo.user, push="pr"))

        (cwd, args), = [c for c in gh.calls() if c[1][:2] == ["pr", "create"]]
        assert os.path.realpath(cwd) == os.path.realpath(repo.user)
        assert arg(args, "--base") == "main"
        assert arg(args, "--head") == f"transcripts/{job['id']}"
        assert arg(args, "--title") == "Add transcript: Weekly (2026-11-18)"
        body = arg(args, "--body")
        assert "Weekly" in body and "2026-11-18" in body and "20m" in body
        assert "transcripts/2026-11-18-Weekly/" in body
        assert body.splitlines()[-1] == f"Job {job['id']}"

    def test_several_transcripts_make_one_pr_titled_with_the_count(self, repo, gh):
        a = make_transcript(repo.user, "Alpha-sync")
        b = make_transcript(repo.user, "Beta-sync", date="2026-11-19")
        job = make_job([a, b], push="pr")

        publish_job(job, ctx_for(repo.user, push="pr"))

        args, = gh.creates()
        assert arg(args, "--title") == "Add 2 transcripts (2026-11-18)"
        assert "Alpha-sync" in arg(args, "--body") and "Beta-sync" in arg(args, "--body")
        assert len(gh.creates()) == 1
        assert sh(repo.origin, "rev-list", "--count", f"main..transcripts/{job['id']}") == "2"

    def test_the_url_is_the_last_https_line_of_what_gh_prints(self):
        assert publish._pr_url("Creating...\nhttps://x/pull/1\nhttps://x/pull/2\n") == "https://x/pull/2"
        assert publish._pr_url("nothing useful") is None

    def test_a_job_spanning_two_repos_records_one_url_per_root(self, repo, gh, tmp_path):
        from test_publish_main import clone, sh as _sh
        import subprocess
        origin2 = tmp_path / "origin2.git"
        subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(origin2)], check=True)
        seed = tmp_path / "seed2"
        seed.mkdir()
        _sh(seed, "init", "-q", "-b", "main")
        (seed / "r.md").write_text("x\n")
        _sh(seed, "add", ".")
        _sh(seed, "commit", "-q", "-m", "seed2")
        _sh(seed, "remote", "add", "origin", str(origin2))
        _sh(seed, "push", "-q", "origin", "main")
        user2 = clone(origin2, tmp_path / "user2")
        job = make_job([make_transcript(repo.user, "Weekly"), make_transcript(user2, "Other")],
                       push="pr")

        publish_job(job, ctx_for(repo.user, push="pr"))

        assert set(job["result"]["pr"]) == {str(repo.user.resolve()), str(user2.resolve())}
        assert len(gh.creates()) == 2


class TestRetryDoesNotDuplicateTheReview:
    """C6: a job that failed after the branch was pushed (gh hiccup) is retried; the retry
    must not commit, push or open a second time."""

    def test_an_existing_branch_and_open_pr_are_reused_without_new_commits_or_prs(self, repo, gh):
        job = make_job([make_transcript(repo.user, "Weekly")], push="pr")
        ctx = ctx_for(repo.user, push="pr")
        publish_job(job, ctx)
        sha = job["result"]["commits"][0]
        gh.existing_pr()
        job["result"].pop("pr")

        publish.step_commit(job, ctx)
        publish.step_push(job, ctx)

        assert len(gh.creates()) == 1
        assert job["result"]["pr"] == URL
        assert job["result"]["commits"] == [sha]
        branch = f"transcripts/{job['id']}"
        assert sh(repo.origin, "rev-list", "--count", f"main..{branch}") == "1"

    def test_a_failed_pr_create_keeps_the_pushed_branch_and_the_retry_only_opens_the_pr(self, repo, gh):
        job = make_job([make_transcript(repo.user, "Weekly")], push="pr")
        ctx = ctx_for(repo.user, push="pr")
        publish.step_commit(job, ctx)
        gh.fail("GraphQL: rate limited " + "x" * 600)

        with pytest.raises(publish.PublishError) as e:
            publish.step_push(job, ctx)

        assert str(e.value).startswith("GraphQL: rate limited") and len(str(e.value)) == 500
        branch = f"transcripts/{job['id']}"
        assert sh(repo.origin, "rev-parse", branch) == job["result"]["commits"][0]
        assert sh(repo.user, "branch", "--list", branch)
        (repo.tmp / "gh.state" / "fail").unlink()

        publish.step_push(job, ctx)

        assert job["result"]["pr"] == URL
        assert sh(repo.origin, "rev-parse", "main") == sh(repo.user, "rev-parse", "origin/main")
        assert origin_subjects(repo) == ["seed"]

    def test_the_branch_already_on_origin_with_the_same_tip_is_not_pushed_again(self, repo, gh, monkeypatch):
        job = make_job([make_transcript(repo.user, "Weekly")], push="pr")
        ctx = ctx_for(repo.user, push="pr")
        publish_job(job, ctx)
        pushes = []
        real = publish.git

        def spy(path, *args, **kw):
            if args[0] == "push":
                pushes.append(args)
            return real(path, *args, **kw)
        monkeypatch.setattr(publish, "git", spy)

        publish.step_push(job, ctx)

        assert pushes == []


class TestGhMissing:
    def test_a_machine_without_gh_fails_at_push_saying_how_to_fix_it_and_keeps_the_branch(
            self, repo, monkeypatch):
        monkeypatch.setenv("GH_BIN", str(repo.tmp / "no-such-gh"))
        job = make_job([make_transcript(repo.user, "Weekly")], push="pr")
        ctx = ctx_for(repo.user, push="pr")
        publish.step_commit(job, ctx)

        with pytest.raises(publish.PublishError, match="gh not found.*GH_BIN"):
            publish.step_push(job, ctx)

        assert sh(repo.user, "branch", "--list", f"transcripts/{job['id']}")
        assert origin_subjects(repo) == ["seed"]

    def test_the_binary_can_come_from_config_when_the_env_is_unset(self, monkeypatch):
        monkeypatch.delenv("GH_BIN", raising=False)
        assert publish._gh_bin({"runner": {"gh": "/opt/gh"}}) == "/opt/gh"
        assert publish._gh_bin({}) == "gh"


class TestProtectedMainFailsTheJobAndLosesNothing:
    """D8: when main refuses the push for a reason other than 'someone pushed first', the
    job fails at push; nobody opens a PR on Tomas's behalf. After the rejection is
    lifted, `jobs retry` lands the very same commits."""

    def _protect(self, repo):
        hook = repo.origin / "hooks" / "pre-receive"
        hook.write_text("#!/bin/sh\necho 'main is protected, open a PR' >&2\nexit 1\n")
        hook.chmod(0o755)
        return hook

    def test_rejection_fails_at_push_keeps_the_branch_and_the_retry_pushes_the_same_shas(
            self, repo, gh, monkeypatch):
        a = make_transcript(repo.user, "Alpha-sync")
        b = make_transcript(repo.user, "Beta-sync", date="2026-11-19")
        job = make_job([a, b])
        ctx = ctx_for(repo.user, push="main")
        ctx.notify = lambda *a, **k: None
        hook = self._protect(repo)
        before = sh(repo.origin, "rev-list", "--count", "main")
        saved = []
        monkeypatch.setattr(runner, "_fresh_state", lambda _id: "running")
        monkeypatch.setattr(runner, "_save", lambda j: saved.append(j["state"]))

        publish.step_commit(job, ctx)
        shas = list(job["result"]["commits"])
        with pytest.raises(publish.PublishError) as e:
            publish.step_push(job, ctx)

        assert "main is protected" in str(e.value)
        assert job["result"]["branch"] == f"transcripts/{job['id']}"
        assert job["result"]["commits"] == shas
        assert sh(repo.origin, "rev-list", "--count", "main") == before
        assert gh.calls() == []   # no automatic PR fallback
        assert sh(repo.user, "rev-list", "--reverse", f"main..transcripts/{job['id']}").split() == shas

        hook.unlink()
        publish.step_commit(job, ctx)   # what the runner does again on retry
        publish.step_push(job, ctx)

        assert job["result"]["commits"] == shas
        assert sh(repo.origin, "rev-list", "--reverse", f"{shas[0]}~1..main").split() == shas
        assert sh(repo.origin, "rev-list", "--count", "main") == str(int(before) + 2)
        assert gh.calls() == []

    def test_the_runner_marks_the_job_failed_at_push_with_the_hook_message(self, repo, gh, monkeypatch):
        """Through the real step machine: error text is git's own words, first 500 chars."""
        job = make_job([make_transcript(repo.user, "Weekly")])
        ctx = ctx_for(repo.user, push="main")
        notes = []
        ctx.notify = lambda *a, **k: notes.append(a)
        self._protect(repo)
        saved = []
        monkeypatch.setattr(runner, "_fresh_state", lambda _id: "running")
        monkeypatch.setattr(runner, "_save", lambda j: saved.append(j["state"]))
        monkeypatch.setattr(runner, "STEP_FUNCS", {**runner.STEP_FUNCS, "deliver": lambda *a: "skipped"})
        jobs.set_step(job, "transcribe", "done")   # the pipeline already ran; start at commit

        outcome = runner.run_job(job, ctx)

        assert outcome == "failed"
        assert job["failed_step"] == "push" and "main is protected" in job["error"]
        assert len(job["error"]) <= 500
        assert job["result"]["branch"] == f"transcripts/{job['id']}"
        assert notes
