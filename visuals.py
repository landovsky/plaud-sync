"""visuals: attach meeting screenshots and phone photos to a transcript.

Working meetings are heavily deictic — the speaker points at the screen and says
"tady". Measured on one 2.5-hour meeting, 31% of turns contain at least one
pointing word whose referent exists only on screen. Text-only, those passages are
unresolvable; the summarizer drops them or guesses. This module collects the
images that were captured while the recording was running, captions them, and
feeds that back into the transcript and the summary.

Two placement classes, deliberately different:

  anchored    — captured inside a recording segment. Time is exact, so the image
                is interleaved into the transcript at its offset.
  associated  — captured during a pause, or shortly after the recording ended
                (photographing a whiteboard happens *after* the discussion).
                Time cannot place these, so they are listed but never asserted
                to belong at a particular moment.

The distinction matters: a Wispr meeting's wall-clock span includes pauses, and
one observed pause contained an entirely different recorded meeting. Attributing
by span rather than by segment files those images under the wrong meeting.

Everything degrades: no images, no phone, no `adb`, no `sips`, a failed caption —
each is a warning and an empty result, never a blocked transcript.
"""

import json
import os
import re
import shutil
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

DEFAULT_SCREENSHOT_DIRS = ["~/Desktop"]
DEFAULT_PHONE_DIRS = ["/sdcard/DCIM/Camera", "/sdcard/Pictures/Screenshots"]
IMAGE_SUFFIXES = (".png", ".jpg", ".jpeg", ".heic")
DEFAULT_TAIL_MIN = 30      # a whiteboard photo taken after the meeting still belongs to it
DEFAULT_MAX_WIDTH = 1600   # UI text stays legible; 31 MB of originals became 4.4 MB


def _cfg(config):
    return (config or {}).get("visuals") or {}


def enabled(config):
    """Opt-in only. Screenshots are indiscriminate — they capture whatever else
    was on screen — so sweeping them into a repo is the user's call, not ours."""
    return bool(_cfg(config).get("enabled"))


def screenshot_dirs(config):
    return [Path(d).expanduser() for d in _cfg(config).get("screenshot_dirs", DEFAULT_SCREENSHOT_DIRS)]


def phone_dirs(config):
    return _cfg(config).get("phone_dirs", DEFAULT_PHONE_DIRS)


def tail_minutes(config):
    return int(_cfg(config).get("tail_minutes", DEFAULT_TAIL_MIN))


def max_width(config):
    return int(_cfg(config).get("max_width", DEFAULT_MAX_WIDTH))


def adb_bin(config):
    return _cfg(config).get("adb", "adb")


# --- Capture time ---

def capture_time(path, tz):
    """When the image was taken.

    Prefer the filename: every screenshot tool stamps it there, and unlike mtime
    it survives copying (an adb pull rewrites mtime to the pull time).
    """
    name = Path(path).name
    m = re.search(r"(\d{4})-(\d{2})-(\d{2}) at (\d{1,2})\.(\d{2})\.(\d{2})", name)
    if m:  # macOS: "Screenshot 2026-09-07 at 10.16.36.png" — local time
        y, mo, d, h, mi, s = map(int, m.groups())
        return datetime(y, mo, d, h, mi, s, tzinfo=tz)
    m = re.search(r"PXL_(\d{4})(\d{2})(\d{2})_(\d{2})(\d{2})(\d{2})", name)
    if m:  # Pixel camera — filename is UTC
        y, mo, d, h, mi, s = map(int, m.groups())
        return datetime(y, mo, d, h, mi, s, tzinfo=timezone.utc).astimezone(tz)
    m = re.search(r"Screenshot_(\d{4})(\d{2})(\d{2})-(\d{2})(\d{2})(\d{2})", name)
    if m:  # Android screenshot — local time
        y, mo, d, h, mi, s = map(int, m.groups())
        return datetime(y, mo, d, h, mi, s, tzinfo=tz)
    try:
        return datetime.fromtimestamp(Path(path).stat().st_mtime, tz=tz)
    except OSError:
        return None


# --- Recording windows ---

