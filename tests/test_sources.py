"""The source registry: one place that knows the sources, so a new one is a
module plus one entry, not eleven edits across the pipeline."""

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

import jobs
import jobs_cli
import phone_calls
import plan_logic
import plaud_transcribe as pt
import runner
import sources
import visuals
from test_phone_calls import FakePhone, CALL_0909

FIXTURE_AUDIO = Path(__file__).parent / "fixtures" / "call-export.m4a"


class TestIdsBelongToExactlyOneSource:
    """An unknown prefix used to be taken for a Plaud id: the job asked for a
    Plaud token and searched the Plaud listing for a recording Plaud never had."""

    @pytest.mark.parametrize("rid, name", [("wispr:abc", "wispr"), ("recorder:7", "recorder"),
                                           ("call:20261126-090907", "call"), ("local:ab12", "local"),
                                           ("0123abcdef", "plaud"), ("plaud:aaa", "plaud")])
    def test_each_prefix_maps_to_its_source(self, rid, name):
        assert sources.source_for_id(rid).name == name

    def test_an_unknown_prefix_is_an_error_that_names_it(self):
        with pytest.raises(sources.UnknownSource, match="'fax:'"):
            sources.source_for_id("fax:1")

    def test_the_runner_fails_such_a_job_at_preflight_before_any_paid_step(self, monkeypatch):
        monkeypatch.setattr(runner.shutil, "which", lambda name, path=None: "/bin/claude")
        with pytest.raises(runner.PreflightError, match="unknown recording source 'fax:'"):
            runner.preflight(jobs.new_job(recordings=["fax:1"]), {}, {"CLAUDE_CODE_OAUTH_TOKEN": "x"})

    def test_enqueue_refuses_it_up_front(self, tmp_path, monkeypatch, capsys):
        monkeypatch.setattr(jobs, "JOBS_DIR", tmp_path / "jobs")
        monkeypatch.setattr(pt, "CONFIG_FILE", tmp_path / "config.json")
        assert jobs_cli.main(["enqueue", "--recording", "fax:1"], host=pt) != 0
        assert "unknown recording source" in capsys.readouterr().err
        assert not (tmp_path / "jobs").exists() or not list((tmp_path / "jobs").iterdir())

    def test_an_unknown_source_on_a_recording_is_not_labelled_plaud(self):
        with pytest.raises(sources.UnknownSource):
            pt.source_label({"id": "x", "source": "fax"})


class TestCredentialsComeFromTheSources:
    @pytest.fixture(autouse=True)
    def claude_on_path(self, monkeypatch):
        monkeypatch.setattr(runner.shutil, "which", lambda name, path=None: "/bin/claude")

    def test_a_call_only_job_needs_elevenlabs_but_no_plaud_token(self):
        runner.preflight(jobs.new_job(recordings=["call:20261126-090907"]), {},
                         {"CLAUDE_CODE_OAUTH_TOKEN": "x", "ELEVENLABS_API_KEY": "e"})
        with pytest.raises(runner.PreflightError) as e:
            runner.preflight(jobs.new_job(recordings=["call:20261126-090907"]), {},
                             {"CLAUDE_CODE_OAUTH_TOKEN": "x"})
        assert "ELEVENLABS_API_KEY" in str(e.value) and "PLAUD" not in str(e.value)

    def test_the_runner_imports_every_credential_some_source_may_need(self):
        assert runner.REQUIRED_ENV == ("CLAUDE_CODE_OAUTH_TOKEN", "ELEVENLABS_API_KEY", "PLAUD_BEARER_TOKEN")


