"""CLI for the job queue: `enqueue` and `jobs list|show|cancel|seen`.

Dispatched from plaud_transcribe.main() before any network call, with its own
argparse subparsers, so `--help` and `enqueue` never touch the Plaud API.
"""

import argparse
import json
import os
import plistlib
import subprocess
import sys
from pathlib import Path

import jobs

COMMANDS = ("enqueue", "jobs", "runner", "install-runner", "uninstall-runner",
            "install-swiftbar", "uninstall-swiftbar")

REPO_DIR = Path(__file__).resolve().parent
RUNNER_ID = "local.plaud-sync.runner"
RUNNER_PLIST = Path.home() / "Library" / "LaunchAgents" / f"{RUNNER_ID}.plist"
RUNNER_LOG = Path.home() / ".plaud-sync" / "runner.log"
DEFAULT_INTERVAL = 60
# launchd's own PATH has none of claude, gh, cc-tree, terminal-notifier or Homebrew git
AGENT_PATH = ["/opt/homebrew/bin", str(Path.home() / ".local" / "bin"),
              "/usr/local/bin", "/usr/bin", "/bin", "/usr/sbin", "/sbin"]

LAUNCHER_NAME = "plaud-transcribe-runner"   # what macOS Login Items shows for the agent
DEFAULT_BIN_DIR = Path.home() / ".plaud-sync" / "bin"

# carried from the installing shell into the plist: paths only, never secrets
CARRIED_ENV = ("SOPS_AGE_KEY_FILE", "SOPS_CONFIG", "XDG_CONFIG_HOME")
_SECRET_WORDS = ("TOKEN", "KEY", "SECRET", "PASSWORD")


def _refuse_secret_name(name):
    up = name.upper()
    if name != "SOPS_AGE_KEY_FILE" and any(w in up for w in _SECRET_WORDS):
        raise jobs.JobError(f"refusing to carry {name}: names containing TOKEN, KEY, SECRET or "
                            "PASSWORD hold secrets and must not be written to the plist "
                            "(only SOPS_AGE_KEY_FILE, a path, is allowed)")


def carried_env(extra=()):
    """Variables (name -> value) copied from this environment into the plist."""
    names = list(CARRIED_ENV)
    for n in extra:
        _refuse_secret_name(n)
        if n not in names:
            names.append(n)
    return {n: os.environ[n] for n in names if os.environ.get(n)}


INSTALL_EPILOG = """\
recipes:
  install the runner agent (starts at login, on a new job file, and every 60 s)
    plaud-transcribe install-runner
  poll less often (the interval is what promotes armed jobs when a meeting ends)
    plaud-transcribe install-runner --interval 120
  remove it
    plaud-transcribe uninstall-runner

the runner has no shell: install-runner carries SOPS_AGE_KEY_FILE so sops can decrypt
  (also SOPS_CONFIG and XDG_CONFIG_HOME when set; --env NAME adds more, repeatable).
  Names containing TOKEN, KEY, SECRET or PASSWORD (and SOPS_AGE_KEY) are refused.
  Only variable names are printed, never values.
    plaud-transcribe install-runner --env SOPS_AGE_RECIPIENTS

launcher: the agent runs ~/.plaud-sync/bin/plaud-transcribe-runner (a small bash script that
  execs python on plaud_transcribe.py runner) so macOS Login Items shows a named item,
  not "python3.x". Directory: --bin-dir or PLAUD_RUNNER_BIN_DIR. uninstall-runner removes it.

what it installs:
  ~/Library/LaunchAgents/local.plaud-sync.runner.plist running
  `python plaud_transcribe.py runner` from this repo, with RunAtLoad, WatchPaths
  on the jobs dir (PLAUD_JOBS_DIR / ~/.plaud-sync/jobs; created if missing) and
  StartInterval. Errors go to ~/.plaud-sync/runner.log. Re-triggers are harmless:
  a second runner exits at once while one holds the lock.
  Credentials come from `direnv exec` at runner start; run `direnv allow` in the
  repo once (see `plaud-transcribe runner --help`).
"""

