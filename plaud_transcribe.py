#!/usr/bin/env python3
"""plaud-transcribe: Download, transcribe, classify and summarize Plaud recordings.

Full pipeline:
  1. Fetch recording list from Plaud.ai cloud
  2. Interactive selection of unprocessed recordings
  3. Download audio
  4. Transcribe via ElevenLabs Scribe v2
  5. Classify & summarize via Claude API (Sonnet)
  6. Write outputs to target project directory
  7. Commit working files to this repo

Env vars:
  PLAUD_BEARER_TOKEN      — Plaud.ai API token (or set in ~/.plaud-sync/config.json)
  ELEVENLABS_API_KEY      — ElevenLabs API key for transcription
  CLAUDE_CODE_OAUTH_TOKEN — Claude subscription OAuth token for summarization
  ANTHROPIC_API_KEY       — Alternative: Anthropic API key (console.anthropic.com)
"""

import argparse
import gzip
import json
import os
import re
import shutil
import subprocess
import sys
import urllib.request
import urllib.error
from datetime import datetime, timezone, timedelta
from pathlib import Path

API_BASE_DEFAULT = "https://api.plaud.ai"
ELEVENLABS_API = "https://api.elevenlabs.io/v1/speech-to-text"
CONFIG_DIR = Path.home() / ".plaud-sync"
CONFIG_FILE = CONFIG_DIR / "config.json"
STATE_FILE = CONFIG_DIR / "state.json"
PROJECT_DIR = Path(__file__).resolve().parent
AUDIO_DIR = PROJECT_DIR / "docs" / "audio"
WORKING_DIR = PROJECT_DIR / "docs" / "working"

HEADERS_TEMPLATE = {
    "app-platform": "web",
    "app-language": "en",
    "edit-from": "web",
    "timezone": "Europe/Prague",
}

AUTO_SKIP_DURATION_MS = 10_000


# --- Config & State ---

def load_config():
    if CONFIG_FILE.exists():
        with open(CONFIG_FILE) as f:
            return json.load(f)
    return {}


def load_state():
    if STATE_FILE.exists():
        with open(STATE_FILE) as f:
            return json.load(f)
    return {"downloaded": {}, "transcribed": {}}


def save_state(state):
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2, ensure_ascii=False)


def get_projects(config):
    projects = config.get("projects")
    if projects:
        return projects
    fm = config.get("folder_mapping", {})
    return {k: v for k, v in fm.items() if v is not None}


# --- Plaud API ---

def get_token():
    token = os.environ.get("PLAUD_BEARER_TOKEN")
    if token:
        return token
    config = load_config()
    token = config.get("token")
    if token:
        return token
    print("Error: No PLAUD_BEARER_TOKEN set and no token in ~/.plaud-sync/config.json", file=sys.stderr)
    sys.exit(1)


def extract_sub_from_jwt(token):
    import base64
    parts = token.split(".")
    if len(parts) != 3:
        return None
    payload = parts[1] + "=" * (4 - len(parts[1]) % 4)
    try:
        data = json.loads(base64.urlsafe_b64decode(payload))
        return data.get("sub")
    except Exception:
        return None


def plaud_headers(token, config):
    headers = {**HEADERS_TEMPLATE}
    headers["Authorization"] = f"Bearer {token}"
    headers["timezone"] = config.get("timezone", "Europe/Prague")
    device_id = extract_sub_from_jwt(token) or "plaud-sync-cli"
    headers["x-device-id"] = device_id
    headers["x-pld-tag"] = device_id
    headers["Origin"] = "https://web.plaud.ai"
    headers["Referer"] = "https://web.plaud.ai/"
    headers["User-Agent"] = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"
    return headers


def resolve_api_base(token, config):
    cached = config.get("_api_base")
    if cached:
        return cached
    url = f"{API_BASE_DEFAULT}/file/simple/web?skip=0&limit=1&is_trash=0"
    req = urllib.request.Request(url, headers=plaud_headers(token, config))
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read())
    except urllib.error.HTTPError as e:
        if e.code == 401:
            print("Error: Plaud auth failed (401). Token may be expired.", file=sys.stderr)
            sys.exit(1)
        raise
    if data.get("status") == -302:
        api_base = data.get("data", {}).get("domains", {}).get("api", API_BASE_DEFAULT)
        config["_api_base"] = api_base
        return api_base
    config["_api_base"] = API_BASE_DEFAULT
    return API_BASE_DEFAULT


def plaud_get(path, token, config):
    api_base = resolve_api_base(token, config)
    url = f"{api_base}{path}"
    req = urllib.request.Request(url, headers=plaud_headers(token, config))
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        if e.code == 401:
            print("Error: Plaud auth failed (401). Token may be expired.", file=sys.stderr)
            sys.exit(1)
        raise


def list_recordings(token, config, days):
    cutoff_ms = int((datetime.now(timezone.utc) - timedelta(days=days)).timestamp() * 1000)
    data = plaud_get(
        "/file/simple/web?skip=0&limit=99999&is_trash=0&sort_by=start_time&is_desc=true",
        token, config,
    )
    recordings = data.get("data_file_list", [])
    return [r for r in recordings if r.get("start_time", 0) >= cutoff_ms]


