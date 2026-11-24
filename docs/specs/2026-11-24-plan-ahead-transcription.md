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
  "deliver": {"node": "hriste:4k2p", "mode": "now", "note": "pull the action items into…"},
  "state": "queued",                          // queued|running|done|failed|held|armed
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

## Progress

_Managed via `/manage feature`; updated per chunk._

| Chunk | Parts | Status | Commit | Notes |
|-------|-------|--------|--------|-------|
| _plan pending_ | | | | |

## Future work

_(out-of-scope proposals from implementation are logged here)_
