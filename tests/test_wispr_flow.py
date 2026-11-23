import json

import wispr_flow


def _write_ndjson(path, objs):
    path.write_text("\n".join(json.dumps(o, ensure_ascii=False) for o in objs) + "\n", encoding="utf-8")


def _refined(ts, text, sid=1):
    return {"id": text, "timestamp": ts, "text": text, "speaker": {"id": sid, "source": "refined", "name": None}}


def _live(segment, start_ms, text, sid=3):
    return {"id": text, "timestamp": "0:00", "text": text, "segment": segment, "startRecordingMs": start_ms,
            "speaker": {"id": sid, "source": "mic", "name": None}}


def _recording(tmp_path, refined, live):
    _write_ndjson(tmp_path / "refined.ndjson", refined)
    _write_ndjson(tmp_path / "live.ndjson", [{"meta": {"v": 3}}] + live)
    return {"_wispr": {"refined_path": str(tmp_path / "refined.ndjson"),
                       "live_path": str(tmp_path / "live.ndjson"), "speaker_names": {1: "Tomáš"}}}


class TestStoppedAndResumedRecording:
    """Wispr refines only the first segment of a stopped-and-resumed meeting and
    never returns for the rest — the 2026-10-20 Hřiště sync lost a request for
    Petr that was said after the restart."""

    def test_keeps_what_was_said_after_the_restart_so_late_requests_reach_the_summary(self, tmp_path):
        rec = _recording(tmp_path,
                         refined=[_refined("00:00", "Začínáme."), _refined("97:57", "Tak jo, díky.")],
                         live=[_live(0, 1_000, "Začínáme."), _live(0, 5_877_000, "Tak jo, díky."),
                               {"id": "m", "marker": "resumed", "segment": 1, "timestamp": "12:00pm"},
                               _live(1, 5_881_000, "Úkol pro mě: indikátor načítání v sidebaru.")])

        text = wispr_flow.transcript_text(rec)

        assert text.endswith("[98:01] Live speaker 3: Úkol pro mě: indikátor načítání v sidebaru.")
        assert text.count("Tak jo, díky.") == 1

    def test_labels_live_speakers_apart_because_live_ids_are_a_different_diarization(self, tmp_path):
        rec = _recording(tmp_path,
                         refined=[_refined("00:05", "Ahoj.")],
                         live=[_live(0, 5_000, "Ahoj."), _live(1, 60_000, "Pokračujeme.", sid=1)])

        assert "[01:00] Live speaker 1: Pokračujeme." in wispr_flow.transcript_text(rec)


class TestFullyRefinedRecording:
    """Most meetings are refined end to end; appending live text there would
    duplicate the whole transcript in a worse quality."""

    def test_adds_nothing_when_refine_covered_every_segment(self, tmp_path):
        rec = _recording(tmp_path,
                         refined=[_refined("00:05", "První část."), _refined("30:00", "Druhá část.")],
                         live=[_live(0, 5_000, "První část."), _live(1, 1_700_000, "Druhá část.")])

        assert wispr_flow.transcript_text(rec) == "[00:05] Tomáš: První část.\n\n[30:00] Tomáš: Druhá část."

    def test_never_appends_the_first_segment_because_its_live_clock_drifts_from_refined(self, tmp_path):
        # Real 10 s clip: refined says 00:01, live says the same words started at 0:07.
        rec = _recording(tmp_path,
                         refined=[_refined("00:01", "Hnusný pasťák na pláži.")],
                         live=[_live(0, 7_000, "Hnusný pasťák na pláži.")])

        assert wispr_flow.transcript_text(rec) == "[00:01] Tomáš: Hnusný pasťák na pláži."

    def test_still_works_for_meetings_without_a_live_transcript(self, tmp_path):
        rec = _recording(tmp_path, refined=[_refined("00:05", "Ahoj.")], live=[])
        (tmp_path / "live.ndjson").unlink()

        assert wispr_flow.transcript_text(rec) == "[00:05] Tomáš: Ahoj."


# --- Meetings Wispr never refined (2026-11-23: "offline for 6 min", no refined.ndjson) ---

import sqlite3
from datetime import datetime, timedelta, timezone


