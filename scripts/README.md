# Transcript word counts

Run offline with Python 3.11+; no API calls or extra dependencies:

```bash
python3 scripts/transcript_stats.py transcript.md --csv /tmp/word-counts.csv
python3 scripts/transcript_stats.py service.log --call-id rtc_YOUR_CALL_ID
```

Accepts raw `journalctl` exports with `agent_said`/`caller_said`, or plain text/Markdown
with `Aria:`, `Caller:`, or `Contact:` labels. Timestamped labels such as
`**12:37:33 EDT — Aria:** Hello.` and labels followed by multiline text also work.
Use one call per Markdown file; raw logs with multiple calls require `--call-id`.

Reports word totals, each speaker's share, mean and maximum entry length, the
Aria/caller ratio, consecutive Aria word counts, and counts for every entry.
Optional CSV includes the full text so you can compare large replies against
what the caller actually said. Apostrophe contractions count as one word;
hyphenated words are split. Unicode letter/number runs count as words, without
language-specific segmentation. Garbled transcription is counted as logged.

These are text measurements, not audio durations or proof of what was heard.
An entry can be an interrupted fragment, and consecutive entries can arrive
out of spoken order. Question-mark counts do not detect multi-choice overload.
Use the measurements to locate turns to review, not to impose a fixed word ratio.
