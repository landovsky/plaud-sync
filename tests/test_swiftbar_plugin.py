import importlib.util
import json
import shlex
import subprocess
import sys
import time
from pathlib import Path

import pytest

import autocapture
import jobs
import jobs_cli

PLUGIN = Path(__file__).resolve().parent.parent / "swiftbar" / "plaud.5s.py"
NOW = 1_800_000_000.0


def _load():
    spec = importlib.util.spec_from_file_location("plaud_swiftbar_plugin", PLUGIN)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


plug = _load()


@pytest.fixture(autouse=True)
def _own_swiftbar_config(monkeypatch, tmp_path):
    """install/uninstall-swiftbar write and delete the sidecar: never the real one."""
    monkeypatch.setenv("PLAUD_SWIFTBAR_CONFIG", str(tmp_path / "swiftbar.json"))


def _iso(age_s=0):
    return time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime(NOW - age_s))


def job(state="done", age=60, **kw):
    j = {"id": kw.pop("id", f"j{int(age)}-{state}"), "schema": 1, "created": _iso(age),
         "recordings": ["plaud:abc"], "project": "hriste", "state": state, "steps": [],
         "result": {"delivered": None}, "error": None, "seen": False}
    j.update(kw)
    return j


def render(jobs_=(), status=None, pid_alive=True, paused=False, overrides=None, actions=None):
    return plug.render(NOW, list(jobs_), status if status is not None else {"state": "watching"},
                       pid_alive, paused, overrides or {}, actions)


def title(out):
    return out.splitlines()[0]


class TestGlyphPriority:
    """One glyph has to say the most important thing; the order is the spec's."""

    def test_idle_watcher_shows_the_quiet_circle(self):
        assert title(render()) == "○"

    def test_capturing_beats_watching(self):
        out = render(status={"state": "capturing", "started": NOW - 23 * 60, "saved": 41})
        assert title(out) == "●"
        assert "capturing 23 min · 41 scenes" in out

    def test_runner_busy_shows_how_many_jobs_are_waiting_or_running(self):
        out = render([job("queued"), job("running", id="r"), job("done")],
                     status={"state": "capturing", "started": NOW})
        assert title(out) == "⟳2"

    def test_a_failed_job_outranks_a_busy_runner(self):
        assert title(render([job("running"), job("failed", error="push rejected")])) == "⚠"

    def test_dead_watcher_outranks_everything_so_a_silent_miss_is_visible(self):
        """F4: the 10-27 reboot killed the watcher and nobody noticed for two days."""
        out = render([job("failed", error="x"), job("running")], pid_alive=False)
        assert title(out) == "✕"
        assert "NOT RUNNING" in out

    def test_stale_status_file_does_not_hide_a_dead_watcher(self):
        out = render(status={"state": "capturing", "started": NOW - 9999}, pid_alive=False)
        assert title(out) == "✕"

    def test_deliberate_stop_is_paused_not_an_alarm(self):
        out = render(pid_alive=False, paused=True)
        assert title(out) == "○ paused"
        assert "Autocapture: paused" in out


class TestFailedJobsStayVisibleUntilSeen:
    """F5/D9: a failed job must nag until Tomas looks, then stop without vanishing."""

    def test_failed_job_shows_warning_and_its_error(self):
        out = render([job("failed", error="git push rejected", failed_step="push")])
        assert title(out) == "⚠"
        assert "push failed: git push rejected" in out

    def test_seen_clears_the_glyph_but_keeps_the_entry(self):
        out = render([job("failed", error="boom", seen=True)])
        assert title(out) == "○"
        assert "boom" in out

    def test_held_job_shows_the_held_id_to_release(self):
        j = job("held", result={"delivered": {"outcome": "held", "id": "9xq1"}})
        out = render([j])
        assert title(out) == "⚠"
        assert "held for you (9xq1)" in out

    def test_held_but_seen_stops_warning(self):
        j = job("held", seen=True, result={"delivered": {"id": "9xq1"}})
        assert title(render([j])) == "○"


