import autocapture


def _isolate(monkeypatch, tmp_path, running_pid=None):
    monkeypatch.setattr(autocapture, "STATE_DIR", tmp_path)
    monkeypatch.setattr(autocapture, "LOG_FILE", tmp_path / "autocapture.log")
    monkeypatch.setattr(autocapture, "PAUSED_FILE", tmp_path / "paused")
    monkeypatch.setattr(autocapture, "_running_pid", lambda: running_pid)
    launches = []
    monkeypatch.setattr(autocapture, "_launch_watch", lambda: launches.append(1) or "via test")
    return launches


class TestWatcherSurvivesRestarts:
    """The watcher was started by hand only; a reboot on 2026-10-27 ended it and
    the 10-28 and 10-29 meetings were recorded with no screens at all."""

    def test_agent_restarts_a_watcher_that_a_reboot_or_crash_ended(self, monkeypatch, tmp_path):
        launches = _isolate(monkeypatch, tmp_path, running_pid=None)

        autocapture.ensure()

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
