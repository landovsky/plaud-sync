# Spec: plan-ahead transcription, hand-off to cc-tree, menu-bar control

Status: draft for review · 2026-11-24 · autonomous spec (acceptance criteria per part)

## Problem

Transcribing is broken into three moments separated by long waits:

1. Tomas starts a run (`plaud-transcribe`), then has to **remember it is running**.
2. He comes back, checks where the output landed, **`move`s it** if the classifier picked
   the wrong project, and commits/pushes it by hand.
3. He opens the right Claude session and tells it to **process the transcript**.

Each gap costs attention, and step 3 is often forgotten or done late. The goal is that every
decision is made **once, at the start** (ideally while the meeting is still running), and the
rest runs unattended until a cc-tree node has the transcript and knows what to do with it.
Tomas hears back only on failure, and can see the state at a glance at any time.

Separately: autocapture can skip a monitor only via `visuals.autocapture.ignore_displays` in
the config. There is no quick on/off per monitor.

## Decisions already made (2026-11-24)

| # | Decision |
|---|----------|
| D1 | A plan can be made **before the recording ends** (pre-armed on a live Wispr meeting). |
| D2 | Pushing straight to `main` is **opt-in per repo**; other repos get a branch + PR, or no push. |
| D3 | Each project carries a **default cc-tree node**, so the plan screen comes pre-filled. |
| D4 | **No `git stash`.** Commits are made in a temporary worktree of `origin/main` (see Part C). |
| D5 | The plan screen is a **Textual** app inside plaud-sync (an optional dependency). cc-tree is called through its CLI and not imported. |
| D6 | **SwiftBar** is the ambient status and toggle surface. The Textual app is the form surface. |

Why D4, which departs from the original ask ("stash main, commit, pop"): stashing swaps
files out from under any Claude session working in that checkout, and the pop can conflict.
Transcripts are new files, so they never need to share the working tree with work in progress.

## Overview

```
 SwiftBar menu ──"Plan…"──▶ Textual plan screen ──writes──▶ ~/.plaud-sync/jobs/<id>.json
      ▲                                                          │ (launchd WatchPaths)
      │ reads status                                             ▼
      └──────────── jobs/*.json + autocapture status ◀──── runner (unattended)
                                                                 │ transcribe → write → commit
                                                                 │ → push (worktree) → deliver
                                                                 ▼
                                                   cc-tree deliver <node> <work>
```

The job file is the contract. The plan screen, a CLI (`plaud-transcribe enqueue …`) and
pre-arming all produce the same file. The runner is the only thing that executes one.

---

## Part A — Config: per-project repo, push mode, default node

