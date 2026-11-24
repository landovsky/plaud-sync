import builtins
import io
import json
import subprocess
from types import SimpleNamespace

import pytest

import plaud_transcribe as pt
import visuals

START_MS = 1_795_000_000_000  # 2026-11-18, any time
REC_A = {"id": "plaud:aaa", "source": "plaud", "filename": "Standup", "start_time": START_MS,
         "duration": 20 * 60 * 1000}
REC_B = {"id": "plaud:bbb", "source": "plaud", "filename": "Standup part 2",
         "start_time": START_MS + 30 * 60 * 1000, "duration": 10 * 60 * 1000}
TRANSCRIPTION = {"words": [{"type": "word", "speaker_id": "s0", "text": "hi",
                      "start": 0.0, "end": 1.0}]}


def _classification(project="Alpha"):
    return {"name": "Weekly sync", "type": "meeting", "project": project, "tags": ["x"],
            "language": "en", "summary": "# Weekly sync\n\nThey agreed to ship Friday.\n\n- a",
            "clean_transcript": ""}


@pytest.fixture
def env(monkeypatch, tmp_path):
    """A pipeline whose every outside touchpoint (paid APIs, network, home dir,
    repo working dirs) is stubbed or pointed at tmp_path."""
    for name, sub in (("STATE_FILE", "state.json"), ("CONFIG_FILE", "config.json")):
        monkeypatch.setattr(pt, name, tmp_path / "cfg" / sub)
    monkeypatch.setattr(pt, "CONFIG_DIR", tmp_path / "cfg")
    monkeypatch.setattr(pt, "WORKING_DIR", tmp_path / "work")
    monkeypatch.setattr(pt, "AUDIO_DIR", tmp_path / "audio")
    (tmp_path / "audio").mkdir()
    for rid in ("aaa", "bbb"):
        (tmp_path / "audio" / f"plaud:{rid}.mp3").write_bytes(b"x")
    calls = SimpleNamespace(summaries=[], transcribed=[])

    def fake_transcribe(path, language="cs", config=None):
        calls.transcribed.append(path)
        return TRANSCRIPTION

    def fake_summarize(text, meta, projects, language="en", **kw):
        calls.summaries.append(kw)
        return _classification(project=calls.pick)

    calls.pick = "Beta"  # the classifier disagrees with the forced project
    monkeypatch.setattr(pt, "transcribe_audio", fake_transcribe)
    monkeypatch.setattr(pt, "fetch_bookmarks", lambda *a, **k: [])
    monkeypatch.setattr(pt, "claude_summarize", fake_summarize)
    monkeypatch.setattr(pt, "download_audio",
                        lambda *a, **k: pytest.fail("audio is cached; no download expected"))
    projects = {"Alpha": str(tmp_path / "alpha"), "Beta": str(tmp_path / "beta")}
    config = {"timezone": "UTC", "default_output": str(tmp_path / "default")}
    return SimpleNamespace(calls=calls, projects=projects, config=config, tmp=tmp_path)


def run(env, state=None, **kw):
    state = {} if state is None else state
    result = pt.process_recording(REC_A, "tok", env.config, {}, env.projects, state, "en", **kw)
    return result, state


