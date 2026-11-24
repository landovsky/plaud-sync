"""The plan screen: pick recordings, a project, a node and a note, then save ONE job file.

`plaud-transcribe plan` runs this (Textual; see requirements-plan.txt). The screen never
transcribes anything: its only write is jobs.save_job() on Enter, and quitting writes nothing
(spec G2). The runner picks the job up. Every field goes through plan_logic.build_job, the same
builder `enqueue` uses, so a job from here equals one from `enqueue` with the same values (G3).

Keys (the footer shows them too)
  Enter  show a one-line summary; Enter again saves the job and exits, printing its id   (inside a text area Enter is a newline;
         use ctrl+s there)
  Esc    quit, nothing written
  Space  select / unselect the recording under the cursor      Tab / shift+Tab  move on
  m      merge the selected recordings into one transcript     c  cut a span out of the
         (not combinable with cuts)                               highlighted recording
  p      pick the project (fuzzy)       n  pick the node (fuzzy)       x  clear the node
  a      arm: select only the highlighted row, to run its job when the meeting ends (the live
         Wispr meeting only; saving with it selected arms the job, it cannot be combined with
         other recordings, m or c)
  Mode (now / later / none) is the radio set: arrows move, Space chooses.

Fastest path (one recording, defaults accepted): launch, wait for the list, Enter, Enter. The first
row (the live meeting when there is one) is already selected, the project is the suggestion for
its title, the node and mode follow the project.

Recipes
  more days of recordings: plaud-transcribe plan --days 60
  plan it during the meeting: `plaud-transcribe plan --arm-live` (the menu's "Arm current meeting...")
  a recording of another source (Wispr/phone) shows in the same list; the live Wispr meeting is
  first and marked "LIVE".
"""

import unicodedata

from textual import work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import (Footer, Input, Label, LoadingIndicator, OptionList, RadioButton,
                             RadioSet, SelectionList, Static, TextArea)
from textual.widgets.option_list import Option
from textual.widgets.selection_list import Selection

import jobs
import plan_logic

MODES = ("now", "later", "none")
NONE_CHOICE = ""           # picker value for "no project" / "clear"


def _fold(text):
    """Lower-cased, diacritics removed, so "hriste" finds "Hřiště"."""
    return "".join(c for c in unicodedata.normalize("NFKD", str(text).lower())
                   if not unicodedata.combining(c))


def fuzzy_filter(query, items):
    """items: [(label, value)] whose label holds every word of the query (diacritics ignored)."""
    words = _fold(query).split()
    return [(label, value) for label, value in items if all(w in _fold(label) for w in words)]


class _OfflineHost:
    """Delegates to the real module, but the Plaud list fails at once instead of hitting the
    network when there is no token."""

    def __init__(self, host):
        self._host = host

    def __getattr__(self, name):
        return getattr(self._host, name)

    def list_recordings(self, *a, **k):
        raise RuntimeError("no Plaud token")


class PickerScreen(ModalScreen):
    """A fuzzy picker: type to filter, Up/Down to move, Enter picks, Esc cancels (None)."""

    BINDINGS = [Binding("escape", "cancel", "Cancel"),
                Binding("down", "cursor(1)", show=False), Binding("up", "cursor(-1)", show=False)]
    DEFAULT_CSS = """
    PickerScreen { align: center middle; }
    PickerScreen > Vertical { width: 80%; height: 70%; border: round $accent; background: $surface; padding: 0 1; }
    PickerScreen OptionList { height: 1fr; }
    """

    def __init__(self, title, items):
        super().__init__()
        self._title, self._items = title, list(items)

    def compose(self):
        with Vertical():
            yield Label(self._title)
            yield Input(placeholder="type to filter", id="picker-query")
            yield OptionList(id="picker-list")

    def on_mount(self):
        self._refill("")
        self.query_one(Input).focus()

    def _refill(self, query):
        ol = self.query_one(OptionList)
        ol.clear_options()
        self._shown = fuzzy_filter(query, self._items)
        ol.add_options([Option(label) for label, _ in self._shown])
        if self._shown:
            ol.highlighted = 0

    def on_input_changed(self, event):
        self._refill(event.value)

    def on_input_submitted(self, event):
        ol = self.query_one(OptionList)
        if ol.highlighted is not None and self._shown:
            self.dismiss(self._shown[ol.highlighted][1])

    def on_option_list_option_selected(self, event):
        self.dismiss(self._shown[event.option_index][1])

    def action_cursor(self, step):
        ol = self.query_one(OptionList)
        (ol.action_cursor_down if step > 0 else ol.action_cursor_up)()

    def action_cancel(self):
        self.dismiss(None)


