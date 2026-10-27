# Copyright 2026 Cisco Systems, Inc. and its affiliates
# SPDX-License-Identifier: Apache-2.0

"""IPv6 addresses Network Sketcher accepts, and IPv6 read from each source."""

from __future__ import annotations

import csv
import json
import pathlib
import re
import subprocess
import sys
import tempfile
import unittest

from aci_converter.src.aci_logical_mapper import _host_cidr as aci_host_cidr
from aci_converter.src.ns_command_builder import (
    build_command_script, cmd_add_ip_address_bulk, ns_ip_cidr,
)
from aci_converter.src.ns_model import NSIPAssignment, NSModel
from catc_converter.src.catc_logical_mapper import _routable_ipv6
from catc_converter.src.catc_physical_mapper import interface_cidrs
from cml_converter.src.config_parser import parse_running_config as cml_parse
from config_converter.src.config_parser import parse_all, parse_running_config as cfg_parse
from config_converter.src.topology_mapper import build_model as config_build_model
from fmc_converter.src.fmc_mapper import build_model as fmc_build_model, gateway_cidr
from fmc_converter.src.fmc_reader import ipv6_cidrs as fmc_ipv6_cidrs, parse_export
from meraki_converter.src.meraki_topology_mapper import (
    _appliance_ipv6_cidrs, _switch_ipv6_cidr,
)
from nd_converter.src.nd_topology import network_l2_info
from sdwan_converter.src.sdwan_topology import (
    controller_vpn0_interfaces, ipv6_by_port, ipv6_cidrs as sdwan_ipv6_cidrs,
)

ROOT = pathlib.Path(__file__).resolve().parents[1]
BUILDERS = [
    "template_converter/src/ns_command_builder.py",
    "aci_converter/src/ns_command_builder.py",
    "catc_converter/src/ns_command_builder.py",
    "cml_converter/src/ns_command_builder.py",
    "config_converter/src/ns_command_builder.py",
    "fmc_converter/src/ns_command_builder.py",
    "meraki_converter/src/ns_command_builder.py",
    "nd_converter/src/ns_command_builder.py",
    "sdwan_converter/src/ns_command_builder.py",
    "3rd_party/netbox_converter/src/ns_command_builder.py",
]

DUAL_STACK = """hostname {name}
interface GigabitEthernet0/0
 no switchport
 ip address 10.0.0.{v4} 255.255.255.252
 ipv6 address 2001:DB8:0:1::{v4}/64
 ipv6 address FE80::{v4} link-local
interface GigabitEthernet0/1
 no switchport
 ipv6 address 2001:db8:0:9::{v6}/127
 ipv6 address 2001:db8:0:2::/64 eui-64
interface Vlan10
 ipv6 address autoconfig
end
"""


