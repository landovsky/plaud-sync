#!/usr/bin/env python3
"""plaud-transcribe: Download, transcribe, classify and summarize Plaud recordings.

Full pipeline:
  1. Fetch recording list from Plaud.ai cloud
  2. Interactive selection of unprocessed recordings
  3. Download audio
  4. Transcribe via ElevenLabs Scribe v2
  5. Classify & summarize via the Claude Code CLI (`claude`)
  6. Write outputs to the target project directory
  7. (optional) Commit outputs into their target repo — set "auto_commit": true

Summarization prompts live in prompts/summarize.<lang>.md (cs, en) — edit them
there rather than in this file.

Summarization currently requires the Claude Code CLI (`claude`) on your PATH.
A pluggable LLM gateway (Anthropic API / OpenAI-compatible) is on the roadmap —
pull requests welcome.

Env vars:
  PLAUD_BEARER_TOKEN      — Plaud.ai API token (or set "token" in ~/.plaud-sync/config.json)
  ELEVENLABS_API_KEY      — ElevenLabs API key for transcription
  CLAUDE_CODE_OAUTH_TOKEN — Claude Code auth (or just have `claude` logged in)
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
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import wispr_flow

API_BASE_DEFAULT = "https://api.plaud.ai"
ELEVENLABS_API = "https://api.elevenlabs.io/v1/speech-to-text"
CONFIG_DIR = Path.home() / ".plaud-sync"
CONFIG_FILE = CONFIG_DIR / "config.json"
STATE_FILE = CONFIG_DIR / "state.json"
PROJECT_DIR = Path(__file__).resolve().parent
AUDIO_DIR = PROJECT_DIR / "docs" / "audio"
WORKING_DIR = PROJECT_DIR / "docs" / "working"
PROMPTS_DIR = PROJECT_DIR / "prompts"
DEFAULT_OUTPUT_DIR = PROJECT_DIR / "docs" / "transcripts"

# Plaud encrypts some responses with a *global* app key (not per-user). If Plaud
# rotates it, override via "fernet_key" in config.json — see fernet_decrypt().
DEFAULT_FERNET_KEY = b"8rRmMCskL3NaUiGr-UsmEEeI8NKH-ZfeGbpRGqSLpuI="

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
    # Roll a one-generation backup before overwriting. state.json is the only
    # record of what's been processed and lives in a single place, so an
    # accidental clobber (a bad run, a stray test) would otherwise re-expose
    # every processed recording as "pending". The .bak makes that recoverable.
    if STATE_FILE.exists():
        try:
            shutil.copy2(STATE_FILE, STATE_FILE.parent / (STATE_FILE.name + ".bak"))
        except OSError:
            pass
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2, ensure_ascii=False)


def get_projects(config):
    projects = config.get("projects")
    if projects:
        return projects
    fm = config.get("folder_mapping", {})
    return {k: v for k, v in fm.items() if v is not None}


def default_output_dir(config):
    """Where recordings land when no project matches. Defaults to this tool's
    own docs/transcripts so a cloner isn't pointed at a stranger's path."""
    val = config.get("default_output")
    return Path(val).expanduser() if val else DEFAULT_OUTPUT_DIR


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
    for r in recordings:
        r.setdefault("source", "plaud")
    return [r for r in recordings if r.get("start_time", 0) >= cutoff_ms]


def list_all_recordings(token, config, days):
    """Plaud recordings plus (if enabled) local Wispr Flow meetings, merged into
    one newest-first list. Both sources share the same recording shape, so the
    rest of the pipeline treats them uniformly. Wispr reads are best-effort and
    never block the Plaud path."""
    recordings = list_recordings(token, config, days)
    recordings += wispr_flow.list_meetings(config, days)
    recordings.sort(key=lambda r: r.get("start_time", 0), reverse=True)
    return recordings


def source_label(recording):
    """Provider name, shown as its own column in list output."""
    return "Wispr" if recording.get("source") == "wispr" else "Plaud"


def fetch_folders(token, config):
    data = plaud_get("/filetag/", token, config)
    return {f["id"]: f["name"] for f in data.get("data_filetag_list", [])}


def fernet_decrypt(data, config=None):
    from cryptography.fernet import Fernet, InvalidToken
    key = (config or {}).get("fernet_key") or DEFAULT_FERNET_KEY
    if isinstance(key, str):
        key = key.encode()
    try:
        return Fernet(key).decrypt(data)
    except InvalidToken:
        print(
            "Error: Fernet decryption failed — Plaud.ai likely rotated their encryption key.\n"
            "Fixes, in order of effort:\n"
            "  1. Use plaud_sync.py instead — it downloads Plaud's own transcripts, needs no\n"
            "     audio decryption, and is therefore immune to key rotation.\n"
            '  2. Set a fresh key in ~/.plaud-sync/config.json as "fernet_key": "<key>".\n'
            "  3. Recover the key: open web.plaud.ai in Chrome DevTools, capture a HAR, search\n"
            "     the JS bundles for the fernet chunk, and read it from the bf() function.",
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
    return json.loads(fernet_decrypt(encrypted, config))


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

def _encode_multipart(fields, file_field, filename, filedata):
    """Build a multipart/form-data body so we can POST via urllib instead of
    shelling out to curl (one less dependency; keeps the API key out of argv)."""
    boundary = "----plaudsync" + os.urandom(16).hex()
    parts = []
    for name, value in fields.items():
        parts.append(
            f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'.encode()
        )
    parts.append(
        f'--{boundary}\r\nContent-Disposition: form-data; name="{file_field}"; '
        f'filename="{filename}"\r\nContent-Type: application/octet-stream\r\n\r\n'.encode()
    )
    body = b"".join(parts) + filedata + f"\r\n--{boundary}--\r\n".encode()
    return body, f"multipart/form-data; boundary={boundary}"


def transcribe_audio(audio_path, language="cs", config=None):
    api_key = os.environ.get("ELEVENLABS_API_KEY")
    if not api_key:
        print("Error: ELEVENLABS_API_KEY not set.", file=sys.stderr)
        sys.exit(1)

    model = (config or {}).get("elevenlabs_model", "scribe_v2")
    audio_path = Path(audio_path)
    fields = {"model_id": model, "diarize": "true", "language_code": language}
    body, content_type = _encode_multipart(fields, "file", audio_path.name, audio_path.read_bytes())
    req = urllib.request.Request(
        ELEVENLABS_API, data=body,
        headers={"xi-api-key": api_key, "Content-Type": content_type},
    )
    try:
        with urllib.request.urlopen(req, timeout=600) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")[:300]
        print(f"Error: ElevenLabs failed ({e.code}): {detail}", file=sys.stderr)
        sys.exit(1)


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

    Opt-in via config: only runs when "process_bookmarks": true is set in
    ~/.plaud-sync/config.json. Returns [] on any failure or when the
    recording has no marks (graceful: a missing bookmark must never block
    transcription).
    """
    if not config.get("process_bookmarks"):
        return []
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

