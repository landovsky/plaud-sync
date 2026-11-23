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
            live.ndjson                   # real-time transcript (fills the unrefined tail)
            speakers.observations.ndjson  # speaker diarization observations (unused)
            upload.ogg                    # source audio (unused — we already have text)

`refined.ndjson` line shape:
    {"id": "...", "timestamp": "MM:SS", "text": "...",
     "speaker": {"id": 1, "source": "refined", "name": null}}

Speaker names come from the meeting's `speakerMap` JSON (numeric speaker id ->
person -> display name); we resolve it into {speaker_id: name} up front.

A recording that was stopped and resumed has several segments. Wispr often
refines only the first one and never comes back for the rest (its
`refinePipelineRanThroughStopSeq` lags `latestRecordingStopSeq`, though the
counter itself isn't reliable), so `refined.ndjson` silently ends at the first
stop. The rest exists only in `live.ndjson` (lines tagged `"segment": N` on a
continuous `startRecordingMs` clock); we append every segment that starts after
the last refined line, so nothing said after a restart is lost.

Sometimes Wispr never writes `refined.ndjson` at all — on 2026-11-23 a meeting
with an "offline for 6 min" gap stayed unrefined for hours, and was skipped
without a word. A finalized meeting still unrefined `live_fallback_minutes`
(default 30) after it ended is therefore processed from `live.ndjson` instead,
with live speaker labels; until then it is held back with a note, since Wispr
may still be refining it.

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


DEFAULT_LIVE_FALLBACK_MIN = 30


def _cfg(config):
    return config.get("wispr") or {}


def live_fallback_minutes(config):
    """How long after a meeting ended to wait for Wispr's refined transcript
    before using the live one (`wispr.live_fallback_minutes`)."""
    return float(_cfg(config).get("live_fallback_minutes", DEFAULT_LIVE_FALLBACK_MIN))


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


def _live_path(config, uuid):
    return wispr_dir(config) / "meetings" / uuid / "live.ndjson"


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
            SELECT id, title, summary, notes, speakerMap, participantNames,
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

    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    for row in rows:
        uuid = row["id"]
        refined = _refined_path(config, uuid)
        live = _live_path(config, uuid)
        created_ms = _parse_created(row["createdAt"])
        ended_ms = row["endedAt"]
        if (created_ms or 0) < cutoff_ms and (ended_ms or 0) < cutoff_ms:
            continue
        title = row["title"] or "Untitled meeting"

        transcript_from = "refined"
        if not refined.exists() or refined.stat().st_size == 0:
            # Never skip silently: a meeting that just vanishes from the list is
            # only noticed when someone counts (2026-11-23).
            if not _live_last_ms(live):
                print(f"  Note: Wispr meeting '{title}' has no transcript yet — not listed",
                      file=sys.stderr)
                continue
            waited_min = (now_ms - int(ended_ms or created_ms or now_ms)) / 60000
            if waited_min < live_fallback_minutes(config):
                print(f"  Note: Wispr is still refining '{title}' — listed once refined, or "
                      f"from its live transcript after {live_fallback_minutes(config):g} min",
                      file=sys.stderr)
                continue
            transcript_from = "live"

        if created_ms and ended_ms:
            duration_ms = max(0, int(ended_ms) - created_ms)
        elif transcript_from == "live":
            duration_ms = _live_last_ms(live) or 0
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
            "filename": title,
            "start_time": start_ms,
            "duration": duration_ms,
            "filetag_id_list": [],
            "_wispr": {
                "uuid": uuid,
                "refined_path": str(refined),
                "live_path": str(live),
                "transcript_from": transcript_from,
                "audio_path": str(_audio_path(config, uuid)) if _audio_path(config, uuid) else None,
                "summary": row["summary"],
                "notes": row["notes"],
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


def _live_last_ms(live_path):
    """End of the last live utterance on the recording clock, or None if the
    live transcript is missing or has no text."""
    last = None
    try:
        with open(live_path, encoding="utf-8") as f:
            for raw in f:
                try:
                    obj = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                if (obj.get("text") or "").strip() and obj.get("endRecordingMs") is not None:
                    last = max(last or 0, int(obj["endRecordingMs"]))
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
    if info.get("transcript_from") == "live":
        # Wispr never refined it: the whole meeting comes from the live feed.
        return "\n\n".join(_unrefined_live_lines(info.get("live_path"), None, all_segments=True))
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
    lines += _unrefined_live_lines(live_path=info.get("live_path"),
                                   refined_through_secs=_last_timestamp_seconds(refined_path))
    return "\n\n".join(lines)


def _format_ms(ms):
    # Same uncapped-minutes clock as refined.ndjson ("97:57").
    secs = int(ms) // 1000
    return f"{secs // 60:02d}:{secs % 60:02d}"


def _unrefined_live_lines(live_path, refined_through_secs, all_segments=False):
    """Live-transcript lines for the segments Wispr never refined: every segment
    after the first whose first utterance starts after the last refined line.
    The first segment is always refined (we only get here with a non-empty
    refined.ndjson, and the live clock can run a few seconds off the refined
    one), and a later refined segment has refined lines at or past its own
    start, so this never repeats refined text.

    Live speaker ids come from a separate, mic-level diarization and don't match
    the refined ids, so they get their own `Live speaker N` label instead of
    being passed off as a refined speaker.

    With all_segments, every segment is returned — for a meeting Wispr never
    refined at all.
    """
    if not live_path or not Path(live_path).exists():
        return []
    segments = {}
    with open(live_path, encoding="utf-8") as f:
        for raw in f:
            raw = raw.strip()
            if not raw:
                continue
            try:
                obj = json.loads(raw)
            except json.JSONDecodeError:
                continue
            text = (obj.get("text") or "").strip()
            start = obj.get("startRecordingMs")
            if obj.get("segment") is None or start is None or not text:
                continue
            segments.setdefault(obj["segment"], []).append((start, obj, text))

    lines = []
    for seg in sorted(segments)[0 if all_segments else 1:]:
        utterances = sorted(segments[seg], key=lambda u: u[0])
        if refined_through_secs is not None and utterances[0][0] // 1000 <= refined_through_secs:
            continue  # refined already covers this segment
        for start, obj, text in utterances:
            spk = obj.get("speaker") or {}
            sid = spk.get("id")
            name = spk.get("name") or (f"Live speaker {sid}" if sid is not None else "Live speaker")
            lines.append(f"[{_format_ms(start)}] {name}: {text}")
    return lines
