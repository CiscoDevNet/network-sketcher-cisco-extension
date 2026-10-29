# Copyright 2026 Cisco Systems, Inc. and its affiliates
# SPDX-License-Identifier: Apache-2.0

"""Untagged (native) VLANs are written with Network Sketcher's '_UNTAG_' suffix.

Every config and export below is synthetic.
"""

from __future__ import annotations

import json
import pathlib
import sys
import tempfile
import unittest

from cml_converter.src import topology_mapper as cml_tm
from cml_converter.src.config_parser import parse_running_config as cml_parse
from cml_converter.src.ns_command_builder import cmd_add_vport_l2_direct_binding as cml_bind
from config_converter.src import topology_mapper as cfg_tm
from config_converter.src.config_parser import ParsedInterface, parse_running_config as cfg_parse
from config_converter.src.ns_model import (
    NSL1Link, NSL2Segment, NSModel, NSPortChannel, NSSubInterface,
)
from config_converter.src.untag import apply_untag_consistency, vlan_segments
from meraki_converter.src import meraki_topology_mapper as mk
from meraki_converter.src.meraki_reader import load_export as meraki_load
from tests.test_cml_overlay import _convert, _lab_yaml

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "3rd_party"))
from netbox_converter.src.netbox_mapper import build_model as netbox_build  # noqa: E402
from netbox_converter.src.netbox_reader import load_export as netbox_load  # noqa: E402


def _segs(model, device, port):
    return [n for s in model.l2_segments_phys if (s.device, s.port) == (device, port) for n in s.vlans]


class HelperTests(unittest.TestCase):
    def test_vlan_segments_marks_only_the_native(self):
        self.assertEqual(vlan_segments([1, 10], 1), ["Vlan1_UNTAG_", "Vlan10"])
        self.assertEqual(vlan_segments([10, 20], 1), ["Vlan10", "Vlan20"])
        self.assertEqual(vlan_segments([10], None), ["Vlan10"])

    def _model(self, a, b, link=("SW1", "Gi 1", "SW2", "Gi 1")):
        m = NSModel()
        m.l1_links.append(NSL1Link(*link))
        m.l2_segments_phys.append(NSL2Segment(link[0], link[1], list(a)))
        if b is not None:
            m.l2_segments_phys.append(NSL2Segment(link[2], link[3], list(b)))
        return m

    def test_access_port_facing_a_native_vlan_becomes_untag(self):
        m = self._model(["Vlan1_UNTAG_", "Vlan10"], ["Vlan1"])
        self.assertEqual(apply_untag_consistency(m), 1)
        self.assertEqual(_segs(m, "SW2", "Gi 1"), ["Vlan1_UNTAG_"])

    def test_native_mismatch_is_left_alone(self):
        m = self._model(["Vlan1_UNTAG_", "Vlan99"], ["Vlan99_UNTAG_", "Vlan1"])
        self.assertEqual(apply_untag_consistency(m), 0)
        self.assertEqual(_segs(m, "SW2", "Gi 1"), ["Vlan99_UNTAG_", "Vlan1"])

    def test_chain_of_dummies_follows(self):
        m = self._model(["Vlan5_UNTAG_"], ["Vlan5"])
        m.l1_links.append(NSL1Link("SW2", "Gi 2", "Dummy_1", "Gi 0"))
        m.l2_segments_phys.append(NSL2Segment("SW2", "Gi 2", ["Vlan5"]))
        m.l2_segments_phys.append(NSL2Segment("Dummy_1", "Gi 0", ["Vlan5"]))
        apply_untag_consistency(m)
        self.assertEqual(_segs(m, "SW2", "Gi 1"), ["Vlan5_UNTAG_"])
        # SW2 Gi 2 is a different port; its own far end decides it.
        self.assertEqual(_segs(m, "SW2", "Gi 2"), ["Vlan5"])

    def test_port_channel_is_read_on_the_logical_port(self):
        m = NSModel()
        m.l1_links.append(NSL1Link("SW1", "Gi 1", "SW2", "Gi 1"))
        m.port_channels.append(NSPortChannel("SW1", ["Gi 1"], "Port-channel 1"))
        m.port_channels.append(NSPortChannel("SW2", ["Gi 1"], "Port-channel 1"))
        m.l2_segments_phys.append(NSL2Segment("SW1", "Port-channel 1", ["Vlan1_UNTAG_", "Vlan10"]))
        m.l2_segments_phys.append(NSL2Segment("SW2", "Port-channel 1", ["Vlan1", "Vlan10"]))
        apply_untag_consistency(m)
        self.assertEqual(_segs(m, "SW2", "Port-channel 1"), ["Vlan1_UNTAG_", "Vlan10"])

    def test_subinterface_facing_a_native_vlan_becomes_native(self):
        m = self._model(["Vlan1_UNTAG_", "Vlan20"], None, link=("SW1", "Gi 1", "R1", "Gi 0"))
        m.subinterfaces.append(NSSubInterface("R1", "Gi 0", "Gi 0.1", vlan_id=1))
        m.subinterfaces.append(NSSubInterface("R1", "Gi 0", "Gi 0.20", vlan_id=20))
        apply_untag_consistency(m)
        self.assertEqual([si.native for si in m.subinterfaces], [True, False])


