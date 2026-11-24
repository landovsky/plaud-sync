import json
import os
import importlib

import pytest

import autocapture

LG, BUILTIN = "LG ULTRAWIDE", "Built-in Retina Display"


def _isolate(monkeypatch, tmp_path):
    monkeypatch.setattr(autocapture, "STATE_DIR", tmp_path)
    monkeypatch.setattr(autocapture, "OVERRIDES_FILE", tmp_path / "displays.json")
    monkeypatch.setattr(autocapture, "_overrides_cache", {"key": None, "data": {}})


class TestOverrideBeatsConfig:
    """ignore_displays is read at start; the override is the 'just for now' switch
    on top of it and must win in both directions."""

    def test_override_off_skips_a_monitor_config_would_capture(self):
        assert autocapture.display_state(LG, [], {LG: False}) == (False, "override")

    def test_override_on_captures_a_monitor_config_would_skip(self):
        assert autocapture.display_state(BUILTIN, ["built-in"], {BUILTIN: True}) == (True, "override")

    def test_without_override_config_decides_and_says_so(self):
        assert autocapture.display_state(BUILTIN, ["built-in"], {}) == (False, "config")
        assert autocapture.display_state(LG, ["built-in"], {}) == (True, "default")

    def test_unknown_name_is_captured_rather_than_losing_the_meeting(self):
        assert autocapture.display_state(None, ["built-in"], {LG: False}) == (True, "default")


class TestOverridesSurviveRestart:
    """F2: a reboot or watcher restart must not forget the toggle."""

    def test_override_written_before_a_restart_is_read_after_it(self, monkeypatch, tmp_path):
        _isolate(monkeypatch, tmp_path)
        autocapture.set_override(LG, False)
        # a fresh process starts with an empty cache
        monkeypatch.setattr(autocapture, "_overrides_cache", {"key": None, "data": {}})
        assert autocapture.load_overrides() == {LG: False}
        assert json.loads((tmp_path / "displays.json").read_text()) == {LG: False}

    def test_write_is_atomic_so_the_loop_never_reads_half_a_file(self, monkeypatch, tmp_path):
        _isolate(monkeypatch, tmp_path)
        autocapture.set_override(LG, True)
        assert not (tmp_path / "displays.json.tmp").exists()
        assert not list(tmp_path.glob("*.tmp"))

    def test_corrupt_file_means_no_overrides_not_a_crashed_capture(self, monkeypatch, tmp_path):
        _isolate(monkeypatch, tmp_path)
        (tmp_path / "displays.json").write_text("{nope")
        assert autocapture.load_overrides() == {}


class TestToggleMidCapture:
    """F1: the loop calls load_overrides every sample; an edit must show up
    without a restart."""

    def test_mtime_change_is_picked_up_without_restart(self, monkeypatch, tmp_path):
        _isolate(monkeypatch, tmp_path)
        f = tmp_path / "displays.json"
        f.write_text(json.dumps({LG: False}))
        assert autocapture.load_overrides() == {LG: False}
        f.write_text(json.dumps({LG: True}))
        os.utime(f, ns=(1, f.stat().st_mtime_ns + 10**9))
        assert autocapture.load_overrides() == {LG: True}

    def test_unchanged_mtime_does_not_reparse(self, monkeypatch, tmp_path):
        _isolate(monkeypatch, tmp_path)
        autocapture.set_override(LG, False)
        autocapture.load_overrides()
        monkeypatch.setattr(autocapture.json, "loads", lambda *_: pytest.fail("re-parsed"))
        assert autocapture.load_overrides() == {LG: False}

    def test_deleting_the_file_clears_overrides(self, monkeypatch, tmp_path):
        _isolate(monkeypatch, tmp_path)
        autocapture.set_override(LG, False)
        autocapture.load_overrides()
        (tmp_path / "displays.json").unlink()
        assert autocapture.load_overrides() == {}


