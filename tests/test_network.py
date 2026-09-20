#!/usr/bin/env python3
"""Network / Wi-Fi configuration tests.

build_wifi_netplan() is implemented inline (mirrors the pattern the recovery
agent will use). All tests are fully synthetic — no real network calls.
"""

from __future__ import annotations

import copy
import unittest

import yaml

# ---------------------------------------------------------------------------
# Known hardware constants (from spec §3.2)
# ---------------------------------------------------------------------------

MAC_WIFI = "34:13:e8:3c:b5:9a"
MAC_LAN  = "44:8a:5b:64:11:2b"
IFACE_WIFI = "wifi0"
IFACE_LAN  = "lan0"

# ---------------------------------------------------------------------------
# Implementation under test (inline)
# ---------------------------------------------------------------------------

def build_wifi_netplan(
    networks: list[dict],
    mode: str = "merge",
    mac_wifi: str = MAC_WIFI,
    mac_lan: str = MAC_LAN,
) -> dict:
    """Build a Netplan config dict for samovar.

    Args:
        networks: list of dicts with 'ssid', 'password', optional 'hidden'.
        mode: 'merge' or 'replace' (affects semantic intent, not the structure here).
        mac_wifi: Wi-Fi interface MAC address.
        mac_lan: Ethernet interface MAC address.

    Returns:
        A Netplan-compatible dict (network.version == 2).
    """
    # Build access-points mapping (no duplicates by SSID)
    seen_ssids: set[str] = set()
    access_points: dict[str, dict] = {}
    for net in networks:
        ssid = net["ssid"]
        if ssid in seen_ssids:
            continue  # deduplicate
        seen_ssids.add(ssid)
        ap: dict = {"password": net["password"]}
        if net.get("hidden", False):
            ap["hidden"] = True
        access_points[ssid] = ap

    netplan: dict = {
        "network": {
            "version": 2,
            "ethernets": {
                IFACE_LAN: {
                    "match": {"macaddress": mac_lan},
                    "set-name": IFACE_LAN,
                    "dhcp4": True,
                    "optional": True,
                    "dhcp4-overrides": {"route-metric": 10},
                }
            },
            "wifis": {
                IFACE_WIFI: {
                    "match": {"macaddress": mac_wifi},
                    "set-name": IFACE_WIFI,
                    "dhcp4": True,
                    "optional": True,
                    "dhcp4-overrides": {"route-metric": 20},
                    "access-points": access_points,
                }
            },
        }
    }
    return netplan


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestBuildWifiNetplanBasic(unittest.TestCase):
    """Basic structure and required fields."""

    def _build(self, networks=None, mode="merge"):
        if networks is None:
            networks = [{"ssid": "HomeNet", "password": "password1234"}]
        return build_wifi_netplan(networks, mode=mode)

    def test_returns_dict(self) -> None:
        result = self._build()
        self.assertIsInstance(result, dict)

    def test_has_network_key(self) -> None:
        result = self._build()
        self.assertIn("network", result)

    def test_version_is_2(self) -> None:
        result = self._build()
        self.assertEqual(result["network"]["version"], 2)

    def test_is_valid_yaml(self) -> None:
        result = self._build()
        text = yaml.dump(result)
        parsed = yaml.safe_load(text)
        self.assertEqual(parsed["network"]["version"], 2)


class TestWifiInterface(unittest.TestCase):
    """Tests for wifi0 interface configuration."""

    def setUp(self) -> None:
        self.netplan = build_wifi_netplan(
            [{"ssid": "TestNet", "password": "testpassword1"}]
        )
        self.wifi_cfg = self.netplan["network"]["wifis"][IFACE_WIFI]

    def test_wifi_interface_name(self) -> None:
        self.assertIn(IFACE_WIFI, self.netplan["network"]["wifis"])

    def test_wifi_mac_match(self) -> None:
        self.assertEqual(self.wifi_cfg["match"]["macaddress"], MAC_WIFI)

    def test_wifi_dhcp4_true(self) -> None:
        self.assertTrue(self.wifi_cfg["dhcp4"])

    def test_wifi_optional_true(self) -> None:
        self.assertTrue(self.wifi_cfg["optional"])

    def test_wifi_metric_higher_than_lan(self) -> None:
        lan_metric = self.netplan["network"]["ethernets"][IFACE_LAN]["dhcp4-overrides"]["route-metric"]
        wifi_metric = self.wifi_cfg["dhcp4-overrides"]["route-metric"]
        self.assertGreater(wifi_metric, lan_metric, "Ethernet should have lower metric than Wi-Fi")


