# Copyright 2026 Cisco Systems, Inc. and its affiliates
# SPDX-License-Identifier: Apache-2.0

"""Fetchers leave secrets out of the export, and config_converter reads every eBGP peer."""

from __future__ import annotations

import importlib.util
import json
import pathlib
import unittest

from aci_converter.src import fetch_from_apic
from catc_converter.src import fetch_from_catc
from config_converter.src.config_parser import parse_running_config
from fmc_converter.src import fetch_from_fmc
from meraki_converter.src import fetch_from_meraki
from nd_converter.src import fetch_from_nd
from nd_converter.src.nd_topology import network_l2_info

ROOT = pathlib.Path(__file__).resolve().parents[1]
SECRET = "S3cr3t-value"


def _load_netbox():
    path = ROOT / "3rd_party" / "netbox_converter" / "src" / "fetch_from_netbox.py"
    spec = importlib.util.spec_from_file_location("fetch_from_netbox_secrets", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


fetch_from_netbox = _load_netbox()


def _dump(doc) -> str:
    return json.dumps(doc)


class FetchSecretTests(unittest.TestCase):
    def test_catc_ssid_passphrase(self):
        doc = {"ssids": [{
            "ssid": "CORP", "passphrase": SECRET,
            "multiPSKSettings": [{"priority": 0, "passphrase": SECRET}],
        }]}
        self.assertEqual(fetch_from_catc.strip_secrets(doc), 2)
        self.assertNotIn(SECRET, _dump(doc))
        self.assertEqual(doc["ssids"][0]["ssid"], "CORP")

    def test_nd_keys_free_text_and_template_password(self):
        network_cfg = {"vlanId": "2301", "gatewayIpAddress": "192.0.2.1/24",
                       "gatewayIpV6Address": "2001:db8::1/64"}
        doc = {
            "fabrics": [{"fabricName": "F1", "nvPairs": {
                "BGP_AUTH_KEY": SECRET, "BGP_AUTH_KEY_TYPE": "3",
                "MACSEC_KEY_STRING": SECRET, "EXTRA_CONF_LEAF": f"key {SECRET}",
                "BGP_AUTH_ENABLE": "false", "ENABLE_BGP_SEND_COMMUNITY": "true",
                "BGP_AS": "64512",
            }}],
            "vrfs": {"F1": [{"vrfName": "BLUE",
                             "vrfTemplateConfig": json.dumps({"vrfVlanId": "2000",
                                                              "bgpPassword": SECRET})}]},
            "networks": {"F1": [{"networkName": "N1",
                                 "networkTemplateConfig": json.dumps(network_cfg)}]},
        }
        before = network_l2_info(doc["networks"]["F1"][0])
        self.assertEqual(fetch_from_nd.strip_secrets(doc), 4)
        self.assertNotIn(SECRET, _dump(doc))
        pairs = doc["fabrics"][0]["nvPairs"]
        self.assertEqual(pairs["BGP_AUTH_KEY_TYPE"], "3")
        self.assertEqual(pairs["BGP_AS"], "64512")
        self.assertEqual(pairs["ENABLE_BGP_SEND_COMMUNITY"], "true")
        vrf = json.loads(doc["vrfs"]["F1"][0]["vrfTemplateConfig"])
        self.assertEqual(vrf, {"vrfVlanId": "2000", "bgpPassword": ""})
        self.assertEqual(network_l2_info(doc["networks"]["F1"][0]), before)

    def test_meraki_psk_radius_and_bgp(self):
        doc = {"networkDetails": {"N1": {
            "wirelessSsids": [{"number": 0, "name": "CORP", "psk": SECRET,
                               "radiusServers": [{"host": "192.0.2.5", "secret": SECRET}]}],
            "applianceVpnBgp": {"enabled": True, "asNumber": 64512, "neighbors": [
                {"ip": "192.0.2.9", "remoteAsNumber": 64600,
                 "authentication": {"password": SECRET}}]},
        }}}
        self.assertEqual(fetch_from_meraki.strip_secrets(doc), 3)
        self.assertNotIn(SECRET, _dump(doc))
        detail = doc["networkDetails"]["N1"]
        self.assertEqual(detail["wirelessSsids"][0]["name"], "CORP")
        self.assertEqual(detail["applianceVpnBgp"]["asNumber"], 64512)

    def test_fmc_keys_and_passwords(self):
        doc = {
            "devices": [{"name": "FTD", "hostName": "fw1", "regKey": SECRET, "natID": SECRET,
                         "interfaces": [{"name": "GigabitEthernet0/0", "ifname": "outside",
                                         "pppoe": {"pppoePassword": SECRET}}]}],
            "ha_pairs": [{"name": "HA", "ftdHABootstrap": {"sharedKey": SECRET}}],
        }
        self.assertEqual(fetch_from_fmc.strip_secrets(doc), 4)
        self.assertNotIn(SECRET, _dump(doc))
        self.assertEqual(doc["devices"][0]["interfaces"][0]["ifname"], "outside")

    def test_netbox_free_form_fields(self):
        doc = {"_meta": {"counts": {}}, "devices": [{
            "name": "r1", "config_context": {"snmp": SECRET},
            "local_context_data": {"tacacs": SECRET}, "custom_fields": {"k": SECRET},
        }], "interfaces": [{"name": "Gi0/0", "custom_fields": {"k": SECRET}}]}
        self.assertEqual(fetch_from_netbox.strip_secrets(doc), 4)
        self.assertNotIn(SECRET, _dump(doc))
        self.assertEqual(doc["devices"][0]["name"], "r1")

    def test_aci_snmp_community_and_password_attributes(self):
        doc = {"totalCount": "2", "imdata": [
            {"fvTenant": {"attributes": {"name": "T1"}, "children": [
                {"fvCtx": {"attributes": {"name": "VRF1"}, "children": [
                    {"snmpCtxP": {"attributes": {}, "children": [
                        {"snmpCommunityP": {"attributes": {"name": SECRET}}}]}}]}},
                {"bgpPeerP": {"attributes": {"addr": "192.0.2.9", "password": SECRET}}},
            ]}},
            {"snmpCommunityP": {"attributes": {"name": SECRET}}},
        ]}
        self.assertEqual(fetch_from_apic.strip_secrets(doc), 3)
        self.assertNotIn(SECRET, _dump(doc))
        self.assertEqual(doc["totalCount"], "1")
        tenant = doc["imdata"][0]["fvTenant"]
        self.assertEqual(tenant["attributes"]["name"], "T1")
        self.assertEqual(tenant["children"][1]["bgpPeerP"]["attributes"]["addr"], "192.0.2.9")

    def test_bundled_samples_hold_no_secret_values(self):
        catc = json.loads((ROOT / "catc_converter/Input_data/sample_catc_export.json")
                          .read_text(encoding="utf-8"))
        self.assertEqual(fetch_from_catc.strip_secrets(catc), 0)


class BgpPeerTests(unittest.TestCase):
    XR = """!! IOS XR Configuration 7.5.1
hostname PE-A
router bgp 64512
 address-family ipv4 unicast
 !
 neighbor-group RR
  remote-as 64512
 !
 neighbor 198.51.100.2
  remote-as 64600
  address-family ipv4 unicast
  !
 !
 neighbor 198.51.100.10
  use neighbor-group RR
 !
 neighbor 198.51.100.6
  remote-as 64700
 !
 vrf BLUE
  neighbor 203.0.113.1
   remote-as 64800
  !
 !
!
end
"""
    IOS = """hostname R1
router bgp 64512
 neighbor 198.51.100.2 remote-as 64600
 !
 address-family ipv4
  neighbor 198.51.100.2 activate
 exit-address-family
 neighbor 198.51.100.6 remote-as 64700
!
interface Loopback0
"""
    FLAT = """hostname R2
router bgp 64512
neighbor 198.51.100.2 remote-as 64600
!
neighbor 198.51.100.9 remote-as 64900
"""

    def _peers(self, text):
        return [(p.neighbor_ip, p.remote_as) for p in parse_running_config(text).bgp_peers]

    def test_iosxr_nested_blocks(self):
        self.assertEqual(self._peers(self.XR), [
            ("198.51.100.2", "64600"), ("198.51.100.6", "64700"), ("203.0.113.1", "64800"),
        ])

    def test_ios_peer_after_bang(self):
        self.assertEqual(self._peers(self.IOS),
                         [("198.51.100.2", "64600"), ("198.51.100.6", "64700")])

    def test_flat_dump_unchanged(self):
        self.assertEqual(self._peers(self.FLAT), [("198.51.100.2", "64600")])


if __name__ == "__main__":
    unittest.main()
