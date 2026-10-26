import pytest

import plaud_transcribe as pt

# The 2026-10-26 portal meeting: walking between rooms, a colleague raised other
# topics (Filip's leave, tomorrow's meeting, two Zakázky bugs) for ~5 minutes.
TRANSCRIPT = "\n\n".join([
    "[28:53] Speaker 1: Takže proto bych mu to dal na rok zdarma.",
    "[29:16] Speaker 2: Já se teda teďko ještě přesunu s nimi.",
    "🔖 ——— BOOKMARK @ 30:00 (device mark) ———",
    "[32:33] Speaker 2: Našel jsem jednu chybku.",
    "[34:55] Speaker 1: Tak bezva.",
    "[35:00] Speaker 1: No, tak co k tomu? Co teď dál s tím?",
])


class TestParsingTheCut:
    def test_reads_offsets_the_way_transcripts_print_them_including_uncapped_minutes(self):
        assert pt.parse_cut("29:16-34:55") == (29 * 60 + 16, 34 * 60 + 55)
        assert pt.parse_cut("97:57-98:30") == (97 * 60 + 57, 98 * 60 + 30)
        assert pt.parse_cut("1:05:00-1:06:00") == (3900, 3960)

    @pytest.mark.parametrize("bad", ["29:16", "34:55-29:16", "12:57pm-13:03", "29-34"])
    def test_refuses_a_range_it_would_have_to_guess_at(self, bad):
        with pytest.raises(ValueError):
            pt.parse_cut(bad)


class TestCuttingTheTranscript:
    """The meeting must be summarised as if the side conversation never happened."""

    def test_drops_every_turn_inside_the_cut_so_the_summary_never_sees_the_side_topic(self):
        out = pt.apply_cuts(TRANSCRIPT, [pt.parse_cut("29:16-35:00")])

        assert "chybku" not in out and "přesunu" not in out
        assert "[28:53]" in out and "[35:00]" in out

    def test_takes_a_bookmark_inside_the_cut_out_with_the_turn_it_belongs_to(self):
        out = pt.apply_cuts(TRANSCRIPT, [pt.parse_cut("29:16-35:00")])

        assert "BOOKMARK" not in out

    def test_leaves_one_marker_so_a_reader_knows_the_gap_is_deliberate(self):
        out = pt.apply_cuts(TRANSCRIPT, [pt.parse_cut("29:16-35:00")])

        assert out.count("LEFT OUT 29:16–35:00") == 1
        assert out.index("[28:53]") < out.index("LEFT OUT") < out.index("[35:00]")

    def test_end_is_exclusive_so_the_meeting_resuming_on_the_boundary_is_kept(self):
        out = pt.apply_cuts(TRANSCRIPT, [pt.parse_cut("29:16-34:55")])

        assert "[34:55] Speaker 1: Tak bezva." in out

    def test_without_a_cut_the_transcript_is_untouched(self):
        assert pt.apply_cuts(TRANSCRIPT, []) == TRANSCRIPT


class TestCuttingTheImages:
    """Phone photos of the bug screens were taken during the walk — they must
    not be captioned into the portal meeting."""

    def test_leaves_out_images_taken_during_the_cut_before_they_are_captioned(self, monkeypatch, tmp_path):
        records = [
            {"path": tmp_path / "before.jpg", "offset": 28 * 60.0, "klass": "anchored"},
            {"path": tmp_path / "bug.jpg", "offset": 32 * 60 + 48.0, "klass": "anchored"},
            {"path": tmp_path / "pause.jpg", "offset": None, "klass": "associated"},
        ]
        monkeypatch.setattr(pt.visuals, "enabled", lambda c: True)
        monkeypatch.setattr(pt.visuals, "collect", lambda *a, **k: list(records))
        captioned = []
        monkeypatch.setattr(pt.visuals, "stage", lambda recs, *a: recs)
        monkeypatch.setattr(pt.visuals, "caption", lambda recs, *a: captioned.extend(recs) or recs)
        monkeypatch.setattr(pt.visuals, "visual_context", lambda recs, c: "")

        pt.prepare_visuals([{}], {}, tmp_path, cuts=[pt.parse_cut("29:16-34:55")])

        assert [r["path"].name for r in captioned] == ["before.jpg", "pause.jpg"]
