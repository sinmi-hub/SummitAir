"""Background research for one call. Haiku reads the live transcript, Exa searches
the web, and the result reaches the voice model only as context -- never as a tool
call and never as a spoken response, so nothing here can pause the conversation."""
from __future__ import annotations

import asyncio
import json
import logging
import re
import time

import httpx
from jsonschema import Draft202012Validator

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

WATCHER_PROMPT = """You watch a live phone call to Summit Air, an HVAC company in the United States, and keep a case file for the voice agent, Aria. You never talk to the customer.

The case file lets Aria confirm details instead of asking for them. That shortens the call and shows the customer that Summit Air is listening. A wrong case file does the opposite: Aria confirms something false, the customer has to correct her, and their trust in Summit Air drops. Accuracy matters more than completeness; an empty slot only means Aria asks.

CUSTOMER lines come from speech recognition on a phone line. Unclear audio can come out as a single Chinese character (such as 嗯 or 说) or a stray fragment in another language; that is noise, not something the customer said. AGENT lines are exactly what Aria said.

The case file has three parts, each with its own job:
- customer_said holds what the customer told us, rebuilt from obvious mishearings. Aria reads these back, so a detail the customer never gave would sound like Summit Air wasn't listening.
- research_suggests holds what a web search found about this customer's own property or equipment. Aria uses it to confirm instead of ask, and to help when the customer isn't sure. A result about a different address or model is someone else's building, not a near match.
- check holds a detail the customer gave that looks wrong, with a short reason Aria can act on. Every check makes the customer repeat themselves, so raise one only when it would change the booking, and let it go once the customer has answered it.

Search when a new, specific fact appears that the web can add to: a property you can identify, or equipment with a symptom. Each search adds delay and uses one of only a few per call, and a query that could match many places returns someone else's property. Never search names or phone numbers. During an emergency (gas, fire, smoke, carbon monoxide), don't search: Aria's only job then is the customer's safety.

Each request also carries facts the system tracks exactly: the digit count of every number the customer said, and the checks Aria has already been shown, with whether the customer has answered since. Rely on them rather than recounting or guessing.

Always answer by calling update_case. The system reads only that tool call, so anything else is lost."""


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


DIGIT_WORDS = {"zero": "0", "oh": "0", "one": "1", "two": "2", "three": "3", "four": "4",
               "five": "5", "six": "6", "seven": "7", "eight": "8", "nine": "9"}
# One property type per group. A line naming two groups is the open question
# ("residential or commercial?"), not a use of the case file.
PROPERTY_WORDS = {"residential": ("single-family", "single family", "residential", "townhouse"),
                  "apartment": ("apartment", "condo"),
                  "commercial": ("commercial", "business", "office")}


def _digits(text: str) -> str:
    # "two zero seven zero seven" and "20707" both become "20707"; other words split runs.
    tokens = re.findall(r"[a-z]+|\d", text.lower())
    return "".join(DIGIT_WORDS.get(t, t) if t.isdigit() or t in DIGIT_WORDS else " " for t in tokens)


def _property_groups(text: str) -> set[str]:
    text = text.lower()
    return {kind for kind, words in PROPERTY_WORDS.items() if any(w in text for w in words)}


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

    def used_in(self, agent_text: str) -> list[str]:
        """Trace only: case-file details the agent just spoke that the customer never
        said -- the only place the agent could have learned them. Never feeds logic."""
        customer = " ".join(text for role, text in self.transcript if role == "customer")
        used = []
        research = self.case.get("research_suggests", {})
        zip_code = re.sub(r"\D", "", research.get("zip", ""))
        if len(zip_code) == 5 and zip_code in _digits(agent_text) and zip_code not in _digits(customer):
            used.append("zip")
        kind = research.get("property_type")
        if (kind in PROPERTY_WORDS and _property_groups(agent_text) == {kind}
                and kind not in _property_groups(customer)):
            used.append("property_type")
        return used

    async def run(self, inject):
        # One pass at a time. Lines that arrive during a pass only set the event,
        # so the next pass reads the whole, longer transcript -- nothing is dropped.
        while True:
            await self.changed.wait()
            self.changed.clear()
            started = time.monotonic()
            try:
                update = await self.step()
                if update:
                    await inject(update)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning("call=%s research_failed=%s", self.call_id, type(exc).__name__)
                continue
            log.info("call=%s research_ms=%.0f injected=%s", self.call_id,
                     (time.monotonic() - started) * 1000, bool(update))

    async def step(self) -> str | None:
        transcript = list(self.transcript)
        case, query = await self.think(transcript)
        if query and query not in self.queries and len(self.queries) < self.settings.research_max_searches:
            self.queries.append(query)
            log.info("call=%s research_query=%r", self.call_id, query)
            results = await self.search(query)
            case, _ = await self.think(transcript, (query, results))
        case = self.phone_check(case)
        if case == self.case:
            return None
        self.case = case
        for check in case.get("check", []):
            self.shown.setdefault(check["detail"], self.agent_lines)
        return render(case) or None

    def phone_check(self, case: dict) -> dict:
        # The one objective check done in code: a phone number is 10 digits. Haiku missed
        # a 13-digit number even with the exact count in its facts. Asked once, like any check.
        digits = re.sub(r"\D", "", case.get("customer_said", {}).get("phone", ""))
        check = {"detail": f"phone number {digits}", "reason": f"{len(digits)} digits; a phone number has 10"}
        if not digits or len(digits) == 10 or check["detail"] in self.answered:
            return case
        return {**case, "check": [c for c in case.get("check", []) if c["detail"] != check["detail"]] + [check]}

    def facts(self, transcript) -> str:
        said = _digits(" ".join(text for role, text in transcript if role == "customer"))
        numbers = dict.fromkeys(run for run in said.split() if len(run) >= 3)
        counts = [f"- {n}: {len(n)} digits" for n in numbers] or ["- none yet"]
        checks = [f"- {detail!r}: " + ("the customer has answered it since Aria saw it; asking again repeats "
                                        "a question they already answered" if detail in self.answered
                                        else "not answered yet") for detail in self.shown] or ["- none yet"]
        return ("\n\nDIGIT COUNTS (exact) of numbers the customer said; for reference, a phone number has 10 "
                "digits and a ZIP code has 5:\n" + "\n".join(counts)
                + "\n\nCHECKS ALREADY SHOWN TO ARIA:\n" + "\n".join(checks))

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