ENQUEUE_EPILOG = """\
recipes:
  enqueue one recording to Hřiště and process it now
    plaud-transcribe enqueue --recording <id> --project "Hřiště Hrou" \\
        --note "pull the action items into the backlog"
  hand it over later, just tell the node a transcript is waiting
    plaud-transcribe enqueue --recording <id> --project "Hřiště Hrou" --mode later
  leave a stretch out (one recording, no --merge)
    plaud-transcribe enqueue --recording <id> --cut 29:16-34:55
  merge two recordings into one transcript
    plaud-transcribe enqueue --recording <a> --recording <b> --merge
  see the job without writing it
    plaud-transcribe enqueue --recording <id> --dry-run

defaults:
  With --project, push / node / deliver_ask come from that project in the config
  and --push / --node override them. Without --project they stay unset and are
  resolved after classification, when the transcript is committed.
  --mode defaults to 'now' when a node is known, else 'none'.
  Recording ids are checked for shape only; nothing is fetched at enqueue time.
  PLAUD_JOBS_DIR changes where job files go (default ~/.plaud-sync/jobs).
"""

JOBS_EPILOG = """\
recipes:
  plaud-transcribe jobs list                what is queued, running, failed
  plaud-transcribe jobs show 20261124-1702  full JSON (a unique id prefix works)
  plaud-transcribe jobs retry <id>          failed/held job back to queued, done steps kept
  plaud-transcribe jobs cancel <id>         only queued/failed/held/armed jobs
  plaud-transcribe jobs seen <id>           clear the warning, keep the job
"""


SWIFTBAR_EPILOG = """\
recipes:
  show autocapture state and the job queue in the menu bar
    plaud-transcribe install-swiftbar
  a non-default plugin folder
    plaud-transcribe install-swiftbar --dir ~/SwiftBarPlugins     (or env PLAUD_SWIFTBAR_DIR)
  remove it
    plaud-transcribe uninstall-swiftbar
The plugin reads local files only (jobs, autocapture status) and imports none of this repo.
Its menu actions (toggle a monitor, pause, retry/cancel/mark seen) run this repo's commands with
the Python that ran install-swiftbar. That path is recorded in ~/.plaud-sync/swiftbar.json
(env PLAUD_SWIFTBAR_CONFIG moves it): {"python": "<abs interpreter>", "repo": "<abs repo dir>"}.
Re-run install-swiftbar after moving the repo or switching Python; without the file the menu
shows no actions. Actions pass --notify-errors, so a failure becomes a notification.
"""


