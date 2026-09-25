"""One control socket per SIP call. Audio remains between Telnyx and OpenAI."""
from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
import json
import logging
import re
import sqlite3
import time
from uuid import uuid4
from urllib.parse import quote

import httpx
from jsonschema import Draft202012Validator, ValidationError
from websockets.asyncio.client import connect

from app.agent.persona import AGENT_GREETING, CLOSING_GOODBYE, system_prompt
from app.research import Researcher
from app.tools import HANDLERS

log = logging.getLogger("summitair")
PHONE = {"type": "string", "pattern": r"^\+[1-9][0-9]{7,14}$"}
TOOL_SPECS = [
    ("lookup", "Find an existing customer by their confirmed callback number.",
     {"phone": PHONE}, ["phone"]),
    ("availability", "Get available appointments. Offer only returned slots.",
     {"days_ahead": {"type": "integer", "minimum": 1, "maximum": 14}}, []),
    ("book", "Book an offered slot only after caller agreement. Creates a new customer record "
     "if the phone number has no existing one. name becomes the calendar event's title; notes "
     "(the HVAC issue or reason for the visit) becomes its description -- keep them separate, "
     "don't combine into one string. Only booked:true confirms success. Never retry an unclear "
     "result; transfer for verification.",
     {"phone": PHONE, "slot_iso": {"type": "string", "minLength": 10, "maxLength": 64},
      "name": {"type": "string", "maxLength": 200},
      "notes": {"type": "string", "maxLength": 1000}},
     ["phone", "slot_iso", "name", "notes"]),
    ("reschedule", "Move an already-booked appointment to a different offered slot. Only for a "
     "caller who already has a confirmed booking and wants a different time; use book for a new "
     "appointment instead. Only rescheduled:true confirms success.",
     {"phone": PHONE, "slot_iso": {"type": "string", "minLength": 10, "maxLength": 64}},
     ["phone", "slot_iso"]),
    ("transfer_to_human", "After explaining the handoff, request transfer to the configured human. "
     "A requested transfer does not prove that anyone answered. No warm-handoff summary is supported.", {}, []),
    ("end_call", "End the call after fully speaking the closing or emergency instructions. "
     "If an interruption cut off a safety instruction before it finished, restate it in full "
     "first; do not call this while a required instruction is incomplete.", {}, []),
]
TOOLS = [{"type": "function", "name": name, "description": description,
          "parameters": {"type": "object", "properties": props,
                         "required": required, "additionalProperties": False}}
         for name, description, props, required in TOOL_SPECS]
VALIDATORS = {t["name"]: Draft202012Validator(t["parameters"]) for t in TOOLS}


def audio_input_config(interrupt_response: bool) -> dict:
    # Shared by acceptance() and the greeting-window session.update restore below,
    # so the two can never drift apart. session.update's merge granularity below
    # the top level isn't documented, so every caller sends this whole object
    # rather than relying on a partial nested patch preserving siblings.
    return {
        "noise_reduction": {"type": "far_field"},
        "turn_detection": {"type": "semantic_vad", "eagerness": "low",
                           "interrupt_response": interrupt_response},
        # gpt-live-transcribe takes the plural `languages`, never `language`.
        # Pinned to English: unclear audio was coming back as single Chinese characters.
        "transcription": {"model": "gpt-live-transcribe", "languages": ["en"]},
    }


def acceptance(settings, instructions=None, tools=None) -> dict:
    # instructions/tools default to SummitAir's own -- callers building a
    # different domain (see outbound/) pass their own explicitly; SummitAir's
    # existing call sites and tests, which never pass these, are unaffected.
    return {"type": "realtime", "model": "gpt-realtime-2.1",
            "instructions": instructions if instructions is not None else system_prompt(),
            "reasoning": {"effort": "medium"},
            "audio": {
                "output": {"voice": settings.openai_voice},
                # interrupt_response starts false: the greeting must play in full,
                # confirmed live -- it was getting cut short on effectively every
                # test call. Restored to true after the greeting's playback stops
                # so normal barge-in works for the rest of the call.
                "input": audio_input_config(interrupt_response=False),
            },
            "tools": tools if tools is not None else TOOLS, "tool_choice": "auto"}


