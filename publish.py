"""Publishing a job's transcripts to git without touching the user's checkout.

The runner's `commit` and `push` steps live here. Transcripts are new files, so
they never have to share a working tree with whatever the user (or a Claude
session) is doing in the repo. The recipe, for `push = main`:

    git -C <root> worktree prune
    git -C <root> fetch origin main
    git -C <root> worktree add --no-track -b transcripts/<job-id> <tmp> origin/main
      copy each transcript dir into <tmp>, `git add -f`, one commit per transcript
    git -C <root> worktree remove --force <tmp>          (the branch stays)
    git -C <root> fetch origin main
    git -C <root> push origin transcripts/<job-id>:main
      non-fast-forward: fetch, rebase the branch in a fresh worktree, push again
    git -C <root> merge --ff-only origin/main            (only if safe, see below)

Never used in the user's checkout: stash, checkout, reset, clean, add. The only
write there is the final `merge --ff-only`, and only when the checkout is on
`main`, behind, and no local change touches a file the merge would change.

Knobs
  PLAUD_PUSH_RETRIES   rebase-and-retry rounds after a non-fast-forward (default 3);
                       config {"runner": {"push_retries": N}} sets the same.
  push mode            per job (`enqueue --push`), else the project's `push` in config.
  push none            nothing is pushed; today's `auto_commit` behaviour applies.
  push pr              the branch is pushed under the same name and `gh pr create` opens
                       a PR against main (run in the repo root); main is never written.
  GH_BIN               the gh binary for pr mode (default `gh`); config
                       {"runner": {"gh": "/path/to/gh"}} sets the same.
  (runner --help repeats these knobs.)

A rejected push that is not a non-fast-forward (protected branch, no network)
fails the push step with the first 500 characters of git's stderr and keeps the
branch `transcripts/<job-id>` (D8: there is no fallback to a PR).
"""

import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

DEFAULT_RETRIES = 3
BASE = "main"


class PublishError(RuntimeError):
    """A git step failed; the message is what the job's `error` will show."""


# ---------------------------------------------------------------- git plumbing

def _env():
    return {**os.environ, "GIT_TERMINAL_PROMPT": "0"}


def git(path, *args, check=True, timeout=120):
    """`git -C <path> ...`; returns CompletedProcess (text). With check, a failure
    raises PublishError carrying the first 500 chars of stderr."""
    try:
        proc = subprocess.run(["git", "-C", str(path), *args], capture_output=True,
                              text=True, timeout=timeout, env=_env())
    except (OSError, subprocess.SubprocessError) as e:
        raise PublishError(f"git {args[0]} failed: {type(e).__name__}: {e}"[:500])
    if check and proc.returncode != 0:
        msg = (proc.stderr or proc.stdout or "").strip() or f"exit {proc.returncode}"
        raise PublishError(f"git {args[0]}: {msg}"[:500])
    return proc


def repo_root(directory):
    """Top of the git repo containing `directory` (symlinks resolved), or None."""
    try:
        proc = git(directory, "rev-parse", "--show-toplevel", check=False, timeout=10)
    except PublishError:
        return None
    top = proc.stdout.strip()
    return Path(top).resolve() if proc.returncode == 0 and top else None


def _branch_exists(root, branch):
    return git(root, "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}",
               check=False).returncode == 0


def _add_worktree(root, branch, create):
    """A temporary worktree outside the repo, on `branch` (new off origin/main when
    `create`, else the existing one)."""
    tmp = tempfile.mkdtemp(prefix="plaud-wt-")
    try:
        if create:
            git(root, "worktree", "add", "--no-track", "-b", branch, tmp, f"origin/{BASE}")
        else:
            git(root, "worktree", "add", tmp, branch)
    except BaseException:
        shutil.rmtree(tmp, ignore_errors=True)
        git(root, "worktree", "prune", check=False)
        raise
    return tmp


def _remove_worktree(root, tmp):
    git(root, "worktree", "remove", "--force", tmp, check=False)
    shutil.rmtree(tmp, ignore_errors=True)
    git(root, "worktree", "prune", check=False)


# ---------------------------------------------------------------- resolution

def resolve(job, results, specs):
    """Fill the job's null push/node/deliver_ask from its project (a classifier-
    routed job learns its project from the first result). The unrouted `default`
    output has no project config: push none, delivery skipped. Returns the push
    mode. A job that mixes projects uses its first result's project."""
    project = job.get("project") or next((r.get("project") for r in results if r.get("project")), None)
    spec = specs.get(project) if project and project != "default" else None
    if job.get("push") is None:
        job["push"] = spec["push"] if spec else "none"
    deliver = job.setdefault("deliver", {})
    if spec:
        deliver["node"] = deliver.get("node") or spec["node"]
        deliver["ask"] = deliver.get("ask") or spec["deliver_ask"]
        if deliver.get("host") is None:
            deliver["host"] = spec.get("host")
    elif not job.get("project"):
        deliver["mode"] = "none"
    job.setdefault("result", {})["project"] = project
    return job["push"]