class ConfigParserTests(unittest.TestCase):
    CFG = """hostname R1
interface GigabitEthernet0/0.1
 encapsulation dot1Q 1 native
 ip address 192.0.2.1 255.255.255.0
interface GigabitEthernet0/0.20
 encapsulation dot1Q 20
 ip address 198.51.100.1 255.255.255.0
"""

    def test_dot1q_upper_case_and_native(self):
        for parse in (cfg_parse, cml_parse):
            ifs = parse(self.CFG, "R1").interfaces
            self.assertEqual((ifs["GigabitEthernet0/0.1"].access_vlan, ifs["GigabitEthernet0/0.1"].dot1q_native), (1, True))
            self.assertEqual((ifs["GigabitEthernet0/0.20"].access_vlan, ifs["GigabitEthernet0/0.20"].dot1q_native), (20, False))

    def test_trunk_native_explicit_and_default(self):
        explicit = ParsedInterface(name="Gi0/1", kind="physical", mode="trunk",
                                   trunk_native_vlan=99, trunk_allowed_vlans=[10, 99])
        default = ParsedInterface(name="Gi0/2", kind="physical", mode="trunk", trunk_allowed_vlans=[1, 20])
        pruned = ParsedInterface(name="Gi0/3", kind="physical", mode="trunk", trunk_allowed_vlans=[10, 20])
        for fn in (lambda i: cfg_tm._trunk_vlans(i), cml_tm._trunk_vlans):
            self.assertEqual(fn(explicit), ["Vlan10", "Vlan99_UNTAG_"])
            self.assertEqual(fn(default), ["Vlan1_UNTAG_", "Vlan20"])
            self.assertEqual(fn(pruned), ["Vlan10", "Vlan20"])

    def test_binding_is_quoted_upper_case_and_native(self):
        m = cml_tm.NSModel()
        m.subinterfaces.append(cml_tm.NSSubInterface("R1", "Gi 0", "Gi 0.1", vlan_id=1, native=True))
        m.subinterfaces.append(cml_tm.NSSubInterface("R1", "Gi 0", "Gi 0.20", vlan_id=20))
        self.assertEqual(cml_bind(m), [
            "add vport_l2_direct_binding 'R1' 'Gi 0.1' 'Vlan1_UNTAG_'",
            "add vport_l2_direct_binding 'R1' 'Gi 0.20' 'Vlan20'",
        ])


SW1 = """hostname SW1
interface GigabitEthernet0/1
 switchport trunk encapsulation dot1q
 switchport trunk native vlan 99
 switchport trunk allowed vlan 10,99
 switchport mode trunk
interface GigabitEthernet0/2
 switchport trunk allowed vlan 1,20
 switchport mode trunk
interface GigabitEthernet0/3
 switchport trunk allowed vlan 1,10
 switchport mode trunk
"""
SW2 = """hostname SW2
interface GigabitEthernet0/1
 switchport trunk native vlan 99
 switchport trunk allowed vlan 10,99
 switchport mode trunk
"""
SW3 = """hostname SW3
interface GigabitEthernet0/3
 switchport access vlan 1
 switchport mode access
"""
R1 = """hostname R1
interface GigabitEthernet0/0
 no ip address
interface GigabitEthernet0/0.1
 encapsulation dot1Q 1 native
 ip address 192.0.2.1 255.255.255.0
interface GigabitEthernet0/0.20
 encapsulation dot1Q 20
 ip address 198.51.100.1 255.255.255.0
"""