class BuilderTests(unittest.TestCase):
    def test_accepted_and_rejected_kinds(self):
        self.assertEqual(ns_ip_cidr("10.0.0.1/24"), ("10.0.0.1/24", ""))
        self.assertEqual(ns_ip_cidr("2001:DB8:0::1/64"), ("2001:db8::1/64", ""))
        self.assertEqual(ns_ip_cidr("fd00:1::1/64"), ("fd00:1::1/64", ""))
        for raw, reason in (
            ("fe80::1/64", "link-local"),
            ("ff02::1/128", "multicast"),
            ("::1/128", "loopback"),
            ("::/128", "unspecified"),
            ("::ffff:10.0.0.1/128", "IPv4-mapped"),
            ("fec0::1/64", "site-local"),
            ("fe80::1%eth0/64", "zone index"),
            ("2001:db8::1", "no prefix length"),
            ("2001:db8::zz/64", "invalid IPv6"),
        ):
            self.assertEqual(ns_ip_cidr(raw), (None, reason), raw)

    def test_rejected_ipv6_leaves_ipv4_in_the_row(self):
        model = NSModel()
        model.ip_assignments = [
            NSIPAssignment("r1", "Vlan 10", ["10.0.0.1/24", "fe80::1/64", "2001:db8::1/64"]),
            NSIPAssignment("r1", "Vlan 20", ["fe80::2/64"]),
        ]
        dropped = []
        cmd = cmd_add_ip_address_bulk(model, dropped)
        self.assertEqual(
            cmd,
            "add ip_address_bulk \"[['r1','Vlan 10',['10.0.0.1/24','2001:db8::1/64']]]\"",
        )
        self.assertEqual(
            dropped,
            [("r1", "Vlan 10", "fe80::1/64", "link-local"),
             ("r1", "Vlan 20", "fe80::2/64", "link-local")],
        )
        script = build_command_script(model)
        self.assertEqual(len(script.dropped_ips), 2)

    def test_ip_bulk_code_is_the_same_in_every_copy(self):
        bodies = set()
        for rel in BUILDERS:
            text = (ROOT / rel).read_text(encoding="utf-8").replace("\r\n", "\n")
            match = re.search(
                r"def ns_ip_cidr\(.*?\n(?=def cmd_rename_l3_instance)", text, re.S)
            self.assertIsNotNone(match, rel)
            bodies.add(match.group(0))
        self.assertEqual(len(bodies), 1)


