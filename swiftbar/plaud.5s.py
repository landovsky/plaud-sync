#!/usr/bin/env python3
# <xbar.title>plaud-sync</xbar.title>
# <xbar.desc>Autocapture state, the transcription job queue and their actions</xbar.desc>
# <swiftbar.hideAbout>true</swiftbar.hideAbout>
# <swiftbar.hideRunInTerminal>true</swiftbar.hideRunInTerminal>
"""SwiftBar plugin: autocapture + transcription jobs in the menu bar.

Stdlib only and deliberately NOT importing plaud_transcribe / autocapture / jobs /
visuals: a refresh must stay far below 100 ms, never touch the network, Claude
or cc-tree (spec F6). It reads local files only:
  jobs/*.json                        (PLAUD_JOBS_DIR, default ~/.plaud-sync/jobs)
  ~/.cache/plaud-autocapture/        status.json, autocapture.pid, paused, displays.json
The paths and the job schema are duplicated from jobs.py / autocapture.py on
purpose; tests/test_swiftbar_plugin.py fails if they drift.

`plaud-transcribe install-swiftbar` symlinks this file into SwiftBar's plugin
folder; the "5s" in the file name is SwiftBar's refresh interval.

Title glyph, highest priority first:
  x watcher down (pid dead, not paused)  >  warn failed/held job not yet seen
  >  runner busy + count  >  capturing  >  watching.  Paused shows "paused".
render(now, jobs, status, pid_alive, paused, overrides, actions) is pure: no I/O.

Actions (toggle a monitor, pause/resume, retry/cancel/mark seen a job) are menu lines with
`bash=<python> param1=<script> ... terminal=false refresh=true`. The interpreter and repo come
from ~/.plaud-sync/swiftbar.json (env PLAUD_SWIFTBAR_CONFIG), written by `install-swiftbar`:
this plugin runs on the system python, the actions need the repo's. Without that file the
menu has no actions (only "Show log"). "Plan a transcript…" opens a terminal (terminal=true) on
`plaud_transcribe.py plan`; swiftbar.json may carry "terminal_cmd" (a list) to use another terminal
instead. Every action passes --notify-errors, because
terminal=false would hide a failure.
"""

import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path

JOBS_DIR = Path(os.environ.get("PLAUD_JOBS_DIR") or "~/.plaud-sync/jobs").expanduser()
STATE_DIR = Path.home() / ".cache" / "plaud-autocapture"
CONFIG_FILE = Path(os.environ.get("PLAUD_SWIFTBAR_CONFIG")
                   or Path.home() / ".plaud-sync" / "swiftbar.json").expanduser()
RUNNER_LOG = Path.home() / ".plaud-sync" / "runner.log"

MAX_JOBS = 10
DONE_HIDE_S = 24 * 3600    # finished jobs older than this leave the menu
NEEDS_ATTENTION = ("failed", "held")
BUSY = ("queued", "running")
LABEL_MAX = 60


def clean(text, limit=LABEL_MAX):
    """SwiftBar treats '|' as the parameter separator and newlines end a line."""
    text = " ".join(str(text if text is not None else "").replace("|", "/").split())
    return text if len(text) <= limit else text[:limit - 1] + "…"


def _q(value):
    """One SwiftBar parameter value: double-quoted when it holds whitespace."""
    value = str(value)
    return f'"{value}"' if any(c.isspace() for c in value) else value


def _param_safe(value):
    """SwiftBar splits a line on '|' and parses quotes itself; a value holding
    those (or a backslash or newline) cannot be passed through intact."""
    return not any(c in str(value) for c in '|"\\\n\r')


def act(actions, *argv, script="autocapture.py"):
    """' | bash=... param1=...' that runs `<python> <repo>/<script> argv... --notify-errors`;
    '' without a recorded interpreter (so the line stays plain text)."""
    if not actions or not actions.get("python") or not actions.get("repo"):
        return ""
    args = [str(Path(actions["repo"]) / script), *argv, "--notify-errors"]
    params = " ".join(f"param{i}={_q(a)}" for i, a in enumerate(args, 1))
    return f" | bash={_q(actions['python'])} {params} terminal=false refresh=true"


def plan_action(actions):
    """' | bash=... terminal=true' that opens a terminal running `plaud_transcribe.py plan`.
    With swiftbar.json "terminal_cmd" (e.g. ["open","-na","Ghostty","--args","-e"]) that
    command line runs instead, with SwiftBar's own terminal off. '' without actions."""
    if not actions or not actions.get("python") or not actions.get("repo"):
        return ""
    run = [actions["python"], str(Path(actions["repo"]) / "plaud_transcribe.py"), "plan"]
    term = actions.get("terminal_cmd")
    if term:
        argv, flag = [*term, *run], "terminal=false"
    else:
        argv, flag = run, "terminal=true"
    if not all(_param_safe(a) for a in argv):
        return ""
    params = " ".join(f"param{i}={_q(a)}" for i, a in enumerate(argv[1:], 1))
    return f" | bash={_q(argv[0])} {params} {flag} refresh=true".replace("  ", " ")


