# CallTool

CallTool is a self-hosted telephone execution service for AI agents. Clients create and
control outbound calls through MCP or REST, and the same worker can answer inbound calls
to a Telnyx number. LiveKit carries media between Telnyx SIP and the selected native
realtime voice provider: Gemini Live or OpenAI Realtime.

The complete architecture and product decisions are documented in [projects.md](projects.md).
Deployment and Telnyx provisioning are documented in
[docs/self-hosting.md](docs/self-hosting.md).

## Development status

The repository is under active development on the `feature/initial` branch. The v0.1 target
is a configurable multilingual inbound and outbound caller with durable state,
policy-controlled commitments, human-in-the-loop input, and low-latency native audio
conversation. German remains the default language.

## Prerequisites before `docker compose`

There are two separate requirements: Docker must be able to run the local stack, and
Telnyx, LiveKit, and at least one supported realtime AI provider must be configured before
real phone calls can work. Python and `uv` are not required on the host when everything is
run through Compose; they are only needed for local development commands.

### Host and Docker

You need:

- A Linux server or workstation with a public, reachable IP address for real SIP calls.
  A private laptop behind NAT is fine for software tests, but Telnyx cannot deliver
  inbound SIP traffic to `localhost`.
- Docker Engine and the Docker Compose plugin. Verify both with:

  ```bash
  docker --version
  docker compose version
  docker info
  ```

- Permission for the current user to access Docker. If the user was just added to the
  `docker` group, open a new terminal or run `newgrp docker` before using Docker.
- Outbound internet access from Docker. The first build downloads container images and
  Python packages from Docker Hub, GHCR, and PyPI. Test DNS before starting:

  ```bash
  docker run --rm busybox:1.36 nslookup files.pythonhosted.org
  ```

  If this fails with a DNS or network error, fix the Docker daemon's DNS/forwarding
  first. `docker compose` will otherwise fail during `uv sync` even if DNS works on the
  host itself.
- Free host ports `80`, `443`, `5060`, `7881`, `10000-20000/udp`, and
  `50000-60000/udp`. The Compose file uses host networking for LiveKit and LiveKit SIP;
  another SIP service or web server must not already use these ports.

For an internet-facing deployment, allow these ports in both the server firewall and
the hosting provider's firewall:

| Port | Protocol | Purpose |
| --- | --- | --- |
| 80 | TCP | Caddy HTTP and certificate validation |
| 443 | TCP/UDP | CallTool and LiveKit HTTPS/HTTP3 |
| 5060 | TCP | Telnyx to LiveKit SIP signaling |
| 7881 | TCP | LiveKit RTC fallback |
| 10000-20000 | UDP | LiveKit SIP RTP media |
| 50000-60000 | UDP | LiveKit RTC media |

The checked-in LiveKit configuration uses `use_external_ip: true`. The advertised
address must therefore be reachable from the public internet. If the server is behind a
router, forward the ports above and make sure the public IP and SIP hostname resolve to
the router/server correctly.

### DNS names

For a real deployment, create DNS `A` records pointing to the server's public IP:

| Name | Used for |
| --- | --- |
| `calltool.example.com` | Public CallTool REST and MCP endpoint |
| `livekit.example.com` | Public LiveKit WebSocket endpoint through Caddy |
| `sip.example.com` | Telnyx FQDN SIP destination, direct to port `5060` |

`calltool.example.com` and `livekit.example.com` are entered as hostnames only, without
`https://`, in `CALLTOOL_DOMAIN` and `LIVEKIT_DOMAIN`. The SIP hostname must not be put
behind an HTTP reverse-proxy or CDN proxy; Telnyx connects to it as SIP/TCP, not HTTP.
Caddy only handles the first two hostnames.

The defaults `localhost` and `livekit.localhost` are useful for local HTTP checks, but
they are not sufficient for Telnyx inbound calls. Caddy also needs ports 80 and 443
reachable from the internet to obtain public certificates for real DNS names.

### Telnyx account and information

The following must already exist in the Telnyx Mission Control Portal:

1. A paid Telnyx account with the required identity/Level 2 verification completed.
   German numbers may require identity and address documents before activation.
2. An active German phone number (DID), written in `+E.164` format, for example
   `+49301234567`. The number must be assigned to the SIP connection below.
