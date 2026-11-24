import json
import os

import pytest

import jobs
import jobs_cli
import plaud_transcribe as pt


def _boom(*a, **k):
    raise AssertionError("network/side effect used where none is allowed")


@pytest.fixture
def env(monkeypatch, tmp_path):
    """Jobs dir and config in tmp_path; every network function raises."""
    monkeypatch.setattr(jobs, "JOBS_DIR", tmp_path / "jobs")
    monkeypatch.setattr(pt, "CONFIG_FILE", tmp_path / "config.json")
    for fn in ("get_token", "plaud_get", "fetch_folders"):
        monkeypatch.setattr(pt, fn, _boom)
    return tmp_path


def _config(tmp_path, projects):
    (tmp_path / "config.json").write_text(json.dumps({"projects": projects}))


def run(argv, capsys):
    code = jobs_cli.main(argv, host=pt)
    out = capsys.readouterr()
    return code, out.out, out.err


HRISTE = {"dir": "~/h", "push": "pr", "node": "hriste:root", "deliver_ask": "process it"}


class TestEnqueueFromAScriptOrMenuBar:
    """enqueue is called from SwiftBar and scripts, often offline or before the
    meeting even ends, so it must never need the Plaud API."""

    def test_enqueue_writes_a_job_without_any_network_call(self, env, capsys):
        code, out, _ = run(["enqueue", "--recording", "abc123"], capsys)
        assert code == 0
        [job] = jobs.list_jobs()
        assert job["recordings"] == ["abc123"]
        assert job["schema"] == 1 and job["seen"] is False and job["state"] == "queued"

    def test_dry_run_shows_the_job_but_leaves_the_queue_untouched(self, env, capsys):
        code, out, _ = run(["enqueue", "--recording", "abc", "--dry-run"], capsys)
        assert code == 0
        assert json.loads(out)["recordings"] == ["abc"]
        assert not jobs.JOBS_DIR.exists() or list(jobs.JOBS_DIR.iterdir()) == []

    def test_a_malformed_recording_id_is_refused_at_enqueue_not_hours_later_in_the_runner(
            self, env, capsys):
        code, _, err = run(["enqueue", "--recording", "bad id!"], capsys)
        assert code == 2 and "not a valid id" in err
        assert jobs.list_jobs() == []

    def test_a_job_without_recordings_is_refused_because_the_runner_would_have_nothing_to_do(
            self, env, capsys):
        code, _, err = run(["enqueue"], capsys)
        assert code == 2 and "at least one recording" in err


class TestCutsOnlyWorkOnOneRecording:
    """Merged mode has no cuts parameter, so the job must say no up front."""

    def test_merge_with_cuts_is_rejected_with_a_message_that_says_what_to_drop(self, env, capsys):
        code, _, err = run(["enqueue", "--recording", "a", "--recording", "b", "--merge",
                            "--cut", "a=1:00-2:00"], capsys)
        assert code == 2 and "merge and cuts cannot be combined" in err
        assert jobs.list_jobs() == []

    def test_validate_job_rejects_the_same_combination_for_jobs_built_elsewhere(self):
        job = jobs.new_job(recordings=["a"], merge=True, cuts={"a": ["1:00-2:00"]})
        with pytest.raises(jobs.JobError, match="cannot be combined"):
            jobs.validate_job(job)

    def test_a_cut_is_stored_per_recording(self, env, capsys):
        run(["enqueue", "--recording", "a", "--cut", "a=29:16-34:55"], capsys)
        assert jobs.list_jobs()[0]["cuts"] == {"a": ["29:16-34:55"]}

    def test_a_cut_for_a_recording_not_in_the_job_is_refused(self, env, capsys):
        code, _, err = run(["enqueue", "--recording", "a", "--cut", "zzz=1:00-2:00"], capsys)
        assert code == 2 and "not one of the job's recordings" in err

    def test_a_backwards_cut_is_refused_using_the_same_rule_as_process(self, env, capsys):
        code, _, err = run(["enqueue", "--recording", "a", "--cut", "a=5:00-1:00"], capsys)
        assert code == 2 and "end must be after start" in err


