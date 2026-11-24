"""The job runner: takes queued jobs one at a time, runs their steps, exits.

    plaud-transcribe runner            work through every queued job, then exit
    plaud-transcribe runner --once     one job only
    plaud-transcribe runner --job ID   only that job (queued, running or after `jobs retry`)

Steps run in order transcribe -> commit -> push -> deliver. A step runs only if it
is not already done/skipped, and its state is written to the job file before and
after, so a crash or reboot resumes at the first unfinished step (B2). The
transcribe step leans on the pipeline's work-dir cache, so a resume does not pay
for the transcription twice. Steps are pluggable (STEP_FUNCS): commit/push are
publish.py (worktree commit + push, never touching the user's checkout);
deliver is still a placeholder that marks itself `skipped`.

One runner at a time: a flock on the lock file (the kernel drops it when the
process dies, so a crash never leaves a stale lock). A second runner exits 0 at
once (B9); the launchd WatchPaths re-trigger relies on that.
"""

import argparse
import fcntl
import os
import shlex
import shutil
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import jobs
import publish
from autocapture import notify  # module-level, dependency-free: reused, not copied
import visuals

STEPS = ("transcribe", "commit", "push", "deliver")
DONE_STEPS = ("done", "skipped")
DEFAULT_DAYS = 30
REPO_DIR = Path(__file__).resolve().parent
REQUIRED_ENV = ("CLAUDE_CODE_OAUTH_TOKEN", "ELEVENLABS_API_KEY", "PLAUD_BEARER_TOKEN")
NON_PLAUD_PREFIXES = ("wispr:", "recorder:", "local:")
LOCK_FILE = Path(os.environ.get("PLAUD_RUNNER_LOCK") or "~/.plaud-sync/runner.lock").expanduser()


class Cancelled(Exception):
    """The job file was cancelled while the runner was working on it."""


# ---------------------------------------------------------------- lock

def acquire_lock(path=None):
    """Take the runner lock; returns the open file (keep it referenced) or None
    when another runner holds it. Never blocks."""
    path = Path(path or LOCK_FILE)
    path.parent.mkdir(parents=True, exist_ok=True)
    f = open(path, "a+")
    try:
        fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        f.close()
        return None
    return f


# ---------------------------------------------------------------- credentials (B4)

class PreflightError(RuntimeError):
    """A credential the job needs is missing; the message names the variable."""


def env_command(config=None):
    """The command that prints the credentials as NUL-separated KEY=VALUE.
    Env PLAUD_RUNNER_ENV_CMD, else config runner.env_cmd, else direnv in the repo
    (the .envrc decrypts the sops files; it is not a plain file to parse)."""
    cmd = os.environ.get("PLAUD_RUNNER_ENV_CMD") or ((config or {}).get("runner") or {}).get("env_cmd")
    if cmd:
        return shlex.split(cmd) if isinstance(cmd, str) else [str(c) for c in cmd]
    return ["direnv", "exec", str(REPO_DIR), "env", "-0"]


def load_runner_env(config=None, environ=None, run=subprocess.run):
    """Make the credentials available without an interactive shell.

    Variables already in the environment win. Only if one of REQUIRED_ENV is
    missing is the env command run (launchd starts us with no .envrc loaded);
    from its output only the REQUIRED_ENV variables that are still unset are
    imported, plus PATH entries we lack (the command's PATH may know `claude`).
    Nothing else is copied and no value is ever printed. A failing command is
    not fatal: preflight names what is still missing. Returns a status line."""
    environ = os.environ if environ is None else environ
    if all(environ.get(k) for k in REQUIRED_ENV):
        return "env: credentials already in the environment"
    cmd = env_command(config)
    try:
        proc = run(cmd, capture_output=True, timeout=60, cwd=str(REPO_DIR))
    except (OSError, subprocess.SubprocessError) as e:
        return f"env: command {cmd[0]!r} failed ({type(e).__name__})"
    if proc.returncode != 0:
        return f"env: command {cmd[0]!r} exited {proc.returncode}"
    found = {}
    for item in (proc.stdout or b"").split(b"\0"):
        key, sep, val = item.decode("utf-8", "replace").partition("=")
        if sep and key:
            found[key] = val
    imported = []
    for key in REQUIRED_ENV:
        if not environ.get(key) and found.get(key):
            environ[key] = found[key]
            imported.append(key)
    have = [d for d in (environ.get("PATH") or "").split(os.pathsep) if d]
    extra = [d for d in (found.get("PATH") or "").split(os.pathsep) if d and d not in have]
    if extra:
        environ["PATH"] = os.pathsep.join(have + extra)
        imported.append("PATH")
    return "env: imported " + (", ".join(imported) if imported else "nothing")


