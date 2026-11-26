import pytest

import plaud_transcribe as pt


def _config(**kw):
    return kw


class TestExistingConfigsKeepWorking:
    """Every config written before object-form projects existed has string values."""

    def test_string_projects_come_back_as_the_same_dir_strings_so_call_sites_are_untouched(self):
        config = _config(projects={"A": "~/a/docs", "B": "~/b/docs"})

        assert pt.get_projects(config) == {"A": "~/a/docs", "B": "~/b/docs"}

    def test_a_string_project_never_pushes_so_upgrading_cannot_start_publishing(self):
        spec = pt.project_specs(_config(projects={"A": "~/a"}))["A"]

        assert spec == {"dir": "~/a", "push": "none", "node": None, "host": None,
                        "hints": None, "deliver_ask": None}

    def test_folder_mapping_fallback_still_feeds_projects_and_skips_null_folders(self):
        config = _config(folder_mapping={"Work": "~/w", "Private": None})

        assert pt.get_projects(config) == {"Work": "~/w"}


class TestObjectFormProject:
    """A project that also says how to publish and whom to hand the transcript to."""

    def test_all_keys_are_read_from_the_object(self):
        config = _config(projects={"H": {"dir": "~/h", "push": "pr", "node": "hriste:root",
                                         "host": "hp-ubuntu",
                                         "hints": "BOM", "deliver_ask": "pull action items"}})

        assert pt.project_specs(config)["H"] == {
            "dir": "~/h", "push": "pr", "node": "hriste:root", "host": "hp-ubuntu",
            "hints": "BOM", "deliver_ask": "pull action items"}

    def test_get_projects_flattens_to_dir_so_expanduser_stays_at_the_use_site(self):
        config = _config(projects={"H": {"dir": "~/h"}, "A": "~/a"})

        assert pt.get_projects(config) == {"H": "~/h", "A": "~/a"}

    def test_push_defaults_to_none_when_omitted(self):
        assert pt.project_specs(_config(projects={"H": {"dir": "~/h"}}))["H"]["push"] == "none"


class TestBadConfigFailsAtLoadNotMidRun:
    """A typo in push must not surface hours later when a job tries to publish."""

    def _load(self, monkeypatch, tmp_path, text):
        f = tmp_path / "config.json"
        f.write_text(text)
        monkeypatch.setattr(pt, "CONFIG_FILE", f)
        return pt.load_config()

    def test_unknown_push_value_is_rejected_naming_the_project(self, monkeypatch, tmp_path):
        with pytest.raises(pt.ConfigError, match=r"project 'Hriste': push must be main\|pr\|none, got 'foo'"):
            self._load(monkeypatch, tmp_path, '{"projects": {"Hriste": {"dir": "~/h", "push": "foo"}}}')

    def test_object_without_dir_is_rejected_since_nothing_could_be_written(self, monkeypatch, tmp_path):
        with pytest.raises(pt.ConfigError, match="project 'X'.*dir"):
            self._load(monkeypatch, tmp_path, '{"projects": {"X": {"push": "main"}}}')

    def test_deliver_ask_must_be_a_string(self, monkeypatch, tmp_path):
        with pytest.raises(pt.ConfigError, match="project 'X': deliver_ask must be a string"):
            self._load(monkeypatch, tmp_path,
                       '{"projects": {"X": {"dir": "~/x", "deliver_ask": ["a"]}}}')

    def test_a_valid_config_loads_unchanged(self, monkeypatch, tmp_path):
        config = self._load(monkeypatch, tmp_path, '{"projects": {"X": "~/x"}, "language": "en"}')

        assert config["language"] == "en"


class TestHintsFromEitherPlace:
    """Hints used to live only in project_hints; both spots are read."""

    def test_top_level_project_hints_still_work(self):
        config = _config(projects={"A": "~/a"}, project_hints={"A": "alpha"})

        assert pt.project_hints(config) == {"A": "alpha"}

    def test_per_project_hints_are_merged_with_top_level_ones(self):
        config = _config(projects={"A": "~/a", "B": {"dir": "~/b", "hints": "beta"}},
                         project_hints={"A": "alpha"})

        assert pt.project_hints(config) == {"A": "alpha", "B": "beta"}

    def test_per_project_hints_win_on_a_clash_because_they_sit_next_to_the_project(self):
        config = _config(projects={"A": {"dir": "~/a", "hints": "new"}},
                         project_hints={"A": "old"})

        assert pt.project_hints(config) == {"A": "new"}


class TestSummarizerPromptStaysClean:
    """push/node/deliver_ask say nothing about content and would skew classification."""

    def test_project_list_shows_only_dir_and_topics(self):
        config = _config(projects={"H": {"dir": "~/h", "push": "main", "node": "hriste:root",
                                         "hints": "BOM", "deliver_ask": "SECRET-ASK"}})

        text = pt.format_project_list(pt.get_projects(config), config, {})

        assert text == "- H: ~/h\n    topics: BOM"


class TestHelpDocumentsProjectKeys:
    def test_help_epilog_lists_every_key_and_runs_nothing(self, monkeypatch, capsys):
        def boom(*a, **k):
            raise AssertionError("--help must not run anything")
        for name in ("get_token", "fetch_folders", "load_state", "load_config"):
            monkeypatch.setattr(pt, name, boom)
        monkeypatch.setattr("sys.argv", ["plaud-transcribe", "--help"])

        with pytest.raises(SystemExit) as e:
            pt.main()

        out = capsys.readouterr().out
        assert e.value.code == 0
        for key in ("dir", "push", "node", "hints", "deliver_ask"):
            assert key in out
        assert "main | pr | none" in out


class TestProjectTypoDoesNotStopScreenCapture:
    """autocapture never reads `projects`; a typo there must not cost a meeting its screens."""

    def test_autocapture_loads_config_without_project_validation(self, monkeypatch, tmp_path):
        import inspect
        import autocapture
        f = tmp_path / "config.json"
        f.write_text('{"projects": {"H": {"dir": "~/h", "push": "foo"}}}')
        monkeypatch.setattr(pt, "CONFIG_FILE", f)

        assert pt.load_config(validate=False)["projects"]["H"]["push"] == "foo"
        with pytest.raises(pt.ConfigError):
            pt.load_config()  # plaud-transcribe itself still refuses
        assert "pt.load_config(validate=False)" in inspect.getsource(autocapture._main)
