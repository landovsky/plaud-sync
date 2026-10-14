"""google_recorder: list and pull Google Recorder (Pixel) recordings over adb.

The Pixel Recorder app writes every recording straight to shared storage
(`/sdcard/Recordings/<title>.m4a`) and registers it in Android's MediaStore with
`owner_package_name=com.google.android.apps.recorder` — so nothing has to be
exported or shared from the phone first. MediaStore also gives us, without
touching the audio:

    datetaken   epoch ms when the file was last finalized — normally the end
                of the recording, but a later edit in the app moves it
    duration    ms
    _size       bytes

The start time comes from Recorder's default title ("Oct 13 at 10_12 …"), which
records when recording began and survives edits; datetaken - duration is the
fallback for retitled recordings.

Listing is a single `adb shell content query`; audio is pulled only when a
recording is actually processed, mirroring Plaud's list-then-download flow.
Everything is best-effort: no adb, no phone, or an unauthorised phone degrades
to "no Recorder recordings" and never blocks the other sources.
"""

import re
import shutil
import subprocess
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

RECORDER_PACKAGE = "com.google.android.apps.recorder"
MEDIA_URI = "content://media/external/audio/media"
PROJECTION = ["_data", "datetaken", "duration", "_size"]

# Projection order is fixed, so anchor on the trailing numeric fields: a title
# may itself contain ", " or "=", which would break a naive split.
ROW_RE = re.compile(
    r"^Row: \d+ _data=(?P<path>.*), datetaken=(?P<taken>\d+|NULL), "
    r"duration=(?P<duration>\d+|NULL), _size=(?P<size>\d+|NULL)$"
)

# Recorder's default titles say when recording started: "Oct 14 at 09_51",
# "Thursday at 06_53" (within the last week), "Today at 08_10". Users keep the
# stamp and append a topic, so it can sit anywhere in a renamed title.
_MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
_WEEKDAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
_STAMP_RE = re.compile(
    r"(?:(?P<mon>" + "|".join(_MONTHS) + r") (?P<day>\d{1,2})|(?P<wd>" + "|".join(_WEEKDAYS)
    + r"|Today|Yesterday)) at (?P<h>\d{2})_(?P<m>\d{2})(?: \(\d+\))?"
)


def _cfg(config):
    return config.get("recorder") or {}


def enabled(config):
    return bool(_cfg(config).get("enabled"))


def adb_bin(config):
    return _cfg(config).get("adb") or (config.get("visuals") or {}).get("adb") or "adb"


def clean_title(name):
    """'Oct 14 at 09_51 hh Šárka obchodní' -> 'hh Šárka obchodní'. The date
    already lives in the output path. A bare default title ('Sunday at 14_50')
    is kept as is rather than becoming empty."""
    stem = Path(name).stem
    rest = re.sub(r"^Copy of ", "", _STAMP_RE.sub("", stem, count=1))
    rest = re.sub(r"\s{2,}", " ", rest).strip(" -–_")
    return rest or stem


def title_start(name, finalized, tz):
    """Start time from the title's stamp, resolved against when the file was
    finalized (year for 'Oct 13', the date for 'Thursday'). None without a
    stamp, or if the stamp would land after the file was finalized."""
    m = _STAMP_RE.search(Path(name).stem)
    if not m:
        return None
    ref = finalized.astimezone(tz)
    hm = {"hour": int(m["h"]), "minute": int(m["m"]), "second": 0, "microsecond": 0}
    try:
        if m["mon"]:
            start = ref.replace(month=_MONTHS.index(m["mon"]) + 1, day=int(m["day"]), **hm)
            if start > ref:   # recorded in December, finalized in January
                start = start.replace(year=start.year - 1)
        else:
            if m["wd"] == "Today":
                back = 0
            elif m["wd"] == "Yesterday":
                back = 1
            else:
                back = (ref.weekday() - _WEEKDAYS.index(m["wd"])) % 7
            start = (ref - timedelta(days=back)).replace(**hm)
            if start > ref:
                start -= timedelta(days=7)
    except ValueError:   # e.g. Feb 29 resolved into a non-leap year
        return None
    return start


