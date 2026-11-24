"""Logic behind the plan screen, with no UI import (plan_app.py is the Textual layer).

  pending_recordings(...)  the pending list, same definition as the interactive CLI
  live_meeting(config)     the Wispr meeting being recorded now, as a first row
  suggest_project(...)     the project earlier, similarly titled recordings went to
  list_nodes(tree)         the nodes of a cc-tree tree, for a node picker
  build_job(fields)        the ONE place plan fields become a job; `enqueue` uses it too

Everything here is safe to call from a UI: network, Wispr and cc-tree failures
come back as an empty result plus a note, never an exception (build_job excepted:
it raises jobs.JobError with a message to show next to the form).

Recipes
  suggestions:  SUGGEST_THRESHOLD (0.5) is the minimum Dice token overlap between the
                title and a past title of a project; lower it to be bolder.
  cc-tree:      CC_TREE_BIN (or config deliver.bin) names the binary; only
                `cc-tree show <tree>` is ever run from here (read-only).
"""

import os
import re
import subprocess

import deliver
import jobs

SUGGEST_THRESHOLD = 0.5   # Dice overlap of title tokens; below it there is no suggestion
SUGGEST_HISTORY = 10      # past titles per project taken into account
NODES_TIMEOUT_S = 15

_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")
_HEADER_RE = re.compile(r"^#\s+.+?\s+\[(?P<slug>[^\]\s]+)\]\s*$")
# "  - [x] title  #4gfd  ◆ note": checkbox optional (top-level nodes have none),
# "  #<id>" always follows the title, "◆ ..." is the node's note.
_ROW_RE = re.compile(r"^(?P<indent>\s*)- (?:\[(?P<box>[ xX])\] )?(?P<title>.+?)  #(?P<id>[0-9a-z]{3,8})(?=\s|$)")


def _host(host):
    if host is None:
        import plaud_transcribe as host
    return host


# --- pending recordings ---

class Pending(list):
    """The pending recordings (a list) plus `.notes`: why the list may be incomplete."""

    def __init__(self, items=(), notes=()):
        super().__init__(items)
        self.notes = list(notes)


def _list_sources(token, config, days, host, notes):
    """Like host.list_all_recordings, but a Plaud failure (offline, expired token, which
    exits the process) costs only the Plaud rows and leaves a note."""
    recs = []
    try:
        recs += host.list_recordings(token, config, days)
    except (Exception, SystemExit) as e:   # plaud_get exits on a 401
        notes.append(f"Plaud not reachable ({type(e).__name__}: {e}); showing local recordings only")
    for label, fn in (("Wispr", host.wispr_flow.list_meetings),
                      ("Recorder", host.google_recorder.list_recordings)):
        try:
            recs += fn(config, days)
        except Exception as e:  # noqa: BLE001  best-effort sources
            notes.append(f"{label} not readable ({type(e).__name__}: {e})")
    recs.sort(key=lambda r: r.get("start_time", 0), reverse=True)
    return recs


def pending_recordings(token, config, state, days, host=None):
    """Recordings still to do: never seen, or transcribed but not finished.

    Same definition as the interactive `process` list (host.split_pending), with
    recordings under 10 s left out. Unlike cmd_process this does NOT write them
    into state.json as skipped: the plan screen writes nothing. Never raises."""
    host = _host(host)
    notes = []
    recs = _list_sources(token, config, days, host, notes)
    short = {r["id"] for r in host.short_unseen(recs, state)}
    pending, _ = host.split_pending([r for r in recs if r["id"] not in short], state)
    return Pending(pending, notes)


def live_meeting(config, host=None):
    """The Wispr meeting being recorded now as a recording-shaped row with
    live=True, or None. Listed first by the screen. Never raises."""
    host = _host(host)
    try:
        import autocapture
        if not host.wispr_flow.wispr_enabled(config):
            return None
        found = autocapture.wispr_live_meeting(config, autocapture.settings(config)["max_hours"])
        if not found:
            return None
        title = "Meeting in progress"
        try:
            conn = host.wispr_flow._connect(config)
            try:
                row = conn.execute("SELECT title FROM Meetings WHERE id = ?", (found["id"],)).fetchone()
            finally:
                conn.close()
            if row and row[0]:
                title = row[0]
        except Exception:  # noqa: BLE001  a missing title must not hide the meeting
            pass
        return {"id": f"wispr:{found['id']}", "source": "wispr", "filename": title,
                "start_time": found["created_ms"], "duration": 0, "live": True,
                "filetag_id_list": []}
    except Exception:  # noqa: BLE001
        return None


def with_live_first(pending, live):
    """Rows for the screen: the live meeting (if any, once) ahead of the pending list."""
    rows = [r for r in pending if not (live and r.get("id") == live["id"])]
    return ([live] if live else []) + rows


# --- project suggestion ---

def _tokens(text, host):
    return {t for t in host.slugify(text or "").split("-") if len(t) >= 3 and not t.isdigit()}