def log_action():
    return f" | bash=/usr/bin/open param1=-a param2=Console param3={_q(RUNNER_LOG)} terminal=false"


def _ts(iso):
    try:
        return datetime.fromisoformat(iso).timestamp()
    except (TypeError, ValueError):
        return None


def needs_attention(job):
    return job.get("state") in NEEDS_ATTENTION and not job.get("seen")


def _job_name(job):
    recs = job.get("recordings") or []
    what = recs[0] if len(recs) == 1 else f"{len(recs)} recordings" if recs else "armed meeting"
    return clean(f"{job.get('project') or 'unrouted'}: {what}", 40)


def _current_step(job):
    for s in reversed(job.get("steps") or []):
        if s.get("state") == "running":
            return s.get("name")
    steps = job.get("steps") or []
    return steps[-1].get("name") if steps else None


def job_line(job):
    state = job.get("state")
    name = _job_name(job)
    delivered = (job.get("result") or {}).get("delivered") or {}
    if state == "failed":
        step = job.get("failed_step") or _current_step(job)
        where = f"{step} failed: " if step else "failed: "
        mark = "⚠" if not job.get("seen") else "✕"
        return f"{mark} {name} — {clean(where + str(job.get('error') or 'unknown error'), 90)}"
    if state == "held":
        hid = delivered.get("id")
        mark = "⚠" if not job.get("seen") else "✓"
        return f"{mark} {name} — held for you" + (f" ({clean(hid, 20)})" if hid else "")
    if state == "running":
        return f"⟳ {name} — {clean(_current_step(job) or 'starting', 30)}"
    if state == "queued":
        return f"⟳ {name} — queued"
    if state == "armed":
        return f"○ {name} — armed, waiting for the meeting to end"
    if state == "cancelled":
        return f"✕ {name} — cancelled"
    if state == "done":
        outcome = delivered.get("outcome")
        tail = {"message": "delivered", "fresh": "delivered (fresh session)"}.get(outcome, "done")
        return f"✓ {name} — {tail}"
    return f"{clean(state)} {name}"


def visible_jobs(jobs, now):
    """Most recent first, finished ones older than 24 h hidden, capped at MAX_JOBS.
    Jobs that need attention are never pushed out by finished ones."""
    out = []
    for j in jobs:
        created = _ts(j.get("created"))
        if (j.get("state") in ("done", "cancelled") or j.get("seen")) \
                and created is not None and now - created > DONE_HIDE_S:
            continue
        out.append((created or 0, j.get("id", ""), j))
    out.sort(key=lambda t: (t[0], t[1]), reverse=True)
    shown = [j for _, _, j in out]
    attention = [j for j in shown if needs_attention(j)]
    rest = [j for j in shown if not needs_attention(j)]
    keep = attention[:MAX_JOBS] + rest[:max(0, MAX_JOBS - len(attention))]
    return [j for j in shown if j in keep]


def job_menu(job, actions):
    """The job's line plus its submenu: Retry / Show log / Cancel / Mark seen, and for a
    held job the id and the command that releases it (D2). Running jobs: click = log."""
    state = job.get("state")
    line = "--" + job_line(job)
    jid = job.get("id", "")
    if state in ("failed", "held", "queued", "armed"):
        sub = []
        delivered = (job.get("result") or {}).get("delivered") or {}
        if state == "held" and delivered.get("id"):
            hid = clean(delivered["id"], 20)
            sub.append(f"----Held for you: {hid}")
            sub.append(f"----Release it: cc-tree deliver --release {hid}")
        if state in ("failed", "held"):
            sub.append("----Retry" + act(actions, "jobs", "retry", jid, script="plaud_transcribe.py"))
        sub.append("----Show log" + log_action())
        sub.append("----Cancel" + act(actions, "jobs", "cancel", jid, script="plaud_transcribe.py"))
        if state in ("failed", "held") and not job.get("seen"):
            sub.append("----Mark seen" + act(actions, "jobs", "seen", jid, script="plaud_transcribe.py"))
        return [line] + sub
    if state == "running":
        return [line + log_action()]
    return [line]