def _is_plaud(rec_id):
    return not rec_id.startswith(NON_PLAUD_PREFIXES)


def preflight(job, config=None, environ=None):
    """Check what this job needs before any paid or network call; raise
    PreflightError naming every missing variable.

    - claude: summarization shells out to `claude -p` (plaud_transcribe.claude_summarize),
      which authenticates with CLAUDE_CODE_OAUTH_TOKEN. A login kept only in the
      Claude keychain cannot be checked without a paid call, so the token is
      required, unless PLAUD_RUNNER_CLAUDE_LOGIN=1 says "trust the CLI's own login".
      The `claude` binary must also be on PATH (launchd's PATH is short).
    - ELEVENLABS_API_KEY: every recording but Wispr ones is transcribed there.
    - PLAUD_BEARER_TOKEN (or config "token", as get_token does): Plaud ids only."""
    environ = os.environ if environ is None else environ
    ids = job.get("recordings") or []
    missing = []
    if not environ.get("CLAUDE_CODE_OAUTH_TOKEN") and environ.get("PLAUD_RUNNER_CLAUDE_LOGIN") != "1":
        missing.append("CLAUDE_CODE_OAUTH_TOKEN (or PLAUD_RUNNER_CLAUDE_LOGIN=1 for a logged-in claude)")
    if not shutil.which("claude", path=environ.get("PATH")):
        missing.append("`claude` CLI on PATH")
    if any(not i.startswith("wispr:") for i in ids) and not environ.get("ELEVENLABS_API_KEY"):
        missing.append("ELEVENLABS_API_KEY")
    if any(_is_plaud(i) for i in ids) and not environ.get("PLAUD_BEARER_TOKEN") \
            and not (config or {}).get("token"):
        missing.append("PLAUD_BEARER_TOKEN (or 'token' in the config)")
    if missing:
        raise PreflightError("missing before start: " + "; ".join(missing)
                             + " - the runner loads them via direnv (see `runner --help`)")


# ---------------------------------------------------------------- steps

def _token(pt):
    try:
        return pt.get_token()
    except SystemExit:  # get_token() exits on a missing token; a job must fail instead
        raise RuntimeError("no Plaud token: set PLAUD_BEARER_TOKEN or 'token' in the config")


def _resolve_recordings(ids, recordings):
    """Recording dicts for the job's ids, in the job's order. The listing is the
    only place ids turn into dicts (plaud, wispr:, recorder:); `local:` ids are a
    content hash of a file on disk, so they cannot be found again from the id."""
    by_id = {r["id"]: r for r in recordings}
    found, missing = [], []
    for rid in ids:
        (found if rid in by_id else missing).append(by_id.get(rid) or rid)
    if missing:
        local = [m for m in missing if m.startswith("local:")]
        if local:
            raise RuntimeError(f"cannot resolve {', '.join(local)}: local files are not "
                               "listed; run `plaud-transcribe file <audio>` instead")
        raise RuntimeError(f"recording(s) not found: {', '.join(missing)} - not in the "
                           "listing for the last days; raise --days / config runner.days")
    return found