def _dice(a, b):
    return 2 * len(a & b) / (len(a) + len(b)) if a and b else 0.0


def suggest_project(title, state, projects, host=None):
    """The configured project whose past recordings are titled most like `title`.

    Score = best Dice overlap (words of 3+ letters, diacritics ignored, dates and
    numbers dropped) between the title and the project's name or any of its last
    SUGGEST_HISTORY done recordings (host.recent_examples). Returns None when the
    best score is under SUGGEST_THRESHOLD or two projects tie: a wrong guess that
    looks confident is worse than an empty picker. Deterministic."""
    host = _host(host)
    mine = _tokens(title, host)
    if not mine:
        return None
    history = host.recent_examples(state or {}, limit=SUGGEST_HISTORY)
    scores = {}
    for name in projects:
        best = _dice(mine, _tokens(name, host))
        for past in history.get(name, []):
            best = max(best, _dice(mine, _tokens(past, host)))
        scores[name] = best
    if not scores:
        return None
    top = max(scores.values())
    winners = [n for n, s in scores.items() if s == top]
    if top < SUGGEST_THRESHOLD or len(winners) != 1:
        return None
    return winners[0]


# --- cc-tree nodes ---

def parse_nodes(text, tree):
    """Rows of `cc-tree show <tree>` as [{ref, title, state, depth}].

    ref is "<tree>:<id>", ready to use as a node. state is "done" for [x], "open"
    otherwise. Lines that are not node rows (headers, workspace notes) are skipped."""
    tree = (tree or "").split(":")[0]
    nodes = []
    slug = tree
    for line in _ANSI_RE.sub("", text or "").splitlines():
        h = _HEADER_RE.match(line)
        if h:
            slug = tree or h.group("slug")
            continue
        m = _ROW_RE.match(line)
        if not m:
            continue
        nodes.append({"ref": f"{slug}:{m.group('id')}", "title": m.group("title").strip(),
                      "state": "done" if (m.group("box") or "").lower() == "x" else "open",
                      "depth": len(m.group("indent").replace("\t", "  ")) // 2})
    return nodes


def list_nodes(tree, config=None):
    """Nodes of a cc-tree tree for the picker; [] on any failure (no picker, no error)."""
    tree = (tree or "").split(":")[0].strip()
    if not tree:
        return []
    try:
        out = subprocess.run([deliver.bin_path(config), "show", tree], capture_output=True,
                             text=True, timeout=NODES_TIMEOUT_S, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError):
        return []
    if out.returncode != 0:
        return []
    try:
        return parse_nodes(out.stdout, tree)
    except Exception:  # noqa: BLE001
        return []


# --- the one job builder ---

FIELDS = ("recordings", "merge", "cuts", "instructions", "project", "push", "node", "mode", "note", "arm")


def build_job(fields, host=None, config=None):
    """Turn plan/enqueue fields into a validated job dict; raises jobs.JobError.

    fields: recordings [ids], merge bool, cuts {rec: ["29:16-34:55"]}, instructions,
    project, push, node, mode, note (all optional but recordings). A project fills
    push / node / deliver.ask from the config; explicit push/node win; mode defaults
    to "now" when a node is known, else "none"."""
    host = _host(host)
    unknown = set(fields) - set(FIELDS)
    if unknown:
        raise jobs.JobError(f"unknown job field(s): {', '.join(sorted(unknown))}")
    cuts = {}
    for rec, spans in (fields.get("cuts") or {}).items():
        for span in spans:
            try:
                host.parse_cut(span)
            except ValueError as e:
                raise jobs.JobError(str(e))
        cuts[rec] = list(spans)

    project = push = node = ask = None
    if fields.get("project"):
        try:
            specs = host.project_specs(config if config is not None else host.load_config())
        except host.ConfigError as e:
            raise jobs.JobError(f"config error: {e}")
        if fields["project"] not in specs:
            raise jobs.JobError(f"unknown project {fields['project']!r}; configured: "
                                + (", ".join(sorted(specs)) or "(none)"))
        spec = specs[fields["project"]]
        project, push, node, ask = fields["project"], spec["push"], spec["node"], spec["deliver_ask"]
    push = fields.get("push") or push
    node = fields.get("node") or node
    mode = fields.get("mode") or ("now" if node else "none")
    extra = {}
    if fields.get("arm"):
        if fields.get("recordings") or fields.get("merge") or cuts:
            raise jobs.JobError("an armed job waits for a meeting that is still running: "
                                "it cannot also name recordings, --merge or --cut")
        extra = {"state": "armed", "trigger": {"wispr": fields["arm"], "armed_at": jobs.now_iso()}}

    job = jobs.new_job(**extra,
        recordings=list(fields.get("recordings") or []), merge=bool(fields.get("merge")),
        cuts=cuts, instructions=fields.get("instructions") or None, project=project, push=push,
        deliver={"node": node, "mode": mode, "note": fields.get("note") or None, "ask": ask})
    return jobs.validate_job(job)
