"""Phone call recordings: Phone app → Share → Files → /sdcard/Download/<stamp>.m4a.

Names and numbers here are made up: the repo is public."""

from datetime import datetime, timezone
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

import phone_calls
import plan_logic
import plaud_transcribe as pt
import sources

PRAGUE = ZoneInfo("Europe/Prague")
DL = "/storage/emulated/0/Download"


def ms(y, mo, d, h, mi, s, tz=PRAGUE):
    return int(datetime(y, mo, d, h, mi, s, tzinfo=tz).timestamp() * 1000)


# The 2026-11-26 09:09 call: the call began 09:09:07 and lasted 57:31, but only
# 09:38:04-10:06:54 of it was recorded (28:50). Same shape as the real export.
CALL_0909 = ms(2026, 11, 26, 9, 9, 7)
END_0909 = ms(2026, 11, 26, 10, 6, 54)
DUR_0909 = (28 * 60 + 50) * 1000
MEDIA_0909 = f"Row: 17 _data={DL}/2026-11-26 09.09.07.m4a, datetaken={END_0909}, duration={DUR_0909}, _size=3572748"
LOG_0909 = f"Row: 0 date={CALL_0909 + 412}, duration={57 * 60 + 31}, type=1, number=+15550100, name=Alex Example"


def media_row(path, taken, duration=60_000, size=1000):
    return {"path": path, "taken": taken, "duration": duration, "size": size}


def build(media, calls=(), config=None, tz=PRAGUE):
    return phone_calls.build_recordings(media, list(calls), config or {}, tz)


class TestExportNames:
    """The Files app names the export after the call's start; nothing else in
    Download should turn into a call row."""

    def test_the_stamp_in_the_name_is_read_as_wall_clock_time(self):
        assert phone_calls.parse_stamp("2026-11-26 09.09.07.m4a") == datetime(2026, 11, 26, 9, 9, 7)

    def test_a_second_share_of_the_same_call_has_the_same_stamp(self):
        assert phone_calls.parse_stamp("2026-11-26 09.09.07 (1).m4a") == datetime(2026, 11, 26, 9, 9, 7)

    @pytest.mark.parametrize("name", ["Sep 5 at 15-32.m4a",           # a Recorder copy
                                      "2026-11-26 09.09.07.txt",       # not audio
                                      "2026-13-40 09.09.07.m4a",       # not a date
                                      "podcast 2026-11-26 09.09.07.mp3"])
    def test_other_files_in_download_are_not_calls(self, name):
        assert phone_calls.parse_stamp(name) is None

    def test_the_id_is_the_stamp_so_it_survives_re_exporting(self):
        assert phone_calls.call_id(datetime(2026, 11, 26, 9, 9, 7)) == "call:20261126-090907"

    def test_a_custom_pattern_from_the_config_is_used(self):
        cfg = {"calls": {"pattern": r"^call_(\d{4}-\d{2}-\d{2} \d{2}\.\d{2}\.\d{2})\.m4a$"}}
        recs = build([media_row(f"{DL}/call_2026-11-26 09.09.07.m4a", END_0909)], config=cfg)
        assert [r["id"] for r in recs] == ["call:20261126-090907"]


class TestSharedTwice:
    """Sharing a call again gives `… (1).m4a`; listing it twice would let Tomas
    transcribe (and pay for) the same call twice."""

    def test_both_files_give_one_row_and_the_unsuffixed_file_is_kept(self):
        recs = build([media_row(f"{DL}/2026-11-26 09.09.07 (1).m4a", END_0909),
                      media_row(f"{DL}/2026-11-26 09.09.07.m4a", END_0909)])
        assert len(recs) == 1
        assert recs[0]["_call"]["path"].endswith("09.09.07.m4a")

    def test_a_file_in_a_subfolder_of_download_is_not_a_call_export(self):
        assert build([media_row(f"{DL}/old/2026-11-26 09.09.07.m4a", END_0909)]) == []