class TestOneListing:
    """`list`, the plan screen and the runner used to list the sources three
    different ways, so a source added to one was missing from the others."""

    @pytest.fixture
    def spy(self, monkeypatch):
        seen = []
        real = sources.list_recordings

        def spy(host, config, days, token=None, **kw):
            seen.append(kw.get("only"))
            return real(host, config, days, token, **kw)

        monkeypatch.setattr(sources, "list_recordings", spy)
        for mod, name in ((pt, "list_recordings"), (pt.wispr_flow, "list_meetings"),
                          (pt.google_recorder, "list_recordings"), (phone_calls, "list_recordings")):
            monkeypatch.setattr(mod, name, lambda *a: [])
        return seen

    def test_list_and_plan_both_go_through_the_registry(self, spy):
        pt.list_all_recordings("tok", {}, 30)
        plan_logic.pending_recordings("tok", {}, {}, 30)
        assert spy == [None, None]

    def test_the_runner_lists_only_the_sources_its_job_needs(self, spy, monkeypatch, tmp_path):
        monkeypatch.setattr(pt, "load_config", lambda **k: {})
        monkeypatch.setattr(pt, "get_token", lambda: pytest.fail("Plaud token asked for"))
        job = jobs.new_job(recordings=["call:20261126-090907"])
        with pytest.raises(RuntimeError, match="not found"):
            runner.step_transcribe(job, SimpleNamespace(host=pt, days=30))
        assert spy == [{"call"}]


class TestANewSourceIsOneRegistryEntry:
    """The rewrite-signals bar: adding a source touches <= 3 places outside its
    module. Here: zero. A dummy source registered in SOURCES alone is listed,
    labelled, prefetched off the phone, fetched and credential-checked."""

    @pytest.fixture
    def dummy(self, monkeypatch, tmp_path):
        pulled = []

        def pull(rec, config, dest):
            pulled.append(rec["id"])
            dest.mkdir(parents=True, exist_ok=True)
            (dest / "d.m4a").write_bytes(b"x")
            return dest / "d.m4a"

        rec = {"id": "dummy:1", "source": "dummy", "filename": "Dummy", "start_time": 9, "duration": 60_000}
        src = sources.Source("dummy", "dummy:", "Dummy", "Dummy", "Dummy box",
                             lister=lambda host, config, days, token: [dict(rec)],
                             pull=pull, on_phone=True, env=("DUMMY_KEY",), id_key="dummy_id")
        monkeypatch.setattr(sources, "SOURCES", sources.SOURCES + (src,))
        monkeypatch.setattr(sources, "BY_NAME", {**sources.BY_NAME, "dummy": src})
        for mod, name in ((pt, "list_recordings"), (pt.wispr_flow, "list_meetings"),
                          (pt.google_recorder, "list_recordings"), (phone_calls, "list_recordings")):
            monkeypatch.setattr(mod, name, lambda *a: [])
        monkeypatch.setattr(pt, "AUDIO_DIR", tmp_path / "audio")
        monkeypatch.setattr(visuals, "enabled", lambda c: False)
        return SimpleNamespace(rec=rec, pulled=pulled)

    def test_it_is_listed_and_labelled(self, dummy):
        listed, = pt.list_all_recordings("tok", {}, 30)
        assert listed["id"] == "dummy:1" and pt.source_label(listed) == "Dummy"

    def test_its_audio_is_prefetched_and_then_fetched_by_the_pipeline(self, dummy):
        pt.prefetch_phone_files([dummy.rec], False, {})
        assert pt.obtain_audio(dummy.rec, None, {}, "[1/4]").name == "d.m4a"
        assert dummy.pulled == ["dummy:1", "dummy:1"]

    def test_its_credential_is_checked_before_a_job_runs(self, dummy, monkeypatch):
        monkeypatch.setattr(runner.shutil, "which", lambda name, path=None: "/bin/claude")
        with pytest.raises(runner.PreflightError, match="DUMMY_KEY"):
            runner.preflight(jobs.new_job(recordings=["dummy:1"]), {}, {"CLAUDE_CODE_OAUTH_TOKEN": "x"})