def _results(job):
    return [r for r in (job.get("result") or {}).get("recordings") or [] if r.get("output_dir")]


def _duration(ms):
    secs = int(ms or 0) // 1000
    h, m = secs // 3600, (secs % 3600) // 60
    return f"{h}h {m}m" if h else f"{m}m"


def commit_message(res):
    """(subject, body) for one transcript."""
    subject = f"Add transcript: {res.get('title') or res.get('name') or 'untitled'} ({res.get('date')})"
    sources = ", ".join(
        f"{s.get('id')} ({s.get('source')})" if isinstance(s, dict) else str(s)
        for s in res.get("sources") or []) or "unknown"
    lines = [f"Source: {sources}", f"Duration: {_duration(res.get('duration'))}"]
    if res.get("summary_first_line"):
        lines += ["", res["summary_first_line"]]
    return subject, "\n".join(lines)


def _retries(config):
    raw = os.environ.get("PLAUD_PUSH_RETRIES") or ((config or {}).get("runner") or {}).get("push_retries")
    try:
        return max(0, int(raw))
    except (TypeError, ValueError):
        return DEFAULT_RETRIES


# ---------------------------------------------------------------- seams (tests)

def _after_commit(root, rel):
    """Called after each transcript's commit; tests raise here to simulate a crash."""


def _before_push(root, branch):
    """Called right before every push attempt; tests push from another clone here."""


# ---------------------------------------------------------------- commit step

def _groups(results):
    """{repo root: [(result, repo-relative path)]}; fails clearly on a dir outside git
    or a repo with no origin."""
    groups = {}
    for res in results:
        out = Path(res["output_dir"])
        root = repo_root(out)
        if root is None:
            raise PublishError(f"{out} is not inside a git repository; cannot commit it")
        if git(root, "remote", "get-url", "origin", check=False).returncode != 0:
            raise PublishError(f"repository {root} has no 'origin' remote; "
                               "add one (git remote add origin <url>) or set push to none")
        groups.setdefault(root, []).append((res, out.resolve().relative_to(root)))
    return groups


def _commits_for(root, branch, rel):
    """Commits on the branch (beyond origin/main) that touch `rel`, oldest first."""
    proc = git(root, "log", "--reverse", "--format=%H", f"origin/{BASE}..{branch}", "--", str(rel),
               check=False)
    return proc.stdout.split() if proc.returncode == 0 else []


def commit_group(root, branch, items):
    """Commit each transcript on `branch` in a temporary worktree. A transcript
    whose path already has a commit on the branch (a retry) is not committed again.
    Returns the shas, one per transcript that has one."""
    git(root, "fetch", "origin", BASE)
    exists = _branch_exists(root, branch)
    shas = {}
    todo = []
    for res, rel in items:
        found = _commits_for(root, branch, rel) if exists else []
        if found:
            shas[str(rel)] = found[-1]
        else:
            todo.append((res, rel))
    if todo:
        tmp = _add_worktree(root, branch, create=not exists)
        try:
            for res, rel in todo:
                shutil.copytree(root / rel, Path(tmp) / rel, dirs_exist_ok=True)
                git(tmp, "add", "-f", "--", str(rel))
                if git(tmp, "diff", "--cached", "--quiet", check=False).returncode == 0:
                    continue   # identical content is already on origin/main
                subject, body = commit_message(res)
                git(tmp, "commit", "-q", "-m", subject, "-m", body)
                shas[str(rel)] = git(tmp, "rev-parse", "HEAD").stdout.strip()
                _after_commit(root, rel)
        finally:
            _remove_worktree(root, tmp)
    return [shas[str(rel)] for _, rel in items if str(rel) in shas]


def step_commit(job, ctx, checkpoint=None):
    host = ctx.host
    config = host.load_config(validate=False)
    results = _results(job)
    if not results:
        return "skipped"
    mode = resolve(job, results, host.project_specs(config))
    if checkpoint:
        checkpoint()   # the resolved push/node/ask survive a crash from here on
    if mode == "none":
        host.git_commit_working(results, config)   # today's opt-in auto_commit
        return "skipped"
    out = job["result"]
    branch = f"transcripts/{job['id']}"
    repos, commits = {}, []
    for root, items in _groups(results).items():
        git(root, "worktree", "prune", check=False)
        repos[str(root)] = commit_group(root, branch, items)
        commits += repos[str(root)]
    out.update(branch=branch, commits=commits, repos=repos)
    return "done"