3. An FQDN SIP Connection configured for both inbound and outbound calling:
   - FQDN destination: `sip.example.com`, port `5060` (replace the hostname). If the
     portal uses one SIP URI field, enter `sip:sip.example.com:5060`.
   - Inbound SIP region: Europe, unless another region is intentionally used.
   - Transport: TCP on port `5060`.
   - Outbound authentication: `Credentials` with a dedicated SIP username and password.
     These are the values for `TELNYX_SIP_USERNAME` and `TELNYX_SIP_PASSWORD`.
   - AnchorSite: `Latency` is suitable for the first setup; a fixed European site can be
     selected later if required.
4. An Outbound Voice Profile assigned to that SIP Connection. Allow Germany (`DE`) and
   every other destination the agent is allowed to call. If the portal asks for a
   traffic type/service plan, select `Conversational` and the `Global` plan intended for
   voice-bot traffic.
5. The German number assigned to this SIP Connection under Telnyx's number settings.

This setup does not need a Telnyx API key in `.env`: the `sip bootstrap` command creates
the LiveKit-side trunks through the self-hosted LiveKit API. A Telnyx API key is only
needed if the Telnyx connection and number setup is automated through Telnyx's API.
The Telnyx SIP username/password are not the Telnyx portal login and not the API key.

The current European Telnyx SIP signaling addresses are `185.246.41.140` and
`185.246.41.141`; they are already allow-listed in `config/calltool.yaml`. If you select
another Telnyx SIP region, update `calls.inbound.allowed_addresses` from Telnyx's
[current SIP network information](https://sip.telnyx.com/) before running the bootstrap.
Telnyx's current RTP networks and port requirements are listed on the same page and
should be considered when restricting the server firewall.

### AI provider and LiveKit credentials

You also need:

- Credentials for the selected realtime voice provider:
  - Gemini: a Google AI Studio API key in `GOOGLE_API_KEY`, with access to the configured
    Gemini Live model.
  - OpenAI: an OpenAI API key in `OPENAI_API_KEY`, with Realtime API access. Supported
    models are `gpt-realtime-2.1` and the faster, lower-cost
    `gpt-realtime-2.1-mini`. There is no OpenAI model named
    `gpt-realtime-2.1-flash`.
- The default background supervisor and Gemini scripted-TTS features also use
  `GOOGLE_API_KEY`. To run with only an OpenAI key, disable `voice.supervisor.enabled`
  in `config/calltool.yaml`; Gemini scripted TTS is bypassed automatically for OpenAI
  Realtime calls. Keep optional Gemini shadow STT disabled as well.
- A self-hosted LiveKit key and secret. For the first local test, keep the matching
  development values from `config/livekit.yaml` and `config/sip.yaml`:
  `devkey` / `secret`. For production, generate strong values and replace them in all
  three places: `.env`, `config/livekit.yaml`, and `config/sip.yaml`.
- A strong random `CALLTOOL_API_KEY`. It protects all REST and MCP routes except health,
  readiness, and metrics. Do not leave `change-me` in an internet-facing deployment.
- A strong random `WEBHOOK_SIGNING_SECRET` if `WEBHOOK_URL` is configured. Webhooks are
  optional; leave `WEBHOOK_URL` empty if no event receiver is needed.

### Configure `.env` before starting

From the repository root:

```bash
cp .env.example .env
```

Edit `.env` and set at least these values for real calls:

```dotenv
CALLTOOL_ENV=development
CALLTOOL_API_KEY=<strong-calltool-api-key>

# Configure the provider key or keys that are used by your setup.
GOOGLE_API_KEY=<google-gemini-api-key>
OPENAI_API_KEY=<openai-api-key>

TELNYX_SIP_ADDRESS=sip.telnyx.eu
TELNYX_SIP_USERNAME=<telnyx-outbound-sip-username>
TELNYX_SIP_PASSWORD=<telnyx-outbound-sip-password>
TELNYX_FROM_NUMBER=+49...

CALLTOOL_DOMAIN=calltool.example.com
LIVEKIT_DOMAIN=livekit.example.com
LIVEKIT_SIP_TRUNK_ID=
```

Use `+E.164` for both `TELNYX_FROM_NUMBER` and every number sent to the API. Keep
`LIVEKIT_SIP_TRUNK_ID` empty initially; the bootstrap command prints the value after it
creates the reusable outbound trunk. The Compose file supplies the internal database,
Redis, and LiveKit URLs automatically. If you change the default PostgreSQL password,
also set `POSTGRES_PASSWORD` in `.env`; Compose uses it for both PostgreSQL and
CallTool. Set it before the first start; changing it later does not change the password
inside an already-initialized PostgreSQL volume.

For production, change `CALLTOOL_ENV` to `production` only after replacing every
development placeholder (`change-me`, `devkey`, and `secret`) and applying the matching
LiveKit config changes described above. Keep `.env` private and never commit it.

### Voice provider, model, language, and voice

The checked-in default is Gemini in German with the `Puck` voice. You can change the
global default in `config/calltool.yaml`, or use these `.env` overrides without editing
YAML:

```dotenv
# Gemini default
CALLTOOL_VOICE_PROVIDER=gemini
CALLTOOL_VOICE_MODEL=gemini-3.1-flash-live-preview
CALLTOOL_VOICE_LANGUAGE=de
CALLTOOL_VOICE_NAME=Puck
```

For OpenAI Realtime, choose either the full model or the official mini model:

```dotenv
OPENAI_API_KEY=<openai-api-key>
CALLTOOL_VOICE_PROVIDER=openai
CALLTOOL_VOICE_MODEL=gpt-realtime-2.1
CALLTOOL_VOICE_LANGUAGE=de
CALLTOOL_VOICE_NAME=marin
```

Use `gpt-realtime-2.1-mini` when lower latency and cost matter more than the full
model's capability. Both variants are native speech-to-speech models. The supported
built-in OpenAI voices are `alloy`, `ash`, `ballad`, `coral`, `echo`, `sage`, `shimmer`,
`verse`, `marin`, and `cedar`. Eligible OpenAI custom voice IDs beginning with `voice_`
are accepted as well.

Language accepts a compact BCP-47 language tag such as `de`, `en`, `en-US`, or `fr-FR`.
It is included in the system instructions and, for OpenAI, passed to input-audio
transcription. The voice is selected before the realtime session starts and cannot be
changed after that session has produced audio.

An individual outbound REST or MCP call can override all four values. Per-call values
take precedence over `.env`, and `.env` takes precedence over `config/calltool.yaml`:

```json
{
  "target": {"phone_number": "+49301234567", "name": "Test"},
  "objective": "Vereinbare einen Rückruf.",
  "voice": {
    "provider": "openai",
    "model": "gpt-realtime-2.1-mini",
    "language": "en-US",
    "voice": "cedar"
  }
}
```

For OpenAI calls, the native Realtime model also speaks the initial disclosure and
greeting so the voice stays consistent from the first sentence. Gemini calls keep the
pre-synthesized scripted greeting for German; other languages use the selected native
realtime model for the localized greeting.

The exact model names and integration options are documented by
[OpenAI GPT-Realtime-2.1](https://developers.openai.com/api/docs/models/gpt-realtime-2.1),
[OpenAI GPT-Realtime-2.1 Mini](https://developers.openai.com/api/docs/models/gpt-realtime-2.1-mini),
and the [LiveKit OpenAI Realtime plugin](https://docs.livekit.io/agents/models/realtime/plugins/openai/).

### File-based prompt profiles

System prompts and default greetings are not embedded in Python. The checked-in profile
is located at `config/prompts/default` and contains:

| File | Purpose |
| --- | --- |
| `system-outbound.md` | Complete system prompt for outbound calls |
| `system-inbound.md` | Complete system prompt for inbound calls |
| `greeting-outbound.txt` | Default outbound greeting source text |
| `greeting-inbound.txt` | Default inbound greeting source text |
| `greeting-instruction.md` | Instruction used when the native realtime model localizes the greeting |
| `watchdog-instruction.md` | Model instruction used to recover from an unexpected silent turn |
| `watchdog-fallback.txt` | Final scripted recovery phrase if model recovery fails |
| `supervisor.md` | Prompt for the optional post-call outcome supervisor |

To create a company-specific profile, copy the directory and edit the copied files:

```bash
cp -a config/prompts/default config/prompts/lwlp
```

Then select it in `.env` using its path inside the Compose container:

```dotenv
CALLTOOL_PROMPT_DIR=/app/config/prompts/lwlp
```

Alternatively, change `voice.prompts.directory` and the optional filenames in
`config/calltool.yaml`. Compose mounts the complete local `config` directory read-only
at `/app/config`, so prompt edits do not require rebuilding the image. A profile is read
as one snapshot when a call starts; changes therefore affect the next call, never the
middle of an active conversation.

Templates use deliberately limited `{{ placeholder }}` substitution without executable
template code. Available placeholders are:

- Call data: `call_id`, `direction`, `objective`, `target_name`,
  `target_phone_number`, `caller_name`, `caller_phone_number`,
  `called_phone_number`, and `organization_name`.
- Runtime data: `language`, `context_json`, `constraints_json`, `permissions_json`,
  `may_commit`, `may_accept_costs`, and `may_disclose_json`.
- Greeting instruction only: `greeting_json`, containing the rendered greeting as a
  JSON string.
- Supervisor only: `outcome_json`, containing the structured call result.

Unknown or malformed placeholders, missing files, invalid UTF-8, empty files, and files
larger than 128 KiB make the API/worker validation fail. Check a profile before calling:

```bash
docker compose run --rm calltool-api doctor
```

For security, MCP and REST callers cannot submit arbitrary server-side file paths. The
operator selects the mounted profile, while call-specific objective, context, permissions,
language, and voice continue to arrive through the normal request schema. Keep the AI
disclosure in custom greeting and system templates where legally required.

## Quick start

Once the prerequisites and `.env` are ready, validate and start the stack:

```bash
docker compose config --quiet
docker compose up -d --build
docker compose ps
```

Wait until PostgreSQL and Redis are healthy and inspect startup errors with:

```bash
docker compose logs --tail=100 calltool-api calltool-worker livekit livekit-sip
```

Create the reusable LiveKit outbound trunk, inbound trunk, and inbound dispatch rule
once. This uses the Telnyx SIP values from `.env`:

```bash
docker compose run --rm calltool-api sip bootstrap
```

Copy the printed `Set LIVEKIT_SIP_TRUNK_ID=...` value into `.env`, then recreate the API
and worker so they receive it:

```bash
docker compose up -d --force-recreate calltool-api calltool-worker
docker compose run --rm calltool-api doctor
```

The doctor must report `READY` before placing a real call. To test inbound calling, call
`TELNYX_FROM_NUMBER` from another phone while following the logs:

```bash
docker compose logs -f calltool-worker livekit-sip
```

For an explicit outbound diagnostic call to a number you control:

```bash
docker compose run --rm calltool-api doctor --call +49...
```

Each inbound caller is routed to a dedicated room and the `calltool` worker answers with
the greeting from `config/prompts/default/greeting-inbound.txt` or the selected custom
prompt profile. The first inbound call should be made only after the Telnyx number is
assigned to the SIP Connection and the bootstrap has completed.

## Durable call history and conversations

PostgreSQL is the source of truth for every inbound and outbound call. The `calls.direction`
column distinguishes `inbound` from `outbound`; `created_at`, `connected_at`, and `ended_at`
record when the job arrived, when the telephone conversation actually started, and when it
ended. History searches use `started_at`, which means `connected_at` when available and
otherwise `created_at` for unanswered or not-yet-connected calls.

`phone_call.list` returns both directions newest-first by default. It can filter by
`direction`, exact `phone_number`, case-insensitive partial `target_name`, `status`, and a
time window. `started_after` is inclusive, `started_before` is exclusive, and both require
an ISO 8601 timezone. Use `next_cursor` unchanged to fetch the next page. For example, an
agent can find the latest outbound call to a doctor with:

```json
{
  "direction": "outbound",
  "target_name": "Arzt",
  "limit": 1
}
```

To list inbound caller IDs from a defined period:

```json
{
  "direction": "inbound",
  "started_after": "2026-08-01T00:00:00+02:00",
  "started_before": "2026-09-01T00:00:00+02:00",
  "limit": 50
}
```

Pass a returned `call_id` to `phone_call.conversation`. It returns the full call record,
timing and duration, structured `summary` and `facts`, and the ordered user/assistant text
transcript. The equivalent REST endpoints are:

```text
GET /v1/calls
GET /v1/calls/{call_id}/conversation
```

Text transcripts are enabled through `storage.transcript: true` in
`config/calltool.yaml`; audio recording remains disabled. Set transcript storage to
`false` if full conversation text must not be retained. Existing calls and events remain
in PostgreSQL across container restarts through the Compose volume.

### Local development without the full Compose stack

If only the API is being developed locally, install Python 3.13.15 and `uv`:

```bash
uv sync --all-extras
uv run calltool api
```

This mode does not replace the LiveKit, LiveKit SIP, PostgreSQL, and Redis services needed
for real calls. Detailed public deployment and Telnyx provisioning notes are in
`docs/self-hosting.md`.

## Container releases

Every pushed Git tag builds the image for `linux/amd64` and `linux/arm64` and publishes it
to:

```text
ghcr.io/lwlp24/voice-mcp
```

Semantic version tags are recommended:

```bash
git tag v0.1.0
git push origin v0.1.0
```

This publishes version aliases such as `0.1.0`, `0.1`, `0`, and `latest`. Pre-release
tags such as `v0.2.0-rc.1` are not promoted to `latest`.
