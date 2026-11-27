"""The plan screen (plan_app.py) driven with Textual's Pilot: keys in, job files out.

Needs Textual, which Homebrew python lacks: run with .venv/bin/pytest (requirements-plan.txt).
Nothing touches ~/.plaud-sync, the network, cc-tree or a real Wispr database.
"""
import asyncio
import json

import pytest

pytest.importorskip("textual")

from textual import events                               # noqa: E402
from textual.widgets import SelectionList, Static, TextArea   # noqa: E402

import jobs                                              # noqa: E402
import plan_app                                          # noqa: E402
import plan_logic                                        # noqa: E402
import plaud_transcribe as pt                            # noqa: E402

MIN = 60_000
NODES = [{"ref": "hriste:a1b", "title": "Backlog", "state": "open", "depth": 0},
         {"ref": "hriste:c2d", "title": "Release notes", "state": "open", "depth": 1}]


def rec(rid, name, start, minutes=30, source="plaud"):
    return {"id": rid, "source": source, "filename": name, "duration": minutes * MIN,
            "start_time": start, "filetag_id_list": []}


RECS = [rec("r-new", "Hřiště Hrou weekly sync", 3_000_000),
        rec("r-mid", "Hřiště Hrou retro", 2_000_000),
        rec("r-old", "Dentist call", 1_000_000)]
PROJECTS = {"Hřiště Hrou": {"dir": "/tmp/hriste", "push": "main", "node": "hriste:root"},
            "Private": {"dir": "/tmp/private", "push": "none"}}