class AskScreen(ModalScreen):
    """One line of text; Enter returns it, Esc returns None."""

    BINDINGS = [Binding("escape", "cancel", "Cancel")]
    DEFAULT_CSS = """
    AskScreen { align: center middle; }
    AskScreen > Vertical { width: 60; height: auto; border: round $accent; background: $surface; padding: 0 1; }
    """

    def __init__(self, title, placeholder=""):
        super().__init__()
        self._title, self._placeholder = title, placeholder

    def compose(self):
        with Vertical():
            yield Label(self._title)
            yield Input(placeholder=self._placeholder, id="ask-input")

    def on_input_submitted(self, event):
        self.dismiss(event.value.strip())

    def action_cancel(self):
        self.dismiss(None)


class ConfirmScreen(ModalScreen):
    """The last look before a job (which may spend money and push to main) is written."""

    BINDINGS = [Binding("enter", "yes", "Save"), Binding("escape", "no", "Back")]
    DEFAULT_CSS = """
    ConfirmScreen { align: center middle; }
    ConfirmScreen > Vertical { width: 90%; height: auto; border: round $accent; background: $surface; padding: 1 2; }
    """

    def __init__(self, summary):
        super().__init__()
        self._summary = summary

    def compose(self):
        with Vertical():
            yield Static(self._summary, id="confirm-summary")
            yield Static("Enter to save, Esc to go back")

    def action_yes(self):
        self.dismiss(True)

    def action_no(self):
        self.dismiss(False)