class CmlEndToEndTests(unittest.TestCase):
    def test_trunks_access_and_router_on_a_stick(self):
        doc = _lab_yaml("untag", [
            ("SW1", "iosvl2", ["GigabitEthernet0/1", "GigabitEthernet0/2", "GigabitEthernet0/3"], SW1),
            ("SW2", "iosvl2", ["GigabitEthernet0/1"], SW2),
            ("SW3", "iosvl2", ["GigabitEthernet0/3"], SW3),
            ("R1", "iosv", ["GigabitEthernet0/0"], R1),
        ], [
            ("SW1", "GigabitEthernet0/1", "SW2", "GigabitEthernet0/1"),
            ("SW1", "GigabitEthernet0/2", "R1", "GigabitEthernet0/0"),
            ("SW1", "GigabitEthernet0/3", "SW3", "GigabitEthernet0/3"),
        ])
        run = _convert(doc)
        self.assertEqual(run.rc, 0, run.stderr)
        script = (run.out_dir / "ns_commands.txt").read_text(encoding="utf-8")
        self.assertIn("['SW1','GigabitEthernet 0/1',['Vlan10','Vlan99_UNTAG_']]", script)
        self.assertIn("['SW2','GigabitEthernet 0/1',['Vlan10','Vlan99_UNTAG_']]", script)
        self.assertIn("['SW1','GigabitEthernet 0/2',['Vlan1_UNTAG_','Vlan20']]", script)
        self.assertIn("['SW3','GigabitEthernet 0/3',['Vlan1_UNTAG_']]", script)
        self.assertIn("add vport_l2_direct_binding 'R1' 'GigabitEthernet 0/0.1' 'Vlan1_UNTAG_'", script)
        self.assertIn("add vport_l2_direct_binding 'R1' 'GigabitEthernet 0/0.20' 'Vlan20'", script)
        self.assertNotIn("' vlan", script)


class MerakiTests(unittest.TestCase):
    def test_trunk_native_and_access(self):
        self.assertEqual(mk._port_vlans({"type": "trunk", "vlan": 5, "allowedVlans": "5,10"}),
                         ["Vlan5_UNTAG_", "Vlan10"])
        self.assertEqual(mk._port_vlans({"type": "access", "vlan": 5}), ["Vlan5"])

    def test_appliance_native_vlan(self):
        self.assertEqual(mk._appliance_native_vlan({"appliancePorts": [
            {"number": 2, "enabled": True, "type": "access", "vlan": 3},
            {"number": 3, "enabled": True, "type": "trunk", "vlan": 7, "dropUntaggedTraffic": False},
        ]}), 7)
        self.assertIsNone(mk._appliance_native_vlan({"appliancePorts": [
            {"number": 3, "enabled": True, "type": "trunk", "vlan": 7, "dropUntaggedTraffic": True},
        ]}))
        self.assertIsNone(mk._appliance_native_vlan({}))

    def test_bundled_sample_switch_mx_and_ap_share_the_untagged_vlan(self):
        export = meraki_load(str(ROOT / "meraki_converter" / "Input_data" / "meraki_export.json"))
        model, _info = mk.build_model(export, {})
        by_port = {(s.device, s.port): s.vlans for s in model.l2_segments_phys}
        for lk in model.l1_links:
            a = by_port.get((lk.a_device, lk.a_port))
            b = by_port.get((lk.b_device, lk.b_port))
            if a and b:
                ua = [n for n in a if n.endswith("_UNTAG_")]
                ub = [n for n in b if n.endswith("_UNTAG_")]
                self.assertEqual(bool(ua), bool(ub), (lk, a, b))
        self.assertTrue(any(n.endswith("_UNTAG_") for v in by_port.values() for n in v))


class NetboxTests(unittest.TestCase):
    def test_tagged_port_untagged_vlan_is_native(self):
        def iface(i, dev, name, peer_dev, peer_name, mode, unt, tagged):
            return {
                "id": i, "name": name, "device": {"id": dev}, "type": {"value": "1000base-t"},
                "mode": {"value": mode}, "untagged_vlan": {"vid": unt},
                "tagged_vlans": [{"vid": v} for v in tagged],
                "connected_endpoints_type": "dcim.interface",
                "connected_endpoints_reachable": True,
                "connected_endpoints": [{"device": {"id": peer_dev}, "name": peer_name}],
            }
        doc = {
            "_meta": {}, "sites": [{"id": 1, "slug": "s1", "name": "s1"}],
            "devices": [
                {"id": 1, "name": "sw1", "site": {"id": 1, "slug": "s1"}, "role": {"slug": "access-switch"}},
                {"id": 2, "name": "sw2", "site": {"id": 1, "slug": "s1"}, "role": {"slug": "access-switch"}},
            ],
            "interfaces": [
                iface(11, 1, "GigabitEthernet1/0/1", 2, "GigabitEthernet1/0/1", "tagged", 99, [10]),
                iface(21, 2, "GigabitEthernet1/0/1", 1, "GigabitEthernet1/0/1", "access", 99, []),
            ],
        }
        with tempfile.TemporaryDirectory() as d:
            path = pathlib.Path(d) / "nb.json"
            path.write_text(json.dumps(doc), encoding="utf-8")
            model, _maps, _report = netbox_build(netbox_load(str(path)))
        by_dev = {s.device: s.vlans for s in model.l2_segments_phys}
        self.assertEqual(by_dev["sw1"], ["Vlan99_UNTAG_", "Vlan10"])
        self.assertEqual(by_dev["sw2"], ["Vlan99_UNTAG_"])


if __name__ == "__main__":
    unittest.main()
