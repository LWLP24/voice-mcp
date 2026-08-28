# CallTool

CallTool is a self-hosted telephone execution service for AI agents. Clients create and
control outbound calls through MCP or REST while LiveKit carries media between Telnyx SIP
and Gemini Live.

The complete architecture and product decisions are documented in [projects.md](projects.md).
Deployment and Telnyx provisioning are documented in
[docs/self-hosting.md](docs/self-hosting.md).

## Development status

The repository is under active development on the `feature/initial` branch. The v0.1 target
is an outbound German-language caller with durable state, policy-controlled commitments,
human-in-the-loop input, and low-latency native audio conversation.

## Local prerequisites

- Python 3.13.15
- uv
- Docker with Compose
- Google Gemini, LiveKit, and Telnyx credentials for real calls

## Quick start

```bash
cp .env.example .env
uv sync --all-extras
uv run calltool api
```

The full local stack will be available through:

```bash
docker compose up -d
```

Real SIP and Gemini calls require the credentials and trunk bootstrap described in
`docs/self-hosting.md`.

After the Telnyx connection and German caller number are active, create the stored
LiveKit outbound trunk once:

```bash
uv run calltool sip bootstrap
```

Copy the returned ID to `LIVEKIT_SIP_TRUNK_ID`, restart API and worker, then run:

```bash
uv run calltool doctor
```

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
