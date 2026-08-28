from livekit import api
from pydantic import SecretStr

from calltool.cli.sip import build_telnyx_trunk
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
    assert trunk.headers_to_attributes == {
        "X-Telnyx-Username": "calltool-user"
    }
