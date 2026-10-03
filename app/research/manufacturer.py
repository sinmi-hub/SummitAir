"""Manufacturer troubleshooting guidance for the case file: 
 - Schema: Haiku response and parsing structure
 - Prompt: System prompt for Aria governing it
 - Render:."""

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

PROMPT = """Manufacturer guidance (inside research_suggests) is for equipment that is reporting something itself: a blinking or colored light, an error code, a message on the display, or a beeping alarm. For those, search for the manufacturer's own guide for that brand and symptom. Fill manufacturer_guidance only from a result that matches this brand and symptom, name the guide in source, and keep only the steps it says a homeowner can do: the filter, the breaker, the thermostat, a reset. Leave out any step that opens a panel, touches wiring or involves gas; an unsafe or wrong step costs far more than an empty slot. A comfort complaint with no signal ("it isn't heating") has many possible causes, so leave guidance empty and don't search for it."""


def section(research: dict) -> tuple[str, list[str]]:
    guidance = research.get("manufacturer_guidance") or {}
    source, steps = guidance.get("source", ""), guidance.get("steps", [])
    # Trimmed here, not in the schema, like the rest of the case file.
    items = [f"- {step[:200]}" for step in steps] if source and steps else []
    return (f"Manufacturer guidance (from {source[:100]}; credit the manufacturer and relay it only "
            "during the Manufacturer check):", items)