class TestUnpluggedMonitor:
    """F3: keyed by name, so a monitor that returns after a trip keeps its setting."""

    def test_override_stays_in_the_file_while_the_monitor_is_away(self, monkeypatch, tmp_path):
        _isolate(monkeypatch, tmp_path)
        autocapture.set_override(LG, False)
        autocapture.set_override(BUILTIN, True)  # other toggles must not drop it
        assert autocapture.load_overrides()[LG] is False
        assert autocapture.display_state(LG, [], autocapture.load_overrides()) == (False, "override")

    def test_the_override_follows_the_name_when_display_numbers_shuffle(self):
        ov = {LG: False}
        before = [BUILTIN, LG]
        after = [LG, BUILTIN]
        assert [autocapture.display_state(n, [], ov)[0] for n in before] == [True, False]
        assert [autocapture.display_state(n, [], ov)[0] for n in after] == [False, True]


class TestDisplayCommand:
    """`display 2 off` is typed from the number `displays` printed, but the NAME is stored."""

    def test_number_resolves_to_the_name(self):
        assert autocapture.resolve_display("2", [BUILTIN, LG]) == LG

    def test_exact_name_is_accepted(self):
        assert autocapture.resolve_display(LG, [BUILTIN, LG]) == LG

    def test_unknown_target_lists_current_displays(self):
        with pytest.raises(ValueError, match="LG ULTRAWIDE"):
            autocapture.resolve_display("3", [BUILTIN, LG])
        with pytest.raises(ValueError, match="current displays"):
            autocapture.resolve_display("Dell", [BUILTIN, LG])

    def _run(self, monkeypatch, tmp_path, argv, names):
        _isolate(monkeypatch, tmp_path)
        monkeypatch.setattr(autocapture, "_screens", lambda: (1, names))
        import plaud_transcribe
        monkeypatch.setattr(plaud_transcribe, "load_config", lambda **k: {})
        monkeypatch.setattr(autocapture.sys, "argv", ["autocapture.py", *argv])
        autocapture.main()

    def test_display_off_by_number_stores_the_name(self, monkeypatch, tmp_path, capsys):
        self._run(monkeypatch, tmp_path, ["display", "2", "off"], [BUILTIN, LG])
        assert json.loads((tmp_path / "displays.json").read_text()) == {LG: False}

    def test_displays_shows_effective_state_and_source(self, monkeypatch, tmp_path, capsys):
        _isolate(monkeypatch, tmp_path)
        autocapture.set_override(LG, False)
        self._run(monkeypatch, tmp_path, ["displays"], [BUILTIN, LG])
        out = capsys.readouterr().out
        assert any(l.startswith("2") and "ignored" in l and "override" in l for l in out.splitlines())
        assert any(l.startswith("1") and "captured" in l and "default" in l for l in out.splitlines())

    def test_help_runs_nothing(self, monkeypatch, tmp_path, capsys):
        _isolate(monkeypatch, tmp_path)
        for fn in ("_screens", "set_override", "_jxa"):
            monkeypatch.setattr(autocapture, fn, lambda *a, **k: pytest.fail("ran"))
        monkeypatch.setattr(autocapture.sys, "argv", ["autocapture.py", "display", "--help"])
        with pytest.raises(SystemExit) as e:
            autocapture.main()
        assert e.value.code == 0
        assert not (tmp_path / "displays.json").exists()

    def test_epilog_has_the_switch_off_recipe(self, monkeypatch, capsys):
        monkeypatch.setattr(autocapture.sys, "argv", ["autocapture.py", "--help"])
        with pytest.raises(SystemExit):
            autocapture.main()
        assert "display 2 off" in capsys.readouterr().out


class TestStatusDisplays:
    """The menu bar plugin reads status.json instead of calling JXA."""

    def test_shape_is_name_captured_source_per_display(self):
        got = autocapture._displays_status([BUILTIN, LG], ["built-in"], {LG: False})
        assert got == [{"name": BUILTIN, "captured": False, "source": "config"},
                       {"name": LG, "captured": False, "source": "override"}]

    def test_unknown_names_give_an_empty_list(self):
        assert autocapture._displays_status([], ["x"], {}) == []