def build_parser():
    p = argparse.ArgumentParser(
        prog="plaud-transcribe",
        description="Job queue commands (offline: they only read/write job files).")
    sub = p.add_subparsers(dest="command", required=True, metavar="{enqueue,jobs,runner,install-runner,uninstall-runner,install-swiftbar,uninstall-swiftbar}")

    e = sub.add_parser(
        "enqueue", help="write a job file for the runner (no network)",
        description="Write a job file. Nothing is transcribed here; the runner does that.",
        epilog=ENQUEUE_EPILOG, formatter_class=argparse.RawDescriptionHelpFormatter)
    e.add_argument("--recording", action="append", default=[], metavar="ID",
                   help="recording id (plaud id, wispr:<uuid>, recorder:<n>, local:<hex>); repeatable")
    e.add_argument("--merge", action="store_true",
                   help="treat the recordings as one session (cannot be combined with --cut)")
    e.add_argument("--cut", action="append", default=[], metavar="REC=START-END",
                   help="leave a stretch of recording REC out, e.g. wispr:abc=29:16-34:55; repeatable")
    e.add_argument("--instructions", help="one-off steering for the summarizer")
    e.add_argument("--project", help="output project (name from config 'projects'); "
                                     "the classifier then does not route")
    e.add_argument("--push", choices=jobs.PUSH_MODES,
                   help="main | pr | none; default: the project's push mode")
    e.add_argument("--node", help="cc-tree ref to hand the transcript to (tree or tree:id); "
                                  "default: the project's node")
    e.add_argument("--mode", choices=jobs.DELIVER_MODES,
                   help="now: hand over and start processing; later: tell the node it is waiting; "
                        "none: no hand-off (default: now if a node is known, else none)")
    e.add_argument("--note", help="what the node should do with the transcript "
                                  "(overrides the project's deliver_ask)")
    e.add_argument("--dry-run", action="store_true", help="print the job JSON, write nothing")

    j = sub.add_parser("jobs", help="list and manage jobs",
                       description="List and manage job files.", epilog=JOBS_EPILOG,
                       formatter_class=argparse.RawDescriptionHelpFormatter)
    js = j.add_subparsers(dest="jobs_command", required=True, metavar="{list,show,retry,cancel,seen}")
    js.add_parser("list", help="one line per job", description="One line per job, oldest first.")
    for name, text in (("show", "print one job as JSON"),
                       ("retry", "requeue a failed or held job; steps already done are kept"),
                       ("cancel", "cancel a queued, failed, held or armed job"),
                       ("seen", "mark a failed/held job as seen; keeps the job")):
        s = js.add_parser(name, help=text, description=text)
        s.add_argument("id", help="job id or unique prefix")
        s.add_argument("--notify-errors", action="store_true",
                       help="show a macOS notification if this fails (the SwiftBar menu runs it "
                            "with terminal=false, where errors would be invisible)")
    sub.add_parser("runner", help="process queued jobs, then exit (see runner --help)",
                   add_help=False)
    i = sub.add_parser("install-runner", help="install the LaunchAgent that runs the runner",
                       description="Install and load the runner LaunchAgent.",
                       epilog=INSTALL_EPILOG, formatter_class=argparse.RawDescriptionHelpFormatter)
    i.add_argument("--interval", type=int, default=DEFAULT_INTERVAL, metavar="SECONDS",
                   help=f"also start the runner every N seconds (default {DEFAULT_INTERVAL}); "
                        "this is what picks up armed jobs")
    i.add_argument("--env", action="append", default=[], metavar="NAME",
                   help="also carry this environment variable into the agent (repeatable); "
                        "secret-looking names are refused")
    i.add_argument("--bin-dir", metavar="DIR", default=None,
                   help="where to write the named launcher script "
                        f"(env PLAUD_RUNNER_BIN_DIR; default {DEFAULT_BIN_DIR})")
    u = sub.add_parser("uninstall-runner", help="unload and remove the runner LaunchAgent",
                       description="Unload and remove the runner LaunchAgent and its launcher script.")
    u.add_argument("--bin-dir", metavar="DIR", default=None,
                   help="launcher directory used at install time (env PLAUD_RUNNER_BIN_DIR)")
    sw = sub.add_parser("install-swiftbar", help="symlink the SwiftBar menu-bar plugin into SwiftBar's plugin folder",
                        description="Symlink swiftbar/plaud.5s.py into SwiftBar's plugin folder. "
                                    "SwiftBar is optional: without it this prints a hint and exits 0.",
                        epilog=SWIFTBAR_EPILOG, formatter_class=argparse.RawDescriptionHelpFormatter)
    sw.add_argument("--dir", metavar="DIR", default=None,
                    help="SwiftBar plugin folder (env PLAUD_SWIFTBAR_DIR; default: SwiftBar's "
                         "PluginDirectory preference, else ~/Library/Application Support/SwiftBar/Plugins)")
    usw = sub.add_parser("uninstall-swiftbar", help="remove the SwiftBar plugin symlink",
                         description="Remove the plaud.5s.py symlink from SwiftBar's plugin folder.",
                         epilog=SWIFTBAR_EPILOG, formatter_class=argparse.RawDescriptionHelpFormatter)
    usw.add_argument("--dir", metavar="DIR", default=None,
                     help="plugin folder used at install time (env PLAUD_SWIFTBAR_DIR)")
    return p


SWIFTBAR_PLUGIN = REPO_DIR / "swiftbar" / "plaud.5s.py"
SWIFTBAR_DEFAULT_DIR = Path.home() / "Library" / "Application Support" / "SwiftBar" / "Plugins"
SWIFTBAR_APP_DIRS = (Path("/Applications/SwiftBar.app"), Path.home() / "Applications" / "SwiftBar.app")


def swiftbar_dir(explicit=None):
    """Plugin folder: --dir, then PLAUD_SWIFTBAR_DIR, then SwiftBar's own preference,
    then SwiftBar's default folder. Returns (path, found) - found is False when SwiftBar
    does not look installed and nothing was asked for explicitly."""
    chosen = explicit or os.environ.get("PLAUD_SWIFTBAR_DIR")
    if chosen:
        return Path(chosen).expanduser(), True
    try:
        r = subprocess.run(["defaults", "read", "com.ameba.SwiftBar", "PluginDirectory"],
                           capture_output=True, text=True, timeout=5)
        pref = r.stdout.strip() if r.returncode == 0 else ""
    except (OSError, subprocess.SubprocessError):
        pref = ""
    if pref:
        return Path(pref).expanduser(), True
    installed = any(p.exists() for p in SWIFTBAR_APP_DIRS) or SWIFTBAR_DEFAULT_DIR.is_dir()
    return SWIFTBAR_DEFAULT_DIR, installed


def swiftbar_config_path():
    return Path(os.environ.get("PLAUD_SWIFTBAR_CONFIG")
                or Path.home() / ".plaud-sync" / "swiftbar.json").expanduser()


