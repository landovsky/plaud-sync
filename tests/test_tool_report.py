import json

import plaud_transcribe as pt


def _stream(*events):
    return "\n".join(json.dumps(e) for e in events)


def _tool_use(name, **inp):
    return {"type": "assistant", "message": {"content": [{"type": "tool_use", "name": name, "input": inp}]}}


DOC_OUTSIDE_CWD = "/Users/x/git/k3s/docs/STANDARD.md"


class TestADocumentTheSummarizerWasNotAllowedToOpen:
    """claude -p denies reads outside the cwd silently; the summary goes on
    without the doc, so the denial must be visible to the person running it."""

    STREAM = _stream(
        {"type": "system", "subtype": "init"},
        _tool_use("ToolSearch", query="select:WebFetch"),
        _tool_use("WebFetch", url="https://example.com", prompt="x"),
        _tool_use("Read", file_path=DOC_OUTSIDE_CWD),
        {"type": "result", "result": "  {\"name\": \"n\"}\n===SUMMARY===\nhi  ",
         "permission_denials": [{"tool_name": "Read", "tool_input": {"file_path": DOC_OUTSIDE_CWD}}]},
    )

    def test_keeps_the_summary_text_so_the_existing_parser_still_works(self):
        text, _, _ = pt.parse_claude_stream(self.STREAM)
        assert text == '{"name": "n"}\n===SUMMARY===\nhi'

    def test_names_the_refused_file_on_its_own_line(self):
        report = pt.format_tool_report(*pt.parse_claude_stream(self.STREAM)[1:])
        assert f"Denied:     Read {DOC_OUTSIDE_CWD}" in report

    def test_marks_the_fetch_that_worked_and_the_read_that_did_not(self):
        report = pt.format_tool_report(*pt.parse_claude_stream(self.STREAM)[1:])
        assert "WebFetch https://example.com ✓" in report
        assert f"Read {DOC_OUTSIDE_CWD} ✗" in report

    def test_hides_toolsearch_because_it_is_plumbing_not_context(self):
        report = pt.format_tool_report(*pt.parse_claude_stream(self.STREAM)[1:])
        assert "ToolSearch" not in report


class TestAPlainSummaryWithNoTools:
    def test_says_none_so_the_absence_of_context_is_explicit(self):
        _, used, denied = pt.parse_claude_stream(_stream({"type": "result", "result": "x"}))
        assert pt.format_tool_report(used, denied) == "  Tools used: none"


class TestATruncatedStream:
    def test_yields_empty_text_so_the_usual_parse_failure_reports_it(self):
        text, _, _ = pt.parse_claude_stream('{"type":"system"}\nnot json\n{"type":"assist')
        assert text == ""