def step_transcribe(job, ctx, checkpoint=None):
    pt = ctx.host
    config = pt.load_config()
    projects = pt.get_projects(config)
    if any(_is_plaud(i) for i in job["recordings"]):
        token = _token(pt)
        listing = pt.list_all_recordings(token, config, ctx.days)
        folders = pt.fetch_folders(token, config)
    else:
        # Wispr / recorder / local only: their listers read the local store or the
        # phone, so no Plaud token and no Plaud API call (K4 gap).
        token, folders = None, {}
        listing = pt.wispr_flow.list_meetings(config, ctx.days) \
            + pt.google_recorder.list_recordings(config, ctx.days)
    recordings = _resolve_recordings(job["recordings"], listing)
    language = config.get("language", "cs")
    project = job.get("project")
    result = job.setdefault("result", {})
    done = result.setdefault("recordings", [])
    finished = {r["file_id"] for r in done if r.get("file_id")}

    # phone first: adb pulls everything the job needs in one go (never prompts:
    # set_interactive(False) was called when the runner started)
    pt.prefetch_phone_files(recordings, bool(job.get("merge")), config)

    def record(res):
        done.append(res)
        result["paths"] = [r["output_dir"] for r in done if r.get("output_dir")]
        if checkpoint:
            checkpoint()

    if job.get("merge"):
        if not done:
            state = pt.load_state()
            res = pt.process_merged_recordings(
                recordings, token, config, folders, projects, state, language,
                extra_instructions=job.get("instructions"),
                forced_project=project, interactive=False)
            if res:
                record(res)
        return "done"

    for rec in recordings:
        if rec["id"] in finished:   # an earlier attempt already filed this one
            continue
        spans = (job.get("cuts") or {}).get(rec["id"])
        cuts = sorted(pt.parse_cut(c) for c in spans) if spans else None
        state = pt.load_state()     # fresh per recording: another process may have saved
        res = pt.process_recording(
            rec, token, config, folders, projects, state, language,
            extra_instructions=job.get("instructions"), cuts=cuts,
            forced_project=project, interactive=False)
        if res:
            record(res)
    return "done"


def _placeholder(job, ctx, checkpoint=None):
    return "skipped"   # K8 registers the real deliver


STEP_FUNCS = {
    "transcribe": step_transcribe,
    "commit": publish.step_commit,
    "push": publish.step_push,
    "deliver": _placeholder,
}


# ---------------------------------------------------------------- job machine

def _fresh_state(job_id):
    try:
        return jobs.load_job(job_id).get("state")
    except jobs.JobError:
        return None


def _save(job):
    """Save, unless the job was cancelled on disk meanwhile: then keep the
    cancellation (persisting our step progress) and stop."""
    if _fresh_state(job["id"]) == "cancelled":
        job["state"] = "cancelled"
        jobs.save_job(job)
        raise Cancelled(job["id"])
    jobs.save_job(job)


def run_job(job, ctx):
    """Run the job's remaining steps. Returns the job's final state. Exceptions
    from steps become a `failed` job (B6); KeyboardInterrupt/SystemExit pass through."""
    job["state"] = "running"
    try:
        _save(job)
        pending = next((s for s in job["steps"] if s["name"] == "transcribe"), None)
        if not (pending and pending["state"] in DONE_STEPS):
            try:
                config = ctx.host.load_config(validate=False)
            except (OSError, ValueError):
                config = {}
            try:
                preflight(job, config)
            except PreflightError as e:
                jobs.set_step(job, "preflight", "failed", error=str(e))
                job.update(state="failed", error=str(e), failed_step="preflight")
                _save(job)
                ctx.notify("Plaud job failed", f"{job['id']}: {str(e)[:200]}", sound="Basso")
                return "failed"
        for name in STEPS:
            if _fresh_state(job["id"]) == "cancelled":
                raise Cancelled(job["id"])
            entry = next((s for s in job["steps"] if s["name"] == name), None)
            if entry and entry["state"] in DONE_STEPS:
                continue
            jobs.set_step(job, name, "running")
            _save(job)
            try:
                outcome = STEP_FUNCS[name](job, ctx, lambda: _save(job))
            except Cancelled:
                raise
            except Exception as e:
                msg = f"{type(e).__name__}: {e}"[:500]
                jobs.set_step(job, name, "failed", error=msg)
                job.update(state="failed", error=msg, failed_step=name)
                _save(job)
                ctx.notify("Plaud job failed", f"{job['id']} at {name}: {msg[:200]}", sound="Basso")
                return "failed"
            jobs.set_step(job, name, outcome or "done")
            _save(job)
        job["state"] = "done"
        job["error"] = None
        job.pop("failed_step", None)
        _save(job)
    except Cancelled:
        return "cancelled"
    if (job.get("deliver") or {}).get("mode") == "none":
        recs = (job.get("result") or {}).get("recordings") or []
        label = recs[0].get("name") if len(recs) == 1 else f"{len(recs)} recordings"
        ctx.notify("Plaud transcript ready", f"{label or job['id']} ({job['id']})")
    return "done"


