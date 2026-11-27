"""The plan screen: pick recordings, a project, a node and a note, then save ONE job file.

`plaud-transcribe plan` runs this (Textual; see requirements-plan.txt). The screen never
transcribes anything: its only write is jobs.save_job() on Enter, and quitting writes nothing
(spec G2). The runner picks the job up. Every field goes through plan_logic.build_job, the same
builder `enqueue` uses, so a job from here equals one from `enqueue` with the same values (G3).
This file only draws and handles keys: loading, arm eligibility, host choices, the job's
defaults and every save rule live in plan_logic, so another surface can reuse them.

Keys (the footer shows them too)
  Enter  show a one-line summary; Enter again saves the job and exits, printing its id   (inside a text area Enter is a newline;
         use ctrl+s there)
  Esc    quit, nothing written
  Space  select / unselect the recording under the cursor      Tab / shift+Tab  move on
  m      merge the selected recordings into one transcript     c  cut a span out of the
         (not combinable with cuts)                               highlighted recording
  p      pick the project (fuzzy)       n  pick the node (fuzzy)       x  clear the node
  u      push: main -> pr -> none (default: the project's)
  h      host to hand off on: this Mac -> each config deliver.hosts entry (e.g. hp-ubuntu):
         cc-tree deliver runs there over ssh, so a fresh session starts there; needs u = main|pr
         n with no node known (the project names none) lists the cc-tree trees first;
         "(another tree…)" in the node list goes back to them
         either list takes a pasted node ref (kopernici:#2vqv or kopernici:2vqv) + Enter
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
  a recording of another source (Wispr, Recorder, an exported phone call) shows in the same
  list ("calls": {"enabled": true} in the config for calls); the live Wispr meeting is
  first and marked "LIVE".
"""

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
SWITCH_TREE = "\x00switch-tree"   # node picker value: go back to the tree list
SWITCH_TREE_LABEL = "(another tree…)"
REF_HINT = "type to filter, or paste a node ref (kopernici:#2vqv)"


parse_node_ref = plan_logic.parse_node_ref
fuzzy_filter = plan_logic.fuzzy_filter