`projects` values may be a string (today's form, unchanged meaning) or an object:

```json
"projects": {
  "Hřiště Hrou": {
    "dir": "~/git/hriste/docs/transcripts",
    "push": "pr",
    "node": "hriste:root",
    "hints": "…"
  },
  "AI Jam": "~/git/ai-jam/docs/transcripts"
}
```

- `push`: `"main"` | `"pr"` | `"none"` (default `"none"`; a string-valued project is `"none"`).
- `node`: a cc-tree ref (`tree` or `tree:id`), optional.
- `hints` may stay in `project_hints`; both forms are read.

**Acceptance**
- A1. An existing config with string-valued `projects` behaves exactly as today (all current tests pass).
- A2. `plaud-transcribe --help` (or `config --help`) documents `dir`, `push`, `node`.
- A3. An unknown `push` value is rejected at load time with the project name in the message.
- A4. `config.example.json` shows one object-form project.

## Part B — Jobs and the runner

**Job file** `~/.plaud-sync/jobs/<id>.json`, written atomically (tmp + rename):

```json
{
  "id": "20261124-1702-ab12",
  "created": "2026-11-24T17:02:00+01:00",
  "recordings": ["plaud:…", "wispr:…"],     // or "trigger" (Part E) instead
  "merge": false,
  "cuts": {"plaud:…": ["29:16-34:55"]},
  "instructions": "…",                        // summarization steering, as today
  "project": "Hřiště Hrou",                   // chosen up front → classifier does not route
  "push": "pr",                               // copied from config, overridable per job
  "deliver": {"node": "hriste:4k2p", "mode": "now", "note": "pull the action items into…", "ask": "<project deliver_ask, K2>"},
  "state": "queued",                          // queued|running|done|failed|held|armed|cancelled (K2)
  "steps": [{"name": "transcribe", "state": "done", "at": "…"}, …],
  "result": {"paths": […], "commits": […], "pr": null, "delivered": "bus id / held id"},
  "error": null
}
```

`deliver.mode`: `"now"` (hand over and start processing) | `"later"` (the node is told a
transcript is waiting, but is not asked to start) | `"none"`.

**Runner** `plaud-transcribe runner`: it processes queued jobs one at a time and exits when
the queue is empty. A LaunchAgent (`plaud-transcribe install-runner`) starts it with
`RunAtLoad` + `WatchPaths` on the jobs dir, so a new job starts it and a reboot resumes it.

**Acceptance**
- B1. Enqueueing a job when nothing is running starts the runner within 10 s, with no terminal open.
- B2. A reboot mid-job leaves the job `queued`/`running`. After login the runner resumes from
  the first step that is not `done`, without re-transcribing (it reuses today's work-dir cache).
- B3. The runner **never prompts**. Every `input()` path (selection, instructions, the phone
  prompt in `visuals._timed_input`) is either bypassed by the job's fields or turned into a
  job failure with a clear `error`. Test: running a job with stdin closed never blocks.
- B4. The runner gets its credentials without an interactive shell. `CLAUDE_CODE_OAUTH_TOKEN`,
  `ELEVENLABS_API_KEY` and `PLAUD_BEARER_TOKEN` come from the repo's `.envrc`/`.env` (see the
  memory note: the Claude token must be sourced, not inherited). A missing one fails the job
  naming the variable, before any paid API call.
- B5. With `project` set, the output goes to that project's `dir` and the classifier's
  project pick is ignored (the summary is still made). `move` is never needed for such a job.
- B6. A failed job ends `failed` with `error` and the failed step. It sends one macOS
  notification (terminal-notifier, as autocapture does) and does not block later jobs.
  `plaud-transcribe jobs retry <id>` resumes it.
- B7. `plaud-transcribe jobs [list|show <id>|retry <id>|cancel <id>]` exists and has `--help`.
- B8. A job that only finished (`deliver.mode = none`) sends a quiet notification. Delivery
  sends none, because the node's session takes over from there.
- B9. Two jobs never run at once (a lock file). A second `runner` exits at once if one holds the lock.

## Part C — Commit and push without touching the working tree

For each job's output repo (the git root containing `dir`):

1. `git fetch origin main`.
2. `git worktree add --detach <tmp> origin/main` (or a new branch `transcripts/<job-id>`
   off `origin/main` when `push = pr`).
3. Copy the job's output files into `<tmp>`, at the same relative paths.
4. **One commit per transcript** (a merged recording counts as one). Message:
   `Add transcript: <title> (<YYYY-MM-DD>)`. The body has the source(s), duration, and the
   summary's first line.
5. Push. For `main`: `git push origin HEAD:main`. On a non-fast-forward rejection, re-fetch,
   rebase in `<tmp>` and retry up to 3 times. For `pr`: push the branch, then `gh pr create`
   with the titles and paths.
6. Remove the worktree.
7. If the user's checkout is on `main`, is behind, and a fast-forward is possible:
   `git merge --ff-only origin/main`. Otherwise leave it untouched and record that in `result`.

`push = none`: files are written as today, plus today's `auto_commit` behaviour.

_As built (K6):_ commits are made on a local branch `transcripts/<job-id>` (kept after the
worktree is removed) and pushed as `transcripts/<job-id>:main`, so a retry never commits twice.
The pipeline itself writes the transcript into the user's checkout as untracked files, which
would block step 7's `--ff-only`. So the fast-forward first removes exactly those files, and
only when each is untracked, lies inside this job's own output dir, and is byte-identical to the
blob just pushed. The merge then restores them as tracked files; if the merge fails they are
written back. Nothing else in the checkout is touched.

**Acceptance** (tests run against throwaway repos with a bare "origin")
- C1. Uncommitted changes in the user's checkout (modified + untracked + staged) are byte-for-byte
  identical before and after a job, and `git stash list` is unchanged.
- C2. Two transcripts in one job give two commits on `origin/main`, each containing only its own files.
- C3. A concurrent push to `origin/main` between fetch and push is absorbed by the retry; nobody's commit is lost.
- C4. `push = pr` never writes to `main`. The PR URL lands in `result.pr`.
- C5. A checkout on another branch is never switched or fast-forwarded.
- C6. When the push fails (no network, protected branch), the job fails at the `push` step. The
  local commit stays in a kept branch `transcripts/<job-id>`, so `retry` does not commit it again.
- C7. hriste-style front matter (`class`/`type: transcript`/`event_date`) is present in what is committed.

## Part D — Hand-off to cc-tree

After a successful push (or write, for `push = none`), with `deliver.mode != none`:

`cc-tree deliver <node> -` with this work text on stdin:

```
New transcript: <title> (<date>, <duration>) — <repo>
Files: <paths relative to the repo>
Commit: <sha>  [PR: <url>]
<mode now:>   Process it now: <deliver.note, or the project's default ask>
<mode later:> No action needed yet — it is here when you need it.
```

`cc-tree deliver` already decides between messaging a healthy session, starting a fresh
one, or holding the work for Tomas. The runner records which one happened.

**Acceptance**
- D1. The work text lists every transcript in the job and paths that exist at `origin/main` (or on the PR branch).
- D2. A deliver that is *held* puts the job in `held` and shows it in the menu (Part F), with the held id.
- D3. `cc-tree` missing or failing fails only the `deliver` step. The transcript stays pushed, and `retry` re-delivers without re-committing.
- D4. `deliver --plan` is used in a dry-run test, so tests never message a real session.

## Part E — Pre-arming on a live meeting (D1)

The plan screen offers **"the meeting that is live now"** whenever the autocapture watcher sees
one (`wispr_live_meeting`). The job is saved `armed` with `trigger: {"wispr": "<uuid>"}`.

When that meeting ends and Wispr's transcript is ready (refined, or the live fallback after
`wispr.live_fallback_minutes`), the job becomes `queued` with the recording filled in.

**Acceptance**
- E1. Arming during a meeting and then ending the meeting produces a delivered transcript with
  no further input. Tested with a recorded Wispr fixture DB.
- E2. A paused-then-resumed meeting (the 2026-11-02 case) does not fire the job at the pause.
- E3. An armed job whose meeting is deleted, or that never ends within `max_hours`, fails with a notification instead of waiting forever.
- E4. Plaud/Recorder recordings cannot be armed in this phase. The plan screen says so instead of offering it.

## Part F — SwiftBar menu (status + toggles)

One plugin, `swiftbar/plaud.5s.py` in this repo. `plaud-transcribe install-swiftbar`
symlinks it into SwiftBar's plugin folder. It reads only local files (`jobs/*.json`,
autocapture `status.json`, the pid file), so a refresh costs a few ms.

Menu-bar title (one glyph + a count):
- recording/capturing ● · watching ○ · runner busy ⟳N · something needs Tomas ⚠ · watcher down ✕

Dropdown:
```
Autocapture: watching (Wispr)             ← or capturing 23 min · 41 scenes / paused / NOT RUNNING
  Displays
    ✓ PHL 272B7QU                         ← click toggles capture for this monitor
      Built-in Retina Display (off)
  Pause autocapture / Resume
---
Jobs
  ⟳ Hřiště: školení … — transcribing      ← click: show log
  ⚠ AI Jam standup — push failed: …       ← submenu: Retry · Show log · Cancel
  ✓ Koperníci weekly — delivered to kopernici:9xq1
Plan a transcript…                         ← opens the Textual plan screen in a terminal
Arm current meeting…                       ← only while a Wispr meeting is live
```

**Display toggle (the missing piece).** Runtime overrides live in
`~/.cache/plaud-autocapture/displays.json` (`{"<display name>": false}`). They are layered on
top of `ignore_displays`: an override wins over the config pattern, both ways. The capture
loop re-reads the file when its mtime changes, so a toggle applies from the next frame,
mid-capture included. `autocapture.py display <name|n> on|off` does the same from a terminal.

**Acceptance**
- F1. Toggling a monitor off mid-capture stops new frames from it within one `interval_s`, and
  the timeline records `ignored` for it. Toggling it on resumes capture, with no restart.
- F2. Overrides survive a watcher restart and a reboot. `autocapture.py displays` shows the effective state and its source (config/override).
- F3. A monitor that is unplugged keeps its override for when it returns (keyed by name, not index).
- F4. With the watcher not running, the menu bar shows ✕ within one refresh. (It is the visible
  twin of the notification added in e4a88ef.)