class TestRecordingStartIsNotTheCallStart:
    """Recording can start anywhere in the call. Dating the recording by the stamp
    put it up to half an hour early and pushed in-call screenshots out (A6)."""

    def test_start_is_the_recordings_end_minus_its_length(self):
        rec, = build(phone_calls.parse_media_rows(MEDIA_0909), phone_calls.parse_call_log(LOG_0909))
        assert datetime.fromtimestamp(rec["start_time"] / 1000, PRAGUE).strftime("%H:%M:%S") == "09:38:04"
        assert rec["duration"] == DUR_0909
        assert rec["call_start"] == CALL_0909 + 412

    def test_a_screenshot_at_10_02_lands_about_24_minutes_into_the_recording(self):
        rec, = build(phone_calls.parse_media_rows(MEDIA_0909))
        offset_s = (ms(2026, 11, 26, 10, 2, 39) - rec["start_time"]) / 1000
        assert 0 <= offset_s <= rec["duration"] / 1000
        assert offset_s == 24 * 60 + 35

    def test_without_an_end_time_the_call_start_still_dates_it(self):
        rec, = build([media_row(f"{DL}/2026-11-26 09.09.07.m4a", None)])
        assert rec["start_time"] == CALL_0909


class TestWhoWasOnTheLine:
    """The call log says who and which way; the file name says neither."""

    def test_a_recording_that_starts_mid_call_still_gets_its_counterpart(self):
        rec, = build(phone_calls.parse_media_rows(MEDIA_0909), phone_calls.parse_call_log(LOG_0909))
        assert rec["filename"] == "Call · Alex Example · in"
        assert (rec["counterpart"], rec["direction"]) == ("Alex Example", "in")
        assert rec["call_duration"] == (57 * 60 + 31) * 1000

    def test_a_recording_with_no_call_in_the_log_is_still_listed_by_its_stamp(self):
        rec, = build(phone_calls.parse_media_rows(MEDIA_0909), [])
        assert rec["filename"] == "Call · 2026-11-26 09.09.07"
        assert rec["counterpart"] is None and rec["direction"] is None

    def test_an_unsaved_number_is_shown_when_there_is_no_contact_name(self):
        log = f"Row: 0 date={CALL_0909}, duration=60, type=2, number=+15550199, name=NULL"
        rec, = build(phone_calls.parse_media_rows(MEDIA_0909), phone_calls.parse_call_log(log))
        assert rec["filename"] == "Call · +15550199 · out"

    def test_a_contact_name_with_a_comma_is_read_whole(self):
        log = f"Row: 0 date={CALL_0909}, duration=60, type=1, number=+15550100, name=Example, Alex"
        assert phone_calls.parse_call_log(log)[0]["name"] == "Example, Alex"

    def test_two_calls_seconds_apart_the_one_nearest_the_stamp_wins(self):
        calls = [{"date": CALL_0909 - 4000, "duration": 5, "type": 2, "number": "+15550111", "name": "Wrong"},
                 {"date": CALL_0909 + 300, "duration": 900, "type": 1, "number": "+15550100", "name": "Right"}]
        rec, = build(phone_calls.parse_media_rows(MEDIA_0909), calls)
        assert rec["counterpart"] == "Right"

    def test_a_call_outside_the_slack_is_not_taken_for_this_one(self):
        calls = [{"date": CALL_0909 + 6000, "duration": 60, "type": 1, "number": "1", "name": "Later"}]
        rec, = build(phone_calls.parse_media_rows(MEDIA_0909), calls)
        assert rec["counterpart"] is None
        cfg = {"calls": {"match_slack_s": 10}}
        rec, = build(phone_calls.parse_media_rows(MEDIA_0909), calls, config=cfg)
        assert rec["counterpart"] == "Later"