class PickerScreen(ModalScreen):
    """A fuzzy picker: type to filter, Up/Down to move, Enter picks, Esc cancels (None)."""

    BINDINGS = [Binding("escape", "cancel", "Cancel"),
                Binding("down", "cursor(1)", show=False), Binding("up", "cursor(-1)", show=False)]
    DEFAULT_CSS = """
    PickerScreen { align: center middle; }
    PickerScreen > Vertical { width: 80%; height: 70%; border: round $accent; background: $surface; padding: 0 1; }
    PickerScreen OptionList { height: 1fr; }
    """

    def __init__(self, title, items, typed=None, placeholder="type to filter"):
        """typed: optional fn(query) -> value or None; a query it accepts (a pasted node ref)
        is picked on Enter even when it matches no row."""
        super().__init__()
        self._title, self._items, self._typed, self._placeholder = title, list(items), typed, placeholder

    def compose(self):
        with Vertical():
            yield Label(self._title)
            yield Input(placeholder=self._placeholder, id="picker-query")
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
        typed = self._typed(event.value) if self._typed else None
        if typed:
            self.dismiss(typed)
            return
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
        Binding("h", "cycle_host", "Host"),
        Binding("u", "cycle_push", "Push"),
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
        self.host_override = None  # None = the project's host; "" = this Mac; else an ssh host
        self.push_override = None  # None = the project's push; else main|pr|none
        self.nodes_cache = {}
        self.trees_cache = None    # `cc-tree trees`, fetched on the first n without a node
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
        data = plan_logic.load_plan_data(self.host, self.days, arm_live=self.arm_live)
        self.call_from_thread(self._loaded, data)

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
        self.query_one("#notes", Static).update("\n".join(data["notes"]))
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

    def _delivery(self):
        """What the job would get now (plan_logic.resolve_delivery, as build_job does)."""
        return plan_logic.resolve_delivery(self._common(), self.specs.get(self.project)
                                           if self.project else None)

    def effective_node(self):
        return self._delivery()["node"]

    def effective_host(self):
        return self._delivery()["host"] or None

    def effective_push(self):
        return self._delivery()["push"]

    def effective_mode(self):
        return self._delivery()["mode"]

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
        live = plan_logic.live_selected(self.rows, ids)
        f = {"recordings": ids}
        if live:     # armed: the job waits for this meeting; build_job refuses anything else
            f = {"recordings": [i for i in ids if i != live["id"]],
                 "arm": live["id"].removeprefix("wispr:"), "arm_title": live.get("filename") or None}
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
        if self.node_override is not None:
            f["node"] = self.node_override       # "" = cleared: no hand-off
        if self.host_override is not None:
            f["host"] = self.host_override
        if self.push_override:
            f["push"] = self.push_override
        if self.mode_choice:
            f["mode"] = self.mode_choice
        return f

    def _with_text(self, f):
        for key, wid in (("note", "#note"), ("instructions", "#instructions")):
            text = self.query_one(wid, TextArea).text.strip()
            if text:
                f[key] = text
        return f

    def _refresh_labels(self):
        push = self.effective_push()
        if self.project:
            proj = f"2. Project: {self.project}   push: {push or '?'}   (p: change, u: push)"
        else:
            proj = (f"2. Project: (none, the classifier routes it)   push: "
                    f"{push or 'set after routing'}   (p: pick, u: push)")
        self.query_one("#project", Static).update(proj)
        node = self.effective_node()
        host = self.effective_host()
        self.query_one("#node", Static).update(
            f"3. Node: {node or '(none)'}   on: {host or 'this Mac'}   "
            "(n: pick from the tree, x: clear, h: host)")
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
                      "pick_node", "clear_node", "arm", "cycle_host", "cycle_push"):
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
            self._pick_tree()
            return
        self._pick_node_in(tree)

    def _pick_tree(self):
        """No node known (the project names none): pick the tree first, then its node."""
        if self.trees_cache is None:
            self.trees_cache = plan_logic.list_trees(self.config)
        if not self.trees_cache:
            self._error("No cc-tree trees listed (cc-tree trees failed)")
            return
        items = [(f"{t['tree']}  {t['title']}", t["tree"]) for t in self.trees_cache]

        def done(value):
            if not value:
                return
            if ":" in value:                    # a pasted node ref
                self._set_node(value)
            else:
                self._pick_node_in(value)
        self.push_screen(PickerScreen("Tree", items, typed=parse_node_ref, placeholder=REF_HINT), done)

    def _pick_node_in(self, tree):
        if tree not in self.nodes_cache:
            self.nodes_cache[tree] = plan_logic.list_nodes(tree, self.config)
        nodes = self.nodes_cache[tree]
        if not nodes:
            self._error(f"No nodes listed for {tree} (cc-tree show failed); the project's node stays")
            return
        items = [("(clear: no hand-off)", NONE_CHOICE), (SWITCH_TREE_LABEL, SWITCH_TREE),
                 (f"{tree}  (the tree itself)", tree)]
        items += [("  " * n["depth"] + f"{'[x] ' if n['state'] == 'done' else ''}{n['title']}  #{n['ref'].split(':')[1]}",
                   n["ref"]) for n in nodes]

        def done(value):
            if value is None:
                return
            if value == SWITCH_TREE:
                self._pick_tree()
                return
            self._set_node(value)
        self.push_screen(PickerScreen(f"Node in {tree}", items, typed=parse_node_ref,
                                      placeholder=REF_HINT), done)

    def _set_node(self, value):
        self.node_override = value
        self.mode_choice = None if value else "none"
        self._error("")
        self._refresh_labels()

    def action_arm(self):
        """Select only the highlighted row for arming; refused for anything that is not a
        live Wispr meeting (E4: Plaud / Recorder / call upload times are unknown)."""
        recs = self.query_one("#recs", SelectionList)
        if recs.highlighted is None or not self.rows:
            return
        row = self.rows[recs.highlighted]
        if not plan_logic.armable(row):
            self._error("Only a live Wispr meeting can be armed (Plaud, Recorder and call recordings "
                        "have no known upload time); this row can be saved as a normal job")
            return
        recs.deselect_all()
        recs.select(row["id"])
        self.merge = False
        self.cuts.pop(row["id"], None)
        self._error("")
        self._refresh_labels()

    def action_cycle_host(self):
        """this Mac -> each config deliver.hosts entry (and the project's host) -> this Mac."""
        hosts = plan_logic.host_options(self.config, self.specs, self.project)
        if len(hosts) == 1:
            self._error('No other host configured: add "deliver": {"hosts": ["hp-ubuntu"]} to the config')
            return
        cur = self.effective_host() or ""
        self.host_override = hosts[(hosts.index(cur) + 1) % len(hosts)] if cur in hosts else hosts[0]
        if self.host_override and self.effective_push() in (None, "none"):
            self._error(f"A session on {self.host_override} reads the transcript from origin: "
                        "press u for push main or pr")
        else:
            self._error("")
        self._refresh_labels()

    def action_cycle_push(self):
        order = ["main", "pr", "none"]
        cur = self.effective_push()
        self.push_override = order[(order.index(cur) + 1) % 3] if cur in order else order[0]
        self._error("")
        self._refresh_labels()

    def action_clear_node(self):
        self.node_override, self.mode_choice = "", "none"
        self._refresh_labels()

    def _save(self):
        if not self.loaded:
            self._error("Still loading the recordings")
            return
        try:
            job = plan_logic.build_job(self.fields(), host=self.host, config=self.config)
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
        self.push_screen(ConfirmScreen(plan_logic.job_summary(job, self.rows, self.specs)), confirmed)