def render(now, jobs, status, pid_alive, paused, overrides, actions=None):
    """The whole plugin output as one string. `now` is epoch seconds; `actions` is
    {"python", "repo"} from swiftbar.json, or None for a menu without actions."""
    status = status or {}
    overrides = overrides or {}
    jobs = [j for j in (jobs or []) if isinstance(j, dict)]

    down = not pid_alive and not paused
    if paused and not pid_alive:
        text = "paused"
    elif down:
        text = "NOT RUNNING"
    elif status.get("state") == "capturing":
        started = status.get("started")
        mins = int((now - started) // 60) if isinstance(started, (int, float)) else None
        text = "capturing" + (f" {max(mins, 0)} min" if mins is not None else "") \
            + f" · {status.get('saved', 0)} scenes"
    elif status.get("state") == "error":
        text = "error: " + clean(status.get("error"), 50)
    else:
        text = "watching (Wispr)"

    busy = sum(1 for j in jobs if j.get("state") in BUSY)
    attention = any(needs_attention(j) for j in jobs)
    if down:
        glyph = "✕"
    elif attention:
        glyph = "⚠"
    elif busy:
        glyph = f"⟳{busy}"
    elif not paused and status.get("state") == "capturing":
        glyph = "●"
    else:
        glyph = "○"
    title = "○ paused" if text == "paused" and glyph == "○" else glyph

    lines = [title, "---", f"Autocapture: {text}"]
    shown = status.get("displays") if isinstance(status.get("displays"), list) else []
    seen_names = set()
    disp = []
    for pos, d in enumerate(shown, 1):
        if not isinstance(d, dict) or not d.get("name"):
            continue
        seen_names.add(d["name"])
        mark = "✓" if d.get("captured") else " "
        suffix = "" if d.get("captured") else " (off)"
        # a name SwiftBar cannot carry is passed as its number, which `display` resolves
        target = d["name"] if _param_safe(d["name"]) else str(pos)
        disp.append(f"--{mark} {clean(d['name'])}{suffix}"
                    + act(actions, "display", target, "off" if d.get("captured") else "on"))
    for name, on in sorted(overrides.items()):   # F3: an unplugged monitor keeps its override
        if name not in seen_names:
            disp.append(f"--  {clean(name)} (not connected, override {'on' if on else 'off'})")
    if disp:
        lines.append("Displays")
        lines.extend(disp)
    if paused:
        lines.append("Resume autocapture" + act(actions, "start", "--watch"))
    elif down:
        lines.append("Start autocapture" + act(actions, "start", "--watch"))
    else:
        lines.append("Pause autocapture" + act(actions, "stop"))

    plan = plan_action(actions)
    if plan:
        lines.append("Plan a transcript…" + plan)
    lines.append("---")
    lines.append("Jobs")
    listed = visible_jobs(jobs, now)
    if not listed:
        lines.append("--No recent jobs")
    for j in listed:
        lines.extend(job_menu(j, actions))
    if not actions:
        lines.append("---")
        lines.append("Menu actions off: run plaud-transcribe install-swiftbar")
    return "\n".join(lines) + "\n"


# --- the thin I/O shell ---

def _read_json(path):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _pid_alive(pid_file):
    try:
        os.kill(int(Path(pid_file).read_text().strip()), 0)
        return True
    except PermissionError:
        return True   # exists, owned by someone else
    except (OSError, ValueError):
        return False


def load_actions(path=None):
    cfg = _read_json(path or CONFIG_FILE)
    if isinstance(cfg, dict) and cfg.get("python") and cfg.get("repo") \
            and os.path.isabs(cfg["python"]) and os.path.isabs(cfg["repo"]):
        out = {"python": cfg["python"], "repo": cfg["repo"]}
        term = cfg.get("terminal_cmd")
        if isinstance(term, list) and term and all(isinstance(t, str) and t for t in term):
            out["terminal_cmd"] = term
        return out
    return None


def gather(state_dir=None, jobs_dir=None, config_file=None):
    state_dir = Path(state_dir or STATE_DIR)
    jobs_dir = Path(jobs_dir or JOBS_DIR)
    jobs = []
    if jobs_dir.is_dir():
        for p in jobs_dir.glob("*.json"):
            j = _read_json(p)
            if isinstance(j, dict) and j.get("id"):
                jobs.append(j)
    status = _read_json(state_dir / "status.json")
    overrides = _read_json(state_dir / "displays.json")
    return dict(jobs=jobs,
                status=status if isinstance(status, dict) else {},
                pid_alive=_pid_alive(state_dir / "autocapture.pid"),
                paused=(state_dir / "paused").exists(),
                overrides=overrides if isinstance(overrides, dict) else {},
                actions=load_actions(config_file))


def main():
    try:
        out = render(time.time(), **gather())
    except Exception as e:   # a menu-bar plugin must show something, not a traceback
        out = f"⚠\n---\nplaud plugin error: {clean(e)}\n"
    sys.stdout.write(out)


if __name__ == "__main__":
    main()
