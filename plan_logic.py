"""Logic behind the plan screen, with no UI import (plan_app.py is the Textual layer).

  pending_recordings(...)  the pending list, same definition as the interactive CLI
  live_meeting(config)     the Wispr meeting being recorded now, as a first row
  suggest_project(...)     the project earlier, similarly titled recordings went to
  list_nodes(tree)         the nodes of a cc-tree tree, for a node picker
  load_plan_data(...)      everything a plan form shows (config, state, rows, specs, notes)
  armable(row) / live_selected(rows, ids)   which row can be armed, and the one that is
  host_options(...)        the hand-off hosts to offer: this Mac, config deliver.hosts, the project's
  resolve_delivery(...)    push / node / host / mode / ask the job will get (labels show these)
  parse_node_ref / fuzzy_filter   a pasted cc-tree ref; the pickers' filter
  build_job(fields)        the ONE place plan fields become a job; `enqueue` uses it too
  job_summary(job, ...)    the one-line "what -> where -> who" shown before saving

This module is the plan form without the form: plan_app.py (Textual) draws it and calls these,
and any other surface (a web page, a menu-bar form, a chat bot) can do the same. Everything
here is safe to call from a UI: config, state, network, Wispr and cc-tree failures come back
as an empty result plus a note, never an exception (build_job excepted: it raises
jobs.JobError with a message to show next to the form as it is).

Recipes
  suggestions:  SUGGEST_THRESHOLD (0.5) is the minimum Dice token overlap between the
                title and a past title of a project; lower it to be bolder.
  cc-tree:      CC_TREE_BIN (or config deliver.bin) names the binary; only
                `cc-tree show <tree>` is ever run from here (read-only).
"""

import re
import subprocess
import unicodedata

import deliver
import jobs
import sources

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


# --- loading everything the form shows ---

class OfflineHost:
    """Delegates to the real module, but the Plaud list fails at once instead of hitting the
    network when there is no token."""

    def __init__(self, host):
        self._host = host

    def __getattr__(self, name):
        return getattr(self._host, name)

    def list_recordings(self, *a, **k):
        raise RuntimeError("no Plaud token")


def load_plan_data(host=None, days=30, arm_live=False):
    """Everything a plan form shows, in one call that never raises.

    Returns {config, state, rows, folders, specs, notes}: rows are the pending recordings with
    the live Wispr meeting first (live=True); notes are the lines to show above the list, why
    the list may be short (no token, Plaud offline, bad config), "No pending recordings.", and
    with arm_live (the user asked to arm the current meeting) a note when none is live.
    Without a Plaud token the network is never tried; local sources still list."""
    host, notes = _host(host), []
    try:
        config = host.load_config()
    except Exception as e:  # noqa: BLE001  ConfigError, bad JSON, unreadable file
        config = {}
        notes.append(f"config not usable ({type(e).__name__}: {e}); no projects offered")
    try:
        state = host.load_state()
    except Exception as e:  # noqa: BLE001
        state = {"downloaded": {}, "transcribed": {}}
        notes.append(f"state not readable ({type(e).__name__}: {e})")
    token, source_host = None, host
    try:
        token = host.get_token()
    except SystemExit:
        notes.append("No Plaud token (PLAUD_BEARER_TOKEN); showing local recordings only")
        source_host = OfflineHost(host)
    pending = pending_recordings(token, config, state, days, host=source_host)
    notes += ([n for n in pending.notes if "no Plaud token" not in n] if token is None
              else pending.notes)
    folders = {}
    if token is not None:
        try:
            folders = host.fetch_folders(token, config)
        except (Exception, SystemExit):  # noqa: BLE001  folders are decoration
            pass
    live = live_meeting(config, host=host)
    try:
        specs = host.project_specs(config)
    except Exception:  # noqa: BLE001
        specs = {}
    rows = with_live_first(pending, live)
    if arm_live and not any(r.get("live") for r in rows):
        notes.append("No Wispr meeting is live right now, so there is nothing to arm: "
                     "this is a normal plan.")
    if not rows:
        notes.append("No pending recordings.")
    return dict(config=config, state=state, rows=rows, folders=folders, specs=specs, notes=notes)