class TestProjectChosenUpFront:
    """A queued job names its project when it is enqueued; the classifier used to
    be free to file the transcript somewhere else, and `move` was the cleanup."""

    def test_forced_project_wins_over_the_classifiers_pick_so_no_move_is_needed(self, env):
        result, state = run(env, forced_project="Alpha")

        assert result["project"] == "Alpha"
        assert result["output_dir"].startswith(env.projects["Alpha"])
        assert not (env.tmp / "beta").exists()
        assert state["transcribed"]["plaud:aaa"]["project"] == "Alpha"

    def test_front_matter_says_the_forced_project_not_the_classifiers(self, env):
        result, _ = run(env, forced_project="Alpha")

        meta = json.loads((env.tmp / "alpha" / result["output_dir"].split("/")[-1]
                           / "metadata.json").read_text())
        assert meta["project"] == "Alpha"

    def test_summary_is_still_produced_because_the_classifier_still_runs_for_name_and_summary(self, env):
        result, _ = run(env, forced_project="Alpha")

        assert result["name"] == "Weekly sync"
        summary = (env.tmp / "alpha").glob("*/summary.md")
        assert "ship Friday" in next(summary).read_text()
        assert env.calls.summaries[0]["forced_project"] == "Alpha"

    def test_without_a_forced_project_the_classifier_still_routes(self, env):
        result, _ = run(env)

        assert result["project"] == "Beta"

    def test_unknown_forced_project_fails_before_any_paid_call(self, env):
        with pytest.raises(ValueError, match="Nowhere"):
            run(env, forced_project="Nowhere")

        assert env.calls.transcribed == []

    def test_the_summarizer_prompt_names_the_forced_project_so_the_summary_does_not_argue(
            self, monkeypatch, tmp_path):
        monkeypatch.setattr(pt, "WORKING_DIR", tmp_path / "work")
        seen = {}

        def fake_run(cmd, input=None, **kw):
            seen["prompt"] = input
            return SimpleNamespace(returncode=1, stdout="", stderr="stub")

        monkeypatch.setattr(subprocess, "run", fake_run)
        meta = {"filename": "f", "duration": 60000, "date": "2026-11-18"}

        pt.claude_summarize("hello", meta, {"Alpha": "/a"}, forced_project="Alpha")
        assert 'belongs to the project "Alpha"' in seen["prompt"]

        pt.claude_summarize("hello", meta, {"Alpha": "/a"})
        assert "Project already decided" not in seen["prompt"]


class TestMergedSessionWithProject:
    """Two parts of one meeting, filed together under the job's project."""

    def test_forced_project_applies_to_every_part_in_state(self, env):
        state = {}
        result = pt.process_merged_recordings([REC_B, REC_A], "tok", env.config, {},
                                              env.projects, state, "en", forced_project="Alpha")

        assert result["project"] == "Alpha"
        assert result["output_dir"].startswith(env.projects["Alpha"])
        assert {state["transcribed"][i]["project"] for i in ("plaud:aaa", "plaud:bbb")} == {"Alpha"}
        assert [s["id"] for s in result["sources"]] == ["plaud:aaa", "plaud:bbb"]
        assert result["duration"] == REC_A["duration"] + REC_B["duration"]

    def test_merged_mode_has_no_cuts_parameter(self):
        import inspect
        assert "cuts" not in inspect.signature(pt.process_merged_recordings).parameters


class TestResultFeedsCommitMessagesAndHandoff:
    """K6 writes commit messages and K8 the hand-off text from this dict."""

    KEYS = {"file_id", "name", "title", "project", "date", "duration", "sources",
            "summary_first_line", "files", "output_dir"}

    def test_single_result_has_every_key_and_keeps_the_old_ones(self, env):
        result, _ = run(env, forced_project="Alpha")

        assert set(result) == self.KEYS
        assert result["file_id"] == "plaud:aaa"
        assert result["date"] == "2026-11-18"
        assert result["duration"] == REC_A["duration"]
        assert result["summary_first_line"] == "They agreed to ship Friday."
        assert {"summary.md", "transcript.md", "metadata.json"} <= set(result["files"])
        assert result["sources"] == [{"id": "plaud:aaa", "source": "plaud", "filename": "Standup"}]

    def test_merged_result_has_the_same_keys(self, env):
        result = pt.process_merged_recordings([REC_A, REC_B], "tok", env.config, {},
                                              env.projects, {}, "en", forced_project="Alpha")

        assert set(result) == self.KEYS