class TestLanInterface(unittest.TestCase):
    """Tests for lan0 interface configuration."""

    def setUp(self) -> None:
        self.netplan = build_wifi_netplan(
            [{"ssid": "TestNet", "password": "testpassword1"}]
        )
        self.lan_cfg = self.netplan["network"]["ethernets"][IFACE_LAN]

    def test_lan_interface_name(self) -> None:
        self.assertIn(IFACE_LAN, self.netplan["network"]["ethernets"])

    def test_lan_mac_match(self) -> None:
        self.assertEqual(self.lan_cfg["match"]["macaddress"], MAC_LAN)

    def test_lan_dhcp4_true(self) -> None:
        self.assertTrue(self.lan_cfg["dhcp4"])

    def test_lan_optional_true(self) -> None:
        self.assertTrue(self.lan_cfg["optional"])

    def test_lan_has_lower_metric_than_wifi(self) -> None:
        lan_metric = self.lan_cfg["dhcp4-overrides"]["route-metric"]
        wifi_metric = self.netplan["network"]["wifis"][IFACE_WIFI]["dhcp4-overrides"]["route-metric"]
        self.assertLess(lan_metric, wifi_metric)


class TestAccessPoints(unittest.TestCase):
    """Tests for Wi-Fi access points rendering."""

    def test_single_ssid(self) -> None:
        netplan = build_wifi_netplan([{"ssid": "MyNet", "password": "password1234"}])
        aps = netplan["network"]["wifis"][IFACE_WIFI]["access-points"]
        self.assertIn("MyNet", aps)
        self.assertEqual(aps["MyNet"]["password"], "password1234")

    def test_multiple_networks(self) -> None:
        networks = [
            {"ssid": "Net1", "password": "password1234"},
            {"ssid": "Net2", "password": "otherpasswd"},
            {"ssid": "Net3", "password": "thirdpasswd"},
        ]
        netplan = build_wifi_netplan(networks)
        aps = netplan["network"]["wifis"][IFACE_WIFI]["access-points"]
        self.assertEqual(len(aps), 3)
        self.assertIn("Net1", aps)
        self.assertIn("Net2", aps)
        self.assertIn("Net3", aps)

    def test_no_duplicates_by_ssid(self) -> None:
        """Merge mode: duplicate SSIDs must not create multiple entries."""
        networks = [
            {"ssid": "SameNet", "password": "password1234"},
            {"ssid": "SameNet", "password": "differentpw"},
        ]
        netplan = build_wifi_netplan(networks, mode="merge")
        aps = netplan["network"]["wifis"][IFACE_WIFI]["access-points"]
        self.assertEqual(len(aps), 1, "Duplicate SSID should be deduplicated")

    def test_hidden_ssid_flagged(self) -> None:
        networks = [{"ssid": "HiddenNet", "password": "password1234", "hidden": True}]
        netplan = build_wifi_netplan(networks)
        aps = netplan["network"]["wifis"][IFACE_WIFI]["access-points"]
        self.assertTrue(aps["HiddenNet"].get("hidden"), "hidden SSID must have hidden: true")

    def test_visible_ssid_no_hidden_flag(self) -> None:
        networks = [{"ssid": "VisibleNet", "password": "password1234", "hidden": False}]
        netplan = build_wifi_netplan(networks)
        aps = netplan["network"]["wifis"][IFACE_WIFI]["access-points"]
        self.assertFalse(aps["VisibleNet"].get("hidden", False))

    def test_ssid_with_spaces(self) -> None:
        """SSID with spaces must be preserved as-is."""
        networks = [{"ssid": "My Home Network", "password": "password1234"}]
        netplan = build_wifi_netplan(networks)
        aps = netplan["network"]["wifis"][IFACE_WIFI]["access-points"]
        self.assertIn("My Home Network", aps)

    def test_ssid_with_special_chars(self) -> None:
        """SSID with special characters must be preserved."""
        networks = [{"ssid": "Net@Work#2024!", "password": "password1234"}]
        netplan = build_wifi_netplan(networks)
        aps = netplan["network"]["wifis"][IFACE_WIFI]["access-points"]
        self.assertIn("Net@Work#2024!", aps)

    def test_empty_networks_list(self) -> None:
        """Empty networks list is valid (no access-points)."""
        netplan = build_wifi_netplan([])
        aps = netplan["network"]["wifis"][IFACE_WIFI]["access-points"]
        self.assertEqual(len(aps), 0)

    def test_multiple_networks_different_passwords(self) -> None:
        networks = [
            {"ssid": "Alpha", "password": "alpha_password"},
            {"ssid": "Beta", "password": "beta_password"},
        ]
        netplan = build_wifi_netplan(networks)
        aps = netplan["network"]["wifis"][IFACE_WIFI]["access-points"]
        self.assertEqual(aps["Alpha"]["password"], "alpha_password")
        self.assertEqual(aps["Beta"]["password"], "beta_password")


