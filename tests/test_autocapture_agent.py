import os
from pathlib import Path

import autocapture


def _isolate(monkeypatch, tmp_path, running_pid=None):
    monkeypatch.setattr(autocapture, "STATE_DIR", tmp_path)
    monkeypatch.setattr(autocapture, "LOG_FILE", tmp_path / "autocapture.log")
    monkeypatch.setattr(autocapture, "PAUSED_FILE", tmp_path / "paused")
    monkeypatch.setattr(autocapture, "_running_pid", lambda: running_pid)
    launches = []
    monkeypatch.setattr(autocapture, "_launch_watch", lambda: launches.append(1) or "via test")
    monkeypatch.setattr(autocapture, "notify", lambda *a, **k: None)
    return launches


class TestWatcherSurvivesRestarts:
    """The watcher was started by hand only; a reboot on 2026-10-27 ended it and
    the 10-28 and 10-29 meetings were recorded with no screens at all."""

    def test_agent_restarts_a_watcher_that_a_reboot_or_crash_ended(self, monkeypatch, tmp_path):
        launches = _isolate(monkeypatch, tmp_path, running_pid=None)
        alive = iter([None, 4242])  # not running before the launch, alive after it
        monkeypatch.setattr(autocapture, "_running_pid", lambda: next(alive))

        autocapture.ensure(wait_s=5)

        assert launches == [1]
        assert "watcher was not running" in (tmp_path / "autocapture.log").read_text()

    def test_agent_leaves_a_live_watcher_alone_so_a_meeting_is_not_split_into_two_sessions(
            self, monkeypatch, tmp_path):
        launches = _isolate(monkeypatch, tmp_path, running_pid=4242)

        autocapture.ensure()

        assert launches == []

    def test_a_deliberate_stop_is_not_undone_by_the_agent_minutes_later(self, monkeypatch, tmp_path):
        launches = _isolate(monkeypatch, tmp_path, running_pid=None)
        (tmp_path / "paused").touch()

        autocapture.ensure()

        assert launches == []


class TestUserHearsWhenTheRestartDidNotTakeHold:
    """`open` returns before the app runs, so a watcher that dies on start (lost
    Screen Recording grant after an app rebuild, broken Homebrew Python) looked
    restarted while every meeting went without screens. Tomas wants a macOS
    notification instead of finding out from an empty session days later."""

    def _alerts(self, monkeypatch):
        sent = []
        monkeypatch.setattr(autocapture, "notify", lambda title, msg, **k: sent.append((title, msg)))
        return sent

    def test_a_watcher_that_never_comes_up_raises_a_notification(self, monkeypatch, tmp_path):
        launches = _isolate(monkeypatch, tmp_path, running_pid=None)
        sent = self._alerts(monkeypatch)

        autocapture.ensure(wait_s=0)

        assert launches == [1]
        assert sent and "not running" in sent[0][0]
        assert "NOT running" in (tmp_path / "autocapture.log").read_text()

    def test_a_launch_that_fails_outright_still_reaches_the_user(self, monkeypatch, tmp_path):
        _isolate(monkeypatch, tmp_path, running_pid=None)
        sent = self._alerts(monkeypatch)

        def broken_open():
            raise OSError("open: app bundle missing")
        monkeypatch.setattr(autocapture, "_launch_watch", broken_open)

        autocapture.ensure(wait_s=0)

        assert sent and "app bundle missing" in sent[0][1]

    def test_a_successful_restart_stays_quiet_so_alerts_keep_meaning_trouble(self, monkeypatch, tmp_path):
        _isolate(monkeypatch, tmp_path, running_pid=None)
        sent = self._alerts(monkeypatch)
        alive = iter([None, 4242])
        monkeypatch.setattr(autocapture, "_running_pid", lambda: next(alive))

        autocapture.ensure(wait_s=5)

        assert sent == []

    def test_a_paused_watcher_is_not_reported_as_broken(self, monkeypatch, tmp_path):
        _isolate(monkeypatch, tmp_path, running_pid=None)
        sent = self._alerts(monkeypatch)
        (tmp_path / "paused").touch()

        autocapture.ensure(wait_s=0)

        assert sent == []


class TestLoginItemHasARecognisableName:
    """macOS Login Items names an agent by ProgramArguments[0]; Tomas will not
    approve an anonymous python3.15 login item, so the agent runs a named script."""

    def _install(self, monkeypatch, tmp_path):
        import subprocess
        monkeypatch.setattr(autocapture, "STATE_DIR", tmp_path / "state")
        monkeypatch.setattr(autocapture, "LOG_FILE", tmp_path / "state" / "autocapture.log")
        monkeypatch.setattr(autocapture, "PAUSED_FILE", tmp_path / "state" / "paused")
        monkeypatch.setattr(autocapture, "AGENT_PLIST", tmp_path / "LaunchAgents" / "x.plist")
        calls = []
        monkeypatch.setattr(subprocess, "run", lambda *a, **k: calls.append(a[0]))
        bin_dir = tmp_path / "bin"
        autocapture.install_agent(300, bin_dir=str(bin_dir))
        return bin_dir / "plaud-autocapture-watchdog", calls

    def test_plist_points_at_the_named_watchdog_not_python(self, monkeypatch, tmp_path):
        import plistlib
        script, _ = self._install(monkeypatch, tmp_path)
        plist = plistlib.loads((tmp_path / "LaunchAgents" / "x.plist").read_bytes())

        assert plist["ProgramArguments"] == [str(script)]
        assert not Path(plist["ProgramArguments"][0]).name.startswith("python")
        assert plist["RunAtLoad"] is True and plist["StartInterval"] == 300

    def test_watchdog_is_executable_and_execs_ensure_with_this_interpreter(self, monkeypatch, tmp_path):
        import sys
        script, _ = self._install(monkeypatch, tmp_path)
        lines = script.read_text().splitlines()

        assert os.access(script, os.X_OK) and (script.stat().st_mode & 0o777) == 0o755
        assert lines[0] == "#!/bin/bash" and "Plaud autocapture watchdog" in lines[1]
        assert lines[-1] == (f'exec "{sys.executable}" "{Path(autocapture.__file__).resolve()}" ensure "$@"')

    def test_uninstall_removes_the_script_so_no_stale_login_item_target_remains(self, monkeypatch, tmp_path):
        script, _ = self._install(monkeypatch, tmp_path)

        autocapture.uninstall_agent(bin_dir=str(script.parent))

        assert not script.exists()
        assert not (tmp_path / "LaunchAgents" / "x.plist").exists()

    def test_bin_dir_env_var_overrides_the_default(self, monkeypatch, tmp_path):
        monkeypatch.setenv("PLAUD_AGENT_BIN_DIR", str(tmp_path / "envbin"))

        assert autocapture._agent_bin_dir() == tmp_path / "envbin"
