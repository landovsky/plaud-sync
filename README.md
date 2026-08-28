# plaud-sync

Turn [Plaud.ai](https://plaud.ai) voice recordings into clean, structured, project-filed Markdown — automatically.

`plaud-sync` pulls your recordings from the Plaud cloud, transcribes them, summarizes and classifies them with an LLM, and drops the results straight into the right project repository. No web app, no copy-pasting, no manual file wrangling.

> ⚠️ **Unofficial.** This tool talks to Plaud's private, undocumented API (reverse-engineered from the web app). It can break whenever Plaud changes their backend. It is not affiliated with or endorsed by Plaud.ai. Use it on your own account, at your own risk.

---

## The problem it solves

Plaud does a great job recording and transcribing meetings. But the path from *"recording done"* to *"information is actionable in my project"* is all friction:

1. Open the Plaud web app
2. Wait for / trigger transcription
3. Copy-paste or download the transcript
4. Save it to the right file, in the right project folder
5. Point an AI tool at it to actually do something

So recordings pile up unprocessed for days. `plaud-sync` collapses steps 2–5 into one command. Say *"process today's meetings"* and structured transcripts + summaries land in the correct repo, ready for AI-assisted follow-up.

**What you get per recording:**

- 🎙️ A **diarized, timestamped transcript** (speaker-labeled, filler removed)
- 📝 A **structured summary** — context, key points, decisions, action items, open questions
- 🏷️ **Automatic classification** — recording type, tags, and which project it belongs to
- 📂 **Automatic routing** — Plaud folders / content map to project directories
- 🔁 **Incremental sync** — state tracking means nothing gets downloaded or transcribed twice
- ✅ **Optional auto-commit** — commit each transcript into its destination repo (opt-in via config)

---

## How it works

```
Plaud device
  └─► Plaud cloud   (auto-syncs over Bluetooth / phone app)
        └─► plaud-sync
              1. list recordings for the last N days
              2. download audio (Fernet-decrypt the pre-signed URL)
              3. transcribe  → ElevenLabs Scribe v2 (diarized)
              4. summarize + classify + clean  → Claude
              5. route to the right project dir, write Markdown
              6. (optional) git commit into the destination repo
```

The repo ships **two tools** — pick based on whether you want your own transcription/summaries or Plaud's:

| Tool | What it does | Uses |
|------|--------------|------|
| **`plaud_transcribe.py`** | Full pipeline. Downloads the **audio**, transcribes it yourself (ElevenLabs), then summarizes/classifies with Claude. Best quality + control. | ElevenLabs + Claude |
| **`plaud_sync.py`** | Lighter. Downloads **Plaud's own** transcript, outline, and summaries (whatever Plaud already generated) and files them as Markdown. No re-transcription. | Plaud only |

Most people want **`plaud_transcribe.py`**. Use `plaud_sync.py` if you're happy with Plaud's built-in output and want to skip the transcription/LLM cost.

---

## Quickstart

### 1. Prerequisites

- **Python 3.10+** (standard library only, plus `cryptography`)
- Install deps: `pip install -r requirements.txt`
- **`git`** on your `PATH`
- For `plaud_transcribe.py`:
  - An **[ElevenLabs](https://elevenlabs.io) API key** (Scribe v2 speech-to-text)
  - The **[Claude Code CLI](https://claude.com/claude-code)** (`claude` on your `PATH`) — required for summarization. A pluggable LLM gateway (Anthropic API / OpenAI-compatible) is on the roadmap; **PRs welcome**.

### 2. Get your Plaud token

The Plaud API uses a long-lived bearer token (JWT, ~10 months). To grab it:

1. Log in to <https://web.plaud.ai>
2. Open DevTools → **Network** tab
3. Click any request to `api.plaud.ai` and copy the `Authorization: Bearer <token>` value

### 3. Set your secrets

```bash
export PLAUD_BEARER_TOKEN="eyJhbGci..."      # from step 2
export ELEVENLABS_API_KEY="sk_..."           # for plaud_transcribe.py
export CLAUDE_CODE_OAUTH_TOKEN="..."         # Claude Code auth (or just have `claude` logged in)
```

> Tip: keep these in a `.envrc` (with [direnv](https://direnv.net)) or your shell profile. `.envrc` is git-ignored in this repo.

### 4. Configure your projects

Scaffold a starter config (or copy [`config.example.json`](config.example.json)):

```bash
./plaud_transcribe.py init      # or: ./plaud_sync.py init
```

Then edit `~/.plaud-sync/config.json` — `projects` maps a classification label to the repo where those recordings should land:

```json
{
  "projects": {
    "playgrove":  "~/git/playgrove/docs/transcripts",
    "medovia":    "~/git/medovia/docs/transcripts",
    "greenfield": "~/git/greenfield/docs/transcripts"
  },
  "default_output": "~/git/plaud-sync/docs/transcripts",
  "language": "en",
  "timezone": "Europe/Prague"
}
```

`default_output` (where unmatched recordings go) is optional — it defaults to this tool's own `docs/transcripts/`.

**How a recording finds its project.** Meetings rarely say the project name out
loud, so classification also gets the names of recordings already filed under
each project — a BOM design sync matches `playgrove` because the last three BOM
syncs went there. That history builds itself as you process recordings; for a
project with little of it, `project_hints` gives the classifier something to
match on:

```json
"project_hints": {
  "playgrove": "BOM, kusovníky, manufacturing, pricing engine",
  "medovia":   "patient portal, pharmacy reservations"
}
```

### 5. Run it

```bash
# Interactive: pick which recordings to process
./plaud_transcribe.py

# Non-interactive: process everything unprocessed from the last 30 days
./plaud_transcribe.py --all
```

That's it. Transcripts and summaries appear under the mapped project directory. (Set `"auto_commit": true` in your config to also commit them into that repo.)

---

## Usage

### `plaud_transcribe.py` — the full pipeline

```bash
./plaud_transcribe.py [command] [options]
```

**Commands**

| Command | Description |
|---------|-------------|
| `process` *(default)* | Interactively select and process recordings |
| `list` | Show recordings, their status (✓ done · ~ incomplete · – skipped · blank pending), and where each processed recording's output landed. Local [Wispr Flow](#wispr-flow-meetings) meetings appear here too, in a `Plaud`/`Wispr` provider column |
| `move <ref> <project>` | Move an already-processed recording to a different project |
| `init` | Write a starter config to `~/.plaud-sync/config.json` |

**Options**

| Flag | Default | Description |
|------|---------|-------------|
| `--days N` | `30` | How many days back to fetch |
| `--all` | off | Process every unprocessed recording, no prompts |
| `--language` | config `language`, else `cs` | Transcription hint + which prompt template (`cs`/`en`) is used |
| `--force` | off | For `init`: overwrite an existing config |

**Interactive prompt commands** (in `process` mode):

```
1,3      process recordings 1 and 3
s1,3     skip recordings 1 and 3 (won't show again)
m1,2     merge recordings 1 and 2 into one session, then process
all      process everything listed
q        quit
```

**Merging** is handy when one meeting spans several recordings — they're concatenated and summarized as a single session.

**Custom summarization instructions.** After a selection, `process` asks for one-off
instructions for that batch:

```
instructions> put most focus on the BOM versioning, pick interesting remarks from the rest
```

They're *appended* to the prompt template, not a replacement — the output format,
classification and section structure stay the same; you're only steering emphasis
and level of detail. Press Enter to skip. They apply to every recording in that
selection (or to the merged session) and are recorded in `state.json` alongside
the entry, so you can later see what a summary was steered toward. `--all` runs
without prompting and uses no custom instructions.

**Moving** a recording after the fact:

```bash
# non-interactive
./plaud_transcribe.py move 59485297e34e... medovia
./plaud_transcribe.py move 2026-05-04-patient-portal-...  medovia   # by output dir name

# interactive: fuzzy-pick a transcript, then a project
./plaud_transcribe.py move
```

### `plaud_sync.py` — pull Plaud's native output

```bash
./plaud_sync.py init              # write a default config
./plaud_sync.py list --days 7     # list recent recordings + sync status
./plaud_sync.py sync --days 7     # download transcripts/outlines/summaries
./plaud_sync.py sync --dry-run    # show what would download, without doing it
./plaud_sync.py sync --folder Medovia   # only one Plaud folder
```

---

## Output

Each recording becomes a dated, slugified directory in its project:

```
docs/transcripts/
  2026-05-04-14-30-medovia-patient-portal-kickoff/
    summary.md         # structured summary with YAML front matter
    transcript.md      # cleaned, diarized, timestamped transcript
    metadata.json      # file_id, duration, project, tags, raw metadata
```

`summary.md` and `transcript.md` carry YAML front matter for easy indexing:

```yaml
---
title: Medovia patient portal — kickoff
date: 2026-05-04
type: meeting
tags: [medovia, portal, onboarding]
project: medovia
language: cs
duration: 47m
plaud_id: 59485297e34e7f472060c0f6baa90648
---
```

(`plaud_sync.py` writes a slightly different set: `transcript.md`, `outline.md`, and one file per Plaud summary type — `task-assignment.md`, `meeting-report.md`, `meeting-minutes.md`.)

---

## How it decides where things go

`plaud_transcribe.py` classifies each recording with Claude and asks it to pick the best-matching **project key** from your config (or `default` if nothing fits). The result is validated against your real config keys, so a hallucinated project name falls back to `default` rather than mis-filing.

`plaud_sync.py` routes by **Plaud folder** instead: it reads each recording's folder tag and looks it up in `folder_mapping`. A folder mapped to `null` (e.g. `"Archive": null`) is explicitly skipped.

---

## Customizing the summary

The summarization prompt lives in [`prompts/`](prompts/) as Markdown — one file per language, so it's easy to find and tweak:

- `prompts/summarize.en.md` — English headings (Context / Key points / Decisions / Action items / Open questions)
- `prompts/summarize.cs.md` — Czech headings (Kontext / Klíčové body / Rozhodnutí / Akční položky / Otevřené otázky)

The template is chosen by `--language en|cs` (or the config `language` key); an unknown language falls back to English. Edit these files to change tone, sections, or output — no code changes needed. Runtime placeholders (`{{FILENAME}}`, `{{PROJECT_LIST}}`, `{{TRANSCRIPT}}`, …) are filled in before the prompt is sent.

---

## Bookmarks (device marks)

Pressing the button on the Plaud device during a recording drops a **bookmark** — a `mark_memo` in Plaud's data (`mark_type: 4`, "hard flag"). It's just a timestamp: no text, no audio, only "pay attention to this moment." What it *means* is up to you.

When enabled, `plaud_transcribe.py` fetches these marks and inlines them into the transcript at the matching moment:

```
[02:57] speaker_0: …takže téma eval smyčka.
🔖 ——— BOOKMARK @ 03:00 (device mark) ———
[03:07] speaker_1: A kdo je teda eval…
```

This does two things, out of the box, with **no changes to the summarization prompt**:

1. **Navigable pins** — the marker survives into the cleaned transcript verbatim, so you can jump to the moments you flagged.
2. **A soft nudge to the summary** — the model reads the marker as "this mattered" and tends to give the marked region a bit more resolution (e.g. an extra action item). It's an emergent bias, not a hard rule — deliberately so, since the button's meaning varies per person.

**Opt-in.** Off by default. Enable it in `~/.plaud-sync/config.json`:

```json
{ "process_bookmarks": true }
```

It's graceful: recordings without marks are untouched (output is byte-identical to the feature being off), and a failed mark fetch never blocks transcription.

---

## Wispr Flow meetings

[Wispr Flow](https://wisprflow.ai) records meetings and transcribes them **locally** — so unlike Plaud, the diarized, speaker-named transcript already sits on your disk. When enabled, `plaud_transcribe.py` reads those meetings straight from Wispr's local store and runs them through the *same* pipeline as Plaud recordings: `list` and `process` show them alongside your Plaud recordings (with a `Plaud`/`Wispr` provider column), and processing classifies, summarizes, and routes each one into the right project repo.

Because Wispr already produced the transcript, the Wispr path **skips the audio download and ElevenLabs entirely** — there's no per-meeting transcription cost, only the Claude summarization step. (This also means it works even before Wispr has finished generating its own summary.)

**What you get per meeting:**

- `summary.md`, `transcript.md`, `metadata.json` — exactly like a Plaud recording (`source: wispr` in the front matter)
- `wispr-summary.md` — **Wispr's own summary**, preserved next to yours so you can compare them (or switch to Wispr's later) without re-running anything. Turn it off with `"keep_wispr_summary": false`.

**Opt-in.** Off by default. Enable it in `~/.plaud-sync/config.json`:

```json
{
  "wispr": {
    "enabled": true,
    "data_dir": "~/Library/Application Support/Wispr Flow",
    "keep_wispr_summary": true
  }
}
```

It's graceful and read-only: the Wispr SQLite store is opened read-only (safe while the app is running), a Wispr meeting whose transcript isn't on disk yet is simply skipped, and any Wispr read error degrades to "no Wispr meetings" without ever blocking the Plaud pipeline. `data_dir` only needs setting if your Wispr Flow store isn't at the macOS default shown above.

> **Note:** the Plaud token is still required — Wispr meetings are merged *into* the Plaud listing, not a standalone mode. If you only use Wispr, you still need a valid Plaud token for the list step to succeed.

---

## State & incremental sync

State lives in `~/.plaud-sync/state.json` and is what makes sync idempotent:

- **`plaud_transcribe.py`** tracks each recording under `transcribed` with a status:
  - `done` — fully processed and filed
  - `transcribed` — audio was transcribed but summarization failed; retried next run (shown as `[incomplete]`)
  - `skipped` — you skipped it, or it was auto-skipped (recordings under 10 seconds are dropped automatically)
- **`plaud_sync.py`** tracks downloads under `downloaded` keyed by `version_ms`, so an updated recording re-downloads but an unchanged one doesn't.

Downloaded audio is cached under `docs/audio/` and intermediate transcripts under `docs/working/` (both git-ignored), so re-runs are cheap and never re-download or re-transcribe.

---

## Configuration reference

`~/.plaud-sync/config.json`:

| Key | Used by | Description |
|-----|---------|-------------|
| `projects` | transcribe | `{ "label": "~/path" }` — classification targets. Claude picks the best-matching label; unknown → `default` |
| `folder_mapping` | sync | `{ "Plaud Folder": "~/path" }` — routes by exact Plaud folder name (use `null` to skip). Also used as a fallback source of `projects` by transcribe |
| `project_hints` | transcribe | `{ "label": "topics, systems, people" }` — extra evidence for classification. Optional; recent recordings per project are used regardless |
| `default_output` | both | Where unmatched recordings go (defaults to this tool's `docs/transcripts/`) |
| `language` | transcribe | Default language / prompt template (`cs`, `en`) when `--language` isn't passed |
| `auto_commit` | transcribe | `true` to git-commit each output into its destination repo (default off) |
| `elevenlabs_model` | transcribe | ElevenLabs model id (default `scribe_v2`) |
| `fernet_key` | transcribe | Override Plaud's audio-URL decryption key if they rotate it |
| `process_bookmarks` | transcribe | `true` to inline device bookmarks into the transcript (default off — see [Bookmarks](#bookmarks-device-marks)) |
| `wispr` | transcribe | Ingest local [Wispr Flow](#wispr-flow-meetings) meetings: `{ "enabled": true, "data_dir": "~/…/Wispr Flow", "keep_wispr_summary": true }` (default off) |
| `timezone` | both | IANA tz for the Plaud API (default `Europe/Prague`) |
| `token` | both | Optional: Plaud token here instead of the env var |

**Environment variables**

| Var | Purpose |
|-----|---------|
| `PLAUD_BEARER_TOKEN` | Plaud API token (overrides `config.json`) |
| `ELEVENLABS_API_KEY` | ElevenLabs Scribe v2 (transcribe only) |
| `CLAUDE_CODE_OAUTH_TOKEN` | Claude Code auth for summarization (or just have `claude` logged in) |

---

## The Plaud API

Everything here was reverse-engineered by capturing traffic from the Plaud web app. The full annotated reference — endpoints, JWT structure, response formats, the S3 content layout, and the response encryption scheme — is in **[DESIGN.md](DESIGN.md)**. (Raw captured API responses aren't published, since they contain live account data.)

A couple of things worth knowing if it breaks:

- **Token expired?** The scripts detect a `401` and tell you to grab a fresh token from DevTools.
- **Fernet decryption failed?** Plaud rotated their audio-URL encryption key (a *global* key, so it breaks for everyone at once). Set a fresh one via `"fernet_key"` in config, or just use `plaud_sync.py`, which needs no audio decryption. This is the most fragile part of the `transcribe` pipeline.

---

## Limitations & notes

- **Private API** — expect it to break on Plaud backend changes.
- **Language** — ships `cs` and `en` prompt templates in [`prompts/`](prompts/); pick with `--language` or the config `language` key. Other languages fall back to the English template.
- **Token** — the Plaud JWT is long-lived (~10 months) but manual: copy it from DevTools, and re-copy when a `401` says it expired.
- **Costs** — `plaud_transcribe.py` spends ElevenLabs and Claude credits per recording. `plaud_sync.py` is free (only downloads what Plaud already made).
- **Personal defaults** — the example project paths (`~/git/playgrove`, etc.) are placeholders; set your own in `config.json`.
- **Secrets** — never commit real tokens. `.envrc`, `docs/audio/`, and `docs/working/` are git-ignored.

---

## License

Released under the [MIT License](LICENSE).