class TestCallJobEndToEnd:
    """A4: a call recording planned as a job goes pull → transcribe → write in the
    runner, with adb and the paid APIs stubbed and a fixture file as the audio."""

    @pytest.fixture
    def world(self, tmp_path, monkeypatch):
        monkeypatch.setattr(jobs, "JOBS_DIR", tmp_path / "jobs")
        monkeypatch.setattr(runner, "LOCK_FILE", tmp_path / "runner.lock")
        for name, sub in (("STATE_FILE", "state.json"), ("CONFIG_FILE", "config.json")):
            monkeypatch.setattr(pt, name, tmp_path / "cfg" / sub)
        monkeypatch.setattr(pt, "CONFIG_DIR", tmp_path / "cfg")
        monkeypatch.setattr(pt, "WORKING_DIR", tmp_path / "work")
        monkeypatch.setattr(pt, "AUDIO_DIR", tmp_path / "audio")
        monkeypatch.setattr(visuals, "_interactive", True)
        monkeypatch.setattr(visuals, "enabled", lambda c: False)
        monkeypatch.setattr(runner, "load_runner_env", lambda *a, **k: "env: stubbed")
        monkeypatch.setattr(runner, "preflight", lambda *a, **k: None)
        monkeypatch.setattr(runner, "notify", lambda *a, **k: None)
        (tmp_path / "cfg").mkdir()
        out = tmp_path / "calls-out"
        (tmp_path / "cfg" / "config.json").write_text(json.dumps({
            "timezone": "Europe/Prague", "calls": {"enabled": True},
            "projects": {"Calls": {"dir": str(out), "push": "none"}}}))

        for name in ("get_token", "fetch_folders", "list_recordings", "download_audio"):
            monkeypatch.setattr(pt, name, lambda *a, **k: pytest.fail("Plaud touched"))
        monkeypatch.setattr(pt.wispr_flow, "list_meetings", lambda *a: pytest.fail("Wispr listed"))
        monkeypatch.setattr(pt.google_recorder, "list_recordings", lambda *a: pytest.fail("Recorder listed"))
        phone = FakePhone(audio=FIXTURE_AUDIO.read_bytes()).install(monkeypatch)
        days = (pt.datetime.now(pt.timezone.utc).timestamp() * 1000 - CALL_0909) // 86_400_000 + 2
        seen = SimpleNamespace(transcribed=[], summarized=[])

        def transcribe(path, language="cs", config=None):
            seen.transcribed.append(Path(path).read_bytes())
            return {"words": [{"type": "word", "speaker_id": "s0", "text": "ahoj", "start": 0.0, "end": 1.0}]}

        def summarize(text, meta, projects, language="en", **kw):
            seen.summarized.append(text)
            return {"name": "Call about the order", "type": "call", "project": "Calls", "tags": [],
                    "language": "cs", "summary": "# Call\n\nThey agreed.", "clean_transcript": ""}

        monkeypatch.setattr(pt, "transcribe_audio", transcribe)
        monkeypatch.setattr(pt, "claude_summarize", summarize)
        return SimpleNamespace(phone=phone, out=out, seen=seen, days=int(days))

    def test_the_call_is_pulled_transcribed_and_written_without_plaud(self, world):
        assert jobs_cli.main(["enqueue", "--recording", "call:20261126-090907",
                              "--project", "Calls"], host=pt) == 0
        jid = jobs.list_jobs()[-1]["id"]

        assert runner.run_queue(pt, days=world.days) == [jid]

        job = jobs.load_job(jid)
        assert job["state"] == "done", job.get("error")
        assert world.seen.transcribed == [FIXTURE_AUDIO.read_bytes()]   # the pulled fixture
        assert any(a[0] == "pull" for a in world.phone.calls)
        out_dir = Path(job["result"]["paths"][0])
        assert out_dir.parent == world.out and out_dir.name.startswith("2026-11-26-09-38-")
        summary = (out_dir / "summary.md").read_text()
        assert "call_id: 20261126-090907" in summary and "source: call" in summary
        assert "Phone call" in world.seen.summarized[0] and "09:38:04" in world.seen.summarized[0]
