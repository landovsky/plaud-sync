import json
import os
import sqlite3
import time
from datetime import datetime, timedelta, timezone

import autocapture


def _store(tmp_path, ended_ms, live_lines, live_age_s=0):
    """A minimal Wispr Flow data dir with one meeting started 30 min ago."""
    mid = "9bf98b25-0000-0000-0000-000000000000"
    created = (datetime.now(timezone.utc) - timedelta(minutes=30)).strftime("%Y-%m-%d %H:%M:%S.000 +00:00")
    conn = sqlite3.connect(tmp_path / "flow.sqlite")
    conn.execute("CREATE TABLE Meetings (id TEXT, createdAt TEXT, endedAt INTEGER, isDeleted INTEGER)")
    conn.execute("INSERT INTO Meetings VALUES (?, ?, ?, 0)", (mid, created, ended_ms))
    conn.commit()
    conn.close()
    d = tmp_path / "meetings" / mid
    d.mkdir(parents=True)
    live = d / "live.ndjson"
    live.write_text("\n".join(json.dumps(o) for o in [{"meta": {"v": 3}}] + live_lines) + "\n")
    if live_age_s:
        t = time.time() - live_age_s
        os.utime(live, (t, t))
    return {"wispr": {"data_dir": str(tmp_path)}}, mid


def _said(segment, text):
    return {"id": text, "text": text, "segment": segment, "startRecordingMs": 1000}


PAUSED = {"id": "p", "marker": "paused", "segment": 0, "epochMs": 1}
RESUMED = {"id": "r", "marker": "resumed", "segment": 1, "epochMs": 2}


class TestResumedMeetingKeepsCapturing:
    """Wispr sets endedAt at a pause and does not clear it on resume. The watcher
    trusted endedAt alone, so on 2026-11-02 the 80 minutes after a 23-minute pause
    of "HH: školení obchod konstrukce" were recorded with no screens at all."""

    def test_a_running_meeting_without_an_end_is_live(self, tmp_path):
        config, mid = _store(tmp_path, None, [_said(0, "Začínáme.")])

        assert autocapture.wispr_live_meeting(config, 3)["id"] == mid

    def test_a_resumed_meeting_is_live_again_although_its_pause_left_an_end_time(self, tmp_path):
        ended = int(time.time() * 1000) - 60_000
        config, mid = _store(tmp_path, ended, [_said(0, "Začínáme."), PAUSED, RESUMED, _said(1, "Pokračujeme.")])

        assert autocapture.wispr_live_meeting(config, 3)["id"] == mid

    def test_a_paused_meeting_stops_capture_so_the_break_is_not_recorded(self, tmp_path):
        ended = int(time.time() * 1000) - 60_000
        config, _ = _store(tmp_path, ended, [_said(0, "Začínáme."), PAUSED])

        assert autocapture.wispr_live_meeting(config, 3) is None

    def test_paused_again_after_a_resume_stops_capture(self, tmp_path):
        ended = int(time.time() * 1000) - 60_000
        config, _ = _store(tmp_path, ended,
                           [PAUSED, RESUMED, _said(1, "Pokračujeme."), dict(PAUSED, segment=1)])

        assert autocapture.wispr_live_meeting(config, 3) is None

    def test_a_resume_wispr_never_closed_does_not_record_for_hours(self, tmp_path):
        # Wispr crashed mid-segment: the last marker stays `resumed` forever.
        ended = int(time.time() * 1000) - 3_600_000
        config, _ = _store(tmp_path, ended, [PAUSED, RESUMED, _said(1, "Pokračujeme.")], live_age_s=20 * 60)

        assert autocapture.wispr_live_meeting(config, 3) is None
