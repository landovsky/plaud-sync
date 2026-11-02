import plaud_transcribe as pt


class TestRedoKeepsItsDirectory:
    """A `--force` redo gets a fresh title from the summarizer; writing it under the
    new slug left the first run's directory behind as a duplicate (2026-11-02
    training) and would break links to an already-committed transcript."""

    def test_a_redo_writes_back_into_the_directory_of_the_first_run(self, tmp_path):
        first = tmp_path / "2026-11-02-08-36-skoleni-nastenky-fronty"
        first.mkdir()
        state = {"transcribed": {"wispr:9bf98b25": {"output_dir": str(first), "status": "done"}}}

        out = pt.resolve_output_dir(state, ["wispr:9bf98b25"], tmp_path, "2026-11-02-08-36-skoleni-blokace-zdv")

        assert out == first

    def test_a_new_recording_gets_a_directory_named_after_its_title(self, tmp_path):
        out = pt.resolve_output_dir({"transcribed": {}}, ["abc"], tmp_path, "2026-11-02-08-36-skoleni")

        assert out == tmp_path / "2026-11-02-08-36-skoleni"

    def test_a_redo_routed_to_another_project_goes_there_instead_of_staying_put(self, tmp_path):
        old_base, new_base = tmp_path / "default", tmp_path / "hriste"
        (old_base / "old-dir").mkdir(parents=True)
        state = {"transcribed": {"abc": {"output_dir": str(old_base / "old-dir")}}}

        out = pt.resolve_output_dir(state, ["abc"], new_base, "new-dir")

        assert out == new_base / "new-dir"

    def test_a_first_run_directory_deleted_since_is_not_resurrected_under_its_old_name(self, tmp_path):
        state = {"transcribed": {"abc": {"output_dir": str(tmp_path / "gone")}}}

        out = pt.resolve_output_dir(state, ["abc"], tmp_path, "new-dir")

        assert out == tmp_path / "new-dir"