class PlanApp(App):
    """One keyboard-first screen with the five sections of spec part G."""

    TITLE = "plaud-transcribe plan"
    CSS = """
    #notes { color: $warning; }
    #recs { height: 10; }
    #error { color: $error; }
    TextArea { height: 5; }
    LoadingIndicator { height: 1; }
    """
    BINDINGS = [
        Binding("enter", "save", "Save job", priority=True),
        Binding("ctrl+s", "save_now", "Save job", show=False, priority=True),
        Binding("escape", "quit_form", "Quit (writes nothing)", priority=True),
        Binding("m", "merge", "Merge"),
        Binding("c", "cut", "Cut"),
        Binding("p", "pick_project", "Project"),
        Binding("n", "pick_node", "Node"),
        Binding("x", "clear_node", "Clear node"),
        Binding("a", "arm", "Arm"),
    ]

    def __init__(self, host=None, days=30, arm_live=False):
        super().__init__()
        if host is None:
            import plaud_transcribe as host
        self.host = host
        self.days = days
        self.arm_live = arm_live
        self.config, self.state, self.folders = {}, {}, {}
        self.specs = {}            # project name -> {push, node, deliver_ask, ...}
        self.rows = []             # recording dicts, in list order
        self.loaded = False
        self.merge = False
        self.cuts = {}             # recording id -> [spans]
        self.project = None
        self.project_touched = False
        self.node_override = None  # None = follow the project; "" = cleared; else a ref
        self.mode_choice = None    # None = default (now when a node is known, else none)
        self.nodes_cache = {}
        self._discard_armed = False

    # --- layout ---

    def compose(self) -> ComposeResult:
        with VerticalScroll():
            yield Static("", id="notes")
            yield Label("1. Recordings  (Space select, m merge, c cut)")
            yield LoadingIndicator(id="loading")
            yield SelectionList(id="recs")
            yield Static("", id="project")
            yield Static("", id="node")
            yield Label("4. Mode")
            with RadioSet(id="mode"):
                for m in MODES:
                    yield RadioButton(m, id=f"mode-{m}", value=(m == "none"))
            yield Label("Note to the node (what to do with the transcript)")
            yield TextArea(id="note")
            yield Label("Instructions to the summarizer")
            yield TextArea(id="instructions")
            yield Static("", id="error")
        yield Footer()

    def on_mount(self):
        self._refresh_labels()
        self.load()

    # --- loading (a worker thread: the network must not block the UI) ---

    @work(thread=True, exclusive=True)
    def load(self):
        data = self._gather()
        self.call_from_thread(self._loaded, data)

    def _gather(self):
        host, notes = self.host, []
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
            source_host = _OfflineHost(host)
        pending = plan_logic.pending_recordings(token, config, state, self.days, host=source_host)
        notes += ([n for n in pending.notes if "no Plaud token" not in n] if token is None
                  else pending.notes)
        folders = {}
        if token is not None:
            try:
                folders = host.fetch_folders(token, config)
            except (Exception, SystemExit):  # noqa: BLE001  folders are decoration
                pass
        live = plan_logic.live_meeting(config, host=host)
        try:
            specs = host.project_specs(config)
        except Exception:  # noqa: BLE001
            specs = {}
        return dict(config=config, state=state, rows=plan_logic.with_live_first(pending, live),
                    folders=folders, specs=specs, notes=notes)

    def _loaded(self, data):
        self.config, self.state, self.folders = data["config"], data["state"], data["folders"]
        self.specs, self.rows = data["specs"], data["rows"]
        recs = self.query_one("#recs", SelectionList)
        recs.clear_options()
        recs.add_options([Selection(self._row_label(r), r["id"], False) for r in self.rows])
        if self.rows:
            recs.select(self.rows[0]["id"])   # G1: the common case needs no extra keystroke
            recs.highlighted = 0
        self.query_one("#loading").display = False
        notes = list(data["notes"])
        if self.arm_live and not any(r.get("live") for r in self.rows):
            notes.append("No Wispr meeting is live right now, so there is nothing to arm: "
                         "this is a normal plan.")
        if not self.rows:
            notes.append("No pending recordings.")
        self.query_one("#notes", Static).update("\n".join(notes))
        self.loaded = True
        recs.focus()
        self._suggest()
        self._refresh_labels()

    def _row_label(self, r):
        h = self.host
        if r.get("live"):
            return f"● LIVE   {r.get('filename', 'Meeting in progress')}  (Wispr, now)"
        when = h.local_dt(r.get("start_time", 0), self.config).strftime("%Y-%m-%d %H:%M")
        folder = h.get_folder_name(r, self.folders)
        done = self.state.get("transcribed", {}).get(r["id"], {})
        tag = "  [incomplete]" if done.get("status") == "transcribed" else ""
        return (f"{when}  {h.format_duration(r.get('duration', 0)):>6}  {h.source_label(r):<5}  "
                f"{r.get('filename', 'untitled')}" + (f" ({folder})" if folder else "") + tag)

    # --- derived fields ---

    def selected_ids(self):
        picked = set(self.query_one("#recs", SelectionList).selected)
        return [r["id"] for r in self.rows if r["id"] in picked]

    def effective_node(self):
        if self.node_override is not None:
            return self.node_override or None
        return (self.specs.get(self.project) or {}).get("node") if self.project else None

    def effective_mode(self):
        return self.mode_choice or ("now" if self.effective_node() else "none")

    def _suggest(self):
        if self.project_touched or not self.specs:
            return
        ids = self.selected_ids()
        first = next((r for r in self.rows if ids and r["id"] == ids[0]), None)
        self.project = (plan_logic.suggest_project(first.get("filename", ""), self.state,
                                                   list(self.specs), host=self.host)
                        if first else None)

    def fields(self):
        """The plan fields exactly as handed to build_job (empty ones left out)."""
        ids = self.selected_ids()
        live = self._live_selected(ids)
        if live:     # armed: no recordings yet, the job waits for this meeting
            return self._with_text({"recordings": [], "arm": live["id"].removeprefix("wispr:"),
                                    "arm_title": live.get("filename") or None, **self._common()})
        f = {"recordings": ids}
        if self.merge:
            f["merge"] = True
        cuts = {r: s for r, s in self.cuts.items() if r in ids and s}
        if cuts:
            f["cuts"] = cuts
        f.update(self._common())
        return self._with_text(f)

    def _common(self):
        f = {}
        if self.project:
            f["project"] = self.project
        if self.node_override:
            f["node"] = self.node_override
        mode = self.mode_choice or ("none" if self.node_override == "" else None)
        if mode:
            f["mode"] = mode
        return f

    def _with_text(self, f):
        for key, wid in (("note", "#note"), ("instructions", "#instructions")):
            text = self.query_one(wid, TextArea).text.strip()
            if text:
                f[key] = text
        return f

    def _live_selected(self, ids):
        """The selected live Wispr row, or None. Only a live Wispr meeting can be armed (E4):
        Plaud and Recorder rows never are, whatever their flags say."""
        return next((r for r in self.rows if r["id"] in ids and r.get("live")
                     and r.get("source") == "wispr" and r["id"].startswith("wispr:")), None)

    def _refresh_labels(self):
        spec = self.specs.get(self.project) if self.project else None
        if self.project:
            proj = f"2. Project: {self.project}   push: {spec['push'] if spec else '?'}   (p: change)"
        else:
            proj = "2. Project: (none, the classifier routes it)   push: set after routing   (p: pick)"
        self.query_one("#project", Static).update(proj)
        node = self.effective_node()
        self.query_one("#node", Static).update(
            f"3. Node: {node or '(none)'}   (n: pick from the tree, x: clear)")
        want = f"mode-{self.effective_mode()}"
        with self.prevent(RadioSet.Changed):
            self.query_one(f"#{want}", RadioButton).value = True
        extra = []
        if self.merge:
            extra.append("merge: on")
        if self.cuts:
            extra.append("cuts: " + "; ".join(f"{r} {','.join(s)}" for r, s in self.cuts.items() if s))
        self.sub_title = "  ".join(extra)

    def _error(self, text=""):
        self.query_one("#error", Static).update(text)

    # --- events ---

    def on_selection_list_selected_changed(self, event):
        self._suggest()
        self._refresh_labels()

    def on_radio_set_changed(self, event):
        self.mode_choice = event.pressed.label.plain
        self._refresh_labels()

    def check_action(self, action, parameters):
        if action in ("save", "save_now", "quit_form", "merge", "cut", "pick_project",
                      "pick_node", "clear_node", "arm"):
            if len(self.screen_stack) > 1:      # a picker is open: its own keys apply
                return False
            # inside a text area Enter and the letters are text; only Esc and ctrl+s act
            if isinstance(self.focused, TextArea) and action not in ("quit_form", "save_now"):
                return False
        return True

    # --- actions ---

    def action_save(self):
        self._save()

    def action_save_now(self):
        self._save()

    def _dirty(self):
        """Did the user change anything from what a fresh launch shows?"""
        if any(self.query_one(w, TextArea).text.strip() for w in ("#note", "#instructions")):
            return True
        default = [self.rows[0]["id"]] if self.rows else []
        return self.selected_ids() != default or bool(self.cuts) or self.merge

    def action_quit_form(self):
        if self._dirty() and not self._discard_armed:
            self._discard_armed = True
            self._error("Discard this plan? Esc again to quit")
            return
        self.exit(None)

    def action_merge(self):
        self.merge = not self.merge
        self._error("merge needs two or more selected recordings" if self.merge and len(self.selected_ids()) < 2
                    else "")
        self._refresh_labels()

    def action_cut(self):
        recs = self.query_one("#recs", SelectionList)
        if recs.highlighted is None or not self.rows:
            return
        if self.rows[recs.highlighted].get("live"):
            self._error("A live meeting cannot be cut: it has no transcript yet")
            return
        rid = self.rows[recs.highlighted]["id"]

        def done(text):
            if text is None:
                return
            if not text:
                self.cuts.pop(rid, None)
            else:
                try:
                    self.host.parse_cut(text)
                except ValueError as e:
                    self._error(str(e))
                    return
                self.cuts.setdefault(rid, []).append(text)
            self._error("")
            self._refresh_labels()
        self.push_screen(AskScreen(f"Leave a span out of {rid}  (START-END, e.g. 29:16-34:55; empty clears)",
                                   "29:16-34:55"), done)

    def action_pick_project(self):
        items = [("(none: let the classifier route)", NONE_CHOICE)]
        items += [(f"{n}   push: {s['push']}" + (f"   node: {s['node']}" if s["node"] else ""), n)
                  for n, s in self.specs.items()]

        def done(value):
            if value is None:
                return
            self.project, self.project_touched = (value or None), True
            self._refresh_labels()
        self.push_screen(PickerScreen("Project", items), done)

    def action_pick_node(self):
        tree = (self.effective_node() or "").split(":")[0]
        if not tree:
            self._error("No tree known: choose a project that has a node first")
            return
        if tree not in self.nodes_cache:
            self.nodes_cache[tree] = plan_logic.list_nodes(tree, self.config)
        nodes = self.nodes_cache[tree]
        if not nodes:
            self._error(f"No nodes listed for {tree} (cc-tree show failed); the project's node stays")
            return
        items = [("(clear: no hand-off)", NONE_CHOICE), (f"{tree}  (the tree itself)", tree)]
        items += [("  " * n["depth"] + f"{'[x] ' if n['state'] == 'done' else ''}{n['title']}  #{n['ref'].split(':')[1]}",
                   n["ref"]) for n in nodes]

        def done(value):
            if value is None:
                return
            self.node_override = value
            self.mode_choice = None if value else "none"
            self._error("")
            self._refresh_labels()
        self.push_screen(PickerScreen(f"Node in {tree}", items), done)

    def action_arm(self):
        """Select only the highlighted row for arming; refused for anything that is not a
        live Wispr meeting (E4: Plaud / Recorder upload times are unknown)."""
        recs = self.query_one("#recs", SelectionList)
        if recs.highlighted is None or not self.rows:
            return
        row = self.rows[recs.highlighted]
        if not self._live_selected([row["id"]]):
            self._error("Only a live Wispr meeting can be armed (Plaud and Recorder recordings "
                        "have no known upload time); this row can be saved as a normal job")
            return
        recs.deselect_all()
        recs.select(row["id"])
        self.merge = False
        self.cuts.pop(row["id"], None)
        self._error("")
        self._refresh_labels()

    def action_clear_node(self):
        self.node_override, self.mode_choice = "", "none"
        self._refresh_labels()

    def _save(self):
        if not self.loaded:
            self._error("Still loading the recordings")
            return
        ids = self.selected_ids()
        if self._live_selected(ids):
            if len(ids) > 1:
                self._error("The live meeting can only be armed on its own: unselect the other "
                            "recordings (a job cannot mix a running meeting with finished ones)")
                return
            if self.merge or any(self.cuts.get(i) for i in ids):
                self._error("An armed job cannot be merged or cut: press m / clear the cut first")
                return
        f = self.fields()
        if not f["recordings"] and "arm" not in f:
            self._error("Select at least one recording (Space)")
            return
        if self.merge and len(f["recordings"]) < 2:
            self._error("merge needs two or more selected recordings")
            return
        try:
            job = plan_logic.build_job(f, host=self.host, config=self.config)
            if self.node_override == "":       # cleared: build_job would refill the project's node
                job["deliver"]["node"] = None
                jobs.validate_job(job)
        except jobs.JobError as e:
            self._error(str(e))
            return
        self._error("")

        def confirmed(yes):
            if not yes:
                return
            try:
                jobs.save_job(job)
            except (jobs.JobError, OSError) as e:
                self._error(str(e))
                return
            self.exit(job["id"])
        self.push_screen(ConfirmScreen(self._summary(job)), confirmed)

    def _summary(self, job):
        """One line: what will be transcribed, where it goes, who gets it."""
        titles = {r["id"]: r.get("filename", r["id"]) for r in self.rows}
        trig = job.get("trigger")
        what = (f"arm: runs when '{trig.get('title') or 'the meeting'}' ends" if trig
                else " + ".join(titles.get(r, r) for r in job["recordings"]))
        spec = self.specs.get(job["project"]) if job["project"] else None
        where = (f"{job['project']} (push {job['push'] or (spec or {}).get('push') or 'none'})"
                 if job["project"] else "classifier-routed project (push set after routing)")
        d = job["deliver"]
        node = d["node"] or "no hand-off"
        return f"{what} \u2192 {where} \u2192 {node}, {d['mode']}"
