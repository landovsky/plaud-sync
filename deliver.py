"""Hand a finished job to cc-tree: the runner's `deliver` step.

After the transcripts are pushed (or written, for push none) the step runs

    <bin> deliver <node> -          the work text on stdin

and cc-tree decides whether to message a healthy session, start a fresh one or
hold the work for Tomas. The runner only records which one happened, in
result.delivered = {"outcome": "message"|"fresh"|"held"|"unknown"|"plan",
"id": <held id or null>, "raw": <first 500 chars of stdout>}.

What `cc-tree deliver` prints on success (cc_tree_deliver.py `cmd`, last lines):
    message   "<ref>: messaged · <why>"   (a ccmsg ack "delivered 1/1" may precede it)
    start     "<ref>: no session: a fresh one started with the work in its brief · <why>"
    reseed    "<ref>: fresh session started from its checkpoint, with the work in its brief · <why>"
    checkpoint "<ref>: checkpoint requested; a fresh session gets the work once ..."  (-> fresh)
    hold      "<ref>: held for Tomas (he's told) · <why> · cc-tree deliver --release <id>"
Exit codes: 0 sent/started/held; 9 refused (node closed, nothing sent); 1/2/3-7 failures.
Any other stdout on exit 0 is "unknown": the raw text is kept and the step is still done.

On another machine (job deliver.host, from the project's "host" or `enqueue --host`):

    ssh -o BatchMode=yes <host> zsh -lc 'cc-tree deliver <node> -'

cc-tree runs there, so a session live on that machine is messaged and a missing one starts
there (cc-tree on the Mac starts fresh sessions on the Mac). That session reads the transcript
from origin: the job must push (main|pr) and the work text says to pull first.

Knobs
  CC_TREE_BIN            the cc-tree binary (default config deliver.bin, else `cc-tree`)
  PLAUD_DELIVER_TIMEOUT  seconds to wait for cc-tree (default 180); config deliver.timeout
  PLAUD_DELIVER_PLAN=1   or `runner --deliver-plan`: run `<bin> deliver <node> --plan`
                         instead; its answer is recorded as outcome "plan", nothing is sent.
  config deliver.remote_shell   how ssh runs cc-tree on the host (default "zsh -lc": a login
                         shell, where ~/.dotfiles/bin and claude are on PATH)
  config deliver.remote_bin     cc-tree on the host (default "cc-tree")
  PLAUD_DELIVER_SSH      the ssh binary (default "ssh")
"""

import os
import re
import shlex
import socket
import subprocess
from pathlib import Path

DEFAULT_BIN = "cc-tree"
DEFAULT_TIMEOUT = 180
DEFAULT_ASK = ("read the new transcript(s) and carry out what they ask for; "
               "flag anything that needs a decision from Tomas.")
LATER_LINE = "No action needed yet — it is here when you need it."

_RELEASE_RE = re.compile(r"--release\s+([0-9A-Za-z]+)")


class DeliverError(RuntimeError):
    """cc-tree is missing, timed out or exited non-zero; fails only the deliver step."""


def _cfg(config):
    return (config or {}).get("deliver") or {}


def bin_path(config=None):
    return os.environ.get("CC_TREE_BIN") or _cfg(config).get("bin") or DEFAULT_BIN


def is_remote(host):
    """True when host names another machine (not empty, "local" or this machine's name)."""
    if not host or host in ("local", "localhost"):
        return False
    me = socket.gethostname().lower()
    return host.lower() not in (me, me.split(".")[0])


def remote_argv(host, args, config=None):
    """ssh argv that runs `<remote_bin> <args>` on host in its login shell."""
    cfg = _cfg(config)
    shell = shlex.split(cfg.get("remote_shell") or "zsh -lc")
    inner = " ".join(shlex.quote(a) for a in [cfg.get("remote_bin") or "cc-tree", *args])
    ssh = os.environ.get("PLAUD_DELIVER_SSH") or "ssh"
    return [ssh, "-o", "BatchMode=yes", "-o", "ConnectTimeout=15", host,
            " ".join(shlex.quote(p) for p in [*shell, inner])]


def timeout_s(config=None):
    raw = os.environ.get("PLAUD_DELIVER_TIMEOUT") or _cfg(config).get("timeout")
    try:
        return max(1, int(raw))
    except (TypeError, ValueError):
        return DEFAULT_TIMEOUT


def plan_mode(ctx=None):
    return bool(getattr(ctx, "deliver_plan", False)) or os.environ.get("PLAUD_DELIVER_PLAN") == "1"


# ---------------------------------------------------------------- the work text

def _duration(ms):
    secs = int(ms or 0) // 1000
    h, m = secs // 3600, (secs % 3600) // 60
    return f"{h}h {m}m" if h else f"{m}m"


def _short(sha):
    return str(sha)[:10]


def _results(job):
    return [r for r in (job.get("result") or {}).get("recordings") or [] if r.get("output_dir")]


def _locate(res, roots):
    """(repo root or None, output dir relative to it, or the dir itself)."""
    out = Path(res["output_dir"]).resolve()
    for root in roots:
        try:
            return Path(root), out.relative_to(Path(root).resolve())
        except ValueError:
            continue
    return None, out


def _pr_for(pr, root):
    if isinstance(pr, dict):
        return pr.get(str(root)) or next(iter(pr.values()), None)
    return pr


