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
        assert p["ProgramArguments"][0] == jobs_cli.sys.executable
        assert p["ProgramArguments"][1].endswith("plaud_transcribe.py")
        assert p["ProgramArguments"][2:] == ["runner"]
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
                ["uninstall-runner"]]

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
        with pytest.raises(SystemExit) as e:
            jobs_cli.main(argv + ["--help"], host=pt)
        assert e.value.code == 0
        assert capsys.readouterr().out.strip()

    def test_install_help_lists_the_interval_flag_and_a_recipe(self, capsys):
        with pytest.raises(SystemExit):
            jobs_cli.main(["install-runner", "--help"], host=pt)
        out = capsys.readouterr().out
        assert "--interval" in out and "recipes:" in out
