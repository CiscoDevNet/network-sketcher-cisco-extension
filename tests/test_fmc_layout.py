# Copyright 2026 Cisco Systems, Inc. and its affiliates
# SPDX-License-Identifier: Apache-2.0

"""Firewall areas, interface direction, HA, and the management waypoint."""

from __future__ import annotations

import unittest

from fmc_converter.src.fmc_mapper import GRAY, WAN_AREA, build_model
from fmc_converter.src.fmc_reader import ipv4_cidr, parse_export
from fmc_converter.src.ns_command_builder import build_command_script


def _iface(name, ifname="", zone="", enabled=True, kind="PhysicalInterface",
           management_only=False, ipv4="", members=None, vlan=None):
    raw = {
        "type": kind,
        "name": name,
        "ifname": ifname,
        "enabled": enabled,
        "managementOnly": management_only,
        "securityZone": {"name": zone} if zone else None,
    }
    if ipv4:
        address, prefix = ipv4.split("/")
        raw["ipv4"] = {"static": {"address": address, "netmask": _mask(int(prefix))}}
    if members:
        raw["selectedInterfaces"] = [{"name": member} for member in members]
    if vlan is not None:
        raw["vlanId"] = vlan
    return raw


def _mask(prefix: int) -> str:
    bits = (0xFFFFFFFF << (32 - prefix)) & 0xFFFFFFFF
    return ".".join(str((bits >> shift) & 255) for shift in (24, 16, 8, 0))


def _device(device_id, hostname, interfaces, name="", routes=None):
    raw = {
        "id": device_id,
        "name": name or hostname,
        "hostName": hostname,
        "model": "Cisco Secure Firewall Threat Defense for VMware",
        "sw_version": "7.6.4",
        "interfaces": interfaces,
    }
    if routes:
        raw["static_routes"] = routes
    return raw


def _export(devices, pairs=None, fmc_name="FMC", fmc_ipv4="", fmc_gateway=""):
    return {
        "fmc": {
            "name": fmc_name,
            "version": "10.0.1 (build 1)",
            "ipv4": fmc_ipv4,
            "gateway": fmc_gateway,
        },
        "devices": devices,
        "ha_pairs": pairs or [],
    }


def _model(document, cfg=None):
    model, _devices, report = build_model(parse_export(document), cfg or {})
    return model, report


def _at(grid, name):
    for row_index, row in enumerate(grid):
        if name in row:
            return row_index, row.index(name)
    raise AssertionError(f"{name} is not in {grid}")


def _pairs(model):
    found = set()
    for link in model.l1_links:
        found.add(frozenset({link.a_device, link.b_device}))
    return found


