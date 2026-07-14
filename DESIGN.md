# Plaud Sync — Design Document

## Motivation

Voice recordings from meetings are a rich source of actionable information — decisions, tasks, context, and insights. Plaud.ai does an excellent job of transcribing and summarizing these recordings (with speaker labels, timestamps, outlines, and multiple summary formats). However, the workflow between "recording done" and "information is actionable in my project" has too many manual steps:

1. Open the Plaud web app
2. Wait for / trigger transcription
3. Copy-paste the transcript (or download it)
4. Save it to the right file in the right project directory
5. Point an AI tool to it for processing

This friction means recordings often sit unprocessed for days. The goal of this project is to eliminate steps 2-5 entirely, creating an automated pipeline that delivers structured transcript data from Plaud.ai directly into the relevant GitHub repository, ready for AI-assisted processing.

## Benefits

- **Zero-touch delivery**: Transcripts, summaries, and outlines appear in the right project repo automatically
- **Immediate actionability**: Say "process recent Plaud notes" and work begins — no copy-pasting, no file management
- **Structured data**: Speaker-labeled, timestamped transcripts in a consistent format across all projects
- **Full context**: Not just transcripts — outlines, task assignments, meeting reports, and meeting minutes are all captured
- **Audit trail**: Every downloaded recording is tracked with version info, preventing duplicates and enabling incremental sync
- **Multi-project routing**: Plaud folders map to GitHub repos, so recordings automatically land in the correct project

## Architecture Overview

```
Plaud Device
  → Plaud Cloud (auto-sync via Bluetooth/app)
    → plaud-sync (this tool)
      → Downloads transcript + outline + summaries
      → Routes to correct repo based on folder mapping
      → Commits to Git
        → Ready for AI processing via Claude Code / MCP / skills
```

## Plaud.ai API — Reverse-Engineered Reference

All endpoints discovered via HAR analysis of the Plaud web app (web.plaud.ai). This is an undocumented API — no official documentation exists.

### Authentication

**Method**: Bearer token (JWT)

Obtained by:
1. Log into https://web.plaud.ai
2. Open DevTools → Network tab
3. Copy the `Authorization: Bearer <token>` header from any request to `api.plaud.ai`

**JWT structure** (decoded payload):
```json
{
  "sub": "<user_id_hash>",
  "aud": "",
  "exp": 1798310963,
  "iat": 1772390963,
  "client_id": "web",
  "region": "aws:us-west-2"
}
```

- Token is long-lived (~10 months from `iat` to `exp`)
- `sub` field is the user ID (32-char hex), used in S3 paths
- Region confirms the API server to use

**Required headers on all API calls**:
```
Authorization: Bearer <jwt_token>
app-platform: web
app-language: en
edit-from: web
timezone: Europe/Prague
x-device-id: <device_id>
x-pld-tag: <device_id>
```

### Base URL

- **Global**: `https://api.plaud.ai`
- **EU**: `https://api-euc1.plaud.ai`

Current account uses **Global** (`aws:us-west-2` region).

### Endpoints

#### List Recordings

```
GET /file/simple/web?skip={offset}&limit={count}&is_trash={0|1|2}&sort_by={field}&is_desc={true|false}
```

**Parameters**:
- `skip`: Offset (0-based)
- `limit`: Records per page (up to 99999)
- `is_trash`: 0 = active only, 1 = trash only, 2 = all
- `sort_by`: `start_time`, `edit_time`
- `is_desc`: `true` = newest first

**Response** (`data_file_list[]` items):
```json
{
  "id": "59485297e34e7f472060c0f6baa90648",
  "filename": "03-24 Meeting: Q2 order pricing review",
  "keywords": [],
  "filesize": 6551238,
  "filetype": "opus",
  "fullname": "59485297e34e7f472060c0f6baa90648.opus",
  "file_md5": "...",
  "ori_ready": true,
  "version": 1775029592,
  "version_ms": 1775029592,
  "edit_time": 1775029592000,
  "edit_from": "web",
  "is_trash": false,
  "start_time": 1774357480000,
  "end_time": 1774359112000,
  "duration": 1632000,
  "timezone": 7200,
  "zonemins": 120,
  "scene": 1,
  "filetag_id_list": ["bc36b0db8d4dc6256f6684438fa58d54"],
  "serial_number": "845217630098451207",
  "is_trans": true,
  "is_summary": true,
  "is_markmemo": false,
  "wait_pull": 0
}
```

**Key fields for sync logic**:
- `is_trans` / `is_summary`: Whether transcription/summary is complete
- `start_time`: Recording timestamp (Unix ms) — use for date filtering
- `version_ms`: Version tracking — changes when content is updated
- `filetag_id_list`: Folder assignments — used for routing to repos
- `scene`: 1 = Plaud device, 101 = phone upload, 102 = web upload

