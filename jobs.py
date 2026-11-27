"""Job files: the contract between enqueue / the plan screen / the runner.

One JSON file per job in JOBS_DIR (default ~/.plaud-sync/jobs, override with the
env var PLAUD_JOBS_DIR). Written atomically (tmp + os.replace) so a reader, a
launchd WatchPaths trigger or a crash never sees half a file.

Fields (schema 1): id, schema, created, recordings, merge, cuts, instructions,
project, push, deliver {node, mode, note, ask}, state, steps, result, error,
seen, and for an armed job trigger {"wispr": "<uuid>", "armed_at": <iso>}
(recordings stays empty until the runner promotes it). `project`, `push`, `deliver.node` and `deliver.ask` are null when the job
was enqueued without a project: they resolve at the commit step from the
project the classifier picks.

States: queued | running | done | failed | held | armed | cancelled.
Stdlib only: no network, no import of plaud_transcribe.
"""

import json
import os
import re
import secrets
from datetime import datetime
from pathlib import Path

SCHEMA = 1
STATES = ("queued", "running", "done", "failed", "held", "armed", "cancelled")
CANCELLABLE = ("queued", "failed", "held", "armed")
PUSH_MODES = ("main", "pr", "none")
DELIVER_MODES = ("now", "later", "none")

JOBS_DIR = Path(os.environ.get("PLAUD_JOBS_DIR") or "~/.plaud-sync/jobs").expanduser()

# Plaud ids are opaque tokens, others are "wispr:<uuid>", "recorder:<n>",
# "call:<YYYYMMDD-HHMMSS>", "local:<hex>".
_REC_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:\-]*$")
_HOST_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.@-]*")
_CUT_RE = re.compile(r"^\d+(:\d+){0,2}-\d+(:\d+){0,2}$")


class JobError(ValueError):
    """A job that cannot be saved or an operation the job's state forbids."""


def now_iso():
    return datetime.now().astimezone().isoformat(timespec="seconds")


def new_job_id(now=None):
    """YYYYMMDD-HHMM-xxxx: sortable by creation time, 4 hex chars against clashes."""
    now = now or datetime.now()
    return f"{now:%Y%m%d-%H%M}-{secrets.token_hex(2)}"


def new_job(**fields):
    """A job with every schema field present; `fields` override the defaults."""
    job = {
        "schema": SCHEMA,
        "id": new_job_id(),
        "created": now_iso(),
        "recordings": [],
        "merge": False,
        "cuts": {},
        "instructions": None,
        "project": None,
        "push": None,
        "deliver": {"node": None, "mode": "none", "note": None, "ask": None, "host": None},
        "state": "queued",
        "steps": [],
        "result": {"paths": [], "commits": [], "pr": None, "delivered": None},
        "error": None,
        "seen": False,
    }
    job.update(fields)
    return job


def validate_job(job):
    """Raise JobError with a message a person can act on; return the job."""
    if not isinstance(job, dict):
        raise JobError("job must be an object")
    jid = job.get("id")
    if not isinstance(jid, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.\-]*", jid):
        raise JobError(f"bad job id {jid!r}")
    if job.get("schema") != SCHEMA:
        raise JobError(f"unsupported job schema {job.get('schema')!r} (this build reads {SCHEMA})")
    if job.get("state") not in STATES:
        raise JobError(f"state must be one of {'|'.join(STATES)}, got {job.get('state')!r}")
    recs = job.get("recordings")
    if not isinstance(recs, list) or not all(isinstance(r, str) for r in recs):
        raise JobError("recordings must be a list of ids")
    trig = job.get("trigger")
    if trig is not None and not (isinstance(trig, dict) and isinstance(trig.get("wispr"), str)
                                 and _REC_ID_RE.match(trig["wispr"])):
        raise JobError('trigger must look like {"wispr": "<meeting uuid>", "armed_at": ...}')
    if not recs and not trig:
        raise JobError("a job needs at least one recording (--recording ID)")
    for r in recs:
        if not _REC_ID_RE.match(r):
            raise JobError(f"recording id {r!r} is not a valid id (letters, digits, : . _ - only)")
    if len(set(recs)) != len(recs):
        raise JobError("the same recording is listed twice")
    cuts = job.get("cuts") or {}
    if not isinstance(cuts, dict):
        raise JobError("cuts must map recording id to a list of START-END")
    for rec, spans in cuts.items():
        if rec not in recs:
            raise JobError(f"cut for {rec!r}, which is not one of the job's recordings")
        if not isinstance(spans, list) or not spans or not all(
                isinstance(s, str) and _CUT_RE.match(s) for s in spans):
            raise JobError(f"cuts for {rec!r} must be a list of START-END like 29:16-34:55")
    if job.get("merge") and any(cuts.values()):
        raise JobError("merge and cuts cannot be combined: cuts work on one recording at a "
                       "time, so drop --merge or drop --cut")
    if job.get("push") is not None and job["push"] not in PUSH_MODES:
        raise JobError(f"push must be {'|'.join(PUSH_MODES)}, got {job['push']!r}")
    deliver = job.get("deliver")
    if not isinstance(deliver, dict) or deliver.get("mode") not in DELIVER_MODES:
        raise JobError(f"deliver.mode must be one of {'|'.join(DELIVER_MODES)}")
    if deliver["mode"] != "none" and job.get("project") and not deliver.get("node"):
        raise JobError(f"deliver mode '{deliver['mode']}' needs a node, and project "
                       f"{job['project']!r} has none: pass --node or use --mode none")
    host = deliver.get("host")
    if host is not None and not isinstance(host, str):
        raise JobError(f"deliver.host must be an ssh host name, got {host!r}")
    if host:                                 # None: the project's (or this Mac); "": this Mac
        if not isinstance(host, str) or not _HOST_RE.fullmatch(host):
            raise JobError(f"deliver.host {host!r} is not an ssh host name")
        if job.get("push") == "none":
            raise JobError(f"hand-off on {host}: the session there reads the transcript from "
                           "origin, so push must be main or pr (not none)")
    return job


