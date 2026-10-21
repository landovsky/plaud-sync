#!/usr/bin/env python3
"""autocapture: screen capture that only costs what the meeting actually pointed at.

Manual screenshots get forgotten exactly when they matter — someone says "tady"
and the screen has moved on by the time you think of it. This module captures
continuously during a meeting, but keeps the cost bounded with two filters, one
at each end:

  capture time — sample every display every few seconds, but keep a frame only
                 when the screen *changed and then settled*. A 32x20 grey
                 thumbnail is the whole comparison; no model is involved. A screen
                 already seen (tabbing back to it) is logged as a revisit, not
                 saved again. An hour of meeting is tens of scenes, not thousands.

  pipeline time — once the transcript exists, a scene is captioned only if a
                 turn with a pointing word ("tady", "tohle", "look at") was
                 spoken while it was on screen. Pointing words alone are too
                 common (~20% of turns, measured on three real meetings) to
                 select anything; pointing *at a scene* is the signal.

Capture is started by hand (`run`) or follows Wispr Flow (`watch`): a meeting is
live while its Meetings row has no endedAt. macOS notifications say when capture
starts, when the first frame proves Screen Recording permission works, and when
it stops — the screen gives no other sign that it is being recorded.

Frames stay in their own directory (never ~/Desktop, which visuals treats as
deliberate screenshots); only selected scenes are ever captioned or copied into
a transcript repo, and sessions older than `retention_days` are deleted.
"""

import argparse
import json
import os
import re
import shutil
import signal
import sqlite3
import subprocess
import sys
import time
import unicodedata
from datetime import datetime, timedelta, timezone
from pathlib import Path

STATE_DIR = Path.home() / ".cache" / "plaud-autocapture"
PID_FILE = STATE_DIR / "autocapture.pid"
STATUS_FILE = STATE_DIR / "status.json"
LOG_FILE = STATE_DIR / "autocapture.log"
APP_PATH = Path.home() / "Applications" / "Plaud Autocapture.app"
APP_ID = "local.plaud-sync.autocapture"

DEFAULTS = {
    "enabled": True,          # only consulted by the pipeline; frames exist only if you ran capture
    "dir": "~/Pictures/plaud-autocapture",
    "interval_s": 3,
    "change": 0.025,          # fraction of settled thumbnail cells that must differ for a new scene
    "moving_max": 0.15,       # a screen with more cells than this still moving has not settled
    "revisit": 0.01,          # at most this different from an earlier scene = the same screen again
    "cell_delta": 8,          # grey-level change (0-255) for a cell to count as changed
    "unsettled_keep_s": 30,   # a screen that never settles (video, long scroll) still yields a frame
    "max_hours": 3,           # a forgotten capture must not record the rest of the day
    "retention_days": 14,
    # Testing phase: keep every sampled frame, so detection can be replayed and
    # re-tuned offline (`replay`) instead of only judging what it chose to keep.
    # Off automatically after this date; set to null to stop sooner.
    "keep_raw_until": "2026-10-28",
    "max_captions": 30,
    "manual_gap_s": 20,       # a deliberate screenshot this close to a pointed-at scene wins
    "fresh_s": 60,            # pointing within this long after a screen change is the strongest signal
    "wispr_poll_s": 10,
    "deixis": [],             # extra pointing words/phrases, diacritics-free lowercase
    # Where each scene came from: the app and window in front on that display,
    # and for Chrome the tab title and URL (asks macOS for Automation once).
    "source": True,
    "source_urls": True,
}
# 96x60, not smaller: at 32x20 a whole new page of text in the same layout
# measured 0.000 change — text averages to the same grey. At 96x60 it is ~5%,
# highlighted rows ~4%. Calibrated on real frames, see _compare.
THUMB_W, THUMB_H = 96, 60
LIBRARY_SIZE = 60
SPEECH_WPS = 2.5              # words per second, for placing a word inside a turn             # recent scenes per display that a revisit can match
MAX_DISPLAYS = 4

# Diacritics-free, matched against normalized text. Bare "this"/"there" are left
# out: in English they are mostly grammar, not pointing.
DEIXIS = [
    "tady", "tadyhle", "tadyten", "tadyta", "tadyto", "tadytu", "tadle", "todle", "tudle",
    "tendle", "tamhle", "tamto", "tamten", "tamta", "tuhle", "tohle", "tohleto", "tahle",
    "tenhle", "tyhle", "tyhlety", "vidis", "vidite", "vidime", "koukni", "kouknete",
    "koukej", "koukejte", "podivej", "podivejte", "mrkni", "mrknete", "nahore", "vlevo",
    "vpravo", "na obrazovce", "na monitoru", "na screenu", "sem", "tudy", "tento", "tato",
    "toto", "tyto", "techto", "tomhle", "tomto", "timhle", "tuto", "tehle", "teto",
    "here", "this one", "that one", "these", "those", "look at", "you see", "can you see",
    "see this", "on the screen", "on screen", "up here", "down here", "over here",
    "over there", "right there",
]


def settings(config):
    s = dict(DEFAULTS)
    s.update(((config or {}).get("visuals") or {}).get("autocapture") or {})
    return s


def capture_root(config):
    return Path(settings(config)["dir"]).expanduser()


def _log(msg):
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    line = f"{datetime.now():%Y-%m-%d %H:%M:%S} {msg}"
    print(line, flush=True)
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass


def notify(title, message, sound=None):
    """macOS notification. Failure is silent — it is a courtesy, not a control.

    terminal-notifier first: osascript's notifications are posted as Script
    Editor, which macOS often has silenced — they were invisible in testing. An
    app launched via `open` gets a bare PATH, so Homebrew is checked by path.
    """
    tn = shutil.which("terminal-notifier") or next(
        (p for p in ("/opt/homebrew/bin/terminal-notifier", "/usr/local/bin/terminal-notifier")
         if os.path.exists(p)), None)
    if tn:
        cmd = [tn, "-title", title, "-message", message, "-group", "plaud-autocapture"]
        if sound:
            cmd += ["-sound", sound]
    else:
        script = (f"display notification {json.dumps(message, ensure_ascii=False)} "
                  f"with title {json.dumps(title, ensure_ascii=False)}")
        if sound:
            script += f" sound name {json.dumps(sound)}"
        cmd = ["osascript", "-e", script]
    try:
        subprocess.run(cmd, capture_output=True, timeout=10)
    except (subprocess.SubprocessError, OSError):
        pass


def _write_status(**fields):
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    fields["updated"] = time.time()
    tmp = STATUS_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(fields, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(STATUS_FILE)


# --- Frames ---

def _grab(tmp):
    """One screencapture call writes one file per display. Returns (paths, error)."""
    names = [tmp / f"d{i}.jpg" for i in range(1, MAX_DISPLAYS + 1)]
    for p in names:
        p.unlink(missing_ok=True)
    try:
        r = subprocess.run(["screencapture", "-x", "-t", "jpg", *map(str, names)],
                           capture_output=True, text=True, timeout=30)
    except (subprocess.SubprocessError, OSError) as e:
        return [], str(e)
    shots = [(i, p) for i, p in enumerate(names, start=1) if p.exists()]
    if not shots:
        return [], (r.stderr.strip() or f"screencapture exited {r.returncode}")
    return shots, None


def _thumb(img, tmp):
    """Grey 32x20 thumbnail as bytes — via sips, so no imaging library is needed."""
    bmp = tmp / f"{img.stem}.bmp"
    r = subprocess.run(["sips", "-z", str(THUMB_H), str(THUMB_W), "-s", "format", "bmp",
                        str(img), "--out", str(bmp)], capture_output=True)
    if r.returncode != 0 or not bmp.exists():
        return None
    data = bmp.read_bytes()
    off = int.from_bytes(data[10:14], "little")
    w = int.from_bytes(data[18:22], "little", signed=True)
    h = abs(int.from_bytes(data[22:26], "little", signed=True))
    step = int.from_bytes(data[28:30], "little") // 8
    if step < 3:
        return None
    row = (w * step + 3) & ~3
    out = bytearray()
    for y in range(h):
        base = off + y * row
        for x in range(w):
            b, g, r_ = data[base + x * step: base + x * step + 3]
            out.append((r_ * 299 + g * 587 + b * 114) // 1000)
    return bytes(out)


def _compare(sig, prev, ref, cell_delta):
    """(moving, changed) as fractions of all cells.

    moving  — cells that differ from the previous sample: video, a scroll in
              progress, a stream of terminal output.
    changed — cells that are *still* now but differ from ref.

    Size alone cannot separate a video tile (~3% of cells) from highlighted
    rows (~4%); behaviour can. Video keeps moving, a page you navigated to holds
    still — so moving cells are left out of the change.
    """
    n = len(sig)
    if ref is None or len(ref) != n:
        return 0.0, 1.0
    moving = changed = 0
    for i in range(n):
        if prev is not None and abs(sig[i] - prev[i]) > cell_delta:
            moving += 1
        elif abs(sig[i] - ref[i]) > cell_delta:
            changed += 1
    return moving / n, changed / n


_CURSOR_JXA = """ObjC.import("AppKit");
const m = $.NSEvent.mouseLocation, s = $.NSScreen.screens; let r = 0;
for (let i = 0; i < s.count; i++) { const f = s.objectAtIndex(i).frame;
  if (m.x >= f.origin.x && m.x < f.origin.x + f.size.width &&
      m.y >= f.origin.y && m.y < f.origin.y + f.size.height) r = i + 1; }
r"""

_WINDOWS_JXA = """ObjC.import("CoreGraphics"); ObjC.import("AppKit");
const a = ObjC.castRefToObject($.CGWindowListCopyWindowInfo(1 | 16, 0)); let named = 0, total = 0;
for (let i = 0; i < a.count; i++) { const d = a.objectAtIndex(i);
  if (d.objectForKey("kCGWindowLayer").js !== 0) continue; total++;
  if (d.objectForKey("kCGWindowName").js) named++; }
named + "/" + total"""


# Per display (numbered like screencapture), the frontmost normal window whose
# centre is on it — skipping small floating ones (a recorder widget over a
# browser) for the first window that covers 40% of the display. CG window
# bounds are top-left based, NSScreen frames bottom-left based. Chrome tabs are matched to windows by bounds, because
# Chrome reports a window's custom name, not its tab, as the title.
_SOURCES_JXA = """ObjC.import("CoreGraphics"); ObjC.import("AppKit");
const urls = %s, scr = $.NSScreen.screens, h0 = scr.objectAtIndex(0).frame.size.height;
const out = {front: $.NSWorkspace.sharedWorkspace.frontmostApplication.localizedName.js, displays: {}};
const wins = ObjC.castRefToObject($.CGWindowListCopyWindowInfo(1 | 16, 0));
for (let i = 0; i < wins.count; i++) { const w = wins.objectAtIndex(i);
  if (w.objectForKey("kCGWindowLayer").js !== 0) continue;
  const b = w.objectForKey("kCGWindowBounds"), x = b.objectForKey("X").js, y = b.objectForKey("Y").js,
        bw = b.objectForKey("Width").js, bh = b.objectForKey("Height").js;
  if (bw < 100 || bh < 100) continue;
  const cx = x + bw / 2, cy = y + bh / 2;
  for (let k = 0; k < scr.count; k++) { const f = scr.objectAtIndex(k).frame, top = h0 - f.origin.y - f.size.height;
    const d = out.displays[k + 1];
    if (cx >= f.origin.x && cx < f.origin.x + f.size.width && cy >= top && cy < top + f.size.height
        && (!d || (d.small && bw * bh >= 0.4 * f.size.width * f.size.height))) {
      const n = w.objectForKey("kCGWindowName");
      out.displays[k + 1] = {app: w.objectForKey("kCGWindowOwnerName").js, window: n.js || "", b: [x, y, bw, bh],
                             small: bw * bh < 0.4 * f.size.width * f.size.height}; } } }
if (urls && Object.values(out.displays).some(d => d.app === "Google Chrome")) {
  try { const cw = Application("Google Chrome").windows();
    for (const d of Object.values(out.displays)) { if (d.app !== "Google Chrome") continue;
      for (const w of cw) { const r = w.bounds();
        if (Math.abs(r.x - d.b[0]) > 4 || Math.abs(r.y - d.b[1]) > 4 || Math.abs(r.width - d.b[2]) > 4) continue;
        if (w.mode() === "incognito") { d.tab = "(incognito)"; break; }
        const t = w.activeTab(); d.tab = t.title(); d.url = t.url(); break; } } } catch (e) {} }
for (const d of Object.values(out.displays)) { delete d.b; delete d.small; }
JSON.stringify(out)"""


def _jxa(script):
    try:
        return subprocess.run(["osascript", "-l", "JavaScript", "-e", script], capture_output=True,
                              text=True, timeout=10).stdout.strip()
    except (subprocess.SubprocessError, OSError):
        return ""


def _cursor_display():
    """Which display the mouse is on (1 = main, as screencapture numbers them),
    or None. Where you point is a better attention signal than which screen
    changed last — a terminal streaming output changes constantly."""
    out = _jxa(_CURSOR_JXA)
    return int(out) if out.isdigit() and int(out) > 0 else None


def _sources(urls=True):
    """{display: {app, window, tab?, url?}} for what is in front on each display,
    plus the frontmost app under "front". {} when it can't tell. URLs lose their
    query and fragment: those are where tokens and session ids live."""
    try:
        out = json.loads(_jxa(_SOURCES_JXA % ("true" if urls else "false")) or "{}")
    except json.JSONDecodeError:
        return {}
    for d in out.get("displays", {}).values():
        if d.get("url"):
            d["url"] = re.split(r"[?#]", d["url"], maxsplit=1)[0]
    return out


def _page(src):
    """What page a source shows, for telling look-alike screens apart: the URL
    when there is one (a Chrome window's title is its custom name, the same for
    every tab), else app + window title. None when unknown."""
    if not src:
        return None
    return f"{src.get('app')}|{src.get('url') or src.get('tab') or src.get('window')}"


def _permission_looks_missing():
    """Without Screen Recording, screencapture still succeeds — returning only the
    wallpaper — so a successful capture proves nothing. Ask macOS in-process
    first: TCC charges the grant to this process (the prompt names Python), the
    same one screencapture's frames are judged by. The window-title probe ran in
    an osascript child and answered "fine" through a whole blind meeting, so it
    is only the fallback. None when neither can tell."""
    try:
        import ctypes
        cg = ctypes.CDLL("/System/Library/Frameworks/CoreGraphics.framework/CoreGraphics")
        cg.CGPreflightScreenCaptureAccess.restype = ctypes.c_bool
        return not cg.CGPreflightScreenCaptureAccess()
    except (OSError, AttributeError):
        pass
    m = re.fullmatch(r"(\d+)/(\d+)", _jxa(_WINDOWS_JXA))
    if not m or int(m.group(2)) == 0:
        return None
    return int(m.group(1)) == 0


def _locked():
    try:
        out = subprocess.run(["ioreg", "-n", "Root", "-d1"], capture_output=True,
                             text=True, timeout=10).stdout
    except (subprocess.SubprocessError, OSError):
        return False
    return '"CGSSessionScreenIsLocked"=Yes' in out


def _event(timeline, **fields):
    fields.setdefault("t", time.time())
    with open(timeline, "a", encoding="utf-8") as f:
        f.write(json.dumps(fields, ensure_ascii=False) + "\n")


def prune(config):
    """Delete sessions older than retention_days."""
    s = settings(config)
    root = capture_root(config)
    if not root.is_dir():
        return 0
    cutoff = time.time() - s["retention_days"] * 86400
    removed = 0
    for d in root.iterdir():
        if d.is_dir() and (d / "timeline.jsonl").exists() and d.stat().st_mtime < cutoff:
            shutil.rmtree(d, ignore_errors=True)
            removed += 1
    return removed


def keep_raw(config):
    until = settings(config).get("keep_raw_until")
    return bool(until) and datetime.now().strftime("%Y-%m-%d") <= str(until)


class SceneTracker:
    """The scene decision for every display, shared by live capture and replay —
    so thresholds tuned by replaying raw frames are exactly what capture uses.

    A frame becomes a scene when its settled cells differ from the current scene
    *and* the screen has stopped moving — so the finished screen is kept, not
    the blur of a scroll or a tab switch.
    """

    def __init__(self, s):
        self.s = s
        self.displays = {}
        self.saved = self.revisits = 0

    def reset(self):
        """After a lock, re-log whatever is on screen once it is back."""
        for d in self.displays.values():
            d["scene"] = None

    def observe(self, idx, sig, t0, where=None):
        """Returns None, or (event_t, scene_name, is_new).

        where() — called only when a scene is about to be logged — names the
        page on screen (app + URL, or app + window). Two orders' ZDV tabs share
        a layout and differ in a line of small text, which pixels alone matched
        as a revisit; a different page name rules that out."""
        s = self.s
        d = self.displays.setdefault(idx, {"scene": None, "prev": None, "prev_t": t0,
                                           "since": None, "library": []})
        moving, change = _compare(sig, d["prev"], d["scene"], s["cell_delta"])
        changed = change >= s["change"]
        stable = d["prev"] is not None and moving < s["moving_max"]
        overdue = d["since"] is not None and t0 - d["since"] >= s["unsettled_keep_s"]
        result = None
        if changed and (stable or overdue):
            # Strict on purpose: a false revisit loses a screen for good (two
            # white pages with a sidebar once matched at a loose threshold), a
            # missed one only costs a duplicate file.
            ident = where() if where else None
            name = next((n for lib_sig, n, lib_ident in d["library"]
                         if (ident is None or lib_ident is None or ident == lib_ident)
                         and _compare(sig, d["prev"], lib_sig, s["cell_delta"])[1] < s["revisit"]), None)
            new = name is None
            if new:
                self.saved += 1
                name = f"scene_{self.saved:04d}_d{idx}.jpg"
                d["library"] = (d["library"] + [(sig, name, ident)])[-LIBRARY_SIZE:]
            else:
                self.revisits += 1
            # Stamp the scene with when it first appeared, not when it was confirmed.
            result = (d["prev_t"] if stable else t0, name, new)
            d["scene"], d["since"] = sig, None
        elif changed:
            d["since"] = d["since"] or t0
        else:
            d["since"] = None
        d["prev"], d["prev_t"] = sig, t0
        return result


def _shrink(src, dest, width, quality=None):
    cmd = ["sips", "-Z", str(width)]
    if quality:
        cmd += ["-s", "format", "jpeg", "-s", "formatOptions", str(quality)]
    if subprocess.run(cmd + [str(src), "--out", str(dest)], capture_output=True).returncode != 0:
        shutil.copy2(src, dest)


def capture(config, should_continue, reason):
    """Capture until should_continue() is false or max_hours passes.

    A frame becomes a scene when it differs from the current scene *and* the next
    sample agrees with it — so the settled screen is kept, not the blur of a
    scroll or a tab switch. Returns (scenes_saved, revisits).
    """
    s = settings(config)
    width = int(((config or {}).get("visuals") or {}).get("max_width", 1600))
    root = capture_root(config)
    pruned = prune(config)
    session = root / datetime.now().strftime("%Y-%m-%d_%H%M%S")
    tmp = session / ".tmp"
    tmp.mkdir(parents=True, exist_ok=True)
    timeline = session / "timeline.jsonl"
    raw_dir = session / "raw" if keep_raw(config) else None
    if raw_dir:
        raw_dir.mkdir()
    _event(timeline, event="start", reason=reason, raw=bool(raw_dir))
    _log(f"capture started ({reason}) -> {session}" + (f"; pruned {pruned} old session(s)" if pruned else ""))
    notify("Autocapture ON", f"Capturing the screen ({reason}). Auto-stops after {s['max_hours']}h.",
           sound="Glass")
    tracker = SceneTracker(s)
    started = time.time()
    locked = False
    focus = None
    error_notified = False
    blind, blind_checked, blind_notified = None, 0.0, 0.0  # None: first check always logs
    stop_reason = "stopped"
    try:
        while True:
            if not should_continue():
                stop_reason = "trigger ended"
                break
            t0 = time.time()
            if t0 - started > s["max_hours"] * 3600:
                stop_reason = f"max_hours ({s['max_hours']}h) reached"
                break
            if _locked():
                if not locked:
                    _event(timeline, event="lock")
                    tracker.reset()
                    locked = True
                time.sleep(s["interval_s"])
                continue
            locked = False

            # Re-checked every minute, not just at start: a 2-hour meeting was
            # once lost to wallpaper because the grant went away and nothing
            # said so. Blind stretches are marked so select() skips their scenes.
            if t0 - blind_checked >= 60:
                blind_checked = t0
                missing = _permission_looks_missing() is True
                if missing != blind:
                    blind = missing
                    _event(timeline, t=t0, event="blind" if blind else "sight")
                    _log("Screen Recording permission " + ("missing — frames show only the wallpaper"
                                                           if blind else "OK"))
                if blind and t0 - blind_notified >= 600:
                    blind_notified = t0
                    notify("Autocapture is blind",
                           "No Screen Recording permission: frames show only the wallpaper. "
                           "Allow it in Privacy & Security → Screen & System Audio Recording, "
                           "then restart autocapture.", sound="Basso")

            shots, err = _grab(tmp)
            if err:
                _log(f"capture error: {err}")
                if not error_notified:
                    notify("Autocapture: no frames",
                           "screencapture failed — grant Screen Recording to the app running "
                           f"autocapture. ({err[:80]})", sound="Basso")
                    error_notified = True
                _write_status(state="error", error=err, session=str(session), reason=reason,
                              started=started, saved=tracker.saved, revisits=tracker.revisits)
                time.sleep(s["interval_s"])
                continue

            cur = _cursor_display() if len(shots) > 1 else None
            if cur and cur != focus:
                _event(timeline, t=t0, focus=cur)
                focus = cur

            sources = {}  # looked up once per sample, only when a display has a scene

            def source_of(idx):
                if not s["source"]:
                    return None
                if "front" not in sources:
                    sources.update(_sources(s["source_urls"]) or {"front": None})
                return (sources.get("displays") or {}).get(str(idx))

            for idx, path in shots:
                if raw_dir:
                    _shrink(path, raw_dir / f"{t0:.1f}_d{idx}.jpg", width, quality=70)
                sig = _thumb(path, tmp)
                if sig is None:
                    continue
                hit = tracker.observe(idx, sig, t0, where=lambda: _page(source_of(idx)))
                if not hit:
                    continue
                t_event, name, new = hit
                if new:
                    _shrink(path, session / name, width)
                    if tracker.saved == 1:
                        notify("Autocapture working", "First scene saved.")
                _event(timeline, t=t_event, display=idx, scene=name)
                src = source_of(idx)
                if src:
                    _event(timeline, t=t_event, display=idx, source=dict(src, front=sources.get("front")))

            _write_status(state="capturing", session=str(session), reason=reason, started=started,
                          saved=tracker.saved, revisits=tracker.revisits, displays=len(shots),
                          focus=focus, raw=bool(raw_dir))
            time.sleep(max(0.0, s["interval_s"] - (time.time() - t0)))
    finally:
        _event(timeline, event="stop", why=stop_reason)
        shutil.rmtree(tmp, ignore_errors=True)
        mins = int((time.time() - started) // 60)
        saved, revisits = tracker.saved, tracker.revisits
        _log(f"capture stopped: {stop_reason}; {saved} scene(s), {revisits} revisit(s), {mins} min")
        notify("Autocapture OFF", f"{stop_reason} — {saved} scenes, {revisits} revisits in {mins} min.")
        _write_status(state="idle", last_session=str(session), last_stop=stop_reason,
                      saved=saved, revisits=revisits)
    return saved, revisits


def replay(session, overrides, config):
    """Re-run scene detection over a session's raw frames with (optionally)
    different thresholds. Writes <session>/replay/ — scene copies plus a
    timeline — and returns its path; the original capture is untouched."""
    s = settings(config)
    s.update(overrides)
    raw_dir = session / "raw"
    frames = {}
    for p in raw_dir.glob("*_d*.jpg"):
        m = re.fullmatch(r"([\d.]+)_d(\d+)\.jpg", p.name)
        if m:
            frames.setdefault(float(m.group(1)), []).append((int(m.group(2)), p))
    out = session / "replay"
    shutil.rmtree(out, ignore_errors=True)
    tmp = out / ".tmp"
    tmp.mkdir(parents=True)
    tracker = SceneTracker(s)
    events = []
    for line in (session / "timeline.jsonl").read_text(encoding="utf-8").splitlines():
        e = json.loads(line)
        if "scene" not in e:  # keep start/stop/lock/focus as captured
            events.append(e)
    lock_times = sorted(e["t"] for e in events if e.get("event") == "lock")
    for t0 in sorted(frames):
        while lock_times and lock_times[0] <= t0:
            lock_times.pop(0)
            tracker.reset()
        for idx, p in sorted(frames[t0]):
            sig = _thumb(p, tmp)
            hit = sig and tracker.observe(idx, sig, t0)
            if hit:
                t_event, name, new = hit
                if new:
                    shutil.copy2(p, out / name)
                events.append({"t": t_event, "display": idx, "scene": name})
    shutil.rmtree(tmp, ignore_errors=True)
    events.sort(key=lambda e: e.get("t", 0))
    (out / "timeline.jsonl").write_text("".join(json.dumps(e) + "\n" for e in events), encoding="utf-8")
    (out / "settings.json").write_text(json.dumps(s, indent=2), encoding="utf-8")
    print(f"{len(frames)} samples -> {tracker.saved} scene(s), {tracker.revisits} revisit(s): {out}")
    for e in events:
        if "scene" in e:
            print(f"  {datetime.fromtimestamp(e['t']):%H:%M:%S}  d{e['display']}  {e['scene']}")
    return out


# --- Wispr Flow trigger ---

def wispr_live_meeting(config, max_hours):
    """The Wispr meeting being recorded right now, as {id, created_ms}, or None.

    Wispr sets Meetings.endedAt when a meeting stops; while it runs it is NULL.
    Rows older than max_hours are ignored, so a crashed meeting that never got
    an endedAt cannot keep capture on forever.
    """
    import wispr_flow
    db = wispr_flow.wispr_db_path(config)
    if not db.exists():
        return None
    try:
        conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=5)
        try:
            row = conn.execute(
                "SELECT id, createdAt FROM Meetings WHERE endedAt IS NULL AND isDeleted = 0 "
                "ORDER BY createdAt DESC LIMIT 1").fetchone()
        finally:
            conn.close()
    except sqlite3.Error as e:
        _log(f"wispr read failed: {e}")
        return None
    if not row:
        return None
    created = wispr_flow._parse_created(row[1])
    if not created or time.time() * 1000 - created > max_hours * 3600 * 1000:
        return None
    return {"id": row[0], "created_ms": created}


def _wispr_files(config, meeting_id):
    """Sizes of the meeting's growing files — logged so a real meeting shows
    whether pauses are detectable from them (not used for decisions yet)."""
    import wispr_flow
    d = wispr_flow.wispr_dir(config) / "meetings" / meeting_id
    out = {}
    for name in ("live.ndjson", "upload.ogg"):
        p = d / name
        if p.exists():
            st = p.stat()
            out[name] = {"size": st.st_size, "age_s": round(time.time() - st.st_mtime)}
    return out


def watch(config, stopping):
    """Follow Wispr Flow: capture while a meeting is live, idle otherwise."""
    s = settings(config)
    done = set()  # meetings that hit max_hours — don't restart them
    _log("watching Wispr Flow for meetings")
    notify("Autocapture armed", "Will capture the screen while a Wispr Flow meeting is recording.")
    _write_status(state="watching")
    while not stopping():
        m = wispr_live_meeting(config, s["max_hours"])
        if m and m["id"] not in done:
            last = {"t": 0.0, "live": True, "files_t": 0.0}

            def still_live():
                if stopping():
                    return False
                now = time.time()
                if now - last["t"] >= s["wispr_poll_s"]:
                    cur = wispr_live_meeting(config, s["max_hours"])
                    last["live"] = bool(cur and cur["id"] == m["id"])
                    last["t"] = now
                if now - last["files_t"] >= 60:
                    _log(f"wispr {m['id'][:8]} files: {_wispr_files(config, m['id'])}")
                    last["files_t"] = now
                return last["live"]

            capture(config, still_live, reason=f"Wispr meeting {m['id'][:8]}")
            if wispr_live_meeting(config, s["max_hours"]):
                done.add(m["id"])  # stopped by max_hours or a signal while still live
            _write_status(state="watching")
        for _ in range(int(s["wispr_poll_s"])):  # in steps, so `stop` is prompt
            if stopping():
                break
            time.sleep(1)
    _write_status(state="idle")


# --- Selection (pipeline time) ---

def _norm(s):
    s = unicodedata.normalize("NFKD", s.lower())
    return "".join(c for c in s if not unicodedata.combining(c))


def _deixis_re(config):
    words = DEIXIS + [_norm(w) for w in settings(config).get("deixis") or []]
    return re.compile(r"\b(" + "|".join(re.escape(w) for w in words) + r")\b")


def _turns(transcript):
    """[(part, offset_s, text)] from `[MM:SS] speaker: text` blocks, honouring
    the `--- Part N` headers of a merged session."""
    out = []
    part = 1
    for block in transcript.split("\n\n"):
        pm = re.match(r"--- Part (\d+)", block)
        if pm:
            part = int(pm.group(1))
            continue
        tm = re.match(r"\[(?:(\d+):)?(\d+):(\d\d)\]", block)
        if tm:
            h, m, sec = int(tm.group(1) or 0), int(tm.group(2)), int(tm.group(3))
            out.append((part, h * 3600 + m * 60 + sec, block))
    return out


def _intervals(root, lo, hi):
    """([(display, t0, t1, path, source)], [(t, display)]) — when each scene was on
    screen, and which display had the mouse, from every session timeline
    overlapping [lo, hi] (epoch seconds). Lock and stop end every display's
    scene: capture off means nothing is known to be on screen. A source event
    belongs to the scene that started on its display just before it (a replay
    with other thresholds can move scene starts, so only within a few seconds)."""
    out, focus = [], []
    if not root.is_dir():
        return out, focus
    for sess in sorted(root.iterdir()):
        tl = sess / "timeline.jsonl"
        if not tl.exists() or tl.stat().st_mtime < lo:
            continue
        events = []
        with open(tl, encoding="utf-8") as f:
            for line in f:
                try:
                    events.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
        events.sort(key=lambda e: e.get("t", 0))
        if not events or events[0].get("t", 0) > hi:
            continue
        cur = {}
        blind = False  # without Screen Recording every scene is the wallpaper
        for e in events:
            t = e.get("t", 0)
            if e.get("event") in ("blind", "sight"):
                blind = e["event"] == "blind"
            if "focus" in e:
                focus.append((t, e["focus"]))
            elif "scene" in e:
                prev = cur.pop(e["display"], None)
                if prev:
                    out.append((e["display"], prev[0], t, prev[1], prev[2]))
                if not blind:
                    cur[e["display"]] = [t, sess / e["scene"], None]
            elif "source" in e:
                c = cur.get(e["display"])
                if c and 0 <= t - c[0] <= 6:
                    c[2] = e["source"]
            elif e.get("event") in ("lock", "stop", "start", "blind"):
                for disp, (t0, p, src) in cur.items():
                    out.append((disp, t0, t, p, src))
                cur = {}
        end = tl.stat().st_mtime  # a crashed session has no stop event
        for disp, (t0, p, src) in cur.items():
            out.append((disp, t0, max(end, t0), p, src))
    focus.sort()
    return [iv for iv in out if iv[2] >= lo and iv[1] <= hi and iv[3].exists()], focus


def _wall(wins, part, offset):
    """Recording offset -> epoch seconds, via the recording windows visuals built."""
    for p, w0, w1, base in wins:
        # Slack both ends: transcript stamps are whole seconds, so the first
        # turn can read 0:09 for a segment that starts at 9.7s.
        if p == part and base - 2 <= offset <= base + (w1 - w0).total_seconds() + 5:
            return w0.timestamp() + (offset - base)
    return None


def select(config, transcript, wins, manual_times, tz):
    """Pick the auto-captured scenes worth captioning.

    For each turn containing a pointing word, the scene on screen during that
    turn gets a hit. With several displays, the one with the mouse wins, else
    the one that changed most recently. A hit
    within fresh_s of the scene appearing counts triple. Each scene is captioned
    once, placed at its first reference; the top max_captions by score survive.

    Returns (records, report): records in visuals' format, and every candidate
    with its score and fate, so the cap can be judged after a real meeting.
    """
    s = settings(config)
    if not s.get("enabled", True) or not transcript or not wins:
        return [], None
    lo = min(w[1] for w in wins).timestamp()
    hi = max(w[2] for w in wins).timestamp()
    intervals, focus = _intervals(capture_root(config), lo, hi)
    if not intervals:
        return [], None
    deixis = _deixis_re(config)
    turns = _turns(transcript)

    scenes = {}
    pointing_turns = 0
    for i, (part, off, text) in enumerate(turns):
        body = _norm(re.sub(r"^\[[\d:]+\]\s*[^:]*:\s*", "", text))
        matches = list(deixis.finditer(body))
        if not matches:
            continue
        pointing_turns += 1
        w0 = _wall(wins, part, off)
        if w0 is None:
            continue
        # When in the turn was the word said? People say "tady…" and then move
        # on, so the scene on screen *at the word* is the referent — not the
        # latest one before the next turn. Position in the text times a typical
        # speaking rate places it well enough at a 3s sampling interval.
        nxt = next((o for p, o, _ in turns[i + 1:] if p == part and o > off), None)
        duration = len(body.split()) / SPEECH_WPS
        if nxt:
            duration = min(duration, nxt - off)
        hit = {}
        for m in matches:
            tp = w0 + duration * m.start() / max(len(body), 1)
            # A scene is stamped when first sampled, up to one interval late.
            live = [iv for iv in intervals if iv[1] <= tp + s["interval_s"] and iv[2] > tp]
            if not live:
                continue
            # The display with the mouse is where attention is; without that, the
            # one that changed most recently.
            at = next((d for t, d in reversed(focus) if t <= tp), None)
            on_focus = [iv for iv in live if iv[0] == at]
            disp, t0, _, path, src = max(on_focus or live, key=lambda iv: iv[1])
            hit.setdefault(str(path), (disp, t0, path, tp, src))
        for key, (disp, t0, path, tp, src) in hit.items():  # one hit per scene per turn
            sc = scenes.setdefault(key, {"path": path, "display": disp, "hits": 0, "fresh": 0,
                                         "part": part, "offset": float(off), "wall": tp,
                                         "origin": src})
            sc["hits"] += 1
            if tp - t0 <= s["fresh_s"]:
                sc["fresh"] += 1

    cands = list(scenes.values())
    for sc in cands:
        sc["score"] = sc["hits"] + 2 * sc["fresh"]
        sc["fate"] = ("manual-nearby" if any(abs(sc["wall"] - m) <= s["manual_gap_s"] for m in manual_times)
                      else None)
    ranked = sorted((c for c in cands if not c["fate"]), key=lambda c: (-c["score"], c["wall"]))
    for i, c in enumerate(ranked):
        c["fate"] = "selected" if i < s["max_captions"] else "over-cap"

    records = [{"path": c["path"], "part": c["part"], "offset": c["offset"], "source": "auto",
                "klass": "anchored", "taken": datetime.fromtimestamp(c["wall"], tz),
                "origin": c["origin"]}
               for c in cands if c["fate"] == "selected"]
    report = {
        "scenes_on_screen": len({str(iv[3]) for iv in intervals}),
        "pointing_turns": pointing_turns,
        "turns": len(turns),
        "pointed_at": len(cands),
        "selected": len(records),
        "over_cap": sum(1 for c in cands if c["fate"] == "over-cap"),
        "manual_nearby": sum(1 for c in cands if c["fate"] == "manual-nearby"),
        "max_captions": s["max_captions"],
        "candidates": sorted(({"scene": c["path"].name, "session": c["path"].parent.name,
                               "display": c["display"], "part": c["part"],
                               "at": f"{int(c['offset']) // 60}:{int(c['offset']) % 60:02d}",
                               "hits": c["hits"], "fresh": c["fresh"], "score": c["score"],
                               "fate": c["fate"], "origin": c["origin"]} for c in cands), key=lambda c: c["score"], reverse=True),
    }
    return records, report


# --- App bundle ---

_LAUNCHER_C = r"""
#include <signal.h>
#include <spawn.h>
#include <stdlib.h>
#include <sys/wait.h>
extern char **environ;
static pid_t child;
static void forward(int sig) { if (child > 0) kill(child, sig); }
int main(int argc, char **argv) {
    char *args[argc + 3];
    int n = 0;
    args[n++] = PYTHON;
    args[n++] = SCRIPT;
    if (argc < 2) args[n++] = "watch";
    for (int i = 1; i < argc; i++) args[n++] = argv[i];
    args[n] = NULL;
    signal(SIGTERM, forward);
    signal(SIGINT, forward);
    if (posix_spawn(&child, PYTHON, NULL, NULL, args, environ) != 0) return 127;
    int status;
    while (waitpid(child, &status, 0) < 0) {}
    return WIFEXITED(status) ? WEXITSTATUS(status) : 1;
}
"""


def _compile_launcher(dest):
    """Build the app's main executable: spawns this script under the current
    Python and waits, forwarding TERM/INT. False when no compiler is around."""
    def c_str(p):
        return '"' + str(p).replace("\\", "\\\\").replace('"', '\\"') + '"'
    src = dest.with_suffix(".c")
    src.write_text(_LAUNCHER_C, encoding="utf-8")
    try:
        ok = subprocess.run(["cc", "-O2", "-o", str(dest), str(src),
                             f"-DPYTHON={c_str(sys.executable)}",
                             f"-DSCRIPT={c_str(Path(__file__).resolve())}"],
                            capture_output=True).returncode == 0
    except OSError:
        ok = False
    src.unlink(missing_ok=True)
    return ok


def install_app():
    """Wrap capture in a tiny .app so macOS asks for — and grants — Screen
    Recording to *it*, by name.

    macOS charges screen access to the "responsible" app of the process tree.
    Under tmux that is the tmux server, which has no app name: the prompt says
    "unknown", and allowing it would hand the screen to everything in tmux.
    Launched via `open`, this bundle is its own responsible app.
    """
    contents = APP_PATH / "Contents"
    (contents / "MacOS").mkdir(parents=True, exist_ok=True)
    (contents / "Info.plist").write_text(f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>CFBundleName</key><string>Plaud Autocapture</string>
  <key>CFBundleDisplayName</key><string>Plaud Autocapture</string>
  <key>CFBundleIdentifier</key><string>{APP_ID}</string>
  <key>CFBundleExecutable</key><string>autocapture</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleVersion</key><string>1</string>
  <key>LSUIElement</key><true/>
</dict></plist>
""", encoding="utf-8")
    launcher = contents / "MacOS" / "autocapture"
    # Python runs as a child, not via exec: exec'ing turns this process into
    # Homebrew's Python.app, and macOS then asks for (and grants to) "Python" —
    # every Python program — instead of this app. A bash launcher was not
    # enough either: the prompt still named python3.15, and the grant covered
    # every script that interpreter runs. A signed Mach-O main executable that
    # spawns Python without disclaiming responsibility keeps the app the
    # responsible process.
    if not _compile_launcher(launcher):
        launcher.write_text("#!/bin/bash\n"
                            f'"{sys.executable}" "{Path(__file__).resolve()}" "${{@:-watch}}" &\n'
                            'child=$!\n'
                            'trap \'kill -TERM "$child" 2>/dev/null\' TERM INT\n'
                            'wait "$child"; wait "$child"\n',
                            encoding="utf-8")
        print("no C compiler — installed the shell launcher; macOS may charge the grant to Python")
    launcher.chmod(0o755)
    # Ad-hoc signature: TCC keys the grant to a stable identity, not just a path.
    subprocess.run(["codesign", "--force", "--deep", "-s", "-", str(APP_PATH)],
                   capture_output=True)
    print(f"installed {APP_PATH}")


# --- CLI ---

def _running_pid():
    try:
        pid = int(PID_FILE.read_text().strip())
        os.kill(pid, 0)
        return pid
    except (OSError, ValueError):
        return None


def _foreground(mode, config, hours):
    if _running_pid():
        sys.exit(f"autocapture already running (pid {_running_pid()}) — `stop` it first")
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    PID_FILE.write_text(str(os.getpid()))
    flag = {"stop": False}

    def on_signal(*_):
        flag["stop"] = True
    signal.signal(signal.SIGTERM, on_signal)
    signal.signal(signal.SIGINT, on_signal)
    try:
        if mode == "watch":
            watch(config, lambda: flag["stop"])
        else:
            if hours:
                config.setdefault("visuals", {}).setdefault("autocapture", {})["max_hours"] = hours
            capture(config, lambda: not flag["stop"], reason="manual")
    finally:
        PID_FILE.unlink(missing_ok=True)


def main():
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import plaud_transcribe as pt
    ap = argparse.ArgumentParser(description="Continuous, change-driven screen capture for meeting visuals.")
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run", help="capture now, in the foreground, until Ctrl-C or max_hours")
    r.add_argument("--hours", type=float, help="override max_hours for this run")
    sub.add_parser("watch", help="capture automatically while a Wispr Flow meeting is recording")
    st = sub.add_parser("start", help="like run/watch, but in the background")
    st.add_argument("--watch", action="store_true", help="follow Wispr Flow instead of capturing now")
    st.add_argument("--hours", type=float)
    sub.add_parser("stop", help="stop a background capture/watch")
    sub.add_parser("status", help="show what autocapture is doing")
    sub.add_parser("install-app", help="build ~/Applications/Plaud Autocapture.app (named permission prompt)")
    rp = sub.add_parser("replay", help="re-run scene detection over a session's raw frames")
    rp.add_argument("session", help="session directory (or its name under the capture dir)")
    rp.add_argument("--set", action="append", default=[], metavar="KEY=VALUE",
                    help="override a detection setting, e.g. --set change=0.04")
    args = ap.parse_args()
    config = pt.load_config()

    if args.cmd in ("run", "watch"):
        _foreground(args.cmd, config, getattr(args, "hours", None))
    elif args.cmd == "start":
        if _running_pid():
            sys.exit(f"autocapture already running (pid {_running_pid()})")
        sub_args = ["watch"] if args.watch else ["run"]
        if args.hours and not args.watch:
            sub_args += ["--hours", str(args.hours)]
        if APP_PATH.exists():  # screen access is then asked for by, and granted to, the app
            subprocess.run(["open", "-g", "-n", str(APP_PATH), "--args", *sub_args], check=True)
            print(f"started via {APP_PATH.name} — log: {LOG_FILE}")
            return
        cmd = [sys.executable, str(Path(__file__).resolve()), *sub_args]
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        with open(LOG_FILE, "a") as log:  # _log already writes the file; only tracebacks go here
            subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=log, stdin=subprocess.DEVNULL,
                             start_new_session=True)
        print(f"started in the background — log: {LOG_FILE}")
    elif args.cmd == "install-app":
        install_app()
    elif args.cmd == "replay":
        session = Path(args.session).expanduser()
        if not session.is_dir():
            session = capture_root(config) / args.session
        if not (session / "raw").is_dir():
            sys.exit(f"no raw frames in {session} — only sessions captured with keep_raw can be replayed")
        overrides = {}
        for kv in args.set:
            k, _, v = kv.partition("=")
            overrides[k] = float(v) if re.fullmatch(r"[\d.]+", v) else v
        replay(session, overrides, config)
    elif args.cmd == "stop":
        pid = _running_pid()
        if not pid:
            print("not running")
            return
        os.kill(pid, signal.SIGTERM)
        print(f"stopping pid {pid}")
    elif args.cmd == "status":
        pid = _running_pid()
        st = json.loads(STATUS_FILE.read_text()) if STATUS_FILE.exists() else {}
        print(f"process: {'running, pid ' + str(pid) if pid else 'not running'}")
        if st:
            age = int(time.time() - st.get("updated", 0))
            print(f"state:   {st.get('state')} (updated {age}s ago)")
            for k in ("reason", "session", "last_session", "saved", "revisits", "displays",
                      "last_stop", "error"):
                if k in st:
                    print(f"{k + ':':9}{st[k]}")


if __name__ == "__main__":
    main()
