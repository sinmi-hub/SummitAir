"""Configuration for the SIP control service and existing Google adapters."""
from __future__ import annotations

import os
from dataclasses import dataclass, field

from dotenv import load_dotenv

load_dotenv()  # reads ./.env


def _get(name: str, default: str = "") -> str:
    return os.environ.get(name, default)


def _get_bool(name: str, default: bool = False) -> bool:
    return _get(name, str(default)).strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    debug: bool = field(default_factory=lambda: _get_bool("DEBUG", False))

    openai_api_key: str = field(default_factory=lambda: _get("OPENAI_API_KEY"), repr=False)
    openai_webhook_secret: str = field(default_factory=lambda: _get("OPENAI_WEBHOOK_SECRET"), repr=False)
    openai_voice: str = field(default_factory=lambda: _get("OPENAI_VOICE", "marin"))
    human_transfer_number: str = field(default_factory=lambda: _get("HUMAN_TRANSFER_NUMBER"))
    call_state_path: str = field(default_factory=lambda: _get("CALL_STATE_PATH", "call-state.sqlite3"))

    # --- Google Workspace (CRM = Sheets, service calls = Calendar) ---
    google_sa_key_path: str = field(default_factory=lambda: _get("GOOGLE_SA_KEY_PATH"))
    # Base64 credential takes precedence over a mounted file.
    google_sa_key_b64: str = field(default_factory=lambda: _get("GOOGLE_SA_KEY_B64"))
    tickets_sheet_id: str = field(default_factory=lambda: _get("TICKETS_SHEET_ID"))
    tickets_sheet_tab: str = field(default_factory=lambda: _get("TICKETS_SHEET_TAB", "Tickets"))
    service_calendar_id: str = field(default_factory=lambda: _get("SERVICE_CALENDAR_ID"))
    service_timezone: str = field(
        default_factory=lambda: _get("SERVICE_TIMEZONE", "America/New_York"))

    # --- Outbound calling (outbound/) -- unused by the inbound SummitAir flow ---
    telnyx_api_key: str = field(default_factory=lambda: _get("TELNYX_API"), repr=False)
    telnyx_public_key: str = field(default_factory=lambda: _get("TELNYX_PUBLIC_KEY"), repr=False)
    outbound_from_number: str = field(default_factory=lambda: _get("OUTBOUND_FROM_NUMBER"))
    call_control_app_name: str = field(
        default_factory=lambda: _get("CALL_CONTROL_APP_NAME", "summitair-outbound"))
    # The same SIP URI already entered on Telnyx's dashboard for the inbound FQDN
    # connection (deploy/README.md's "Translated Number") -- needed in code here
    # because we, not Telnyx's dashboard config, issue the transfer that reaches it.
    openai_sip_uri: str = field(default_factory=lambda: _get("OPENAI_SIP_URI"))


settings = Settings()
