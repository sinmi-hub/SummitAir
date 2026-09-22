"""One control socket per SIP call. Audio remains between Telnyx and OpenAI."""
from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
import json
import logging
import re
import sqlite3
import time
from urllib.parse import quote

import httpx
from jsonschema import Draft202012Validator, ValidationError
from websockets.asyncio.client import connect

from app.agent.persona import AGENT_GREETING, CLOSING_GOODBYE, system_prompt
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
        "turn_detection": {"type": "semantic_vad", "eagerness": "high",
                           "interrupt_response": interrupt_response},
        "transcription": {"model": "gpt-live-transcribe"},
    }


def acceptance(settings) -> dict:
    return {"type": "realtime", "model": "gpt-realtime-2.1", "instructions": system_prompt(),
            "reasoning": {"effort": "medium"},
            "audio": {
                "output": {"voice": settings.openai_voice},
                # interrupt_response starts false: the greeting must play in full,
                # confirmed live -- it was getting cut short on effectively every
                # test call. Restored to true after the greeting's response.done
                # so normal barge-in works for the rest of the call.
                "input": audio_input_config(interrupt_response=False),
            },
            "tools": TOOLS, "tool_choice": "auto"}


class CallManager:
    def __init__(self, settings, http=None, socket_factory=connect):
        self.settings = settings
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
            await self.action(call_id, "accept", acceptance(self.settings))
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
                call = Call(self, call_id, ws)
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