class TestJobList:
    def test_most_recent_first(self):
        out = render([job("done", age=500, id="old"), job("running", age=5, id="new")])
        jobs_part = out.split("Jobs\n")[1]
        assert jobs_part.index("⟳") < jobs_part.index("✓")

    def test_done_jobs_older_than_a_day_are_hidden_but_failures_are_not(self):
        out = render([job("done", age=25 * 3600, project="ancient"),
                      job("failed", age=25 * 3600, project="stuck", error="e")])
        assert "ancient" not in out and "stuck" in out

    def test_list_is_capped_and_failures_survive_the_cap(self):
        many = [job("done", age=i, id=f"d{i}") for i in range(30)]
        many.append(job("failed", age=3000, id="f", project="keepme", error="e"))
        out = render(many)
        listed = out.split("Jobs\n")[1].split("\n---\n")[0].strip().splitlines()
        assert len([l for l in listed if not l.startswith("----")]) == plug.MAX_JOBS
        assert "keepme" in out

    def test_delivered_job_says_so(self):
        out = render([job("done", result={"delivered": {"outcome": "message"}})])
        assert "delivered" in out

    def test_no_jobs_says_so(self):
        assert "No recent jobs" in render()


class TestDisplaysSubmenu:
    def test_captured_displays_get_a_tick_and_off_ones_say_off(self):
        out = render(status={"state": "capturing", "displays": [
            {"name": "PHL 272B7QU", "captured": True, "source": "default"},
            {"name": "Built-in Retina Display", "captured": False, "source": "override"}]})
        assert "--✓ PHL 272B7QU" in out
        assert "Built-in Retina Display (off)" in out

    def test_pipe_in_a_display_name_cannot_inject_swiftbar_parameters(self):
        """SwiftBar splits a line at '|' and reads the rest as key=value params."""
        out = render(status={"state": "capturing", "displays": [
            {"name": "Evil | bash=/bin/rm", "captured": True, "source": "default"}]})
        assert "|" not in out

    def test_unplugged_monitor_keeps_its_override_visible(self):
        """F3: the override is keyed by name and waits for the monitor to return."""
        out = render(overrides={"Studio Display": False})
        assert "Studio Display (not connected, override off)" in out


class TestPluginIsCheapAndStandalone:
    def test_rendering_50_jobs_stays_far_below_the_100ms_budget(self):
        many = [job(["done", "failed", "running", "queued", "held"][i % 5], age=i * 60, id=f"j{i}",
                    error="e") for i in range(50)]
        start = time.perf_counter()
        for _ in range(10):
            render(many, status={"state": "capturing", "started": NOW, "displays": [
                {"name": f"D{i}", "captured": True} for i in range(3)]})
        assert (time.perf_counter() - start) / 10 < 0.02

    def test_importing_the_plugin_pulls_in_none_of_the_heavy_modules(self):
        """F6: plaud_transcribe imports visuals and friends; that alone blows the budget."""
        code = ("import importlib.util, sys; "
                f"s = importlib.util.spec_from_file_location('p', {str(PLUGIN)!r}); "
                "m = importlib.util.module_from_spec(s); s.loader.exec_module(m); "
                "bad = [n for n in ('plaud_transcribe','autocapture','jobs','visuals','socket','requests')"
                " if n in sys.modules]; print(bad)")
        out = subprocess.run([sys.executable, "-I", "-c", code], capture_output=True, text=True,
                             check=True).stdout.strip()
        assert out == "[]"

    def test_repo_copy_uses_env_python_and_is_executable(self):
        assert PLUGIN.read_text().startswith("#!/usr/bin/env python3\n")
        assert PLUGIN.stat().st_mode & 0o111

    def test_paths_match_the_modules_they_duplicate(self):
        """The plugin may not import jobs.py or autocapture.py, so a test pins the copies."""
        assert plug.JOBS_DIR == jobs.JOBS_DIR
        src = (Path(PLUGIN).parent.parent / "autocapture.py").read_text()
        assert 'STATE_DIR = Path.home() / ".cache" / "plaud-autocapture"' in src
        assert plug.STATE_DIR == Path.home() / ".cache" / "plaud-autocapture"
        assert set(plug.NEEDS_ATTENTION) <= set(jobs.STATES) and set(plug.BUSY) <= set(jobs.STATES)

    def test_gather_reads_the_files_and_main_survives_garbage(self, tmp_path):
        state, jd = tmp_path / "s", tmp_path / "j"
        state.mkdir(); jd.mkdir()
        (state / "status.json").write_text("{not json")
        (jd / "a.json").write_text("{}")
        (jd / "b.json").write_text('{"id": "b", "state": "queued", "recordings": ["x"]}')
        got = plug.gather(state, jd)
        assert got["status"] == {} and got["pid_alive"] is False and len(got["jobs"]) == 1
        assert title(plug.render(NOW, **got)) == "✕"