def build_work_text(job):
    """The work text of Part D for every transcript in the job (D1)."""
    out = job.get("result") or {}
    deliver = job.get("deliver") or {}
    repos = out.get("repos") or {}
    results = _results(job)
    blocks = []
    for res in results:
        root, rel = _locate(res, list(repos))
        repo = root.name if root else (rel.parent.name or str(rel))
        title = res.get("title") or res.get("name") or "untitled"
        if res.get("files"):
            files = ", ".join(str(rel / f) for f in res["files"])
        else:
            files = f"{rel}/"
        lines = [f"New transcript: {title} ({res.get('date')}, {_duration(res.get('duration'))}) — {repo}",
                 f"Files: {files}"]
        shas = repos.get(str(root)) if root else None
        if not shas and not repos:
            shas = out.get("commits")
        if shas:
            mine = [r for r in results if _locate(r, list(repos))[0] == root]
            if len(shas) == len(mine):
                shas = [shas[mine.index(res)]]
            commit = f"Commit: {', '.join(_short(s) for s in shas)}"
            pr = _pr_for(out.get("pr"), root)
            if pr:
                commit += f"  PR: {pr}"
            lines.append(commit)
        blocks.append("\n".join(lines))
    if is_remote(deliver.get("host")):
        blocks.append("Pushed to origin from the Mac: pull (or fetch the PR branch) before reading.")
    if deliver.get("mode") == "later":
        tail = LATER_LINE
    else:
        tail = "Process it now: " + (deliver.get("note") or deliver.get("ask") or DEFAULT_ASK)
    return "\n\n".join(blocks) + "\n\n" + tail + "\n"


# ---------------------------------------------------------------- running cc-tree

def parse_outcome(stdout):
    """(outcome, held id) from cc-tree deliver's stdout; see the module docstring."""
    text = stdout or ""
    if "held for Tomas" in text:
        m = _RELEASE_RE.search(text)
        return "held", (m.group(1) if m else None)
    if re.search(r"^\S+: (no session: a fresh one started|fresh session started|checkpoint requested)",
                 text, re.M):
        return "fresh", None
    if re.search(r"^\S+: messaged\b", text, re.M) or re.search(r"^delivered \d+/\d+", text, re.M):
        return "message", None
    return "unknown", None


def run_cc_tree(args, text=None, config=None, run=subprocess.run, host=None):
    bin_ = bin_path(config)
    argv = remote_argv(host, args, config) if is_remote(host) else [bin_, *args]
    where = f" on {host}" if is_remote(host) else ""
    try:
        proc = run(argv, input=text, capture_output=True, text=True,
                   timeout=timeout_s(config))
    except FileNotFoundError:
        if where:
            raise DeliverError(f"ssh not found ({argv[0]!r}); `jobs retry` re-delivers")
        raise DeliverError(f"cc-tree not found ({bin_!r}): install it or set CC_TREE_BIN / "
                           "config deliver.bin; the transcript is pushed, `jobs retry` re-delivers")
    except subprocess.TimeoutExpired:
        raise DeliverError(f"cc-tree deliver timed out after {timeout_s(config)}s")
    except OSError as e:
        raise DeliverError(f"cc-tree could not run: {type(e).__name__}: {e}"[:500])
    if proc.returncode != 0:
        msg = (proc.stderr or proc.stdout or "").strip() or "no output"
        if where and proc.returncode == 255:
            raise DeliverError(f"ssh to {host} failed: {msg}; `jobs retry` re-delivers"[:500])
        raise DeliverError(f"cc-tree deliver{where} exited {proc.returncode}: {msg}"[:500])
    return proc.stdout or ""


def deliver(job, text, config=None, plan=False, run=subprocess.run):
    """Hand the work over (or ask what would happen); returns the result.delivered dict."""
    node, host = job["deliver"]["node"], job["deliver"].get("host")
    extra = {"host": host} if is_remote(host) else {}
    if plan:
        stdout = run_cc_tree(["deliver", node, "--plan"], None, config, run, host)
        return {"outcome": "plan", "id": None, "raw": stdout.strip()[:500], **extra}
    stdout = run_cc_tree(["deliver", node, "-"], text, config, run, host)
    outcome, held_id = parse_outcome(stdout)
    return {"outcome": outcome, "id": held_id, "raw": stdout.strip()[:500], **extra}


def step_deliver(job, ctx, checkpoint=None):
    """Runner step. Skipped without a node or with mode none. A held delivery
    returns "held" (not "done"), so `jobs retry` runs this step again and only it."""
    d = job.get("deliver") or {}
    if d.get("mode", "none") == "none" or not d.get("node") or not _results(job):
        return "skipped"
    try:
        config = ctx.host.load_config(validate=False)
    except (AttributeError, OSError, ValueError):
        config = {}
    host = d.get("host")
    if is_remote(host) and job.get("push") == "none":
        raise DeliverError(f"hand-off on {host} needs the transcript on origin, but this job "
                           "pushes nothing: set push main|pr for the project (or --push) and re-plan")
    plan = plan_mode(ctx)
    delivered = deliver(job, build_work_text(job), config, plan)
    job.setdefault("result", {})["delivered"] = delivered
    return "held" if delivered["outcome"] == "held" else "done"
