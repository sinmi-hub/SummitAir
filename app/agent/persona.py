"""The checked-in prompt is the source of truth for each accepted call."""
from pathlib import Path

AGENT_NAME = "Aria"
AGENT_GREETING = (
    "Thanks for calling Summit Air Heating and Cooling, this is Aria. "
    "How can I help you today?"
)
# Spoken by the code itself if end_call fires without this having been said --
# live testing showed the model reliably skips it in favor of a generic
# "let me wrap this up" line, even though CALL CLOSING specifies it.
CLOSING_GOODBYE = "Thank you for choosing Summit Air. I hope you have an amazing day."


def system_prompt() -> str:
    return Path(__file__).with_name("SYSTEM-PROMPT.md").read_text(encoding="utf-8")