class TestInstallSwiftbar:
    """F7: SwiftBar is optional; install links the plugin or says so and still exits 0."""

    @staticmethod
    def _defaults(monkeypatch, value=None):
        calls = []

        def fake(cmd, **kw):
            calls.append(cmd)
            return subprocess.CompletedProcess(cmd, 0 if value else 1, stdout=(value or "") + "\n")
        monkeypatch.setattr(jobs_cli.subprocess, "run", fake)
        return calls

    def test_links_the_plugin_into_the_folder_swiftbar_reports(self, monkeypatch, tmp_path, capsys):
        folder = tmp_path / "plugins"
        self._defaults(monkeypatch, str(folder))
        monkeypatch.delenv("PLAUD_SWIFTBAR_DIR", raising=False)
        assert jobs_cli.main(["install-swiftbar"], host=None) == 0
        link = folder / "plaud.5s.py"
        assert link.is_symlink() and link.resolve() == PLUGIN.resolve()

    def test_dir_flag_beats_swiftbars_own_preference(self, monkeypatch, tmp_path):
        calls = self._defaults(monkeypatch, str(tmp_path / "other"))
        assert jobs_cli.main(["install-swiftbar", "--dir", str(tmp_path / "mine")], host=None) == 0
        assert (tmp_path / "mine" / "plaud.5s.py").is_symlink()
        assert calls == []

    def test_env_var_names_the_folder_too(self, monkeypatch, tmp_path):
        self._defaults(monkeypatch)
        monkeypatch.setenv("PLAUD_SWIFTBAR_DIR", str(tmp_path / "envdir"))
        jobs_cli.main(["install-swiftbar"], host=None)
        assert (tmp_path / "envdir" / "plaud.5s.py").is_symlink()

    def test_without_swiftbar_it_prints_a_hint_and_exits_zero(self, monkeypatch, tmp_path, capsys):
        self._defaults(monkeypatch)
        monkeypatch.delenv("PLAUD_SWIFTBAR_DIR", raising=False)
        monkeypatch.setattr(jobs_cli, "SWIFTBAR_APP_DIRS", (tmp_path / "nope.app",))
        monkeypatch.setattr(jobs_cli, "SWIFTBAR_DEFAULT_DIR", tmp_path / "no-plugins")
        assert jobs_cli.main(["install-swiftbar"], host=None) == 0
        assert "does not look installed" in capsys.readouterr().out
        assert not (tmp_path / "no-plugins").exists()

    def test_installing_twice_is_harmless_and_a_stale_link_is_repaired(self, monkeypatch, tmp_path):
        self._defaults(monkeypatch)
        d = tmp_path / "p"
        d.mkdir()
        (d / "plaud.5s.py").symlink_to(tmp_path / "old-checkout.py")
        jobs_cli.main(["install-swiftbar", "--dir", str(d)], host=None)
        jobs_cli.main(["install-swiftbar", "--dir", str(d)], host=None)
        assert (d / "plaud.5s.py").resolve() == PLUGIN.resolve()

    def test_a_real_file_in_the_way_is_never_overwritten(self, monkeypatch, tmp_path, capsys):
        self._defaults(monkeypatch)
        (tmp_path / "plaud.5s.py").write_text("mine")
        assert jobs_cli.main(["install-swiftbar", "--dir", str(tmp_path)], host=None) == 2
        assert (tmp_path / "plaud.5s.py").read_text() == "mine"

    def test_uninstall_removes_only_the_symlink(self, monkeypatch, tmp_path):
        self._defaults(monkeypatch)
        jobs_cli.main(["install-swiftbar", "--dir", str(tmp_path)], host=None)
        jobs_cli.main(["uninstall-swiftbar", "--dir", str(tmp_path)], host=None)
        assert not (tmp_path / "plaud.5s.py").exists() and PLUGIN.exists()


# --- K11: actions ---

PY, REPO = "/opt/venv/bin/python3", "/Users/x/my repo"   # a space in the path must survive too
ACT = {"python": PY, "repo": REPO}


def menu_items(out):
    """[(label, argv)] for every line carrying a bash= action, the way SwiftBar splits it:
    label before ' | ', then key=value pairs (double quotes keep spaces together)."""
    items = []
    for line in out.splitlines():
        label, sep, rest = line.rpartition(" | bash=")
        if not sep:
            continue
        params = dict(p.split("=", 1) for p in shlex.split("bash=" + rest))
        argv = [params[f"param{i}"] for i in range(1, len(params)) if f"param{i}" in params]
        items.append((label.lstrip("-"), params, argv))
    return items


def by_label(out, startswith):
    return [(p, a) for l, p, a in menu_items(out) if l.startswith(startswith)]


