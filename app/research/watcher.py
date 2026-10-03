"""Background research for one call. Haiku reads the live transcript, Exa searches the web, and the result reaches the voice model only as context 
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from pathlib import Path

import httpx
from jsonschema import Draft202012Validator

from app.research import manufacturer

log = logging.getLogger("summitair")
ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"
EXA_URL = "https://api.exa.ai/search"

def _slots(fields: dict) -> dict:
    return {"type": "object", "additionalProperties": False, "required": list(fields),
            "properties": {name: {"type": "string", "description": text + " Empty string if not known."}
                           for name, text in fields.items()}}


# Where a detail came from is part of the case file's shape, not a rule Haiku has to
# remember: what the customer said and what research suggests live in separate slots.
CUSTOMER_SAID = _slots({
    "issue": "The HVAC problem in the customer's own terms.",
    "equipment": "Brand, model or system type the customer named.",
    "address": "Service address as the customer gave it.",
    "zip": "ZIP code as the customer gave it.",
    "phone": "The number the customer gave to reach them, as digits.",
    "property_type": "What the customer said about the property (home, apartment, business...).",
    "unit_number": "Unit or apartment number the customer gave, or what they said about it.",
})
RESEARCH_SUGGESTS = _slots({
    "property_type": "residential, apartment, commercial or unknown -- from a result about this property.",
    "unit_number_needed": "yes, no or unknown -- whether this building has separate units.",
    "zip": "ZIP code a result gives for this address.",
})
RESEARCH_SUGGESTS["properties"]["notes"] = {"type": "array", "items": {"type": "string"},
                                            "description": "Other short facts about this property or equipment."}
RESEARCH_SUGGESTS["required"].append("notes")
RESEARCH_SUGGESTS["properties"]["manufacturer_guidance"] = manufacturer.GUIDANCE
RESEARCH_SUGGESTS["required"].append("manufacturer_guidance")
CASE = {"type": "object", "additionalProperties": False, "required": ["customer_said", "research_suggests", "check"],
        "properties": {
            "customer_said": CUSTOMER_SAID,
            "research_suggests": RESEARCH_SUGGESTS,
            "check": {"type": "array", "items": {
                "type": "object", "additionalProperties": False, "required": ["detail", "reason"],
                "properties": {"detail": {"type": "string"}, "reason": {"type": "string"}}}},
        }}
UPDATE_TOOL = {
    "name": "update_case",
    "description": "Record the updated case file and, optionally, one new web search to run.",
    "input_schema": {"type": "object", "additionalProperties": False, "properties": {
        "case": CASE,
        "search_query": {"type": "string", "description": "One new web search, or empty for none."},
    }, "required": ["case", "search_query"]},
}
VALIDATE = Draft202012Validator(UPDATE_TOOL["input_schema"])

WATCHER_PROMPT = Path(__file__).with_name("WATCHER-PROMPT.md").read_text(encoding="utf-8").rstrip()


def render(case: dict) -> str:
    said, research = case.get("customer_said", {}), case.get("research_suggests", {})
    said_labels = [("issue", "Issue"), ("equipment", "Equipment"), ("address", "Address"), ("zip", "ZIP"),
                   ("phone", "Number to reach them"), ("property_type", "Property"), ("unit_number", "Unit")]
    research_labels = [("property_type", "Property type"), ("unit_number_needed", "Unit number needed"),
                       ("zip", "ZIP")]
    # Lengths are trimmed here, not enforced in the schema: an over-long string from
    # Haiku should cost a few characters, never the whole pass.
    sections = [
        ("The customer said:", [f"- {label}: {said[key][:200]}" for key, label in said_labels
                                if said.get(key)]),
        ("Research suggests (the customer hasn't confirmed these):",
         [f"- {label}: {research[key][:200]}" for key, label in research_labels
          if research.get(key) and research[key] != "unknown"]
         + [f"- {note[:200]}" for note in research.get("notes", [])]),
        manufacturer.section(research),
        ("Check with the customer (ask about these specifically):",
         [f"- {c['detail'][:120]}: {c['reason'][:200]}" for c in case.get("check", [])]),
    ]
    lines = [line for title, items in sections if items for line in [title, *items]]
    if not lines:
        return ""  # nothing learned yet -- don't put an empty case file in front of Aria
    return ("Background case file, built automatically from the call transcript and a web search. "
            "Research can be wrong, so treat it as unconfirmed until the customer says yes. Confirm "
            "details from it instead of asking for them, which keeps the call short. Never state it "
            "as fact, diagnose from it, or mention searching. The customer's own words always win.\n"
            + "\n".join(lines))


class Researcher:
    def __init__(self, settings, call_id: str, http: httpx.AsyncClient | None = None):
        self.settings, self.call_id = settings, call_id
        self.http = http or httpx.AsyncClient(timeout=30)
        self.transcript: list[tuple[str, str]] = []
        self.case: dict = {}
        self.queries: list[str] = []
        self.changed = asyncio.Event()
        self.agent_lines = 0
        self.shown: dict[str, int] = {}  # check detail -> agent lines spoken when it was first shown
        self.answered: set[str] = set()  # checks the customer has spoken to since Aria saw them

    def heard(self, role: str, text: str):
        # Only customer lines start a pass; agent lines ride along as context on the
        # next one, which halves Haiku calls without losing anything.
        if text and text.strip():
            self.transcript.append((role, text.strip()))
            if role == "agent":
                self.agent_lines += 1
            if role == "customer":
                # A fact for Haiku, not a filter: Aria has had a turn with the check since it
                # was shown, and the customer has now spoken.
                self.answered |= {d for d, shown_at in self.shown.items() if self.agent_lines > shown_at}
                self.changed.set()

    async def run(self, inject):
        # One pass at a time. Lines that arrive during a pass only set the event,
        # so the next pass reads the whole, longer transcript -- nothing is dropped.
        while True:
            await self.changed.wait()
            self.changed.clear()
            started = time.monotonic()
            try:
                update = await self.step(inject)
                if update:
                    await inject(update)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning("call=%s research_failed=%s", self.call_id, type(exc).__name__)
                continue
            log.info("call=%s research_ms=%.0f injected=%s", self.call_id,
                     (time.monotonic() - started) * 1000, bool(update))

    async def step(self, inject=None) -> str | None:
        transcript = list(self.transcript)
        case, query = await self.think(transcript)


        if query and query not in self.queries and len(self.queries) < self.settings.research_max_searches:
            self.queries.append(query)
            log.info("call=%s research_query=%r", self.call_id, query)
            results = await self.search(query)
            if inject and results:
                # Aria gets the raw highlights now; the fold pass below replaces them.
                await inject(self.found(results))
            case, _ = await self.think(transcript, (query, results))
        case = self.phone_check(case)
        
        if case == self.case:
            return None
        self.case = case

        for check in case.get("check", []):
            self.shown.setdefault(check["detail"], self.agent_lines)
        return render(case) or None

    def found(self, results: list[dict]) -> str:
        lines = [f"- {r['title']}: {h[:200]}" for r in results[:3] for h in r["highlights"][:1]]
        case = render(self.case)
        return ("Search just found this, not yet checked against the call. Treat it as unconfirmed, "
                "never state it as fact, and never mention searching.\n" + "\n".join(lines)
                + ("\n\n" + case if case else ""))

    def phone_check(self, case: dict) -> dict:
        # The one objective check done in code: a phone number is 10 digits. Haiku missed
        # a 13-digit number even when it was given the exact digit count. Asked once, like any check.
        digits = re.sub(r"\D", "", case.get("customer_said", {}).get("phone", ""))
        check = {"detail": f"phone number {digits}", "reason": f"{len(digits)} digits; a phone number has 10"}
        if not digits or len(digits) == 10 or check["detail"] in self.answered:
            return case
        return {**case, "check": [c for c in case.get("check", []) if c["detail"] != check["detail"]] + [check]}

    def facts(self, transcript) -> str:
        checks = [f"- {detail!r}: " + ("the customer has answered it since Aria saw it; asking again repeats "
                                        "a question they already answered" if detail in self.answered
                                        else "not answered yet") for detail in self.shown] or ["- none yet"]
        return "\n\nCHECKS ALREADY SHOWN TO ARIA:\n" + "\n".join(checks)

    async def think(self, transcript, searched=None) -> tuple[dict, str]:
        content = ("TRANSCRIPT:\n" + "\n".join(f"{role.upper()}: {text}" for role, text in transcript)
                   + "\n\nCURRENT CASE FILE:\n" + json.dumps(self.case or "empty")
                   + "\n\nQUERIES ALREADY RUN:\n" + json.dumps(self.queries) + self.facts(transcript))
        if searched:
            content += f"\n\nSEARCH RESULTS for {searched[0]!r}:\n" + json.dumps(searched[1])
        response = await self.http.post(ANTHROPIC_URL, headers={
            "x-api-key": self.settings.anthropic_api_key, "anthropic-version": "2023-06-01",
            "anthropic-workspace-id": self.settings.anthropic_workspace_id,
        }, json={
            "model": self.settings.watcher_model, "max_tokens": 1024, "system": WATCHER_PROMPT,
            "tools": [UPDATE_TOOL], "tool_choice": {"type": "tool", "name": "update_case"},
            "messages": [{"role": "user", "content": content}],
        })
        response.raise_for_status()
        for block in response.json().get("content", []):
            if block.get("type") == "tool_use":
                VALIDATE.validate(block.get("input"))
                return block["input"]["case"], block["input"]["search_query"].strip()
        raise ValueError("watcher returned no case update")

    async def search(self, query: str) -> list[dict]:
        # Request shape per Exa's build-with-exa skill: bare highlights, nothing
        # decorative. numResults is deliberate -- fewer results keep Haiku's fold
        # pass small and fast.
        response = await self.http.post(EXA_URL, headers={"x-api-key": self.settings.exa_api_key}, json={
            "query": query, "type": self.settings.exa_search_type, "numResults": 5,
            "contents": {"highlights": True},
        })
        response.raise_for_status()
        return [{"title": r.get("title"), "url": r.get("url"), "highlights": r.get("highlights", [])}
                for r in response.json().get("results", [])]

    async def close(self):
        await self.http.aclose()


