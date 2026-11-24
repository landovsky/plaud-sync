import importlib.util
import subprocess
import sys
import time
from pathlib import Path

import pytest

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


def _iso(age_s=0):
    return time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime(NOW - age_s))


def job(state="done", age=60, **kw):
    j = {"id": kw.pop("id", f"j{int(age)}-{state}"), "schema": 1, "created": _iso(age),
         "recordings": ["plaud:abc"], "project": "hriste", "state": state, "steps": [],
         "result": {"delivered": None}, "error": None, "seen": False}
    j.update(kw)
    return j


def render(jobs_=(), status=None, pid_alive=True, paused=False, overrides=None):
    return plug.render(NOW, list(jobs_), status if status is not None else {"state": "watching"},
                       pid_alive, paused, overrides or {})


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
        assert len(out.split("Jobs\n")[1].strip().splitlines()) == plug.MAX_JOBS
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