def fetch_folders(token, config):
    data = plaud_get("/filetag/", token, config)
    return {f["id"]: f["name"] for f in data.get("data_filetag_list", [])}


def fernet_decrypt(data):
    from cryptography.fernet import Fernet, InvalidToken
    key = b"8rRmMCskL3NaUiGr-UsmEEeI8NKH-ZfeGbpRGqSLpuI="
    try:
        return Fernet(key).decrypt(data)
    except InvalidToken:
        print(
            "Error: Fernet decryption failed — Plaud.ai likely rotated their encryption key.\n"
            "To fix: open web.plaud.ai in Chrome DevTools, capture a HAR, then search the JS\n"
            "bundles for the fernet chunk and extract the new key from the bf() function.\n"
            "Update the key in plaud_transcribe.py:fernet_decrypt().",
            file=sys.stderr,
        )
        sys.exit(1)


def plaud_get_encrypted(path, token, config):
    api_base = resolve_api_base(token, config)
    url = f"{api_base}{path}"
    headers = plaud_headers(token, config)
    headers["x-encrypt-response"] = "1"
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=30) as resp:
        encrypted = resp.read()
    return json.loads(fernet_decrypt(encrypted))


def download_audio(file_id, token, config):
    AUDIO_DIR.mkdir(parents=True, exist_ok=True)
    temp_url_data = plaud_get_encrypted(f"/file/temp-url/{file_id}?is_opus=1", token, config)
    audio_url = temp_url_data.get("temp_url")
    if not audio_url:
        raise RuntimeError(f"No audio URL returned for {file_id}")
    req = urllib.request.Request(audio_url)
    with urllib.request.urlopen(req, timeout=300) as resp:
        data = resp.read()
    audio_path = AUDIO_DIR / f"{file_id}.mp3"
    audio_path.write_bytes(data)
    return audio_path


# --- ElevenLabs Transcription ---

def transcribe_audio(audio_path, language="cs"):
    api_key = os.environ.get("ELEVENLABS_API_KEY")
    if not api_key:
        print("Error: ELEVENLABS_API_KEY not set.", file=sys.stderr)
        sys.exit(1)

    result = subprocess.run(
        [
            "curl", "-s", "-X", "POST", ELEVENLABS_API,
            "-H", f"xi-api-key: {api_key}",
            "-F", f"file=@{audio_path}",
            "-F", "model_id=scribe_v2",
            "-F", "diarize=true",
            "-F", f"language_code={language}",
        ],
        capture_output=True, text=True, timeout=600,
    )
    if result.returncode != 0:
        print(f"Error: ElevenLabs failed: {result.stderr}", file=sys.stderr)
        sys.exit(1)

    return json.loads(result.stdout)


def download_gzipped_json(url):
    """Download a gzipped JSON file from S3 and return parsed content."""
    req = urllib.request.Request(url)
    with urllib.request.urlopen(req, timeout=60) as resp:
        raw = resp.read()
    try:
        return json.loads(gzip.decompress(raw))
    except gzip.BadGzipFile:
        return json.loads(raw)


def fetch_bookmarks(recording, token, config):
    """Return a sorted list of bookmark times (in seconds) for a recording.

    Bookmarks are Plaud "mark_memo" entries — a device button press during
    recording drops a mark_type:4 (HARD_FLAG) pin at a timestamp. The pin
    carries no user text, just a position, so we surface it as a neutral
    marker and let downstream summarization react to it however it wants.

    Returns [] on any failure or when the recording has no marks (graceful:
    a missing bookmark must never block transcription).
    """
    if not recording.get("is_markmemo"):
        return []
    file_id = recording["id"]
    try:
        detail = plaud_get(f"/file/detail/{file_id}", token, config)
        content_list = detail.get("data", {}).get("content_list", [])
        marks = []
        for content in content_list:
            if content.get("data_type") != "mark_memo" or not content.get("data_link"):
                continue
            for m in download_gzipped_json(content["data_link"]):
                ts = m.get("timestamp")
                if ts is not None:
                    marks.append(ts / 1000.0)
        return sorted(marks)
    except Exception as e:
        print(f"  Warning: could not fetch bookmarks for {file_id}: {e}", file=sys.stderr)
        return []


def format_diarized_transcript(transcription_data, bookmarks=None):
    words = transcription_data.get("words", [])
    turns = []
    current_speaker = None
    current_text = ""
    current_start = 0

    for w in words:
        if w["type"] != "word":
            continue
        speaker = w.get("speaker_id", "unknown")
        if speaker != current_speaker:
            if current_text:
                turns.append({"speaker": current_speaker, "start": current_start, "text": current_text.strip()})
            current_speaker = speaker
            current_text = w["text"]
            current_start = w["start"]
        else:
            current_text += " " + w["text"]

    if current_text:
        turns.append({"speaker": current_speaker, "start": current_start, "text": current_text.strip()})

    def stamp(seconds):
        return f"{int(seconds) // 60:02d}:{int(seconds) % 60:02d}"

    # Interleave bookmark markers chronologically: a mark is placed right
    # before the first turn that starts at or after it, so it sits between
    # the turn being spoken and the next one — timestamps stay ordered.
    pending_marks = sorted(bookmarks or [])
    lines = []
    for t in turns:
        while pending_marks and pending_marks[0] <= t["start"]:
            lines.append(f"🔖 ——— BOOKMARK @ {stamp(pending_marks.pop(0))} (device mark) ———")
        lines.append(f"[{stamp(t['start'])}] {t['speaker']}: {t['text']}")
    for mark in pending_marks:  # marks after the last turn
        lines.append(f"🔖 ——— BOOKMARK @ {stamp(mark)} (device mark) ———")

    return "\n\n".join(lines)