class TestProjectDefaults:
    """The project carries push mode, node and the default ask so the plan
    needs no repeated typing; flags still win for a one-off."""

    def test_project_supplies_push_node_and_ask(self, env, capsys):
        _config(env, {"Hriste": HRISTE})
        run(["enqueue", "--recording", "a", "--project", "Hriste"], capsys)
        job = jobs.list_jobs()[0]
        assert job["project"] == "Hriste" and job["push"] == "pr"
        assert job["deliver"] == {"node": "hriste:root", "mode": "now", "note": None,
                                  "ask": "process it"}

    def test_flags_override_the_project_defaults(self, env, capsys):
        _config(env, {"Hriste": HRISTE})
        run(["enqueue", "--recording", "a", "--project", "Hriste", "--push", "main",
             "--node", "x:1", "--mode", "later", "--note", "just file it"], capsys)
        job = jobs.list_jobs()[0]
        assert job["push"] == "main"
        assert job["deliver"]["node"] == "x:1" and job["deliver"]["mode"] == "later"
        assert job["deliver"]["note"] == "just file it"

    def test_without_a_project_push_and_node_stay_unset_for_the_commit_step(self, env, capsys):
        _config(env, {"Hriste": HRISTE})
        run(["enqueue", "--recording", "a"], capsys)
        job = jobs.list_jobs()[0]
        assert job["project"] is None and job["push"] is None
        assert job["deliver"]["node"] is None and job["deliver"]["mode"] == "none"

    def test_a_string_project_has_no_node_so_mode_defaults_to_none(self, env, capsys):
        _config(env, {"Plain": "~/p"})
        run(["enqueue", "--recording", "a", "--project", "Plain"], capsys)
        job = jobs.list_jobs()[0]
        assert job["push"] == "none" and job["deliver"]["mode"] == "none"

    def test_unknown_project_lists_the_configured_names(self, env, capsys):
        _config(env, {"Hriste": HRISTE, "AI Jam": "~/a"})
        code, _, err = run(["enqueue", "--recording", "a", "--project", "Nope"], capsys)
        assert code == 2 and "AI Jam, Hriste" in err
        assert jobs.list_jobs() == []

    def test_asking_to_deliver_to_a_project_with_no_node_is_an_error_not_a_silent_skip(
            self, env, capsys):
        _config(env, {"Plain": "~/p"})
        code, _, err = run(["enqueue", "--recording", "a", "--project", "Plain",
                            "--mode", "now"], capsys)
        assert code == 2 and "needs a node" in err


class TestAtomicStore:
    """A reboot or kill mid-write must not leave a job the runner chokes on."""

    def test_crash_between_tmp_and_rename_leaves_no_job_and_no_tmp_file(self, env, monkeypatch):
        monkeypatch.setattr(os, "replace", lambda *a: (_ for _ in ()).throw(KeyboardInterrupt()))
        with pytest.raises(KeyboardInterrupt):
            jobs.save_job(jobs.new_job(recordings=["a"]))
        assert list(jobs.JOBS_DIR.iterdir()) == []
        assert jobs.list_jobs() == []

    def test_crash_while_updating_keeps_the_previous_complete_file(self, env, monkeypatch):
        job = jobs.new_job(recordings=["a"])
        jobs.save_job(job)
        job["state"] = "running"
        monkeypatch.setattr(os, "replace", lambda *a: (_ for _ in ()).throw(OSError("disk")))
        with pytest.raises(OSError):
            jobs.save_job(job)
        assert jobs.load_job(job["id"])["state"] == "queued"
        assert [p.name for p in jobs.JOBS_DIR.iterdir()] == [f"{job['id']}.json"]

    def test_unparseable_files_do_not_hide_the_good_jobs(self, env):
        jobs.save_job(jobs.new_job(recordings=["a"]))
        (jobs.JOBS_DIR / "junk.json").write_text("{half")
        assert len(jobs.list_jobs()) == 1

    def test_set_step_updates_in_place_so_a_resume_sees_one_entry_per_step(self):
        job = jobs.new_job(recordings=["a"])
        jobs.set_step(job, "transcribe", "running")
        jobs.set_step(job, "transcribe", "done")
        assert [(s["name"], s["state"]) for s in job["steps"]] == [("transcribe", "done")]

    def test_job_ids_sort_by_creation_time(self):
        assert jobs.new_job_id().count("-") == 2