def _store(tmp_path, ended_min_ago, refined=None, live=None):
    """A Wispr data dir with one finalized meeting that ended `ended_min_ago` minutes ago."""
    db = sqlite3.connect(tmp_path / "flow.sqlite")
    db.execute("CREATE TABLE Meetings (id, title, summary, notes, speakerMap, participantNames,"
               " createdAt, endedAt, isDeleted, finalized)")
    ended = datetime.now(timezone.utc) - timedelta(minutes=ended_min_ago)
    created = ended - timedelta(minutes=5)
    db.execute("INSERT INTO Meetings VALUES ('m1', 'Radka', NULL, NULL, NULL, NULL, ?, ?, 0, 1)",
               (created.strftime("%Y-%m-%d %H:%M:%S.%f")[:-3] + " +00:00", int(ended.timestamp() * 1000)))
    db.commit()
    db.close()
    d = tmp_path / "meetings" / "m1"
    d.mkdir(parents=True)
    if refined is not None:
        _write_ndjson(d / "refined.ndjson", refined)
    if live is not None:
        _write_ndjson(d / "live.ndjson", [{"meta": {"v": 3}}] + live)
    return {"wispr": {"enabled": True, "data_dir": str(tmp_path)}}


def _live_turn(start_ms, text, segment=0):
    return {**_live(segment, start_ms, text, sid=1), "endRecordingMs": start_ms + 2_000}


class TestMeetingWisprNeverRefined:
    """The 23.11 Radka meeting was finalized, but Wispr never wrote its refined
    transcript — and the pipeline skipped it without a word, so it was only
    missed when someone counted the day's recordings."""

    def test_is_processed_from_the_live_transcript_once_wispr_had_its_chance(self, tmp_path):
        config = _store(tmp_path, ended_min_ago=120,
                        live=[_live_turn(7_000, "Předávací protokoly."), _live_turn(60_000, "Kalkulace.")])

        [meeting] = wispr_flow.list_meetings(config, days=1)

        assert meeting["_wispr"]["transcript_from"] == "live"
        assert wispr_flow.transcript_text(meeting) == (
            "[00:07] Live speaker 1: Předávací protokoly.\n\n[01:00] Live speaker 1: Kalkulace.")

    def test_waits_while_wispr_may_still_be_refining_but_says_so(self, tmp_path, capsys):
        config = _store(tmp_path, ended_min_ago=5, live=[_live_turn(7_000, "Ahoj.")])

        assert wispr_flow.list_meetings(config, days=1) == []
        assert "still refining 'Radka'" in capsys.readouterr().err

    def test_the_wait_is_configurable_for_people_who_want_it_sooner(self, tmp_path):
        config = _store(tmp_path, ended_min_ago=5, live=[_live_turn(7_000, "Ahoj.")])
        config["wispr"]["live_fallback_minutes"] = 1

        assert len(wispr_flow.list_meetings(config, days=1)) == 1

    def test_a_meeting_with_no_transcript_at_all_is_reported_not_dropped_silently(self, tmp_path, capsys):
        config = _store(tmp_path, ended_min_ago=120, live=[])

        assert wispr_flow.list_meetings(config, days=1) == []
        assert "'Radka' has no transcript yet" in capsys.readouterr().err

    def test_a_refined_meeting_still_uses_the_refined_transcript(self, tmp_path):
        config = _store(tmp_path, ended_min_ago=120, refined=[_refined("00:05", "Ahoj.")],
                        live=[_live_turn(5_000, "Ahoj.")])

        [meeting] = wispr_flow.list_meetings(config, days=1)

        assert meeting["_wispr"]["transcript_from"] == "refined"


class TestLiveFallbackIsNotCached:
    """If Wispr refines the meeting later, a --force redo must get the better transcript."""

    def test_live_text_is_not_written_to_the_transcript_cache(self, tmp_path, monkeypatch):
        import plaud_transcribe
        monkeypatch.setattr(wispr_flow, "transcript_text", lambda r: "[00:07] Live speaker 1: Ahoj.")
        rec = {"_wispr": {"transcript_from": "live"}}

        assert plaud_transcribe.wispr_raw_text_cached(rec, tmp_path) == "[00:07] Live speaker 1: Ahoj."
        assert not (tmp_path / "transcript.txt").exists()
