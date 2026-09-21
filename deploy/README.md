# Deployment

Google Cloud is used for this project. Billing must be configured and an account must be set up

Instance/ server his hosted on google cloud:
- machine type e2-small (2 shared vCPUs, 2 GiB RAM), Debian 12, a 10 GB disk
- Static ip4 address for access:`summitair.34.57.120.135.sslip.io`. 
- Caddy terminates HTTPS; Uvicorn and the control sockets run under systemd. 

For this project, No GPU, load balancer, container registry is used. 

## Files in this folder

- `provision.sh`: one-time script that creates the VM itself, its network,
  firewall rules, and static IP. Run it only to stand up a new instance.
- `install.sh`: runs on the VM. Installs Python, Caddy, and the systemd
  service from the uploaded source, then starts them.
- `summitair.service`: the systemd unit `install.sh` installs. Defines the
  run user, working directory, env file, start command, and restart policy.
- `Caddyfile`: the reverse proxy config `install.sh` installs. Routes
  `/health` and `/webhooks/openai` to the app on `127.0.0.1:8000` and
  handles HTTPS certificates.


## Provisioning a new instance

```sh
PROJECT=your-project-id APPROVE_NEW_HOSTING_CHARGES=yes ./deploy/provision.sh
```


## Installing and routing

1. Point a DNS hostname at the reserved IP (a temporary demo hostname can
   use `<IP>.sslip.io`).
2. In the same OpenAI project as the API key, create a webhook subscribed
   to `realtime.call.incoming`, targeting `https://HOST/webhooks/openai`.
   Save the displayed secret as `OPENAI_WEBHOOK_SECRET` and record the
   `proj_…` ID.
3. Upload `app/`, `config.py`, `pyproject.toml`, and `deploy/` to the VM:
   `gcloud compute scp --tunnel-through-iap --zone=us-central1-a --project=YOUR_PROJECT_ID`.
4. Install the config as root-owned `/etc/summitair.env` (mode 600) and
   mount the Google service-account key at
   `/etc/summitair/google-sa.json` (or set `GOOGLE_SA_KEY_B64` instead).
5. From the uploaded source, run `SUMMITAIR_HOSTNAME=HOST ./deploy/install.sh`.
   Caddy gets its HTTPS certificate automatically.
6. Verify `/health` returns 200, an unsigned webhook returns 400, a signed
   test event returns 204, and the deployed process can read the sheet and
   calendar. Review `journalctl -u summitair`. Test a real inbound call
   before considering the cutover done.

## Telnyx SIP configuration

Follow [OpenAI Realtime SIP](https://developers.openai.com/api/docs/guides/voice-sip?api=realtime)
and [Telnyx's FQDN API](https://developers.telnyx.com/api-reference/fqdn-connections/create-an-fqdn-connection) to set up configuration and wiring up

Create a new FQDN SIP connection named `SummitAir OpenAI Realtime`, not the
old Call Control application:

- FQDN `sip.api.openai.com`, DNS type A, port 5061.
- TLS signaling and mandatory SRTP media.
- A common SIP codec (Opus, PCMU, or PCMA).
- Number's Translated Number set to the OpenAI project ID, including
  `proj_`: `sip:proj_…@sip.api.openai.com:5061;transport=tls`.
- An outbound voice profile permitting the confirmed human transfer
  destination.

The live number is already assigned to the OpenAI SIP connection. The
prior AI Assistant, TeXML app, and Call Control app have been removed.

## Logs
Logs report webhook-to-accept time, webhook-to-control-ready time, tool
elapsed time, and `speech_stop_to_output_start_ms`. 

The last metric above is backend event timing, not caller-perceived latency. 
