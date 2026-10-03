"""Telnyx call control only; SIP carries media directly to OpenAI."""
from urllib.parse import quote
from uuid import NAMESPACE_URL, uuid5

import httpx

from config import settings
from outbound import state

_BASE = "https://api.telnyx.com/v2"


def _headers(config):
    return {"Authorization": f"Bearer {config.telnyx_api_key}"}


async def get_connection_id(config=settings):
    async with httpx.AsyncClient(timeout=10) as client:
        page = 1
        while True:
            resp = await client.get(f"{_BASE}/call_control_applications", headers=_headers(config),
                                    params={"page[size]": 100, "page[number]": page})
            resp.raise_for_status()
            apps = resp.json().get("data", [])
            for app in apps:
                if app.get("application_name") == config.call_control_app_name:
                    return app["id"]
            if len(apps) < 100:
                break
            page += 1
    raise RuntimeError(f"no Call Control application named {config.call_control_app_name!r}")


async def dial(to, webhook_url, token, *, config=settings):
    body = {
        "connection_id": await get_connection_id(config),
        "to": to,
        "from": config.outbound_from_number,
        "webhook_url": webhook_url,
        "client_state": state.client_state(token, "pstn"),
        "command_id": str(uuid5(NAMESPACE_URL, token + ":dial")),
        "timeout_secs": 60,
    }
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.post(f"{_BASE}/calls", headers=_headers(config), json=body)
        resp.raise_for_status()
        call_id = resp.json().get("data", {}).get("call_control_id")
        if not isinstance(call_id, str) or not call_id:
            raise ValueError("Dial response missing call_control_id; result unknown")
        return call_id


async def transfer_to_openai(call_control_id, token, *, config=settings):
    body = {
        "to": config.openai_sip_uri,
        "from": config.outbound_from_number,
        "sip_transport_protocol": "TLS",
        "media_encryption": "SRTP",
        "client_state": state.client_state(token, "pstn"),
        "target_leg_client_state": state.client_state(token, "openai"),
        "custom_headers": [{"name": state.HEADER, "value": token}],
        "command_id": str(uuid5(NAMESPACE_URL, token + ":transfer")),
        "timeout_secs": 30,
    }
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.post(
            f"{_BASE}/calls/{quote(call_control_id, safe='')}/actions/transfer",
            headers=_headers(config), json=body,
        )
        resp.raise_for_status()


async def hangup(call_control_id, *, config=settings):
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.post(
            f"{_BASE}/calls/{quote(call_control_id, safe='')}/actions/hangup",
            headers=_headers(config),
            json={"command_id": str(uuid5(NAMESPACE_URL, call_control_id + ":hangup"))},
        )
        if resp.status_code == 422:
            try:
                errors = resp.json().get("errors", [])
                if errors and all(str(e.get("code")) == "90018" for e in errors):
                    return  # Documented "Call has already ended" response.
            except (ValueError, TypeError, AttributeError):
                pass
        resp.raise_for_status()
