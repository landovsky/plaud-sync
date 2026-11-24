import json

import autocapture

LG, BUILTIN = "LG ULTRAWIDE", "Built-in Retina Display"
S = {"display_refresh_s": 300, "ignore_displays": ["built-in"], "wispr_poll_s": 1}


def _isolate(monkeypatch, tmp_path, names=(LG, BUILTIN)):
    monkeypatch.setattr(autocapture, "STATE_DIR", tmp_path)
    monkeypatch.setattr(autocapture, "STATUS_FILE", tmp_path / "status.json")
    monkeypatch.setattr(autocapture, "OVERRIDES_FILE", tmp_path / "displays.json")
    monkeypatch.setattr(autocapture, "_overrides_cache", {"key": None, "data": {}})
    calls = []
    monkeypatch.setattr(autocapture, "_screens", lambda: calls.append(1) or (None, list(names)))
    return calls


def _status(tmp_path):
    return json.loads((tmp_path / "status.json").read_text())


class TestMenuCanPreSetAMonitorBetweenMeetings:
    """The SwiftBar Displays submenu was empty exactly when Tomas wanted to switch a monitor
    off: status.json only carried `displays` while capturing."""

    def test_displays_are_recorded_the_moment_watching_starts(self, monkeypatch, tmp_path):
        calls = _isolate(monkeypatch, tmp_path)
        cache = {"t": None, "mt": None}

        assert autocapture._refresh_idle_displays(S, cache, now=1000.0) is True

        st = _status(tmp_path)
        assert st["state"] == "watching"
        assert st["displays"] == [{"name": LG, "captured": True, "source": "default"},
                                  {"name": BUILTIN, "captured": False, "source": "config"}]
        assert len(calls) == 1

    def test_between_refreshes_macos_is_not_asked_again(self, monkeypatch, tmp_path):
        calls = _isolate(monkeypatch, tmp_path)
        cache = {"t": None, "mt": None}
        autocapture._refresh_idle_displays(S, cache, now=1000.0)

        assert autocapture._refresh_idle_displays(S, cache, now=1000.0 + 299) is False
        assert len(calls) == 1
        assert autocapture._refresh_idle_displays(S, cache, now=1000.0 + 301) is True
        assert len(calls) == 2

    def test_a_toggle_shows_in_the_menu_at_once_not_after_five_minutes(self, monkeypatch, tmp_path):
        calls = _isolate(monkeypatch, tmp_path)
        cache = {"t": None, "mt": None}
        autocapture._refresh_idle_displays(S, cache, now=1000.0)

        autocapture.set_override(LG, False)
        assert autocapture._refresh_idle_displays(S, cache, now=1001.0) is True

        shown = {d["name"]: d for d in _status(tmp_path)["displays"]}
        assert shown[LG] == {"name": LG, "captured": False, "source": "override"}
        assert len(calls) == 2   # one JXA call per refresh

    def test_a_monitor_replugged_after_the_refresh_window_is_picked_up(self, monkeypatch, tmp_path):
        _isolate(monkeypatch, tmp_path, names=(LG,))
        cache = {"t": None, "mt": None}
        autocapture._refresh_idle_displays(S, cache, now=0.0)
        assert [d["name"] for d in _status(tmp_path)["displays"]] == [LG]
        monkeypatch.setattr(autocapture, "_screens", lambda: (None, [LG, BUILTIN]))
        autocapture._refresh_idle_displays(S, cache, now=400.0)
        assert [d["name"] for d in _status(tmp_path)["displays"]] == [LG, BUILTIN]

    def test_default_interval_is_configurable_and_documented(self):
        assert autocapture.settings({})["display_refresh_s"] == 300
        cfg = {"visuals": {"autocapture": {"display_refresh_s": 60}}}
        assert autocapture.settings(cfg)["display_refresh_s"] == 60
        assert "display_refresh_s" in autocapture.build_parser().epilog


class TestWatchLoopKeepsDisplaysFresh:
    def test_watching_with_no_meeting_still_publishes_the_displays(self, monkeypatch, tmp_path):
        _isolate(monkeypatch, tmp_path)
        monkeypatch.setattr(autocapture, "LOG_FILE", tmp_path / "autocapture.log")
        monkeypatch.setattr(autocapture, "notify", lambda *a, **k: None)
        monkeypatch.setattr(autocapture, "wispr_live_meeting", lambda *a, **k: None)
        monkeypatch.setattr(autocapture.time, "sleep", lambda s: None)
        writes = []
        real = autocapture._write_status
        monkeypatch.setattr(autocapture, "_write_status", lambda **f: writes.append(f) or real(**f))
        ticks = iter([False, True])

        autocapture.watch({"visuals": {"autocapture": {"ignore_displays": ["built-in"]}}},
                          lambda: next(ticks, True))

        with_displays = [w for w in writes if w.get("displays")]
        assert with_displays and with_displays[0]["state"] == "watching"
        assert [d["name"] for d in with_displays[0]["displays"]] == [LG, BUILTIN]
