import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from app.realtime import Call, CallManager
from app.research import Researcher, render
from tests.test_sip import Socket, config

SAID = {"issue": "", "equipment": "", "address": "", "zip": "", "phone": "", "property_type": "", "unit_number": ""}
RESEARCH = {"property_type": "unknown", "unit_number_needed": "unknown", "zip": "", "notes": []}


def case(check=(), notes=(), research=None, **said):
    return {"customer_said": {**SAID, **said}, "check": list(check),
            "research_suggests": {**RESEARCH, **(research or {}), "notes": list(notes)}}


EMPTY = case()


def haiku(query="", **kwargs):
    return httpx.Response(200, json={"content": [{"type": "tool_use", "name": "update_case",
                                                  "input": {"case": case(**kwargs), "search_query": query}}]})


class Backend:
    """Scripted Anthropic + Exa responses; records every request body."""
    def __init__(self, *haiku_responses):
        self.haiku, self.requests = list(haiku_responses), []

    def __call__(self, request):
        body = json.loads(request.content)
        self.requests.append((request.url.host, body))
        if request.url.host == "api.exa.ai":
            return httpx.Response(200, json={"results": [
                {"title": "4210 Oak Ln, Laurel, MD 20707", "url": "https://example.com/p",
                 "highlights": ["Single-family home"]}]})
        return self.haiku.pop(0)


def researcher(tmp_path, backend, **overrides):
    settings = config(tmp_path, research_enabled=True, anthropic_api_key="a", exa_api_key="e", **overrides)
    return Researcher(settings, "rtc_test", httpx.AsyncClient(transport=httpx.MockTransport(backend)))


@pytest.fixture
def call(tmp_path):
    manager = SimpleNamespace(settings=config(tmp_path, research_enabled=True), action=AsyncMock())
    c = Call(manager, "rtc_test", Socket(), research=True)
    c.greeting_protected = False
    return c


def test_render_separates_what_was_said_from_what_research_suggests():
    text = render(case(address="4210 Oak Ln", research={"property_type": "residential"},
                       notes=["Listed as a single-family home"]))
    assert "unconfirmed until the customer says yes" in text and "Never state it" in text
    said, research = text.split("The customer said:")[1].split("Research suggests")
    assert "- Address: 4210 Oak Ln" in said
    assert "- Property type: residential" in research and "- Listed as a single-family home" in research
    assert "Unit number" not in text and "Issue" not in text and "Check with" not in text


async def test_step_searches_then_folds_results_into_case(tmp_path):
    backend = Backend(haiku("4210 Oak Lane Laurel MD property", address="4210 Oak Ln, Laurel"),
                      haiku(address="4210 Oak Ln, Laurel",
                            research={"zip": "20707", "property_type": "residential", "unit_number_needed": "no"}))
    r = researcher(tmp_path, backend)
    r.heard("agent", "What's the service address?")
    r.heard("customer", "4210 Oak Lane in 说 Laurel")
    update = await r.step()
    hosts = [host for host, _ in backend.requests]
    assert hosts == ["api.anthropic.com", "api.exa.ai", "api.anthropic.com"]
    first, exa, fold = (body for _, body in backend.requests)
    assert first["tool_choice"] == {"type": "tool", "name": "update_case"}
    assert "CUSTOMER: 4210 Oak Lane in 说 Laurel" in first["messages"][0]["content"]
    assert exa["type"] == "fast" and exa["query"] == "4210 Oak Lane Laurel MD property"
    assert "SEARCH RESULTS" in fold["messages"][0]["content"]
    said, research = update.split("The customer said:")[1].split("Research suggests")
    assert "ZIP" not in said and "- ZIP: 20707" in research
    assert r.queries == ["4210 Oak Lane Laurel MD property"]
    await r.close()