# ---------------------------------------------------------------- push step

def _is_non_ff(stderr):
    s = stderr or ""
    return "[rejected]" in s and ("non-fast-forward" in s or "fetch first" in s)


def _rebase(root, branch):
    tmp = _add_worktree(root, branch, create=False)
    try:
        proc = git(tmp, "rebase", f"origin/{BASE}", check=False)
        if proc.returncode != 0:
            git(tmp, "rebase", "--abort", check=False)
            raise PublishError(("rebase onto origin/main failed (a transcript path changed "
                                "upstream?): " + (proc.stderr or proc.stdout).strip())[:500])
    finally:
        _remove_worktree(root, tmp)


def push_main(root, branch, retries):
    """Push the branch to origin main; absorbs a concurrent push by rebasing."""
    for attempt in range(retries + 1):
        git(root, "fetch", "origin", BASE)
        if git(root, "merge-base", "--is-ancestor", branch, f"origin/{BASE}",
               check=False).returncode == 0:
            return   # already on origin/main (an earlier attempt got through)
        _before_push(root, branch)
        proc = git(root, "push", "origin", f"{branch}:{BASE}", check=False)
        if proc.returncode == 0:
            return
        if not _is_non_ff(proc.stderr):
            raise PublishError((proc.stderr or proc.stdout).strip()[:500]
                               or f"git push exited {proc.returncode}")
        if attempt == retries:
            raise PublishError(f"origin/{BASE} kept moving: still rejected after "
                               f"{retries} rebase retries; branch {branch} kept")
        git(root, "fetch", "origin", BASE)
        _rebase(root, branch)


def _status_paths(root):
    out = git(root, "status", "--porcelain", "-z", "--untracked-files=all").stdout
    paths, parts = [], out.split("\0")
    i = 0
    while i < len(parts):
        entry = parts[i]
        i += 1
        if len(entry) > 3:
            paths.append(entry[3:])
            if entry[0] in "RC":
                i += 1   # rename entries carry the old path as a separate field
    return paths


def fast_forward_checkout(root, ours=()):
    """Bring the user's checkout up to origin/main when that is harmless. Returns
    a note for result.checkout. Never switches branches. `ours`: repo-relative
    paths the job wrote; an untracked file there that is byte-identical to origin/main's
    copy is the transcript this job just pushed, so it may make way for the merge."""
    root = Path(root)
    head = git(root, "symbolic-ref", "--short", "-q", "HEAD", check=False).stdout.strip()
    if head != BASE:
        return f"left behind: checkout is on {head or 'a detached HEAD'}, not {BASE}"
    if git(root, "merge-base", "--is-ancestor", "HEAD", f"origin/{BASE}", check=False).returncode != 0:
        return "left behind: local commits are not on origin/main"
    changed = git(root, "diff", "--name-only", "-z", "HEAD", f"origin/{BASE}").stdout.split("\0")
    changed = [c for c in changed if c]
    if not changed:
        return "up to date"
    local = _status_paths(root)
    mine = [str(p) for p in ours]
    removable = {}
    for path in local:
        if not any(path == m or path.startswith(m.rstrip("/") + "/") for m in mine):
            continue
        tracked = git(root, "ls-files", "--error-unmatch", "--", path, check=False).returncode == 0
        f = root / path
        if not tracked and f.is_file():
            shown = subprocess.run(["git", "-C", str(root), "show", f"origin/{BASE}:{path}"],
                                   capture_output=True, env=_env())   # bytes: blobs may be images
            if shown.returncode == 0 and f.read_bytes() == shown.stdout:
                removable[path] = shown.stdout
    blocking = [c for c in changed if c not in removable and any(
        c == p or p.endswith("/") and c.startswith(p) for p in local)]
    if blocking:
        return f"left behind: local changes in {blocking[0]}"
    for path in removable:
        (root / path).unlink()
    proc = git(root, "merge", "--ff-only", f"origin/{BASE}", check=False)
    if proc.returncode != 0:
        for path, blob in removable.items():   # put our own files back
            (root / path).parent.mkdir(parents=True, exist_ok=True)
            (root / path).write_bytes(blob)
        why = (proc.stderr or proc.stdout).strip().splitlines()
        return "left behind: " + (why[0] if why else "ff-only merge refused")[:200]
    return f"fast-forwarded to {git(root, 'rev-parse', '--short', 'HEAD').stdout.strip()}"