def load_prompt_template(language):
    """Load the summarization prompt for a language from prompts/summarize.<lang>.md.
    Falls back to English if the requested language has no template."""
    path = PROMPTS_DIR / f"summarize.{language}.md"
    if not path.exists():
        path = PROMPTS_DIR / "summarize.en.md"
    return path.read_text(encoding="utf-8")


SUMMARY_SENTINEL = "===SUMMARY==="
TRANSCRIPT_SENTINEL = "===TRANSCRIPT==="


def _strip_fences(s):
    s = s.strip()
    if s.startswith("```"):
        s = re.sub(r"^```\w*\n?", "", s)
        s = re.sub(r"\n?```$", "", s)
    return s.strip()


def _parse_claude_json(text):
    """Parse a JSON object tolerantly.

    Two failure modes to survive: (1) literal newlines/tabs inside string values,
    which strict JSON rejects — so we retry with strict=False; (2) stray prose
    around the object — so we also try a slice from the first '{' to the last
    '}'."""
    candidates = [text]
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end > start:
        candidates.append(text[start:end + 1])
    for candidate in candidates:
        for strict in (True, False):
            try:
                return json.loads(candidate, strict=strict)
            except (json.JSONDecodeError, TypeError):
                continue
    return None


def _parse_summary_response(text):
    """Parse the model's reply into a classification dict.

    Preferred format is a small JSON metadata object followed by sentinel-
    delimited free-text sections:

        {..name/type/tags/project/language..}
        ===SUMMARY===
        <markdown summary>
        ===TRANSCRIPT===
        <cleaned transcript>

    Keeping the free text OUT of the JSON is what makes this robust: summaries
    routinely contain double quotes (e.g. a repo name) and newlines, which would
    otherwise break a single all-in-one JSON object. Falls back to the legacy
    all-in-one JSON object when no sentinel is present."""
    if SUMMARY_SENTINEL in text:
        head, _, rest = text.partition(SUMMARY_SENTINEL)
        summary_part, sep, transcript_part = rest.partition(TRANSCRIPT_SENTINEL)
        meta = _parse_claude_json(_strip_fences(head))
        if meta is None:
            return None
        meta["summary"] = summary_part.strip()
        if sep:  # the transcript section is optional (omitted for clean sources)
            meta["clean_transcript"] = transcript_part.strip()
        return meta
    return _parse_claude_json(_strip_fences(text))


