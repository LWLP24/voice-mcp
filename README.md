# CallTool

CallTool is a self-hosted telephone execution service for AI agents. Clients create and
control outbound calls through MCP or REST, and the same worker can answer inbound calls
to a Telnyx number. LiveKit carries media between Telnyx SIP and Gemini Live.

The complete architecture and product decisions are documented in [projects.md](projects.md).
Deployment and Telnyx provisioning are documented in
[docs/self-hosting.md](docs/self-hosting.md).

## Development status

The repository is under active development on the `feature/initial` branch. The v0.1 target
is a German-language inbound and outbound caller with durable state, policy-controlled
commitments, human-in-the-loop input, and low-latency native audio conversation.

## Prerequisites before `docker compose`

There are two separate requirements: Docker must be able to run the local stack, and
Telnyx, LiveKit, and Gemini must be configured before real phone calls can work. Python
and `uv` are not required on the host when everything is run through Compose; they are
only needed for local development commands.

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

### Gemini and LiveKit credentials

You also need:

- A Google AI Studio/Gemini API key with access to the configured Gemini Live, TTS, and
  supervisor models. This is entered as `GOOGLE_API_KEY`. The current implementation does
  not use an OpenAI API key.
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
GOOGLE_API_KEY=<google-gemini-api-key>

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
the greeting configured under `calls.inbound` in `config/calltool.yaml`. The first inbound
call should be made only after the Telnyx number is assigned to the SIP Connection and
the bootstrap has completed.

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
