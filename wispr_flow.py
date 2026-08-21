"""wispr_flow: read finalized meetings from the local Wispr Flow app store.

Wispr Flow (https://wisprflow.ai) records meetings locally and — unlike Plaud —
produces the transcript *on-device*. Each finalized meeting has a diarized,
speaker-named `refined.ndjson` sitting on disk, so there's no audio download and
no re-transcription step: we read that transcript and hand it straight to the
same classify / summarize / route pipeline used for Plaud recordings.

Data layout (macOS):

    ~/Library/Application Support/Wispr Flow/
        flow.sqlite                       # Meetings table (title, summary, speakerMap, ...)
        meetings/<uuid>/
            refined.ndjson                # cleaned diarized transcript (one JSON obj/line)
            live.ndjson                   # real-time transcript (unused)
            speakers.observations.ndjson  # speaker diarization observations (unused)
            upload.ogg                    # source audio (unused — we already have text)

`refined.ndjson` line shape:
    {"id": "...", "timestamp": "MM:SS", "text": "...",
     "speaker": {"id": 1, "source": "refined", "name": null}}

Speaker names come from the meeting's `speakerMap` JSON (numeric speaker id ->
person -> display name); we resolve it into {speaker_id: name} up front.

Everything here is read-only and best-effort: the SQLite DB is opened `mode=ro`
(safe alongside the running app's WAL), and any failure degrades to "no Wispr
meetings" rather than blocking the Plaud pipeline.
"""

import json
import sqlite3
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

DEFAULT_WISPR_DIR = Path.home() / "Library" / "Application Support" / "Wispr Flow"


def _cfg(config):
    return config.get("wispr") or {}


def wispr_dir(config):
    val = _cfg(config).get("data_dir")
    return Path(val).expanduser() if val else DEFAULT_WISPR_DIR


def wispr_db_path(config):
    return wispr_dir(config) / "flow.sqlite"


def wispr_enabled(config):
    """True only when explicitly enabled in config *and* the store is present."""
    if not _cfg(config).get("enabled"):
        return False
    return wispr_db_path(config).exists()


def keep_wispr_summary(config):
    # Preserve Wispr's own summary next to ours unless the user opts out.
    return _cfg(config).get("keep_wispr_summary", True)


# --- SQLite (read-only) ---

def _connect(config):
    db = wispr_db_path(config)
    # mode=ro reads a live WAL database safely: Wispr keeps the app open and
    # writing, and a read-only connection sees a consistent snapshot. We never
    # write, so we never disturb Wispr's own state.
    uri = f"file:{db}?mode=ro"
    conn = sqlite3.connect(uri, uri=True, timeout=5)
    conn.row_factory = sqlite3.Row
    return conn


# --- Parsing helpers ---

def _parse_created(s):
    """Parse Wispr's createdAt (e.g. '2026-08-18 12:10:03.267 +00:00') to unix ms."""
    if not s:
        return None
    s = s.strip()
    for fmt in (
        "%Y-%m-%d %H:%M:%S.%f %z",
        "%Y-%m-%d %H:%M:%S %z",
        "%Y-%m-%d %H:%M:%S.%f%z",
        "%Y-%m-%d %H:%M:%S%z",
    ):
        try:
            return int(datetime.strptime(s, fmt).timestamp() * 1000)
        except ValueError:
            continue
    return None


def _ts_to_seconds(ts):
    """'MM:SS' or 'HH:MM:SS' (minutes uncapped, e.g. '114:58') -> seconds."""
    try:
        parts = [int(p) for p in str(ts).split(":")]
    except (ValueError, AttributeError):
        return None
    if len(parts) == 2:
        return parts[0] * 60 + parts[1]
    if len(parts) == 3:
        return parts[0] * 3600 + parts[1] * 60 + parts[2]
    return None


def _resolve_speakers(speaker_map):
    """Turn a meeting's speakerMap JSON into {speaker_id: display_name}.

    speakerMap shape:
        {"people": {"<personId>": {"name": "Tomáš", ...}},
         "assignments": {"1": {"consensus": "<personId>", "dom": "...", ...}}}
    """
    if not speaker_map:
        return {}
    if isinstance(speaker_map, str):
        try:
            speaker_map = json.loads(speaker_map)
        except (json.JSONDecodeError, TypeError):
            return {}
    people = speaker_map.get("people") or {}
    out = {}
    for num, assign in (speaker_map.get("assignments") or {}).items():
        assign = assign or {}
        pid = assign.get("consensus") or assign.get("dom") or assign.get("user") or assign.get("llm")
        person = people.get(pid) if pid else None
        name = person.get("name") if person else None
        if not name:
            continue
        try:
            out[int(num)] = name
        except (ValueError, TypeError):
            out[num] = name
    return out