class Call:
    def __init__(self, manager, call_id, ws):
        self.manager, self.call_id, self.ws = manager, call_id, ws
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
        self.emergency_declared = False
        self.last_event = None
        self.last_event_at = None
        self.saw_error_event = False

    async def send(self, message):
        await self.ws.send(json.dumps(message))

    async def continue_response(self):
        if self.needs_response and not self.response_active and not self.pending and not self.ending:
            self.needs_response = False
            self.response_active = True
            await self.send({"type": "response.create"})

    async def run(self):
        self.response_active = True
        await self.send({"type": "response.create", "response": {
            "instructions": "Say exactly this greeting, then listen: " + AGENT_GREETING,
            "tool_choice": "none",
        }})
        try:
            async for raw in self.ws:
                event = json.loads(raw)
                await self.event(event)
                if self.ending:
                    break
        finally:
            tasks = list(self.pending)
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    async def event(self, event):
        kind = event.get("type")
        self.last_event, self.last_event_at = kind, time.monotonic()
        if kind == "error":
            self.saw_error_event = True
        if kind == "response.created":
            self.response_active = True
            self.playback_idle.clear()
        elif kind == "response.done":
            self.response_active = False
            if not self.speaking:
                self.playback_idle.set()
            if self.greeting_protected:
                # The greeting (the very first response) just finished playing in
                # full, uninterrupted. Restore normal barge-in for the rest of the
                # call. Any real caller turn that landed during the greeting still
                # gets answered -- create_response stays on, the server's attempt
                # to auto-respond to it while our greeting response was active
                # returns conversation_already_has_active_response, which
                # continue_response()'s needs_response retry already handles.
                self.greeting_protected = False
                await self.send({"type": "session.update",
                                  "session": {"type": "realtime",
                                              "audio": {"input": audio_input_config(True)}}})
            await self.continue_response()
        elif kind == "output_audio_buffer.started":
            self.speaking = True
            self.playback_idle.clear()
            if self.speech_stopped_at is not None:
                log.info("call=%s speech_stop_to_output_start_ms=%.0f", self.call_id,
                         (time.monotonic() - self.speech_stopped_at) * 1000)
                self.speech_stopped_at = None
        elif kind in {"output_audio_buffer.stopped", "output_audio_buffer.cleared"}:
            if kind == "output_audio_buffer.cleared" and self.speaking:
                log.info("call=%s agent_speech_interrupted=true", self.call_id)
            self.speaking = False
            self.agent_audio_stopped_at = time.monotonic()
            if not self.response_active:
                self.playback_idle.set()
        elif kind == "input_audio_buffer.speech_started":
            # No amplitude gate exists on semantic_vad (unlike server_vad's
            # threshold), so this can fire on a low-level artifact rather than
            # real speech. Logged only; nothing acts on it. during_agent_speech
            # separates leaked playback from a false trigger on true silence;
            # those need different fixes.
            self.speech_started_audio_ms = event.get("audio_start_ms")
            since_agent_stopped = (
                None if self.agent_audio_stopped_at is None
                else "%.0f" % ((time.monotonic() - self.agent_audio_stopped_at) * 1000)
            )
            log.info("call=%s speech_started item=%s during_agent_speech=%s since_agent_stopped_ms=%s",
                      self.call_id, event.get("item_id"), self.speaking, since_agent_stopped)
        elif kind == "input_audio_buffer.speech_stopped":
            self.speech_stopped_at = time.monotonic()
            audio_end_ms = event.get("audio_end_ms")
            duration_ms = (
                None if self.speech_started_audio_ms is None or audio_end_ms is None
                else audio_end_ms - self.speech_started_audio_ms
            )
            log.info("call=%s speech_stopped item=%s detected_duration_ms=%s",
                      self.call_id, event.get("item_id"), duration_ms)
        elif kind == "conversation.item.input_audio_transcription.completed":
            log.info("call=%s caller_said=%r", self.call_id, event.get("transcript", ""))
        elif kind == "response.output_audio_transcript.done":
            self.last_agent_text = event.get("transcript") or ""
            # Grounds the end_call goodbye gate below in what the emergency
            # instruction actually requires the model to say, not a guess.
            if "911" in self.last_agent_text:
                self.emergency_declared = True
            log.info("call=%s agent_said=%r", self.call_id, self.last_agent_text)
        elif kind == "response.function_call_arguments.done":
            invocation = event.get("call_id")
            if not isinstance(invocation, str) or invocation in self.seen:
                return
            self.seen.add(invocation)
            task = asyncio.create_task(self.tool(event))
            self.pending.add(task)
            task.add_done_callback(self.tool_finished)
        elif kind == "error":
            code = event.get("error", {}).get("code", "unknown")
            log.warning("call=%s realtime_error=%s", self.call_id, code)
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
            if name not in VALIDATORS:
                raise ValueError("unknown tool")
            VALIDATORS[name].validate(args)
            result = await self.execute(name, args)
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
        return any(phrase in text for phrase in
                    ("thank you for choosing", "have a great day",
                     "have an amazing day", "have a wonderful day"))

    async def execute(self, name, args):
        if name in {"transfer_to_human", "end_call"}:
            # response.done is generation completion, not audible playback completion.
            await asyncio.wait_for(self.playback_idle.wait(), timeout=30)
            if name == "end_call":
                # Confirmed live, three separate calls: the model reliably invokes
                # end_call after a generic "let me wrap this up" line instead of the
                # specific closing text CALL CLOSING requires. Rather than trust the
                # prompt again, say it ourselves before the hangup actually happens
                # -- skipped for an emergency close, where a goodbye is wrong.
                if not self.emergency_declared and not self._closing_said():
                    log.info("call=%s end_call_missing_goodbye=true", self.call_id)
                    await self.send({"type": "response.create", "response": {
                        "instructions": "Say exactly this and nothing else: " + CLOSING_GOODBYE,
                        "tool_choice": "none",
                    }})
                    self.response_active = True
                    self.playback_idle.clear()
                    await asyncio.wait_for(self.playback_idle.wait(), timeout=30)
                # Logged before hangup/close: closing the socket here races
                # Call.run()'s read loop, which can cancel this task's own
                # finally-block "tool=end_call" log before it runs.
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
        future = asyncio.get_running_loop().run_in_executor(self.manager.worker, HANDLERS[name], args)
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