@pytest.fixture
def env(monkeypatch, tmp_path):
    """Config, state, jobs and every source in tmp_path or stubbed; offline by construction."""
    monkeypatch.setattr(pt, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(pt, "STATE_FILE", tmp_path / "state.json")
    monkeypatch.setattr(pt, "CONFIG_FILE", tmp_path / "config.json")
    monkeypatch.setattr(jobs, "JOBS_DIR", tmp_path / "jobs")
    (tmp_path / "config.json").write_text(json.dumps({"projects": PROJECTS}))
    (tmp_path / "state.json").write_text(json.dumps({"downloaded": {}, "transcribed": {
        "old1": {"status": "done", "project": "Hřiště Hrou", "title": "Hřiště Hrou weekly"}}}))
    monkeypatch.setattr(pt, "get_token", lambda: "tok")
    monkeypatch.setattr(pt, "list_recordings", lambda *a: [dict(r) for r in RECS])
    monkeypatch.setattr(pt, "fetch_folders", lambda *a: {})
    monkeypatch.setattr(pt.wispr_flow, "list_meetings", lambda *a: [])
    monkeypatch.setattr(pt.google_recorder, "list_recordings", lambda *a: [])
    monkeypatch.setattr(plan_logic, "live_meeting", lambda *a, **k: None)
    monkeypatch.setattr(plan_logic, "list_nodes", lambda tree, config=None: list(NODES))
    monkeypatch.setattr(plan_logic, "list_trees", lambda config=None: [
        {"tree": "garden", "title": "Garden"}, {"tree": "hriste", "title": "Hřiště"}])
    return tmp_path


def run(scenario):
    """Run `async def scenario(app, pilot)` against a fresh PlanApp; returns (app, exit value)."""
    app = plan_app.PlanApp(host=pt)

    async def go():
        async with app.run_test(size=(120, 50)) as pilot:
            await app.workers.wait_for_complete()
            await pilot.pause()
            await scenario(app, pilot)
            await pilot.pause()
        return app.return_value
    return app, asyncio.run(go())


def saved_jobs():
    return jobs.list_jobs()


def same_job(saved, fields):
    """The saved job equals build_job of the same fields (G3), except id and timestamp."""
    expected = plan_logic.build_job(fields, host=pt, config=pt.load_config())
    def strip(j):
        j = {k: v for k, v in j.items() if k not in ("id", "created")}
        if j.get("trigger"):
            j["trigger"] = {k: v for k, v in j["trigger"].items() if k != "armed_at"}
        return j
    return strip(saved) == strip(expected)


class TestOneRecordingDefaultsAccepted:
    """G1: the common case must not turn into a form to fill in."""

    def test_enter_asks_to_confirm_and_a_second_enter_saves_the_job_for_the_newest_recording(self, env):
        keys = ["enter", "enter"]

        async def scenario(app, pilot):
            await pilot.press(keys[0])
            assert saved_jobs() == [], "one Enter must not create a job that spends money"
            assert "Hřiště Hrou weekly sync" in str(app.screen.query_one("#confirm-summary", Static).render())
            await pilot.press(keys[1])
        app, job_id = run(scenario)
        assert keys == ["enter", "enter"]
        (job,) = saved_jobs()
        assert job_id == job["id"] and job["recordings"] == ["r-new"]
        assert len(keys) <= 5, "spec G1: launch to saved job in at most 5 keystrokes"
        assert same_job(job, {"recordings": ["r-new"], "project": "Hřiště Hrou"})

    def test_the_project_is_the_suggestion_from_earlier_similar_titles(self, env):
        async def scenario(app, pilot):
            assert app.project == "Hřiště Hrou"
            assert "push: main" in str(app.query_one("#project", Static).render())
        run(scenario)

    def test_node_and_mode_follow_the_project_so_the_hand_off_needs_no_typing(self, env):
        async def scenario(app, pilot):
            await pilot.press("enter", "enter")
        run(scenario)
        (job,) = saved_jobs()
        assert job["deliver"]["node"] == "hriste:root" and job["deliver"]["mode"] == "now"


class TestSelectingAndMerging:
    """G4: two recordings of one meeting become one transcript."""

    def test_space_adds_a_second_recording_and_m_merges_them(self, env):
        async def scenario(app, pilot):
            await pilot.press("down", "space", "m", "enter", "enter")
        run(scenario)
        (job,) = saved_jobs()
        assert job["recordings"] == ["r-new", "r-mid"] and job["merge"] is True
        assert same_job(job, {"recordings": ["r-new", "r-mid"], "merge": True,
                              "project": "Hřiště Hrou"})

    def test_merge_with_a_single_recording_is_refused_with_a_message(self, env):
        async def scenario(app, pilot):
            await pilot.press("m", "enter")
            assert "two or more" in str(app.query_one("#error", Static).render())
        run(scenario)
        assert saved_jobs() == []

    def test_merge_plus_cut_shows_the_error_build_job_gives_and_writes_nothing(self, env):
        async def scenario(app, pilot):
            await pilot.press("down", "space")
            await pilot.press("c")
            await pilot.press(*"29:16-34:55", "enter")
            await pilot.press("m", "enter")
            assert "merge and cuts cannot be combined" in str(app.query_one("#error", Static).render())
        _, result = run(scenario)
        assert saved_jobs() == [] and result is None


class TestCuts:
    def test_a_cut_on_the_highlighted_recording_lands_in_the_job(self, env):
        async def scenario(app, pilot):
            await pilot.press("c")
            await pilot.press(*"29:16-34:55", "enter")
            await pilot.press("enter", "enter")
        run(scenario)
        (job,) = saved_jobs()
        assert job["cuts"] == {"r-new": ["29:16-34:55"]}

    def test_a_malformed_span_is_reported_instead_of_stored(self, env):
        async def scenario(app, pilot):
            await pilot.press("c")
            await pilot.press(*"nonsense", "enter")
            assert app.cuts == {} and str(app.query_one("#error", Static).render())
        run(scenario)


class TestNodeOverride:
    """G4: the project's node is only a default; the tree's other nodes are one picker away."""

    def test_n_opens_a_fuzzy_picker_and_the_chosen_node_is_saved(self, env):
        async def scenario(app, pilot):
            await pilot.press("n")
            await pilot.press(*"release")          # filters the list; Enter takes the first match
            await pilot.press("enter")
            await pilot.press("enter", "enter")
        run(scenario)
        (job,) = saved_jobs()
        assert job["deliver"]["node"] == "hriste:c2d"
        assert same_job(job, {"recordings": ["r-new"], "project": "Hřiště Hrou",
                              "node": "hriste:c2d"})

    def test_x_clears_the_node_and_the_job_hands_nothing_over(self, env):
        async def scenario(app, pilot):
            await pilot.press("x", "enter", "enter")
        run(scenario)
        (job,) = saved_jobs()
        assert job["deliver"]["node"] is None and job["deliver"]["mode"] == "none"

    def test_escape_inside_the_picker_closes_only_the_picker(self, env):
        async def scenario(app, pilot):
            await pilot.press("n", "escape")
            assert len(app.screen_stack) == 1
            await pilot.press("enter", "enter")
        run(scenario)
        assert len(saved_jobs()) == 1


class TestNodeForAProjectWithoutOne:
    """Projects configured as a bare path name no node; n must still reach every node by
    listing the cc-tree trees first, or a hand-off can never be planned (2026-11-25)."""

    def _private(self, app):
        app.project, app.project_touched = "Private", True
        app._refresh_labels()

    def test_n_lists_the_trees_then_the_chosen_trees_nodes(self, env, monkeypatch):
        asked = []
        monkeypatch.setattr(plan_logic, "list_nodes",
                            lambda tree, config=None: asked.append(tree) or list(NODES))

        async def scenario(app, pilot):
            self._private(app)
            await pilot.press("n")
            assert app.screen.query_one("Label").render().plain == "Tree"
            await pilot.press(*"hriste", "enter")       # the tree
            await pilot.press(*"release", "enter")      # its node
            assert len(app.screen_stack) == 1
            await pilot.press("enter", "enter")
        run(scenario)
        assert asked == ["hriste"]
        (job,) = saved_jobs()
        assert job["deliver"]["node"] == "hriste:c2d" and job["deliver"]["mode"] == "now"

    def test_another_tree_goes_back_to_the_tree_list(self, env):
        async def scenario(app, pilot):
            await pilot.press("n")                       # Hřiště Hrou's node: straight to nodes
            await pilot.press(*"another tree", "enter")
            assert app.screen.query_one("Label").render().plain == "Tree"
            await pilot.press(*"garden", "enter")
            assert app.screen.query_one("Label").render().plain == "Node in garden"
        run(scenario)

    def test_a_pasted_node_ref_is_taken_as_is_from_the_tree_list(self, env):
        """Tomas knows the ref (kopernici:#2vqv, as cc-tree prints it) and pastes it where
        only tree names are listed; it must not just filter the list down to nothing."""
        async def scenario(app, pilot):
            self._private(app)
            await pilot.press("n", *"kopernici:#2vqv", "enter")
            assert len(app.screen_stack) == 1
            await pilot.press("enter", "enter")
        run(scenario)
        (job,) = saved_jobs()
        assert job["deliver"]["node"] == "kopernici:2vqv" and job["deliver"]["mode"] == "now"

    def test_a_pasted_ref_also_works_in_the_node_list(self, env):
        async def scenario(app, pilot):
            await pilot.press("n", *"garden:4gfd", "enter")
            await pilot.press("enter", "enter")
        run(scenario)
        assert saved_jobs()[0]["deliver"]["node"] == "garden:4gfd"

    def test_words_that_are_not_a_ref_still_just_filter(self):
        assert plan_app.parse_node_ref("Tereza") is None
        assert plan_app.parse_node_ref("hriste") is None
        assert plan_app.parse_node_ref(" kopernici:#2vqv ") == "kopernici:2vqv"

    def test_no_trees_listed_says_so_and_keeps_the_form(self, env, monkeypatch):
        monkeypatch.setattr(plan_logic, "list_trees", lambda config=None: [])

        async def scenario(app, pilot):
            self._private(app)
            await pilot.press("n")
            assert len(app.screen_stack) == 1
            assert "cc-tree trees failed" in str(app.query_one("#error", Static).render())
        run(scenario)


class TestHandOffHost:
    """h picks the machine the hand-off runs on (a fresh session starts there); u sets push,
    which a remote host needs: that session reads the transcript from origin."""

    def _hosts(self, env):
        cfg = json.loads((env / "config.json").read_text())
        cfg["deliver"] = {"hosts": ["hp-ubuntu"]}
        (env / "config.json").write_text(json.dumps(cfg))

    def test_h_moves_the_hand_off_to_the_hp_and_the_job_says_so(self, env):
        self._hosts(env)

        async def scenario(app, pilot):
            await pilot.press("h")
            assert "on: hp-ubuntu" in str(app.query_one("#node", Static).render())
            await pilot.press("enter", "enter")
        run(scenario)
        (job,) = saved_jobs()
        assert job["deliver"]["host"] == "hp-ubuntu" and job["push"] == "main"

    def test_h_again_comes_back_to_this_mac(self, env):
        self._hosts(env)

        async def scenario(app, pilot):
            await pilot.press("h", "h")
            assert "on: this Mac" in str(app.query_one("#node", Static).render())
        run(scenario)

    def test_a_project_that_pushes_nothing_is_warned_and_u_turns_push_on(self, env):
        self._hosts(env)

        async def scenario(app, pilot):
            app.project, app.project_touched = "Private", True      # push none
            app.node_override = "hriste:c2d"
            app._refresh_labels()
            await pilot.press("h")
            assert "press u" in str(app.query_one("#error", Static).render())
            await pilot.press("u")                                     # none -> main
            assert "push: main" in str(app.query_one("#project", Static).render())
            await pilot.press("enter", "enter")
        run(scenario)
        (job,) = saved_jobs()
        assert job["push"] == "main" and job["deliver"]["host"] == "hp-ubuntu"

    def test_no_hosts_configured_says_how_to_add_one(self, env):
        async def scenario(app, pilot):
            await pilot.press("h")
            assert "deliver" in str(app.query_one("#error", Static).render())
            assert "on: this Mac" in str(app.query_one("#node", Static).render())
        run(scenario)


class TestQuittingWritesNothing:
    """G2: the screen never starts work and an abandoned form leaves no trace."""

    def test_escape_in_the_middle_of_the_form_asks_once_then_leaves_the_jobs_dir_empty(self, env):
        async def scenario(app, pilot):
            await pilot.press("down", "space", "tab", "tab")   # selected two; focus in the note
            await pilot.press(*"half-written")
            await pilot.press("escape")
            assert app.is_running and "Discard this plan" in str(app.query_one("#error", Static).render())
            await pilot.press("escape")
        _, result = run(scenario)
        assert result is None
        assert not (env / "jobs").exists() or list((env / "jobs").iterdir()) == []

    def test_escape_on_an_untouched_form_quits_at_once(self, env):
        async def scenario(app, pilot):
            await pilot.press("escape")
            await pilot.pause()
            assert not app.is_running
        run(scenario)
        assert saved_jobs() == []

    def test_changing_the_recordings_alone_also_asks_before_discarding(self, env):
        async def scenario(app, pilot):
            await pilot.press("down", "space", "escape")
            assert app.is_running and "Discard this plan" in str(app.query_one("#error", Static).render())
        run(scenario)

    def test_going_back_from_the_confirmation_writes_nothing_and_keeps_the_form(self, env):
        async def scenario(app, pilot):
            await pilot.press("enter", "escape")
            assert len(app.screen_stack) == 1 and app.is_running
        run(scenario)
        assert saved_jobs() == []


class TestTextAreas:
    """Pasted paragraphs are the norm for the note and the instructions."""

    def test_a_pasted_multi_paragraph_note_keeps_its_blank_lines(self, env):
        text = "First paragraph.\n\nSecond one, with: punctuation & ‘quotes’."

        async def scenario(app, pilot):
            await pilot.press("tab", "tab")
            app.post_message(events.Paste(text))   # as a terminal delivers it: to the app, then the focused widget
            await pilot.pause()
            await pilot.press("ctrl+s", "enter")
        run(scenario)
        (job,) = saved_jobs()
        assert job["deliver"]["note"] == text

    def test_enter_in_a_text_area_is_a_newline_and_does_not_save(self, env):
        async def scenario(app, pilot):
            await pilot.press("tab", "tab", "tab")
            await pilot.press("a", "enter", "b")
            assert app.query_one("#instructions", TextArea).text == "a\nb"
        run(scenario)
        assert saved_jobs() == []

    def test_instructions_reach_the_job(self, env):
        async def scenario(app, pilot):
            await pilot.press("tab", "tab", "tab")
            await pilot.press(*"short", "ctrl+s", "enter")
        run(scenario)
        assert saved_jobs()[0]["instructions"] == "short"


class TestLiveMeeting:
    def test_the_meeting_in_progress_is_the_first_row_marked_live_and_preselected(
            self, env, monkeypatch):
        live = {"id": "wispr:m1", "source": "wispr", "filename": "Standup", "start_time": 4_000_000,
                "duration": 0, "live": True, "filetag_id_list": []}
        monkeypatch.setattr(plan_logic, "live_meeting", lambda *a, **k: live)

        async def scenario(app, pilot):
            recs = app.query_one("#recs", SelectionList)
            assert app.rows[0]["id"] == "wispr:m1"
            assert "LIVE" in str(recs.get_option_at_index(0).prompt)
            await pilot.press("enter", "enter")
        run(scenario)
        # the live row is armed, not queued as a recording (K15)
        assert saved_jobs()[0]["trigger"]["wispr"] == "m1" and saved_jobs()[0]["recordings"] == []


LIVE_ROW = {"id": "wispr:9f2c-aaaa", "source": "wispr", "filename": "Koperníci weekly",
            "start_time": 4_000_000, "duration": 0, "live": True, "filetag_id_list": []}


class TestArmingTheLiveMeeting:
    """E4 / K15: saving with the live meeting selected arms the job instead of queueing a
    recording that does not exist yet."""

    def _live(self, monkeypatch):
        monkeypatch.setattr(plan_logic, "live_meeting", lambda *a, **k: dict(LIVE_ROW))

    def test_enter_enter_arms_the_job_and_the_summary_says_when_it_runs(self, env, monkeypatch):
        self._live(monkeypatch)
        seen = {}

        async def scenario(app, pilot):
            await pilot.press("enter")
            seen["summary"] = str(app.screen.query_one("#confirm-summary", Static).render())
            await pilot.press("enter")
        run(scenario)
        assert "arm: runs when 'Koperníci weekly' ends" in seen["summary"]
        (job,) = saved_jobs()
        assert job["state"] == "armed" and job["recordings"] == []
        assert same_job(job, {"recordings": [], "arm": "9f2c-aaaa", "arm_title": "Koperníci weekly"})

    def test_the_other_recordings_cannot_ride_along_with_the_meeting(self, env, monkeypatch):
        self._live(monkeypatch)

        async def scenario(app, pilot):
            await pilot.press("down", "space", "enter")     # live + r-new
            assert "only be armed on its own" in str(app.query_one("#error", Static).render())
        run(scenario)
        assert saved_jobs() == []

    def test_merge_with_the_live_meeting_is_refused_and_writes_nothing(self, env, monkeypatch):
        self._live(monkeypatch)

        async def scenario(app, pilot):
            await pilot.press("down", "space", "m", "enter")
            assert saved_jobs() == []
            assert "only be armed" in str(app.query_one("#error", Static).render())
        run(scenario)

    def test_a_live_meeting_has_no_transcript_to_cut(self, env, monkeypatch):
        self._live(monkeypatch)

        async def scenario(app, pilot):
            await pilot.press("c")
            assert "cannot be cut" in str(app.query_one("#error", Static).render())
            assert app.cuts == {}
        run(scenario)

    def test_a_plaud_row_can_never_be_armed(self, env, monkeypatch):
        self._live(monkeypatch)

        async def scenario(app, pilot):
            await pilot.press("down", "a")      # r-new is a Plaud recording
            assert "Only a live Wispr meeting can be armed" in str(app.query_one("#error", Static).render())
            assert app.selected_ids() == ["wispr:9f2c-aaaa"]
        run(scenario)

    def test_a_recorder_row_flagged_live_is_still_not_armable(self, env, monkeypatch):
        bogus = dict(LIVE_ROW, id="recorder:x1", source="recorder")
        monkeypatch.setattr(plan_logic, "live_meeting", lambda *a, **k: bogus)

        async def scenario(app, pilot):
            await pilot.press("enter", "enter")
        run(scenario)
        (job,) = saved_jobs()
        assert job["state"] != "armed" and job["recordings"] == ["recorder:x1"]

    def test_a_on_the_live_row_leaves_only_it_selected(self, env, monkeypatch):
        self._live(monkeypatch)

        async def scenario(app, pilot):
            await pilot.press("down", "space", "up", "a")
            assert app.selected_ids() == ["wispr:9f2c-aaaa"]
        run(scenario)


class TestArmLiveFlag:
    def test_with_no_live_meeting_the_screen_says_so_and_plans_normally(self, env):
        app = plan_app.PlanApp(host=pt, arm_live=True)

        async def go():
            async with app.run_test(size=(120, 50)) as pilot:
                await app.workers.wait_for_complete()
                await pilot.pause()
                assert "No Wispr meeting is live" in str(app.query_one("#notes", Static).render())
                await pilot.press("enter", "enter")
            return app.return_value
        asyncio.run(go())
        (job,) = saved_jobs()
        assert job["state"] == "queued" and job["recordings"] == ["r-new"]

    def test_with_a_live_meeting_it_is_the_selection_and_no_warning_shows(self, env, monkeypatch):
        monkeypatch.setattr(plan_logic, "live_meeting", lambda *a, **k: dict(LIVE_ROW))
        app = plan_app.PlanApp(host=pt, arm_live=True)

        async def go():
            async with app.run_test(size=(120, 50)) as pilot:
                await app.workers.wait_for_complete()
                await pilot.pause()
                assert "No Wispr meeting" not in str(app.query_one("#notes", Static).render())
                assert app.selected_ids() == ["wispr:9f2c-aaaa"]
        asyncio.run(go())


class TestOffline:
    """The laptop is on a plane: the screen must explain, not crash."""

    def test_plaud_unreachable_shows_the_note_and_still_lists_local_recordings(
            self, env, monkeypatch):
        def down(*a):
            raise OSError("network is unreachable")
        monkeypatch.setattr(pt, "list_recordings", down)
        monkeypatch.setattr(pt.wispr_flow, "list_meetings",
                            lambda *a: [rec("wispr:z", "Local meeting", 5_000_000, source="wispr")])

        async def scenario(app, pilot):
            assert "Plaud not reachable" in str(app.query_one("#notes", Static).render())
            assert [r["id"] for r in app.rows] == ["wispr:z"]
        run(scenario)

    def test_no_token_is_a_note_and_the_network_is_never_tried(self, env, monkeypatch):
        def no_token():
            raise SystemExit(1)
        monkeypatch.setattr(pt, "get_token", no_token)
        monkeypatch.setattr(pt, "list_recordings", lambda *a: pytest.fail("network call"))

        async def scenario(app, pilot):
            assert "No Plaud token" in str(app.query_one("#notes", Static).render())
            assert app.rows == []
            await pilot.press("enter")
            assert "Select at least one" in str(app.query_one("#error", Static).render())
        run(scenario)
        assert saved_jobs() == []

    def test_nothing_pending_says_so(self, env, monkeypatch):
        monkeypatch.setattr(pt, "list_recordings", lambda *a: [])

        async def scenario(app, pilot):
            assert "No pending recordings" in str(app.query_one("#notes", Static).render())
        run(scenario)


class TestTheScreenAndEnqueueWriteTheSameJob:
    """G3: the screen is only a way to fill in build_job's fields, so the same choices made
    with keys or with `enqueue` flags give the same job (id and timestamps aside)."""

    @staticmethod
    def _enqueue(**kw):
        import argparse
        import jobs_cli
        base = dict(recording=[], merge=False, cut=[], instructions=None, project=None, push=None,
                    node=None, host=None, mode=None, note=None, dry_run=True)
        return jobs_cli.build_enqueue_job(argparse.Namespace(**{**base, **kw}), pt)

    @staticmethod
    def _strip(job):
        return {k: v for k, v in job.items() if k not in ("id", "created")}

    def test_merged_recordings_with_the_node_cleared(self, env):
        async def scenario(app, pilot):
            await pilot.press("down", "space", "m", "x", "enter", "enter")
        run(scenario)
        (job,) = saved_jobs()
        cli = self._enqueue(recording=["r-new", "r-mid"], merge=True, project="Hřiště Hrou",
                            node="")
        assert self._strip(job) == self._strip(cli) and job["deliver"]["node"] is None

    def test_a_hand_off_moved_to_another_host_with_a_cut(self, env):
        cfg = json.loads((env / "config.json").read_text())
        cfg["deliver"] = {"hosts": ["hp-ubuntu"]}
        (env / "config.json").write_text(json.dumps(cfg))

        async def scenario(app, pilot):
            await pilot.press("c")
            await pilot.press(*"1:00-2:00", "enter")
            await pilot.press("h", "enter", "enter")
        run(scenario)
        (job,) = saved_jobs()
        cli = self._enqueue(recording=["r-new"], cut=["r-new=1:00-2:00"], project="Hřiště Hrou",
                            host="hp-ubuntu")
        assert self._strip(job) == self._strip(cli) and job["deliver"]["host"] == "hp-ubuntu"
