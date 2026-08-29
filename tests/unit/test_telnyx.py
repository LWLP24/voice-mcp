from livekit import api
from pydantic import SecretStr

from calltool.cli.sip import (
    build_inbound_dispatch_rule,
    build_telnyx_inbound_trunk,
    build_telnyx_trunk,
)
from calltool.config import Settings


def test_telnyx_trunk_uses_europe_tcp_and_forced_digest_identity() -> None:
    settings = Settings(
        CALLTOOL_ENV="test",
        TELNYX_SIP_ADDRESS="sip.telnyx.eu",
        TELNYX_SIP_USERNAME="calltool-user",
        TELNYX_SIP_PASSWORD=SecretStr("strong-password"),
        TELNYX_FROM_NUMBER="+49301234567",
    )

    trunk = build_telnyx_trunk(settings)

    assert trunk.address == "sip.telnyx.eu"
    assert trunk.destination_country == "DE"
    assert trunk.numbers == ["+49301234567"]
    assert trunk.auth_username == "calltool-user"
    assert trunk.auth_password == "strong-password"
    assert trunk.transport == api.SIP_TRANSPORT_TCP
    assert trunk.headers_to_attributes == {"X-Telnyx-Username": "calltool-user"}


def test_telnyx_inbound_trunk_uses_european_signaling_allowlist() -> None:
    settings = Settings(
        CALLTOOL_ENV="test",
        TELNYX_FROM_NUMBER="+49301234567",
    )

    trunk = build_telnyx_inbound_trunk(settings)

    assert trunk.numbers == ["+49301234567"]
    assert trunk.allowed_addresses == [
        "185.246.41.140/32",
        "185.246.41.141/32",
    ]
    assert trunk.max_call_duration.seconds == 1800


def test_inbound_dispatch_creates_dedicated_agent_job_per_caller() -> None:
    settings = Settings(CALLTOOL_ENV="test")

    dispatch = build_inbound_dispatch_rule(settings, "ST_inbound")

    assert dispatch.trunk_ids == ["ST_inbound"]
    assert dispatch.rule.dispatch_rule_individual.room_prefix == "calltool-inbound-"
    assert len(dispatch.room_config.agents) == 1
    agent = dispatch.room_config.agents[0]
    assert agent.agent_name == "calltool"
    assert agent.metadata == '{"direction":"inbound"}'
