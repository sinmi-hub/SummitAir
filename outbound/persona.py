"""Mirrors app/agent/persona.py's pattern for the outbound check-in domain."""
from pathlib import Path

# The application speaks this before handing the conversation to the model.
GREETING = (
    "Hi, it's Aria again, Sinmi's AI assistant. Sorry, our last call cut out on my end. "
    "I'm recording this call. Up for a chat?"
)

# Also fits a declined call or a different person answering.
CLOSING_GOODBYE = "Thanks for chatting. Take care!"
CLOSING_PHRASES = ("thanks for chatting", "take care", "goodbye", "bye", "safe trip")


def system_prompt() -> str:
    # The real prompt is git-ignored; the committed example is the fallback.
    path = Path(__file__).with_name("SYSTEM-PROMPT.md")
    if not path.exists():
        path = path.with_name("SYSTEM-PROMPT.example.md")
    return path.read_text(encoding="utf-8")
