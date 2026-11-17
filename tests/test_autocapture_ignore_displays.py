import json

import autocapture


class TestTheLaptopPanelIsNeverCaptured:
    """Meetings are shared on the external monitor; the MacBook panel holds the
    terminal, chat and notes, which must not end up in a meeting's visuals.
    Monitors are matched by name because screencapture's numbers follow which
    display is main and shift when one is plugged in."""

    def test_part_of_the_name_is_enough_so_config_survives_a_new_macbook(self):
        assert autocapture._ignored_display("Built-in Retina Display", ["built-in"])

    def test_the_external_monitor_is_still_captured(self):
        assert not autocapture._ignored_display("PHL 272B7QU", ["built-in"])

    def test_nothing_is_ignored_unless_configured(self):
        assert autocapture.settings({})["ignore_displays"] == []
        assert not autocapture._ignored_display("Built-in Retina Display", [])

    def test_an_unknown_name_is_captured_rather_than_losing_the_meeting(self):
        assert not autocapture._ignored_display(None, ["built-in"])


class TestIgnoringADisplayEndsItsScene:
    """Without an end, the last scene on a display would count as on screen for
    the whole time it was ignored, and pointing words then would select it."""

    def test_the_scene_before_the_switch_ends_when_the_display_is_ignored(self, tmp_path):
        sess = tmp_path / "2026-11-17_150000"
        sess.mkdir()
        (sess / "s1.jpg").write_bytes(b"x")
        events = [
            {"t": 100, "event": "start"},
            {"t": 110, "display": 1, "scene": "s1.jpg"},
            {"t": 200, "display": 1, "event": "ignored", "name": "Built-in Retina Display"},
            {"t": 900, "event": "stop"},
        ]
        (sess / "timeline.jsonl").write_text("".join(json.dumps(e) + "\n" for e in events))

        intervals, _ = autocapture._intervals(tmp_path, 0, 10**10)

        assert [(d, t0, t1) for d, t0, t1, _, _ in intervals] == [(1, 110, 200)]