class TestTimezones:
    """The name is phone-local wall-clock time; the call log is epoch ms."""

    def test_a_summer_call_matches_across_the_dst_offset(self):
        call = ms(2026, 7, 1, 14, 0, 0)    # CEST, UTC+2
        media = [media_row(f"{DL}/2026-07-01 14.00.00.m4a", call + 600_000)]
        rec, = build(media, [{"date": call, "duration": 600, "type": 1, "number": "1", "name": "Summer"}])
        assert rec["counterpart"] == "Summer" and rec["id"] == "call:20260701-140000"

    def test_a_wrong_zone_finds_no_call_rather_than_the_wrong_one(self):
        call = ms(2026, 11, 26, 9, 9, 7)
        rec, = build(phone_calls.parse_media_rows(MEDIA_0909),
                     [{"date": call, "duration": 60, "type": 1, "number": "1", "name": "X"}], tz=timezone.utc)
        assert rec["counterpart"] is None   # 08:09:07 UTC is not 09:09:07

    def test_the_recording_start_does_not_depend_on_the_zone(self):
        a, = build(phone_calls.parse_media_rows(MEDIA_0909), tz=PRAGUE)
        b, = build(phone_calls.parse_media_rows(MEDIA_0909), tz=timezone.utc)
        assert a["start_time"] == b["start_time"] == END_0909 - DUR_0909


class FakePhone:
    """Stands in for adb: answers the two content queries and copies a fixture on pull."""

    def __init__(self, state="ready", media=MEDIA_0909, log=LOG_0909, audio=b"m4a"):
        self.state, self.media, self.log, self.audio = state, media, log, audio
        self.calls = []

    def install(self, monkeypatch):
        monkeypatch.setattr(phone_calls, "_adb_available", lambda adb: True)
        monkeypatch.setattr(phone_calls, "_phone_state", lambda adb: self.state)
        monkeypatch.setattr(phone_calls, "_adb", self.run)
        return self

    def run(self, adb, *args, timeout=60):
        self.calls.append(args)
        if args[0] == "shell":
            out = self.media if "media/external" in args[1] else self.log
            return SimpleNamespace(stdout=out, stderr="", returncode=0)
        if args[0] == "pull":
            pt.Path(args[2]).write_bytes(self.audio)
            return SimpleNamespace(stdout="", stderr="", returncode=0)
        raise AssertionError(args)


ON = {"calls": {"enabled": True}, "timezone": "Europe/Prague"}


@pytest.fixture
def fresh(monkeypatch):
    """`days` reaches back to 2026-11-26 whatever today's date."""
    days = (datetime.now(timezone.utc) - datetime(2026, 11, 20, tzinfo=timezone.utc)).days + 1
    return days


class TestListing:
    def test_rows_come_out_titled_and_dated_from_the_phone(self, monkeypatch, fresh):
        phone = FakePhone().install(monkeypatch)
        rec, = phone_calls.list_recordings(ON, fresh)
        assert rec["id"] == "call:20261126-090907" and rec["source"] == "call"
        assert rec["filename"] == "Call · Alex Example · in"
        where = [a[1] for a in phone.calls]
        assert f"_data LIKE '{DL}/%'" in where[0] and "call_log" in where[1]

    def test_off_by_default_so_nobody_is_surprised_by_adb_calls(self, monkeypatch, fresh):
        phone = FakePhone().install(monkeypatch)
        assert phone_calls.list_recordings({}, fresh) == []
        assert phone.calls == []

    def test_recordings_older_than_days_are_left_out(self, monkeypatch):
        FakePhone().install(monkeypatch)
        assert phone_calls.list_recordings(ON, 0) == []

    def test_an_unreadable_call_log_still_lists_the_calls_untitled(self, monkeypatch, fresh):
        FakePhone(log="Error: permission denial").install(monkeypatch)
        rec, = phone_calls.list_recordings(ON, fresh)
        assert rec["filename"].startswith("Call · 2026-11-26")


