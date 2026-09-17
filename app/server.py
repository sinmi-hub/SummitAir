"""Signed OpenAI webhook receiver; run one Uvicorn worker on an always-on host."""
from contextlib import asynccontextmanager
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


def create_app(config=settings, manager_factory=CallManager):
    @asynccontextmanager
    async def lifespan(app):
        if not config.openai_api_key or not config.openai_webhook_secret:
            raise RuntimeError("OPENAI_API_KEY and OPENAI_WEBHOOK_SECRET are required")
        if config.human_transfer_number and not re.fullmatch(PHONE["pattern"], config.human_transfer_number):
            raise RuntimeError("HUMAN_TRANSFER_NUMBER must be an E.164 number")
        logging.basicConfig(level=logging.INFO)
        app.state.verifier = OpenAI(api_key=config.openai_api_key, webhook_secret=config.openai_webhook_secret)
        app.state.calls = manager_factory(config)
        try:
            await app.state.calls.recover()
            yield
        finally:
            await app.state.calls.close()
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
        accepted = request.app.state.calls.incoming(call_id)
        return Response(status_code=200 if accepted else 503)

    return Starlette(routes=[Route("/health", health), Route("/webhooks/openai", webhook, methods=["POST"])],
                     lifespan=lifespan)


app = create_app()
