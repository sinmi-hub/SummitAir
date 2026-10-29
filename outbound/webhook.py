"""Signed Telnyx events and durable cleanup for the two outbound call legs."""
import asyncio
import json
import logging

from starlette.responses import Response

from outbound import signature, state, telnyx

log = logging.getLogger("summitair")


async def cleanup(app):
    # Serialize background and event-triggered cleanup in this one-worker service.
    async with app.state.outbound_cleanup_lock:
        for attempt in app.state.outbound_state.cleanup_due():
            legs = {attempt["original"], attempt["target"]} - {None}
            if not legs:
                continue  # An ambiguous dial can still deliver its signed webhook later.
            succeeded = True
            for call_id in legs:
                try:
                    await telnyx.hangup(call_id, config=app.state.config)
                except Exception as exc:
                    # Never log request headers, client_state, or the correlation token.
                    log.warning("outbound cleanup failed: %s", type(exc).__name__)
                    succeeded = False
            if succeeded:
                app.state.outbound_state.finish(attempt)


async def cleanup_loop(app):
    while True:
        try:
            await cleanup(app)
        except Exception as exc:
            log.error("outbound cleanup sweep failed: %s", type(exc).__name__)
        await asyncio.sleep(5)


async def telnyx_webhook(request):
    config = request.app.state.config
    if not config.telnyx_public_key:
        return Response(status_code=503)
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > 65536:
            return Response(status_code=413)
    if not signature.verify(bytes(body), request.headers.get("telnyx-signature-ed25519", ""),
                            request.headers.get("telnyx-timestamp", ""), config.telnyx_public_key):
        return Response(status_code=401)
    try:
        payload = json.loads(body)
        data = payload["data"]
        call = data["payload"]
        event = data["event_type"]
        call_id = call["call_control_id"]
        if not isinstance(event, str) or not isinstance(call_id, str) or not 1 <= len(call_id) <= 1024:
            raise ValueError("invalid event")
        correlation = state.decode_client_state(call.get("client_state"))
    except (ValueError, TypeError, KeyError, UnicodeDecodeError):
        return Response(status_code=400)
    if correlation is None:
        return Response(status_code=200)
    token, leg = correlation
    store = request.app.state.outbound_state
    try:
        claimed = store.observe(token, leg, call_id, event)
    except Exception:
        # A conflicting ID or unavailable database must not trigger any call action.
        return Response(status_code=503)
    if claimed:
        try:
            await telnyx.transfer_to_openai(call_id, token, config=config)
        except Exception as exc:
            log.warning("outbound transfer failed: %s", type(exc).__name__)
            store.fail(token)
    await cleanup(request.app)
    return Response(status_code=200)