- F5. A failed or held job shows ⚠ until it is retried, cancelled or seen. "Seen" clears the glyph but keeps the entry.
- F6. The plugin runs in < 100 ms and never calls the network, Claude or cc-tree.
- F7. Without SwiftBar everything still works. The plugin is optional, and nothing else depends on it.

## Part G — Textual plan screen

`plaud-transcribe plan` (Textual, optional dependency: `pip install textual`; without it, the
command says how to install it and points to `enqueue`).

One screen, keyboard-first:
1. **Recordings**: the pending list as today (source, date, duration, folder). Multi-select,
   `m` merges, `c` adds a cut, and the live Wispr meeting comes first when there is one.
2. **Project**: a fuzzy picker over `projects`, pre-selected from the classifier's last guess
   for similar titles (`recent_examples`). It shows `push` mode.
3. **Node**: pre-filled from the project's `node`. `n` opens a fuzzy picker over
   `cc-tree show <tree>` rows. It can be cleared.
4. **Mode**: now / later / none. **Note** to the node and **instructions** to the summarizer
   are two multi-line text areas (pasted paragraphs are the norm).
5. `Enter` saves the job (or arms it) and quits. The terminal is free again.

`plaud-transcribe enqueue` takes the same fields as flags (for scripts and for SwiftBar).
The old interactive `process` flow stays unchanged.

**Acceptance**
- G1. From launch to a saved job takes ≤ 5 keystrokes for the common case: one recording, defaults accepted.
- G2. The screen never starts work in its own process. Quitting it mid-form writes nothing.
- G3. `enqueue --help` lists every job field. A job made by `enqueue` is identical to one from `plan`.
- G4. Textual tests (`App.run_test`) cover selecting, merging, overriding the node, and saving.

---

## Out of scope

- Pre-arming Plaud or Recorder recordings (their upload time is unknown). Revisit after E ships.
- Running the runner on hp-ubuntu (Wispr, adb and the screen are all on the Mac).
- Changing how cc-tree deliver decides between message, fresh start and hold.
- Embedding plaud-sync into cc-tree's own Textual view.

## Order of work

1. A + B + C + D via `enqueue` (no UI). This removes the remembering, the `move` and the hand-off.
2. F (SwiftBar incl. display toggle). Visible state + monitor switch.
3. G (Textual plan screen).
4. E (pre-arm), which builds on G's "live meeting" row and F's "Arm current meeting…".

## Resolved questions (2026-11-24)

- Q1 → **D7**: per-project `deliver_ask` in config is the default "process it" ask; the note typed in the plan overrides it. (Part A gains `deliver_ask`; Part D uses it.)
- Q2 → **D8**: a rejected push to `main` **fails the job and notifies**; the commit stays on `transcripts/<job-id>`. No automatic fallback to a PR.
- Q3 → **D9**: ⚠ stays until the job is retried, cancelled or explicitly marked **Seen** in the menu. No time-based clearing.

## Implementation plan

Planned 2026-11-24 against the code at 5ed9916. One chunk = one focused session = one commit
(or PR). Each chunk leaves `/opt/homebrew/bin/pytest` green and ships on its own.

### Ground rules (apply to every chunk)

- **Tests**: `/opt/homebrew/bin/pytest` (not `python3 -m pytest`). Style: classes describe the
  real-world scenario, test names say why (`tests/test_autocapture_agent.py`). Nothing in tests
  touches `~/.plaud-sync`, `~/.cache/plaud-autocapture`, the network, launchd, or a real
  remote: monkeypatch `CONFIG_DIR`/`STATE_DIR`-style constants to `tmp_path` (pattern:
  `_isolate()` in `tests/test_autocapture_agent.py`).
- **Tunables**: every new knob is a flag, env var or config key and appears in `--help`
  (epilog "recipes" block, as `autocapture.py main()` does). `--help` on every new subcommand
  must not run anything: add one shared test that calls `main([...,"--help"])` for each
  subcommand with all side-effect functions monkeypatched to raise.
- **No `git stash`**, anywhere (code, tests, docs). Part C uses worktrees only.
- **CLI shape**: today's `plaud_transcribe.main()` is one flat parser (`command` has `choices=`,
  one `extra` positional) and `main()` calls `get_token()` + `fetch_folders()` (network) before
  `process`/`list`. New subcommands (`enqueue`, `jobs`, `runner`, `install-runner`,
  `install-swiftbar`, `plan`) must be dispatched **before** that, from a new module
  (`jobs_cli.py`) with their own `argparse` subparsers, so `--help` works and enqueue never
  touches the network. Leave the old flat parser's behaviour unchanged (add the new names to
  `choices` only so `plaud-transcribe --help` lists them).