def parse_for_real(params, argv):
    """Dry: run the emitted command line through the target command's own argparse."""
    script = Path(argv[0]).name
    if script == "autocapture.py":
        return autocapture.build_parser().parse_args(argv[1:])
    assert script == "plaud_transcribe.py"
    return jobs_cli.build_parser().parse_args(argv[1:])


class TestDisplayToggleItems:
    """F1 UI: clicking a monitor in the menu flips it, without opening a terminal."""

    DISPLAYS = [{"name": "PHL 272B7QU", "captured": True},
                {"name": "Built-in Retina Display", "captured": False}]

    def _out(self, displays=None):
        return render(status={"state": "watching", "displays": displays or self.DISPLAYS}, actions=ACT)

    def test_click_turns_a_captured_monitor_off_and_an_off_one_on(self):
        out = self._out()
        (p1, a1), = by_label(out, "✓ PHL")
        (p2, a2), = by_label(out, "  Built-in")
        assert a1[1:] == ["display", "PHL 272B7QU", "off", "--notify-errors"]
        assert a2[1:] == ["display", "Built-in Retina Display", "on", "--notify-errors"]
        assert p1["terminal"] == "false" and p1["refresh"] == "true" and p1["bash"] == PY

    def test_names_and_repo_paths_with_spaces_survive_swiftbars_param_splitting(self):
        (p, a), = by_label(self._out(), "  Built-in")
        assert a[0] == f"{REPO}/autocapture.py"
        assert "Built-in Retina Display" in a

    def test_a_name_with_a_pipe_is_passed_by_number_not_corrupted(self):
        """'|' would end the parameter list; the number is resolved by `display` itself."""
        weird = [{"name": "A", "captured": True}, {"name": 'Odd | "TV"', "captured": True}]
        out = self._out(weird)
        assert len([l for l in out.splitlines() if "Odd" in l]) == 1
        (p, a), = by_label(out, "✓ Odd")
        assert a[1:] == ["display", "2", "off", "--notify-errors"]

    def test_every_emitted_command_is_accepted_by_the_real_parser(self):
        for _, p, a in menu_items(self._out()):
            ns = parse_for_real(p, a)
            assert ns.notify_errors is True

    def test_without_the_recorded_interpreter_the_lines_are_plain_text(self):
        out = render(status={"state": "watching", "displays": self.DISPLAYS})
        assert "bash=" not in out and "PHL 272B7QU" in out
        assert "install-swiftbar" in out


class TestPauseResumeItem:
    """Pause is `stop` (which leaves PAUSED_FILE so the agent respects it); resume is `start --watch`."""

    def test_running_watcher_offers_pause(self):
        (p, a), = by_label(render(actions=ACT), "Pause")
        assert a[1:] == ["stop", "--notify-errors"]
        assert parse_for_real(p, a).cmd == "stop"

    def test_paused_watcher_offers_resume(self):
        out = render(pid_alive=False, paused=True, actions=ACT)
        assert "Pause" not in out
        (p, a), = by_label(out, "Resume")
        assert a[1:] == ["start", "--watch", "--notify-errors"]
        ns = parse_for_real(p, a)
        assert ns.cmd == "start" and ns.watch is True

    def test_dead_unpaused_watcher_offers_start(self):
        (p, a), = by_label(render(pid_alive=False, actions=ACT), "Start")
        assert a[1:3] == ["start", "--watch"]

    def test_state_comes_from_the_paused_file_on_disk(self, tmp_path):
        state, jd = tmp_path / "s", tmp_path / "j"
        state.mkdir(); jd.mkdir()
        (state / "paused").touch()
        got = plug.gather(state, jd)
        assert "Resume" in plug.render(NOW, **got)


