"""Tests for the US REST client's DNS-failure fallback path."""

from unittest import mock

from polyalpha.us import rest as rest_mod
from polyalpha.us.rest import PublicUsClient, _FALLBACK_IPS


def test_fallback_ips_cover_gateway_host():
    assert "gateway.polymarket.us" in _FALLBACK_IPS
    assert _FALLBACK_IPS["gateway.polymarket.us"]


def test_ip_connection_dials_ip_not_hostname():
    conn = rest_mod._IPHTTPSConnection("gateway.polymarket.us", "104.18.38.40")
    with mock.patch.object(rest_mod.socket, "create_connection") as create, \
         mock.patch.object(conn._context, "wrap_socket") as wrap:
        fake_sock = mock.Mock()
        create.return_value = fake_sock
        wrap.return_value = fake_sock
        conn.connect()
        # Dial the pinned IP, but present the real hostname for SNI.
        create.assert_called_once_with(("104.18.38.40", 443), conn.timeout)
        wrap.assert_called_once_with(fake_sock, server_hostname="gateway.polymarket.us")


def test_fallback_ip_unknown_host_returns_none():
    client = PublicUsClient(base_url="https://unknown-host.invalid")
    assert client._get_via_fallback_ip("/v1/markets") is None


def test_fallback_ip_returns_body_on_2xx():
    client = PublicUsClient(base_url="https://gateway.polymarket.us")

    class FakeResp:
        status = 200
        reason = "OK"
        def read(self):
            return b'{"ok": true}'
        def getheaders(self):
            return [("Content-Type", "application/json")]

    class FakeConn:
        def __init__(self, *a, **k):
            pass
        def request(self, *a, **k):
            pass
        def getresponse(self):
            return FakeResp()
        def close(self):
            pass

    with mock.patch.object(rest_mod, "_IPHTTPSConnection", FakeConn):
        body, received = client._get_via_fallback_ip("/v1/markets")
    assert body == {"ok": True}
    assert received is not None
