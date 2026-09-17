# Summit Air — direct SIP voice agent

Call the live agent at **+1 386-306-3395**. Source code:
**https://github.com/sinmi-hub/SummitAir**.

Telnyx sends telephone audio directly to OpenAI `gpt-realtime` over TLS/SRTP.
This Python service verifies incoming OpenAI webhooks, accepts each call with
`app/agent/SYSTEM-PROMPT.md`, delivers the greeting, and maintains one control
WebSocket per call. It never relays, records, transcodes, or generates audio.

`app/server.py` serves `GET /health` and `POST /webhooks/openai`.
`app/realtime.py` owns call control and Realtime tool dispatch. `app/tools.py`
contains the existing lookup, availability, and booking handlers. Google adapters
and the system-prompt file are preserved byte-for-byte. The undeployed Telnyx
Edge wrapper is removed.

## Local development

Requires Python 3.11 or newer (3.12 used for testing).

```sh
make install
cp .env.example .env
# Supply actual credentials. Never disable signature verification.
make test
make serve
```

Set `OPENAI_API_KEY` and the real `OPENAI_WEBHOOK_SECRET` before starting.
The latter comes from creating the project webhook in OpenAI, not from the API
key. `HUMAN_TRANSFER_NUMBER` must be a known E.164 destination. No number is
inferred from operator contact information or caller metadata.

The supplied tool schemas validate model arguments. Google operations run in a
single dedicated worker because the existing adapters share a cached,
non-thread-safe Google HTTP client. The socket keeps processing call events
while Google I/O runs. There are no public unauthenticated tool endpoints.

Run **one worker and one instance**, with `CALL_STATE_PATH` on persistent disk.
A tiny SQLite ledger deduplicates incoming calls across restarts for four days.
Duplicate tool invocation IDs are ignored within a call; repeated booking
attempts for the same phone and slot are blocked. The service offers at most
16 simultaneous control connections. Restarting ends active calls; startup
ends orphaned calls before serving webhooks. Deploy between calls.

A lost control connection ends the call rather than replaying possibly completed
bookings. Google operations time out to the caller after 20 seconds, but an
already-running write may still finish. Never retry an unclear booking result.

## Current demo limitations

- Booking requires an existing sheet row. New-customer intake is not persisted.
- Callback creation is not implemented and no callback tool is advertised.
- The existing calendar adapter checks availability but does not reserve slots
  atomically, account for technician capacity, or roll back a calendar event if
  the sheet update fails. The legacy sheet status remains `demo_booked`.
- Transfer sends OpenAI a SIP REFER to the configured number. HTTP success means
  the request reached the carrier; it does **not** prove that a human answered.
  Warm transfer, summary delivery, and no-answer recovery are not implemented.
  Verify carrier behavior before claiming the prompt's full fallback flow works.
- The new greeting omits the recording claim because direct SIP does not inherit
  the old assistant's recording configuration. The preserved prompt still assumes
  recording. Resolve this by configuring and verifying carrier recording and
  restoring the disclosure before using a recorded demo, or by separately
  authorizing a prompt correction. No audio recording is added to the backend.

See [deployment and cutover](deploy/README.md).

## Public repository hygiene

Copy `.env.example` for configuration; keep real values in ignored `.env` files.
Keep service-account JSON keys in `.keys/` outside source control. Never include
call recordings, transcripts, customer exports, or private deployment snapshots.
Local deployment status is intentionally ignored.

Enable the staged-file credential check after cloning:

```sh
git config core.hooksPath .githooks
```

Before publishing, review `git diff --cached` and `git status --ignored`.
The hook is a guard against common credential formats, not a guarantee that every
kind of private data is detected. `.gitignore` does not remove files already in
Git history; exposed credentials must be revoked and replaced.
