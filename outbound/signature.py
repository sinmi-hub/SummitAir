"""Telnyx webhook signature verification (Ed25519).

Mirrors the proven implementation in ~/voice-agent/app/telephony/signature.py.
Telnyx signs each webhook over f"{timestamp}|{raw_body}" and sends
telnyx-signature-ed25519 (base64 signature) and telnyx-timestamp (unix seconds).
Verified with the account's base64 public key (Telnyx portal -> Account ->
Keys & Credentials -> Public Key). Verification is only enforced when
a public key is configured. Missing keys fail closed.
"""
from __future__ import annotations

import base64
import time

from config import settings

_TOLERANCE_S = 300  # 5 minutes -- generous for clock skew, tight against replay


def verify(raw_body: bytes, signature_b64: str, timestamp: str, public_key=None) -> bool:
    public_key = settings.telnyx_public_key if public_key is None else public_key
    if not public_key:
        return False
    if not signature_b64 or not timestamp:
        return False
    try:
        from nacl.exceptions import BadSignatureError
        from nacl.signing import VerifyKey
    except ImportError:  # pragma: no cover - dependency missing
        return False  # fail closed: a key was configured but we can't verify

    try:
        if abs(time.time() - int(timestamp)) > _TOLERANCE_S:
            return False
    except (TypeError, ValueError):
        return False

    signed = f"{timestamp}|".encode() + raw_body
    try:
        key = VerifyKey(base64.b64decode(public_key, validate=True))
        key.verify(signed, base64.b64decode(signature_b64, validate=True))
        return True
    except (BadSignatureError, ValueError):
        return False
