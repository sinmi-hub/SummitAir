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

from app.agent.persona import AGENT_GREETING, system_prompt
from app.tools import HANDLERS

log = logging.getLogger("summitair")
PHONE = {"type": "string", "pattern": r"^\+[1-9][0-9]{7,14}$"}
TOOL_SPECS = [
    ("lookup", "Find an existing customer by their confirmed callback number.",
     {"phone": PHONE}, ["phone"]),
    ("availability", "Get available appointments. Offer only returned slots.",
     {"days_ahead": {"type": "integer", "minimum": 1, "maximum": 14}}, []),
    ("book", "Book an offered slot only after caller agreement. Requires an existing sheet row. "
     "Only booked:true confirms success. Never retry an unclear result; transfer for verification.",
     {"phone": PHONE, "slot_iso": {"type": "string", "minLength": 10, "maxLength": 64},
      "summary": {"type": "string", "maxLength": 1000}}, ["phone", "slot_iso"]),
    ("transfer_to_human", "After explaining the handoff, request transfer to the configured human. "
     "A requested transfer does not prove that anyone answered. No warm-handoff summary is supported.", {}, []),
    ("end_call", "End the call after speaking the closing or emergency instructions.", {}, []),
]
TOOLS = [{"type": "function", "name": name, "description": description,
          "parameters": {"type": "object", "properties": props,
                         "required": required, "additionalProperties": False}}
         for name, description, props, required in TOOL_SPECS]
VALIDATORS = {t["name"]: Draft202012Validator(t["parameters"]) for t in TOOLS}


def acceptance(settings) -> dict:
    return {"type": "realtime", "model": "gpt-realtime", "instructions": system_prompt(),
            "audio": {"output": {"voice": settings.openai_voice}}, "tools": TOOLS,
            "tool_choice": "auto"}


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
                        open_timeout=10, max_size=1024 * 1024,
                    )
                    break
                except Exception:
                    if attempt == 2:
                        raise
                    await asyncio.sleep(0.5 * (attempt + 1))
            log.info("call=%s control_ready_ms=%.0f", call_id, (time.monotonic() - started) * 1000)
            try:
                await Call(self, call_id, ws).run()
            finally:
                await ws.close()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            detail = ""
            frame = getattr(exc, "rcvd", None) or getattr(exc, "sent", None)
            if frame is not None:
                detail = " close_code=%s close_reason=%r" % (frame.code, frame.reason)
            log.error("call=%s control_failed=%s%s", call_id, type(exc).__name__, detail)
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
        self.ending = False
        self.transfer_attempted = False

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
        if kind == "response.created":
            self.response_active = True
            self.playback_idle.clear()
        elif kind == "response.done":
            self.response_active = False
            if not self.speaking:
                self.playback_idle.set()
            await self.continue_response()
        elif kind == "output_audio_buffer.started":
            self.speaking = True
            self.playback_idle.clear()
            if self.speech_stopped_at is not None:
                log.info("call=%s speech_stop_to_output_start_ms=%.0f", self.call_id,
                         (time.monotonic() - self.speech_stopped_at) * 1000)
                self.speech_stopped_at = None
        elif kind in {"output_audio_buffer.stopped", "output_audio_buffer.cleared"}:
            self.speaking = False
            if not self.response_active:
                self.playback_idle.set()
        elif kind == "input_audio_buffer.speech_stopped":
            self.speech_stopped_at = time.monotonic()
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
        # Never decode, store, log, forward, or send audio payloads.

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

    async def execute(self, name, args):
        if name in {"transfer_to_human", "end_call"}:
            # response.done is generation completion, not audible playback completion.
            await asyncio.wait_for(self.playback_idle.wait(), timeout=30)
            if name == "end_call":
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
        if name == "book":
            if args["slot_iso"] not in self.offered_slots:
                return {"booked": False, "error": "Check availability and offer a returned slot first."}
            booking = (args["phone"], args["slot_iso"])
            if booking in self.bookings:
                return {"booked": False, "error": "Already attempted this booking; transfer to verify, do not retry."}
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
