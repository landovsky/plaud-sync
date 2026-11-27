"""phone_calls: list and pull Pixel Phone app call recordings over adb.

The Phone app keeps its call recordings in app-private storage that adb can't
reach. The only way out is the app's own Share → Files, which saves
`/sdcard/Download/YYYY-MM-DD HH.MM.SS.m4a` (a second share of the same call
becomes `… (1).m4a`). Those exports are what this source lists.

Three times matter, and they are easy to mix up:

    stamp       in the file name: when the CALL started (equals the call log's
                `date` to the second). It names the call, so it is the id.
    datetaken   MediaStore: when the RECORDING ended (the m4a's creation_time)
    duration    MediaStore: how long the recording is

Recording can start anywhere in a call, so the recording's start is
datetaken - duration, never the stamp. Using the stamp put a recording up to
half an hour too early and pushed in-call screenshots out of it (2026-11-26).

Who was on the line comes from the call log (`content://call_log/calls`, which
adb's shell user may read): the call whose `date` is the stamp, within
`calls.match_slack_s`. The listing never needs a call-log match: a recording
with no call is still listed, titled `Call · <stamp>`.

Config (`calls` in ~/.plaud-sync/config.json):

    enabled        false by default
    dir            /storage/emulated/0/Download
    pattern        regex for the export names; group 1 is the stamp
    match_slack_s  5: how far the call log's `date` may be from the stamp
    adb            falls back to recorder.adb, then visuals.adb, then "adb"

Recipes:

    # see the call rows (and why they are missing, on stderr)
    plaud-transcribe list
    # transcribe one call
    plaud-transcribe process call:20261126-090907
    # queue it for the runner
    plaud-transcribe enqueue --recording call:20261126-090907 --project <name>

Everything is best-effort, like the Recorder: no adb, no phone or an
unauthorised phone means no call rows plus one note on stderr, and the other
sources carry on.
"""

import re
import shutil
import subprocess
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

import adb_link
import google_recorder

DEFAULT_DIR = "/storage/emulated/0/Download"
DEFAULT_PATTERN = r"^(\d{4}-\d{2}-\d{2} \d{2}\.\d{2}\.\d{2})(?: \(\d+\))?\.(m4a|mp3|wav)$"
DEFAULT_SLACK_S = 5
STAMP_FORMAT = "%Y-%m-%d %H.%M.%S"

MEDIA_URI = "content://media/external/audio/media"
MEDIA_PROJECTION = google_recorder.PROJECTION   # _data, datetaken, duration, _size
CALL_LOG_URI = "content://call_log/calls"
# name last: a contact name may contain ", " and is matched greedily to the end
CALL_LOG_PROJECTION = ["date", "duration", "type", "number", "name"]
CALL_ROW_RE = re.compile(
    r"^Row: \d+ date=(?P<date>\d+), duration=(?P<duration>\d+|NULL), "
    r"type=(?P<type>\d+|NULL), number=(?P<number>.*?), name=(?P<name>.*)$"
)
# CallLog.Calls.TYPE: 1 incoming, 2 outgoing. The others (missed, rejected,
# blocked …) never have a recording, so they get no direction.
DIRECTIONS = {1: "in", 2: "out"}

CONFIG_HELP = """\
config: calls  (phone call recordings, exported with the Phone app's Share → Files)
    "calls": {"enabled": true, "dir": "/storage/emulated/0/Download",
              "pattern": "<regex, group 1 = YYYY-MM-DD HH.MM.SS>", "match_slack_s": 5}
  enabled        list them (default false). Rows: call:<YYYYMMDD-HHMMSS>, titled
                 "Call · <contact or number> · <in|out>" from the call log
  dir            phone folder the exports land in (default /storage/emulated/0/Download)
  pattern        export file names; default matches "2026-11-26 09.09.07.m4a" and its
                 second share "2026-11-26 09.09.07 (1).m4a" (listed once)
  match_slack_s  seconds between the name's stamp and the call log's date (default 5)
  adb            adb binary; shared with "recorder": {"adb": ...} (default "adb")
  The stamp is when the call started; the recording's own start is MediaStore
  datetaken (its end) minus its duration, so visuals line up with what was recorded.
"""


def _cfg(config):
    return config.get("calls") or {}


def enabled(config):
    return bool(_cfg(config).get("enabled"))


def adb_bin(config):
    return _cfg(config).get("adb") or google_recorder.adb_bin(config)


def calls_dir(config):
    return (_cfg(config).get("dir") or DEFAULT_DIR).rstrip("/")


def pattern(config):
    return re.compile(_cfg(config).get("pattern") or DEFAULT_PATTERN)


def slack_s(config):
    return float(_cfg(config).get("match_slack_s", DEFAULT_SLACK_S))


# --- parsing (pure) ---

