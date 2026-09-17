"""The checked-in prompt is the source of truth for each accepted call."""
from pathlib import Path

AGENT_NAME = "Aria"
AGENT_GREETING = (
    "Thanks for calling Summit Air Heating and Cooling, this is Aria. "
    "How can I help you today?"
)


def system_prompt() -> str:
    return Path(__file__).with_name("SYSTEM-PROMPT.md").read_text(encoding="utf-8")