def windows_for(recordings, config):
    """Wall-clock windows of *actual recording*, with the recording-time offset
    each window starts at, as [(part, start_dt, end_dt, base_offset_s)].

    Wispr meetings are stop/resume: their segments come from the live transcript,
    because createdAt→endedAt spans the pauses too. Plaud recordings are
    continuous, so start + duration is the window.
    """
    import plaud_transcribe as pt  # local import: avoids a circular import at module load
    tz = pt.local_tz(config)
    out = []
    for i, rec in enumerate(recordings, start=1):
        start_ms = rec.get("start_time") or 0
        segs = _wispr_segments(rec) if rec.get("source") == "wispr" else None
        if segs:
            for s in segs:
                out.append((i, datetime.fromtimestamp(s["e0"] / 1000, tz),
                            datetime.fromtimestamp(s["e1"] / 1000, tz), s["r0"] / 1000.0))
        else:
            dur = (rec.get("duration") or 0) / 1000.0
            begin = datetime.fromtimestamp(start_ms / 1000, tz)
            out.append((i, begin, begin + timedelta(seconds=dur), 0.0))
    return out


def _wispr_segments(rec):
    path = ((rec.get("_wispr") or {}).get("refined_path") or "")
    live = Path(path).parent / "live.ndjson" if path else None
    if not live or not live.exists():
        return None
    segs = {}
    try:
        with open(live, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    o = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not o.get("startEpochMs"):
                    continue
                d = segs.setdefault(o.get("segment", 0), {
                    "e0": o["startEpochMs"], "e1": o["endEpochMs"],
                    "r0": o["startRecordingMs"], "r1": o["endRecordingMs"]})
                d["e0"] = min(d["e0"], o["startEpochMs"]); d["e1"] = max(d["e1"], o["endEpochMs"])
                d["r0"] = min(d["r0"], o["startRecordingMs"]); d["r1"] = max(d["r1"], o["endRecordingMs"])
    except OSError:
        return None
    return [segs[k] for k in sorted(segs)] or None


# --- Collection ---

def _device_state(adb):
    """'ready' | 'unauthorized' | 'absent' — the three cases worth telling apart."""
    try:
        devs = subprocess.run([adb, "devices"], capture_output=True, text=True, timeout=20).stdout
    except (subprocess.SubprocessError, OSError):
        return "absent"
    lines = [l for l in devs.splitlines()[1:] if l.strip()]
    if any(l.split()[1:2] == ["device"] for l in lines):
        return "ready"
    if any("unauthorized" in l for l in lines):
        return "unauthorized"
    return "absent"


def _await_phone(adb):
    """Ask the user to plug the phone in, and retry until they decline.

    Phone photos are often the only record of a physical drawing or of the other
    participant's screen, and the pipeline may be run hours later — a silent skip
    would lose them with no way to notice. Non-interactive runs (cron, piped
    stdin) skip without asking, so automation never blocks on a prompt.
    """
    state = _device_state(adb)
    if state == "ready":
        return True
    if not (sys.stdin.isatty() and sys.stderr.isatty()):
        print(f"  Note: phone {state} — skipping phone photos (non-interactive run)",
              file=sys.stderr)
        return False

    while True:
        if state == "unauthorized":
            print("\n  Phone is connected but not authorised — accept the "
                  "USB-debugging prompt on the device.")
        else:
            print("\n  No phone detected. Connect it to include photos taken during "
                  "the meeting.")
        try:
            answer = input("  Retry? [Y/n] ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print()
            return False
        if answer in ("n", "no", "s", "skip"):
            print("  Skipping phone photos.")
            return False
        state = _device_state(adb)
        if state == "ready":
            print("  Phone detected.")
            return True


def _pull_phone(config, lo, hi, staging):
    """Copy phone images whose filename time falls in the window."""
    adb = adb_bin(config)
    if not phone_dirs(config):
        return []
    if not shutil.which(adb):
        print(f"  Note: '{adb}' not found — skipping phone photos", file=sys.stderr)
        return []
    if not _await_phone(adb):
        return []

    import plaud_transcribe as pt
    tz = pt.local_tz(config)
    pulled = []
    staging.mkdir(parents=True, exist_ok=True)
    for remote in phone_dirs(config):
        try:
            listing = subprocess.run([adb, "shell", f"ls {remote} 2>/dev/null"],
                                     capture_output=True, text=True, timeout=60).stdout
        except (subprocess.SubprocessError, OSError):
            continue
        for name in listing.split():
            if not name.lower().endswith(IMAGE_SUFFIXES):
                continue
            t = capture_time(name, tz)   # filename only — no stat() on the device
            if not t or not (lo <= t <= hi):
                continue
            dest = staging / name
            try:
                subprocess.run([adb, "pull", f"{remote}/{name}", str(dest)],
                               capture_output=True, timeout=180)
            except (subprocess.SubprocessError, OSError):
                continue
            if dest.exists():
                pulled.append(dest)

    # The phone is only needed for this step. Say so explicitly — otherwise it
    # stays plugged in for the rest of the run (captioning and summarizing can
    # take minutes) for no reason.
    if pulled:
        print(f"  {len(pulled)} phone photo(s) copied — you can unplug the phone now.")
    else:
        print("  No phone photos in the meeting window — you can unplug the phone now.")
    return pulled


def collect(recordings, config, staging):
    """Find every image belonging to this session and classify it.

    Returns records: {path, part, offset, source, klass, taken}
      klass "anchored"   -> offset is meaningful, image goes inline in the transcript
      klass "associated" -> taken in a pause or the post-meeting tail; listed only
    """
    import plaud_transcribe as pt
    tz = pt.local_tz(config)
    wins = windows_for(recordings, config)
    if not wins:
        return []
    lo = min(w[1] for w in wins)
    hi = max(w[2] for w in wins) + timedelta(minutes=tail_minutes(config))

    candidates = []
    for d in screenshot_dirs(config):
        if not d.is_dir():
            continue
        for p in d.iterdir():
            if p.suffix.lower() in IMAGE_SUFFIXES:
                candidates.append(p)
    candidates += _pull_phone(config, lo, hi, staging)

    records = []
    for p in candidates:
        t = capture_time(p, tz)
        if not t or not (lo <= t <= hi):
            continue
        hit = next((w for w in wins if w[1] <= t <= w[2]), None)
        src = "phone" if re.match(r"(PXL_|Screenshot_)", p.name) else "screen"
        if hit:
            part, w0, _, base = hit
            records.append({"path": p, "part": part, "offset": base + (t - w0).total_seconds(),
                            "source": src, "klass": "anchored", "taken": t})
        else:
            records.append({"path": p, "part": None, "offset": None,
                            "source": src, "klass": "associated", "taken": t})
    records.sort(key=lambda r: r["taken"])
    return records


# --- Staging & captioning ---

def stage(records, out_dir, config):
    """Copy images into the meeting directory, downscaled. Names carry part and
    offset so a reader can place them without opening anything."""
    shots = out_dir / "screenshots"
    shots.mkdir(parents=True, exist_ok=True)
    width = max_width(config)
    for i, r in enumerate(records, start=1):
        if r["klass"] == "anchored":
            o = int(r["offset"])
            base = f"p{r['part']}_{o // 60:03d}m{o % 60:02d}s_{r['source']}"
        else:
            base = f"assoc_{i:02d}_{r['taken']:%H%M}_{r['source']}"
        dest = shots / f"{base}.jpg"
        ok = False
        if shutil.which("sips"):
            ok = subprocess.run(["sips", "-Z", str(width), "-s", "format", "jpeg",
                                 str(r["path"]), "--out", str(dest)],
                                capture_output=True).returncode == 0
        if not ok:  # no sips (non-macOS) or conversion failed — keep the original
            dest = shots / f"{base}{r['path'].suffix.lower()}"
            try:
                shutil.copy2(r["path"], dest)
            except OSError as e:
                print(f"  Warning: could not stage {r['path'].name}: {e}", file=sys.stderr)
                continue
        r["staged"] = dest.name
    return [r for r in records if r.get("staged")]


def caption(records, out_dir, config):
    """One vision call per image, cached by staged filename.

    Captions rather than raw images are what reach the summarizer: it keeps the
    prompt small, and it makes what the model claimed to see an artifact the user
    can read and correct, instead of something buried inside a summary.
    """
    cache_path = out_dir / "captions.json"
    cache = {}
    if cache_path.exists():
        try:
            cache = json.loads(cache_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            cache = {}

    prompt_tpl = (
        "Read the image file screenshots/{name} and describe it factually in Czech, "
        "in 1-3 sentences. Include any legible identifiers, numbers, names, screen or "
        "column titles, and visible state (counts, filters, errors). Describe ONLY what "
        "is legible in the image — do not infer intent or what was decided. "
        "Reply with the description only."
    )
    for r in records:
        name = r["staged"]
        if name in cache:
            r["caption"] = cache[name]
            continue
        try:
            res = subprocess.run(
                ["claude", "-p", "--output-format", "text", "--allowedTools", "Read"],
                input=prompt_tpl.format(name=name), capture_output=True, text=True,
                timeout=180, cwd=str(out_dir),
            )
        except (subprocess.SubprocessError, OSError, FileNotFoundError) as e:
            print(f"  Warning: captioning failed for {name}: {e}", file=sys.stderr)
            continue
        if res.returncode != 0:
            print(f"  Warning: captioning failed for {name}: {res.stderr[:120]}", file=sys.stderr)
            continue
        text = res.stdout.strip()
        if text:
            r["caption"] = cache[name] = text
    try:
        cache_path.write_text(json.dumps(cache, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError:
        pass
    return records


# --- Output ---

def _stamp(seconds):
    return f"{int(seconds) // 60}:{int(seconds) % 60:02d}"


def visual_context(records, config):
    """The block handed to the summarizer, and written alongside the summary."""
    anchored = [r for r in records if r["klass"] == "anchored" and r.get("caption")]
    assoc = [r for r in records if r["klass"] != "anchored" and r.get("caption")]
    if not (anchored or assoc):
        return ""
    out = ["# Vizuální kontext",
           "",
           "Snímky pořízené během schůzky. Popisy vycházejí z toho, co je na snímku čitelné.",
           ""]
    if anchored:
        out.append("## Zařazené podle času")
        out.append("")
        for r in anchored:
            where = "mobil" if r["source"] == "phone" else "obrazovka"
            part = f"část {r['part']}, " if r["part"] else ""
            out.append(f"- **[{part}{_stamp(r['offset'])}] {where}** — {r['caption']}")
        out.append("")
    if assoc:
        out.append("## Bez přesného zařazení v čase")
        out.append("")
        out.append("Pořízeno v pauze nahrávání nebo krátce po schůzce — k obsahu je nelze "
                   "spolehlivě přiřadit podle času.")
        out.append("")
        for r in assoc:
            where = "mobil" if r["source"] == "phone" else "obrazovka"
            out.append(f"- **[{r['taken']:%H:%M}] {where}** — {r['caption']}")
        out.append("")
    return "\n".join(out)


SUMMARY_INSTRUCTIONS = (
    "Součástí vstupu je sekce '# Vizuální kontext' — popisy snímků pořízených během "
    "schůzky. Použij je k rozklíčování toho, na co mluvčí ukazují ('tady', 'tohle'), a "
    "ke správnému uvedení identifikátorů, čísel a názvů obrazovek či prvků. NEPOUŽÍVEJ "
    "je jako důkaz o tom, co bylo rozhodnuto — rozhodnutí plynou výhradně z toho, co "
    "bylo řečeno. Snímky bez přesného zařazení v čase neuváděj jako součást časové osy."
)


def interleave(transcript_text, records):
    """Put a marker at each anchored image's position, mirroring the bookmark
    markers the pipeline already emits for Plaud device marks."""
    anchored = sorted([r for r in records if r["klass"] == "anchored" and r.get("staged")],
                      key=lambda r: (r["part"] or 1, r["offset"]))
    if not anchored:
        return transcript_text
    pending = list(anchored)
    part = 1
    out = []
    for block in transcript_text.split("\n\n"):
        pm = re.match(r"--- Part (\d+)", block)
        if pm:
            part = int(pm.group(1))
        tm = re.match(r"\[(\d+):(\d\d)\]", block)
        if tm:
            t = int(tm.group(1)) * 60 + int(tm.group(2))
            for r in [x for x in pending if (x["part"] or 1) == part and x["offset"] <= t]:
                where = "mobil" if r["source"] == "phone" else "obrazovka"
                out.append(f"🖼 ——— SNÍMEK @ {_stamp(r['offset'])} ({where}) "
                           f"→ screenshots/{r['staged']} ———")
                pending.remove(r)
        out.append(block)
    for r in pending:  # anything after the last timestamped turn
        where = "mobil" if r["source"] == "phone" else "obrazovka"
        out.append(f"🖼 ——— SNÍMEK @ {_stamp(r['offset'])} ({where}) "
                   f"→ screenshots/{r['staged']} ———")
    return "\n\n".join(out)
