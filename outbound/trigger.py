"""Run on the server host with its environment and absolute CALL_STATE_PATH."""
import asyncio
from pathlib import Path
import re
import sys
from urllib.parse import urlparse

from config import settings
from outbound import state, telnyx


async def main(to: str, webhook_url: str, config=settings) -> None:
    if not re.fullmatch(r"\+[1-9]\d{7,14}", to):
        raise ValueError("Destination must be E.164")
    if urlparse(webhook_url).scheme != "https" or not urlparse(webhook_url).hostname:
        raise ValueError("Webhook URL must be HTTPS")
    if not all((config.telnyx_api_key, config.telnyx_public_key,
                config.outbound_from_number, config.openai_sip_uri)):
        raise ValueError("Outbound API key, public key, caller ID, and SIP URI are required")
    if not Path(config.call_state_path).is_absolute() or not Path(config.call_state_path + ".outbound-state").is_file():
        raise ValueError("Use the running server's absolute CALL_STATE_PATH and shared state directory")
    store = state.Store(config.call_state_path)
    token = store.create()  # Commit before the carrier can emit the first webhook.
    try:
        call_id = await telnyx.dial(to, webhook_url, token, config=config)
        store.observe(token, "pstn", call_id, "dial.response")
    except Exception:
        # An HTTP timeout can still mean a call was placed. Retain the attempt so
        # a late signed webhook can identify and clean up that call; never redial.
        store.fail(token)
        raise
    print(f"dialing {to}, call_control_id={call_id}")


if __name__ == "__main__":
    if len(sys.argv) != 3:
        print("usage: python -m outbound.trigger <to_e164> <telnyx_webhook_url>")
        raise SystemExit(1)
    try:
        asyncio.run(main(sys.argv[1], sys.argv[2]))
    except Exception as exc:
        # HTTP exceptions can include URLs; avoid printing credentials or metadata.
        print(f"Outbound trigger failed ({type(exc).__name__}); do not retry an uncertain dial.")
        raise SystemExit(1)
