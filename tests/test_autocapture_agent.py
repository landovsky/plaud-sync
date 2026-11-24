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
