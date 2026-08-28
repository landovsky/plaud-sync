You are processing a transcribed recording. Analyze it and produce a JSON response.

## Recording metadata
- Original filename: {{FILENAME}}
- Duration: {{DURATION_MIN}} minutes
- Date: {{DATE}}
- Plaud folder: {{FOLDER}}

## Known projects
{{PROJECT_LIST}}

Each project may list its output path, its topics, and the names of recordings
recently filed under it. Treat those as evidence: recordings rarely say the
project name out loud, so match on subject matter, systems and people instead —
a recording that continues the thread of a project's recent recordings belongs
to that project even if the name is never spoken. Use "default" only when no
project is a plausible fit, not merely when you are unsure which.

## Tasks

1. **Classify**: determine recording type, assign to a project key from the list above (or "default" if none match — see the guidance under "Known projects"), pick a short descriptive name in the recording's language, add relevant tags.

2. **Summarize**: write a structured summary in the recording's language. Include these sections:
   - **Context** (who, what, why — 2-3 sentences)
   - **Key points** (bulleted)
   - **Decisions** (bulleted, if any)
   - **Action items** (bulleted with owner if identifiable, e.g. "[speaker_0]")
   - **Open questions** (if any)

3. **Clean transcript**: produce a cleaned version that removes filler words (um, uh, like, you know used as pure filler) and irrelevant passages (background noise, phone interruptions) ONLY with high confidence. Keep all substantive content, speaker labels, and timestamps.

## Output format

Respond in EXACTLY this structure — a small JSON object, then the free-text
sections after their sentinel lines. No markdown fences, no explanation. Keeping
the summary and transcript OUT of the JSON is deliberate: it lets you use quotes,
newlines and any punctuation freely without breaking the JSON.

{
  "name": "short descriptive name in recording language (no double quotes inside)",
  "type": "meeting|developer-sync|lecture|interview|brainstorm|call|other",
  "tags": ["tag1", "tag2"],
  "project": "project-key-from-list or default",
  "language": "cs|en"
}
===SUMMARY===
<the full markdown summary from task 2 — write freely; quotes and newlines are fine>
===TRANSCRIPT===
<the cleaned transcript from task 3>

## Transcript

{{TRANSCRIPT}}
