"""Tests for LAN address selection used by DIAL/HTTPS binding (no network)."""

import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from receiver import dial


class TestHelpers(unittest.TestCase):
    def test_virtual_interfaces(self):
        for name in ("lo", "docker0", "tailscale0", "veth123", "virbr0", "wgp1"):
            self.assertTrue(dial._virtual_iface(name), name)
        for name in ("wlp16s0", "enp3s0", "eth0"):
            self.assertFalse(dial._virtual_iface(name), name)

    def test_usable_addresses(self):
        for address in (None, "127.0.0.1", "169.254.10.2", "0.0.0.0"):
            self.assertFalse(dial._usable(address), address)
        self.assertTrue(dial._usable("192.168.12.116"))


class TestDetectLanIp(unittest.TestCase):
    def test_prefers_a_real_default_route(self):
        with (
            mock.patch.object(dial, "_default_route_iface", return_value="wlp16s0"),
            mock.patch.object(dial, "_iface_ipv4", return_value="192.168.12.116"),
            mock.patch.object(
                dial.socket, "socket", side_effect=AssertionError("probe not needed")
            ),
        ):
            self.assertEqual(dial.detect_lan_ip(), "192.168.12.116")

    def test_skips_a_virtual_default_route(self):
        addresses = {"docker0": "172.17.0.1", "wlp16s0": "192.168.12.116"}
        with (
            mock.patch.object(dial, "_default_route_iface", return_value="docker0"),
            mock.patch.object(dial, "_iface_ipv4", side_effect=addresses.get),
            mock.patch.object(dial.socket, "socket", side_effect=OSError),
            mock.patch.object(
                dial.socket,
                "if_nameindex",
                return_value=[(1, "lo"), (2, "docker0"), (3, "wlp16s0")],
            ),
        ):
            self.assertEqual(dial.detect_lan_ip(), "192.168.12.116")

    def test_returns_none_without_a_lan_address(self):
        with (
            mock.patch.object(dial, "_default_route_iface", return_value=None),
            mock.patch.object(dial.socket, "socket", side_effect=OSError),
            mock.patch.object(dial.socket, "if_nameindex", return_value=[(1, "lo")]),
        ):
            self.assertIsNone(dial.detect_lan_ip())


class TestWaitForLanIp(unittest.TestCase):
    def test_returns_immediately_when_available(self):
        with mock.patch.object(dial, "detect_lan_ip", return_value="10.0.0.5"):
            self.assertEqual(dial.wait_for_lan_ip(0), "10.0.0.5")

    def test_times_out_instead_of_falling_back_to_loopback(self):
        with mock.patch.object(dial, "detect_lan_ip", return_value=None):
            self.assertIsNone(dial.wait_for_lan_ip(0))


class TestSSDP(unittest.TestCase):
    def test_search_targets(self):
        self.assertEqual(
            dial.replies_for_st("urn:dial-multiscreen-org:service:dial:1"),
            [dial.DIAL_ST],
        )
        self.assertEqual(
            dial.replies_for_st("ssdp:all", "uuid:test"),
            [
                dial.DIAL_ST,
                "urn:schemas-upnp-org:device:tvdevice:1",
                "upnp:rootdevice",
                "uuid:test",
            ],
        )
        self.assertEqual(dial.replies_for_st("upnp:rootdevice"), ["upnp:rootdevice"])
        self.assertEqual(
            dial.replies_for_st("urn:schemas-upnp-org:device:tvdevice:1"),
            ["urn:schemas-upnp-org:device:tvdevice:1"],
        )
        self.assertEqual(dial.replies_for_st("uuid:test", "uuid:test"), ["uuid:test"])
        self.assertEqual(
            dial.replies_for_st("urn:schemas-upnp-org:device:InternetGatewayDevice:1"),
            [],
        )

    def test_response_carries_st_usn_and_location(self):
        body = dial.build_ssdp_response(
            "uuid:test",
            "http://10.0.0.2:60000/dd.xml",
            "upnp:rootdevice",
            mac="AA:BB:CC:DD:EE:FF",
        ).decode()
        self.assertIn("ST: upnp:rootdevice", body)
        self.assertIn("USN: uuid:test::upnp:rootdevice", body)
        self.assertIn("LOCATION: http://10.0.0.2:60000/dd.xml", body)
        self.assertIn("WAKEUP: MAC=AA:BB:CC:DD:EE:FF;Timeout=30", body)
        bare = dial.build_ssdp_response(
            "uuid:test", "http://10.0.0.2:60000/dd.xml", "uuid:test"
        ).decode()
        self.assertIn("USN: uuid:test\r\n", bare)


if __name__ == "__main__":
    unittest.main()
