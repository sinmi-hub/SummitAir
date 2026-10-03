"""Mirrors app/agent/persona.py's pattern for the outbound check-in domain."""
from pathlib import Path

# The application speaks this before handing the conversation to the model.
GREETING = (
    "Hi, I'm Aria, Sinmi's AI assistant. He can't call himself, so you're his "
    "test audience. Up for a chat?"
)

# Also fits a declined call or a different person answering.
CLOSING_GOODBYE = "Thanks for chatting. Take care!"
CLOSING_PHRASES = ("thanks for chatting", "take care", "goodbye", "bye", "safe trip")


def system_prompt() -> str:
    return Path(__file__).with_name("SYSTEM-PROMPT.md").read_text(encoding="utf-8")