async def test_unchanged_case_repeat_query_and_budget_do_not_search_or_inject(tmp_path):
    backend = Backend(haiku("q1", issue="no heat"), haiku(issue="no heat"),
                      haiku("q1", issue="no heat"), haiku("q2", issue="no heat"))
    r = researcher(tmp_path, backend, research_max_searches=1)
    r.heard("customer", "no heat")
    assert await r.step()
    assert await r.step() is None  # repeat query skipped, case unchanged
    assert await r.step() is None  # over budget
    assert [h for h, _ in backend.requests].count("api.exa.ai") == 1
    await r.close()


async def test_loop_coalesces_lines_and_survives_failures(tmp_path):
    gate = asyncio.Event()
    seen = []

    class Slow(Researcher):
        async def step(self):
            seen.append(len(self.transcript))
            if len(seen) == 1:
                await gate.wait()
                raise RuntimeError("watcher down")
            return "case"

    r = Slow(config(tmp_path), "rtc_test", httpx.AsyncClient())
    injected = []
    async def inject(text): injected.append(text)
    task = asyncio.create_task(r.run(inject))
    r.heard("customer", "one")
    await asyncio.sleep(0)
    r.heard("customer", "two")
    r.heard("customer", "three")
    r.heard("customer", "   ")  # blank ASR output is ignored
    gate.set()
    for _ in range(50):
        if injected: break
        await asyncio.sleep(0.005)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    assert seen == [1, 3] and injected == ["case"]
    await r.close()


async def test_transcripts_feed_the_researcher(call):
    await call.event({"type": "response.output_audio_transcript.done", "transcript": "What's going on?"})
    await call.event({"type": "conversation.item.input_audio_transcription.completed", "transcript": "嗯"})
    assert call.researcher.transcript == [("agent", "What's going on?"), ("customer", "嗯")]
    assert call.researcher.changed.is_set()


async def test_injection_replaces_context_and_never_requests_a_response(call):
    await call.inject_research("first")
    await call.inject_research("second")
    types = [m["type"] for m in call.ws.messages]
    assert types == ["conversation.item.create", "conversation.item.delete", "conversation.item.create"]
    first, delete, second = call.ws.messages
    assert first["item"]["role"] == "system" and first["item"]["content"][0]["text"] == "first"
    assert delete["item_id"] == first["item"]["id"] and second["item"]["id"] == call.research_item_id
    assert all(m["event_id"].startswith("research-") for m in call.ws.messages)
    assert not call.response_active and not call.needs_response


async def test_research_errors_never_end_the_call(call):
    await call.event({"type": "error", "error": {"code": "item_not_found", "event_id": "research-abc"}})
    with pytest.raises(RuntimeError):
        await call.event({"type": "error", "error": {"code": "item_not_found", "event_id": "evt_other"}})


async def test_emergency_stops_research_and_blocks_injection(call):
    call.research_task = asyncio.create_task(asyncio.sleep(10))
    await call.event({"type": "response.output_audio_transcript.done",
                      "transcript": "Please leave the building and call 911 now."})
    await asyncio.gather(call.research_task, return_exceptions=True)
    assert call.research_task.cancelled()
    await call.inject_research("case")
    assert not call.ws.messages


async def test_ending_blocks_injection(call):
    call.ending = True
    await call.inject_research("case")
    assert not call.ws.messages


async def test_call_teardown_cancels_research_and_closes_client(call):
    call.researcher.close = AsyncMock()
    await call.run()  # Socket() ends immediately, like a dropped or finished call
    assert call.research_task.cancelled()
    call.researcher.close.assert_awaited_once()


async def test_research_is_off_by_default_and_opt_out_per_manager(tmp_path):
    # Explicit research_enabled=False: Settings also reads the developer's local .env.
    off = config(tmp_path, research_enabled=False)
    manager = SimpleNamespace(settings=off, action=AsyncMock())
    c = Call(manager, "rtc_test", Socket())
    await c.run()
    assert c.researcher is None and c.research_task is None
    assert [m["type"] for m in c.ws.messages] == ["response.create"]
    assert CallManager(off).research is False
    assert CallManager(config(tmp_path, research_enabled=True), research=False).research is False
    assert CallManager(config(tmp_path, research_enabled=True)).research is True


