# Copyright 2026 Cisco Systems, Inc. and its affiliates
# SPDX-License-Identifier: Apache-2.0

"""Interfaces in routing blocks, canvas rows, day-0 configs and routing summaries."""

from __future__ import annotations

import unittest
from types import SimpleNamespace

from cml_converter.src.config_parser import (
    parse_running_config as cml_parse, summarize_routing,
)
from cml_converter.src.convert import split_day0_configs
from cml_converter.src.fetch_from_cml import strip_credentials
from cml_converter.src.stencil_mapper import map_one
from cml_converter.src.topology_mapper import _place_by_coordinates
from config_converter.src.config_parser import parse_running_config as cfg_parse

XR = """hostname PE-A
vrf BLUE
 address-family ipv4 unicast
 !
!
interface Loopback0
 ipv4 address 192.0.2.10 255.255.255.255
 ipv6 address 2001:db8:a::10/128
!
interface GigabitEthernet0/0/0/2
 ipv4 address 198.51.100.5 255.255.255.252
 ipv6 enable
!
router isis 1
 is-type level-2-only
 interface Loopback0
  passive
 !
 interface GigabitEthernet0/0/0/2
  point-to-point
 !
!
router bgp 64512
 address-family ipv4 unicast
 !
 address-family vpnv4 unicast
 !
 vrf BLUE
  address-family ipv4 unicast
  !
 !
!
segment-routing
 srv6
  locators
   locator LOC1
    prefix 2001:db8:1::/48
   !
  !
 !
!
end
"""

IOS_INDENTED = """  hostname EDGE-A
  vrf definition 20
   address-family ipv4
   exit-address-family
  !
  interface GigabitEthernet3
   ip address 198.51.100.65 255.255.255.192
  !
  sdwan
   interface GigabitEthernet3
    tunnel-interface
  !
  router ospf 7 vrf 20
   network 192.0.2.0 0.0.0.255 area 0
  !
"""

FLAT = """interface GigabitEthernet0/0
ip address 192.0.2.1 255.255.255.0
!
router ospf 1
!
interface GigabitEthernet0/1
ip address 198.51.100.1 255.255.255.0
"""

DAY0 = "hostname inserthostname-here\ninterface range Ethernet 0/0 - 3\n no ip address\n shutdown\n"
DAY0_EDITED = "hostname inserthostname-here\ninterface Ethernet0/0\n ip address 192.0.2.9 255.255.255.0\n"


class NestedInterfaceTests(unittest.TestCase):
    def test_routing_block_does_not_replace_interfaces(self):
        for parse in (cml_parse, cfg_parse):
            parsed = parse(XR)
            self.assertEqual(parsed.interfaces["Loopback0"].all_cidrs(),
                             ["192.0.2.10/32", "2001:db8:a::10/128"])
            self.assertEqual(parsed.interfaces["GigabitEthernet0/0/0/2"].all_cidrs(),
                             ["198.51.100.5/30"])

    def test_whole_config_indented(self):
        for parse in (cml_parse, cfg_parse):
            parsed = parse(IOS_INDENTED)
            self.assertEqual(parsed.interfaces["GigabitEthernet3"].all_cidrs(),
                             ["198.51.100.65/26"])

    def test_flattened_dump_still_reads_every_interface(self):
        for parse in (cml_parse, cfg_parse):
            parsed = parse(FLAT)
            self.assertEqual(parsed.interfaces["GigabitEthernet0/0"].all_cidrs(), ["192.0.2.1/24"])
            self.assertEqual(parsed.interfaces["GigabitEthernet0/1"].all_cidrs(), ["198.51.100.1/24"])


class LayoutTests(unittest.TestCase):
    def test_wide_canvas_keeps_nodes_above(self):
        devs = [SimpleNamespace(name=n, x=x, y=y) for n, x, y in (
            ("top", 300, 40), ("mid", 300, 90), ("a", 0, 140), ("b", 300, 140),
            ("c", 600, 140), ("d", 900, 140), ("e", 1200, 140),
        )]
        grid = _place_by_coordinates(devs)
        row_of = {name: r for r, row in enumerate(grid) for name in row if name != "_AIR_"}
        self.assertLess(row_of["top"], row_of["mid"])
        self.assertLess(row_of["mid"], row_of["b"])
        self.assertEqual(len({row_of[n] for n in "abcde"}), 1)
        self.assertEqual([n for n in grid[row_of["b"]] if n != "_AIR_"], list("abcde"))

    def test_close_heights_share_a_row(self):
        devs = [SimpleNamespace(name=n, x=x, y=y) for n, x, y in (
            ("a", 0, 100), ("b", 800, 110), ("c", 400, 300),
        )]
        grid = _place_by_coordinates(devs)
        row_of = {name: r for r, row in enumerate(grid) for name in row if name != "_AIR_"}
        self.assertEqual(row_of["a"], row_of["b"])
        self.assertLess(row_of["a"], row_of["c"])


class FetchTests(unittest.TestCase):
    def test_pyats_credentials_are_removed(self):
        nodes = [
            {"label": "n1", "pyats": {"username": "u", "password": "p", "enable_password": "e"}},
            {"label": "n2"},
        ]
        self.assertEqual(strip_credentials(nodes), 1)
        self.assertEqual(nodes, [{"label": "n1"}, {"label": "n2"}])


class Day0Tests(unittest.TestCase):
    def test_day0_template_is_reported_and_edited_one_kept(self):
        raw = {"blank": DAY0, "edited": DAY0_EDITED, "pe": XR}
        parsed = {label: cml_parse(text) for label, text in raw.items()}
        kept, day0 = split_day0_configs(raw, parsed)
        self.assertEqual(day0, ["blank"])
        self.assertEqual(sorted(kept), ["edited", "pe"])


class StencilTests(unittest.TestCase):
    def test_xrd_control_plane_with_version(self):
        mapping = map_one("cp", "iosxrd-cp-1-2-3")
        self.assertEqual(mapping.stencil_type, "Router")
        self.assertEqual(mapping.model, "XRd Control Plane (Cisco IOS XR Container)")
        self.assertEqual(mapping.os, "IOS-XR")
        self.assertEqual(mapping.confidence, 1.0)

    def test_version_suffix_does_not_match_a_different_platform(self):
        self.assertEqual(map_one("pe", "iol-xe").model, "IOL-XE (Cisco IOS-XE on Linux)")
        self.assertEqual(map_one("pe", "iol-17-12").model, "IOL (Cisco IOS-on-Linux)")


class RoutingSummaryTests(unittest.TestCase):
    def test_xr_summary(self):
        self.assertEqual(summarize_routing(XR), [
            "IS-IS 1 L2", "BGP 64512 (ipv4, vpnv4)", "SRv6 LOC1 2001:db8:1::/48", "VRF BLUE",
        ])

    def test_indented_ios_summary(self):
        self.assertEqual(summarize_routing(IOS_INDENTED), ["OSPF 7", "VRF 20"])


if __name__ == "__main__":
    unittest.main()