def project_hints(config):
    """Optional per-project keywords from config, e.g.

        "project_hints": {"Hriste Hrou": "playground BOM, manufacturing, pricing"}

    Purely additive — projects without hints still work off their name, path and
    recent-recording history."""
    hints = config.get("project_hints") or {}
    return {k: v for k, v in hints.items() if isinstance(v, str) and v.strip()}


def recent_examples(state, limit=4):
    """The names of recently classified recordings, grouped by project.

    Classification kept falling back to "default" for meetings that never say
    the project name out loud — a BOM design sync is only recognisable as
    "Hriste Hrou" if you know the three previous BOM syncs went there. That
    history is already in state.json; this hands it to the model as evidence
    instead of expecting it to guess from a bare project key."""
    by_project = {}
    entries = [e for e in state.get("transcribed", {}).values()
               if e.get("status") == "done" and e.get("name")
               and e.get("project") and e.get("project") != "default"]
    # Newest first, so a project that has drifted in topic is represented by
    # what it is now rather than what it was a year ago.
    entries.sort(key=lambda e: e.get("transcribed_at") or e.get("date") or "", reverse=True)
    for e in entries:
        names = by_project.setdefault(e["project"], [])
        if len(names) < limit and e["name"] not in names:
            names.append(e["name"])
    return by_project


def format_project_list(projects, config, state):
    """One block per project: key, output path, optional hints, recent recordings."""
    if not projects:
        return "(none configured)"
    hints = project_hints(config)
    examples = recent_examples(state or {})
    lines = []
    for name, path in projects.items():
        lines.append(f"- {name}: {path}")
        if hints.get(name):
            lines.append(f"    topics: {hints[name]}")
        if examples.get(name):
            lines.append(f"    recent recordings: {'; '.join(examples[name])}")
    return "\n".join(lines)


def claude_summarize(transcript_text, recording_meta, projects, language="en", want_clean=True,
                     extra_instructions=None, config=None, state=None):
    project_list = format_project_list(projects, config or {}, state or {})
    duration_min = recording_meta.get("duration", 0) // 60000

    prompt = (
        load_prompt_template(language)
        .replace("{{FILENAME}}", str(recording_meta.get("filename", "unknown")))
        .replace("{{DURATION_MIN}}", str(duration_min))
        .replace("{{DATE}}", str(recording_meta.get("date", "unknown")))
        .replace("{{FOLDER}}", str(recording_meta.get("folder", "none")))
        .replace("{{PROJECT_LIST}}", project_list)
        .replace("{{TRANSCRIPT}}", transcript_text)
    )

    # When the transcript is already clean (e.g. Wispr Flow's refined output),
    # don't make the model echo the whole thing back inside the JSON — that bloats
    # the response and, on long meetings, truncates it into invalid JSON. We keep
    # the original transcript and only ask for classification + summary.
    if not want_clean:
        prompt += (
            '\n\n## Override\n'
            'The transcript above is already cleaned and diarized — do NOT echo or '
            're-clean it. Omit the ===TRANSCRIPT=== section entirely. Still return '
            'the JSON metadata and the ===SUMMARY=== section as specified.'
        )

    # Per-run steering typed by the user at selection time. Appended last so it
    # is the most recent instruction the model sees, but explicitly framed as an
    # addition — it must not replace the output contract above, or the response
    # stops parsing.
    if extra_instructions:
        prompt += (
            '\n\n## Additional instructions from the user\n'
            'These refine the summary — emphasis, level of detail, what to pull out. '
            'They do NOT replace anything above: keep the same output format, '
            'sentinels, JSON metadata and section structure.\n\n'
            f'{extra_instructions.strip()}\n'
        )

    timeout = max(300, duration_min * 30)
    try:
        result = subprocess.run(
            ["claude", "-p", "--output-format", "text"],
            input=prompt, capture_output=True, text=True, timeout=timeout,
        )
    except FileNotFoundError:
        print(
            "Error: the 'claude' CLI (Claude Code) was not found on your PATH.\n"
            "plaud-transcribe currently requires Claude Code for summarization:\n"
            "  https://claude.com/claude-code\n"
            "(A pluggable LLM gateway is on the roadmap — PRs welcome.)",
            file=sys.stderr,
        )
        sys.exit(1)
    except subprocess.TimeoutExpired:
        print(f"TIMEOUT ({timeout}s)", file=sys.stderr)
        return None

    if result.returncode != 0:
        print(f"Error: claude CLI failed: {result.stderr[:300]}", file=sys.stderr)
        return None

    text = result.stdout.strip()
    WORKING_DIR.mkdir(parents=True, exist_ok=True)
    (WORKING_DIR / "last_claude_response.txt").write_text(text, encoding="utf-8")
    parsed = _parse_summary_response(text)
    if parsed is None:
        print("Error: Could not parse Claude response", file=sys.stderr)
        # Show the tail too: truncated output (a failure mode on long
        # transcripts) is only visible at the end, not the head.
        print(f"Response ({len(text)} chars) head: {text[:300]}", file=sys.stderr)
        print(f"…tail: {text[-300:]}", file=sys.stderr)
    return parsed