# --- Claude CLI for Classification & Summarization ---

def claude_summarize(transcript_text, recording_meta, projects):
    project_list = "\n".join(f"- {name}: {path}" for name, path in projects.items())
    duration_min = recording_meta.get("duration", 0) // 60000

    prompt = f"""You are processing a transcribed recording. Analyze it and produce a JSON response.

## Recording metadata
- Original filename: {recording_meta.get('filename', 'unknown')}
- Duration: {duration_min} minutes
- Date: {recording_meta.get('date', 'unknown')}
- Plaud folder: {recording_meta.get('folder', 'none')}

## Known projects
{project_list}

## Tasks

1. **Classify**: determine recording type, assign to a project key from the list above (or "default" if none match), pick a short descriptive name in the recording's language, add relevant tags.

2. **Summarize**: write a structured summary in the recording's language. Include these sections:
   - **Kontext** (who, what, why — 2-3 sentences)
   - **Klíčové body** (bulleted)
   - **Rozhodnutí** (bulleted, if any)
   - **Akční položky** (bulleted with owner if identifiable, e.g. "[speaker_0]")
   - **Otevřené otázky** (if any)

3. **Clean transcript**: produce a cleaned version that removes filler words (eh, mhm, no, jo used as pure filler) and irrelevant passages (background noise, phone interruptions) ONLY with high confidence. Keep all substantive content, speaker labels, and timestamps.

Respond with ONLY valid JSON, no markdown fences, no explanation:
{{
  "name": "short descriptive name in recording language",
  "type": "meeting|developer-sync|lecture|interview|brainstorm|call|other",
  "tags": ["tag1", "tag2"],
  "project": "project-key-from-list or default",
  "language": "cs|en",
  "summary": "full markdown summary",
  "clean_transcript": "cleaned transcript text"
}}

## Transcript

{transcript_text}"""

    timeout = max(300, duration_min * 30)
    try:
        result = subprocess.run(
            ["claude", "-p", "--output-format", "text"],
            input=prompt, capture_output=True, text=True, timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        print(f"TIMEOUT ({timeout}s)", file=sys.stderr)
        return None

    if result.returncode != 0:
        print(f"Error: claude CLI failed: {result.stderr[:300]}", file=sys.stderr)
        return None

    text = result.stdout.strip()
    if text.startswith("```"):
        text = re.sub(r"^```\w*\n?", "", text)
        text = re.sub(r"\n?```$", "", text)

    try:
        return json.loads(text)
    except json.JSONDecodeError:
        print(f"Error: Could not parse Claude response as JSON", file=sys.stderr)
        print(f"Response: {text[:500]}", file=sys.stderr)
        return None


# --- Helpers ---

def slugify(text):
    tr = str.maketrans(
        "áčďéěíňóřšťúůýžÁČĎÉĚÍŇÓŘŠŤÚŮÝŽ",
        "acdeeinorstuuyzACDEEINORSTUUYZ",
    )
    text = text.translate(tr)
    text = re.sub(r"[^\w\s-]", "", text.lower())
    text = re.sub(r"[\s_]+", "-", text.strip())
    text = re.sub(r"-+", "-", text)
    return text[:80].rstrip("-")


def format_duration(ms):
    total_secs = ms // 1000
    hours = total_secs // 3600
    minutes = (total_secs % 3600) // 60
    if hours > 0:
        return f"{hours}h {minutes}m"
    return f"{minutes}m"


def get_folder_name(recording, folders):
    for tag_id in recording.get("filetag_id_list", []):
        if tag_id in folders:
            return folders[tag_id]
    return None


def yaml_front_matter(meta):
    lines = ["---"]
    for k, v in meta.items():
        if isinstance(v, list):
            lines.append(f"{k}: [{', '.join(str(x) for x in v)}]")
        elif isinstance(v, str) and (":" in v or '"' in v or "\n" in v):
            lines.append(f'{k}: "{v}"')
        else:
            lines.append(f"{k}: {v}")
    lines.append("---")
    return "\n".join(lines)


# --- File Output ---

def write_outputs(classification, recording_meta, raw_transcript_text, output_dir):
    output_dir.mkdir(parents=True, exist_ok=True)

    date = recording_meta.get("date", "unknown")
    name = classification.get("name", recording_meta.get("filename", "untitled"))

    front_matter = {
        "title": name,
        "date": date,
        "type": classification.get("type", "other"),
        "tags": classification.get("tags", []),
        "project": classification.get("project", "default"),
        "language": classification.get("language", "cs"),
        "duration": format_duration(recording_meta.get("duration", 0)),
        "plaud_id": recording_meta.get("file_id"),
    }

    fm = yaml_front_matter(front_matter)

    summary_path = output_dir / "summary.md"
    summary_path.write_text(f"{fm}\n\n# {name}\n\n{classification.get('summary', '')}\n", encoding="utf-8")

    transcript_path = output_dir / "transcript.md"
    clean = classification.get("clean_transcript", raw_transcript_text)
    transcript_path.write_text(f"{fm}\n\n# Transcript: {name}\n\n{clean}\n", encoding="utf-8")

    meta_path = output_dir / "metadata.json"
    meta_path.write_text(
        json.dumps({**front_matter, "raw_recording": recording_meta}, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )

    return summary_path, transcript_path


# --- Main Pipeline ---

def process_recording(recording, token, config, folders, projects, state, language):
    file_id = recording["id"]
    filename = recording.get("filename", "untitled")
    start_ms = recording.get("start_time", 0)
    dt = datetime.fromtimestamp(start_ms / 1000, tz=timezone.utc)
    date_str = dt.strftime("%Y-%m-%d")
    duration = recording.get("duration", 0)
    folder_name = get_folder_name(recording, folders)

    recording_meta = {
        "file_id": file_id,
        "filename": filename,
        "date": date_str,
        "duration": duration,
        "folder": folder_name,
        "start_time": start_ms,
    }

    print(f"\n{'='*60}")
    print(f"  {filename}")
    print(f"  {date_str} · {format_duration(duration)} · folder: {folder_name or '—'}")
    print(f"{'='*60}")

    work_dir = WORKING_DIR / file_id
    work_dir.mkdir(parents=True, exist_ok=True)

    # 1. Download audio (skip if already downloaded)
    cached = list(AUDIO_DIR.glob(f"{file_id}.*"))
    if cached:
        audio_path = cached[0]
        size_mb = audio_path.stat().st_size / 1024 / 1024
        print(f"  [1/4] Audio cached ({size_mb:.1f} MB)")
    else:
        print(f"  [1/4] Downloading audio...", end=" ", flush=True)
        audio_path = download_audio(file_id, token, config)
        size_mb = audio_path.stat().st_size / 1024 / 1024
        print(f"{size_mb:.1f} MB")

    # 2. Transcribe (skip if already done)
    transcript_path = work_dir / "transcript.txt"
    if transcript_path.exists():
        print(f"  [2/4] Transcript cached")
        raw_text = transcript_path.read_text(encoding="utf-8")
    else:
        print(f"  [2/4] Transcribing via ElevenLabs...", end=" ", flush=True)
        transcription = transcribe_audio(audio_path, language)
        bookmarks = fetch_bookmarks(recording, token, config)
        raw_text = format_diarized_transcript(transcription, bookmarks)

        word_count = len([w for w in transcription.get("words", []) if w["type"] == "word"])
        speakers = len(set(w.get("speaker_id") for w in transcription.get("words", []) if w["type"] == "word"))
        mark_note = f", {len(bookmarks)} bookmark(s)" if bookmarks else ""
        print(f"{word_count} words, {speakers} speakers{mark_note}")

        (work_dir / "elevenlabs_raw.json").write_text(
            json.dumps(transcription, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        transcript_path.write_text(raw_text, encoding="utf-8")

    # 3. Classify & summarize via Claude
    print(f"  [3/4] Classifying & summarizing via Claude...", end=" ", flush=True)
    classification = claude_summarize(raw_text, recording_meta, projects)
    if not classification:
        print("FAILED")
        state.setdefault("transcribed", {})[file_id] = {
            "date": date_str, "filename": filename, "status": "transcribed",
            "work_dir": str(work_dir),
        }
        save_state(state)
        return None

    # Normalize: only keep the project label if it matches a real config key,
    # otherwise mark it "default" so unmatched recordings aren't mislabeled.
    project_key = classification.get("project", "default")
    if project_key not in projects:
        project_key = "default"
    classification["project"] = project_key
    rec_name = classification.get("name", filename)
    rec_type = classification.get("type", "other")
    print(f"{rec_type} → {project_key}")

    # Save classification for reference
    (work_dir / "classification.json").write_text(
        json.dumps(classification, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    # 4. Write outputs to target directory
    print(f"  [4/4] Writing outputs...", end=" ", flush=True)
    slug = slugify(rec_name)
    time_str = dt.strftime("%H-%M")
    dir_name = f"{date_str}-{time_str}-{slug}"

    if project_key != "default":
        target_base = Path(projects[project_key]).expanduser()
    else:
        target_base = Path(config.get("default_output", "~/git/plaud-sync/docs/transcripts")).expanduser()

    output_dir = target_base / dir_name
    write_outputs(classification, recording_meta, raw_text, output_dir)
    print(f"→ {output_dir}")

    # Update state
    state.setdefault("transcribed", {})[file_id] = {
        "date": date_str,
        "name": rec_name,
        "project": project_key,
        "type": rec_type,
        "output_dir": str(output_dir),
        "status": "done",
        "transcribed_at": datetime.now(timezone.utc).isoformat(),
    }
    save_state(state)

    return {"file_id": file_id, "name": rec_name, "project": project_key, "output_dir": str(output_dir)}


def process_merged_recordings(recordings, token, config, folders, projects, state, language):
    """Process multiple recordings as a single merged session."""
    recordings = sorted(recordings, key=lambda r: r.get("start_time", 0))

    first = recordings[0]
    first_ms = first.get("start_time", 0)
    dt_first = datetime.fromtimestamp(first_ms / 1000, tz=timezone.utc)
    date_str = dt_first.strftime("%Y-%m-%d")
    total_duration = sum(r.get("duration", 0) for r in recordings)
    file_ids = [r["id"] for r in recordings]

    print(f"\n{'='*60}")
    print(f"  MERGED SESSION ({len(recordings)} parts)")
    for r in recordings:
        dt = datetime.fromtimestamp(r.get("start_time", 0) / 1000, tz=timezone.utc)
        print(f"    {dt.strftime('%H:%M')} · {format_duration(r.get('duration', 0))} · {r.get('filename', 'untitled')}")
    print(f"  Total: {format_duration(total_duration)}")
    print(f"{'='*60}")

    merged_work_dir = WORKING_DIR / f"merged_{'_'.join(fid[:8] for fid in file_ids)}"
    merged_work_dir.mkdir(parents=True, exist_ok=True)

    transcript_parts = []
    step_total = len(recordings) * 2 + 2  # download + transcribe per recording, then classify + write
    step = 0

    for i, rec in enumerate(recordings):
        file_id = rec["id"]
        part_label = f"part {i+1}/{len(recordings)}"

        # Download audio
        step += 1
        cached = list(AUDIO_DIR.glob(f"{file_id}.*"))
        if cached:
            audio_path = cached[0]
            size_mb = audio_path.stat().st_size / 1024 / 1024
            print(f"  [{step}/{step_total}] Audio cached {part_label} ({size_mb:.1f} MB)")
        else:
            print(f"  [{step}/{step_total}] Downloading audio {part_label}...", end=" ", flush=True)
            audio_path = download_audio(file_id, token, config)
            size_mb = audio_path.stat().st_size / 1024 / 1024
            print(f"{size_mb:.1f} MB")

        # Transcribe
        step += 1
        work_dir = WORKING_DIR / file_id
        work_dir.mkdir(parents=True, exist_ok=True)
        transcript_path = work_dir / "transcript.txt"
        if transcript_path.exists():
            print(f"  [{step}/{step_total}] Transcript cached {part_label}")
            raw_text = transcript_path.read_text(encoding="utf-8")
        else:
            print(f"  [{step}/{step_total}] Transcribing {part_label}...", end=" ", flush=True)
            transcription = transcribe_audio(audio_path, language)
            bookmarks = fetch_bookmarks(rec, token, config)
            raw_text = format_diarized_transcript(transcription, bookmarks)
            word_count = len([w for w in transcription.get("words", []) if w["type"] == "word"])
            speakers = len(set(w.get("speaker_id") for w in transcription.get("words", []) if w["type"] == "word"))
            mark_note = f", {len(bookmarks)} bookmark(s)" if bookmarks else ""
            print(f"{word_count} words, {speakers} speakers{mark_note}")
            (work_dir / "elevenlabs_raw.json").write_text(
                json.dumps(transcription, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            transcript_path.write_text(raw_text, encoding="utf-8")

        dt = datetime.fromtimestamp(rec.get("start_time", 0) / 1000, tz=timezone.utc)
        header = f"--- Part {i+1} ({dt.strftime('%H:%M')}, {format_duration(rec.get('duration', 0))}) ---"
        transcript_parts.append(f"{header}\n\n{raw_text}")

    combined_transcript = "\n\n".join(transcript_parts)
    (merged_work_dir / "merged_transcript.txt").write_text(combined_transcript, encoding="utf-8")

    folder_name = get_folder_name(first, folders)
    recording_meta = {
        "file_id": file_ids[0],
        "merged_ids": file_ids,
        "filename": first.get("filename", "untitled"),
        "date": date_str,
        "duration": total_duration,
        "folder": folder_name,
        "start_time": first_ms,
        "merged": True,
        "part_count": len(recordings),
    }

    # Classify & summarize
    step += 1
    print(f"  [{step}/{step_total}] Classifying & summarizing merged session via Claude...", end=" ", flush=True)
    classification = claude_summarize(combined_transcript, recording_meta, projects)
    if not classification:
        print("FAILED")
        for fid in file_ids:
            state.setdefault("transcribed", {})[fid] = {
                "date": date_str, "status": "transcribed",
                "work_dir": str(WORKING_DIR / fid),
            }
        save_state(state)
        return None

    # Normalize: only keep the project label if it matches a real config key,
    # otherwise mark it "default" so unmatched recordings aren't mislabeled.
    project_key = classification.get("project", "default")
    if project_key not in projects:
        project_key = "default"
    classification["project"] = project_key
    rec_name = classification.get("name", first.get("filename", "untitled"))
    rec_type = classification.get("type", "other")
    print(f"{rec_type} → {project_key}")

    (merged_work_dir / "classification.json").write_text(
        json.dumps(classification, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    # Write outputs
    step += 1
    print(f"  [{step}/{step_total}] Writing outputs...", end=" ", flush=True)
    slug = slugify(rec_name)
    time_str = dt_first.strftime("%H-%M")
    dir_name = f"{date_str}-{time_str}-{slug}"

    if project_key != "default":
        target_base = Path(projects[project_key]).expanduser()
    else:
        target_base = Path(config.get("default_output", "~/git/plaud-sync/docs/transcripts")).expanduser()

    output_dir = target_base / dir_name
    write_outputs(classification, recording_meta, combined_transcript, output_dir)
    print(f"→ {output_dir}")

    # Mark all parts as done
    now = datetime.now(timezone.utc).isoformat()
    for fid in file_ids:
        state.setdefault("transcribed", {})[fid] = {
            "date": date_str,
            "name": rec_name,
            "project": project_key,
            "type": rec_type,
            "output_dir": str(output_dir),
            "status": "done",
            "merged_with": [f for f in file_ids if f != fid],
            "transcribed_at": now,
        }
    save_state(state)

    return {"file_id": file_ids[0], "name": rec_name, "project": project_key, "output_dir": str(output_dir)}


# --- Commands ---

def auto_skip_short(recordings, state):
    skipped = 0
    for r in recordings:
        fid = r["id"]
        if fid in state.get("transcribed", {}):
            continue
        if r.get("duration", 0) < AUTO_SKIP_DURATION_MS:
            state.setdefault("transcribed", {})[fid] = {
                "status": "skipped",
                "reason": f"Auto-skipped: {r.get('duration', 0) / 1000:.0f}s < 10s",
            }
            skipped += 1
    if skipped:
        save_state(state)
        print(f"  Auto-skipped {skipped} recording(s) under 10 seconds.")


def skip_recordings(indices, pending, state):
    for i in indices:
        r = pending[i]
        fid = r["id"]
        fname = r.get("filename", "untitled")
        dur = format_duration(r.get("duration", 0))
        state.setdefault("transcribed", {})[fid] = {
            "status": "skipped",
            "reason": "Manually skipped by user",
        }
        print(f"  Skipped: {fname} ({dur})")
    save_state(state)


def parse_indices(text, max_len):
    indices = []
    for part in text.split(","):
        part = part.strip()
        if part.isdigit():
            idx = int(part) - 1
            if 0 <= idx < max_len:
                indices.append(idx)
    return indices


def show_pending(recordings, folders, state):
    """Show pending recordings list. Returns (pending, incomplete) lists."""
    transcribed = state.get("transcribed", {})
    incomplete = [r for r in recordings
                  if r["id"] in transcribed and transcribed[r["id"]].get("status") == "transcribed"]
    new = [r for r in recordings if r["id"] not in transcribed]
    pending = new + incomplete

    if not pending:
        print("No new recordings to process.")
        return [], []

    if incomplete:
        print(f"\n{len(new)} new, {len(incomplete)} incomplete recording(s):\n")
    else:
        print(f"\n{len(pending)} unprocessed recording(s):\n")
    for i, r in enumerate(pending):
        dt = datetime.fromtimestamp(r.get("start_time", 0) / 1000, tz=timezone.utc)
        date_str = dt.strftime("%Y-%m-%d %H:%M")
        dur = format_duration(r.get("duration", 0))
        fname = r.get("filename", "untitled")
        folder = get_folder_name(r, folders)
        folder_str = f" ({folder})" if folder else ""
        tag = " [incomplete]" if r in incomplete else ""
        print(f"  [{i+1}] {date_str}  {dur:>6}  {fname}{folder_str}{tag}")

    return pending, incomplete


def prompt_selection(pending):
    """Prompt user for selection. Returns (selected, merge, quit)."""
    print()
    print("  Commands: 1,3 = process · s1,3 = skip · m1,2 = merge & process · all · q = quit")
    sel = input("> ").strip()
    if sel.lower() in ("q", "quit", ""):
        return [], False, True
    if sel.lower() == "all":
        return pending, False, False

    if sel.lower().startswith("s"):
        return parse_indices(sel[1:], len(pending)), False, "skip"

    if sel.lower().startswith("m"):
        indices = parse_indices(sel[1:], len(pending))
        if len(indices) < 2:
            print("  Merge requires at least 2 recordings.")
            return [], False, False
        selected = [pending[i] for i in indices]
        total_dur = sum(r.get("duration", 0) for r in selected)
        print(f"  Merging {len(selected)} recordings ({format_duration(total_dur)} total)")
        return selected, True, False

    indices = parse_indices(sel, len(pending))
    return [pending[i] for i in indices], False, False


def cmd_list(args, token, config, state, folders):
    recordings = list_recordings(token, config, args.days)
    transcribed = state.get("transcribed", {})

    auto_skip_short(recordings, state)
    transcribed = state.get("transcribed", {})

    pending = [r for r in recordings if r["id"] not in transcribed]
    done = [r for r in recordings if r["id"] in transcribed]
    print(f"\n{len(pending)} pending, {len(done)} done/skipped\n")

    for r in recordings:
        dt = datetime.fromtimestamp(r.get("start_time", 0) / 1000, tz=timezone.utc)
        date_str = dt.strftime("%Y-%m-%d %H:%M")
        dur = format_duration(r.get("duration", 0))
        fname = r.get("filename", "untitled")
        folder = get_folder_name(r, folders)
        folder_str = f" ({folder})" if folder else ""

        entry = transcribed.get(r["id"], {})
        if entry.get("status") == "skipped":
            status = "–"
        elif entry:
            status = "✓"
        else:
            status = " "

        print(f"  {status} {date_str}  {dur:>6}  {fname}{folder_str}")


def git_commit_working(results):
    if not results:
        return
    try:
        subprocess.run(["git", "add", "docs/"], cwd=PROJECT_DIR, capture_output=True, timeout=10)
        status = subprocess.run(
            ["git", "status", "--porcelain", "docs/"],
            cwd=PROJECT_DIR, capture_output=True, text=True, timeout=10,
        )
        if status.stdout.strip():
            subprocess.run(
                ["git", "commit", "-m", f"Transcribe {len(results)} recording(s)"],
                cwd=PROJECT_DIR, capture_output=True, timeout=30,
            )
    except Exception:
        pass


def execute_selection(selected, merge, token, config, folders, projects, state, language):
    results = []
    if merge:
        print(f"\nMerging & processing {len(selected)} recording(s)...")
        result = process_merged_recordings(selected, token, config, folders, projects, state, language)
        if result:
            results.append(result)
    else:
        print(f"\nProcessing {len(selected)} recording(s)...")
        for rec in selected:
            result = process_recording(rec, token, config, folders, projects, state, language)
            if result:
                results.append(result)

    git_commit_working(results)

    if results:
        print(f"\n{'='*60}")
        print(f"  Done! {len(results)} recording(s) processed:")
        for r in results:
            print(f"    {r['name']}")
            print(f"      → {r['output_dir']}")
        print(f"{'='*60}")

    return results


def resolve_project(name, projects):
    """Match a user-supplied project name against config keys (case/diacritics-insensitive).
    Returns (project_key, target_base) or (None, None) on no match. 'default' is valid."""
    if name.lower() == "default":
        return "default", None
    want = slugify(name)
    for key, path in projects.items():
        if slugify(key) == want:
            return key, Path(path).expanduser()
    return None, None


def find_transcribed_entry(ref, state):
    """Find a transcribed entry by file_id (exact) or by output_dir basename/substring.
    Returns (file_id, entry) or (None, None)."""
    transcribed = state.get("transcribed", {})
    if ref in transcribed:
        return ref, transcribed[ref]
    ref_base = Path(ref.rstrip("/")).name
    matches = [
        (fid, e) for fid, e in transcribed.items()
        if ref_base and ref_base == Path(e.get("output_dir", "")).name
    ]
    if not matches:
        matches = [
            (fid, e) for fid, e in transcribed.items()
            if ref and ref in e.get("output_dir", "")
        ]
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        print(f"Ambiguous reference '{ref}' matches {len(matches)} recordings:")
        for fid, e in matches:
            print(f"  {fid}  {e.get('output_dir')}")
    return None, None


def fuzzy_match(query, text):
    """True if every whitespace token of query is a substring of text (diacritics-insensitive)."""
    text_s = slugify(text)
    return all(tok in text_s for tok in slugify(query).split("-") if tok)


def select_transcript(state, query=""):
    """Interactively pick a transcribed recording. Returns (file_id, entry) or (None, None)."""
    entries = sorted(
        [kv for kv in state.get("transcribed", {}).items() if kv[1].get("output_dir")],
        key=lambda kv: kv[1].get("transcribed_at", ""), reverse=True,
    )
    if not entries:
        print("No transcribed recordings in state.")
        return None, None

    while True:
        haystack = lambda e: f"{e.get('name','')} {e.get('project','')} {Path(e.get('output_dir','')).name}"
        matches = [(fid, e) for fid, e in entries if fuzzy_match(query, haystack(e))] if query else entries
        print()
        if query:
            print(f"  Transcripts matching '{query}'  ({len(matches)}):")
        else:
            print(f"  Transcripts ({len(matches)}):")
        for i, (fid, e) in enumerate(matches, 1):
            print(f"  {i:>2}  {e.get('date','?')}  [{e.get('project','?')}]  {e.get('name','?')}")
        print("  Type a number to pick · text to filter · q = cancel")
        sel = input("> ").strip()
        if sel.lower() in ("q", "quit", ""):
            return None, None
        if sel.isdigit():
            idx = int(sel) - 1
            if 0 <= idx < len(matches):
                return matches[idx]
            print("  Out of range.")
            continue
        query = sel


def select_project(config, query=""):
    """Interactively pick a destination project. Returns project name or None."""
    options = list(get_projects(config)) + ["default"]
    while True:
        matches = [p for p in options if fuzzy_match(query, p)] if query else options
        print()
        print(f"  Projects ({len(matches)}):")
        for i, p in enumerate(matches, 1):
            print(f"  {i:>2}  {p}")
        print("  Type a number to pick · text to filter · q = cancel")
        sel = input("> ").strip()
        if sel.lower() in ("q", "quit", ""):
            return None
        if sel.isdigit():
            idx = int(sel) - 1
            if 0 <= idx < len(matches):
                return matches[idx]
            print("  Out of range.")
            continue
        query = sel


def do_move(file_id, entry, project_name, config, state):
    """Resolve project and relocate the recording. Returns True on success."""
    projects = get_projects(config)
    project_key, target_base = resolve_project(project_name, projects)
    if project_key is None:
        print(f"Unknown project '{project_name}'. Known: {', '.join(projects)}, default")
        return False
    if target_base is None:  # default
        target_base = Path(config.get("default_output", "~/git/plaud-sync/docs/transcripts")).expanduser()

    old_dir = Path(entry["output_dir"])
    new_dir = target_base / old_dir.name

    if old_dir.resolve() == new_dir.resolve():
        print(f"Already in '{project_key}': {old_dir}")
        return False

    if new_dir.exists():
        # A copy already lives at the target (e.g. an earlier run wrote it directly).
        # Reconcile: point state at the existing target and drop the stale source
        # so the recording shows as moved rather than duplicated.
        removed = ""
        if old_dir.exists() and old_dir.resolve() != new_dir.resolve():
            shutil.rmtree(old_dir)
            removed = f"  removed stale source: {old_dir}\n"
        entry["project"] = project_key
        entry["output_dir"] = str(new_dir)
        save_state(state)
        print(f"\nReconciled {file_id} (target already existed)")
        print(removed + f"  state → {new_dir}  (project: {project_key})")
        return True

    if not old_dir.exists():
        print(f"Source directory missing: {old_dir}")
        return False

    target_base.mkdir(parents=True, exist_ok=True)
    shutil.move(str(old_dir), str(new_dir))

    entry["project"] = project_key
    entry["output_dir"] = str(new_dir)
    save_state(state)

    print(f"\nMoved {file_id}")
    print(f"  {old_dir}")
    print(f"  → {new_dir}  (project: {project_key})")
    return True


def cmd_move(args, config, state):
    """Move an already-transcribed recording to a different project directory.

    Non-interactive:  move <file_id|dir-name|path> <project>
    Interactive:      move [filter]   → fuzzy-pick a transcript, then a project
    """
    # Two args + the first resolves directly → keep fast non-interactive path.
    if len(args.extra) >= 2:
        ref, project_name = args.extra[0], args.extra[1]
        file_id, entry = find_transcribed_entry(ref, state)
        if not entry:
            print(f"No transcribed recording found for '{ref}'.")
            return
        do_move(file_id, entry, project_name, config, state)
        return

    # Interactive flow. Any single arg seeds the transcript filter; loops for more moves.
    seed = args.extra[0] if args.extra else ""
    while True:
        file_id, entry = select_transcript(state, seed)
        seed = ""  # only seed the first round
        if not entry:
            print("Done.")
            return
        project_name = select_project(config)
        if not project_name:
            print("  Skipped — back to transcripts.")
            continue
        do_move(file_id, entry, project_name, config, state)


def cmd_process(args, token, config, state, folders):
    projects = get_projects(config)
    recordings = list_recordings(token, config, args.days)

    auto_skip_short(recordings, state)

    if args.all:
        transcribed = state.get("transcribed", {})
        selected = [r for r in recordings
                    if r["id"] not in transcribed
                    or transcribed[r["id"]].get("status") == "transcribed"]
        if selected:
            execute_selection(selected, False, token, config, folders, projects, state, args.language)
        return

    while True:
        pending, _ = show_pending(recordings, folders, state)
        if not pending:
            break

        selected, merge, quit_signal = prompt_selection(pending)

        if quit_signal is True:
            break

        if quit_signal == "skip":
            skip_recordings(selected, pending, state)
            continue

        if not selected:
            continue

        execute_selection(selected, merge, token, config, folders, projects, state, args.language)


def main():
    parser = argparse.ArgumentParser(
        prog="plaud-transcribe",
        description="Download, transcribe, classify and summarize Plaud recordings",
    )
    parser.add_argument("command", nargs="?", default="process", choices=["list", "process", "move"])
    parser.add_argument("extra", nargs="*", help="For 'move': <file_id|dir-name|path> <project>")
    parser.add_argument("--days", type=int, default=30)
    parser.add_argument("--all", action="store_true", help="Process all unprocessed recordings")
    parser.add_argument("--language", default="cs", help="Primary language (default: cs)")
    args = parser.parse_args()

    config = load_config()
    state = load_state()

    if args.command == "move":
        cmd_move(args, config, state)
        return

    token = get_token()

    print(f"Fetching recordings from last {args.days} days...")
    folders = fetch_folders(token, config)

    if args.command == "list":
        cmd_list(args, token, config, state, folders)
    else:
        cmd_process(args, token, config, state, folders)


if __name__ == "__main__":
    main()