def _gh_bin(config):
    return os.environ.get("GH_BIN") or ((config or {}).get("runner") or {}).get("gh") or "gh"


def _gh(gh, root, *args):
    """Run gh in the repo root (it finds the repo from there); stdout, or PublishError
    carrying the first 500 chars of stderr."""
    try:
        proc = subprocess.run([gh, *args], cwd=str(root), capture_output=True, text=True,
                              timeout=120, env=_env())
    except FileNotFoundError:
        raise PublishError(f"gh not found ({gh!r}): install the GitHub CLI or set GH_BIN / "
                           f"config runner.gh; branch kept, `jobs retry` resumes at push")
    except (OSError, subprocess.SubprocessError) as e:
        raise PublishError(f"gh {args[0]} {args[1]} failed: {type(e).__name__}: {e}"[:500])
    if proc.returncode != 0:
        raise PublishError(((proc.stderr or proc.stdout).strip() or f"gh exited {proc.returncode}")[:500])
    return proc.stdout


def pr_text(job, items):
    """(title, body) for a PR holding `items` = [(result, repo-relative path)]."""
    if len(items) == 1:
        title = commit_message(items[0][0])[0]
    else:
        title = f"Add {len(items)} transcripts ({items[0][0].get('date')})"
    blocks = []
    for res, rel in items:
        blocks.append("\n".join([
            f"- {res.get('title') or res.get('name') or 'untitled'}",
            f"  Date: {res.get('date')}",
            f"  Duration: {_duration(res.get('duration'))}",
            f"  Path: {rel}/"]))
    return title, "\n\n".join(blocks) + f"\n\nJob {job['id']}"


def _pr_url(text):
    urls = [l.strip() for l in (text or "").splitlines() if l.strip().startswith("https://")]
    return urls[-1] if urls else None


def push_pr(job, root, branch, items, gh):
    """Push the kept branch under its own name, then open (or find) its PR. Both halves
    are idempotent, so a retry after a half-done attempt repeats neither."""
    tip = git(root, "rev-parse", f"refs/heads/{branch}").stdout.strip()
    remote = git(root, "ls-remote", "origin", f"refs/heads/{branch}").stdout.split()
    if not remote or remote[0] != tip:
        git(root, "push", "origin", f"{branch}:refs/heads/{branch}")
    listed = _gh(gh, root, "pr", "list", "--head", branch, "--json", "url")
    try:
        existing = [e["url"] for e in json.loads(listed or "[]") if e.get("url")]
    except (ValueError, TypeError, KeyError):
        existing = []
    if existing:
        return existing[0]
    title, body = pr_text(job, items)
    out = _gh(gh, root, "pr", "create", "--base", BASE, "--head", branch,
              "--title", title, "--body", body)
    url = _pr_url(out)
    if not url:
        raise PublishError(f"gh pr create printed no URL: {out.strip()}"[:500])
    return url


def _step_push_pr(job, ctx):
    out = job["result"]
    branch = out.get("branch")
    repos = out.get("repos") or {}
    if not branch or not repos:
        raise PublishError("nothing to push: the commit step recorded no branch")
    gh = _gh_bin(ctx.host.load_config(validate=False))
    urls = {}
    for root in repos:
        items = [(r, Path(r["output_dir"]).resolve().relative_to(root)) for r in _results(job)
                 if Path(r["output_dir"]).resolve().is_relative_to(root)]
        urls[root] = push_pr(job, root, branch, items, gh)
    out["pr"] = next(iter(urls.values())) if len(urls) == 1 else urls
    return "done"


def step_push(job, ctx, checkpoint=None):
    mode = job.get("push") or "none"
    if mode == "none":
        return "skipped"
    if mode == "pr":
        return _step_push_pr(job, ctx)
    out = job["result"]
    branch = out.get("branch")
    repos = out.get("repos") or {}
    if not branch or not repos:
        raise PublishError("nothing to push: the commit step recorded no branch")
    config = ctx.host.load_config(validate=False)
    retries = _retries(config)
    notes = {}
    for root in repos:
        push_main(root, branch, retries)
        git(root, "fetch", "origin", BASE)
        ours = [Path(r["output_dir"]).resolve().relative_to(root) for r in _results(job)
                if Path(r["output_dir"]).resolve().is_relative_to(root)]
        notes[root] = fast_forward_checkout(root, ours)
        repos[root] = list(reversed(git(root, "rev-list", f"--max-count={len(repos[root])}",
                                        branch).stdout.split()))   # shas after any rebase
    out["commits"] = [s for shas in repos.values() for s in shas]
    out["checkout"] = next(iter(notes.values())) if len(notes) == 1 else notes
    return "done"
