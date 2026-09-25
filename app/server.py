"""Signed OpenAI webhook receiver; run one Uvicorn worker on an always-on host."""
import asyncio
from contextlib import asynccontextmanager
from dataclasses import replace
import json
import logging
import re

from openai import OpenAI, InvalidWebhookSignatureError
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from config import settings
from app.realtime import CallManager, PHONE
from outbound import persona as outbound_persona
from outbound import state as outbound_state
from outbound import tools as outbound_tools
from outbound.webhook import cleanup, cleanup_loop, telnyx_webhook


def create_app(config=settings, manager_factory=CallManager):
    @asynccontextmanager
    async def lifespan(app):
        if not config.openai_api_key or not config.openai_webhook_secret:
            raise RuntimeError("OPENAI_API_KEY and OPENAI_WEBHOOK_SECRET are required")
        if config.human_transfer_number and not re.fullmatch(PHONE["pattern"], config.human_transfer_number):
            raise RuntimeError("HUMAN_TRANSFER_NUMBER must be an E.164 number")
        logging.basicConfig(level=logging.INFO)
        app.state.verifier = OpenAI(api_key=config.openai_api_key, webhook_secret=config.openai_webhook_secret)
        app.state.config = config
        app.state.outbound_state = outbound_state.Store(config.call_state_path)
        app.state.outbound_cleanup_lock = asyncio.Lock()
        app.state.calls = manager_factory(config)
        # A second manager, same machinery, different prompt/tools/greeting --
        # selected through a persisted outbound attempt, never caller-ID.
        # Separate call-state DB -- the two managers must never see each other's
        # in-flight calls in recover(), even though call_ids are globally unique.
        outbound_config = replace(config, call_state_path=config.call_state_path + ".outbound")
        app.state.outbound_calls = manager_factory(
            outbound_config,
            instructions=outbound_persona.system_prompt(),
            tools=outbound_tools.TOOLS,
            handlers=outbound_tools.HANDLERS,
            validators=outbound_tools.VALIDATORS,
            greeting=outbound_persona.GREETING,
            closing_goodbye=outbound_persona.CLOSING_GOODBYE,
            closing_phrases=outbound_persona.CLOSING_PHRASES,
            research=False,
        )
        cleanup_task = None
        try:
            await app.state.calls.recover()
            await app.state.outbound_calls.recover()
            app.state.outbound_state.recover()
            cleanup_task = asyncio.create_task(cleanup_loop(app))
            yield
        finally:
            if cleanup_task is not None:
                cleanup_task.cancel()
                await asyncio.gather(cleanup_task, return_exceptions=True)
            app.state.outbound_state.recover()
            await cleanup(app)
            await app.state.calls.close()
            await app.state.outbound_calls.close()
            app.state.verifier.close()

    async def health(request):
        return JSONResponse({"ok": True, "service": "summitair-sip-control"})

    async def webhook(request: Request):
        body = bytearray()
        async for chunk in request.stream():
            body.extend(chunk)
            if len(body) > 65536:
                return Response(status_code=413)
        try:
            # Verify the exact body and timestamp before parsing caller-controlled data.
            request.app.state.verifier.webhooks.verify_signature(bytes(body), request.headers)
            event = json.loads(body)
            if not isinstance(event, dict):
                raise ValueError("object required")
        except InvalidWebhookSignatureError:
            logging.warning("webhook rejected: invalid signature")
            return Response(status_code=400)
        except (ValueError, UnicodeDecodeError):
            logging.warning("webhook rejected: invalid JSON object")
            return Response(status_code=400)
        if event.get("type") != "realtime.call.incoming":
            return Response(status_code=204)
        data = event.get("data")
        call_id = data.get("call_id") if isinstance(data, dict) else None
        if not isinstance(call_id, str) or not 1 <= len(call_id) <= 256:
            logging.warning("signed incoming webhook rejected: missing or invalid call_id")
            return Response(status_code=400)
        headers = data.get("sip_headers", [])
        if not isinstance(headers, list) or any(not isinstance(h, dict) for h in headers):
            return Response(status_code=400)
        tokens = [h.get("value") for h in headers
                  if str(h.get("name", "")).lower() == outbound_state.HEADER.lower()]
        if len(tokens) > 1 or (tokens and (not isinstance(tokens[0], str) or not tokens[0])):
            return Response(status_code=400)
        try:
            domain = request.app.state.outbound_state.route(call_id, tokens[0] if tokens else None)
        except ValueError:
            return Response(status_code=403)
        manager = (request.app.state.outbound_calls if domain == "outbound"
                   else request.app.state.calls)
        accepted = manager.incoming(call_id)
        return Response(status_code=200 if accepted else 503)

    return Starlette(routes=[
        Route("/health", health),
        Route("/webhooks/openai", webhook, methods=["POST"]),
        Route("/webhooks/telnyx-outbound", telnyx_webhook, methods=["POST"]),
    ],
                     lifespan=lifespan)


app = create_app()