def write_swiftbar_config():
    """Record the interpreter and repo the menu actions run: SwiftBar has no PATH and the
    plugin itself runs on /usr/bin/python3, which cannot import this repo's dependencies."""
    path = swiftbar_config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps({"python": sys.executable, "repo": str(REPO_DIR)}, indent=2) + "\n")
    os.replace(tmp, path)
    return path


def install_swiftbar(directory=None):
    target_dir, found = swiftbar_dir(directory)
    if not found:
        print("SwiftBar does not look installed (brew install --cask swiftbar), so no plugin was "
              "linked. Everything else works without it; run this again after installing, or pass "
              "--dir / PLAUD_SWIFTBAR_DIR.")
        return 0
    link = target_dir / SWIFTBAR_PLUGIN.name
    target_dir.mkdir(parents=True, exist_ok=True)
    print(f"menu actions will run {sys.executable} (recorded in {write_swiftbar_config()})")
    if link.is_symlink() or link.exists():
        if link.is_symlink() and Path(os.readlink(link)) == SWIFTBAR_PLUGIN:
            print(f"already linked: {link}")
            return 0
        if not link.is_symlink():
            raise jobs.JobError(f"{link} exists and is not a symlink: move it away first")
        link.unlink()
    link.symlink_to(SWIFTBAR_PLUGIN)
    print(f"linked {link} -> {SWIFTBAR_PLUGIN} (SwiftBar picks it up on its next refresh)")
    return 0


def uninstall_swiftbar(directory=None):
    target_dir, _ = swiftbar_dir(directory)
    link = target_dir / SWIFTBAR_PLUGIN.name
    swiftbar_config_path().unlink(missing_ok=True)
    if link.is_symlink():
        link.unlink()
        print(f"removed {link}")
    else:
        print(f"nothing to remove: {link} is not a symlink")
    return 0


def launcher_path(bin_dir=None):
    return Path(bin_dir or os.environ.get("PLAUD_RUNNER_BIN_DIR") or DEFAULT_BIN_DIR) / LAUNCHER_NAME


def write_launcher(path):
    """Named wrapper script so Login Items shows plaud-transcribe-runner, not python3.x."""
    path.parent.mkdir(parents=True, exist_ok=True)
    body = (f"#!/bin/bash\n"
            f"# plaud-sync runner launcher, written by `plaud-transcribe install-runner`\n"
            f"# repo: {REPO_DIR}  agent plist: {RUNNER_PLIST}\n"
            f'exec "{sys.executable}" "{REPO_DIR / "plaud_transcribe.py"}" runner "$@"\n')
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(body)
    os.chmod(tmp, 0o755)
    os.replace(tmp, path)


def runner_plist(interval_s, extra_env=(), launcher=None):
    return {
        "Label": RUNNER_ID,
        "ProgramArguments": [str(launcher or launcher_path())],
        "RunAtLoad": True,
        "WatchPaths": [str(jobs.JOBS_DIR)],
        "StartInterval": int(interval_s),
        "WorkingDirectory": str(REPO_DIR),   # the summarizer reads only below its cwd
        "EnvironmentVariables": {"PATH": os.pathsep.join(AGENT_PATH), **carried_env(extra_env)},
        "ProcessType": "Background",
        "StandardOutPath": "/dev/null",
        "StandardErrorPath": str(RUNNER_LOG),
    }


def install_runner(interval_s=DEFAULT_INTERVAL, extra_env=(), bin_dir=None):
    if interval_s < 1:
        raise jobs.JobError("--interval must be at least 1 second")
    launcher = launcher_path(bin_dir)
    plist = runner_plist(interval_s, extra_env, launcher)   # validates --env before anything is touched
    RUNNER_PLIST.parent.mkdir(parents=True, exist_ok=True)
    RUNNER_LOG.parent.mkdir(parents=True, exist_ok=True)
    jobs.JOBS_DIR.mkdir(parents=True, exist_ok=True)   # WatchPaths needs a target
    domain = f"gui/{os.getuid()}"
    subprocess.run(["launchctl", "bootout", f"{domain}/{RUNNER_ID}"], capture_output=True)
    write_launcher(launcher)
    with open(RUNNER_PLIST, "wb") as f:
        plistlib.dump(plist, f)
    subprocess.run(["launchctl", "bootstrap", domain, str(RUNNER_PLIST)], check=True)
    print(f"installed {RUNNER_PLIST} - runs at login, on job-file changes in "
          f"{jobs.JOBS_DIR} and every {int(interval_s)}s; log: {RUNNER_LOG}")
    carried = [n for n in plist["EnvironmentVariables"] if n != "PATH"]
    print("carried environment variables: " + (", ".join(carried) if carried else "none"))
    return 0