class TestModeSemantics(unittest.TestCase):
    """Tests for merge vs replace mode semantics."""

    def test_merge_mode_accepted(self) -> None:
        netplan = build_wifi_netplan(
            [{"ssid": "Net1", "password": "password1234"}],
            mode="merge",
        )
        # Structure should still be correct
        self.assertEqual(netplan["network"]["version"], 2)

    def test_replace_mode_accepted(self) -> None:
        netplan = build_wifi_netplan(
            [{"ssid": "Net1", "password": "password1234"}],
            mode="replace",
        )
        self.assertEqual(netplan["network"]["version"], 2)

    def test_merge_mode_no_duplicates_from_existing(self) -> None:
        """Simulate merging: if existing networks already exist, don't duplicate."""
        existing = [{"ssid": "Existing", "password": "existingpw"}]
        new_networks = [
            {"ssid": "Existing", "password": "existingpw"},  # same SSID
            {"ssid": "NewNet", "password": "newpassword1"},
        ]
        netplan = build_wifi_netplan(existing + new_networks, mode="merge")
        aps = netplan["network"]["wifis"][IFACE_WIFI]["access-points"]
        # Only one entry for "Existing"
        self.assertEqual(len([k for k in aps if k == "Existing"]), 1)
        self.assertIn("NewNet", aps)


class TestNetplanYamlOutput(unittest.TestCase):
    """Test that the generated YAML is valid Netplan structure."""

    def test_valid_yaml_output(self) -> None:
        netplan = build_wifi_netplan([{"ssid": "TestNet", "password": "password1234"}])
        yaml_text = yaml.dump(netplan, default_flow_style=False)
        parsed = yaml.safe_load(yaml_text)
        self.assertIsNotNone(parsed)
        self.assertIn("network", parsed)

    def test_version_2_in_yaml(self) -> None:
        netplan = build_wifi_netplan([{"ssid": "TestNet", "password": "password1234"}])
        yaml_text = yaml.dump(netplan, default_flow_style=False)
        self.assertIn("version: 2", yaml_text)

    def test_both_interface_names_in_yaml(self) -> None:
        netplan = build_wifi_netplan([{"ssid": "TestNet", "password": "password1234"}])
        yaml_text = yaml.dump(netplan, default_flow_style=False)
        self.assertIn(IFACE_WIFI, yaml_text)
        self.assertIn(IFACE_LAN, yaml_text)

    def test_both_macs_in_yaml(self) -> None:
        netplan = build_wifi_netplan([{"ssid": "TestNet", "password": "password1234"}])
        yaml_text = yaml.dump(netplan, default_flow_style=False)
        self.assertIn(MAC_WIFI, yaml_text)
        self.assertIn(MAC_LAN, yaml_text)

    def test_password_not_logged_in_interface_name(self) -> None:
        """Passwords should only be inside access-points, not in interface keys."""
        secret = "supersecretpassword"
        netplan = build_wifi_netplan([{"ssid": "Net", "password": secret}])
        # The password must be inside access-points, not used as dict key
        wifis = netplan["network"]["wifis"]
        self.assertNotIn(secret, wifis)


if __name__ == "__main__":
    unittest.main()
