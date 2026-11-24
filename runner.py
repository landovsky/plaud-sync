"""The job runner: takes queued jobs one at a time, runs their steps, exits.

    plaud-transcribe runner            work through every queued job, then exit
    plaud-transcribe runner --once     one job only
    plaud-transcribe runner --job ID   only that job (queued, running or after `jobs retry`)

Steps run in order transcribe -> commit -> push -> deliver. A step runs only if it
is not already done/skipped, and its state is written to the job file before and
after, so a crash or reboot resumes at the first unfinished step (B2). The
transcribe step leans on the pipeline's work-dir cache, so a resume does not pay
for the transcription twice. commit/push/deliver are pluggable (STEP_FUNCS);
until their chunks land they are placeholders that mark themselves `skipped`.

One runner at a time: a flock on the lock file (the kernel drops it when the
process dies, so a crash never leaves a stale lock). A second runner exits 0 at
once (B9); the launchd WatchPaths re-trigger relies on that.
"""

import argparse
import fcntl
import os
import sys
from pathlib import Path
from types import SimpleNamespace

import jobs
from autocapture import notify  # module-level, dependency-free: reused, not copied
import visuals

STEPS = ("transcribe", "commit", "push", "deliver")
DONE_STEPS = ("done", "skipped")
DEFAULT_DAYS = 30
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
    token = _token(pt)
    recordings = _resolve_recordings(job["recordings"],
                                     pt.list_all_recordings(token, config, ctx.days))
    folders = pt.fetch_folders(token, config)
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
    return "skipped"   # K6-K8 register the real commit / push / deliver


STEP_FUNCS = {
    "transcribe": step_transcribe,
    "commit": _placeholder,
    "push": _placeholder,
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
