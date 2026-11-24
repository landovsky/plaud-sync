"""`plaud-transcribe plan` on a Python without Textual (Homebrew's): it must find a Python
that has it, or say how to get one. Nothing here needs Textual or the network."""
import builtins
import os
import sys

import pytest

import jobs_cli
import plaud_transcribe as pt


@pytest.fixture
def no_textual(monkeypatch):
    real = builtins.__import__

    def fake(name, *a, **k):
        if name == "textual" or name.startswith("textual."):
            raise ImportError("No module named 'textual'")
        return real(name, *a, **k)
    monkeypatch.setattr(builtins, "__import__", fake)
    monkeypatch.setenv("PLAUD_PLAN_REEXEC", "x")   # cmd_plan sets it itself: make monkeypatch undo that
    monkeypatch.delenv("PLAUD_PLAN_REEXEC")
    monkeypatch.delenv("PLAUD_PLAN_PYTHON", raising=False)
    monkeypatch.setattr(pt, "CONFIG_FILE", pt.CONFIG_DIR / "nonexistent-config.json")


class TestHomebrewPythonHasNoTextual:
    """Textual lives in the repo's .venv, but SwiftBar and the shell start the system Python."""

    def test_it_re_runs_itself_with_the_interpreter_named_in_the_environment(
            self, no_textual, monkeypatch, tmp_path):
        py = tmp_path / "venvpython"
        py.write_text("")
        monkeypatch.setenv("PLAUD_PLAN_PYTHON", str(py))
        calls = []
        monkeypatch.setattr(os, "execv", lambda path, argv: calls.append((path, argv)))
        jobs_cli.main(["plan", "--days", "7"], host=pt)
        (path, argv), = calls
        assert path == str(py) and argv[1].endswith("plaud_transcribe.py")
        assert argv[2:] == ["plan", "--days", "7"]

    def test_config_plan_python_is_used_when_the_environment_names_none(
            self, no_textual, monkeypatch, tmp_path):
        py = tmp_path / "cfgpython"
        py.write_text("")
        (tmp_path / "config.json").write_text('{"plan": {"python": "%s"}}' % py)
        monkeypatch.setattr(pt, "CONFIG_FILE", tmp_path / "config.json")
        monkeypatch.setattr(jobs_cli, "REPO_DIR", tmp_path)   # no .venv here
        calls = []
        monkeypatch.setattr(os, "execv", lambda path, argv: calls.append(path))
        jobs_cli.main(["plan"], host=pt)
        assert calls == [str(py)]

    def test_the_repo_venv_is_the_last_resort(self, no_textual, monkeypatch, tmp_path):
        (tmp_path / ".venv" / "bin").mkdir(parents=True)
        (tmp_path / ".venv" / "bin" / "python").write_text("")
        monkeypatch.setattr(jobs_cli, "REPO_DIR", tmp_path)
        calls = []
        monkeypatch.setattr(os, "execv", lambda path, argv: calls.append(path))
        jobs_cli.main(["plan"], host=pt)
        assert calls == [str(tmp_path / ".venv" / "bin" / "python")]

    def test_a_second_hop_is_never_made_so_a_python_without_textual_cannot_loop(
            self, no_textual, monkeypatch, tmp_path, capsys):
        py = tmp_path / "p"
        py.write_text("")
        monkeypatch.setenv("PLAUD_PLAN_PYTHON", str(py))
        monkeypatch.setenv("PLAUD_PLAN_REEXEC", "1")
        monkeypatch.setattr(os, "execv", lambda *a: pytest.fail("looped"))
        assert jobs_cli.main(["plan"], host=pt) == 2
        assert "requirements-plan.txt" in capsys.readouterr().err

    def test_with_no_interpreter_at_all_it_prints_the_install_hint_and_points_to_enqueue(
            self, no_textual, monkeypatch, tmp_path, capsys):
        monkeypatch.setattr(jobs_cli, "REPO_DIR", tmp_path)
        monkeypatch.setattr(os, "execv", lambda *a: pytest.fail("nothing to run"))
        assert jobs_cli.main(["plan"], host=pt) == 2
        err = capsys.readouterr().err
        assert "requirements-plan.txt" in err and "enqueue" in err and sys.executable in err

    def test_plan_is_listed_in_the_top_level_help(self, monkeypatch, capsys):
        monkeypatch.setattr("sys.argv", ["plaud-transcribe", "--help"])
        with pytest.raises(SystemExit):
            pt.main()
        assert "plan" in capsys.readouterr().out


class TestArmLive:
    """`plan --arm-live` is what the menu's "Arm current meeting..." runs; it must survive the
    hop into the venv Python."""

    def test_the_flag_survives_the_re_exec_into_the_venv_python(self, no_textual, monkeypatch, tmp_path):
        py = tmp_path / "venvpython"
        py.write_text("")
        monkeypatch.setenv("PLAUD_PLAN_PYTHON", str(py))
        calls = []
        monkeypatch.setattr(os, "execv", lambda path, argv: calls.append(argv))
        jobs_cli.main(["plan", "--arm-live"], host=pt)
        assert calls[0][2:] == ["plan", "--days", "30", "--arm-live"]

    def test_help_documents_it_and_runs_nothing(self, monkeypatch, capsys):
        monkeypatch.setattr(os, "execv", lambda *a: pytest.fail("ran"))
        with pytest.raises(SystemExit):
            jobs_cli.main(["plan", "--help"], host=pt)
        out = capsys.readouterr().out
        assert "--arm-live" in out and "Arm current meeting" in out
