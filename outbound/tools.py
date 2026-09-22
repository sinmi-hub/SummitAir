"""Tool surface for the outbound check-in call. Deliberately minimal -- no HVAC
tools apply here, and per the scoping decision this call reports back to Sinmi
via the same agent_said/caller_said journal logging app/realtime.py already
does, not a dedicated tool. Only end_call is needed, reusing the goodbye-gate
hook built for SummitAir (app/realtime.py Call.execute) unchanged."""
from __future__ import annotations

from jsonschema import Draft202012Validator

TOOL_SPECS = [
    ("end_call", "End the call after the spoken goodbye has finished. Use when the person "
     "confirms they are done, declines to chat, is busy, or asks to hang up. A request to stop "
     "does not require any further questions or confirmation. If they resume talking before "
     "the goodbye finishes, respond before ending the call.", {}, []),
]
TOOLS = [{"type": "function", "name": name, "description": description,
          "parameters": {"type": "object", "properties": props,
                         "required": required, "additionalProperties": False}}
         for name, description, props, required in TOOL_SPECS]
VALIDATORS = {t["name"]: Draft202012Validator(t["parameters"]) for t in TOOLS}

HANDLERS: dict = {}  # end_call and transfer_to_human are special-cased inside
                     # Call.execute() itself, never dispatched through HANDLERS.