def _device_ready(adb):
    try:
        out = subprocess.run([adb, "devices"], capture_output=True, text=True, timeout=20).stdout
    except (subprocess.SubprocessError, OSError):
        return False
    return any(l.split()[1:2] == ["device"] for l in out.splitlines()[1:])


def parse_rows(output):
    """MediaStore `content query` output -> [{path, taken, duration, size}]."""
    rows = []
    for line in output.splitlines():
        m = ROW_RE.match(line.strip())
        if not m or m["taken"] == "NULL":
            continue
        rows.append({
            "path": m["path"],
            "taken": int(m["taken"]),
            "duration": int(m["duration"]) if m["duration"] != "NULL" else 0,
            "size": int(m["size"]) if m["size"] != "NULL" else 0,
        })
    return rows


def to_recording(row, tz):
    """A MediaStore row in the shared recording shape. The id is MediaStore's
    datetaken: a rename in the app changes the file name but not datetaken, so
    retitling on the phone does not make a processed recording look new."""
    name = row["path"].rsplit("/", 1)[-1]
    start = title_start(name, datetime.fromtimestamp(row["taken"] / 1000, timezone.utc), tz)
    return {
        "id": f"recorder:{row['taken']}",
        "source": "recorder",
        "filename": clean_title(name),
        "start_time": int(start.timestamp() * 1000) if start else row["taken"] - row["duration"],
        "duration": row["duration"],
        "_recorder": {"path": row["path"], "size": row["size"]},
    }


def list_recordings(config, days):
    """Recorder recordings from the last `days` days, newest first."""
    if not enabled(config):
        return []
    adb = adb_bin(config)
    if not shutil.which(adb):
        print(f"Note: '{adb}' not found — Google Recorder recordings not listed", file=sys.stderr)
        return []
    if not _device_ready(adb):
        print("Note: phone not connected — Google Recorder recordings not listed", file=sys.stderr)
        return []
    # One string for `adb shell` (it re-joins args through the device shell);
    # the where-clause quotes must survive that.
    cmd = (f"content query --uri {MEDIA_URI} --projection {':'.join(PROJECTION)} "
           f"--where \"owner_package_name='{RECORDER_PACKAGE}'\"")
    try:
        out = subprocess.run([adb, "shell", cmd], capture_output=True, text=True, timeout=60).stdout
    except (subprocess.SubprocessError, OSError) as e:
        print(f"Note: Google Recorder listing failed ({e})", file=sys.stderr)
        return []
    cutoff_ms = int((datetime.now(timezone.utc) - timedelta(days=days)).timestamp() * 1000)
    import plaud_transcribe as pt
    tz = pt.local_tz(config)
    recs = [to_recording(r, tz) for r in parse_rows(out)]
    recs = [r for r in recs if r["start_time"] >= cutoff_ms]
    recs.sort(key=lambda r: r["start_time"], reverse=True)
    return recs


def pull(recording, config, dest_dir):
    """Copy the recording's audio off the phone into dest_dir (cached by id).
    Pulls to a temp name first so an interrupted transfer never looks cached."""
    info = recording["_recorder"]
    ext = Path(info["path"]).suffix or ".m4a"
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / f"{recording['id'].replace(':', '_')}{ext}"
    if dest.exists() and (not info.get("size") or dest.stat().st_size == info["size"]):
        return dest
    adb = adb_bin(config)
    if not _device_ready(adb):
        raise RuntimeError("phone not connected — reconnect it to pull the Recorder audio")
    tmp = dest.with_suffix(dest.suffix + ".part")
    res = subprocess.run([adb, "pull", info["path"], str(tmp)], capture_output=True, text=True,
                         timeout=900)
    if res.returncode != 0 or not tmp.exists():
        tmp.unlink(missing_ok=True)
        raise RuntimeError(f"adb pull failed: {(res.stderr or res.stdout).strip()}")
    tmp.rename(dest)
    return dest