class CallManager:
    def __init__(self, settings, http=None, socket_factory=connect, *,
                 instructions=None, tools=None, handlers=None, validators=None, greeting=None,
                 closing_goodbye=None, closing_phrases=None, research=None):
        self.settings = settings
        # Background research is written for SummitAir's inbound flow; outbound
        # passes research=False explicitly.
        self.research = settings.research_enabled if research is None else research
        # Same defaulting rationale as acceptance() above.
        self.instructions = instructions if instructions is not None else system_prompt()
        self.tools = tools if tools is not None else TOOLS
        self.handlers = handlers if handlers is not None else HANDLERS
        self.validators = validators if validators is not None else VALIDATORS
        self.greeting = greeting if greeting is not None else AGENT_GREETING
        self.closing_goodbye = closing_goodbye if closing_goodbye is not None else CLOSING_GOODBYE
        self.closing_phrases = closing_phrases if closing_phrases is not None else DEFAULT_CLOSING_PHRASES
        self.http = http or httpx.AsyncClient(
            base_url="https://api.openai.com/v1/",
            headers={"Authorization": f"Bearer {settings.openai_api_key}"},
            timeout=10,
        )
        self.socket_factory = socket_factory
        # Existing googleapiclient cached transports aren't thread-safe. One dedicated
        # worker preserves the adapters and serializes their I/O across calls.
        self.worker = ThreadPoolExecutor(max_workers=1, thread_name_prefix="google-tools")
        self.calls: dict[str, asyncio.Task] = {}
        self.db = sqlite3.connect(settings.call_state_path)
        self.db.execute("CREATE TABLE IF NOT EXISTS calls (id TEXT PRIMARY KEY, created REAL, active INTEGER NOT NULL DEFAULT 1)")
        self.db.commit()
        self.closing = False

    async def action(self, call_id: str, action: str, payload=None):
        response = await self.http.post(
            f"realtime/calls/{quote(call_id, safe='')}/{action}", json=payload,
        )
        response.raise_for_status()

    def incoming(self, call_id: str) -> bool:
        """Durably claim before acknowledging; don't accept a redelivered call twice."""
        if self.closing:
            return False
        if self.db.execute("SELECT 1 FROM calls WHERE id=?", (call_id,)).fetchone():
            return True
        if len(self.calls) >= 16:
            return False
        with self.db:
            self.db.execute("DELETE FROM calls WHERE created < ?", (time.time() - 4 * 86400,))
            self.db.execute("INSERT INTO calls (id, created) VALUES (?, ?)", (call_id, time.time()))
        task = asyncio.create_task(self.run(call_id))
        self.calls[call_id] = task
        task.add_done_callback(lambda _: self.calls.pop(call_id, None))
        return True

    async def run(self, call_id):
        started = time.monotonic()
        ws = None
        call = None
        try:
            await self.action(call_id, "accept", acceptance(self.settings, self.instructions, self.tools))
            log.info("call=%s accept_ms=%.0f", call_id, (time.monotonic() - started) * 1000)
            url = "wss://api.openai.com/v1/realtime?call_id=" + quote(call_id, safe="")
            # Retry only attachment setup. After a connected socket is lost we cannot
            # safely recover missed tool events; end the call instead of replaying work.
            for attempt in range(3):
                try:
                    ws = await self.socket_factory(
                        url, additional_headers={"Authorization": f"Bearer {self.settings.openai_api_key}"},
                        open_timeout=3, max_size=1024 * 1024,
                    )
                    break
                except Exception:
                    if attempt == 2:
                        raise
                    await asyncio.sleep(0.2)
            log.info("call=%s control_ready_ms=%.0f", call_id, (time.monotonic() - started) * 1000)
            try:
                call = Call(self, call_id, ws, greeting=self.greeting,
                           handlers=self.handlers, validators=self.validators,
                           closing_goodbye=self.closing_goodbye, closing_phrases=self.closing_phrases,
                           research=self.research)
                await call.run()
            finally:
                await ws.close()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            age = "%.0fs" % (time.monotonic() - call.last_event_at) if call and call.last_event_at else None
            response = getattr(exc, "response", None)
            # A close triggered by our own end_call/transfer handling (call.ending
            # already True) races Call.run()'s read loop against that same close
            # and surfaces as this same exception. Expected, not a failure.
            level = log.info if call and call.ending else log.error
            level(
                "call=%s control_closed=%s expected=%s message=%r "
                "rcvd=%r sent=%r rcvd_then_sent=%r "
                "ws_close_code=%r ws_close_reason=%r "
                "http_status=%r http_body=%r "
                "last_event=%r last_event_age=%r saw_realtime_error=%r",
                call_id, type(exc).__name__, bool(call and call.ending), str(exc),
                getattr(exc, "rcvd", None), getattr(exc, "sent", None),
                getattr(exc, "rcvd_then_sent", None),
                getattr(ws, "close_code", None) if ws else None,
                getattr(ws, "close_reason", None) if ws else None,
                getattr(response, "status_code", None),
                getattr(response, "body", b"")[:500] if response else None,
                call.last_event if call else None, age,
                call.saw_error_event if call else None,
            )
        finally:
            # Includes ambiguous acceptance failures, shutdown, and lost control.
            # No automatic acceptance/tool retries after an uncertain mutation.
            try:
                await self.action(call_id, "hangup")
            except Exception as exc:
                log.info("call=%s cleanup=%s", call_id, type(exc).__name__)
            else:
                with self.db:
                    self.db.execute("UPDATE calls SET active=0 WHERE id=?", (call_id,))

    async def recover(self):
        """End calls orphaned by a process crash; never replay their tools."""
        for (call_id,) in self.db.execute("SELECT id FROM calls WHERE active=1").fetchall():
            try:
                await self.action(call_id, "hangup")
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code not in {404, 410}:
                    raise
            with self.db:
                self.db.execute("UPDATE calls SET active=0 WHERE id=?", (call_id,))

    async def close(self):
        self.closing = True
        tasks = list(self.calls.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self.worker.shutdown(wait=False, cancel_futures=True)
        await self.http.aclose()
        self.db.close()


DEFAULT_CLOSING_PHRASES = ("thank you for choosing", "have a great day",
                           "have an amazing day", "have a wonderful day")


class Call:
    def __init__(self, manager, call_id, ws, *, greeting=None, handlers=None, validators=None,
                 closing_goodbye=None, closing_phrases=None, research=False):
        self.manager, self.call_id, self.ws = manager, call_id, ws
        self.researcher = Researcher(manager.settings, call_id) if research else None
        self.research_task = None
        self.research_item_id = None
        self.greeting = greeting if greeting is not None else AGENT_GREETING
        self.handlers = handlers if handlers is not None else HANDLERS
        self.validators = validators if validators is not None else VALIDATORS
        self.closing_goodbye = closing_goodbye if closing_goodbye is not None else CLOSING_GOODBYE
        self.closing_phrases = closing_phrases if closing_phrases is not None else DEFAULT_CLOSING_PHRASES
        self.pending: set[asyncio.Task] = set()
        self.seen: set[str] = set()
        self.bookings: set[tuple[str, str]] = set()
        self.offered_slots: set[str] = set()
        self.response_active = False
        self.needs_response = False
        self.speaking = False
        self.playback_idle = asyncio.Event()
        self.playback_idle.set()
        self.speech_stopped_at = None
        self.speech_started_audio_ms = None
        self.agent_audio_stopped_at = None
        self.ending = False
        self.transfer_attempted = False
        self.greeting_protected = True
        self.last_agent_text = ""
        self.last_agent_response_id = None
        self.playback_states = {}
        self.response_epochs = {}
        self.tagged_responses = {}
        self.playback_changed = asyncio.Event()
        self.speech_epoch = 0
        self.caller_speaking = False
        self.greeting_tag = None
        self.greeting_attempts = 0
        self.emergency_declared = False
        self.last_event = None
        self.last_event_at = None
        self.saw_error_event = False

    async def send(self, message):
        await self.ws.send(json.dumps(message))

    async def continue_response(self):
        if not self.needs_response:
            return
        # Diagnostics for post-tool dead air: name every gate holding a pending reply.
        gates = {"response_active": self.response_active, "pending_tools": bool(self.pending),
                 "ending": self.ending, "greeting_protected": self.greeting_protected,
                 "speaking": self.speaking, "caller_speaking": self.caller_speaking}
        blocked = [name for name, on in gates.items() if on]
        if blocked:
            log.info("call=%s response_held_by=%s", self.call_id, ",".join(blocked))
            return
        self.needs_response = False
        self.response_active = True
        log.info("call=%s response_requested", self.call_id)
        await self.send({"type": "response.create"})

    async def request_greeting(self):
        self.greeting_attempts += 1
        self.greeting_tag = uuid4().hex
        self.response_active = True
        await self.send({"type": "response.create", "response": {
            "metadata": {"playback_gate": self.greeting_tag},
            "instructions": "Say exactly this greeting, then listen: " + self.greeting,
            "tool_choice": "none",
        }})

    async def update_greeting_gate(self):
        response_id = self.tagged_responses.get(self.greeting_tag)
        state = self.playback_states.get(response_id)
        if not self.greeting_protected or state not in {"stopped", "cleared", "failed"}:
            return
        if state != "stopped":
            if self.response_active:
                return
            if self.greeting_attempts >= 2:
                raise RuntimeError("Greeting playback could not complete")
            log.info("call=%s greeting_retry response_id=%s reason=%s", self.call_id, response_id, state)
            await self.request_greeting()
            return
        self.greeting_protected = False
        log.info("call=%s greeting_playback_complete response_id=%s", self.call_id, response_id)
        await self.send({"type": "session.update", "session": {
            "type": "realtime", "audio": {"input": audio_input_config(True)},
        }})
        await self.continue_response()

    async def wait_for_goodbye(self, response_id, tag, speech_epoch):
        async with asyncio.timeout(30):
            while True:
                self.playback_changed.clear()
                if self.speech_epoch != speech_epoch or self.caller_speaking:
                    return False
                target = self.tagged_responses.get(tag) if tag else response_id
                state = self.playback_states.get(target)
                if state in {"stopped", "cleared", "failed"}:
                    return state == "stopped"
                await self.playback_changed.wait()

    async def inject_research(self, text):
        # Context only: no response.create, so this never makes the agent speak.
        # One current case file -- the previous one is deleted, not stacked.
        if self.ending or self.emergency_declared:
            return
        previous, self.research_item_id = self.research_item_id, "research_" + uuid4().hex[:16]
        if previous:
            await self.send({"type": "conversation.item.delete",
                             "event_id": "research-" + uuid4().hex[:16], "item_id": previous})
        await self.send({"type": "conversation.item.create",
                         "event_id": "research-" + uuid4().hex[:16], "item": {
                             "id": self.research_item_id, "type": "message", "role": "system",
                             "content": [{"type": "input_text", "text": text}]}})
        # Everything after the fixed header line, so the log shows exactly what Aria saw.
        log.info("call=%s research_injected item=%s case_file=%r", self.call_id,
                 self.research_item_id, text.split("\n", 1)[-1])

    def stop_research(self):
        if self.research_task:
            self.research_task.cancel()

    async def run(self):
        await self.request_greeting()
        if self.researcher:
            self.research_task = asyncio.create_task(self.researcher.run(self.inject_research))
        try:
            async for raw in self.ws:
                event = json.loads(raw)
                await self.event(event)
                if self.ending:
                    break
        finally:
            # Every exit path -- end_call, transfer, hangup, dropped socket,
            # shutdown -- lands here, so research can never outlive its call.
            tasks = list(self.pending) + ([self.research_task] if self.research_task else [])
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            if self.researcher:
                await self.researcher.close()

    async def event(self, event):
        kind = event.get("type")
        self.last_event, self.last_event_at = kind, time.monotonic()
        if kind == "error":
            self.saw_error_event = True
        if kind == "response.created":
            self.response_active = True
            self.playback_idle.clear()
            response = event.get("response", {})
            response_id = response.get("id")
            if response_id:
                self.response_epochs[response_id] = self.speech_epoch
                tag = (response.get("metadata") or {}).get("playback_gate")
                if tag:
                    self.tagged_responses[tag] = response_id
                self.playback_changed.set()
        elif kind == "response.done":
            self.response_active = False
            response = event.get("response", {})
            details = response.get("status_details") or {}
            log.info("call=%s response_done id=%s status=%s reason=%s outputs=%s", self.call_id,
                     response.get("id"), response.get("status"),
                     details.get("reason") or (details.get("error") or {}).get("code"),
                     ",".join(item.get("type", "?") for item in response.get("output") or []) or "none")
            if response.get("id") and response.get("status") in {"cancelled", "failed", "incomplete"}:
                self.playback_states[response["id"]] = "failed"
                self.playback_changed.set()
            if not self.speaking:
                self.playback_idle.set()
            await self.update_greeting_gate()
            await self.continue_response()
        elif kind == "output_audio_buffer.started":
            response_id = event.get("response_id")
            log.info("call=%s playback=started response_id=%s", self.call_id, response_id)
            if response_id:
                self.playback_states.setdefault(response_id, "started")
            self.speaking = True
            self.playback_idle.clear()
            if self.speech_stopped_at is not None:
                log.info("call=%s speech_stop_to_output_start_ms=%.0f", self.call_id,
                         (time.monotonic() - self.speech_stopped_at) * 1000)
                self.speech_stopped_at = None
        elif kind in {"output_audio_buffer.stopped", "output_audio_buffer.cleared"}:
            response_id = event.get("response_id")
            state = kind.rsplit(".", 1)[1]
            log.info("call=%s playback=%s response_id=%s", self.call_id, state, response_id)
            if response_id and self.playback_states.get(response_id) not in {"cleared", "failed"}:
                self.playback_states[response_id] = state
            self.playback_changed.set()
            if kind == "output_audio_buffer.cleared" and self.speaking:
                log.info("call=%s agent_speech_interrupted=true", self.call_id)
            self.speaking = False
            self.agent_audio_stopped_at = time.monotonic()
            if not self.response_active:
                self.playback_idle.set()
            await self.update_greeting_gate()
            await self.continue_response()
        elif kind == "input_audio_buffer.speech_started":
            self.speech_epoch += 1
            self.caller_speaking = True
            self.playback_changed.set()
            # No amplitude gate exists on semantic_vad (unlike server_vad's
            # threshold), so this can fire on a low-level artifact rather than
            # real speech. Conservatively defer a pending goodbye on any new
            # speech signal. during_agent_speech helps diagnose false triggers.
            self.speech_started_audio_ms = event.get("audio_start_ms")
            since_agent_stopped = (
                None if self.agent_audio_stopped_at is None
                else "%.0f" % ((time.monotonic() - self.agent_audio_stopped_at) * 1000)
            )
            log.info("call=%s speech_started item=%s during_agent_speech=%s since_agent_stopped_ms=%s",
                      self.call_id, event.get("item_id"), self.speaking, since_agent_stopped)
        elif kind == "input_audio_buffer.speech_stopped":
            self.caller_speaking = False
            self.speech_stopped_at = time.monotonic()
            audio_end_ms = event.get("audio_end_ms")
            duration_ms = (
                None if self.speech_started_audio_ms is None or audio_end_ms is None
                else audio_end_ms - self.speech_started_audio_ms
            )
            log.info("call=%s speech_stopped item=%s detected_duration_ms=%s",
                      self.call_id, event.get("item_id"), duration_ms)
        elif kind == "conversation.item.input_audio_transcription.completed":
            log.info("call=%s transcript_ref=caller item_id=%s", self.call_id, event.get("item_id"))
            log.info("call=%s caller_said=%r", self.call_id, event.get("transcript", ""))
            if self.researcher:
                self.researcher.heard("customer", event.get("transcript") or "")
        elif kind == "response.output_audio_transcript.done":
            self.last_agent_text = event.get("transcript") or ""
            self.last_agent_response_id = event.get("response_id")
            log.info("call=%s transcript_ref=agent response_id=%s item_id=%s", self.call_id,
                     self.last_agent_response_id, event.get("item_id"))
            # Grounds the end_call goodbye gate below in what the emergency
            # instruction actually requires the model to say, not a guess.
            if "911" in self.last_agent_text:
                self.emergency_declared = True
                self.stop_research()
            log.info("call=%s agent_said=%r", self.call_id, self.last_agent_text)
            if self.researcher:
                used = self.researcher.used_in(self.last_agent_text)
                if used:
                    log.info("call=%s case_file_used=%s item=%s", self.call_id,
                             ",".join(used), self.research_item_id)
                self.researcher.heard("agent", self.last_agent_text)
        elif kind == "response.function_call_arguments.done":
            invocation = event.get("call_id")
            if not isinstance(invocation, str) or invocation in self.seen:
                return
            self.seen.add(invocation)
            log.info("call=%s tool_requested=%s response_id=%s invocation=%s", self.call_id,
                     event.get("name"), event.get("response_id"), invocation)
            task = asyncio.create_task(self.tool(event))
            self.pending.add(task)
            task.add_done_callback(self.tool_finished)
        elif kind == "error":
            error = event.get("error") or {}
            code = error.get("code", "unknown")
            log.warning("call=%s realtime_error=%s event_id=%s message=%r", self.call_id, code,
                        error.get("event_id"), error.get("message"))
            if str(error.get("event_id") or "").startswith("research-"):
                return  # Research is best-effort; a rejected injection never ends a call.
            if code == "conversation_already_has_active_response":
                self.response_active = True
                self.needs_response = True
            else:
                raise RuntimeError("Realtime session error")
        # Transcript text (from OpenAI's own transcription, not decoded here) is
        # logged for live debugging; raw audio payloads are still never touched.

    def tool_finished(self, task):
        self.pending.discard(task)
        if not task.cancelled() and task.exception():
            log.error("call=%s tool_delivery_failed=%s", self.call_id,
                      type(task.exception()).__name__)

    async def tool(self, event):
        name = event.get("name")
        started = time.monotonic()
        try:
            args = json.loads(event.get("arguments", ""))
            if name not in self.validators:
                raise ValueError("unknown tool")
            self.validators[name].validate(args)
            result = await self.execute(name, args, response_id=event.get("response_id"))
        except (ValueError, ValidationError, TypeError):
            result = {"error": "Invalid tool arguments. Correct the request before proceeding."}
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.warning("call=%s tool=%s failed=%s", self.call_id, name, type(exc).__name__)
            result = {"error": "Action failed or its result is unknown. Do not claim success or retry "
                      "a booking/transfer. Offer human assistance. Callback recording is unavailable."}
        try:
            if not self.ending:
                await self.send({"type": "conversation.item.create", "item": {
                    "type": "function_call_output", "call_id": event["call_id"],
                    "output": json.dumps(result),
                }})
                self.needs_response = True
        except Exception:
            await self.ws.close()
            raise
        finally:
            self.pending.discard(asyncio.current_task())
            log.info("call=%s tool=%s duration_ms=%.0f", self.call_id, name,
                     (time.monotonic() - started) * 1000)
        try:
            await self.continue_response()
        except Exception:
            await self.ws.close()

    def _closing_said(self) -> bool:
        text = self.last_agent_text.lower()
        return any(phrase in text for phrase in self.closing_phrases)

    async def execute(self, name, args, *, response_id=None):
        if name in {"transfer_to_human", "end_call"}:
            speech_epoch = self.speech_epoch
            # response.done is generation completion, not audible playback completion.
            await asyncio.wait_for(self.playback_idle.wait(), timeout=30)
            if name == "end_call":
                if (self.caller_speaking or self.speech_epoch != speech_epoch
                        or self.playback_states.get(response_id) in {"cleared", "failed"}
                        or (response_id in self.response_epochs
                            and self.response_epochs[response_id] != speech_epoch)):
                    return {"ended": False, "reason": "Caller resumed speaking; respond before ending the call."}
                target = self.last_agent_response_id
                tag = None
                closing_is_current = (target is not None and self.response_epochs.get(target) == speech_epoch
                                      and (self._closing_said() or "911" in self.last_agent_text))
                if not closing_is_current:
                    log.info("call=%s end_call_missing_goodbye=true", self.call_id)
                    tag = uuid4().hex
                    self.response_active = True
                    self.playback_idle.clear()
                    await self.send({"type": "response.create", "response": {
                        "metadata": {"playback_gate": tag},
                        "instructions": "Say exactly this and nothing else: " + self.closing_goodbye,
                        "tool_choice": "none",
                    }})
                try:
                    completed = await self.wait_for_goodbye(target, tag, speech_epoch)
                except TimeoutError:
                    log.warning("call=%s goodbye_playback_timeout=true", self.call_id)
                    return {"ended": False, "reason": "Goodbye playback was not confirmed. Do not claim the call ended."}
                if not completed:
                    log.info("call=%s end_call_deferred=interrupted", self.call_id)
                    return {"ended": False, "reason": "Goodbye interrupted; respond to the caller before ending."}
                log.info("call=%s goodbye_playback_complete response_id=%s", self.call_id,
                         self.tagged_responses.get(tag) if tag else target)
                log.info("call=%s invoking=end_call", self.call_id)
                await self.manager.action(self.call_id, "hangup")
                self.ending = True
                await self.ws.close()
                return {"ended": True}
            number = self.manager.settings.human_transfer_number
            if not number or not re.fullmatch(PHONE["pattern"], number):
                return {"transferred": False, "error": "Human transfer is not configured. Callback recording is unavailable."}
            if self.transfer_attempted:
                return {"transferred": False, "error": "Transfer already attempted; answer status is unknown. Do not retry."}
            self.transfer_attempted = True
            await self.manager.action(self.call_id, "refer", {"target_uri": "tel:" + number})
            return {"transfer_requested": True, "answer_status": "unknown",
                    "note": "The carrier owns transfer completion. Do not say a human answered."}
        if name in {"book", "reschedule"}:
            if args["slot_iso"] not in self.offered_slots:
                key = "booked" if name == "book" else "rescheduled"
                return {key: False, "error": "Check availability and offer a returned slot first."}
            booking = (args["phone"], args["slot_iso"])
            if booking in self.bookings:
                key = "booked" if name == "book" else "rescheduled"
                return {key: False, "error": "Already attempted this booking; transfer to verify, do not retry."}
            self.bookings.add(booking)
        future = asyncio.get_running_loop().run_in_executor(self.manager.worker, self.handlers[name], args)
        # A timed-out Google mutation may still finish in its worker. Never auto-retry.
        try:
            result = await asyncio.wait_for(asyncio.shield(future), timeout=20)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            # Cancel queued work; a write already running in the thread may finish.
            future.cancel()
            raise
        if name == "availability":
            self.offered_slots = {slot["iso"] for slot in result["slots"]}
        return result