- **New modules** (keep `plaud_transcribe.py` from growing past its 2171 lines): `jobs.py`
  (model + store), `jobs_cli.py` (argparse for new commands), `runner.py` (loop, lock, steps),
  `publish.py` (worktree git), `deliver.py` (cc-tree), `plan_logic.py` + `plan_app.py` (Textual),
  `swiftbar/plaud.5s.py` (stdlib only, no `import plaud_transcribe`: that imports `visuals`
  and friends and would break F6's 100 ms budget).
- **cc-tree**: not modified. `cc-tree deliver --help` is NOT supported (it treats `--help` as a
  tree name), so do not call it in tests or from `--help`. The binary is overridable
  (`CC_TREE_BIN` env / `deliver.bin` config) so tests point it at a stub script.
- **Wide risk (state.json)**: `save_state()` rewrites the whole file from a snapshot loaded at
  start. An interactive `plaud-transcribe` session and the runner would overwrite each other.
  From K3 on, the runner re-loads state before each recording and saves right after
  (`process_recording` already saves per recording); do not hold one `state` dict for a whole job.

### K1 — Part A: per-project object config  (A1–A4, D7 config half)
- Files: `plaud_transcribe.py` (`get_projects` ~l.106, `project_hints` ~l.606, `format_project_list` ~l.639, `cmd_init` template ~l.2064), `config.example.json`, `README.md`.
- New: `project_specs(config) -> {name: {dir, push, node, hints, deliver_ask}}` (validates; raises `ConfigError("project 'X': push must be main|pr|none, got 'foo'")` at `load_config()` time, A3). `get_projects()` keeps returning `{name: dir-string}` so the 7 call sites (`projects[project_key]`, `resolve_project`, `select_project`, `format_project_list`) do not change. `project_hints()` merges `project_hints` and per-project `hints`.
- Help: `plaud-transcribe --help` epilog gets a "config: projects" block documenting `dir`, `push`, `node`, `hints`, `deliver_ask` (A2; there is no `config` subcommand today, so epilog it is).
- Tests: `tests/test_project_config.py` (string form unchanged; object form; unknown push names the project; hints from both places; default push `none`). Run the whole suite for A1.
- Risks: `format_project_list` feeds the summarizer prompt: do not add `push`/`node` to it (leaks nothing useful, changes classification). `dir` must still `expanduser()` at each use site.

### K2 — Job model, store, `enqueue`, `jobs list|show|cancel|seen`  (B7 minus retry, G3 half)
- Files: new `jobs.py`, `jobs_cli.py`; `plaud_transcribe.py` (dispatch hook at top of `main()` + `choices`).
- New: `JOBS_DIR` (`~/.plaud-sync/jobs`, env `PLAUD_JOBS_DIR`), `new_job_id()` (`YYYYMMDD-HHMM-xxxx`), `save_job()` (tmp + `os.replace`, atomic), `load_job()`, `list_jobs()`, `set_step()`, `validate_job()`; commands `enqueue` (flags for every job field: `--recording` repeatable, `--merge`, `--cut REC=START-END`, `--instructions`, `--project`, `--push`, `--node`, `--mode now|later|none`, `--note`, `--dry-run` prints the JSON and writes nothing), `jobs list|show <id>|cancel <id>|seen <id>`. Enqueue defaults `push`/`node` from the chosen project's config (K1); recording ids are only validated syntactically (no network). Job gets `seen: false` (D9) and `schema: 1`.
- Tests: `tests/test_jobs.py` (atomic write: kill between tmp and rename leaves no half file; enqueue offline with the network function monkeypatched to raise; `--dry-run` writes nothing; cancel only from queued/failed/held/armed; `seen` clears the glyph flag but keeps the job).
- Risks: this fixes the job schema that K4–K15 and the SwiftBar plugin all read, so settle the open points in "Spec gaps" first; add `schema` so later changes can migrate. A job file watched by launchd `WatchPaths` fires on every write, including the runner's own step updates, so the runner must tolerate re-trigger (K4/K5: lock makes it exit at once, B9).

### K3 — Non-interactive pipeline hooks  (B3, B5)
- Files: `plaud_transcribe.py` — `process_recording` (~l.1084), `process_merged_recordings` (~l.1253), `execute_selection` (~l.1704), `claude_summarize` (~l.655); `visuals.py` (`_await_phone` ~l.215, `_timed_input` ~l.186).
- New params: `forced_project=None` on both process functions (skips the `project_key not in projects` normalisation at ~l.1199/~l.1379 and picks `projects[forced]`; the classifier is still run for name/summary; pass the forced project in the prompt so the summary does not argue with it) and `interactive=True`. The functions return a richer dict (`date`, `duration`, `sources`, `summary_first_line`, `files`, `output_dir`) which Part C commit messages and Part D work text need. Merged mode gains a `cuts` parameter only if the open question on cuts+merge is decided that way.
- B3 audit (all `input()` sites): `prompt_selection` l.1506, `prompt_custom_instructions` l.1545, `select_transcript` l.1826, `select_project` l.1848 are only reached from `cmd_process` interactive loop and `cmd_move`; the runner must never call those, enforced by a test that sets `builtins.input` to raise and `sys.stdin` to a closed stream. The one reachable-by-the-pipeline prompt is `visuals._await_phone` → `_timed_input` (`select` on stdin): today it is skipped only because `isatty()` is false; make it explicit (`visuals.set_interactive(False)` / env `PLAUD_NONINTERACTIVE=1`) so a runner started from a terminal cannot prompt either. `prefetch_phone_files` still runs (adb) and is fine non-interactively.
- Tests: `tests/test_noninteractive_pipeline.py` (stubbed `transcribe_audio`/`claude_summarize`: forced project wins over the classifier's pick; summary still produced; closed stdin never blocks; `_timed_input` never called), extend `tests/test_phone_prefetch.py` only if its monkeypatching breaks.
- Risks: `process_recording` is 170 lines used by `cmd_process` and `cmd_file` (`cmd_file` ~l.2034 passes positional args): keep new params keyword-only with defaults. The `done` entry written to state must keep `project` = the forced project so `recent_examples` learns from it.

### K4 — Runner core: lock, step machine, resume, failure  (B2, B6, B8, B9, `jobs retry`)
- Files: new `runner.py`; `jobs_cli.py` (`runner`, `jobs retry`); `autocapture.py` (import `notify` — it is module-level and dependency-free; do not copy it).
- New: `acquire_lock()` (`fcntl.flock` on `~/.plaud-sync/runner.lock`; second runner exits 0 at once, B9), `run_queue()` (oldest `queued` first; loop until none; exit), `run_job(job)` with steps `transcribe` → `commit` → `push` → `deliver`; each step runs only if not `done` (B2) and writes `steps[]` before and after. `transcribe` step = `execute_selection`-equivalent (K3) using the work-dir cache so a rerun does not re-transcribe. `commit`/`push`/`deliver` are registered as pluggable functions: `no-op` placeholders until K6–K8 land (a `push=none` job is complete after `transcribe`). On exception: job `failed`, `error`, `failed_step`, one `notify()` (B6), continue with the next job. Finished with `deliver.mode=none` → quiet notification (B8). On start, any job left `running` (crash/reboot) is treated as resumable. `runner --once`, `--job ID`, `--lock-file` flags for tests.
- Tests: `tests/test_runner.py` (fake step functions: resume skips `done` steps; stale `running` resumed; failing job does not block the next; two runners → second exits immediately, using two processes or a held flock; `retry` clears `error`, keeps `done` steps; cancel while queued).
- Risks: flock must be released when the process dies (it is: kernel) — do not use a pid-file. Re-triggering via `WatchPaths` while running is expected. A job `cancel`led mid-run: runner re-reads the job file between steps and stops. Never swallow `KeyboardInterrupt`/`SystemExit` into `failed`.

### K5 — Credentials and the LaunchAgent  (B1, B4)
- Files: `runner.py` (`load_runner_env`, `preflight`), `jobs_cli.py` (`install-runner`, `uninstall-runner`), new `tests/test_install_runner.py`.
- New: `install_runner()` modelled on `autocapture.install_agent()` (~l.1182: plistlib, `launchctl bootout` then `bootstrap gui/<uid>`): `RunAtLoad` true, `WatchPaths` [jobs dir], `WorkingDirectory` = repo dir (the summarizer can only read files under the folder it runs from, see module docstring), `EnvironmentVariables.PATH` including `/opt/homebrew/bin` and `~/.local/bin` (launchd's PATH has neither `claude`, `gh`, `cc-tree`, `terminal-notifier` nor `git` from Homebrew), `StandardErrorPath` log. `preflight(job)`: before any paid API call check `CLAUDE_CODE_OAUTH_TOKEN`, `ELEVENLABS_API_KEY` (only if a Plaud/local/recorder recording is in the job; Wispr jobs need no ElevenLabs), `PLAUD_BEARER_TOKEN` (only for Plaud recordings) and fail naming the variable. `load_runner_env()`: env already present wins; else `direnv exec <repo> env -0` (the `.envrc` runs `sops` and `dotenv .env`, so it cannot be parsed as a plain file); knob `PLAUD_RUNNER_ENV_CMD` / config `runner.env_cmd` overrides the command; a `runner.env_file` (KEY=VALUE, mode 600) is the fallback.
- Tests: plist content asserted (WatchPaths, RunAtLoad, PATH, WorkingDirectory) with `launchctl` monkeypatched; preflight names each missing variable and runs before any API function (assert transcribe stub not called).
- Risks: see Spec gaps #1 (ELEVENLABS is in no repo file). B1 ("starts within 10 s, no terminal open") is a manual check after install, record the result in Notes. `bootstrap` must not run in tests.

### K6 — Part C core: worktree commit + push to main + ff  (C1, C2, C3, C5, C7)
- Files: new `publish.py`; `runner.py` (register `commit`+`push` steps); `plaud_transcribe.py` untouched except re-using `git_commit_working` is NOT possible (it commits in the user's checkout); leave it for `push=none` + `auto_commit`.
- New: `repo_root(dir)`, `publish_main(job, results)`: `git fetch origin main`; `git worktree add --detach <tmp> origin/main`; copy each result's `output_dir` to the same relative path; one `git add`/`commit` per transcript (message from K3's result: `Add transcript: <title> (<date>)` + body with sources, duration, summary first line); `git push origin HEAD:main`; on non-fast-forward `fetch` + `rebase origin/main` in `<tmp>`, retry max 3 (env/flag `PLAUD_PUSH_RETRIES`); `worktree remove --force` in `finally`; `fast_forward_checkout(root)` only if the checkout is on `main`, is behind and `merge-base --is-ancestor` holds, using `git merge --ff-only origin/main` — and only if the working tree does not touch a file the ff would change (otherwise record `result.checkout = "left behind: local changes"`). Use `git -C <path>` and `GIT_TERMINAL_PROMPT=0` for every call; never `git stash`, `checkout`, `reset` in the user's checkout.
- Tests: `tests/test_publish_main.py` with a fixture building a bare origin + clone via `git init --bare` and `tmp_path` (set `GIT_CONFIG_GLOBAL`/author env so tests do not depend on the dev's git config): C1 (modified + untracked + staged files byte-identical, `git stash list` empty before/after, `git status --porcelain` identical), C2 (two commits each with only its files), C3 (a second clone pushes between fetch and push via a hook seam `publish._before_push` monkeypatched), C5 (checkout on another branch: HEAD and files untouched), C7 (front matter in committed blob: `class: immutable`, `type: transcript`, `event_date`; already produced by `write_outputs` ~l.847, the test pins it).
- Risks: the transcript dirs may live in an ignored path (plaud-sync's own `docs/transcripts` is gitignored and a nested repo): `git add -f` inside the worktree is correct, but `repo_root()` for such a project is the nested repo, check it has an `origin` or fail the `commit` step with a clear error. Copying must preserve nested `visuals/` images. The worktree directory goes under `tempfile.mkdtemp(prefix="plaud-wt-")` outside the repo; remove it also when the runner is killed (next start prunes `git worktree prune`). Rebase conflicts on a transcript path are only possible on a same-name dir: fail, do not resolve.

### K7 — Part C: PR mode, kept branch, retry idempotence  (C4, C6, D8)
- Files: `publish.py`, `runner.py`.
- New: `publish_pr(job, results)` (worktree on a new branch `transcripts/<job-id>` off `origin/main`, push branch, `gh pr create --title … --body …` with titles and relative paths, URL → `result.pr`; `gh` binary overridable `GH_BIN`); common failure handling: on any push failure the local commits stay on branch `transcripts/<job-id>` (D8: a rejected push to `main` fails the job and notifies, no auto-PR), recorded in `result.commits`/`result.branch`; `retry` of the `push` step detects the existing branch and re-pushes without committing again (C6). `push=none` keeps `auto_commit` behaviour via the existing `git_commit_working`.
- Tests: `tests/test_publish_pr.py` (C4: nothing written to `main`, PR url from a stub `gh` on PATH; C6: push rejected by a `pre-receive` hook in the bare origin → job failed at `push`, branch kept, retry after removing the hook pushes the same sha, commit count unchanged).
- Risks: `gh` is never run for real (stub script in `tmp_path/bin` prepended to PATH). A protected-branch rejection and "no network" look different in stderr: store the first 500 chars verbatim in `error`.

### K8 — Part D: hand-off to cc-tree  (D1–D4; closes stage 1)
- Files: new `deliver.py`; `runner.py` (register `deliver` step); `README.md` (stage-1 usage section); `docs/` none.
- New: `build_work_text(job, results)` (format in Part D; for `later` the "No action needed yet" line; for `now` the `deliver.note` else the project's `deliver_ask` (D7) else a built-in default), `deliver(job, text)` runs `[bin, "deliver", node, "-"]` with the text on stdin, parses the outcome (message / fresh start / held id) into `result.delivered`; held → job `held` (D2); `cc-tree` missing/non-zero → fail only `deliver`, transcript stays pushed (D3). `--plan` mode: knob `PLAUD_DELIVER_PLAN=1` / `runner --deliver-plan` runs `cc-tree deliver <node> --plan` instead and records its output, never messaging (D4).
- Tests: `tests/test_deliver.py` with a stub `cc-tree` shell script (records argv + stdin, prints canned "held id 9xq1" / "sent bus id"); D1 text lists every transcript and repo-relative paths that exist in `origin/main`/PR branch (assert against the throwaway repo); D3 retry re-delivers without a new commit; end-to-end `tests/test_runner_e2e.py` (enqueue → runner → push to bare origin → stub deliver) with all network stubs.
- Risks: the actual stdout format of `cc-tree deliver` is not documented in `--help` (only "a message when healthy, a fresh start …, else held"): read `~/.dotfiles/bin/cc-tree` / its docs for the exact lines before writing the parser, and fall back to storing raw stdout when unparsed (do not fail on an unrecognised success). Never run a real `cc-tree deliver` in tests, including by accident via PATH: tests set `CC_TREE_BIN` explicitly.

### K9 — Part F: display overrides  (F1, F2, F3)
- Files: `autocapture.py` — `capture()` loop ~l.600–640, `_ignored_display` ~l.330, `displays` command ~l.1364, `main()` subparsers, epilog recipes.
- New: `OVERRIDES_FILE = STATE_DIR / "displays.json"`, `load_overrides()` (mtime-cached; keyed by display name), `set_override(name, on)` (atomic write), `display_state(name, patterns, overrides) -> (captured: bool, source: "config"|"override"|"default")` (override wins both ways), command `display <name|n> on|off` (resolves a number through `_screens()` then stores the **name**, F3) and `displays` printing the source column. In the loop: re-check the file mtime per sample; the `if len(shots) > 1 or s["ignore_displays"]` condition (~l.601) must also be true when overrides exist, otherwise names are never fetched for a single-monitor capture. Replace the `_ignored_display(name, s["ignore_displays"])` call with `display_state`; event/log text says `ignored by override` vs `by config`; toggling on removes the idx from `ignored` (already works, l.636) and logs "capturing".
- Tests: `tests/test_autocapture_display_override.py` (override beats pattern both ways; persisted file survives re-import = F2; unplugged name kept = F3; mtime change picked up without restart: drive the pure `display_state` + loader, not `screencapture`; timeline gets `ignored` once, using `_event`; `_intervals` still ends the scene, reuse the fixture style of `tests/test_autocapture_ignore_displays.py`). Existing `test_autocapture_ignore_displays.py` must stay green: keep `_ignored_display` as is.
- Risks: `capture()` is long and untested end to end; keep the change to the one decision point. `status.json` should also carry `displays` (name + captured) so the menu in K10 does not need to call JXA (F6).

### K10 — SwiftBar plugin, read-only status  (F4, F6, F7, part of F5)
- Files: new `swiftbar/plaud.5s.py`, `jobs_cli.py` (`install-swiftbar`), `autocapture.py` (status.json fields only if K9 did not add them).
- New: plugin renders the title glyph (● ○ ⟳N ⚠ ✕) and dropdown from local files only: `jobs/*.json`, `status.json`, pid file (`os.kill(pid,0)`), `paused`, `displays.json`. Pure function `render(now, jobs, status, pid_alive, paused, overrides) -> str` so tests need no SwiftBar. Staleness: `status.json` older than ~3x poll with no pid → ✕ (F4). `install-swiftbar` symlinks into the plugin folder (`--dir` flag / `PLAUD_SWIFTBAR_DIR`, default read from `defaults read com.ameba.SwiftBar PluginDirectory`, fall back to the SwiftBar default `~/Library/Application Support/SwiftBar/Plugins`); prints a hint if SwiftBar is not installed and exits 0 (F7).
- Tests: `tests/test_swiftbar_plugin.py` (glyph priority: ✕ > ⚠ > ⟳ > ● > ○; the F4 case; a failed job shows ⚠ until `seen`; 100 ms budget asserted by timing `render` over 50 jobs; module imports only stdlib — assert `sys.modules` has no `plaud_transcribe`/`autocapture` after import; no `socket` use).
- Risks: SwiftBar runs plugins with a minimal env and no PATH: shebang must be an absolute interpreter, and file paths come from `Path.home()`, not env. Unicode glyph width differs by font: keep to those in the spec. Plugin shares schema knowledge with `jobs.py` but must not import it (imports `autocapture`-free is fine; a tiny `swiftbar/_paths.py` or duplicated constants guarded by a test comparing them to `jobs.JOBS_DIR`).

### K11 — SwiftBar actions  (F1 UI, F5, D2 menu, D9)
- Files: `swiftbar/plaud.5s.py`, `jobs_cli.py`, `autocapture.py` (`pause`/`resume` aliases if wanted).
- New: menu items emit `bash=<abs python> param1=… terminal=false refresh=true` lines for: toggle display (`autocapture.py display <name> on|off`), Pause/Resume autocapture (existing `stop` / `start --watch`; paused = `PAUSED_FILE` exists), per-job Retry / Show log / Cancel / Mark seen (`jobs …`), "Plan a transcript…" (opens a terminal running `plaud-transcribe plan`, `terminal=true`), held job shows the held id and the `cc-tree deliver --release` hint (D2). "Arm current meeting…" is added in K15.
- Tests: extend `tests/test_swiftbar_plugin.py` (rendered lines contain the right argv; quoting of display names with spaces; menu action commands each accept `--help` and are real subcommands: parse every emitted `param*` through the real argparse in a dry test).
- Risks: `terminal=false` actions hide errors: have `jobs` actions notify on failure. Display names can contain `|`, which SwiftBar treats as a parameter separator: sanitise.

### K12 — Plan logic without a UI  (G1 logic, G3)
- Files: new `plan_logic.py`; `jobs_cli.py` (`enqueue` and `plan` both build jobs through one `build_job(fields)`); `plaud_transcribe.py` only for extracting `pending_recordings(token, config, state, days)` out of `cmd_process`/`show_pending` (~l.1473) so the form and the CLI share the "pending" definition (`auto_skip_short`, state statuses).
- New: `pending_recordings()`, `suggest_project(title, state, projects)` (uses `recent_examples` ~l.617; the "similar titles" measure is not defined in the spec, use `fuzzy_match` ~l.1774 / token overlap on names, deterministic, falling back to none), `list_nodes(tree)` (parses `cc-tree show <tree>` rows: format not documented, confirm against real output and store a fixture in `tests/fixtures/cc_tree_show.txt`), `build_job(fields)` (shared by enqueue/plan: G3 identical jobs).
- Tests: `tests/test_plan_logic.py` (identical job from `enqueue` args and from the form's dict; default node from the project; suggestion from history; live Wispr meeting listed first).
- Risks: parsing `cc-tree show` is fragile (colour codes, live state): strip ANSI, treat failure as "no node picker", not an error.

### K13 — Textual plan screen  (G1, G2, G4)
- Files: new `plan_app.py`, `jobs_cli.py` (`plan`), `requirements-plan.txt` (textual, pinned), `README.md`.
- New: `PlanApp(App)` with the five sections of Part G, keys `m`/`c`/`n`/`Enter`/`Esc`; `plan` command that prints the install hint plus the `enqueue` pointer when `import textual` fails. Quit writes nothing (G2): the only write is `jobs.save_job()` on Enter. Textual is NOT installed for Homebrew python3.15: tests use `pytest.importorskip("textual")`, and the plan is to create `.venv` (`python3 -m venv .venv && .venv/bin/pip install textual pytest`) and run `.venv/bin/pytest`; record in Notes that the Homebrew run skips them. Decide whether the SwiftBar "Plan…" action must use the venv's python (it must, if Textual only lives there): make `plan` re-exec through `PLAUD_PLAN_PYTHON`/config `plan.python`.
- Tests: `tests/test_plan_app.py` using `App.run_test()` + `Pilot` (G4: select, merge, override node, save; G1: keystroke count from launch to saved job for the default case; G2: Esc mid-form leaves `jobs/` empty).
- Risks: pasted multi-line text areas — test with a paste event, not typed keys. The app must not call the network on mount blocking the UI (list recordings in a worker with a spinner, offline → empty list + message).

### K14 — Part E: armed jobs and promotion  (E1, E2, E3 mechanics)
- Files: `jobs.py` (`armed` + `trigger`), `runner.py` (`promote_armed()` at the start of each run), `autocapture.py` (reuse `wispr_live_meeting`, `_resumed_after_end`), `wispr_flow.py` (readiness through `list_meetings`, which already applies `live_fallback_minutes`), `jobs_cli.py` (`jobs promote` for a manual/launchd poll), `install-runner` (adds the periodic trigger, see Spec gaps #4).
- New: `meeting_ready(config, uuid) -> "live" | "ready" | "gone" | "waiting"`: live = `wispr_live_meeting` returns this id (covers the paused-then-resumed case, E2); ready = not live and `list_meetings` returns it; gone = deleted row; `waiting` otherwise (ended, transcript not refined and fallback minutes not passed). `promote_armed()` fills `recordings` from the meeting and sets `queued`. E3: `max_hours` (config `visuals.autocapture.max_hours` or a new `arm.max_hours` flag/config) passed or `gone` → `failed` + `notify`.
- Tests: `tests/test_armed_jobs.py` with the fixture-DB style from `tests/test_autocapture_wispr_trigger.py` (`_store()` builds a minimal Wispr dir): E1 (arm during live → end → refined file appears → promoted → runner → delivered with stubs), E2 (pause marker then resume does not promote at the pause), E3 (deleted, and never-ending).
- Risks: `wispr_live_meeting` signature is `(config, max_hours)` and logs via `_log`; calling it from the runner is fine but it writes to the autocapture log. `list_meetings` hides a meeting until refined or the fallback elapsed: promotion latency is up to `live_fallback_minutes` (30 by default) after the meeting ends: say so in `--help`.

### K15 — Part E entry points and docs  (E4, menu, final docs)
- Files: `jobs_cli.py` (`enqueue --arm-live` / `--arm-wispr UUID`), `plan_app.py` (live-meeting row; for Plaud/Recorder rows the "cannot be armed in this phase" message), `swiftbar/plaud.5s.py` ("Arm current meeting…" only while `status.json` / `wispr_live_meeting` says live), `README.md`, `DESIGN.md`, `docs/specs/…` Progress table, `artifacts/gitops.md` is N/A (no deployed app).
- Tests: extend `tests/test_plan_app.py`, `tests/test_swiftbar_plugin.py`, `tests/test_jobs.py` (E4: arming a Plaud id is refused with the explanatory text).
- Risks: the menu must not call Wispr's sqlite in the plugin (F6): read the live meeting id from `status.json` (autocapture watcher writes it when it starts a capture: add `meeting` to `_write_status` in `watch()` ~l.826).

### Spec gaps and ambiguities found while planning

1. **B4 credentials source.** Spec: "`ELEVENLABS_API_KEY` and `PLAUD_BEARER_TOKEN` come from the repo's `.envrc`/`.env`". Reality: `.env` holds only `PLAUD_BEARER_TOKEN`; `.envrc` decrypts `.env.enc` with `sops` (needs the age key) and evals `CLAUDE_CODE_OAUTH_TOKEN` from `~/.dotfiles/secrets/credentials.env.enc`; `ELEVENLABS_API_KEY` is in no repo file (it is in Tomas's interactive shell env). Also `claude` is a zsh function in his shell; the binary is `/opt/homebrew/bin/claude` or `~/.local/bin/claude`. Needs a decision: `direnv exec` plus an `ELEVENLABS_API_KEY` entry in the sops file, or a `runner.env_file`. Blocks K5 (the install, not the code).
2. **Cuts with merge.** Job example: `"merge": false, "cuts": {"plaud:…": [...]}`, plan screen: "`m` merges, `c` adds a cut". Today `--cut` works only for one recording (`parser.error("--cut needs one recording…")`) and `process_merged_recordings` has no `cuts` parameter. Decide: forbid `merge` + `cuts` in a job (simple, reject in `validate_job`) or extend merging. Affects K2/K3/K13.
3. **Project/push/node when the classifier routes.** "`push`: copied from config, overridable per job" and "`project`: chosen up front → classifier does not route". If no project is chosen, the project is known only after classification, so `push` and `deliver.node` (D3 default) cannot be copied at enqueue time. Plan assumes: resolve `push`/`node`/`deliver_ask` from the final project at the `commit` step when the job leaves them null; `default` project (unrouted, `default_output`) has `push=none` and no node, so deliver is skipped with `mode` forced to `none`. Please confirm.
4. **What wakes the runner for an armed job.** "launchd `WatchPaths` on the jobs dir" fires only on file changes; a meeting ending changes no job file. Section E says nothing on who promotes `armed` → `queued`. Plan assumes the runner agent gets a `StartInterval` (default 60 s, `install-runner --poll`) that runs `runner` (which calls `promote_armed()` first). Alternative: the autocapture watcher promotes. Needs a choice before K14.
5. **A2 "or `config --help`"**: there is no `config` subcommand. Plan documents the keys in the `plaud-transcribe --help` epilog.
6. **cc-tree deliver output format and `cc-tree deliver --help`.** `cc-tree deliver --help` is not supported (treated as a tree name), and the spec does not say how the runner recognises "message / fresh start / held id" (D2, `result.delivered`). Plan: read the real output in K8; store raw stdout when unparsed.
7. **G "pre-selected from the classifier's last guess for similar titles"**: no similarity measure exists (`recent_examples` returns names per project only). Plan: simple fuzzy/token overlap, deterministic (K12). Not blocking.
8. **"Seen"** (D9) needs a field the job example lacks: plan adds `seen: bool` and `jobs seen <id>` (K2). Not blocking.
9. **Plaud recordings at runner time** need a network call (`list_all_recordings(days)`) to turn ids into recording dicts; the job has no `days`. Plan: runner uses `--days` (default 30) / config `runner.days`, and jobs for older recordings fail with a clear message. Not blocking.

**Manager decisions (2026-11-24)**

- Cuts + merge: forbidden. `enqueue`/`validate_job` reject a job with `merge=true` and any cuts, with a clear message (matches today's `--cut` rule). Extending merged mode with cuts is logged under "## Future work".
- Classifier-routed jobs (no project chosen): `push`/`node`/`deliver_ask` resolve at the commit step from the final project; the `default` output means `push=none` and delivery skipped. Confirmed.
- Armed-job promotion: the runner LaunchAgent gets `StartInterval` 60 s in addition to `WatchPaths`; `promote_armed()` runs at each start. The autocapture watcher is not involved.
- A2 help location: epilog of `plaud-transcribe --help`. cc-tree deliver: stub via `CC_TREE_BIN`, raw stdout kept when unparsed. "Similar titles" = fuzzy/token overlap; `seen` field added to the schema; `runner.days` default 30; runner reloads `state.json` per recording. All accepted.
- Credentials (gap 1): **Tomas chose `direnv exec` + sops** (2026-11-24). `load_runner_env()` runs `direnv exec <repo> env -0`; no plaintext env file. Tomas adds `ELEVENLABS_API_KEY` to the sops-encrypted credentials file himself — agents never read or write secrets. K5 install verification waits on that.

## Progress

_Managed via `/manage feature`; updated per chunk._

| Chunk | Parts | Status | Commit | Notes |
|-------|-------|--------|--------|-------|
| K1 | A | done | 0d0cf3a | `project_specs()` + `ConfigError` validated in `load_config()`, `project_hints` merges per-project hints, epilog in `--help`, example/README updated, 15 tests; deviations: `main()` turns ConfigError into a clean exit 2; `load_config(validate=False)` used by autocapture so a project typo cannot stop capture; `cmd_init` template `project-b` is now object-form (`push` none) |
| K2 | B (store, enqueue, jobs list/show/cancel/seen), G3 | done | 6937115 | `jobs.py` (atomic store, validate, schema 1), `jobs_cli.py` (`enqueue`, `jobs list/show/cancel/seen`), dispatched at top of `main()`, 39 tests; deviations: cancel sets new state `cancelled`; `deliver` gained an `ask` field (project deliver_ask); job ids accept a unique prefix; `jobs_cli.main(argv, host=)` takes the plaud_transcribe module to avoid a second import of `__main__` |
| K3 | B3, B5 | done | 3ec0a21 | `forced_project`/`interactive` keyword-only on both process functions, `visuals.set_interactive`/`PLAUD_NONINTERACTIVE`, richer result dict, 12 tests in `tests/test_noninteractive_pipeline.py` |
| K4 | B2, B6, B8, B9, B7 retry | done | 1f87447 | `runner.py` (flock, step machine, resume, failure notify, `--once/--job/--lock-file/--days`), `jobs retry`, 26 tests in `tests/test_runner.py` |
| K5 | B1, B4 | done | af01afa | `load_runner_env` (direnv exec, only 3 keys + PATH), `preflight` before any paid call, Wispr/recorder jobs resolved without the Plaud API, `install-runner`/`uninstall-runner` (WatchPaths + StartInterval 60), shared `--help` test; uncommitted. B1 (starts within 10 s after enqueue, live) pending a manual check by the manager after install |
| K6 | C1, C2, C3, C5, C7 | done | `3fb2919` | `publish.py` commit/push steps registered in runner (worktree on `transcripts/<id>`, push to main with rebase retry, safe ff), 22 tests in `tests/test_publish_main.py`; uncommitted; pr mode fails with "pr lands in K7" |
| K7 | C4, C6, D8 | done | | `publish.push_pr` (push branch if origin tip differs, reuse open PR via `gh pr list --head`, else `gh pr create`), `GH_BIN`/`runner.gh`, knobs in `runner --help`, 12 tests in `tests/test_publish_pr.py`; uncommitted |
| K8 | D1-D4 | pending | | cc-tree deliver step + stage-1 e2e test + README |
| K9 | F1, F2, F3 | pending | | Display overrides + `display` command |
| K10 | F4, F6, F7 | pending | | SwiftBar plugin, read-only status + install-swiftbar |
| K11 | F5, D2, D9 | pending | | SwiftBar actions: toggles, pause, retry/cancel/seen |
| K12 | G1, G3 (logic) | pending | | Plan logic without UI: pending list, suggestions, build_job |
| K13 | G1, G2, G4 | pending | | Textual plan screen + tests (needs venv with textual) |
| K14 | E1, E2, E3 | pending | | Armed jobs: meeting_ready, promotion, timeouts |
| K15 | E4, F menu, docs | pending | | Arm entry points (enqueue/plan/menu) + final docs |

## Future work

_(out-of-scope proposals from implementation are logged here)_

- Cuts combined with merge: extend merged mode (`process_merged_recordings` gains a `cuts` parameter) so a job can have `merge=true` and cuts. Today `validate_job` rejects the combination.
- `local:<hash>` recordings cannot be enqueued usefully: the runner cannot map the hash back to a file (K4). `enqueue` could reject them, or jobs could carry the source path.
- Cancelling a `running` job (`jobs cancel` refuses it today; the runner already stops between steps if the file says `cancelled`).
- PR mode retry after the earlier PR was closed or merged opens a new PR for the same branch (`gh pr list` sees open PRs only, K7).
