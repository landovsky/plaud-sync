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
