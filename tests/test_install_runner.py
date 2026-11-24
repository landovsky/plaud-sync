import os
import plistlib
import subprocess

import pytest

import jobs
import jobs_cli
import plaud_transcribe as pt
import runner


@pytest.fixture
def agent(monkeypatch, tmp_path):
    """The plist, log and jobs dir live in tmp_path and launchctl is recorded,
    never run: bootstrap on the real machine would start the real runner."""
    monkeypatch.setattr(jobs_cli, "RUNNER_PLIST", tmp_path / "LaunchAgents" / "r.plist")
    monkeypatch.setattr(jobs_cli, "RUNNER_LOG", tmp_path / "plaud" / "runner.log")
    monkeypatch.setattr(jobs, "JOBS_DIR", tmp_path / "jobs")
    monkeypatch.setenv("PLAUD_RUNNER_BIN_DIR", str(tmp_path / "bin"))
    calls = []

    def fake_run(cmd, **kw):
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0)

    monkeypatch.setattr(jobs_cli.subprocess, "run", fake_run)
    return calls


class TestRunnerStartsWithoutATerminal:
    """B1: a job enqueued with no terminal open must start the runner, and a
    reboot must resume it, so launchd needs the right triggers and a PATH that
    finds claude, gh, git and terminal-notifier."""

    def plist(self, tmp_path):
        with open(tmp_path / "LaunchAgents" / "r.plist", "rb") as f:
            return plistlib.load(f)

    def test_plist_triggers_at_login_on_job_files_and_on_a_timer(self, agent, tmp_path):
        jobs_cli.main(["install-runner"], host=pt)
        p = self.plist(tmp_path)
        assert p["Label"] == "local.plaud-sync.runner"
        assert p["RunAtLoad"] is True
        assert p["WatchPaths"] == [str(tmp_path / "jobs")]
        assert p["StartInterval"] == 60      # promotes armed jobs: no file changes when a meeting ends

    def test_plist_runs_the_runner_from_the_repo_so_the_summarizer_can_read_files(self, agent, tmp_path):
        jobs_cli.main(["install-runner"], host=pt)
        p = self.plist(tmp_path)
        assert p["ProgramArguments"] == [str(tmp_path / "bin" / "plaud-transcribe-runner")]
        script = (tmp_path / "bin" / "plaud-transcribe-runner").read_text()
        assert f'exec "{jobs_cli.sys.executable}" "{jobs_cli.REPO_DIR}/plaud_transcribe.py" runner "$@"' in script
        assert p["WorkingDirectory"] == str(jobs_cli.REPO_DIR)

    def test_launchd_path_includes_homebrew_and_local_bin(self, agent, tmp_path):
        jobs_cli.main(["install-runner"], host=pt)
        path = self.plist(tmp_path)["EnvironmentVariables"]["PATH"].split(os.pathsep)
        for d in ("/opt/homebrew/bin", os.path.expanduser("~/.local/bin"), "/usr/bin", "/bin",
                  "/usr/sbin", "/sbin"):
            assert d in path

    def test_log_goes_to_a_file_and_stdout_is_discarded(self, agent, tmp_path):
        jobs_cli.main(["install-runner"], host=pt)
        p = self.plist(tmp_path)
        assert p["StandardErrorPath"] == str(tmp_path / "plaud" / "runner.log")
        assert p["StandardOutPath"] == "/dev/null"

    def test_interval_flag_changes_the_timer(self, agent, tmp_path):
        jobs_cli.main(["install-runner", "--interval", "120"], host=pt)
        assert self.plist(tmp_path)["StartInterval"] == 120

    def test_jobs_dir_is_created_so_watchpaths_has_a_target(self, agent, tmp_path):
        assert not (tmp_path / "jobs").exists()
        jobs_cli.main(["install-runner"], host=pt)
        assert (tmp_path / "jobs").is_dir()

    def test_reinstall_boots_out_the_old_agent_before_bootstrapping_the_new(self, agent):
        jobs_cli.main(["install-runner"], host=pt)
        domain = f"gui/{os.getuid()}"
        assert agent[0] == ["launchctl", "bootout", f"{domain}/local.plaud-sync.runner"]
        assert agent[1][:3] == ["launchctl", "bootstrap", domain]

    def test_uninstall_removes_plist_and_boots_out(self, agent, tmp_path):
        jobs_cli.main(["install-runner"], host=pt)
        agent.clear()
        jobs_cli.main(["uninstall-runner"], host=pt)
        assert not (tmp_path / "LaunchAgents" / "r.plist").exists()
        assert agent[0][:2] == ["launchctl", "bootout"]

    def test_uninstall_without_an_installed_agent_is_fine(self, agent):
        assert jobs_cli.main(["uninstall-runner"], host=pt) == 0

    def test_a_nonsense_interval_is_rejected_before_anything_is_written(self, agent, tmp_path, capsys):
        assert jobs_cli.main(["install-runner", "--interval", "0"], host=pt) == 2
        assert agent == [] and not (tmp_path / "LaunchAgents" / "r.plist").exists()