def _refined_path(config, uuid):
    return wispr_dir(config) / "meetings" / uuid / "refined.ndjson"


def _audio_path(config, uuid):
    p = wispr_dir(config) / "meetings" / uuid / "upload.ogg"
    return p if p.exists() else None


# --- Public API ---

def list_meetings(config, days):
    """Return finalized Wispr meetings from the last `days`, normalized to the
    same recording shape the Plaud pipeline consumes.

    Best-effort: returns [] (with a stderr warning) on any read error, and skips
    meetings whose refined transcript isn't on disk yet.
    """
    if not wispr_enabled(config):
        return []
    try:
        conn = _connect(config)
    except sqlite3.Error as e:
        print(f"  Warning: could not open Wispr Flow store: {e}", file=sys.stderr)
        return []

    cutoff_ms = int((datetime.now(timezone.utc) - timedelta(days=days)).timestamp() * 1000)
    meetings = []
    try:
        rows = conn.execute(
            """
            SELECT id, title, summary, speakerMap, participantNames,
                   createdAt, endedAt
            FROM Meetings
            WHERE isDeleted = 0 AND finalized = 1
            ORDER BY createdAt DESC
            """
        ).fetchall()
    except sqlite3.Error as e:
        print(f"  Warning: could not read Wispr Flow meetings: {e}", file=sys.stderr)
        conn.close()
        return []
    finally:
        try:
            conn.close()
        except sqlite3.Error:
            pass

    for row in rows:
        uuid = row["id"]
        refined = _refined_path(config, uuid)
        if not refined.exists() or refined.stat().st_size == 0:
            continue  # transcript not ready — nothing to process yet

        created_ms = _parse_created(row["createdAt"])
        ended_ms = row["endedAt"]

        if created_ms and ended_ms:
            duration_ms = max(0, int(ended_ms) - created_ms)
        else:
            last_secs = _last_timestamp_seconds(refined)
            duration_ms = (last_secs * 1000) if last_secs else 0

        if created_ms is not None:
            start_ms = created_ms
        elif ended_ms:
            start_ms = int(ended_ms) - duration_ms
        else:
            start_ms = 0

        if start_ms < cutoff_ms:
            continue

        meetings.append({
            "id": f"wispr:{uuid}",
            "source": "wispr",
            "filename": row["title"] or "Untitled meeting",
            "start_time": start_ms,
            "duration": duration_ms,
            "filetag_id_list": [],
            "_wispr": {
                "uuid": uuid,
                "refined_path": str(refined),
                "audio_path": str(_audio_path(config, uuid)) if _audio_path(config, uuid) else None,
                "summary": row["summary"],
                "speaker_names": _resolve_speakers(row["speakerMap"]),
                "participant_names": row["participantNames"],
            },
        })
    return meetings


def _last_timestamp_seconds(refined_path):
    last = None
    try:
        with open(refined_path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    secs = _ts_to_seconds(json.loads(line).get("timestamp"))
                except (json.JSONDecodeError, AttributeError):
                    continue
                if secs is not None:
                    last = secs
    except OSError:
        return None
    return last


def transcript_text(recording):
    """Build a diarized transcript string from a meeting's refined.ndjson,
    formatted like the Plaud pipeline's output: `[MM:SS] Speaker: text`.

    Speaker labels use the resolved speakerMap name when available, otherwise
    fall back to `Speaker N`.
    """
    info = recording.get("_wispr") or {}
    refined_path = info.get("refined_path")
    speaker_names = info.get("speaker_names") or {}
    if not refined_path:
        return ""

    lines = []
    with open(refined_path, encoding="utf-8") as f:
        for raw in f:
            raw = raw.strip()
            if not raw:
                continue
            try:
                obj = json.loads(raw)
            except json.JSONDecodeError:
                continue
            text = (obj.get("text") or "").strip()
            if not text:
                continue
            spk = obj.get("speaker") or {}
            sid = spk.get("id")
            name = spk.get("name") or speaker_names.get(sid)
            if not name:
                name = f"Speaker {sid}" if sid is not None else "Speaker"
            ts = obj.get("timestamp") or ""
            lines.append(f"[{ts}] {name}: {text}")
    return "\n\n".join(lines)