#### Get File Detail (with pre-signed download URLs)

```
GET /file/detail/{file_id}
```

**Response** — the critical endpoint. Returns full metadata plus `content_list[]` with pre-signed S3 URLs for all generated content:

```json
{
  "status": 0,
  "data": {
    "file_id": "...",
    "file_name": "...",
    "duration": 1632000,
    "start_time": 1774357480000,
    "scene": 1,
    "serial_number": "...",
    "filetag_id_list": ["..."],
    "content_list": [
      {
        "data_id": "source_transaction:...",
        "data_type": "transaction",
        "task_status": 1,
        "data_link": "https://prod-plaud-content-storage.s3.amazonaws.com/...",
        "extra": { "task_id": "..." }
      },
      {
        "data_id": "source_outline:...",
        "data_type": "outline",
        "task_status": 1,
        "data_link": "https://...",
        "extra": { "task_id": "..." }
      },
      {
        "data_type": "auto_sum_note",
        "data_tab_name": "Summary",
        "data_link": "https://...",
        "extra": {
          "summ_type": "MEETING-ARRANGEMENT",
          "used_template": { "template_name": "Task Assignment" }
        }
      },
      {
        "data_type": "sum_multi_note",
        "data_tab_name": "Meeting Report",
        "data_link": "https://...",
        "extra": { "summ_type": "MEETING-REPORT" }
      },
      {
        "data_type": "sum_multi_note",
        "data_tab_name": "Meeting Minutes",
        "data_link": "https://...",
        "extra": { "summ_type": "DIM-MEETING-DETAILS" }
      }
    ],
    "embeddings": {
      "Speaker 1": [0.123, ...],
      "Speaker 2": [0.456, ...]
    },
    "extra_data": {
      "aiContentHeader": {
        "headline": "...",
        "category": "...",
        "recommend_questions": ["..."],
        "language_code": "cs"
      },
      "model": "gpt-5",
      "tranConfig": {
        "language": "cs",
        "diarization": 1,
        "llm": "auto",
        "type": "AUTO-SELECT"
      }
    }
  }
}
```

**Content types in `content_list[]`**:

| `data_type` | `summ_type` | Description | S3 filename |
|-------------|-------------|-------------|-------------|
| `transaction` | — | Transcript (speaker-labeled, timestamped) | `trans_result.json.gz` |
| `outline` | — | Topic outline with time ranges | `outline.json.gz` |
| `auto_sum_note` | `MEETING-ARRANGEMENT` | Task Assignment summary | `ai_content_part_0.json.gz` |
| `sum_multi_note` | `MEETING-REPORT` | Meeting Report | `ai_content_part_1.json.gz` |
| `sum_multi_note` | `DIM-MEETING-DETAILS` | Meeting Minutes | `ai_content_part_2.json.gz` |

Pre-signed URLs expire in **300 seconds** (5 minutes). No additional auth needed to download from S3.

#### S3 Content Formats

**Transcript** (`trans_result.json.gz`):
```json
[
  {
    "content": "Tak, pojďme se podívat na ten plán výroby.",
    "start_time": 12340,
    "end_time": 15670,
    "speaker": "Speaker 1",
    "original_speaker": "Speaker 1",
    "embeddingKey": null
  },
  {
    "content": "Ano, mám tady připravené podklady.",
    "start_time": 15890,
    "end_time": 18200,
    "speaker": "Speaker 2",
    "original_speaker": "Speaker 2",
    "embeddingKey": null
  }
]
```

Times are in **milliseconds** from recording start.

**Outline** (`outline.json.gz`):
```json
[
  {
    "start_time": 0,
    "end_time": 120000,
    "topic": "Uvod a orientace"
  },
  {
    "start_time": 120000,
    "end_time": 350000,
    "topic": "Zalozeni listku a naklady"
  }
]
```

**Summaries** (`ai_content_part_N.json.gz`):
```json
{
  "ai_content": "## Meeting Report\n\n### Topic 1: ...\n\n...",
  "header": {
    "headline": "Meeting title",
    "category": "meeting"
  },
  "summary_id": "...",
  "summ_type": "MEETING-REPORT",
  "used_template": "MEETING-REPORT",
  "state": 10
}
```

The `ai_content` field is **Markdown**.

#### Check Task Status

```
GET /ai/file-task-status
```

Returns status of recent transcription/summary tasks across all files:

```json
{
  "data": {
    "file_status_list": [
      {
        "file_id": "...",
        "task_id": "...",
        "task_status": 1,
        "task_type": "transcript",
        "auto_save": true
      },
      {
        "file_id": "...",
        "task_status": 1,
        "task_type": "summary",
        "auto_save": true
      }
    ]
  }
}
```