class LayoutTests(unittest.TestCase):
    def test_lab_directions_and_management_path(self):
        area = "198.18.133.11"
        fw_area = "Dummy_Area_1"
        model, report = _model(_export([_device("fw1", area, [
            _iface("GigabitEthernet0/0", "INDUSTRIAL", ipv4="10.1.0.1/24"),
            _iface("GigabitEthernet0/1", "IDMZ", ipv4="10.2.0.1/24"),
            _iface("GigabitEthernet0/2", "ENTERPRISE", ipv4="10.3.0.1/24"),
            _iface("GigabitEthernet0/3", enabled=False),
            _iface("GigabitEthernet0/4", enabled=False),
            _iface("Management0/0", "management", management_only=True),
        ], name="FTD")]))
        grid = model.area_to_devices[fw_area]
        firewall = _at(grid, area)
        management = _at(grid, f"{fw_area}_Dummy_mgmt1")
        dmz = _at(grid, f"{fw_area}_Dummy_DMZ")
        inside = _at(grid, f"{fw_area}_Dummy_Inside")
        inside2 = _at(grid, f"{fw_area}_Dummy_Inside2")

        self.assertLess(management[0], firewall[0])
        self.assertEqual(management[1], firewall[1])
        self.assertEqual(dmz[0], firewall[0])
        self.assertGreater(dmz[1], firewall[1])
        self.assertGreater(inside[0], firewall[0])
        self.assertGreater(inside2[0], firewall[0])
        self.assertEqual(inside[0], inside2[0])
        self.assertNotIn("_AIR_", str(model.areas))
        self.assertEqual(model.areas, [["FMC"], [WAN_AREA], [fw_area]])
        self.assertTrue(all(
            sum(name.endswith("_wp_") for name in row) <= 1 for row in model.areas
        ))

        pairs = _pairs(model)
        self.assertIn(frozenset({f"{fw_area}_Dummy_mgmt1", "Dummy_wan"}), pairs)
        self.assertIn(frozenset({"Dummy_wan", "Dummy_fmc"}), pairs)
        self.assertIn(frozenset({"FMC", "Dummy_fmc"}), pairs)
        self.assertNotIn(frozenset({f"{fw_area}_Dummy_mgmt1", "Dummy_fmc"}), pairs)
        self.assertNotIn(frozenset({f"{fw_area}_Dummy_mgmt1", "FMC"}), pairs)
        for link in model.l1_links:
            ends = {model.devices[link.a_device].area, model.devices[link.b_device].area}
            self.assertTrue(len(ends) == 1 or WAN_AREA in ends)
            self.assertNotIn("0/3", link.a_port + link.b_port)
            self.assertNotIn("0/4", link.a_port + link.b_port)

        def _ends(left: str, right: str) -> set:
            found = set()
            for link in model.l1_links:
                if {link.a_device, link.b_device} == {left, right}:
                    found.add((link.a_device, link.a_port))
                    found.add((link.b_device, link.b_port))
            return found

        mgmt_ports = _ends(area, f"{fw_area}_Dummy_mgmt1")
        self.assertIn((area, "Management 0/0"), mgmt_ports)
        self.assertIn((f"{fw_area}_Dummy_mgmt1", "GigabitEthernet 0/0"), mgmt_ports)
        dmz_ports = _ends(area, f"{fw_area}_Dummy_DMZ")
        self.assertIn((area, "GigabitEthernet 0/1"), dmz_ports)
        self.assertIn((f"{fw_area}_Dummy_DMZ", "GigabitEthernet 0/1"), dmz_ports)

        self.assertEqual(model.devices["FMC"].stencil.stencil_type, "Server")
        self.assertIsNone(model.devices["FMC"].default_color)
        self.assertEqual(model.devices[area].stencil.stencil_type, "Firewall")
        self.assertNotIn("FTD_198.18.133.11", model.devices)
        self.assertEqual(model.devices[area].area, fw_area)
        self.assertEqual(model.devices[f"{fw_area}_Dummy_Inside"].default_color, GRAY)
        self.assertEqual(model.devices["Dummy_wan"].stencil.stencil_type, "Cloud")
        self.assertEqual(model.devices["Dummy_wan"].default_color, GRAY)
        self.assertTrue(any("INDUSTRIAL -> bottom" in line for line in report.placements))
        self.assertTrue(any("IDMZ -> right" in line for line in report.placements))
        self.assertTrue(any("management -> management" in line for line in report.placements))
        self.assertTrue(any(ip.port == "GigabitEthernet 0/0" for ip in model.ip_assignments))
        script = build_command_script(model).text()
        self.assertIn('add area_location "[[\'FMC\'],[\'wan_wp_\'],[\'Dummy_Area_1\']]"', script)
        self.assertIn("[200, 200, 200]", script)
        self.assertIn("[235, 241, 222]", script)

    def test_ha_pair_is_one_area_side_by_side(self):
        model, report = _model(_export(
            [
                _device("p", "fw-primary", [
                    _iface("GigabitEthernet0/0", "inside"),
                    _iface("GigabitEthernet0/7", "failover"),
                ]),
                _device("s", "fw-secondary", [
                    _iface("GigabitEthernet0/0", "inside"),
                    _iface("GigabitEthernet0/7", "failover"),
                ]),
                _device("o", "fw-aaa", [_iface("GigabitEthernet0/0", "inside")]),
            ],
            [{"name": "HA", "primary": {"id": "p"}, "secondary": {"id": "s"}}],
        ))
        self.assertEqual(model.areas[2], ["Dummy_Area_1", "Dummy_Area_2"])
        self.assertEqual(model.devices["fw-aaa"].area, "Dummy_Area_1")
        self.assertEqual(model.devices["fw-primary"].area, "Dummy_Area_2")
        self.assertEqual(model.devices["fw-secondary"].area, "Dummy_Area_2")
        grid = model.area_to_devices["Dummy_Area_2"]
        primary = _at(grid, "fw-primary")
        secondary = _at(grid, "fw-secondary")
        self.assertEqual(primary[0], secondary[0])
        self.assertEqual(secondary[1], primary[1] + 1)
        self.assertIn(
            frozenset({"fw-primary", "fw-secondary"}), _pairs(model),
        )
        self.assertTrue(any("failover peer" in line for line in report.placements))
        self.assertFalse(any("failover" in name for name in model.devices))
        self.assertTrue(all(
            sum(name.endswith("_wp_") for name in row) <= 1 for row in model.areas
        ))

    def test_portchannel_and_subinterface(self):
        model, _report = _model(_export([_device("fw", "edge", [
            _iface("GigabitEthernet0/1"),
            _iface("GigabitEthernet0/2"),
            _iface(
                "Port-channel1", "outside", kind="EtherChannelInterface",
                members=["GigabitEthernet0/1", "GigabitEthernet0/2"],
                ipv4="10.1.1.1/24",
            ),
            _iface("GigabitEthernet0/0", "inside"),
            _iface(
                "GigabitEthernet0/0.10", "inside", kind="SubInterface",
                vlan=10, ipv4="10.10.10.1/24",
            ),
        ])]))
        self.assertIn("Dummy_Area_1_Dummy_Outside", model.devices)
        self.assertNotIn("Dummy_Area_1_Dummy_Outside2", model.devices)
        channel = model.port_channels[0]
        self.assertEqual(channel.portchannel_name, "Port-channel 1")
        self.assertEqual(
            channel.physical_ports,
            ["GigabitEthernet 0/1", "GigabitEthernet 0/2"],
        )
        self.assertTrue(any(
            ip.port == "Port-channel 1" and ip.cidrs == ["10.1.1.1/24"]
            for ip in model.ip_assignments
        ))
        sub = model.subinterfaces[0]
        self.assertEqual(sub.parent_port, "GigabitEthernet 0/0")
        self.assertEqual(sub.subif_port, "GigabitEthernet 0/0.10")
        self.assertEqual(sub.vlan_id, 10)
        self.assertTrue(any(ip.port == "GigabitEthernet 0/0.10" for ip in model.ip_assignments))
        script = build_command_script(model).text()
        self.assertIn("add portchannel_bulk", script)
        self.assertIn("Port-channel 1", script)

    def test_keyword_config_and_sanitized_area(self):
        model, report = _model(
            _export([_device("fw", "site/a", [
                _iface("GigabitEthernet0/0", "INDUSTRIAL"),
            ])]),
            {"top_keywords": ["industrial"]},
        )
        self.assertIn("Dummy_Area_1", model.area_to_devices)
        grid = model.area_to_devices["Dummy_Area_1"]
        firewall = _at(grid, "site_a")
        outside = _at(grid, "Dummy_Area_1_Dummy_Outside")
        self.assertLess(outside[0], firewall[0])
        self.assertTrue(any("INDUSTRIAL -> top" in line for line in report.placements))

    def test_disabled_string_is_not_drawn(self):
        model, report = _model(_export([_device("fw", "edge", [
            {"type": "PhysicalInterface", "name": "GigabitEthernet0/3",
             "ifname": "", "enabled": "false"},
            _iface("GigabitEthernet0/0", "inside"),
        ])]))
        ports = " ".join(
            f"{link.a_port} {link.b_port}" for link in model.l1_links
        )
        self.assertNotIn("0/3", ports)
        self.assertTrue(any("0/3" in line for line in report.skipped))

    def test_prefix_length_netmask(self):
        self.assertEqual(
            ipv4_cidr({"static": {"address": "10.0.0.1", "netmask": "24"}}),
            "10.0.0.1/24",
        )
        self.assertEqual(
            ipv4_cidr({"static": {"address": "10.0.0.1", "netmask": "255.255.255.0"}}),
            "10.0.0.1/24",
        )

    def test_default_gateway_on_facing_dummy(self):
        model, report = _model(_export(
            [_device("fw", "edge", [
                _iface("GigabitEthernet0/1", "IDMZ", ipv4="10.2.0.2/24"),
                _iface("GigabitEthernet0/0", "inside", ipv4="10.1.0.2/24"),
            ], routes=[
                {
                    "interfaceName": "IDMZ",
                    "metricValue": 1,
                    "selectedNetworks": [{"name": "any-ipv4", "value": "0.0.0.0/0"}],
                    "gateway": {"literal": {"value": "10.2.0.1"}},
                },
                {
                    "interfaceName": "inside",
                    "metricValue": 1,
                    "selectedNetworks": [{"name": "CELL-1", "value": "10.9.0.0/16"}],
                    "gateway": {"literal": {"value": "10.1.0.1"}},
                },
            ])],
            fmc_ipv4="192.0.2.10/24",
            fmc_gateway="192.0.2.1",
        ))
        assigned = {
            (ip.device, ip.port): tuple(ip.cidrs) for ip in model.ip_assignments
        }
        self.assertEqual(assigned[("Dummy_Area_1_Dummy_DMZ", "GigabitEthernet 0/1")], ("10.2.0.1/24",))
        self.assertNotIn(("Dummy_Area_1_Dummy_Inside", "GigabitEthernet 0/0"), assigned)
        self.assertEqual(assigned[("Dummy_fmc", "Ethernet 0")], ("192.0.2.1/24",))
        self.assertEqual(assigned[("FMC", "Ethernet 0")], ("192.0.2.10/24",))
        self.assertTrue(any("default gateway 10.2.0.1/24" in line for line in report.placements))
        self.assertTrue(any("Dummy_fmc Ethernet 0" in line for line in report.placements))


if __name__ == "__main__":
    unittest.main()