class SourceTests(unittest.TestCase):
    def test_nd_ipv6_gateway_list_is_split(self):
        info = network_l2_info({
            "networkName": "N1",
            "networkTemplateConfig": json.dumps({
                "gatewayIpAddress": "10.1.1.1/24",
                "gatewayIpV6Address": "2001:db8:1::1/64, 2001:db8:2::1/64",
            }),
        })
        self.assertEqual(info["gw_v6"], ["2001:db8:1::1/64", "2001:db8:2::1/64"])

    def test_aci_host_cidr_keeps_an_existing_prefix(self):
        self.assertEqual(aci_host_cidr("10.0.0.5"), "10.0.0.5/32")
        self.assertEqual(aci_host_cidr("2001:db8::5"), "2001:db8::5/128")
        self.assertEqual(aci_host_cidr("2001:db8::5/64"), "2001:db8::5/64")

    def test_running_config_parsers(self):
        for parse in (cfg_parse, cml_parse):
            parsed = parse(DUAL_STACK.format(name="R1", v4=1, v6=1))
            gi0 = parsed.interfaces["GigabitEthernet0/0"]
            gi1 = parsed.interfaces["GigabitEthernet0/1"]
            self.assertEqual(gi0.all_cidrs(), ["10.0.0.1/30", "2001:db8:0:1::1/64"])
            self.assertEqual(gi1.all_cidrs(), ["2001:db8:0:9::1/127"])
            self.assertTrue(gi1.is_routed())
            self.assertEqual(parsed.interfaces["Vlan10"].all_cidrs(), [])

    def test_config_links_ipv6_only_port_once(self):
        parsed = parse_all({
            "R1": DUAL_STACK.format(name="R1", v4=1, v6=0),
            "R2": DUAL_STACK.format(name="R2", v4=2, v6=1),
        })
        model, _mappings, _report = config_build_model(parsed, {})
        links = sorted((lk.a_device, lk.a_port, lk.b_device, lk.b_port) for lk in model.l1_links)
        self.assertEqual(links, [
            ("R1", "GigabitEthernet 0/0", "R2", "GigabitEthernet 0/0"),
            ("R1", "GigabitEthernet 0/1", "R2", "GigabitEthernet 0/1"),
        ])
        ips = {(ip.device, ip.port): ip.cidrs for ip in model.ip_assignments}
        self.assertEqual(ips[("R1", "GigabitEthernet 0/0")], ["10.0.0.1/30", "2001:db8:0:1::1/64"])
        self.assertEqual(ips[("R2", "GigabitEthernet 0/1")], ["2001:db8:0:9::1/127"])

    def test_fmc_ipv6_addresses_and_default_route(self):
        self.assertEqual(fmc_ipv6_cidrs({
            "linkLocalAddress": "fe80::1",
            "addresses": [
                {"address": "2001:DB8:1::1", "prefix": "64"},
                {"address": "2001:db8:2::", "prefix": "64", "enforceEUI64": True},
                {"address": "fe80::5/64"},
            ],
        }), ["2001:db8:1::1/64"])
        self.assertEqual(gateway_cidr("2001:db8:1::fe", ["10.0.0.2/24", "2001:db8:1::1/64"]),
                         "2001:db8:1::fe/64")
        self.assertEqual(gateway_cidr("2001:db8:9::1", ["10.0.0.2/24"]), "2001:db8:9::1/128")
        doc = {
            "fmc": {"name": "FMC"},
            "devices": [{
                "id": "fw", "name": "FTD", "hostName": "edge",
                "interfaces": [{
                    "type": "PhysicalInterface", "name": "GigabitEthernet0/0",
                    "ifname": "outside", "enabled": True,
                    "ipv4": {"static": {"address": "10.0.0.2", "netmask": "24"}},
                    "ipv6": {"addresses": [{"address": "2001:db8:1::2", "prefix": "64"}]},
                }],
                "static_routes": [
                    {"interfaceName": "outside", "selectedNetworks": [{"name": "any-ipv6"}],
                     "gateway": {"literal": {"value": "2001:db8:1::1"}}},
                    {"interfaceName": "outside", "selectedNetworks": [{"name": "any-ipv4"}],
                     "gateway": {"literal": {"value": "10.0.0.1"}}},
                ],
            }],
        }
        model, _devices, _report = fmc_build_model(parse_export(doc), {})
        ips = {(ip.device, ip.port): ip.cidrs for ip in model.ip_assignments}
        self.assertEqual(ips[("edge", "GigabitEthernet 0/0")], ["10.0.0.2/24", "2001:db8:1::2/64"])
        self.assertEqual(ips[("Dummy_Area_1_Dummy_Outside", "GigabitEthernet 0/0")],
                         ["2001:db8:1::1/64", "10.0.0.1/24"])

    def test_fmc_link_local_next_hop_is_reported(self):
        doc = {"fmc": {}, "devices": [{
            "id": "fw", "hostName": "edge",
            "interfaces": [{"type": "PhysicalInterface", "name": "GigabitEthernet0/0",
                            "ifname": "outside",
                            "ipv6": {"addresses": [{"address": "2001:db8:1::2", "prefix": "64"}]}}],
            "static_routes": [{"interfaceName": "outside",
                               "selectedNetworks": [{"value": "::/0"}],
                               "gateway": {"literal": {"value": "fe80::1"}}}],
        }]}
        model, _devices, report = fmc_build_model(parse_export(doc), {})
        self.assertTrue(any("link-local" in note for note in report.notes))
        self.assertFalse(any(ip.device.endswith("Dummy_Outside") for ip in model.ip_assignments))

    def test_catc_ipv6(self):
        self.assertEqual(_routable_ipv6(["fe80::5054:ff:fe45:ffda", "2001:db8::10"]),
                         ["2001:db8::10"])
        self.assertEqual(interface_cidrs({
            "ipv4Address": "10.0.0.1", "ipv4Mask": "255.255.255.0",
            "addresses": [
                {"type": "IPV4_PRIMARY", "address": {"ipAddress": {"address": "10.0.0.1"},
                                                      "ipMask": {"address": "255.255.255.0"}}},
                {"type": "IPV4_SECONDARY", "address": {"ipAddress": {"address": "10.0.1.1"},
                                                        "ipMask": {"address": "255.255.255.0"}}},
                {"type": "IPV6", "address": {"ipAddress": {"address": "2001:db8::1"},
                                              "ipMask": {"address": "64"}}},
            ],
        }), ["10.0.0.1/24", "10.0.1.1/24", "2001:db8::1/64"])

    def test_sdwan_dash_placeholder_and_ipv6(self):
        rows = [
            {"vpn-id": "0", "ifname": "eth0", "ip-address": "-", "af-type": "ipv6"},
            {"vpn-id": "0", "ifname": "eth1", "ip-address": "172.16.0.101/24"},
            {"vpn-id": "0", "ifname": "eth1", "ip-address": "-",
             "ipv6-address": "2001:db8:16::101/64"},
            {"vpn-id": "0", "ifname": "eth2", "ipv6-addrs": ["fe80::1", "fe80::1/64"]},
        ]
        self.assertEqual([r["ifname"] for r in controller_vpn0_interfaces(rows)], ["eth1"])
        self.assertEqual(ipv6_by_port(rows), {(0, "eth1"): ["2001:db8:16::101/64"]})
        self.assertEqual(sdwan_ipv6_cidrs({"ipv6-addrs": "2001:db8::1/64, fe80::1/64"}),
                         ["2001:db8::1/64"])

    def test_meraki_ipv6(self):
        self.assertEqual(_appliance_ipv6_cidrs({
            "enabled": True,
            "prefixAssignments": [
                {"autonomous": False, "staticPrefix": "2001:db8:15::/64",
                 "staticApplianceIp6": "2001:db8:15::1"},
                {"autonomous": True, "origin": {"type": "internet"}},
            ],
        }), ["2001:db8:15::1/64"])
        self.assertEqual(_appliance_ipv6_cidrs({"enabled": False}), [])
        self.assertEqual(_switch_ipv6_cidr({"ipv6": {
            "assignmentMode": "static", "address": "2001:db8:20::1", "prefix": "2001:db8:20::/64",
        }}), "2001:db8:20::1/64")
        self.assertIsNone(_switch_ipv6_cidr({"ipv6": {"assignmentMode": "eui-64"}}))