def test_used_in_flags_only_details_the_customer_never_said(tmp_path):
    r = researcher(tmp_path, Backend())
    r.case = case(address="4210 Oak Ln", research={"zip": "20707", "property_type": "residential"})
    r.heard("customer", "4210 Oak Lane in Laurel")
    assert r.used_in("That's zip code two zero seven zero seven, a single-family home, right?") == [
        "zip", "property_type"]
    assert r.used_in("And that's 20707?") == ["zip"]
    # The open question names two groups, so it's not a use of the case file.
    assert r.used_in("Is this residential or commercial?") == []
    assert r.used_in("What's the best callback number?") == []
    r.heard("customer", "it's a single family house, zip 20707")
    assert r.used_in("That's a single-family home at 20707, right?") == []


async def test_case_file_use_is_logged(call, caplog):
    call.researcher.case = case(research={"zip": "20707"})
    call.research_item_id = "research_abc"
    with caplog.at_level("INFO", logger="summitair"):
        await call.event({"type": "response.output_audio_transcript.done",
                          "transcript": "Is the ZIP two zero seven zero seven?"})
    assert "case_file_used=zip item=research_abc" in caplog.text



def test_render_lists_checks_and_is_empty_when_nothing_is_known():
    assert render(EMPTY) == ""
    text = render(case(check=[{"detail": "phone 2021048899828", "reason": "13 digits; a US number has 10"}]))
    assert "Check with the customer" in text
    assert "- phone 2021048899828: 13 digits; a US number has 10" in text


async def test_empty_case_is_never_injected(tmp_path):
    r = researcher(tmp_path, Backend(haiku()))
    r.heard("customer", "嗯")
    assert await r.step() is None
    await r.close()


def test_only_customer_lines_start_a_pass(tmp_path):
    r = researcher(tmp_path, Backend())
    r.heard("agent", "How can I help you today?")
    assert not r.changed.is_set() and r.transcript == [("agent", "How can I help you today?")]
    r.heard("customer", "no heat")
    assert r.changed.is_set()


async def test_injection_logs_what_aria_saw(call, caplog):
    with caplog.at_level("INFO", logger="summitair"):
        await call.inject_research(render(case(zip="10003")))
    assert "case_file='The customer said:\\n- ZIP: 10003'" in caplog.text


def test_haiku_gets_exact_facts_instead_of_rules(tmp_path):
    r = researcher(tmp_path, Backend())
    r.heard("customer", "my number is two zero two one zero four eight eight nine nine eight two eight, zip 10100")
    zip_check = {"detail": "ZIP 10100", "reason": "search found 10003 for this address"}
    r.case = case(check=[zip_check])
    r.shown[zip_check["detail"]] = r.agent_lines
    facts = r.facts(r.transcript)
    assert "- 2021048899828: 13 digits" in facts and "- 10100: 5 digits" in facts
    assert "'ZIP 10100': not answered yet" in facts
    r.heard("agent", "I caught one zero one zero zero. Is that right?")
    r.heard("customer", "yes, 10100")
    assert "'ZIP 10100': the customer has answered it since Aria saw it" in r.facts(r.transcript)


def test_a_phone_number_that_is_not_ten_digits_is_flagged_once(tmp_path):
    r = researcher(tmp_path, Backend())
    for number in ("4439292703", ""):
        assert r.phone_check(case(phone=number))["check"] == []
    for number in ("2021048899828", "14439292703", "443929270"):
        [check] = r.phone_check(case(phone=number))["check"]
        assert check["reason"] == f"{len(number)} digits; a phone number has 10"
    r.answered.add("phone number 2021048899828")
    assert r.phone_check(case(phone="2021048899828"))["check"] == []  # answered: not raised again