class TestHelpNeverRunsAnything:
    """--help is how the next agent learns the recipes; it must be safe everywhere."""

    COMMANDS = [["enqueue"], ["jobs", "list"], ["jobs", "show", "x"], ["jobs", "cancel", "x"],
                ["jobs", "seen", "x"], ["jobs", "retry", "x"], ["runner"], ["install-runner"],
                ["uninstall-runner"], ["install-swiftbar"], ["uninstall-swiftbar"], ["plan"]]

    @pytest.mark.parametrize("argv", COMMANDS, ids=lambda a: " ".join(a))
    def test_help_exits_zero_and_calls_no_side_effect(self, argv, monkeypatch, capsys):
        def boom(*a, **k):
            pytest.fail("side effect during --help")
        monkeypatch.setattr(jobs, "save_job", boom)
        monkeypatch.setattr(runner, "run_queue", boom)
        monkeypatch.setattr(runner, "load_runner_env", boom)
        monkeypatch.setattr(runner, "acquire_lock", boom)
        monkeypatch.setattr(jobs_cli.subprocess, "run", boom)
        monkeypatch.setattr(jobs_cli, "install_runner", boom)
        monkeypatch.setattr(jobs_cli, "uninstall_runner", boom)
        monkeypatch.setattr(jobs_cli, "install_swiftbar", boom)
        monkeypatch.setattr(jobs_cli, "uninstall_swiftbar", boom)
        monkeypatch.setattr(jobs_cli, "cmd_plan", boom)
        with pytest.raises(SystemExit) as e:
            jobs_cli.main(argv + ["--help"], host=pt)
        assert e.value.code == 0
        assert capsys.readouterr().out.strip()

    def test_install_help_lists_the_interval_flag_and_a_recipe(self, capsys):
        with pytest.raises(SystemExit):
            jobs_cli.main(["install-runner", "--help"], host=pt)
        out = capsys.readouterr().out
        assert "--interval" in out and "recipes:" in out