`task_status`: 1 = complete. Other values not yet observed.

#### Folders (Tags)

```
GET /filetag/
```

```json
{
  "data_filetag_list": [
    { "id": "8fdb1cab...", "name": "AI Guild" },
    { "id": "bb39ec15...", "name": "Archive" },
    { "id": "bc36b0db...", "name": "Playgrove" },
    { "id": "789c980f...", "name": "Medovia" },
    { "id": "d295b128...", "name": "Greenfield" }
  ]
}
```

Recordings reference folders via `filetag_id_list[]` in their metadata.

**Assigning a file to a folder**: Endpoint not yet captured. Likely `PATCH /file/{id}` with `filetag_id_list`. Needs HAR capture of the action.

#### Other Endpoints Observed

| Endpoint | Method | Purpose |
|----------|--------|---------|
| `/user/me` | GET | User profile (email, membership, seconds remaining) |
| `/user/me/settings` | GET | User settings |
| `/user/feature-access` | GET | Beta feature flags |
| `/device/list` | GET | Connected Plaud devices |
| `/config/init` | GET | App config |
| `/membership/free-trial/status` | GET | Trial status |
| `/membership/stripe/v2/prices` | GET | Pricing |
| `/share/private/get` | POST | Private share info for a file |
| `/share/public/get` | POST | Public share info for a file |
| `/ask/skills` | GET | Available AI skills (16 total) |
| `/ask/history/note` | POST | Analytics/history tracking |
| `/ask/get_recommend_questions_by_note_id` | POST | AI-suggested questions |
| `/others/web-config` | GET | Web app config |
| `/others/upload-info` | POST | Analytics event upload |
| `wss://api.plaud.ai/ws/notify` | WS | Real-time notifications |

#### AI Skills (via Ask Plaud)

16 skills available post-transcription (accessed via `/ask/skills`):

- Process review, Confirm action items, To-dos, Next meeting agenda
- Draft email, Team brief, Insights, Explain context
- Risk review, Decision support, Product insights, Sales update
- Create playbook, Leadership feedback, Generate infographic (10/day), Growth opportunities

These are invoked through the "Ask Plaud" chat interface — endpoint for programmatic invocation not yet captured.

#### Account Details

- **Membership**: Pro (Yearly, via Google Play)
- **Transcription quota**: 72,000 seconds total, ~31,236 seconds remaining
- **Region**: US West 2 (Global)
- **Language**: Czech (`cs`)
- **Diarization**: Enabled
- **AI model**: GPT-5 (used by Plaud for summaries)

## Folder-to-Repository Mapping

Each Plaud folder maps to a GitHub repository where transcripts should be delivered:

```json
{
  "Playgrove": "~/git/playgrove/docs/transcripts/",
  "Medovia": "~/git/medovia/docs/transcripts/",
  "Greenfield": "~/git/greenfield/docs/transcripts/",
  "AI Guild": "~/git/ai-guild/docs/transcripts/",
  "Archive": null
}
```

Recordings without a folder tag go to a configurable default location.

## Path to the Endgame

### Stage 1: Download script (BUILD FIRST)

A CLI tool that downloads all Plaud content for recent recordings.

**Command**:
```bash
plaud-sync --days=7 [--token=...] [--list] [--folder=<name>]
```

**Flow**:
1. Read bearer token from `~/.plaud-sync/config.json` or `--token` flag
2. `GET /file/simple/web` — list recordings from the last N days
3. Filter: skip recordings already in state file (same `version_ms`)
4. Filter: skip recordings where `is_trans` is false (not yet transcribed)
5. For each recording:
   a. `GET /file/detail/{id}` — get pre-signed URLs
   b. Download + decompress all content from S3 (`trans_result.json.gz`, `outline.json.gz`, `ai_content_part_*.json.gz`)
   c. Format and save to the target directory based on folder mapping
   d. Update state file
6. Report what was downloaded

**Output directory structure**:
```
docs/transcripts/
  2026-03-24-meeting-q2-order-pricing-review/
    transcript.md        # Speaker-labeled, timestamped transcript
    outline.md           # Topic outline with timestamps
    task-assignment.md   # MEETING-ARRANGEMENT summary
    meeting-report.md    # MEETING-REPORT summary
    meeting-minutes.md   # DIM-MEETING-DETAILS summary
    metadata.json        # Raw metadata (file_id, duration, speakers, etc.)
```

**State tracking** (`~/.plaud-sync/state.json`):
```json
{
  "last_sync": "2026-04-01T10:00:00Z",
  "downloaded": {
    "59485297e34e7f472060c0f6baa90648": {
      "version_ms": 1775029592,
      "downloaded_at": "2026-04-01T10:00:00Z",
      "target_dir": "~/git/playgrove/docs/transcripts/2026-03-24-schuzka-...",
      "folder": "Playgrove"
    }
  }
}
```