class SnaTests(unittest.TestCase):
    def test_ipv6_flows_do_not_stop_the_run(self):
        cols = ["searchSubject.ipAddress", "peer.ipAddress", "peer.portProtocol.port",
                "searchSubject.portProtocol.protocol", "peer.synAckPackets",
                "connection.transferBytes", "activeDuration"]
        with tempfile.TemporaryDirectory() as tmp:
            base = pathlib.Path(tmp)
            (base / "in").mkdir()
            with open(base / "in" / "flows.csv", "w", newline="", encoding="utf-8") as fh:
                writer = csv.writer(fh)
                writer.writerow(cols)
                for i in range(300):
                    writer.writerow([f"2001:db8:{i % 7}::{i + 1}", "2001:db8:ff::1",
                                     "443", "TCP", "1", "90000", "2000"])
                    writer.writerow([f"10.1.{i % 4}.{i % 200 + 10}", "10.9.0.10",
                                     "443", "TCP", "1", "90000", "2000"])
            cfg = json.loads((ROOT / "sna_converter" / "sna_to_ns_config.json")
                             .read_text(encoding="utf-8"))
            cfg["inside_outinit_ratio"] = {"value": 0.5}
            cfg["inside_outinit_min"] = {"value": 10}
            cfg["inside_public"] = {"value": ["2001:db8:"]}
            (base / "cfg.json").write_text(json.dumps(cfg), encoding="utf-8")
            run = subprocess.run(
                [sys.executable, str(ROOT / "sna_converter" / "sna_to_ns_commands.py"),
                 "--input-dir", str(base / "in"), "--output-dir", str(base / "out"),
                 "--config", str(base / "cfg.json")],
                capture_output=True, text=True, encoding="utf-8", errors="replace",
                env={**__import__("os").environ, "PYTHONIOENCODING": "utf-8"},
            )
            self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
            self.assertIn("300 flow row(s) with an IPv6 endpoint were skipped", run.stdout)


if __name__ == "__main__":
    unittest.main()
