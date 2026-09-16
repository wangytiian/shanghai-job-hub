import socket

import pytest

from app.services.outbound_policy import OutboundPolicyError, validate_public_http_url
from app.services.ai_settings import OpenAICompatibleClient


def test_outbound_policy_accepts_https_domain_with_only_public_dns_results():
    def resolver(host: str, port: int, *args):
        assert (host, port) == ("gateway.example.com", 443)
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 443))]

    assert validate_public_http_url("https://gateway.example.com/v1", resolver=resolver) == "https://gateway.example.com/v1"


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1:8080/v1",
        "http://localhost:8080/v1",
        "http://169.254.169.254/latest/meta-data",
        "file:///etc/passwd",
    ],
)
def test_outbound_policy_blocks_loopback_metadata_and_non_http_urls(url: str):
    with pytest.raises(OutboundPolicyError):
        validate_public_http_url(url, resolver=lambda *_args: [])


def test_outbound_policy_blocks_domain_resolving_to_private_network():
    def resolver(*_args):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.8", 443))]

    with pytest.raises(OutboundPolicyError, match="私网"):
        validate_public_http_url("https://gateway.example.com", resolver=resolver)


def test_openai_client_applies_the_configured_outbound_policy_before_request():
    client = OpenAICompatibleClient(validate_outbound=lambda _url: (_ for _ in ()).throw(OutboundPolicyError("已阻断")))

    with pytest.raises(OutboundPolicyError, match="已阻断"):
        client.test_connection("test-key", "http://127.0.0.1:8080", "gpt-test")