class TestRunnerCanDecryptItsCredentials:
    """The runner loads the Claude token via direnv/sops; launchd has no shell, so without
    SOPS_AGE_KEY_FILE sops falls back to a different default key and decryption fails silently."""

    def env(self, tmp_path):
        with open(tmp_path / "LaunchAgents" / "r.plist", "rb") as f:
            return plistlib.load(f)["EnvironmentVariables"]

    @pytest.fixture(autouse=True)
    def clean_env(self, monkeypatch):
        for n in ("SOPS_AGE_KEY_FILE", "SOPS_CONFIG", "XDG_CONFIG_HOME", "SOPS_AGE_KEY",
                  "CLAUDE_CODE_OAUTH_TOKEN", "MY_PATH_VAR"):
            monkeypatch.delenv(n, raising=False)

    def test_plist_carries_the_age_key_file_so_sops_finds_the_right_key(self, agent, tmp_path, monkeypatch, capsys):
        monkeypatch.setenv("SOPS_AGE_KEY_FILE", "/home/me/age.txt")
        jobs_cli.main(["install-runner"], host=pt)
        assert self.env(tmp_path)["SOPS_AGE_KEY_FILE"] == "/home/me/age.txt"
        out = capsys.readouterr().out
        assert "SOPS_AGE_KEY_FILE" in out and "/home/me/age.txt" not in out   # names only

    def test_plist_omits_it_when_unset_rather_than_writing_an_empty_value(self, agent, tmp_path):
        jobs_cli.main(["install-runner"], host=pt)
        assert list(self.env(tmp_path)) == ["PATH"]

    def test_secret_values_in_the_environment_are_never_written_to_the_plist(self, agent, tmp_path, monkeypatch):
        monkeypatch.setenv("SOPS_AGE_KEY", "AGE-SECRET-KEY-1XYZ")
        monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "tok")
        jobs_cli.main(["install-runner"], host=pt)
        assert set(self.env(tmp_path)) == {"PATH"}

    @pytest.mark.parametrize("name", ["SOPS_AGE_KEY", "CLAUDE_CODE_OAUTH_TOKEN", "my_secret",
                                      "DB_PASSWORD", "API_KEY"])
    def test_env_flag_refuses_secret_looking_names_and_installs_nothing(self, agent, tmp_path, monkeypatch, name):
        monkeypatch.setenv(name, "v")
        assert jobs_cli.main(["install-runner", "--env", name], host=pt) != 0
        assert not (tmp_path / "LaunchAgents" / "r.plist").exists()
        assert agent == []   # launchctl never touched

    def test_env_flag_carries_extra_path_variables_by_name(self, agent, tmp_path, monkeypatch):
        monkeypatch.setenv("MY_PATH_VAR", "/x")
        jobs_cli.main(["install-runner", "--env", "MY_PATH_VAR"], host=pt)
        assert self.env(tmp_path)["MY_PATH_VAR"] == "/x"

    def test_help_tells_the_next_agent_why_the_variable_is_carried(self, capsys):
        with pytest.raises(SystemExit):
            jobs_cli.main(["install-runner", "--help"], host=pt)
        assert "the runner has no shell: install-runner carries SOPS_AGE_KEY_FILE" in capsys.readouterr().out


class TestLoginItemsShowsANamedItem:
    """macOS Login Items names an agent after ProgramArguments[0]; a bare python3.x item looks
    unknown and gets denied, so the plist points at a named launcher script instead."""

    def test_plist_program_is_the_named_launcher_not_a_python_binary(self, agent, tmp_path):
        jobs_cli.main(["install-runner"], host=pt)
        with open(tmp_path / "LaunchAgents" / "r.plist", "rb") as f:
            prog = plistlib.load(f)["ProgramArguments"]
        assert len(prog) == 1
        assert os.path.basename(prog[0]) == "plaud-transcribe-runner"
        assert not os.path.basename(prog[0]).startswith("python")

    def test_launcher_is_executable_and_says_who_installed_it(self, agent, tmp_path):
        jobs_cli.main(["install-runner"], host=pt)
        launcher = tmp_path / "bin" / "plaud-transcribe-runner"
        assert os.access(launcher, os.X_OK) and (launcher.stat().st_mode & 0o777) == 0o755
        lines = launcher.read_text().splitlines()
        assert lines[0] == "#!/bin/bash"
        assert "install-runner" in lines[1]
        assert str(jobs_cli.REPO_DIR) in lines[2] and str(tmp_path / "LaunchAgents" / "r.plist") in lines[2]
        assert not list((tmp_path / "bin").glob("*.tmp"))   # atomic write leaves no temp file

    def test_bin_dir_flag_overrides_the_environment(self, agent, tmp_path):
        jobs_cli.main(["install-runner", "--bin-dir", str(tmp_path / "elsewhere")], host=pt)
        assert (tmp_path / "elsewhere" / "plaud-transcribe-runner").exists()
        assert not (tmp_path / "bin" / "plaud-transcribe-runner").exists()

    def test_uninstall_removes_the_launcher_so_nothing_is_left_behind(self, agent, tmp_path):
        jobs_cli.main(["install-runner"], host=pt)
        jobs_cli.main(["uninstall-runner"], host=pt)
        assert not (tmp_path / "bin" / "plaud-transcribe-runner").exists()

    def test_help_documents_the_launcher_directory_override(self, capsys):
        with pytest.raises(SystemExit):
            jobs_cli.main(["install-runner", "--help"], host=pt)
        out = capsys.readouterr().out
        assert "--bin-dir" in out and "PLAUD_RUNNER_BIN_DIR" in out
