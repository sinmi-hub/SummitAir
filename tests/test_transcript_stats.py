import pytest

from scripts.transcript_stats import parse_transcript, report


def test_journal_requires_call_selection_and_decodes_text():
    source = (
        'stamp INFO:call=one agent_said="I’m Aria.\\nWhat’s wrong?"\n'
        "stamp INFO:call=one caller_said='Um...'\n"
        "stamp INFO:call=two agent_said='Unrelated call'\n"
    )
    with pytest.raises(ValueError, match="Multiple calls"):
        parse_transcript(source)
    entries = parse_transcript(source, "one")
    assert [e.words for e in entries] == [4, 1]
    assert entries[0].text == "I’m Aria.\nWhat’s wrong?"
    assert "4.00:1" in report(entries)
    with pytest.raises(ValueError, match="No transcript"):
        parse_transcript(source, "missing")


def test_markdown_multiline_inline_and_consecutive_entries():
    entries = parse_transcript(
        "# Transcript\nMetadata ignored\n\n**12:00:00 EDT — Aria**\n\nHello.\n\n"
        "Still there?\n\n**12:00:01 — Aria:** Take your time.\n"
        "**12:00:02 — Contact:** Mmm.\n"
    )
    assert [e.words for e in entries] == [3, 3, 1]
    assert "consecutive Aria run in transcript order: 6 words" in report(entries)


def test_empty_malformed_and_no_caller_words():
    with pytest.raises(ValueError, match="No transcript"):
        parse_transcript("INFO: health check")
    with pytest.raises(ValueError, match="Malformed"):
        parse_transcript("stamp call=one agent_said='truncated")
    entries = parse_transcript("Aria: Hello!\nCaller: ...")
    assert "n/a (no caller words)" in report(entries)
