You are processing a transcribed recording. Analyze it and produce a JSON response.

## Recording metadata
- Original filename: {{FILENAME}}
- Duration: {{DURATION_MIN}} minutes
- Date: {{DATE}}
- Plaud folder: {{FOLDER}}

## Known projects
{{PROJECT_LIST}}

## Tasks

1. **Classify**: determine recording type, assign to a project key from the list above (or "default" if none match), pick a short descriptive name in the recording's language, add relevant tags.

2. **Summarize**: write a structured summary in the recording's language. Include these sections:
   - **Context** (who, what, why — 2-3 sentences)
   - **Key points** (bulleted)
   - **Decisions** (bulleted, if any)
   - **Action items** (bulleted with owner if identifiable, e.g. "[speaker_0]")
   - **Open questions** (if any)

3. **Clean transcript**: produce a cleaned version that removes filler words (um, uh, like, you know used as pure filler) and irrelevant passages (background noise, phone interruptions) ONLY with high confidence. Keep all substantive content, speaker labels, and timestamps.

Respond with ONLY valid JSON, no markdown fences, no explanation:
{
  "name": "short descriptive name in recording language",
  "type": "meeting|developer-sync|lecture|interview|brainstorm|call|other",
  "tags": ["tag1", "tag2"],
  "project": "project-key-from-list or default",
  "language": "cs|en",
  "summary": "full markdown summary",
  "clean_transcript": "cleaned transcript text"
}

## Transcript

{{TRANSCRIPT}}