class TestJobActions:
    """F5/D2/D9: a failed or held job can be dealt with from the menu."""

    def _failed(self, **kw):
        return job("failed", id="20261124-1702", error="push rejected", failed_step="push", **kw)

    def test_failed_job_gets_retry_log_cancel_and_mark_seen(self):
        out = render([self._failed()], actions=ACT)
        labels = [l for l, _, _ in menu_items(out) if l in ("Retry", "Show log", "Cancel", "Mark seen")]
        assert labels == ["Retry", "Show log", "Cancel", "Mark seen"]
        (p, a), = by_label(out, "Retry")
        assert a[1:] == ["jobs", "retry", "20261124-1702", "--notify-errors"]
        assert a[0] == f"{REPO}/plaud_transcribe.py"

    def test_every_job_action_parses_through_the_real_jobs_parser(self):
        held = job("held", id="h1", result={"delivered": {"outcome": "held", "id": "9xq1"}})
        for j in (self._failed(), held, job("queued", id="q1")):
            for label, p, a in menu_items(render([j], actions=ACT)):
                if p["bash"] == PY and a[0].endswith("plaud_transcribe.py"):
                    ns = parse_for_real(p, a)
                    assert ns.command == "jobs" and ns.jobs_command in ("retry", "cancel", "seen")
                    assert ns.id == j["id"] and ns.notify_errors is True

    def test_show_log_opens_the_runner_log_in_console_and_needs_no_python(self):
        (p, a), = by_label(render([self._failed()]), "Show log")
        assert p["bash"] == "/usr/bin/open" and a[:2] == ["-a", "Console"]
        assert a[2].endswith(".plaud-sync/runner.log")

    def test_held_job_shows_its_id_and_how_to_release_it(self):
        held = job("held", id="h1", result={"delivered": {"outcome": "held", "id": "9xq1"}})
        out = render([held], actions=ACT)
        assert "Held for you: 9xq1" in out
        assert "cc-tree deliver --release 9xq1" in out

    def test_a_seen_job_loses_the_warning_and_the_mark_seen_item_but_keeps_retry(self):
        out = render([self._failed(seen=True)], actions=ACT)
        assert title(out) == "○"
        assert not by_label(out, "Mark seen") and by_label(out, "Retry")

    def test_finished_jobs_have_no_actions_and_running_ones_open_the_log(self):
        out = render([job("done", id="d"), job("running", id="r")], actions=ACT)
        acts = [l for l, p, a in menu_items(out) if "—" in l]
        assert len(acts) == 1 and acts[0].startswith("⟳")

    def test_plugin_has_no_plan_or_arm_entries_yet(self):
        out = render([self._failed()], actions=ACT)
        assert "Plan a transcript" not in out and "Arm current" not in out


class TestFailuresAreNotInvisible:
    """terminal=false hides stderr; --notify-errors turns a failure into a notification."""

    def test_failing_jobs_action_notifies_and_still_returns_an_error(self, monkeypatch, tmp_path):
        monkeypatch.setattr(jobs, "JOBS_DIR", tmp_path)
        seen = []
        monkeypatch.setattr(autocapture, "notify", lambda *a, **k: seen.append(a))
        assert jobs_cli.main(["jobs", "retry", "nope", "--notify-errors"], host=None) == 2
        assert len(seen) == 1 and "retry" in seen[0][1]

    def test_without_the_flag_a_terminal_user_gets_no_popup(self, monkeypatch, tmp_path):
        monkeypatch.setattr(jobs, "JOBS_DIR", tmp_path)
        monkeypatch.setattr(autocapture, "notify", lambda *a, **k: pytest.fail("notified"))
        assert jobs_cli.main(["jobs", "retry", "nope"], host=None) == 2

    def test_display_action_on_an_unknown_monitor_notifies(self, monkeypatch, tmp_path):
        import plaud_transcribe
        monkeypatch.setattr(autocapture, "STATE_DIR", tmp_path)
        monkeypatch.setattr(autocapture, "OVERRIDES_FILE", tmp_path / "displays.json")
        monkeypatch.setattr(autocapture, "_screens", lambda: (None, ["Only One"]))
        monkeypatch.setattr(plaud_transcribe, "load_config", lambda **k: {})
        monkeypatch.setattr(sys, "argv", ["autocapture.py", "display", "Gone", "off", "--notify-errors"])
        seen = []
        monkeypatch.setattr(autocapture, "notify", lambda *a, **k: seen.append(a))
        with pytest.raises(SystemExit):
            autocapture.main()
        assert len(seen) == 1 and "unknown display" in seen[0][1]
        assert not (tmp_path / "displays.json").exists()


class TestInstallRecordsTheInterpreter:
    def test_install_writes_the_sidecar_the_plugin_reads(self, monkeypatch, tmp_path):
        monkeypatch.setattr(jobs_cli.subprocess, "run",
                            lambda cmd, **kw: subprocess.CompletedProcess(cmd, 1, stdout=""))
        jobs_cli.main(["install-swiftbar", "--dir", str(tmp_path / "plugins")], host=None)
        cfg = json.loads((tmp_path / "swiftbar.json").read_text())
        assert cfg == {"python": sys.executable, "repo": str(jobs_cli.REPO_DIR)}
        assert plug.load_actions(tmp_path / "swiftbar.json") == cfg

    def test_relative_or_garbage_config_means_no_actions(self, tmp_path):
        f = tmp_path / "c.json"
        f.write_text('{"python": "python3", "repo": "x"}')
        assert plug.load_actions(f) is None
        f.write_text("{nope")
        assert plug.load_actions(f) is None