### Stage 2: Auto-trigger transcription (DEFERRED)

Plaud is preparing a feature for automatic transcription triggering. This stage is deferred until we know whether the native feature covers the need or if we still need API-level triggering.

If needed later, the approach would be:
- Detect recordings where `is_trans: false`
- POST to the transcription trigger endpoint (not yet captured)
- Poll `/ai/file-task-status` until `task_status: 1`
- Proceed with download

### Stage 3: Auto-organize into Plaud folders

Automatically assign recordings to the correct Plaud folder based on configurable rules.

**Needs**: HAR capture of moving a file to a folder to discover the endpoint (likely `PATCH /file/{id}` with `filetag_id_list`).

**Rules engine** (simple, configurable):
```json
{
  "rules": [
    { "match": { "title_contains": "playgrove" }, "folder": "Playgrove" },
    { "match": { "title_contains": "medovia" }, "folder": "Medovia" },
    { "match": { "device_sn": "845217630098451207" }, "folder": "Playgrove" },
    { "match": { "default": true }, "folder": "Archive" }
  ]
}
```

### Stage 4: MCP server / Claude Code skill

Wrap the sync tool so it's callable from within Claude Code conversations:

**As MCP tools**:
- `plaud_sync(days?, folder?)` — download recent transcripts, return file paths
- `plaud_list(days?)` — list recent recordings with status
- `plaud_status()` — show sync state, last sync time, pending recordings

**As a skill** (`/plaud-sync`):
- Runs the sync, then loads the downloaded transcripts into the conversation
- Enables: "process today's meeting notes" as a single command

### Stage 5: Full automation (endgame)

Zero-touch pipeline running on a schedule:

```
Every 15 minutes (cron):
  1. List new/updated recordings (since last sync)
  2. Skip un-transcribed (wait for Plaud's auto-transcribe)
  3. Download all content for ready recordings
  4. Route to correct repo based on folder→repo mapping
  5. Git commit + push
  6. (Optional) Notify via webhook/Slack
```

The folder-to-repo mapping config is the routing table. Recordings without a folder go to a default repo or are skipped.

## Technical Decisions

### Language

The script should be written in a language that:
- Is easy to iterate on (scripting-friendly)
- Has good HTTP/JSON/gzip support
- Can later be wrapped as an MCP server

**Recommendation**: Ruby (consistent with the team's primary stack) or shell + jq (simplest MVP). Python is also viable if the MCP server SDK preference leans that way.

### State storage

**Central** (`~/.plaud-sync/`): Token, config, and download state live here. This is cross-project — one sync state regardless of which repo you're working in.

**Project-local** (`docs/transcripts/`): The actual transcript files. Each repo gets its own transcripts based on folder mapping.

### Token management

Start with manual paste into config file. The JWT token is long-lived (~10 months). When it expires, the script should detect the 401 and prompt for a new token.

## API Discovery Artifacts

Raw API responses from HAR analysis are preserved at:
```
~/git/playgrove/tmp/plaud-api-responses/
  1-file-detail-body.json          # /file/detail/{id} response
  2-file-simple-web-limit5.json    # /file/simple/web (5 records)
  2-file-simple-web-full.json      # /file/simple/web (all 166 records)
  3-user-me-body.json              # /user/me response
  4-trans-result.json              # Transcript JSON (decompressed)
  5-outline.json                   # Outline JSON
  5-summary-part-0.json            # Task Assignment summary
  5-summary-part-1.json            # Meeting Report summary
  5-summary-part-2.json            # Meeting Minutes summary
  file-task-status-0.json          # /ai/file-task-status response
  filetag-0.json                   # /filetag/ response
  ask-skills-0.json                # /ask/skills response
  user-feature-access.json         # /user/feature-access response
```

These should be copied into the new project for reference during implementation.

## Open Questions

1. **Token refresh**: Is there a refresh token flow, or is re-login the only option when the JWT expires?
2. **Folder assignment endpoint**: Needs HAR capture of moving a file to a folder
3. **Transcription trigger endpoint**: Deferred, but may be needed if Plaud's auto-feature doesn't cover all cases
4. **Rate limits**: Plaud's API has no documented limits; OpenPlaud implements defensive backoff (1s/2s/4s). We should do the same.
5. **WebSocket notifications**: `wss://api.plaud.ai/ws/notify` could enable push-based sync instead of polling — worth investigating for Stage 5
6. **Speaker name mapping**: Plaud supports renaming speakers (Speaker 1 → "Tomáš"). The `embeddings` field contains voice fingerprints. Could we auto-map speakers across recordings?