# --- Helpers ---

def local_tz(config):
    """The zone recordings are displayed and named in.

    Both sources stamp a recording with an absolute epoch, so rendering it in UTC
    silently shifts every label: a meeting that started 13:13 in Prague was listed
    as 11:13 and landed on disk as `...-11-13-...`. Anything before 02:00 local
    also lands on the previous day. Config key "timezone"; system zone otherwise.
    """
    name = (config or {}).get("timezone")
    if name:
        try:
            return ZoneInfo(name)
        except (ZoneInfoNotFoundError, ValueError):
            print(f"  Warning: unknown timezone {name!r} in config, using system zone",
                  file=sys.stderr)
    return datetime.now().astimezone().tzinfo


def local_dt(ms, config):
    """Epoch milliseconds -> aware datetime in the configured local zone."""
    return datetime.fromtimestamp((ms or 0) / 1000, tz=local_tz(config))

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
            # Free-text values (custom instructions above all) can contain the
            # very characters that make YAML ambiguous, so escape rather than
            # just wrap: an unescaped quote would silently break the block for
            # every downstream reader.
            escaped = v.replace("\\", "\\\\").replace('"', '\\"')
            escaped = escaped.replace("\n", " ").replace("\r", " ")
            lines.append(f'{k}: "{escaped}"')
        else:
            lines.append(f"{k}: {v}")
    lines.append("---")
    return "\n".join(lines)


# --- File Output ---

def strip_leading_h1(summary):
    """Drop a title the model wrote itself.

    The summary is always written under `# {name}`, so a model-authored H1 lands
    directly beneath it as a second title. That mostly happens when custom
    instructions ask for a piece of prose rather than notes — the model titles
    its own essay."""
    stripped = summary.lstrip()
    if not stripped.startswith("# "):
        return summary
    _, _, rest = stripped.partition("\n")
    return rest.lstrip("\n")


