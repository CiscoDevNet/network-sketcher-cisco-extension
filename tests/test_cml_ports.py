# Copyright 2026 Cisco Systems, Inc. and its affiliates
# SPDX-License-Identifier: Apache-2.0

"""Port names NS accepts, and no L2/L3 on ports an L1 link never creates."""

from __future__ import annotations

import unittest

from cml_converter.src.ns_command_builder import build_command_script
from cml_converter.src.topology_mapper import (
    NSIPAssignment, NSL1Link, NSModel, NSPortChannel, NSSubInterface,
    NSVirtualPort, normalise_port_name, prune_unlinked_ports,
)


class PortNameTests(unittest.TestCase):
    def test_sdwan_validator_ge_port(self):
        self.assertEqual(normalise_port_name("ge0/0"), "GigabitEthernet 0/0")

    def test_unknown_type_still_gets_a_space(self):
        self.assertEqual(normalise_port_name("xyz0/1"), "xyz 0/1")
        self.assertEqual(normalise_port_name("xyz_3"), "xyz 3")

    def test_known_names_unchanged(self):
        self.assertEqual(normalise_port_name("GigabitEthernet0/1"), "GigabitEthernet 0/1")
        self.assertEqual(normalise_port_name("eth0"), "Ethernet 0")
        self.assertEqual(normalise_port_name("port3"), "Ethernet 3")


class PruneUnlinkedPortsTests(unittest.TestCase):
    def test_ip_and_vrf_on_unlinked_port_are_dropped(self):
        model = NSModel()
        model.l1_links = [NSL1Link("r1", "GigabitEthernet 0/0", "r2", "GigabitEthernet 0/0")]
        model.virtual_ports = [NSVirtualPort(device="r1", port="Loopback 0", is_loopback=True)]
        model.ip_assignments = [
            NSIPAssignment("r1", "GigabitEthernet 0/0", ["10.0.0.1/30"]),
            NSIPAssignment("r1", "GigabitEthernet 0/2", ["10.0.2.1/24"]),
            NSIPAssignment("r1", "Loopback 0", ["10.255.0.1/32"]),
        ]
        model.vrf_renames = [
            ("r1", "GigabitEthernet 0/0", "10"),
            ("r1", "GigabitEthernet 0/2", "10"),
        ]
        prune_unlinked_ports(model)
        self.assertEqual(
            [(ip.device, ip.port) for ip in model.ip_assignments],
            [("r1", "GigabitEthernet 0/0"), ("r1", "Loopback 0")],
        )
        self.assertEqual(model.vrf_renames, [("r1", "GigabitEthernet 0/0", "10")])
        self.assertEqual(
            model.skipped_ports,
            [("r1", "GigabitEthernet 0/2", "ip address"),
             ("r1", "GigabitEthernet 0/2", "vrf 10")],
        )
        script = build_command_script(model).text()
        self.assertNotIn("GigabitEthernet 0/2", script)

    def test_portchannel_and_subif_need_a_linked_parent(self):
        model = NSModel()
        model.l1_links = [NSL1Link("r1", "GigabitEthernet 0/0", "r2", "GigabitEthernet 0/0")]
        model.port_channels = [
            NSPortChannel("r1", ["GigabitEthernet 0/0", "GigabitEthernet 0/1"], "Port-channel 1"),
            NSPortChannel("r1", ["GigabitEthernet 0/5"], "Port-channel 2"),
        ]
        model.subinterfaces = [
            NSSubInterface("r1", "GigabitEthernet 0/0", "GigabitEthernet 0/0.10", 10),
            NSSubInterface("r1", "GigabitEthernet 0/3", "GigabitEthernet 0/3.20", 20),
        ]
        model.ip_assignments = [
            NSIPAssignment("r1", "Port-channel 1", ["10.1.0.1/30"]),
            NSIPAssignment("r1", "Port-channel 2", ["10.2.0.1/30"]),
            NSIPAssignment("r1", "GigabitEthernet 0/0.10", ["10.10.0.1/24"]),
            NSIPAssignment("r1", "GigabitEthernet 0/3.20", ["10.20.0.1/24"]),
        ]
        prune_unlinked_ports(model)
        self.assertEqual(
            [(pc.portchannel_name, pc.physical_ports) for pc in model.port_channels],
            [("Port-channel 1", ["GigabitEthernet 0/0"])],
        )
        self.assertEqual([si.subif_port for si in model.subinterfaces],
                         ["GigabitEthernet 0/0.10"])
        self.assertEqual(
            [ip.port for ip in model.ip_assignments],
            ["Port-channel 1", "GigabitEthernet 0/0.10"],
        )
