"""Offline word counts for SummitAir journal logs or speaker-labelled transcripts."""
from __future__ import annotations

import argparse
import ast
import csv
from dataclasses import dataclass
from pathlib import Path
import re


WORD = re.compile(r"[^\W_]+(?:['’][^\W_]+)*", re.UNICODE)
JOURNAL = re.compile(r"\bcall=(\S+) (agent_said|caller_said)=(.*)$")
LABEL = re.compile(
    r"^(?:(?P<time>.+?)\s+[—–]\s+)?(?P<speaker>Aria|Caller|Sister):?\s*(?P<text>.*)$",
    re.IGNORECASE,
)


@dataclass
class Entry:
    speaker: str
    text: str
    timestamp: str = ""

    @property
    def words(self) -> int:
        return len(WORD.findall(self.text))


def parse_transcript(text: str, call_id: str | None = None) -> list[Entry]:
    """Preserve log order; never silently combine calls from a journal export."""
    lines = text.splitlines()
    records = [match for line in lines if (match := JOURNAL.search(line))]
    if records:
        ids = {match[1] for match in records}
        if call_id is None and len(ids) > 1:
            raise ValueError("Multiple calls found; select one with --call-id: " + ", ".join(sorted(ids)))
        entries = []
        for line in lines:
            match = JOURNAL.search(line)
            if not match or (call_id and match[1] != call_id):
                continue
            try:
                words = ast.literal_eval(match[3])
            except (ValueError, SyntaxError) as exc:
                raise ValueError("Malformed transcript entry; use an untruncated journal export") from exc
            if not isinstance(words, str):
                raise ValueError("Transcript entry must contain text")
            entries.append(Entry("Aria" if match[2] == "agent_said" else "Caller", words, line.split()[0]))
    else:
        if call_id:
            raise ValueError("--call-id requires raw journal logs")
        entries = []
        current = None
        for line in lines:
            clean = line.replace("**", "").strip()
            match = LABEL.match(clean)
            if match:
                current = Entry("Aria" if match['speaker'].lower() == "aria" else "Caller",
                                match['text'], match['time'] or "")
                entries.append(current)
            elif clean.startswith("#") or clean == "---":
                current = None
            elif clean and current is not None:
                current.text += "\n" + clean
    if not entries:
        raise ValueError("No transcript entries found (expected Aria:/Caller:/Sister: labels or journal logs)")
    return entries


def report(entries: list[Entry]) -> str:
    total = sum(entry.words for entry in entries)
    lines = ["Speaker   Words   Share   Entries   Mean words/entry   Longest entry"]
    for speaker in ("Aria", "Caller"):
        counts = [entry.words for entry in entries if entry.speaker == speaker]
        words = sum(counts)
        share = words / total * 100 if total else 0
        mean = words / len(counts) if counts else 0
        lines.append(f"{speaker:<8} {words:>6} {share:>6.1f}% {len(counts):>9} {mean:>18.1f} {max(counts, default=0):>15}")
    agent = sum(e.words for e in entries if e.speaker == "Aria")
    caller = total - agent
    lines.append(f"Aria/caller word ratio: {agent / caller:.2f}:1" if caller else "Aria/caller word ratio: n/a (no caller words)")
    runs = []
    for entry in entries:
        if runs and runs[-1][0] == entry.speaker:
            runs[-1][1] += entry.words
        else:
            runs.append([entry.speaker, entry.words])
    longest = max((n for speaker, n in runs if speaker == "Aria"), default=0)
    lines.append(f"Longest consecutive Aria run in transcript order: {longest} words")
    lines.extend(["", "Entry  Speaker  Words  Question marks  Timestamp"])
    for i, entry in enumerate(entries, 1):
        lines.append(f"{i:>5}  {entry.speaker:<7} {entry.words:>5} {entry.text.count('?'):>15}  {entry.timestamp}")
    lines.extend(["", "Counts describe logged text, not speaking time or verified audible words.",
                  "Transcription errors, interruptions, and late events affect the totals and order.",
                  "Question marks do not measure choices or cognitive load; no target ratio is assumed."])
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("transcript", type=Path)
    parser.add_argument("--call-id", help="Select a call from a raw journal export")
    parser.add_argument("--csv", type=Path, help="Save each entry, its text, and counts for comparison")
    args = parser.parse_args()
    try:
        entries = parse_transcript(args.transcript.read_text(encoding="utf-8"), args.call_id)
        if args.csv:
            if args.csv.resolve() == args.transcript.resolve():
                raise ValueError("CSV output must differ from the input transcript")
            with args.csv.open("w", newline="", encoding="utf-8") as output:
                writer = csv.writer(output)
                writer.writerow(["entry", "speaker", "timestamp", "words", "question_marks", "text"])
                for i, entry in enumerate(entries, 1):
                    writer.writerow([i, entry.speaker, entry.timestamp, entry.words, entry.text.count("?"), entry.text])
    except (OSError, ValueError) as exc:
        parser.exit(2, f"Error: {exc}\n")
    print(report(entries))


if __name__ == "__main__":
    main()