def _path(job_id):
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.\-]*", job_id or ""):
        raise JobError(f"bad job id {job_id!r}")
    return JOBS_DIR / f"{job_id}.json"


def save_job(job):
    """Validate, then write via a temp file and os.replace. A failure at any
    point leaves the previous file (or none) in place and no temp file."""
    validate_job(job)
    JOBS_DIR.mkdir(parents=True, exist_ok=True)
    target = _path(job["id"])
    tmp = target.with_name(target.name + ".tmp")  # not *.json: list_jobs ignores it
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(job, f, indent=2, ensure_ascii=False)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, target)
    except BaseException:
        try:
            tmp.unlink()
        except FileNotFoundError:
            pass
        raise
    return target


def load_job(job_id):
    """Load by exact id, or by a unique prefix. Raises JobError when absent/ambiguous."""
    p = _path(job_id)
    if not p.exists():
        matches = sorted(JOBS_DIR.glob(f"{job_id}*.json")) if JOBS_DIR.is_dir() else []
        if len(matches) > 1:
            raise JobError(f"job id {job_id!r} is ambiguous: "
                           + ", ".join(m.stem for m in matches))
        if not matches:
            raise JobError(f"no job {job_id!r} in {JOBS_DIR}")
        p = matches[0]
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        raise JobError(f"cannot read {p}: {e}")


def list_jobs():
    """All readable jobs, oldest first. Unreadable files are skipped."""
    if not JOBS_DIR.is_dir():
        return []
    jobs = []
    for p in JOBS_DIR.glob("*.json"):
        try:
            job = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(job, dict) and job.get("id"):
            jobs.append(job)
    return sorted(jobs, key=lambda j: (j.get("created") or "", j["id"]))


def set_step(job, name, state, error=None):
    """Record a step's state in job['steps'] (in memory; the caller saves)."""
    entry = next((s for s in job["steps"] if s["name"] == name), None)
    if entry is None:
        entry = {"name": name}
        job["steps"].append(entry)
    entry.update({"state": state, "at": now_iso()})
    if error:
        entry["error"] = error
    else:
        entry.pop("error", None)
    return job


def cancel_job(job_id):
    job = load_job(job_id)
    if job["state"] not in CANCELLABLE:
        raise JobError(f"job {job['id']} is {job['state']}: only "
                       f"{'/'.join(CANCELLABLE)} jobs can be cancelled")
    job["state"] = "cancelled"
    save_job(job)
    return job


RETRYABLE = ("failed", "held")


def retry_job(job_id):
    """failed/held -> queued. Clears the error, keeps steps that are done/skipped
    so the runner resumes where the job stopped."""
    job = load_job(job_id)
    if job["state"] not in RETRYABLE:
        raise JobError(f"job {job['id']} is {job['state']}: only "
                       f"{'/'.join(RETRYABLE)} jobs can be retried")
    if job.get("trigger") and not job.get("recordings"):
        # An armed job that timed out or lost its meeting never got a recording:
        # retrying means waiting for the meeting again, from now.
        job["state"] = "armed"
        job["trigger"]["armed_at"] = now_iso()
        job["error"] = None
        job.pop("failed_step", None)
        job["steps"] = []
        save_job(job)
        return job
    job["state"] = "queued"
    job["error"] = None
    job.pop("failed_step", None)
    job["steps"] = [s for s in job["steps"] if s.get("state") in ("done", "skipped")]
    save_job(job)
    return job


def mark_seen(job_id):
    job = load_job(job_id)
    job["seen"] = True
    save_job(job)
    return job