class TestCancelAndSeen:
    """Cancel must not pull a job out from under a running runner; seen only
    silences the warning (D9)."""

    @pytest.mark.parametrize("state", ["queued", "failed", "held", "armed"])
    def test_cancel_works_from_states_nothing_is_executing(self, env, capsys, state):
        job = jobs.new_job(recordings=["a"], state=state)
        jobs.save_job(job)
        code, _, _ = run(["jobs", "cancel", job["id"]], capsys)
        assert code == 0 and jobs.load_job(job["id"])["state"] == "cancelled"

    @pytest.mark.parametrize("state", ["running", "done", "cancelled"])
    def test_cancel_refuses_running_and_finished_jobs(self, env, capsys, state):
        job = jobs.new_job(recordings=["a"], state=state)
        jobs.save_job(job)
        code, _, err = run(["jobs", "cancel", job["id"]], capsys)
        assert code == 2 and state in err
        assert jobs.load_job(job["id"])["state"] == state

    def test_seen_keeps_the_job_and_its_state(self, env, capsys):
        job = jobs.new_job(recordings=["a"], state="failed")
        jobs.save_job(job)
        run(["jobs", "seen", job["id"]], capsys)
        again = jobs.load_job(job["id"])
        assert again["seen"] is True and again["state"] == "failed"

    def test_show_and_list_find_a_job_by_unique_prefix(self, env, capsys):
        job = jobs.new_job(recordings=["a"])
        jobs.save_job(job)
        _, out, _ = run(["jobs", "show", job["id"][:13]], capsys)
        assert json.loads(out)["id"] == job["id"]
        _, out, _ = run(["jobs", "list"], capsys)
        assert job["id"] in out

    def test_unknown_job_is_a_clean_error(self, env, capsys):
        code, _, err = run(["jobs", "show", "nope"], capsys)
        assert code == 2 and "no job" in err


class TestHelpRunsNothing:
    """SwiftBar and agents discover the CLI through --help; it must be inert."""

    ARGVS = [["enqueue", "--help"], ["jobs", "--help"], ["jobs", "list", "--help"],
             ["jobs", "show", "--help"], ["jobs", "cancel", "--help"],
             ["jobs", "seen", "--help"]]

    @pytest.mark.parametrize("argv", ARGVS)
    def test_help_prints_usage_and_touches_nothing(self, env, monkeypatch, capsys, argv):
        for name in ("save_job", "load_job", "list_jobs", "cancel_job", "mark_seen"):
            monkeypatch.setattr(jobs, name, _boom)
        monkeypatch.setattr(pt, "load_config", _boom)
        with pytest.raises(SystemExit) as e:
            jobs_cli.main(argv, host=pt)
        assert e.value.code == 0
        assert "usage:" in capsys.readouterr().out

    def test_enqueue_help_lists_every_job_field_flag_and_a_recipe(self, capsys):
        with pytest.raises(SystemExit):
            jobs_cli.main(["enqueue", "--help"], host=pt)
        out = capsys.readouterr().out
        for flag in ("--recording", "--merge", "--cut", "--instructions", "--project", "--push",
                     "--node", "--mode", "--note", "--dry-run", "recipes:"):
            assert flag in out


class TestDispatchBeforeNetwork:
    """The old main() fetches a token and the folder list before anything else."""

    def test_main_routes_enqueue_to_the_job_cli_without_get_token(self, env, monkeypatch, capsys):
        monkeypatch.setattr("sys.argv", ["plaud-transcribe", "enqueue", "--recording", "a"])
        with pytest.raises(SystemExit) as e:
            pt.main()
        assert e.value.code == 0 and len(jobs.list_jobs()) == 1

    def test_top_level_help_lists_the_new_commands(self, monkeypatch, capsys):
        monkeypatch.setattr("sys.argv", ["plaud-transcribe", "--help"])
        with pytest.raises(SystemExit):
            pt.main()
        out = capsys.readouterr().out
        assert "enqueue" in out and "jobs" in out
