"""Plan-screen logic (plan_logic.py): no Textual, no network, no real Wispr or cc-tree."""
import argparse
import json
from pathlib import Path

import pytest

import jobs
import jobs_cli
import plan_logic
import plaud_transcribe as pt

FIXTURE = Path(__file__).parent / "fixtures" / "cc_tree_show.txt"
MIN = 60_000


def rec(rid, name="Weekly sync", source="plaud", minutes=30, start=1_000_000):
    return {"id": rid, "source": source, "filename": name, "duration": minutes * MIN,
            "start_time": start, "filetag_id_list": []}


@pytest.fixture
def env(monkeypatch, tmp_path):
    """State, config and jobs dirs in tmp_path; nothing reads ~/.plaud-sync."""
    monkeypatch.setattr(pt, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(pt, "STATE_FILE", tmp_path / "state.json")
    monkeypatch.setattr(pt, "CONFIG_FILE", tmp_path / "config.json")
    monkeypatch.setattr(jobs, "JOBS_DIR", tmp_path / "jobs")
    return tmp_path


def write_config(env, projects):
    (env / "config.json").write_text(json.dumps({"projects": projects}))


def sources(monkeypatch, plaud=(), wispr=(), recorder=()):
    monkeypatch.setattr(pt, "list_recordings", lambda *a: list(plaud))
    monkeypatch.setattr(pt.wispr_flow, "list_meetings", lambda *a: list(wispr))
    monkeypatch.setattr(pt.google_recorder, "list_recordings", lambda *a: list(recorder))


class TestPendingMeansWhatTheInteractiveListMeans:
    """The plan screen and `plaud-transcribe process` must never disagree about what is
    waiting, or a recording would be offered in one place and hidden in the other."""

    STATE = {"transcribed": {
        "done1": {"status": "done"}, "half": {"status": "transcribed"},
        "skip1": {"status": "skipped"}}}
    RECS = [rec("new1", start=5), rec("done1", start=4), rec("half", start=3),
            rec("skip1", start=2), rec("tiny", minutes=0, start=1)]

    def test_plan_list_equals_the_list_cmd_process_shows(self, env, monkeypatch, capsys):
        sources(monkeypatch, plaud=self.RECS)
        state = json.loads(json.dumps(self.STATE))
        shown, _ = pt.show_pending([r for r in self.RECS if r["id"] != "tiny"], {}, state, {})
        got = plan_logic.pending_recordings("tok", {}, self.STATE, 30)
        assert [r["id"] for r in got] == [r["id"] for r in shown] == ["new1", "half"]

    def test_cmd_process_still_auto_skips_short_ones_into_state(self, env, monkeypatch):
        sources(monkeypatch, plaud=self.RECS)
        state = json.loads(json.dumps(self.STATE))
        shown = []
        monkeypatch.setattr(pt, "show_pending", lambda recs, f, s, c: shown.append(
            [r["id"] for r in pt.split_pending(recs, s)[0]]) or ([], []))
        args = argparse.Namespace(days=30, extra=[], all=False)
        pt.cmd_process(args, "tok", {"projects": {}}, state, {})
        assert state["transcribed"]["tiny"]["status"] == "skipped"
        assert shown == [["new1", "half"]]

    def test_the_plan_screen_does_not_write_state_because_quitting_must_write_nothing(
            self, env, monkeypatch):
        sources(monkeypatch, plaud=self.RECS)
        state = json.loads(json.dumps(self.STATE))
        plan_logic.pending_recordings("tok", {}, state, 30)
        assert state == self.STATE
        assert not (env / "state.json").exists()


class TestOfflineStillPlans:
    """On a train there is no Plaud API, but a Wispr meeting or a Recorder file
    can still be planned."""

    def test_plaud_failure_keeps_wispr_and_recorder_rows_and_says_why(self, env, monkeypatch):
        def boom(*a):
            raise OSError("network is unreachable")
        sources(monkeypatch, wispr=[rec("wispr:a", source="wispr", start=2)],
                recorder=[rec("recorder:1", source="recorder", start=3)])
        monkeypatch.setattr(pt, "list_recordings", boom)
        got = plan_logic.pending_recordings("tok", {}, {}, 30)
        assert [r["id"] for r in got] == ["recorder:1", "wispr:a"]
        assert any("Plaud not reachable" in n and "network is unreachable" in n for n in got.notes)

    def test_expired_token_exits_inside_plaud_get_yet_does_not_kill_the_screen(self, env, monkeypatch):
        sources(monkeypatch, wispr=[rec("wispr:a", source="wispr")])
        monkeypatch.setattr(pt, "list_recordings", lambda *a: (_ for _ in ()).throw(SystemExit(1)))
        got = plan_logic.pending_recordings("tok", {}, {}, 30)
        assert [r["id"] for r in got] == ["wispr:a"] and got.notes

    def test_an_unreadable_wispr_store_leaves_a_note_not_a_crash(self, env, monkeypatch):
        sources(monkeypatch, plaud=[rec("p1")])
        monkeypatch.setattr(pt.wispr_flow, "list_meetings", lambda *a: 1 / 0)
        got = plan_logic.pending_recordings("tok", {}, {}, 30)
        assert [r["id"] for r in got] == ["p1"]
        assert any(n.startswith("Wispr not readable") for n in got.notes)


class TestLiveMeetingComesFirst:
    """The meeting being recorded right now is what Tomas most often wants to plan."""

    def test_live_row_is_flagged_and_listed_before_older_pending_rows(self, env, monkeypatch):
        import autocapture
        monkeypatch.setattr(pt.wispr_flow, "wispr_enabled", lambda c: True)
        monkeypatch.setattr(autocapture, "wispr_live_meeting",
                            lambda c, h: {"id": "uuid-1", "created_ms": 9_999_999})
        monkeypatch.setattr(pt.wispr_flow, "_connect", lambda c: (_ for _ in ()).throw(OSError("no db")))
        live = plan_logic.live_meeting({})
        assert live["live"] is True and live["id"] == "wispr:uuid-1" and live["source"] == "wispr"
        rows = plan_logic.with_live_first([rec("p1", start=1), rec("wispr:uuid-1", start=2)], live)
        assert [r["id"] for r in rows] == ["wispr:uuid-1", "p1"]
        assert rows[0].get("live") is True   # no duplicate of the same meeting further down

    def test_no_live_meeting_gives_none_and_leaves_the_list_alone(self, env, monkeypatch):
        import autocapture
        monkeypatch.setattr(pt.wispr_flow, "wispr_enabled", lambda c: True)
        monkeypatch.setattr(autocapture, "wispr_live_meeting", lambda c, h: None)
        assert plan_logic.live_meeting({}) is None
        assert plan_logic.with_live_first([rec("p1")], None) == [rec("p1")]

    def test_a_broken_wispr_read_means_no_live_row_not_an_exception(self, env, monkeypatch):
        import autocapture
        monkeypatch.setattr(pt.wispr_flow, "wispr_enabled", lambda c: True)
        monkeypatch.setattr(autocapture, "wispr_live_meeting", lambda c, h: 1 / 0)
        assert plan_logic.live_meeting({}) is None


class TestSuggestionFromHistory:
    """Meetings that never say the project name aloud are still recognisable by how
    earlier ones in that project were titled."""

    STATE = {"transcribed": {
        "a": {"status": "done", "name": "BOM design sync", "project": "Hriste", "date": "2026-11-01"},
        "b": {"status": "done", "name": "Pharmacy stock review", "project": "Lekarny", "date": "2026-11-02"},
        "c": {"status": "done", "name": "Odd one", "project": "default", "date": "2026-11-03"}}}
    PROJECTS = ["Hriste", "Lekarny", "Breeze"]

    def test_a_similar_title_picks_the_project_its_predecessors_went_to(self):
        assert plan_logic.suggest_project("BOM design sync 24.11.", self.STATE, self.PROJECTS) == "Hriste"

    def test_diacritics_and_case_do_not_hide_a_match(self):
        state = {"transcribed": {"a": {"status": "done", "name": "Lékárny porada", "project": "Lekarny"}}}
        assert plan_logic.suggest_project("LEKARNY PORADA", state, self.PROJECTS) == "Lekarny"

    def test_title_that_names_the_project_matches_even_without_history(self):
        assert plan_logic.suggest_project("Breeze kickoff", {}, self.PROJECTS) == "Breeze"
        assert plan_logic.suggest_project("Breeze", {}, self.PROJECTS) == "Breeze"

    def test_nothing_similar_gives_none_so_the_picker_starts_empty(self):
        assert plan_logic.suggest_project("Dentist appointment", self.STATE, self.PROJECTS) is None

    def test_one_shared_word_is_not_enough_evidence(self):
        assert plan_logic.suggest_project("Team sync", self.STATE, self.PROJECTS) is None

    def test_a_tie_between_projects_is_none_rather_than_a_coin_flip(self):
        state = {"transcribed": {
            "a": {"status": "done", "name": "Weekly planning", "project": "Hriste"},
            "b": {"status": "done", "name": "Weekly planning", "project": "Lekarny"}}}
        assert plan_logic.suggest_project("Weekly planning", state, self.PROJECTS) is None

    def test_projects_removed_from_config_are_never_suggested(self):
        assert plan_logic.suggest_project("BOM design sync", self.STATE, ["Lekarny"]) is None

    def test_default_bucket_history_is_ignored(self):
        assert plan_logic.suggest_project("Odd one", self.STATE, self.PROJECTS + ["default"]) is None

    def test_empty_title_is_none(self):
        assert plan_logic.suggest_project("", self.STATE, self.PROJECTS) is None


class TestCcTreeNodePicker:
    """`cc-tree show` output is undocumented and carries colour codes when a tty is
    attached; a parse problem must mean no picker, never a crashed form."""

    def test_rows_of_the_real_output_shape_are_parsed(self):
        nodes = plan_logic.parse_nodes(FIXTURE.read_text(), "garden")
        by_ref = {n["ref"]: n for n in nodes}
        assert by_ref["garden:4gfd"] == {"ref": "garden:4gfd", "title": "⏸ Plan the spring planting",
                                         "state": "open", "depth": 0}
        assert by_ref["garden:249f"]["state"] == "done" and by_ref["garden:249f"]["depth"] == 1
        assert by_ref["garden:7g4k"]["depth"] == 2
        assert by_ref["garden:3vkb"]["title"] == "Compost"   # top-level, no checkbox, trailing note
        assert by_ref["garden:5dq4"]["title"] == "Fix the irrigation timer"   # note after ◆ is not title
        assert by_ref["garden:25g4"]["title"].startswith("👤 Two deliveries")   # emoji belongs to the title
        assert by_ref["garden:32dc"]["state"] == "open"   # a later "#notanid" in the note is ignored
        assert len(nodes) == 12   # header, blank line and the ▣ workspace line are not rows

    def test_ansi_colour_codes_do_not_break_the_rows(self):
        coloured = "\n".join(f"\x1b[1m{l}\x1b[0m" if l.startswith("-") else l
                             for l in FIXTURE.read_text().splitlines())
        coloured = coloured.replace("[x]", "\x1b[32m[x]\x1b[0m")
        assert plan_logic.parse_nodes(coloured, "garden") == plan_logic.parse_nodes(FIXTURE.read_text(), "garden")

    def test_garbage_gives_no_nodes(self):
        for junk in ("", "\x00\x01 not a tree", "Error: no such tree\n", "- [x]\n- #\n", None):
            assert plan_logic.parse_nodes(junk, "garden") == []

    def test_tree_taken_from_a_tree_colon_id_node_and_header_slug_used_when_none_given(self):
        assert plan_logic.parse_nodes(FIXTURE.read_text(), "garden:4gfd")[0]["ref"] == "garden:4gfd"
        assert plan_logic.parse_nodes(FIXTURE.read_text(), "")[0]["ref"] == "garden:4gfd"

    def _stub(self, tmp_path, monkeypatch, body, code=0):
        script = tmp_path / "cc-tree-stub"
        script.write_text(f"#!/bin/bash\necho \"$@\" > {tmp_path}/argv\n{body}\nexit {code}\n")
        script.chmod(0o755)
        monkeypatch.setenv("CC_TREE_BIN", str(script))

    def test_list_nodes_runs_only_show_with_the_tree(self, tmp_path, monkeypatch):
        self._stub(tmp_path, monkeypatch, f"cat {FIXTURE}")
        nodes = plan_logic.list_nodes("garden:abcd")
        assert (tmp_path / "argv").read_text().strip() == "show garden"
        assert nodes[0]["ref"] == "garden:4gfd"

    def test_missing_binary_nonzero_exit_and_timeout_all_mean_no_picker(self, tmp_path, monkeypatch):
        monkeypatch.setenv("CC_TREE_BIN", str(tmp_path / "nope"))
        assert plan_logic.list_nodes("garden") == []
        self._stub(tmp_path, monkeypatch, "echo broken", code=3)
        assert plan_logic.list_nodes("garden") == []
        self._stub(tmp_path, monkeypatch, "sleep 5")
        monkeypatch.setattr(plan_logic, "NODES_TIMEOUT_S", 0.2)
        assert plan_logic.list_nodes("garden") == []

    def test_no_tree_means_no_call(self, monkeypatch):
        monkeypatch.setenv("CC_TREE_BIN", "/definitely/not/run")
        assert plan_logic.list_nodes("") == [] and plan_logic.list_nodes(None) == []


class TestOneJobWhoeverBuildsIt:
    """`enqueue` is what scripts and SwiftBar use, the plan screen is what Tomas uses;
    a job must not depend on which one made it (G3)."""

    PROJECTS = {"Hriste": {"dir": "/x/hriste", "push": "main", "node": "hriste:root",
                           "deliver_ask": "do the backlog"},
                "Plain": "/x/plain"}

    def _enqueue_args(self, **kw):
        base = dict(recording=["r1"], merge=False, cut=[], instructions=None, project=None,
                    push=None, node=None, mode=None, note=None, dry_run=True)
        base.update(kw)
        return argparse.Namespace(**base)

    def _same(self, a, b):
        a, b = dict(a), dict(b)
        for j in (a, b):
            j.pop("id"), j.pop("created")
        return a == b

    def test_enqueue_and_plan_fields_give_identical_jobs(self, env):
        write_config(env, self.PROJECTS)
        from_cli = jobs_cli.build_enqueue_job(
            self._enqueue_args(recording=["r1", "r2"], merge=True, project="Hriste",
                               instructions="short", note="look at it", mode="later"), pt)
        from_plan = plan_logic.build_job(
            {"recordings": ["r1", "r2"], "merge": True, "project": "Hriste",
             "instructions": "short", "note": "look at it", "mode": "later"}, host=pt)
        assert self._same(from_cli, from_plan)

    def test_cuts_from_either_door_end_up_the_same(self, env):
        write_config(env, self.PROJECTS)
        a = jobs_cli.build_enqueue_job(self._enqueue_args(cut=["r1=1:00-2:00", "r1=5:00-6:00"]), pt)
        b = plan_logic.build_job({"recordings": ["r1"], "cuts": {"r1": ["1:00-2:00", "5:00-6:00"]}}, host=pt)
        assert self._same(a, b) and a["cuts"] == {"r1": ["1:00-2:00", "5:00-6:00"]}

    def test_project_supplies_push_node_and_ask_and_mode_defaults_to_now(self, env):
        write_config(env, self.PROJECTS)
        job = plan_logic.build_job({"recordings": ["r1"], "project": "Hriste"}, host=pt)
        assert (job["project"], job["push"]) == ("Hriste", "main")
        assert job["deliver"] == {"node": "hriste:root", "mode": "now", "note": None,
                                  "ask": "do the backlog"}

    def test_explicit_push_and_node_beat_the_project_defaults(self, env):
        write_config(env, self.PROJECTS)
        job = plan_logic.build_job({"recordings": ["r1"], "project": "Hriste", "push": "pr",
                                    "node": "other:abcd", "mode": "none"}, host=pt)
        assert job["push"] == "pr" and job["deliver"]["node"] == "other:abcd"
        assert job["deliver"]["mode"] == "none"

    def test_project_without_node_defaults_to_no_handoff(self, env):
        write_config(env, self.PROJECTS)
        job = plan_logic.build_job({"recordings": ["r1"], "project": "Plain"}, host=pt)
        assert job["push"] == "none" and job["deliver"]["mode"] == "none"

    def test_no_project_leaves_everything_to_the_classifier(self, env):
        job = plan_logic.build_job({"recordings": ["r1"]}, host=pt, config={"projects": {}})
        assert job["project"] is None and job["push"] is None
        assert job["deliver"] == {"node": None, "mode": "none", "note": None, "ask": None}

    def test_mistakes_are_rejected_with_a_message_for_the_form(self, env):
        write_config(env, self.PROJECTS)
        bad = [({"recordings": []}, "at least one recording"),
               ({"recordings": ["r1"], "project": "Nope"}, "unknown project"),
               ({"recordings": ["r1", "r2"], "merge": True, "cuts": {"r1": ["1:00-2:00"]}}, "cannot be combined"),
               ({"recordings": ["r1"], "cuts": {"r1": ["2:00-1:00"]}}, "after start"),
               ({"recordings": ["r1"], "colour": "red"}, "unknown job field")]
        for fields, text in bad:
            with pytest.raises(jobs.JobError, match=text):
                plan_logic.build_job(fields, host=pt)

    def test_mode_now_for_a_project_without_a_node_is_refused(self, env):
        write_config(env, self.PROJECTS)
        with pytest.raises(jobs.JobError, match="needs a node"):
            plan_logic.build_job({"recordings": ["r1"], "project": "Plain", "mode": "now"}, host=pt)