def uninstall_runner(bin_dir=None):
    subprocess.run(["launchctl", "bootout", f"gui/{os.getuid()}/{RUNNER_ID}"], capture_output=True)
    RUNNER_PLIST.unlink(missing_ok=True)
    launcher_path(bin_dir).unlink(missing_ok=True)
    print("runner agent removed - queued jobs wait until `plaud-transcribe runner` is run by hand")
    return 0


def _host(host):
    if host is None:
        import plaud_transcribe as host
    return host


def build_enqueue_job(args, host):
    """Turn enqueue flags into a validated job dict. Raises jobs.JobError."""
    cuts = {}
    for item in args.cut:
        rec, sep, span = item.partition("=")
        if not sep or not rec or not span:
            raise jobs.JobError(f"--cut must be REC=START-END, got {item!r}")
        try:
            host.parse_cut(span)
        except ValueError as e:
            raise jobs.JobError(str(e))
        cuts.setdefault(rec, []).append(span)

    project = push = node = ask = None
    if args.project:
        try:
            specs = host.project_specs(host.load_config())
        except host.ConfigError as e:
            raise jobs.JobError(f"config error: {e}")
        if args.project not in specs:
            raise jobs.JobError(f"unknown project {args.project!r}; configured: "
                                + (", ".join(sorted(specs)) or "(none)"))
        spec = specs[args.project]
        project, push, node, ask = args.project, spec["push"], spec["node"], spec["deliver_ask"]
    push = args.push or push
    node = args.node or node
    mode = args.mode or ("now" if node else "none")

    job = jobs.new_job(
        recordings=list(args.recording), merge=args.merge, cuts=cuts,
        instructions=args.instructions, project=project, push=push,
        deliver={"node": node, "mode": mode, "note": args.note, "ask": ask})
    return jobs.validate_job(job)


def cmd_enqueue(args, host):
    job = build_enqueue_job(args, host)
    if args.dry_run:
        print(json.dumps(job, indent=2, ensure_ascii=False))
        return 0
    path = jobs.save_job(job)
    print(f"queued {job['id']}  ({path})")
    return 0


def _summary(job):
    recs = job.get("recordings") or []
    what = recs[0] if len(recs) == 1 else f"{len(recs)} recordings"
    flag = "!" if job.get("state") in ("failed", "held") and not job.get("seen") else " "
    return (f"{flag} {job['id']}  {job.get('state', '?'):<9} "
            f"{(job.get('project') or '(classifier)'):<20} {what}")


def cmd_jobs(args):
    if args.jobs_command == "list":
        listed = jobs.list_jobs()
        if not listed:
            print("no jobs")
        for job in listed:
            print(_summary(job))
    elif args.jobs_command == "show":
        print(json.dumps(jobs.load_job(args.id), indent=2, ensure_ascii=False))
    elif args.jobs_command == "retry":
        job = jobs.retry_job(args.id)
        print(f"requeued {job['id']} (the runner resumes from the first unfinished step)")
    elif args.jobs_command == "cancel":
        job = jobs.cancel_job(args.id)
        print(f"cancelled {job['id']}")
    elif args.jobs_command == "seen":
        job = jobs.mark_seen(args.id)
        print(f"marked {job['id']} as seen")
    return 0


def _notify_failure(args, message):
    """--notify-errors: terminal=false menu actions have nowhere to print an error."""
    if getattr(args, "notify_errors", False):
        from autocapture import notify
        notify("Plaud job action failed", f"{getattr(args, 'jobs_command', args.command)}: {message}"[:200],
               sound="Basso")


def main(argv=None, host=None):
    if argv and argv[0] == "runner":   # own parser; --help must not build jobs parsers' required args
        import runner
        return runner.main(argv[1:], host=_host(host))
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "install-runner":
            return install_runner(args.interval, args.env, args.bin_dir)
        if args.command == "uninstall-runner":
            return uninstall_runner(args.bin_dir)
        if args.command == "install-swiftbar":
            return install_swiftbar(args.dir)
        if args.command == "uninstall-swiftbar":
            return uninstall_swiftbar(args.dir)
        if args.command == "enqueue":
            return cmd_enqueue(args, _host(host))
        return cmd_jobs(args)
    except jobs.JobError as e:
        print(f"plaud-transcribe {args.command}: {e}", file=sys.stderr)
        _notify_failure(args, str(e))
        return 2
    except Exception as e:  # noqa: BLE001
        _notify_failure(args, f"{type(e).__name__}: {e}")
        raise


if __name__ == "__main__":
    sys.exit(main())
