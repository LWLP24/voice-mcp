# Self-hosting CallTool with Telnyx

CallTool uses Telnyx as its PSTN/SIP provider and self-hosted LiveKit SIP as the media
bridge. Telnyx and LiveKit both document this integration directly.

## What you need

- A paid Telnyx account
- An activated German Telnyx phone number
- A Telnyx Outbound Voice Profile with conversational traffic and the global service plan
- A Telnyx FQDN SIP Connection with outbound username/password authentication
- A public server for LiveKit, LiveKit SIP, CallTool, PostgreSQL, Redis, and Caddy
- Google Gemini API credentials

German numbers are not instant. Telnyx requires identity and address documents; local
numbers also require an address matching the area code. Telnyx currently states that
validation usually takes about 72 hours after all documents arrive.

## 1. Network and DNS

Point your API and LiveKit WebSocket hostnames at the server running Docker Compose. For
the Telnyx FQDN Connection, also create a dedicated SIP hostname such as
`sip.example.com` pointing directly at the server. Caddy only proxies HTTP/WebSocket;
SIP does not pass through Caddy.

Open at least these ports in the host and provider firewall:

| Port | Protocol | Purpose |
| --- | --- | --- |
| 80, 443 | TCP and 443/UDP | Caddy HTTP, HTTPS, HTTP/3 |
| 5060 | TCP | Telnyx to LiveKit SIP signaling |
| 7881 | TCP | LiveKit RTC fallback |
| 10000–20000 | UDP | LiveKit SIP RTP media |
| 50000–60000 | UDP | LiveKit RTC media |

The checked-in configuration uses `use_external_ip: true`. The host therefore needs a
publicly reachable IP and must not hide the published media ports behind an incompatible
NAT.

## 2. Configure Telnyx

In the Telnyx Mission Control Portal:

1. Complete the account and German DID verification, then purchase the caller number.
2. Create an Outbound Voice Profile with traffic type `conversational` and service plan
   `global`.
3. Create a SIP Connection of type FQDN.
4. Configure outbound authentication with a strong username and password.
5. Select TCP, the European SIP region, and either latency-based anchoring or Frankfurt.
6. Link the Outbound Voice Profile to the connection.
7. Add the public LiveKit SIP hostname and port `5060` to the FQDN connection.
8. Assign the German phone number to that connection.

CallTool sends destinations and caller IDs in `+E.164`. Keep the Telnyx destination and
origination number formats on `+E.164`. The configured caller number must belong to the
connection; otherwise Telnyx can reject the call with `403 Caller Origination Number is
Invalid`.

Telnyx currently documents HD Voice enablement for US customers. Do not assume G.722 HD
Voice for a German number; the normal PSTN/G.711 path remains compatible with CallTool.

## 3. Configure CallTool

Copy the environment template and replace every placeholder:

```bash
cp .env.example .env
```

The Telnyx-specific values are:

```dotenv
TELNYX_SIP_ADDRESS=sip.telnyx.eu
TELNYX_SIP_USERNAME=<outbound-auth-username>
TELNYX_SIP_PASSWORD=<outbound-auth-password>
TELNYX_FROM_NUMBER=+49...
LIVEKIT_SIP_TRUNK_ID=
```

`sip.telnyx.eu` keeps SIP signaling in the European Telnyx region. The connection's
AnchorSite controls media placement separately.

## 4. Start and bootstrap

Start the infrastructure:

```bash
docker compose up -d
```

Create the reusable LiveKit outbound trunk:

```bash
docker compose run --rm calltool-api sip bootstrap
```

The bootstrap uses TCP, SIP digest credentials, `destination_country=DE`, and the
`X-Telnyx-Username` header required to select the correct Telnyx credential connection on
the first INVITE. Copy the returned trunk ID into `.env` as `LIVEKIT_SIP_TRUNK_ID`, then
apply it:

```bash
docker compose up -d --force-recreate calltool-api calltool-worker
```

## 5. Verify

Run the dependency checks first:

```bash
docker compose run --rm calltool-api doctor
```

Then place an explicit diagnostic call to a number you control:

```bash
docker compose run --rm calltool-api doctor --call +49...
```

For SIP failures, inspect both `docker compose logs livekit-sip calltool-worker` and the
Telnyx SIP call-flow debugger. Common setup errors are a wrong caller ID, a number not
assigned to the connection, missing outbound profile, an incorrect `sip.telnyx.eu`
address, or blocked RTP ports.

## Primary documentation

- [LiveKit Telnyx setup](https://docs.livekit.io/telephony/start/providers/telnyx/)
- [LiveKit outbound trunks](https://docs.livekit.io/telephony/making-calls/outbound-trunk/)
- [Telnyx LiveKit configuration](https://developers.telnyx.com/docs/voice/sip-trunking/livekit-configuration-guide)
- [Telnyx SIP regions and ports](https://sip.telnyx.com/)
- [German DID requirements](https://support.telnyx.com/en/articles/1311450-germany-did-requirements)