def parse_stamp(name, pat=None):
    """'2026-11-26 09.09.07 (1).m4a' -> naive datetime(2026, 11, 26, 9, 9, 7), or
    None when the name isn't a call export (or the stamp isn't a real time)."""
    m = (pat or re.compile(DEFAULT_PATTERN)).match(name)
    if not m:
        return None
    try:
        return datetime.strptime(m.group(1), STAMP_FORMAT)
    except ValueError:
        return None


def call_id(stamp):
    return f"call:{stamp:%Y%m%d-%H%M%S}"


def parse_media_rows(output):
    """MediaStore output -> [{path, taken|None, duration, size}]. Unlike the
    Recorder, a NULL datetaken is kept: the stamp still dates the call."""
    rows = []
    for line in output.splitlines():
        m = google_recorder.ROW_RE.match(line.strip())
        if not m:
            continue
        num = lambda k: int(m[k]) if m[k] != "NULL" else None  # noqa: E731
        rows.append({"path": m["path"], "taken": num("taken"),
                     "duration": num("duration") or 0, "size": num("size") or 0})
    return rows


def parse_call_log(output):
    """Call log output -> [{date (epoch ms), duration (s), type, number, name}]."""
    calls = []
    for line in output.splitlines():
        m = CALL_ROW_RE.match(line.strip())
        if not m:
            continue
        null = lambda v: None if v in ("NULL", "") else v  # noqa: E731
        calls.append({
            "date": int(m["date"]),
            "duration": int(m["duration"]) if m["duration"] != "NULL" else 0,
            "type": int(m["type"]) if m["type"] != "NULL" else None,
            "number": null(m["number"]),
            "name": null(m["name"]),
        })
    return calls


def match_call(stamp, calls, tz, slack):
    """The call log row that started at `stamp` (a naive phone-local time), or None.

    Compared in local wall-clock time, the same way the phone wrote the name, so
    a stamp inside a DST change still matches. With two calls inside the slack
    the nearest wins. A call's duration is never used: a recording may cover any
    part of its call."""
    best, best_diff = None, None
    for c in calls:
        local = datetime.fromtimestamp(c["date"] / 1000, tz).replace(tzinfo=None)
        diff = abs((local - stamp).total_seconds())
        if diff <= slack and (best_diff is None or diff < best_diff):
            best, best_diff = c, diff
    return best


def _title(stamp, counterpart, direction):
    if not counterpart:
        return f"Call · {stamp:{STAMP_FORMAT}}"
    return " · ".join(["Call", counterpart] + ([direction] if direction else []))


def to_recording(row, stamp, tz, calls=(), slack=DEFAULT_SLACK_S):
    """A Download export in the shared recording shape."""
    call = match_call(stamp, calls, tz, slack)
    counterpart = (call.get("name") or call.get("number")) if call else None
    direction = DIRECTIONS.get(call.get("type")) if call else None
    if row["taken"]:
        start_ms = row["taken"] - row["duration"]
    else:   # no end time known: the call's start is the best date there is
        start_ms = int(stamp.replace(tzinfo=tz).timestamp() * 1000)
    rec = {
        "id": call_id(stamp),
        "source": "call",
        "filename": _title(stamp, counterpart, direction),
        "start_time": start_ms,
        "duration": row["duration"],
        "counterpart": counterpart,
        "direction": direction,
        "call_start": call["date"] if call else int(stamp.replace(tzinfo=tz).timestamp() * 1000),
        "call_duration": call["duration"] * 1000 if call else None,
        "_call": {"path": row["path"], "size": row["size"], "stamp": f"{stamp:{STAMP_FORMAT}}"},
    }
    return rec


def build_recordings(media_rows, call_rows, config, tz):
    """MediaStore rows -> one recording per call, newest first. A second share of
    the same call (`… (1).m4a`) has the same stamp, so the same id: kept once,
    preferring the unsuffixed file."""
    pat, folder = pattern(config), calls_dir(config)
    by_id = {}
    for row in sorted(media_rows, key=lambda r: (" (" in r["path"].rsplit("/", 1)[-1], r["path"])):
        parent, _, name = row["path"].rpartition("/")
        if parent.rstrip("/") != folder:
            continue
        stamp = parse_stamp(name, pat)
        if stamp is None:
            continue
        rec = to_recording(row, stamp, tz, call_rows, slack_s(config))
        by_id.setdefault(rec["id"], rec)
    return sorted(by_id.values(), key=lambda r: r["start_time"], reverse=True)


# --- adb ---

# The only three ways this module touches adb (tests replace them).

def _adb_available(adb):
    return bool(shutil.which(adb))


def _phone_state(adb):
    """'ready' | 'unauthorized' | 'absent', trying Wi-Fi when there is no cable."""
    return adb_link.find_phone(adb)