def _runnable(job):
    # a job still `running` while we hold the lock was left by a crash or reboot
    return job.get("state") in ("queued", "running")


def run_queue(host, days=DEFAULT_DAYS, once=False, only=None, say=print):
    """Oldest queued job first, until none is left. Returns the ids it ran."""
    visuals.set_interactive(False)   # before anything can reach the phone prompt
    ctx = SimpleNamespace(host=host, days=days, notify=lambda *a, **k: notify(*a, **k))
    ran = []
    while True:
        candidates = [j for j in jobs.list_jobs()
                      if _runnable(j) and j["id"] not in ran and (only is None or j["id"] == only)]
        if not candidates:
            break
        job = candidates[0]
        say(f"runner: {job['id']} ({job.get('state')})")
        ran.append(job["id"])
        say(f"runner: {job['id']} -> {run_job(job, ctx)}")
        if once or only:
            break
    return ran


# ---------------------------------------------------------------- CLI

EPILOG = """\
recipes:
  work through everything queued, then exit
    plaud-transcribe runner
  run just one job (also the way to resume it after `jobs retry`)
    plaud-transcribe runner --job 20261124-1702
  look further back for Plaud recordings than the default 30 days
    plaud-transcribe runner --days 90        (or config: {"runner": {"days": 90}})
  use another lock file (tests)
    plaud-transcribe runner --lock-file /tmp/r.lock

behaviour:
  Only one runner works at a time; a second exits 0 at once. A job left
  'running' by a crash is resumed from its first unfinished step. It never
  prompts. A failed job gets one notification and the queue goes on.
  Env: PLAUD_RUNNER_LOCK sets the default lock file, PLAUD_JOBS_DIR the jobs dir.

credentials:
  Started by launchd there is no shell, so at start the runner fills in what is
  missing (CLAUDE_CODE_OAUTH_TOKEN, ELEVENLABS_API_KEY, PLAUD_BEARER_TOKEN) by
  running `direnv exec <repo> env -0` (needs `direnv allow` once; the .envrc
  runs sops). Variables already set win; nothing else is imported.
    PLAUD_RUNNER_ENV_CMD="..."   or config {"runner": {"env_cmd": "..."}}
                                 other command printing NUL-separated KEY=VALUE
    PLAUD_RUNNER_CLAUDE_LOGIN=1  accept a logged-in `claude` instead of the token
  Before any paid call a job whose credentials are missing fails naming them.
  Wispr-only jobs need no ElevenLabs key and no Plaud token.
"""


def build_parser():
    p = argparse.ArgumentParser(
        prog="plaud-transcribe runner",
        description="Process queued jobs one at a time, then exit.",
        epilog=EPILOG, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--once", action="store_true", help="run one job, then exit")
    p.add_argument("--job", metavar="ID", help="run only this job (id or unique prefix)")
    p.add_argument("--lock-file", metavar="PATH", default=None,
                   help=f"lock file (default: {LOCK_FILE})")
    p.add_argument("--days", type=int, default=None,
                   help=f"how far back to look up Plaud recordings by id "
                        f"(default: config runner.days, else {DEFAULT_DAYS})")
    return p


def main(argv=None, host=None):
    args = build_parser().parse_args(argv)
    if host is None:
        import plaud_transcribe as host
    only = None
    if args.job:
        try:
            only = jobs.load_job(args.job)["id"]
        except jobs.JobError as e:
            print(f"plaud-transcribe runner: {e}", file=sys.stderr)
            return 2
    lock = acquire_lock(args.lock_file)
    if lock is None:
        print("runner: another runner holds the lock; exiting")
        return 0
    try:
        try:
            cfg = host.load_config(validate=False)
        except (OSError, ValueError):
            cfg = {}
        print("runner: " + load_runner_env(cfg))
        days = args.days
        if days is None:
            try:
                days = int((host.load_config(validate=False).get("runner") or {}).get("days", DEFAULT_DAYS))
            except (OSError, ValueError, TypeError):
                days = DEFAULT_DAYS
        run_queue(host, days=days, once=args.once, only=only)
    finally:
        lock.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
