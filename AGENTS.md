# Repository Guidelines

This is the Summit Air Telnyx SIP → OpenAI gpt-realtime control service.
`app/server.py` verifies signed incoming OpenAI webhooks. `app/realtime.py`
accepts calls, maintains control WebSockets and dispatches tools. Audio must stay
between Telnyx and OpenAI. Never restore an audio bridge or local STT/TTS/VAD.

Preserve `app/agent/SYSTEM-PROMPT.md` exactly unless the user authorizes a prompt
change. `persona.system_prompt()` reads that file. Google adapters live in
`app/integrations/`; reusable business handlers are in `app/tools.py`.
Configuration lives in `config.py`.

Use Python 3.11+, four-space indentation, and small modules. Run `make test` for
focused offline tests, `make serve` for local serving, and read `deploy/README.md`
for deployment/cutover. One process/worker and persistent SQLite state are required.
Never print credentials. Do not change phone routing before deployment is verified.
The old Telnyx assistant and TeXML/Call Control applications were removed after
the SIP cutover. Report booking/callback/transfer limitations
honestly; do not expand business logic without instruction.