# --- pending recordings ---

class Pending(list):
    """The pending recordings (a list) plus `.notes`: why the list may be incomplete."""

    def __init__(self, items=(), notes=()):
        super().__init__(items)
        self.notes = list(notes)


def _list_sources(token, config, days, host, notes):
    """The same listing as `list` (sources.list_recordings), but a failing source
    (Plaud offline, an expired token, which exits the process) costs only its rows
    and leaves a note."""
    return sources.list_recordings(host, config, days, token, notes=notes)


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


def armable(row):
    """Only a live Wispr meeting can be armed (E4): Plaud, Recorder and call rows have no known
    upload time, so they never are, whatever their flags say."""
    return bool(row and row.get("live") and row.get("source") == "wispr"
                and str(row.get("id", "")).startswith("wispr:"))


def live_selected(rows, ids):
    """The selected row that can be armed, or None."""
    return next((r for r in rows if r.get("id") in ids and armable(r)), None)


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

def parse_node_ref(text):
    """A pasted cc-tree node ref, "tree:id" or "tree:#id" (as cc-tree prints it), -> "tree:id";
    anything else -> None, so the query just filters."""
    m = re.fullmatch(r"\s*([\w.-]+):#?([0-9a-z]{3,})\s*", text or "")
    return f"{m.group(1)}:{m.group(2)}" if m else None


def _fold(text):
    """Lower-cased, diacritics removed, so "hriste" finds "Hřiště"."""
    return "".join(c for c in unicodedata.normalize("NFKD", str(text).lower())
                   if not unicodedata.combining(c))


def fuzzy_filter(query, items):
    """items: [(label, value)] whose label holds every word of the query (diacritics ignored)."""
    words = _fold(query).split()
    return [(label, value) for label, value in items if all(w in _fold(label) for w in words)]


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


def parse_trees(text):
    """`cc-tree trees` lines ("slug<TAB>title<TAB>N open") -> [{tree, title}]; junk skipped."""
    trees = []
    for line in (text or "").splitlines():
        parts = line.split("\t")
        slug = parts[0].strip()
        if not slug or not re.fullmatch(r"[\w.-]+", slug):
            continue
        trees.append({"tree": slug, "title": parts[1].strip() if len(parts) > 1 else slug})
    return trees


def list_trees(config=None):
    """Every cc-tree tree, for picking a node when the project names none; [] on any failure.
    Runs only the read-only `cc-tree trees`."""
    try:
        out = subprocess.run([deliver.bin_path(config), "trees"], capture_output=True,
                             text=True, timeout=NODES_TIMEOUT_S, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError):
        return []
    if out.returncode != 0:
        return []
    return parse_trees(out.stdout)


# --- the one job builder ---

FIELDS = ("recordings", "merge", "cuts", "instructions", "project", "push", "node", "host", "mode",
          "note", "arm", "arm_title")


def host_options(config, specs, project):
    """Hosts a hand-off can run on, in the order a form cycles them: "" (this Mac), then each
    config deliver.hosts entry, then the project's own host if the config does not list it.
    Only "" means no other host is configured."""
    hosts = [""] + [h for h in ((config or {}).get("deliver") or {}).get("hosts") or []
                    if isinstance(h, str) and h]
    own = ((specs or {}).get(project) or {}).get("host") if project else None
    if own and own not in hosts:
        hosts.append(own)
    return hosts


def resolve_delivery(fields, spec=None):
    """push / node / host / mode / ask a job with these fields gets, given the project's spec
    (None: no project). Used by build_job and by forms to label what the job will do.

    Explicit fields beat the project: node "" = no hand-off even though the project names a
    node; host "" = this Mac even though the project names a host (host None = the project's,
    which is None for this Mac); mode defaults to "now" when a node is known, else "none"."""
    spec = spec or {}
    push = fields.get("push") or spec.get("push")
    node = spec.get("node")
    if fields.get("node") is not None:
        node = fields["node"] or None
    host_ = spec.get("host")
    if fields.get("host") is not None:
        host_ = fields["host"]
    mode = fields.get("mode") or ("now" if node else "none")
    return {"push": push, "node": node, "host": host_, "mode": mode, "ask": spec.get("deliver_ask")}


