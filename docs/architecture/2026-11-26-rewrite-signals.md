# When to rewrite plaud-sync: signals, costs, recommendation

Status: analysis · 2026-11-26 · updated 2026-11-27: step 1 (the source registry, `sources.py`) is
built, with calls as its first new source (spec Part A). §1.2, S1, S2, S6 and S10 give the
readings after it; everything else is the 11-26 analysis as it was.
Inputs: `DESIGN.md`, `README.md`, `docs/specs/2026-11-24-plan-ahead-transcription.md`,
`docs/specs/2026-11-26-phone-intake-session.md`, git log up to `a3f4ddc` (PR #7), the code,
the shape (keys and counts only) of `~/.plaud-sync/state.json` and the job files, a read-only look at HP.

## In short

- **Restructure, don't rewrite.** 4 of the 10 signals below are red. All of them sit in two
  seams: how sources plug in, and how a job is tied to the Mac. The core that PR #6 built (the job
  file, the runner's steps, publish, deliver) is green. A rewrite would throw that away and
  re-earn every source quirk while the tool is in daily use.
- **Start with a source registry, built together with spec Part A (calls).** As written, Part A
  names 2 of the 8 places a new source has to be added. A `call:` job would then need a working
  Plaud token on the runner, and its audio would not be copied off the phone up front.
- **Then make the job portable**: a sealed bundle (audio or transcript + images + metadata + the
  job) that any machine can process. That is what makes "HP does it overnight" possible.
- **Rewrite trigger:** after the registry and the bundle exist, adding a source still needs more
  than 3 edits outside its own module, or the pipeline core still branches on the source.

---

## 1. Current shape

### 1.1 Sources

Done recordings in `state.json`, by month, show what is actually used:

| Month | Plaud | Wispr | Recorder | Local |
|---|---:|---:|---:|---:|
| 2026-08 | 30 | 7 | | |
| 2026-09 | 16 | 21 | | |
| 2026-10 | 2 | 26 | 8 | |
| 2026-11 | 4 | 17 | 8 | 2 |
| all time | 60 | 71 | 16 | 2 |

In November, 27 of 31 recordings came from sources that only the Mac can read.

| Source | Module | Read through | Id | Bound to |
|---|---|---|---|---|
| Plaud | `plaud_transcribe.py` (API calls inline) | Plaud API, Fernet-decrypted listing, MP3 | Plaud hex id | cloud |
| Wispr Flow | `wispr_flow.py` (411) | read-only sqlite + meeting folders; transcript already local | `wispr:<uuid>` | Mac app |
| Google Recorder | `google_recorder.py` (190) | adb MediaStore query + pull | `recorder:<datetaken>` | phone on the Mac |
| Local file | `plaud_transcribe.local_recording` | a path on the command line, interactive only | `local:<hash>` | the machine with the file |
| Phone calls (spec) | `phone_calls.py` (planned) | adb MediaStore on Download + call log | `call:<stamp>` | phone on the Mac |
| Screens, photos | `visuals.py` (753), `autocapture.py` (1594) | `~/Desktop`, adb photo pull, autocapture scenes | n/a | Mac + phone |

### 1.2 Where a source is registered

**After the registry (2026-11-27).** `sources.py` holds one `Source` per source (prefix, label,
lister, phone pull, credentials, front-matter key, source path, summarizer context, config
help). Of the 11 places below it now owns 1–7, 9's source path and origin, 10, and the
front-matter id key: `list`, `plan` and the runner all call `sources.list_recordings` (the runner
with `only=` the job's sources), an unknown prefix raises `UnknownSource` at `enqueue` and at
preflight, and `REQUIRED_ENV` is built from the sources. Still source-specific: the Wispr
"transcript already exists" branches in 8 and 9 (Wispr is the only source with a ready transcript)
and 11 (`visuals.windows_for`, Wispr pauses). The list below is the 11-26 reading.

Spec Part A expects two registries. There are **three listing registries**, and the code also
branches on the source in eight more places:

Listing: ids become recording dicts
1. `plaud_transcribe.list_all_recordings` (`plaud_transcribe.py:257`): `list`, `process`, and the
   runner when a job has a Plaud id. A Plaud failure stops the run.
2. `plan_logic._list_sources` (`plan_logic.py:54`): `plan`, `enqueue`, and `intake data` (spec).
   A Plaud failure turns into a note.
3. `runner.step_transcribe` (`runner.py:185`), the branch for jobs without a Plaud id: it lists
   Wispr and Recorder itself and does not call Plaud at all.

Dispatch: `if source == …`
4. `runner.NON_PLAUD_PREFIXES` / `_is_plaud` (`runner.py:49`, `:126`). An id whose prefix isn't
   listed here is **treated as a Plaud id**. It then needs the Plaud token and the Plaud listing.
5. `runner.preflight` (`runner.py:148`): "every source except Wispr needs ElevenLabs".
   `REQUIRED_ENV` is one fixed tuple, so a source with its own credential can't be added without
   making that credential required for every job.
6. `plaud_transcribe.source_label` (`:270`). An unknown source shows up as "Plaud".
7. `plaud_transcribe.obtain_audio` (`:354`). An unknown source falls through to the Plaud download.
8. `plaud_transcribe._process_recording` (`:1190`): folder, `_wispr` info, `source_path` per
   source, origin label, step count, and the Wispr "transcript already exists" branch (5 conditions).
9. `plaud_transcribe._process_merged` (`:1365`): the same Wispr branch. A merge of mixed sources
   is labelled `plaud`.
10. `plaud_transcribe.prefetch_phone_files` (`:1790`): copies only `recorder` audio off the phone.
11. `visuals.windows_for` (`visuals.py:108`): pause segments for Wispr only.

Visual sources: `visuals.collect` (`visuals.py:441`) has its own registry: Desktop folders,
`_pull_phone`, and `autocapture.select`.

Whether a Wispr meeting has ended is decided in three modules: `autocapture.wispr_live_meeting`
(`autocapture.py:820`), `runner.meeting_ready` (`runner.py:392`) and `plan_logic.live_meeting`
(`plan_logic.py:86`).

### 1.3 Unit of work: the job

`~/.plaud-sync/jobs/<id>.json`, schema 1, written atomically. It holds recording **ids**, merge,
cuts, instructions, project, push, deliver (node, mode, note, ask, host), state, steps
(`transcribe`, `commit`, `push`, `deliver`) and result (paths, commits, pr, delivered).

- The job carries ids, not data. The runner turns them back into recordings by listing the
  sources again (`_resolve_recordings`) on whatever machine it runs on. So a job can only run
  where all of its sources can be listed.
- `transcribe` is a single step that does everything: fetch audio, ElevenLabs, phone photos,
  autocapture selection, captions, `claude -p`, and writing the output. `commit`, `push` and
  `deliver` are separate steps and can be resumed.
- The hand-off already crosses machines (`ssh <host> zsh -lc 'cc-tree deliver …'`, PR #7). The
  processing doesn't.

### 1.4 What ties it to the Mac

| Tie | Where | Needed for |
|---|---|---|
| adb and the phone | `google_recorder.py`, `visuals.py` (photos), `adb_link.py` (Wi-Fi), calls (spec) | Recorder, calls, phone photos |
| Wispr Flow's store | `wispr_flow.py` (`~/Library/Application Support/Wispr Flow`), also read by `autocapture.py` and `runner.py` | Wispr, arming a job on a live meeting |
| Screen capture | `autocapture.py`: `screencapture`, JXA, the Screen Recording grant on a compiled app, scenes in `~/Pictures/plaud-autocapture/<session>/` (1 to 31 MB per meeting) | autocapture |
| Desktop screenshots | `visuals.screenshot_dirs` (`~/Desktop`) | manual screenshots |
| macOS UI | terminal-notifier, the SwiftBar plugin, osascript/Ghostty (intake spec) | status, failure notices, intake |
| launchd | runner agent (WatchPaths on the jobs dir + StartInterval 60), autocapture watch agent, intake watch (spec) | unattended runs |
| Credentials | `runner.load_runner_env` → `direnv exec <repo> env -0` → `.envrc` (sops + the age key path that `install-runner` writes; the Claude token from a shared credentials file) | every paid call |
| Absolute paths | `state.json` `transcribed.*.output_dir` (absolute `/Users/…` in all 149 done entries), job `result.paths` and `result.recordings[].output_dir`; caches `docs/audio` (974 MB) and `docs/working` (579 MB) inside the code checkout | re-processing, `move`, publish |
| One local writer | `state.json` is rewritten whole, the runner's flock, the jobs dir | dedupe, "what is pending" |

**HP today** (read-only check, 2026-11-26): its checkout is at `85da35d` (2026-11-17) with local
changes, so it predates PR #6 and #7. It has no `~/.plaud-sync` (no config, state or jobs) and
no adb. `claude`, `cc-tree`, `ffmpeg`, `direnv` and `sops` are on its login-shell PATH. Whether
its keys decrypt this repo's secrets was not checked (secrets weren't read).

### 1.5 Sizes

| Module | Lines | | Module | Lines |
|---|---:|---|---|---:|
| `plaud_transcribe.py` | 2343 | | `jobs_cli.py` | 622 |
| `autocapture.py` | 1594 | | `plaud_sync.py` (Plaud-native tool) | 601 |
| `visuals.py` | 753 | | `publish.py` | 453 |
| `plan_app.py` | 694 | | `wispr_flow.py` | 411 |
| `runner.py` | 650 | | others (6 modules + SwiftBar plugin) | 1415 |

That is about 9.5k lines of non-test Python, against 1.7k at the public release (2026-07-14).
`plaud_transcribe.py` grew 1159 → 1458 (Wispr) → 1854 (Recorder, local) → 2171 (when the
11-24 spec set it as a ceiling) → 2343, and 31 of the 56 non-merge commits touch it. Tests:
6044 lines, 488 pass and 1 is skipped, in 33 s.

---

## 2. Rewrite signals

Each signal is a test you can repeat, with a threshold and today's reading.

| # | Signal | How to measure | Red when | Today | |
|---|---|---|---|---|---|
| S1 | Cost of adding a source | edits outside the new module, from a paper dry run (§2.1) | > 3 | 11-26: Google Meet 11, calls 8. 11-27: an audio source 1 (its `Source` entry; a test registers a dummy source and nothing else), Meet with its own transcript 3 (+ the two Wispr transcript branches) | 🟢 |
| S2 | One list of sources | listing registries, and what happens with an unknown id prefix | > 1, or an unknown prefix is accepted silently | 11-26: 3 registries with 3 different failure rules; an unknown prefix is taken as Plaud. 11-27: 1 (`sources.list_recordings`; strict for `list`, notes for `plan`); an unknown prefix is refused by `enqueue` and fails preflight | 🟢 |
| S3 | The same seam fixed again and again | fix commits per seam in 60 days (§2.2) | ≥ 3 in one seam caused by our own structure | timeline 5 (+2 caught while building), phone 5 | 🔴 |
| S4 | Machine independence | can a queued job finish on HP while the Mac sleeps? | no | no, for every source, Plaud included (§1.4) | 🔴 |
| S5 | Stubs needed to test a job | stubs needed to push one job through the real pipeline code | > 5, or no such test | no such test exists. The runner e2e test stubs `process_recording` itself plus 13 more names; the pipeline test stubs 8 names plus stdin, tty and select | 🟡 |
| S6 | Size and churn of the main module | lines, and the share of commits that touch it | above the 11-24 ceiling (2171) | 2343, 31 of 56 commits (11-27: 2348, with calls added) | 🟡 |
| S7 | How far one job field spreads | modules touched to add one job field | > 3 | `deliver.host`: 8 modules (PR #7). Partly fair, because each surface (plan screen, `enqueue`, and soon intake) has to offer the field | 🟡 |
| S8 | Platform fixes | launchd, TCC, PATH and env fixes in 60 days | rising after a move to another machine | 8 (§2.2). Inherent to macOS; a rewrite would pay them again | 🟡 |
| S9 | The core contract holds | did the job schema, steps or resume need a breaking change since 11-24? | yes | no: schema 1, and PR #7 only added a field | 🟢 |
| S10 | Suite health | pass rate and time | failing or slow | 488 pass in 33 s (11-27: 542 in 33 s) | 🟢 |

The red ones fall into two seams: **sources** (S1, S2, S3) and **the job's tie to the Mac** (S4).
(11-27: S1 and S2 are green after the registry. S3's timeline seam is not addressed: the
registry has no `timeline` owner yet.)
Neither is the core. That is the main argument for restructuring rather than rewriting.

### 2.1 Paper dry run: adding Google Meet

Assumed shape, not verified for the account in use: Meet's own recording lands in the organiser's
Google Drive as a video file, with a transcript document when transcripts are on. That means a
Google OAuth credential, a cloud listing (like Plaud), audio pulled out of a video (ffmpeg), and
maybe a transcript that already exists (like Wispr).

| # | Edit | Why |
|---|---|---|
| 0 | new `google_meet.py`: `list_recordings`, `pull`, transcript reader | the source itself |
| 1 | `list_all_recordings` | `list` / `process` |
| 2 | `plan_logic._list_sources` | `plan`, `enqueue`, intake |
| 3 | runner's no-Plaud listing branch | jobs without a Plaud id |
| 4 | `NON_PLAUD_PREFIXES` | otherwise `meet:` counts as Plaud |
| 5 | `preflight` | skip ElevenLabs when Meet has a transcript; check the Google credential |
| 6 | `REQUIRED_ENV` / `load_runner_env` | the runner imports only 3 fixed keys |
| 7 | `source_label` | otherwise it shows as "Plaud" |
| 8 | `obtain_audio` | video → audio |
| 9 | `_process_recording` | `source_path`, origin, and generalising the Wispr transcript branch |
| 10 | `_process_merged` | the same Wispr branch |
| 11 | `visuals.collect` | only to use the screen-share frames as visual context |
| + | config keys, `--help` epilog, `config.example.json`, README | tunables rule |

That is 11 edits in 4 modules besides the new one. Existing tests stay green only because a new
source defaults to off.

**Spec Part A (calls) as written** names 1 and 2. It also needs 3 and 4: without them a
call-only job takes the Plaud path, requires `PLAUD_BEARER_TOKEN`, and fails whenever Plaud
rejects the token. It needs 7, 8 and 9 for the label, the pull and the metadata. And it needs
`prefetch_phone_files`: otherwise call audio is not copied off the phone up front, and "you can
unplug the phone now" is wrong. Acceptance A4 would pass only with a stubbed Plaud token.

### 2.2 Fixes by seam (git log)

| Seam | Fix commits | Modules | What it says |
|---|---|---|---|
| Recording timeline (start, end, ready, per source) | `e090de8` 09-04 time zone; `befbf76` 10-22 Wispr restart lost the second part; `bf7c692` 11-02 Wispr resume, in autocapture; `fc58173` 11-23 Wispr never refined; `03604bb` 11-24 `paused` means a pause or the end. Caught while building: the Recorder's `datetaken` is rewritten by edits (`c3420d6`); the call stamp is the call's start, not the recording's (11-26, a run had to be redone) | `wispr_flow`, `autocapture`, `runner`, `plaud_transcribe`, `google_recorder` | The fact "when did this recording start and end" has no owner. Wispr's end is read in 3 modules and was fixed in 2 of them separately. Every visual source depends on this fact too (§3.2). |
| Phone / adb | `e065d96` 09-30, `830db1d` 10-26, `6209763` 10-26, `8377098` 11-23, `e548316` 11-23 | `visuals`, `google_recorder`, `adb_link`, `plaud_transcribe` | Phone access is split between photos (`visuals`) and audio (`plaud_transcribe`). |
| macOS agents, permissions, env | `0193557`, `292aad2`, `1b60d79` 10-21; `af227f8` 10-29; `e4a88ef`, `01c7cb6`, `44777d4` 11-24; launchd PATH in `3a39ace` 11-25 | `autocapture`, `jobs_cli`, `runner` | Platform cost. It doesn't depend on our structure. |
| Publish (worktree commit, safe fast-forward) | 2 open bugs from the first live jobs, 11-26: a pre-commit hook that needs the checkout's `.env` fails in the temp worktree; an LFS pointer never matches the real file, so the safe fast-forward is skipped | `publish` | New code meeting real repos. Expected; not structural. |
| Cross-host hand-off | `3a39ace` 11-25: `deliver.host` threaded through 8 modules. The Mac's cc-tree didn't see a live HP session (open, in cc-tree) | 8 modules | Processing on the machine where the node lives would avoid the cross-machine lookup. |

---

## 3. The three directions, costed

(a) keep adding to the current shape · (b) restructure in place, step by step · (c) rewrite.

### 3.1 Many sources, new ones easy to add

**(b) in detail.** A `sources.py` registry, with one object per source:

```
prefix, label, where ("cloud" | "mac" | "phone")
list(config, days) -> recordings     # raises SourceUnavailable(note), never exits
fetch(rec, config, dest) -> audio path, or transcript text when the source has one
timeline(rec) -> start, end, pauses  # the one owner of "when", with a test per source
needs_env -> [keys]                  # what preflight and load_runner_env check, per source
phone_files(rec) -> [remote paths]   # what prefetch copies before "unplug now"
```

All three listings call `sources.list_all(config, days, notes)`. Every dispatch point calls
`sources.for_id(id)`. An unknown prefix fails with "no source for `x:`". The Wispr transcript
branch becomes "`fetch` returned text", so the pipeline core stops naming sources.

| | Size | What it gets | Main risk |
|---|---|---|---|
| (a) | S per source (Meet M) | calls next week | each source adds 8 to 11 edits; the three registries drift further apart (they already handle failures differently) |
| (b) | M | Meet = 1 module + 1 registry line + config | moving the Wispr branch out of `_process_recording` touches the path used most. Mitigation: a golden-output test per source before the move |
| (c) | L | the same as (b) | re-earning Wispr restart, resume, `paused` and never-refined, the Recorder title stamp, call timing and adb over Wi-Fi, while the tool is in daily use |

### 3.2 Visual context: autocapture today, live phone video next

Today `visuals.collect` merges three image sources into one record format
(`path, part, offset, source, klass, taken`), and each image is placed on the recording's
wall-clock window. That record format is already a good seam. The weak spot is the clock: an
image can only be anchored if the recording's start is right, and that is exactly where calls
and the Recorder went wrong (§2.2).

**What live phone video would need** (nothing exists yet):
- **Capture.** Either record-then-pull (the phone records a video file, which is pulled at
  plug-in like photos) or live (the phone streams to a machine during the meeting, for example
  through scrcpy's camera mode over adb, not tried here). Live keeps the phone tethered to that
  machine for the whole meeting.
- **A clock.** A wall-clock time for every frame, aligned with the audio's timeline. This needs a
  verified start-time rule per device and a test like spec A6.
- **Scenes.** The "settled change" detection that autocapture runs on screen thumbnails, applied
  to decoded frames. It writes the same `timeline.jsonl` + frames layout, so `autocapture.select`,
  captioning and anchoring work unchanged.
- **Scoring.** `select` scores scenes with screen signals (display, mouse, window source,
  revisits). A camera has none of them, so pointing words and recency have to be enough, tuned
  with `replay` on real recordings.
- **Privacy.** The private-screen filter works on window titles and URLs, which a camera doesn't
  have. A rule is needed, even if it is "none".
- **Volume.** An hour of phone video is several GB (an estimate), against 1 to 31 MB of
  autocapture scenes per meeting. This makes moving files and the processing machine (§3.3) matter.

| | Size | Main risk |
|---|---|---|
| (a) a fourth branch in `visuals.collect`, video → scenes inside `autocapture.py` | M | `autocapture.py` (1594 lines) becomes the second big module; the frame clock is untested |
| (b) every image source writes one scene timeline; select, caption and anchor read only that. Video → scenes is a pure function of (file, start time), so it can run on HP | M (L with live streaming) | camera scenes need new scoring, and tuning needs real recordings |
| (c) | L | losing the calibration earned from live failures: thumbnail size, revisit match, blind detection, the private-screen filter |

### 3.3 Several machines: the meeting ends, HP does the rest overnight

Target flow:

```
Mac (capture side)                          HP (processing side)
meeting ends / phone plugged in
  → intake or plan writes the job
  → seal: copy audio or transcript,  ──rsync over ssh──▶  inbox/<bundle>/
    phone photos, candidate scenes,                         → systemd path unit starts the runner
    metadata (start, end, pauses,                           → transcribe, caption, claude -p,
    counterpart, notes) + the job                             write, commit, push
  → Mac may sleep                                           → cc-tree deliver, locally on HP
SwiftBar / state.json  ◀──result: paths, commits, status─────┘
```

The bundle is a folder: `manifest.json` (schema, recordings with source, start, end, pauses,
title, counterpart, notes), `job.json` (today's job without `result`), `audio/` or `transcript/`,
`images/`, and `scenes/` (all candidate scenes in the window, because selection needs the
transcript). Cloud sources (Plaud, maybe Meet) don't need sealing: HP can fetch them itself.

What it changes:
- The runner resolves recordings from the bundle, not by listing sources again.
- Output paths are stored as project + relative path, not `/Users/…`. The caches move out of the
  code checkout.
- "Done" lives in the job. HP sends the result back, and the Mac's `state.json` keeps only
  capture-side facts (offered, skipped, shipped).
- HP needs config and credentials (sops/direnv, already installed there), a systemd user unit
  instead of the LaunchAgent, failure notices over Telegram instead of terminal-notifier, and
  clean project checkouts. The two open publish bugs (hook env, LFS) would bite there too.
- "Processed by a Claude session" splits in two. The runner does the fixed pipeline (with
  `claude -p`). The cc-tree node gets the hand-off on the same machine, which also avoids the
  open cross-machine lookup issue.

| | Size | Main risk |
|---|---|---|
| (a) a second full install on HP that lists its own sources | M | HP can reach only the cloud sources: 4 of November's 31 recordings. Wispr and the phone still need the Mac awake, and there are two `state.json` files to keep in step |
| (b) seal → ship → process anywhere, in three M pieces (bundle + runner reads bundles, paths and state split, HP install + ship + result back) | L | keeping state right across two machines (re-offers in intake, a lost result); HP's project checkouts must exist and be clean for publish |
| (c) | L | everything in (b), plus (c) of §3.1 and §3.2 |

---

## 4. Recommendation

**Restructure in place.** Order:

1. **[M] Source registry, built together with spec Part A.** Calls is the first source written
   against it; Plaud, Wispr, Recorder and Local move onto it. Done when the Google Meet dry run
   needs at most 3 edits outside its module, there is one listing function, and an unknown prefix
   fails clearly. Spec Parts B–E don't change: they write jobs through `enqueue` on the Mac.
2. **[M] A portable job: bundle, plus a runner that reads bundles, with relative output paths.**
   Prove it on one machine first: a sealed bundle processed under a temporary HOME with every
   source switched off reaches `done`.
3. **[M] HP as a processor**: systemd unit, credentials, Telegram notice, a `ship` step on the
   Mac, the result sent back, deliver run locally on HP.
4. **[M] Visual sources write one scene timeline.** Then phone video, record-then-pull first.

Splitting `plaud_transcribe.py` (pipeline core apart from the CLI) comes out of 1 and 2 on its
own. It isn't a goal by itself.

**What would change the answer**
- **To a rewrite of the pipeline core:** after step 1, a new source still needs more than 3
  edits outside its module, or after step 2 `_process_recording` still branches on the source.
  Either means the core is shaped around Plaud and should be rebuilt around the bundle. The
  sources and the job, runner, publish and deliver modules would stay.
- **To a wider rewrite:** the plan-ahead core (S9) needs a breaking change, for example a real
  multi-worker queue. Or the tool gets a second user or turns into a service.
- **To "neither yet":** no new source, video or HP processing is wanted in the next two months.
  Then do only step 1, which pays for itself with calls alone.

**Re-reading the signals:** repeat S1 (dry run), S2 (registry count) and S4 (HP check) after
each step. S3 is a `git log` over the seam's modules.

## 5. Open decisions

Recorded as ❓ nodes under the cc-tree task "When to rewrite plaud-sync (analysis)". Each has a
recommendation and a size.
