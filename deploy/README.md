# Deployment

Deploy one always-on CPU VM for the webhook receiver and control sockets.

## Host and cost gate

Use one on-demand Compute Engine **e2-small**, 2 GiB RAM, Debian 12,
10 GiB standard persistent boot disk, `us-central1-a`, and one static IPv4.
Caddy terminates HTTPS; Uvicorn and its control sockets stay running under systemd.
This is a small CPU-only service; no GPU, audio models, load balancer, Cloud NAT,
container registry, task queue, or Kubernetes is needed.

The live deployment uses Compute Engine instance `summitair-sip` in project
`settl-voice-agent`, zone `us-central1-a`. It has an e2-small machine (2 shared
vCPUs, 2 GiB RAM), Debian 12, a 10 GB standard persistent disk, and one static
external IPv4 address. Caddy serves
`https://summitair.34.57.120.135.sslip.io`; the Python service runs under systemd.

At Google's published on-demand rates, a 730-hour month is about **$16.28**:
e2-small $12.23, attached IPv4 $3.65, and 10 GB standard disk $0.40.
**$17/month** is a rounded infrastructure estimate, not the full bill. Network
egress, taxes, and OpenAI/Telnyx usage are additional. Usage and credits can
change the actual charge. Pricing sources:
[Compute pricing](https://cloud.google.com/products/compute/pricing/general-purpose),
[network pricing](https://cloud.google.com/vpc/pricing),
[disk pricing](https://cloud.google.com/compute/disks-image-pricing).

Review the hosting costs and set your own project before provisioning:

```sh
PROJECT=your-project-id APPROVE_NEW_HOSTING_CHARGES=yes ./deploy/provision.sh
```

It creates only the VM, its disk/IP, a small VPC/subnet, public HTTP/HTTPS rules,
and SSH restricted to Google's IAP range. It does not change phone routing.
The VM has no Google service account; the existing application credential is
mounted separately. Gcloud login is already working. Billing must be enabled on
your selected project before resource creation.

## Install and verify before routing

1. Point an available DNS hostname at the reserved IP. A temporary demo hostname
   may use `<IP>.sslip.io`; verify public resolution and TLS issuance first.
2. In the **same OpenAI project as the API key**, create a webhook subscribed to
   `realtime.call.incoming`, targeting `https://HOST/webhooks/openai`. Securely
   save the displayed secret as `OPENAI_WEBHOOK_SECRET`. Record the `proj_…` ID.
3. Upload only `app/`, `config.py`, `pyproject.toml`, and `deploy/` to the VM with
   `gcloud compute scp --tunnel-through-iap --zone=us-central1-a --project=YOUR_PROJECT_ID`.
   Do not upload local virtualenvs or the reference project's audio pipeline.
4. Install the configuration as root-owned `/etc/summitair.env`, mode 600.
   Mount the existing Google key at `/etc/summitair/google-sa.json` with group
   `summitair` and mode 640. Alternatively use `GOOGLE_SA_KEY_B64` in the root-only
   environment file. Map reference settings explicitly:

   | Reference setting | Service setting |
   | --- | --- |
   | `LEADS_SHEET_ID` | `TICKETS_SHEET_ID` |
   | `LEADS_SHEET_TAB` (`Leads`) | `TICKETS_SHEET_TAB` |
   | `DEMO_CALENDAR_ID` | `SERVICE_CALENDAR_ID` |
   | `DEMO_TIMEZONE` | `SERVICE_TIMEZONE` |

5. From the uploaded source, run `SUMMITAIR_HOSTNAME=HOST ./deploy/install.sh`.
   Caddy obtains HTTPS certificates automatically. Only ports 80/443 are public;
   the outbound control WebSocket connects directly to OpenAI on 443.
6. Verify HTTPS `/health` returns 200, unsigned webhook returns 400, a correctly
   signed non-call test event returns 204, and an altered/stale event returns 400.
   Verify a real OpenAI dashboard test delivery and its signature. A dashboard
   test call ID is not a real SIP call and cannot prove call acceptance.
7. Verify live read-only sheet/calendar access from the deployed process. Review
   `journalctl -u summitair`. Do not publish secrets, SIP headers, caller details,
   transcripts, or audio payloads. Health only proves process health, not SIP or
   Google access. Test actual inbound calls in the cutover window below.

## Telnyx SIP configuration

Official contracts verified 2026-09-17:
[OpenAI Realtime SIP](https://developers.openai.com/api/docs/guides/voice-sip?api=realtime),
[Realtime control socket](https://developers.openai.com/api/docs/guides/voice-server-controls?api=realtime),
[webhook signatures](https://developers.openai.com/api/docs/guides/webhooks),
[Telnyx FQDN API](https://developers.telnyx.com/api-reference/fqdn-connections/create-an-fqdn-connection).
Telnyx's [OpenAI SIP guide](https://developers.telnyx.com/docs/voice/sip-trunking/gpt-live-configuration-guide)
also documents FQDN, transport, and translated-number settings, but its example
application uses **GPT-Live**. Keep this application's requested **gpt-realtime**
model, `realtime.call.incoming`, `data.call_id`, and `/v1/realtime/calls` APIs.

After the deployed service passes verification, create a new FQDN SIP connection
named `SummitAir OpenAI Realtime`. Do not reuse the old Call Control application.
Configure:

- FQDN `sip.api.openai.com`, DNS type A, port 5061.
- TLS signaling and mandatory SRTP media.
- A common SIP codec (Opus, PCMU, or PCMA); confirm actual SDP negotiation.
- Number's Translated Number = the **actual OpenAI project ID**, including `proj_`.
  Result: `sip:proj_…@sip.api.openai.com:5061;transport=tls`.
- Existing outbound voice profile permitting the confirmed human destination.
  [Telnyx external transfer requirements](https://developers.telnyx.com/docs/voice/sip-trunking/features/external-transfers)
  include carrier call matching and Diversion-header handling. Verify the actual
  REFER, ring, answer, and no-answer cases; do not infer them from OpenAI HTTP 200.

The live number is already assigned to the OpenAI SIP connection. For a new
deployment, verify the current number assignment and webhook before making any
routing changes. The prior AI Assistant, TeXML app, and Call Control app have
been removed.

## Call verification and latency

Verify greeting once, two-way speech, interruption, existing-customer lookup,
availability, agreed-slot booking against a designated demo row, missing-row
failure, callback honesty, transfer request/ringing/answer/no-answer, emergency
closing, caller hangup, duplicate webhook, and server restart. Inspect the sheet
and calendar for a single intended booking. Cleanup test writes only with clear
identification of the test records.

Logs report webhook-to-accept, webhook-to-control-ready, tool elapsed time, and
`speech_stop_to_output_start_ms` from sideband events. The last metric measures
OpenAI event timing observed on the backend, **not** caller-perceived latency.
Compare it with a timed real call (or carrier recording when configured), record
sample count, median and p95, and separate greeting/tool/non-tool turns. No claim
about hosting/CPU causing latency is justified until the new path is measured.

## Recovery

The old AI Assistant is no longer available as a rollback target. Leave the
working SIP connection attached while testing changes to the control service.
If a future routing change fails, restore the last verified SIP connection and
number voice settings from a fresh private snapshot. No repository script
automatically changes phone routing.