def write_outputs(classification, recording_meta, raw_transcript_text, output_dir,
                  extra_instructions=None):
    output_dir.mkdir(parents=True, exist_ok=True)

    date = recording_meta.get("date", "unknown")
    name = classification.get("name", recording_meta.get("filename", "untitled"))
    source = recording_meta.get("source", "plaud")

    front_matter = {
        "title": name,
        "date": date,
        "type": classification.get("type", "other"),
        "tags": classification.get("tags", []),
        "project": classification.get("project", "default"),
        "language": classification.get("language", "cs"),
        "duration": format_duration(recording_meta.get("duration", 0)),
        "source": source,
    }
    # Keep the source's native id under a source-specific key.
    front_matter["wispr_id" if source == "wispr" else "plaud_id"] = recording_meta.get("file_id")
    # Surface the one-off steering in the document itself: reading a summary
    # months later, "why is this so BOM-heavy?" should be answerable in place.
    if extra_instructions:
        front_matter["instructions"] = extra_instructions

    fm = yaml_front_matter(front_matter)

    summary_path = output_dir / "summary.md"
    summary_path.write_text(
        f"{fm}\n\n# {name}\n\n{strip_leading_h1(classification.get('summary', ''))}\n",
        encoding="utf-8",
    )

    transcript_path = output_dir / "transcript.md"
    # Fall back to the raw transcript when the model returns no cleaned version
    # (missing or deliberately empty, e.g. Wispr's already-clean transcript).
    clean = classification.get("clean_transcript") or raw_transcript_text
    transcript_path.write_text(f"{fm}\n\n# Transcript: {name}\n\n{clean}\n", encoding="utf-8")

    meta_path = output_dir / "metadata.json"
    meta_path.write_text(
        json.dumps({**front_matter, "raw_recording": recording_meta}, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )

    return summary_path, transcript_path


def write_wispr_summary(wispr_info, recording_meta, output_dir, config):
    """Preserve Wispr Flow's own summary next to ours as `wispr-summary.md`, so
    you can compare the two — or switch to Wispr's later — without re-running
    anything. Opt out with "keep_wispr_summary": false in the wispr config."""
    if not wispr_flow.keep_wispr_summary(config):
        return
    summary = (wispr_info or {}).get("summary")
    if not summary or not summary.strip():
        return
    title = recording_meta.get("filename", "Meeting")
    header = (
        f"# {title} — Wispr Flow summary\n\n"
        "> Generated by Wispr Flow, not by this pipeline. Kept for comparison.\n\n"
    )
    (output_dir / "wispr-summary.md").write_text(
        header + summary.strip() + "\n", encoding="utf-8"
    )


# --- Main Pipeline ---

def wispr_raw_text_cached(recording, work_dir):
    """Read (and cache) a Wispr meeting's diarized transcript. Returns the text,
    or None if it can't be read. Wispr already produced the transcript locally,
    so this replaces Plaud's download + ElevenLabs steps entirely."""
    transcript_path = work_dir / "transcript.txt"
    if transcript_path.exists():
        return transcript_path.read_text(encoding="utf-8")
    try:
        raw_text = wispr_flow.transcript_text(recording)
    except OSError as e:
        print(f"\n  Error reading Wispr transcript: {e}", file=sys.stderr)
        return None
    if not raw_text:
        return None
    transcript_path.write_text(raw_text, encoding="utf-8")
    return raw_text


def process_recording(recording, token, config, folders, projects, state, language,
                      extra_instructions=None):
    file_id = recording["id"]
    source = recording.get("source", "plaud")
    filename = recording.get("filename", "untitled")
    start_ms = recording.get("start_time", 0)
    dt = local_dt(start_ms, config)
    date_str = dt.strftime("%Y-%m-%d")
    duration = recording.get("duration", 0)
    folder_name = None if source == "wispr" else get_folder_name(recording, folders)
    wispr_info = recording.get("_wispr", {}) if source == "wispr" else {}

    recording_meta = {
        "file_id": wispr_info.get("uuid", file_id),
        "source": source,
        "filename": filename,
        "date": date_str,
        "duration": duration,
        "folder": folder_name,
        "start_time": start_ms,
    }

    origin = "Wispr Flow" if source == "wispr" else (folder_name or "—")
    print(f"\n{'='*60}")
    print(f"  {filename}")
    print(f"  {date_str} · {format_duration(duration)} · {origin}")
    print(f"{'='*60}")

    work_dir = WORKING_DIR / file_id.replace(":", "_")
    work_dir.mkdir(parents=True, exist_ok=True)

    # Steps 1-2: obtain the diarized transcript. Plaud downloads audio and
    # transcribes it via ElevenLabs; Wispr already produced a local transcript,
    # so we just read it (no audio download, no re-transcription cost).
    nsteps = 3 if source == "wispr" else 4
    if source == "wispr":
        print(f"  [1/{nsteps}] Reading Wispr Flow transcript...", end=" ", flush=True)
        raw_text = wispr_raw_text_cached(recording, work_dir)
        if not raw_text:
            print("FAILED (no transcript)")
            return None
        turns = raw_text.count("\n\n") + 1
        print(f"{turns} turns")
    else:
        # 1. Download audio (skip if already downloaded)
        cached = list(AUDIO_DIR.glob(f"{file_id}.*"))
        if cached:
            audio_path = cached[0]
            size_mb = audio_path.stat().st_size / 1024 / 1024
            print(f"  [1/{nsteps}] Audio cached ({size_mb:.1f} MB)")
        else:
            print(f"  [1/{nsteps}] Downloading audio...", end=" ", flush=True)
            audio_path = download_audio(file_id, token, config)
            size_mb = audio_path.stat().st_size / 1024 / 1024
            print(f"{size_mb:.1f} MB")

        # 2. Transcribe (skip if already done)
        transcript_path = work_dir / "transcript.txt"
        if transcript_path.exists():
            print(f"  [2/{nsteps}] Transcript cached")
            raw_text = transcript_path.read_text(encoding="utf-8")
        else:
            print(f"  [2/{nsteps}] Transcribing via ElevenLabs...", end=" ", flush=True)
            transcription = transcribe_audio(audio_path, language, config)
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
    print(f"  [{nsteps - 1}/{nsteps}] Classifying & summarizing via Claude...", end=" ", flush=True)
    classification = claude_summarize(raw_text, recording_meta, projects, language,
                                      want_clean=(source != "wispr"),
                                      extra_instructions=extra_instructions,
                                      config=config, state=state)
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
    print(f"  [{nsteps}/{nsteps}] Writing outputs...", end=" ", flush=True)
    slug = slugify(rec_name)
    time_str = dt.strftime("%H-%M")
    dir_name = f"{date_str}-{time_str}-{slug}"

    if project_key != "default":
        target_base = Path(projects[project_key]).expanduser()
    else:
        target_base = default_output_dir(config)

    output_dir = target_base / dir_name
    write_outputs(classification, recording_meta, raw_text, output_dir,
                  extra_instructions=extra_instructions)
    if source == "wispr":
        write_wispr_summary(wispr_info, recording_meta, output_dir, config)
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
        # Keep the one-off steering with the entry: a re-read of a summary later
        # is confusing without knowing what the user asked it to emphasize.
        **({"instructions": extra_instructions} if extra_instructions else {}),
    }
    save_state(state)

    return {"file_id": file_id, "name": rec_name, "project": project_key, "output_dir": str(output_dir)}