class TestNoPhoneNoProblem:
    """A3: same rule as the Recorder. One stderr note, no call rows, and the
    other sources are not affected."""

    @pytest.mark.parametrize("state, said", [("absent", "no phone"),
                                             ("unauthorized", "allow USB debugging")])
    def test_one_note_and_no_rows(self, monkeypatch, capsys, fresh, state, said):
        FakePhone(state=state).install(monkeypatch)
        assert phone_calls.list_recordings(ON, fresh) == []
        err = capsys.readouterr().err.strip().splitlines()
        assert len(err) == 1 and said in err[0] and "call" in err[0]

    def test_no_adb_binary_is_one_note(self, monkeypatch, capsys, fresh):
        monkeypatch.setattr(phone_calls, "_adb_available", lambda adb: False)
        assert phone_calls.list_recordings(ON, fresh) == []
        assert len(capsys.readouterr().err.strip().splitlines()) == 1

    def test_the_plan_list_keeps_plaud_and_wispr_rows_without_a_phone(self, monkeypatch, fresh):
        FakePhone(state="absent").install(monkeypatch)
        monkeypatch.setattr(pt, "list_recordings", lambda *a: [{"id": "p1", "start_time": 2, "duration": 60_000}])
        monkeypatch.setattr(pt.wispr_flow, "list_meetings",
                            lambda *a: [{"id": "wispr:a", "source": "wispr", "start_time": 1, "duration": 60_000}])
        monkeypatch.setattr(pt.google_recorder, "list_recordings", lambda *a: [])
        got = plan_logic.pending_recordings("tok", ON, {}, fresh)
        assert [r["id"] for r in got] == ["p1", "wispr:a"]

    def test_a_crashing_call_lister_costs_only_the_call_rows_in_the_plan(self, monkeypatch, fresh):
        monkeypatch.setattr(phone_calls, "list_recordings", lambda *a: 1 / 0)
        monkeypatch.setattr(pt, "list_recordings", lambda *a: [{"id": "p1", "start_time": 2, "duration": 60_000}])
        monkeypatch.setattr(pt.wispr_flow, "list_meetings", lambda *a: [])
        monkeypatch.setattr(pt.google_recorder, "list_recordings", lambda *a: [])
        got = plan_logic.pending_recordings("tok", ON, {}, fresh)
        assert [r["id"] for r in got] == ["p1"]
        assert any(n.startswith("Calls not readable") for n in got.notes)


class TestPull:
    """Pulled once, cached by id, never half-written."""

    def rec(self, size=3):
        return {"id": "call:20261126-090907", "source": "call",
                "_call": {"path": f"{DL}/2026-11-26 09.09.07.m4a", "size": size}}

    def test_pull_writes_part_first_then_the_cached_name(self, monkeypatch, tmp_path):
        phone = FakePhone().install(monkeypatch)
        path = phone_calls.pull(self.rec(), ON, tmp_path)
        assert path.name == "call_20261126-090907.m4a" and path.read_bytes() == b"m4a"
        assert phone.calls[-1][2].endswith(".m4a.part")

    def test_a_cached_copy_of_the_right_size_needs_no_phone(self, monkeypatch, tmp_path):
        (tmp_path / "call_20261126-090907.m4a").write_bytes(b"m4a")
        monkeypatch.setattr(phone_calls, "_phone_state", lambda adb: pytest.fail("phone asked"))
        assert phone_calls.pull(self.rec(), ON, tmp_path).exists()

    def test_a_failed_pull_leaves_no_file_that_looks_cached(self, monkeypatch, tmp_path):
        FakePhone().install(monkeypatch)
        monkeypatch.setattr(phone_calls, "_adb",
                            lambda adb, *a, **k: SimpleNamespace(stdout="", stderr="device offline", returncode=1))
        with pytest.raises(RuntimeError, match="device offline"):
            phone_calls.pull(self.rec(), ON, tmp_path)
        assert list(tmp_path.iterdir()) == []


class TestSummarizerIsToldWhatWasRecorded:
    """A summary of a call recorded from minute 29 must not claim the whole call."""

    def test_the_context_names_the_call_and_the_recorded_stretch(self):
        rec, = build(phone_calls.parse_media_rows(MEDIA_0909), phone_calls.parse_call_log(LOG_0909))
        ctx = sources.summary_context(rec, PRAGUE)
        assert "Alex Example" in ctx and "incoming" in ctx
        assert "09:38:04–10:06:54" in ctx and "57:31" in ctx
        assert "first 28:57 of the call is not in the transcript" in ctx

    def test_other_sources_get_no_call_context(self):
        assert sources.summary_context({"id": "p", "source": "plaud"}, PRAGUE) == ""
