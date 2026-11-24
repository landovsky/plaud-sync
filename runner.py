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
deliver is deliver.py (cc-tree hand-off). A held delivery leaves the deliver step
in state `held` and the job `held`; `jobs retry` then runs that step alone.

One runner at a time: a flock on the lock file (the kernel drops it when the
process dies, so a crash never leaves a stale lock). A second runner exits 0 at
once (B9); the launchd WatchPaths re-trigger relies on that.
"""

import argparse
import contextlib
import fcntl
import io
import json
import math
import os
import shlex
import shutil
import sqlite3
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import deliver
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


STEP_FUNCS = {
    "transcribe": step_transcribe,
    "commit": publish.step_commit,
    "push": publish.step_push,
    "deliver": deliver.step_deliver,
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
            if outcome == "held":
                held_id = ((job.get("result") or {}).get("delivered") or {}).get("id")
                job.update(state="held", error=f"held for you: {held_id or '?'}")
                _save(job)
                ctx.notify("Plaud job held", f"held for you: {held_id or '?'} ({job['id']})")
                return "held"
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


# ---------------------------------------------------------------- armed jobs (Part E)

ARM_ENV = "PLAUD_ARM_MAX_HOURS"


def arm_max_hours(config=None, flag=None):
    """How long an armed job may wait for its meeting: --arm-max-hours, else env
    PLAUD_ARM_MAX_HOURS, else config arm.max_hours, else the autocapture max_hours
    (3 by default: the same "a meeting never lasts longer" bound the watcher uses)."""
    import autocapture
    config = config or {}
    for raw in (flag, os.environ.get(ARM_ENV), (config.get("arm") or {}).get("max_hours")):
        if raw not in (None, ""):
            try:
                value = float(raw)
            except (TypeError, ValueError):
                continue
            if value > 0:
                return value
    return float(autocapture.settings(config)["max_hours"])


def _meeting_row(config, uuid):
    """'deleted' when the meeting has no row or isDeleted=1, 'present' when it
    exists, None when Wispr's store cannot be read (not installed, locked, odd
    schema: the caller waits, it never guesses)."""
    import wispr_flow
    db = wispr_flow.wispr_db_path(config)
    if not db.exists():
        return None
    try:
        conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=5)
        try:
            row = conn.execute("SELECT isDeleted FROM Meetings WHERE id = ?", (uuid,)).fetchone()
        finally:
            conn.close()
    except sqlite3.Error:
        return None
    return "deleted" if row is None or row[0] else "present"


def _last_marker(config, uuid):
    """(marker, age in seconds of live.ndjson) for the meeting's newest
    paused/stopped/resumed marker; (None, None) without a live transcript."""
    import wispr_flow
    p = wispr_flow._live_path(config, uuid)
    try:
        age = time.time() - p.stat().st_mtime
        lines = p.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return None, None
    for line in reversed(lines):
        if '"marker"' not in line:
            continue
        try:
            marker = json.loads(line).get("marker")
        except json.JSONDecodeError:
            continue
        if marker in ("paused", "stopped", "resumed"):
            return marker, age
    return None, age


def meeting_ready(config, uuid):
    """Where a Wispr meeting stands for an armed job:

    "gone"     its row is deleted (or never existed): nothing will ever arrive
    "live"     autocapture.wispr_live_meeting says it is being recorded: still
               running, which includes a paused-then-resumed meeting whose endedAt
               Wispr set at the pause and never cleared (2026-11-02, E2)
    "ready"    not live and wispr_flow.list_meetings lists it: its transcript is
               refined, or live_fallback_minutes passed since it ended
    "waiting"  anything else: ended but not refined yet, finalized=0, a pause that
               may still be resumed, Wispr not installed or its store unreadable

    A pause leaves the meeting looking ended, with a refined first segment, so
    list_meetings alone would list it: while the newest marker is `paused` and the
    live transcript changed less than live_fallback_minutes ago it stays waiting.
    """
    import autocapture
    import wispr_flow
    state = _meeting_row(config, uuid)
    if state == "deleted":
        return "gone"
    if state is None or not wispr_flow.wispr_enabled(config):
        return "waiting"
    live = autocapture.wispr_live_meeting(config, autocapture.settings(config)["max_hours"])
    if live and live["id"] == uuid:
        return "live"
    marker, age = _last_marker(config, uuid)
    if marker == "paused" and age is not None and age < wispr_flow.live_fallback_minutes(config) * 60:
        return "waiting"
    days = max(2, math.ceil(arm_max_hours(config) / 24) + 1)
    with contextlib.redirect_stderr(io.StringIO()):   # "still refining" notes would fill the runner log every minute
        listed = wispr_flow.list_meetings(config, days)
    return "ready" if any(m["id"] == f"wispr:{uuid}" for m in listed) else "waiting"


def _armed_age_hours(job):
    try:
        armed = datetime.fromisoformat(job["trigger"]["armed_at"])
        return (datetime.now(armed.tzinfo) - armed).total_seconds() / 3600
    except (KeyError, TypeError, ValueError):
        return 0.0   # no usable armed_at: do not time it out on a guess


def _fail_armed(job, message, notify_fn):
    jobs.set_step(job, "arm", "failed", error=message)
    job.update(state="failed", error=message, failed_step="arm")
    jobs.save_job(job)
    notify_fn("Plaud armed job failed", f"{job['id']}: {message}"[:200], sound="Basso")


def promote_armed(host, max_hours=None, notify_fn=None, say=print):
    """Turn armed jobs whose meeting is over into queued ones. Returns
    [(job id, "queued" | "failed" | "armed")] for the jobs it looked at.

    ready -> recordings ["wispr:<uuid>"], state queued (the same runner pass runs it);
    gone -> failed + one notification; armed longer than max_hours -> failed + one
    notification (a meeting that never ends must not wait forever, E3);
    live / waiting -> unchanged. Never raises: a broken store leaves jobs armed."""
    notify_fn = notify_fn or (lambda *a, **k: notify(*a, **k))
    armed = [j for j in jobs.list_jobs() if j.get("state") == "armed" and j.get("trigger")]
    if not armed:
        return []
    try:
        config = host.load_config(validate=False)
    except (OSError, ValueError):
        config = {}
    limit = arm_max_hours(config, max_hours)
    changes = []
    for job in armed:
        uuid = job["trigger"]["wispr"]
        try:
            status = meeting_ready(config, uuid)
            fresh = jobs.load_job(job["id"])
            if fresh.get("state") != "armed":     # cancelled while we looked
                continue
            job = fresh
            if status == "ready":
                job["recordings"] = [f"wispr:{uuid}"]
                job["state"] = "queued"
                job["trigger"]["promoted_at"] = jobs.now_iso()
                jobs.save_job(job)
            elif status == "gone":
                _fail_armed(job, f"the Wispr meeting {uuid} was deleted before its transcript "
                                 "was ready; nothing to transcribe", notify_fn)
            elif _armed_age_hours(job) > limit:
                _fail_armed(job, f"armed for more than {limit:g} h and meeting {uuid} never became "
                                 f"ready ({status}); retry re-arms it, or enqueue the recording by hand",
                            notify_fn)
            else:
                say(f"runner: {job['id']} still armed ({status})")
        except Exception as e:  # noqa: BLE001  one bad job must not stop the queue
            say(f"runner: could not check armed job {job['id']}: {type(e).__name__}: {e}")
            continue
        changes.append((job["id"], job["state"]))
        if job["state"] != "armed":
            say(f"runner: {job['id']} armed -> {job['state']}"
                + (f" ({job['error']})" if job["state"] == "failed" else ""))
    return changes


def _runnable(job):
    # a job still `running` while we hold the lock was left by a crash or reboot
    return job.get("state") in ("queued", "running")


def run_queue(host, days=DEFAULT_DAYS, once=False, only=None, say=print, deliver_plan=False,
              arm_hours=None):
    """Oldest queued job first, until none is left. Returns the ids it ran.
    Armed jobs are promoted before each pick, so one that became ready while a long
    job ran is run in this pass."""
    visuals.set_interactive(False)   # before anything can reach the phone prompt
    ctx = SimpleNamespace(host=host, days=days, deliver_plan=deliver_plan, notify=lambda *a, **k: notify(*a, **k))
    ran = []
    while True:
        promote_armed(host, max_hours=arm_hours, notify_fn=ctx.notify, say=say)
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
  see what delivery would do, without messaging any session
    plaud-transcribe runner --deliver-plan
  wait longer (or shorter) for an armed job's meeting before giving up
    plaud-transcribe runner --arm-max-hours 5   (or PLAUD_ARM_MAX_HOURS=5, or config {"arm": {"max_hours": 5}})
  use another lock file (tests)
    plaud-transcribe runner --lock-file /tmp/r.lock

behaviour:
  Only one runner works at a time; a second exits 0 at once. A job left
  'running' by a crash is resumed from its first unfinished step. It never
  prompts. A failed job gets one notification and the queue goes on.
  Env: PLAUD_RUNNER_LOCK sets the default lock file, PLAUD_JOBS_DIR the jobs dir.

publishing (push = main | pr, see publish.py):
  main  commits go to origin/main from a temporary worktree; the checkout is
        only fast-forwarded when that is harmless. A rejected push fails the job
        at `push` and keeps the local branch transcripts/<job-id>; there is no
        fallback to a PR. `jobs retry` then pushes the same commits.
  pr    the branch transcripts/<job-id> is pushed to origin under that name and
        `gh pr create --base main --head <branch>` runs in the repo root; the URL
        lands in result.pr. main and the checkout are never written. A retry
        reuses the pushed branch and the open PR.
    PLAUD_PUSH_RETRIES=N   rebase-and-retry rounds after a non-fast-forward
                           (default 3); config {"runner": {"push_retries": N}}
    GH_BIN=/path/to/gh     gh binary for pr mode (default gh);
                           config {"runner": {"gh": "/path/to/gh"}}

hand-off (deliver.py): `cc-tree deliver <node> -` with the work text on stdin
  once the transcripts are pushed. Skipped for mode none or no node. A held
  delivery sets the job to 'held' (one notification, "held for you: <id>");
  `jobs retry` then re-delivers only. cc-tree missing or failing fails just the
  deliver step; the transcript stays pushed and a retry makes no new commit.
    CC_TREE_BIN=/path/cc-tree      cc-tree binary (default cc-tree);
                                   config {"deliver": {"bin": "..."}}
    PLAUD_NONINTERACTIVE=1         never read stdin in the pipeline, even on a terminal (the runner sets it itself)
    PLAUD_DELIVER_TIMEOUT=SECONDS  wait for cc-tree (default 180); config deliver.timeout
    PLAUD_DELIVER_PLAN=1           dry run, same as --deliver-plan: runs
                                   `cc-tree deliver <node> --plan`, records its
                                   answer, never messages anything

armed jobs (enqueue --arm-wispr): before every pick the runner checks each armed
  job's Wispr meeting. Live (incl. paused-then-resumed) or still being refined: the
  job stays armed. Ready: it becomes queued and runs in the same pass. Deleted, or
  armed longer than the limit (default: config arm.max_hours, else
  visuals.autocapture.max_hours, else 3 h): failed with one notification.
  Ready = Wispr's refined transcript, or its live one wispr.live_fallback_minutes
  (default 30) after the meeting ended, so a meeting that Wispr does not refine is
  picked up up to that long after it ends. A meeting whose last marker is a pause
  waits that long too, in case it is resumed. Without Wispr installed armed jobs
  just wait for the limit. `plaud-transcribe jobs promote` does this check once.

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
    p.add_argument("--arm-max-hours", type=float, default=None, metavar="H",
                   help="give up on an armed job's meeting after H hours "
                        f"(env {ARM_ENV}; config arm.max_hours; default autocapture max_hours, 3)")
    p.add_argument("--deliver-plan", action="store_true",
                   help="dry-run the hand-off: `cc-tree deliver <node> --plan`, nothing is sent "
                        "(same as PLAUD_DELIVER_PLAN=1)")
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
        run_queue(host, days=days, once=args.once, only=only, deliver_plan=args.deliver_plan,
                  arm_hours=args.arm_max_hours)
    finally:
        lock.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