def process_merged_recordings(recordings, token, config, folders, projects, state, language,
                              extra_instructions=None):
    """Process multiple recordings as a single merged session."""
    recordings = sorted(recordings, key=lambda r: r.get("start_time", 0))

    first = recordings[0]
    first_ms = first.get("start_time", 0)
    dt_first = local_dt(first_ms, config)
    date_str = dt_first.strftime("%Y-%m-%d")
    total_duration = sum(r.get("duration", 0) for r in recordings)
    file_ids = [r["id"] for r in recordings]

    print(f"\n{'='*60}")
    print(f"  MERGED SESSION ({len(recordings)} parts)")
    for r in recordings:
        dt = local_dt(r.get("start_time", 0), config)
        print(f"    {dt.strftime('%H:%M')} · {format_duration(r.get('duration', 0))} · {r.get('filename', 'untitled')}")
    print(f"  Total: {format_duration(total_duration)}")
    print(f"{'='*60}")

    merged_work_dir = WORKING_DIR / f"merged_{'_'.join(fid.replace(':', '_')[:12] for fid in file_ids)}"
    merged_work_dir.mkdir(parents=True, exist_ok=True)

    transcript_parts = []
    step_total = len(recordings) * 2 + 2  # download + transcribe per recording, then classify + write
    step = 0

    for i, rec in enumerate(recordings):
        file_id = rec["id"]
        rec_source = rec.get("source", "plaud")
        part_label = f"part {i+1}/{len(recordings)}"
        work_dir = WORKING_DIR / file_id.replace(":", "_")
        work_dir.mkdir(parents=True, exist_ok=True)

        if rec_source == "wispr":
            # Wispr already has the transcript locally — read it in place of the
            # download + transcribe steps (bump step twice to keep the counter
            # aligned with the 2-per-part budget).
            step += 1
            print(f"  [{step}/{step_total}] Reading Wispr transcript {part_label}...", end=" ", flush=True)
            raw_text = wispr_raw_text_cached(rec, work_dir)
            if not raw_text:
                print("FAILED (no transcript)")
                return None
            print(f"{raw_text.count(chr(10) + chr(10)) + 1} turns")
            step += 1
        else:
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
            transcript_path = work_dir / "transcript.txt"
            if transcript_path.exists():
                print(f"  [{step}/{step_total}] Transcript cached {part_label}")
                raw_text = transcript_path.read_text(encoding="utf-8")
            else:
                print(f"  [{step}/{step_total}] Transcribing {part_label}...", end=" ", flush=True)
                transcription = transcribe_audio(audio_path, language, config)
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

        dt = local_dt(rec.get("start_time", 0), config)
        header = f"--- Part {i+1} ({dt.strftime('%H:%M')}, {format_duration(rec.get('duration', 0))}) ---"
        transcript_parts.append(f"{header}\n\n{raw_text}")

    combined_transcript = "\n\n".join(transcript_parts)
    (merged_work_dir / "merged_transcript.txt").write_text(combined_transcript, encoding="utf-8")

    folder_name = get_folder_name(first, folders)
    merged_source = "wispr" if all(r.get("source") == "wispr" for r in recordings) else "plaud"
    recording_meta = {
        "file_id": file_ids[0].split(":", 1)[-1],
        "source": merged_source,
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
    classification = claude_summarize(combined_transcript, recording_meta, projects, language,
                                      want_clean=(merged_source != "wispr"),
                                      extra_instructions=extra_instructions,
                                      config=config, state=state)
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
        target_base = default_output_dir(config)

    output_dir = target_base / dir_name
    write_outputs(classification, recording_meta, combined_transcript, output_dir,
                  extra_instructions=extra_instructions)
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
            **({"instructions": extra_instructions} if extra_instructions else {}),
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


def show_pending(recordings, folders, state, config):
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
        dt = local_dt(r.get("start_time", 0), config)
        date_str = dt.strftime("%Y-%m-%d %H:%M")
        dur = format_duration(r.get("duration", 0))
        fname = r.get("filename", "untitled")
        folder = get_folder_name(r, folders)
        folder_str = f" ({folder})" if folder else ""
        tag = " [incomplete]" if r in incomplete else ""
        print(f"  [{i+1}] {date_str}  {dur:>6}  {source_label(r):<5}  {fname}{folder_str}{tag}")

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


def prompt_custom_instructions():
    """Ask for one-off summarization steering after a selection is made.

    Read line by line until an explicit terminator rather than taking a single
    input(): these are usually pasted paragraphs, and a one-line read both
    silently truncated the instructions and left the remaining lines in stdin,
    where the next prompt would have consumed them as selection commands.
    Blank first line means "none"; EOF (piped stdin) ends the read."""
    print()
    print("  Custom summarization instructions (optional) — Enter = none.")
    print("  Multi-line: paste freely, then finish with a line containing only '.'")
    lines = []
    while True:
        try:
            # No continuation prompt: it would interleave with pasted lines and
            # make the paste unreadable as it echoes.
            line = input("instructions> " if not lines else "")
        except EOFError:
            break
        if line.strip() == ".":
            break
        if not lines and not line.strip():
            return ""
        lines.append(line)
    return "\n".join(lines).strip()


def cmd_list(args, token, config, state, folders):
    recordings = list_all_recordings(token, config, args.days)
    transcribed = state.get("transcribed", {})

    auto_skip_short(recordings, state)
    transcribed = state.get("transcribed", {})

    pending = [r for r in recordings if r["id"] not in transcribed]
    done = [r for r in recordings if r["id"] in transcribed]
    print(f"\n{len(pending)} pending, {len(done)} done/skipped")
    print("  ✓ done · ~ incomplete · – skipped · (blank) pending\n")

    def shorten(path):
        """Show the output path relative to $HOME (~) so it stays readable."""
        try:
            return f"~/{Path(path).relative_to(Path.home())}"
        except ValueError:
            return str(path)

    for r in recordings:
        dt = local_dt(r.get("start_time", 0), config)
        date_str = dt.strftime("%Y-%m-%d %H:%M")
        dur = format_duration(r.get("duration", 0))
        fname = r.get("filename", "untitled")
        folder = get_folder_name(r, folders)
        folder_str = f" ({folder})" if folder else ""

        entry = transcribed.get(r["id"], {})
        entry_status = entry.get("status")
        if entry_status == "skipped":
            status = "–"
        elif entry_status == "done":
            status = "✓"
        elif entry:  # transcribed but summarization not finished
            status = "~"
        else:
            status = " "

        print(f"  {status} {date_str}  {dur:>6}  {source_label(r):<5}  {fname}{folder_str}")

        # Overlay where the outputs landed, so `list` doubles as a "where is it?"
        if entry_status == "done" and entry.get("output_dir"):
            project = entry.get("project")
            tag = f"  [{project}]" if project and project != "default" else ""
            print(f"        → {shorten(entry['output_dir'])}{tag}")
        elif entry and entry_status not in ("done", "skipped") and entry.get("work_dir"):
            print(f"        → {shorten(entry['work_dir'])}  (incomplete — will retry)")


def git_commit_working(results, config):
    """Optionally commit each recording's output into the repo that actually
    contains it (the destination project repo), not the plaud-sync clone.

    Off by default — enable with "auto_commit": true in ~/.plaud-sync/config.json.
    Output dirs that aren't inside a git repo (or are gitignored there) are
    silently skipped.
    """
    if not results or not config.get("auto_commit"):
        return

    # Group output dirs by their containing git repo root.
    repos = {}
    for r in results:
        out = r.get("output_dir")
        if not out:
            continue
        try:
            top = subprocess.run(
                ["git", "-C", out, "rev-parse", "--show-toplevel"],
                capture_output=True, text=True, timeout=10,
            )
        except Exception:
            continue
        root = top.stdout.strip()
        if top.returncode == 0 and root:
            repos.setdefault(root, []).append(out)

    for root, dirs in repos.items():
        try:
            subprocess.run(["git", "-C", root, "add", *dirs], capture_output=True, timeout=15)
            status = subprocess.run(
                ["git", "-C", root, "status", "--porcelain", *dirs],
                capture_output=True, text=True, timeout=15,
            )
            if status.stdout.strip():
                subprocess.run(
                    ["git", "-C", root, "commit", "-m", f"Add {len(dirs)} transcript(s)"],
                    capture_output=True, timeout=30,
                )
                print(f"  committed {len(dirs)} transcript(s) in {root}")
        except Exception:
            pass


def execute_selection(selected, merge, token, config, folders, projects, state, language,
                      extra_instructions=None):
    results = []
    if extra_instructions:
        print(f"\n  Custom instructions: {extra_instructions}")
    if merge:
        print(f"\nMerging & processing {len(selected)} recording(s)...")
        result = process_merged_recordings(selected, token, config, folders, projects, state, language,
                                           extra_instructions=extra_instructions)
        if result:
            results.append(result)
    else:
        print(f"\nProcessing {len(selected)} recording(s)...")
        for rec in selected:
            result = process_recording(rec, token, config, folders, projects, state, language,
                                       extra_instructions=extra_instructions)
            if result:
                results.append(result)

    git_commit_working(results, config)

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
        target_base = default_output_dir(config)

    old_dir = Path(entry["output_dir"])
    new_dir = target_base / old_dir.name

    if old_dir.resolve() == new_dir.resolve():
        # Files are already in the right place, but the state label can lag behind
        # (e.g. the directory was written before the project existed in config, so
        # the entry is still tagged "default"). Re-tag it instead of bailing out.
        if entry.get("project") != project_key:
            was = entry.get("project")
            entry["project"] = project_key
            entry["output_dir"] = str(new_dir)
            save_state(state)
            print(f"\nRe-tagged {file_id} (files already in place)")
            print(f"  project: {was!r} → {project_key!r}")
            print(f"  {new_dir}")
            return True
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
    recordings = list_all_recordings(token, config, args.days)

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
        pending, _ = show_pending(recordings, folders, state, config)
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

        instructions = prompt_custom_instructions()

        execute_selection(selected, merge, token, config, folders, projects, state, args.language,
                          extra_instructions=instructions)


EXAMPLE_CONFIG = {
    "projects": {
        "project-a": "~/git/project-a/docs/transcripts",
        "project-b": "~/git/project-b/docs/transcripts",
    },
    # Optional per-project topic keywords. Classification also learns from the
    # names of recordings already filed under each project, so this is only
    # needed for projects with little or no history.
    "project_hints": {
        "project-a": "keywords, systems or people that identify this project",
    },
    "default_output": str(DEFAULT_OUTPUT_DIR),
    "timezone": "Europe/Prague",
    "language": "en",
    "auto_commit": False,
    "process_bookmarks": False,
    "wispr": {
        "enabled": False,
        "data_dir": "~/Library/Application Support/Wispr Flow",
        "keep_wispr_summary": True,
    },
}


def cmd_init(force=False):
    """Write a starter config to ~/.plaud-sync/config.json."""
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    if CONFIG_FILE.exists() and not force:
        print(f"Config already exists at {CONFIG_FILE}")
        print("Use 'init --force' to overwrite.")
        return
    CONFIG_FILE.write_text(json.dumps(EXAMPLE_CONFIG, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"Wrote starter config to {CONFIG_FILE}")
    print("Edit 'projects' to map classification labels to your repo paths.")


def main():
    parser = argparse.ArgumentParser(
        prog="plaud-transcribe",
        description="Download, transcribe, classify and summarize Plaud recordings",
    )
    parser.add_argument("command", nargs="?", default="process", choices=["list", "process", "move", "init"])
    parser.add_argument("extra", nargs="*", help="For 'move': <file_id|dir-name|path> <project>")
    parser.add_argument("--days", type=int, default=30)
    parser.add_argument("--all", action="store_true", help="Process all unprocessed recordings")
    parser.add_argument("--language", default=None, help="Primary language (default: config 'language' or cs)")
    parser.add_argument("--force", action="store_true", help="For 'init': overwrite an existing config")
    args = parser.parse_args()

    config = load_config()
    state = load_state()

    # Resolve language: CLI flag > config "language" > cs
    if not args.language:
        args.language = config.get("language", "cs")

    if args.command == "init":
        cmd_init(force=args.force)
        return

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
    try:
        main()
    except KeyboardInterrupt:
        # Ctrl-C is a normal way out of a long transcription — report it as one
        # rather than dumping a traceback over the progress output.
        print("\nInterrupted.", file=sys.stderr)
        sys.exit(130)