def _check_selection(fields, cuts):
    """The rules a form would otherwise repeat, with the message to show next to it."""
    recs = list(fields.get("recordings") or [])
    if fields.get("arm"):
        if recs:
            raise jobs.JobError("The live meeting can only be armed on its own: unselect the other "
                                "recordings (a job cannot mix a running meeting with finished ones)")
        if fields.get("merge") or any(cuts.values()):
            raise jobs.JobError("An armed job cannot be merged or cut: the meeting has no "
                                "transcript yet, so turn merge off / clear the cut first")
        return
    if not recs:
        raise jobs.JobError("Select at least one recording to transcribe (enqueue: --recording ID)")
    if fields.get("merge") and len(recs) < 2:
        raise jobs.JobError("merge needs two or more selected recordings")


def build_job(fields, host=None, config=None):
    """Turn plan/enqueue fields into a validated job dict; raises jobs.JobError.

    fields: recordings [ids], merge bool, cuts {rec: ["29:16-34:55"]}, instructions,
    project, push, node, host, mode, note (all optional but recordings), arm (a Wispr meeting
    uuid: the job waits for it instead of naming recordings) with arm_title (shown in the
    menu while it waits). A project fills push / node / host / deliver.ask from the config;
    explicit push / node / host win (node "" = no hand-off, host "" = this Mac; see
    resolve_delivery). The JobError text is meant to be shown to the user as it is."""
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
    _check_selection(fields, cuts)

    project = spec = None
    if fields.get("project"):
        try:
            specs = host.project_specs(config if config is not None else host.load_config())
        except host.ConfigError as e:
            raise jobs.JobError(f"config error: {e}")
        if fields["project"] not in specs:
            raise jobs.JobError(f"unknown project {fields['project']!r}; configured: "
                                + (", ".join(sorted(specs)) or "(none)"))
        project, spec = fields["project"], specs[fields["project"]]
    d = resolve_delivery(fields, spec)
    extra = {}
    if fields.get("arm"):
        trigger = {"wispr": fields["arm"], "armed_at": jobs.now_iso()}
        if fields.get("arm_title"):
            trigger["title"] = str(fields["arm_title"])
        extra = {"state": "armed", "trigger": trigger}

    job = jobs.new_job(**extra,
        recordings=list(fields.get("recordings") or []), merge=bool(fields.get("merge")),
        cuts=cuts, instructions=fields.get("instructions") or None, project=project, push=d["push"],
        deliver={"node": d["node"], "mode": d["mode"], "note": fields.get("note") or None,
                 "ask": d["ask"], "host": d["host"]})
    for rid in job["recordings"]:
        try:
            sources.source_for_id(rid)
        except sources.UnknownSource as e:
            raise jobs.JobError(str(e)) from None
    return jobs.validate_job(job)


def job_summary(job, rows=(), specs=None):
    """One line before saving: what will be transcribed -> where it goes -> who gets it.
    rows (the listed recordings) give titles; specs give the project's push when the job
    does not set one."""
    titles = {r["id"]: r.get("filename", r["id"]) for r in rows}
    trig = job.get("trigger")
    what = (f"arm: runs when '{trig.get('title') or 'the meeting'}' ends" if trig
            else " + ".join(titles.get(r, r) for r in job["recordings"]))
    spec = (specs or {}).get(job["project"]) if job["project"] else None
    where = (f"{job['project']} (push {job['push'] or (spec or {}).get('push') or 'none'})"
             if job["project"] else "classifier-routed project (push set after routing)")
    d = job["deliver"]
    node = d["node"] or "no hand-off"
    if d["node"] and d.get("host"):
        node += f" on {d['host']}"
    return f"{what} \u2192 {where} \u2192 {node}, {d['mode']}"
