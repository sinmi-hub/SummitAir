"""Manufacturer troubleshooting guidance for the case file: the schema Haiku fills and
how it renders for Aria. The instructions to Haiku live in WATCHER-PROMPT.md."""

GUIDANCE = {
    "type": "object", 
    "additionalProperties": False, 
    "required": ["source", "steps"],
    "properties": {
        "source": {"type": "string",
                   "description": "The manufacturer guide the steps come from, e.g. 'Carrier furnace manual'. "
                                  "Empty string if none."},
        "steps": {"type": "array", "items": {"type": "string"},
                  "description": "Steps from that guide a homeowner can safely do, in order. Empty if none."},
    },
}

def section(research: dict) -> tuple[str, list[str]]:
    guidance = research.get("manufacturer_guidance") or {}
    source, steps = guidance.get("source", ""), guidance.get("steps", [])
    # Trimmed here, not in the schema, like the rest of the case file.
    items = [f"- {step[:200]}" for step in steps] if source and steps else []
    return (f"Manufacturer guidance (from {source[:100]}; credit the manufacturer and relay it only "
            "during the Manufacturer check):", items)