class TestNeverBlocksOnAPrompt:
    """The runner starts from launchd or a terminal; either way a prompt would
    hang the queue forever."""

    @pytest.fixture
    def dead_stdin(self, monkeypatch):
        def no_input(*a, **k):
            raise AssertionError("input() was called")

        closed = io.StringIO()
        closed.close()
        monkeypatch.setattr(builtins, "input", no_input)
        monkeypatch.setattr("sys.stdin", closed)

    def test_process_recording_runs_with_input_raising_and_stdin_closed(self, env, dead_stdin):
        result, _ = run(env, forced_project="Alpha", interactive=False)

        assert result["project"] == "Alpha"

    def test_phone_prompt_is_skipped_even_when_stdin_claims_to_be_a_tty(self, env, monkeypatch):
        """A runner started by hand from a terminal has a real TTY on stdin."""
        env.config["visuals"] = {"enabled": True}
        tty = SimpleNamespace(isatty=lambda: True)
        monkeypatch.setattr("sys.stdin", tty)
        monkeypatch.setattr("sys.stderr", SimpleNamespace(isatty=lambda: True, write=lambda s: 0,
                                                          flush=lambda: None))
        monkeypatch.setattr(visuals, "_device_state", lambda adb: "absent")
        monkeypatch.setattr(visuals, "_timed_input",
                            lambda *a: pytest.fail("_timed_input reached"))
        prompted = []

        def collect(recordings, config, work_dir, transcript):
            prompted.append(visuals._await_phone(None))
            return []

        monkeypatch.setattr(visuals, "collect", collect)

        run(env, forced_project="Alpha", interactive=False)

        assert prompted == [False]

    def test_the_interactive_switch_is_restored_after_the_run(self, env):
        run(env, forced_project="Alpha", interactive=False)

        assert visuals._interactive is True


class TestExplicitNonInteractiveSwitch:
    def _tty(self, monkeypatch):
        monkeypatch.setattr("sys.stdin", SimpleNamespace(isatty=lambda: True))
        monkeypatch.setattr("sys.stderr", SimpleNamespace(isatty=lambda: True, write=lambda s: 0,
                                                          flush=lambda: None))
        monkeypatch.setattr(visuals, "_device_state", lambda adb: "absent")
        monkeypatch.setattr(visuals, "_timed_input",
                            lambda *a: pytest.fail("_timed_input reached"))

    def test_set_interactive_false_beats_a_real_tty(self, monkeypatch):
        self._tty(monkeypatch)
        monkeypatch.setattr(visuals, "_interactive", True)
        visuals.set_interactive(False)
        try:
            assert visuals._await_phone(None) is False
        finally:
            visuals.set_interactive(True)

    def test_env_var_beats_a_real_tty_for_a_runner_started_from_a_terminal(self, monkeypatch):
        self._tty(monkeypatch)
        monkeypatch.setenv("PLAUD_NONINTERACTIVE", "1")

        assert visuals._await_phone(None) is False

    def test_timed_input_never_reads_stdin_when_off(self, monkeypatch):
        monkeypatch.setenv("PLAUD_NONINTERACTIVE", "1")
        monkeypatch.setattr("select.select", lambda *a: pytest.fail("select on stdin"))

        assert visuals._timed_input("? ", 1) is None

    def test_a_tty_user_is_still_prompted_by_default(self, monkeypatch):
        monkeypatch.delenv("PLAUD_NONINTERACTIVE", raising=False)
        monkeypatch.setattr(visuals, "_interactive", True)
        monkeypatch.setattr("sys.stdin", SimpleNamespace(isatty=lambda: True))
        monkeypatch.setattr("sys.stderr", SimpleNamespace(isatty=lambda: True))

        assert visuals.is_interactive() is True


class TestExistingCallersUnchanged:
    """cmd_process and cmd_file pass the first nine arguments positionally."""

    def test_positional_call_with_instructions_and_cuts_still_works(self, env):
        state = {}
        result = pt.process_recording(REC_A, "tok", env.config, {}, env.projects, state, "en",
                                      "be brief", [(0, 60)])

        assert result["project"] == "Beta"  # classifier routing as before
        assert state["transcribed"]["plaud:aaa"]["instructions"] == "be brief"

    def test_new_parameters_are_keyword_only(self, env):
        with pytest.raises(TypeError):
            pt.process_recording(REC_A, "tok", env.config, {}, env.projects, {}, "en",
                                 None, None, "Alpha")