def _adb(adb, *args, timeout=60):
    return subprocess.run([adb, *args], capture_output=True, text=True, timeout=timeout)


def _shell(adb, cmd):
    return _adb(adb, "shell", cmd).stdout


def list_recordings(config, days):
    """Exported call recordings from the last `days` days, newest first."""
    if not enabled(config):
        return []
    adb = adb_bin(config)
    if not _adb_available(adb):
        print(f"Note: '{adb}' not found — phone call recordings not listed", file=sys.stderr)
        return []
    state = _phone_state(adb)
    if state != "ready":
        why = ("phone not authorised — unlock it and allow USB debugging" if state == "unauthorized"
               else "no phone (cable or Wi-Fi)")
        print(f"Note: {why} — phone call recordings not listed", file=sys.stderr)
        return []
    folder = calls_dir(config)
    # One string for `adb shell`; the where-clause quotes must survive the device shell.
    media_cmd = (f"content query --uri {MEDIA_URI} --projection {':'.join(MEDIA_PROJECTION)} "
                 f"--where \"_data LIKE '{folder}/%'\"")
    try:
        media = parse_media_rows(_shell(adb, media_cmd))
    except (subprocess.SubprocessError, OSError) as e:
        print(f"Note: phone call listing failed ({e})", file=sys.stderr)
        return []
    cutoff_ms = int((datetime.now(timezone.utc) - timedelta(days=days)).timestamp() * 1000)
    calls = []
    if media:
        # a day of margin: the cutoff applies to the recording, the call began earlier
        since = cutoff_ms - 24 * 3600 * 1000
        log_cmd = (f"content query --uri {CALL_LOG_URI} --projection {':'.join(CALL_LOG_PROJECTION)} "
                   f"--where \"date >= {since}\"")
        try:
            calls = parse_call_log(_shell(adb, log_cmd))
        except (subprocess.SubprocessError, OSError) as e:
            print(f"Note: call log not readable ({e}) — calls listed without names", file=sys.stderr)
    import plaud_transcribe as pt
    recs = build_recordings(media, calls, config, pt.local_tz(config))
    return [r for r in recs if r["start_time"] >= cutoff_ms]


def pull(recording, config, dest_dir):
    """Copy the call's audio off the phone into dest_dir (cached by id). Pulls to
    a temp name first so an interrupted transfer never looks cached."""
    info = recording["_call"]
    ext = Path(info["path"]).suffix or ".m4a"
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / f"{recording['id'].replace(':', '_')}{ext}"
    if dest.exists() and (not info.get("size") or dest.stat().st_size == info["size"]):
        return dest
    adb = adb_bin(config)
    if _phone_state(adb) != "ready":
        raise RuntimeError("phone not connected — reconnect it to pull the call recording")
    tmp = dest.with_suffix(dest.suffix + ".part")
    res = _adb(adb, "pull", info["path"], str(tmp), timeout=900)
    if res.returncode != 0 or not tmp.exists():
        tmp.unlink(missing_ok=True)
        raise RuntimeError(f"adb pull failed: {(res.stderr or res.stdout).strip()}")
    tmp.rename(dest)
    return dest


# --- summarizer context ---

def _clock(ms, tz):
    return datetime.fromtimestamp(ms / 1000, tz).strftime("%H:%M:%S")


def _mmss(ms):
    s = int(round(ms / 1000))
    return f"{s // 3600}:{s % 3600 // 60:02d}:{s % 60:02d}" if s >= 3600 else f"{s // 60}:{s % 60:02d}"


def summary_context(recording, tz):
    """What the summarizer should know about a call: who, which way, and which
    stretch of the call the recording covers. '' for any other source."""
    if recording.get("source") != "call":
        return ""
    rec_start = recording.get("start_time") or 0
    rec_end = rec_start + (recording.get("duration") or 0)
    who = recording.get("counterpart")
    way = {"in": "incoming", "out": "outgoing"}.get(recording.get("direction"))
    lines = ["## Phone call",
             "This recording is a phone call"
             + (f" with {who}" if who else "") + (f" ({way})" if way else "") + "."]
    call_start, call_dur = recording.get("call_start"), recording.get("call_duration")
    span = f"{_clock(rec_start, tz)}–{_clock(rec_end, tz)}"
    if call_start and call_dur:
        missed = max(0, rec_start - call_start)
        lines.append(f"The call ran {_clock(call_start, tz)}–{_clock(call_start + call_dur, tz)} "
                     f"({_mmss(call_dur)}); the recording covers only {span}"
                     + (f", so the first {_mmss(missed)} of the call is not in the transcript."
                        if missed >= 60_000 else "."))
    elif call_start:
        lines.append(f"The call started at {_clock(call_start, tz)}; the recording covers {span}.")
    return "\n".join(lines)
