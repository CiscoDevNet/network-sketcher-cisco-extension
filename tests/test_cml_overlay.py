# Copyright 2026 Cisco Systems, Inc. and its affiliates
# SPDX-License-Identifier: Apache-2.0

"""The CML integrated overlay (``--mode overlay``): EVPN, SD-WAN and L3VPN.

Every config below is synthetic and uses documentation addresses only.
"""

from __future__ import annotations

import contextlib
import io
import json
import pathlib
import tempfile
import unittest
from typing import Dict, List, Optional, Tuple

import yaml

from cml_converter.src.convert import main as convert_main

# ---------------------------------------------------------------------------
# Synthetic configs
# ---------------------------------------------------------------------------

NXOS_LEAF = """hostname {name}
feature nv overlay
feature vn-segment-vlan-based
nv overlay evpn
vlan 10
  name WEB
  vn-segment 30010
vlan 2500
  vn-segment 50000
vrf context BLUE
  vni 50000
  rd auto
  address-family ipv4 unicast
    route-target both auto evpn
interface Vlan10
  no shutdown
  vrf member BLUE
  ip address 10.10.10.1/24
  fabric forwarding mode anycast-gateway
interface Vlan2500
  no shutdown
  vrf member BLUE
  ip forward
interface nve1
  no shutdown
  host-reachability protocol bgp
  source-interface loopback1
  member vni 30010
    ingress-replication protocol bgp
  member vni 50000 associate-vrf
interface Ethernet1/1
  no switchport
  ip address {fabric_ip}
interface Ethernet1/2
  switchport
  switchport access vlan 10
{extra}"""

NXOS_BORDER_PORT = """interface Ethernet1/3
  no switchport
  vrf member BLUE
  ip address 192.0.2.1/30
"""

IOSXE_LEAF = """hostname xe-leaf1
l2vpn evpn
 replication-type static
 router-id Loopback1
!
l2vpn evpn instance 101 vlan-based
 encapsulation vxlan
!
vlan 101
 name APP
!
vlan configuration 101
 member evpn-instance 101 vni 10101
vlan configuration 901
 member vni 50901
!
vrf definition GREEN
 rd 65001:901
 address-family ipv4
  route-target export 65001:901 stitching
  route-target import 65001:901 stitching
!
interface Loopback1
 ip address 10.255.1.1 255.255.255.255
!
interface Vlan101
 vrf forwarding GREEN
 ip address 10.1.101.1 255.255.255.0
!
interface Vlan901
 vrf forwarding GREEN
 ip unnumbered Loopback1
!
interface GigabitEthernet1/0/2
 switchport access vlan 101
 switchport mode access
!
interface nve1
 source-interface Loopback1
 host-reachability protocol bgp
 member vni 10101 ingress-replication
 member vni 50901 vrf GREEN
!
"""

CEDGE = """hostname {name}
system
 system-ip {system_ip}
 site-id {site_id}
 organization-name example-org
!
vrf definition 10
 address-family ipv4
 exit-address-family
!
interface GigabitEthernet1
 ip address {wan_ip} 255.255.255.0
!
interface GigabitEthernet3
 vrf forwarding 10
 ip address {lan_ip} 255.255.255.252
!
interface GigabitEthernet4
 vrf forwarding 10
 ip address {lan2_ip} 255.255.255.0
!
interface Loopback10
 vrf forwarding 10
 ip address {loop_ip} 255.255.255.255
!
interface Tunnel1
 ip unnumbered GigabitEthernet1
 tunnel source GigabitEthernet1
 tunnel mode sdwan
!
sdwan
 interface GigabitEthernet1
  tunnel-interface
   encapsulation ipsec
   color biz-internet
  exit
 exit
!
"""

XR_PE = """hostname {name}
vrf CUST-A
 address-family ipv4 unicast
  import route-target
   65000:100
  !
  export route-target
   65000:100
  !
 !
!
interface Loopback0
 ipv4 address {loop_ip} 255.255.255.255
!
interface GigabitEthernet0/0/0/0
 ipv4 address {core_ip} 255.255.255.0
!
interface GigabitEthernet0/0/0/1
 vrf CUST-A
 ipv4 address {vrf_ip} 255.255.255.252
!
router bgp 65000
 address-family vpnv4 unicast
 !
 vrf CUST-A
  rd 65000:100
  address-family ipv4 unicast
   redistribute connected
!
segment-routing
 srv6
  locators
   locator MAIN
    prefix 2001:db8:0:{n}::/64
"""

IOS_PE = """hostname ios-pe1
ip vrf RED
 rd 65010:1
 route-target export 65010:1
 route-target import 65010:1
!
mpls label protocol ldp
!
interface GigabitEthernet0/0
 ip address 10.0.0.1 255.255.255.0
 mpls ip
!
interface GigabitEthernet0/1
 ip vrf forwarding RED
 ip address 198.51.100.9 255.255.255.252
!
router bgp 65010
 address-family vpnv4
  neighbor 10.0.0.2 activate
 exit-address-family
!
"""

XR_PE_BUNDLE = """hostname pe-be
vrf CUST
 address-family ipv4 unicast
  import route-target
   65000:200
  !
  export route-target
   65000:200
  !
 !
!
interface Bundle-Ether1
 description to-CE
 vrf CUST
 ipv4 address 198.51.100.1 255.255.255.252
!
interface Loopback0
 ipv4 address 10.255.255.11 255.255.255.255
!
interface GigabitEthernet0/0/0/0
 ipv4 address 10.0.13.1 255.255.255.252
!
interface GigabitEthernet0/0/0/1
 description to-CE
 bundle id 1 mode on
!
router bgp 65000
 address-family vpnv4 unicast
 !
 vrf CUST
  rd 1:200
  address-family ipv4 unicast
   redistribute connected
!
segment-routing
 srv6
  locators
   locator MAIN
    prefix 2001:db8:0:11::/64
"""

P_ROUTER = """hostname p1
interface Loopback0
 ipv4 address 10.255.255.12 255.255.255.255
!
interface GigabitEthernet0/0/0/0
 ipv4 address 10.0.13.2 255.255.255.252
!
"""

IOS_PE_PO = """hostname ios-pe-po
ip vrf RED
 rd 65010:2
 route-target export 65010:2
 route-target import 65010:2
!
interface Port-channel1
 ip vrf forwarding RED
 ip address 192.0.2.9 255.255.255.252
!
interface GigabitEthernet0/1
 channel-group 1 mode active
!
interface GigabitEthernet0/2
 channel-group 1 mode active
!
router bgp 65010
 address-family vpnv4
  neighbor 10.0.0.2 activate
 exit-address-family
!
"""

DAY0 = "hostname inserthostname-here\n"

CE = """hostname {name}
interface GigabitEthernet0/0
 ip address {ip} 255.255.255.252
!
"""

HOST = "ip addr add {ip} dev eth0\n"

# ---------------------------------------------------------------------------
# Lab builders
# ---------------------------------------------------------------------------

NodeSpec = Tuple[str, str, List[str], str]       # label, node_definition, ifaces, config
LinkSpec = Tuple[str, str, str, str]             # a, a_iface, b, b_iface


def _lab_yaml(title: str, nodes: List[NodeSpec], links: List[LinkSpec]) -> Dict:
    """A CML UI-export style lab (node-local interface ids, n1/i1 links)."""
    out_nodes, index = [], {}
    for n, (label, nd, ifaces, cfg) in enumerate(nodes):
        node = {
            "id": f"n{n}", "label": label, "node_definition": nd,
            "x": n * 100, "y": 0, "tags": [],
            "interfaces": [{"id": f"i{k}", "label": l, "type": "physical"}
                           for k, l in enumerate(ifaces)],
        }
        if cfg:
            node["configuration"] = cfg
        out_nodes.append(node)
        index[label] = (f"n{n}", {l: f"i{k}" for k, l in enumerate(ifaces)})
    out_links = [
        {"id": f"l{k}", "n1": index[a][0], "i1": index[a][1][ap],
         "n2": index[b][0], "i2": index[b][1][bp]}
        for k, (a, ap, b, bp) in enumerate(links)
    ]
    return {"lab": {"title": title, "version": "0.2.2"}, "nodes": out_nodes, "links": out_links}


def _lab_fetch_json(title: str, nodes: List[NodeSpec], links: List[LinkSpec],
                    prefix: str = "node") -> Dict:
    """The same lab as fetch_from_cml writes it (API ids, node_a/interface_a)."""
    out_nodes, index = [], {}
    for n, (label, nd, ifaces, cfg) in enumerate(nodes):
        nid = f"{prefix}-{n:04d}"
        ifs = [{"id": f"{nid}-if{k}", "label": l, "type": "physical", "node": nid}
               for k, l in enumerate(ifaces)]
        node = {"id": nid, "label": label, "node_definition": nd,
                "x": n * 100, "y": 0, "tags": [], "interfaces": ifs}
        if cfg:
            node["configuration"] = [{"name": "Main", "content": cfg}]
        out_nodes.append(node)
        index[label] = (nid, {l: f"{nid}-if{k}" for k, l in enumerate(ifaces)})
    out_links = [
        {"id": f"link-{k}", "node_a": index[a][0], "interface_a": index[a][1][ap],
         "node_b": index[b][0], "interface_b": index[b][1][bp]}
        for k, (a, ap, b, bp) in enumerate(links)
    ]
    return {"labs": [{"id": f"{prefix}-lab", "lab_title": title, "nodes": out_nodes, "links": out_links}]}


def _labs_fetch_json(*labs: Tuple[str, List[NodeSpec], List[LinkSpec]]) -> Dict:
    """Several labs in one fetch_from_cml document."""
    out: List[Dict] = []
    for i, (title, nodes, links) in enumerate(labs):
        out.extend(_lab_fetch_json(title, nodes, links, prefix=f"lab{i}")["labs"])
    return {"labs": out}


def _evpn_nodes(border: bool = False) -> List[NodeSpec]:
    return [
        ("spine1", "nxosv9000", ["Ethernet1/1", "Ethernet1/2"], "hostname spine1\n"),
        ("leaf1", "nxosv9000", ["Ethernet1/1", "Ethernet1/2", "Ethernet1/3"],
         NXOS_LEAF.format(name="leaf1", fabric_ip="10.0.0.1/31",
                          extra=NXOS_BORDER_PORT if border else "")),
        ("leaf2", "nxosv9000", ["Ethernet1/1", "Ethernet1/2"],
         NXOS_LEAF.format(name="leaf2", fabric_ip="10.0.0.3/31", extra="")),
        ("host1", "alpine", ["eth0"], HOST.format(ip="10.10.10.11/24")),
        ("host2", "alpine", ["eth0"], HOST.format(ip="10.10.10.12/24")),
    ]


EVPN_LINKS: List[LinkSpec] = [
    ("spine1", "Ethernet1/1", "leaf1", "Ethernet1/1"),
    ("spine1", "Ethernet1/2", "leaf2", "Ethernet1/1"),
    ("leaf1", "Ethernet1/2", "host1", "eth0"),
    ("leaf2", "Ethernet1/2", "host2", "eth0"),
]


def _cedge(name: str, site: int, n: int) -> NodeSpec:
    return (name, "cat-sdwan-edge",
            ["GigabitEthernet1", "GigabitEthernet2", "GigabitEthernet3", "GigabitEthernet4"],
            CEDGE.format(name=name, system_ip=f"10.255.0.{n}", site_id=site,
                         wan_ip=f"198.51.100.{n}", lan_ip=f"192.0.2.{4 * n - 2}",
                         lan2_ip=f"172.16.{n}.1", loop_ip=f"10.10.0.{n}"))


def _sdwan_nodes() -> List[NodeSpec]:
    return [
        ("INET", "unmanaged_switch", ["port0", "port1", "port2"], ""),
        _cedge("dc-edge", 100, 1),
        _cedge("br-edge", 200, 2),
        ("br-pc", "desktop", ["eth0"], ""),
    ]


SDWAN_LINKS: List[LinkSpec] = [
    ("dc-edge", "GigabitEthernet1", "INET", "port0"),
    ("br-edge", "GigabitEthernet1", "INET", "port1"),
    ("br-edge", "GigabitEthernet3", "br-pc", "eth0"),
]


class _Run:
    def __init__(self, rc: int, out_dir: pathlib.Path, stderr: str) -> None:
        self.rc = rc
        self.out_dir = out_dir
        self.stderr = stderr

    @property
    def script(self) -> str:
        return (self.out_dir / "ns_commands_cml_overlay.txt").read_text(encoding="utf-8")

    @property
    def model(self) -> Dict:
        return json.loads((self.out_dir / "ns_model_overlay.json").read_text(encoding="utf-8"))

    @property
    def report(self) -> str:
        return (self.out_dir / "overlay_report.md").read_text(encoding="utf-8")


def _convert(doc: Dict, *extra: str, as_json: bool = False) -> _Run:
    tmp = pathlib.Path(tempfile.mkdtemp(prefix="cml_overlay_"))
    src = tmp / ("lab.json" if as_json else "lab.yaml")
    if as_json:
        src.write_text(json.dumps(doc), encoding="utf-8")
    else:
        src.write_text(yaml.safe_dump(doc, sort_keys=False), encoding="utf-8")
    out = tmp / "out"
    err = io.StringIO()
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
        rc = convert_main(["--yaml", str(src), "--out", str(out / "ns_commands.txt"), *extra])
    return _Run(rc, out, err.getvalue())


def _overlay(title: str, nodes: List[NodeSpec], links: List[LinkSpec], *extra: str) -> _Run:
    run = _convert(_lab_yaml(title, nodes, links), "--mode", "overlay", *extra)
    if run.rc != 0:
        raise AssertionError(f"convert failed rc={run.rc}: {run.stderr}")
    return run


def _links(model: Dict) -> List[Tuple[str, str, str, str]]:
    return [(l["a_device"], l["a_port"], l["b_device"], l["b_port"]) for l in model["l1_links"]]


def _linked(model: Dict, a: str, b: str) -> Optional[Tuple[str, str, str, str]]:
    for lk in _links(model):
        if {lk[0], lk[2]} == {a, b}:
            return lk
    return None


def _ips(model: Dict) -> Dict[Tuple[str, str], List[str]]:
    return {(x["device"], x["port"]): x["cidrs"] for x in model["ip_assignments"]}


def _vrfs(model: Dict) -> Dict[Tuple[str, str], str]:
    return {(x["device"], x["port"]): x["vrf"] for x in model["vrf_renames"]}


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class EvpnTests(unittest.TestCase):
    def test_nxos_evpn_gateway_network_and_hosts(self):
        run = _overlay("evpn-lab", _evpn_nodes(), EVPN_LINKS)
        m = run.model
        devices = m["devices"]
        gw, net = "VRF-GW:evpn-lab-BLUE", "NET:evpn-lab-WEB"
        self.assertIn(gw, devices)
        self.assertIn(net, devices)
        for leaf in ("leaf1", "leaf2"):
            self.assertNotIn(leaf, devices)     # the VRF-GW stands for the VTEPs
        # The spine takes no part in the overlay: drawn as in the underlay, unlinked.
        self.assertIn("spine1", devices)
        self.assertIsNone(_linked(m, "spine1", "leaf1"))
        self.assertIn("the EVPN VTEP leaf1 is shown as its VRF-GW", run.report)
        self.assertEqual(devices[gw]["area"], "evpn-lab")
        self.assertEqual(_ips(m)[(gw, "Vlan 10")], ["10.10.10.1/24"])
        self.assertEqual(_vrfs(m)[(gw, "Vlan 10")], "BLUE")
        self.assertIsNotNone(_linked(m, net, gw))
        for host, ip in (("host1", "10.10.10.11/24"), ("host2", "10.10.10.12/24")):
            lk = _linked(m, host, net)
            self.assertIsNotNone(lk, host)
            self.assertEqual(_ips(m)[(host, "Ethernet 0")], [ip])
        self.assertIn("L3VNI 50000", run.script)
        self.assertIn("L2VNI 30010", run.script)
        self.assertIn("['Unknown','Unknown','Unknown']", run.script)

    def test_iosxe_evpn(self):
        run = _overlay("xe", [
            ("xe-leaf1", "cat9000v", ["GigabitEthernet1/0/1", "GigabitEthernet1/0/2"], IOSXE_LEAF),
            ("h1", "alpine", ["eth0"], HOST.format(ip="10.1.101.10/24")),
        ], [("xe-leaf1", "GigabitEthernet1/0/2", "h1", "eth0")])
        m = run.model
        gw, net = "VRF-GW:xe-GREEN", "NET:xe-APP"
        self.assertIn(gw, m["devices"])
        self.assertIn(net, m["devices"])
        self.assertEqual(_ips(m)[(gw, "Vlan 101")], ["10.1.101.1/24"])
        self.assertIsNotNone(_linked(m, "h1", net))
        self.assertIn("L3VNI 50901", run.script)
        self.assertIn("L2VNI 10101", run.script)


class SdwanTests(unittest.TestCase):
    def test_two_sites_vpn_cloud_and_lan_dummy(self):
        run = _overlay("sdwan", _sdwan_nodes(), SDWAN_LINKS)
        m = run.model
        d = m["devices"]
        self.assertEqual(d["dc-edge"]["area"], "sdwan-site100")
        self.assertEqual(d["br-edge"]["area"], "sdwan-site200")
        self.assertEqual(d["VPN:sdwan-10"]["area"], "sdwan-wan_wp_")
        # The transport switch is drawn as in the underlay in the lab area; its
        # links to the Edges would join two areas, so they are reported instead.
        self.assertEqual(d["INET"]["area"], "sdwan")
        self.assertIsNone(_linked(m, "INET", "dc-edge"))
        self.assertIn("INET Ethernet 0 [sdwan] would cross areas", run.report)
        for edge in ("dc-edge", "br-edge"):
            lk = _linked(m, edge, "VPN:sdwan-10")
            self.assertIsNotNone(lk)
            self.assertEqual(_vrfs(m)[(edge, "Vpn 10")], "10")
            self.assertEqual(_vrfs(m)[(edge, "Loopback 10")], "10")
        # br-edge Gi3 reaches its PC; the unlinked LAN ports go to LAN dummies.
        self.assertIsNotNone(_linked(m, "br-edge", "br-pc"))
        self.assertEqual(d["br-pc"]["area"], "sdwan-site200")
        self.assertIsNotNone(_linked(m, "dc-edge", "LAN:dc-edge"))
        self.assertEqual(_ips(m)[("dc-edge", "GigabitEthernet 3")], ["192.0.2.2/30"])
        self.assertIn("WayPoint", run.script)
        self.assertEqual(m["area_layout"][0], ["sdwan-wan_wp_"])


class L3vpnTests(unittest.TestCase):
    def test_xr_srv6_pe_vrf_cloud_and_ce(self):
        nodes: List[NodeSpec] = [
            ("pe1", "iosxrv9000", ["GigabitEthernet0/0/0/0", "GigabitEthernet0/0/0/1"],
             XR_PE.format(name="pe1", loop_ip="10.255.255.1", core_ip="10.0.12.1",
                          vrf_ip="203.0.113.1", n=1)),
            ("pe2", "iosxrv9000", ["GigabitEthernet0/0/0/0", "GigabitEthernet0/0/0/1"],
             XR_PE.format(name="pe2", loop_ip="10.255.255.2", core_ip="10.0.12.2",
                          vrf_ip="203.0.113.5", n=2)),
            ("ce1", "iosv", ["GigabitEthernet0/0"], CE.format(name="ce1", ip="203.0.113.2")),
        ]
        links: List[LinkSpec] = [
            ("pe1", "GigabitEthernet0/0/0/0", "pe2", "GigabitEthernet0/0/0/0"),
            ("pe1", "GigabitEthernet0/0/0/1", "ce1", "GigabitEthernet0/0"),
        ]
        run = _overlay("l3", nodes, links)
        m = run.model
        cloud = "VRF:l3-CUST-A"
        self.assertEqual(m["devices"][cloud]["area"], "l3-wan_wp_")
        for pe in ("pe1", "pe2"):
            self.assertEqual(m["devices"][pe]["area"], "l3")
            self.assertIsNotNone(_linked(m, pe, cloud))
            self.assertEqual(_vrfs(m)[(pe, "Vrf 1")], "CUST-A")
        # The core link is an underlay link between two drawn devices.
        self.assertIsNotNone(_linked(m, "pe1", "pe2"))
        self.assertEqual(_ips(m)[("pe1", "GigabitEthernet 0/0/0/0")], ["10.0.12.1/24"])
        lk = _linked(m, "pe1", "ce1")
        self.assertIsNotNone(lk)
        self.assertEqual(_ips(m)[("ce1", "GigabitEthernet 0/0")], ["203.0.113.2/30"])
        self.assertEqual(_vrfs(m)[("pe1", "GigabitEthernet 0/0/0/1")], "CUST-A")
        self.assertIn("RD 65000:100", run.script)
        self.assertIn("RT import 65000:100", run.script)
        self.assertIn("SRv6", run.script)

    def test_ios_mpls_ldp_pe(self):
        run = _overlay("mpls", [
            ("ios-pe1", "iosv", ["GigabitEthernet0/0", "GigabitEthernet0/1"], IOS_PE),
            ("ce-red", "iosv", ["GigabitEthernet0/0"], CE.format(name="ce-red", ip="198.51.100.10")),
        ], [("ios-pe1", "GigabitEthernet0/1", "ce-red", "GigabitEthernet0/0")])
        m = run.model
        self.assertIn("VRF:mpls-RED", m["devices"])
        self.assertEqual(_vrfs(m)[("ios-pe1", "GigabitEthernet 0/1")], "RED")
        self.assertIsNotNone(_linked(m, "ios-pe1", "ce-red"))
        self.assertIn("LDP", run.script)
        self.assertIn("RD 65010:1", run.script)


class MixedTests(unittest.TestCase):
    def _mixed(self) -> Tuple[List[NodeSpec], List[LinkSpec]]:
        nodes = _evpn_nodes(border=True) + _sdwan_nodes()
        links = EVPN_LINKS + SDWAN_LINKS + [("leaf1", "Ethernet1/3", "dc-edge", "GigabitEthernet3")]
        return nodes, links

    def test_dc_evpn_hands_off_to_cedge(self):
        nodes, links = self._mixed()
        run = _overlay("mix", nodes, links)
        m = run.model
        d = m["devices"]
        gw = "VRF-GW:mix-site100-BLUE"
        self.assertIn(gw, d)
        for name in (gw, "NET:mix-site100-WEB", "host1", "host2", "dc-edge"):
            self.assertEqual(d[name]["area"], "mix-site100", name)
        self.assertEqual(d["br-edge"]["area"], "mix-site200")
        lk = _linked(m, gw, "dc-edge")
        self.assertIsNotNone(lk)
        gw_port = lk[1] if lk[0] == gw else lk[3]
        self.assertEqual(_ips(m)[(gw, gw_port)], ["192.0.2.1/30"])
        self.assertEqual(_vrfs(m)[(gw, gw_port)], "BLUE")
        self.assertEqual(_vrfs(m)[("dc-edge", "GigabitEthernet 3")], "10")
        self.assertTrue(
            "| dc-edge GigabitEthernet 3 | VPN 10 | leaf1 Ethernet 1/3 | VRF BLUE | confirmed |" in run.report
            or "| leaf1 Ethernet 1/3 | VRF BLUE | dc-edge GigabitEthernet 3 | VPN 10 | confirmed |" in run.report,
            run.report)
        self.assertIn("Handoff:", run.script)

    def test_no_cross_area_real_links_and_wp_not_adjacent(self):
        nodes, links = self._mixed()
        m = _overlay("mix", nodes, links).model
        d = m["devices"]
        for a, _ap, b, _bp in _links(m):
            area_a, area_b = d[a]["area"], d[b]["area"]
            if area_a != area_b:
                self.assertTrue(area_a.endswith("_wp_") or area_b.endswith("_wp_"), (a, b))
        for row in m["area_layout"]:
            for left, right in zip(row, row[1:]):
                self.assertFalse(left.endswith("_wp_") and right.endswith("_wp_"), row)
        self.assertEqual(m["area_layout"][0], ["mix-wan_wp_"])

    def test_yaml_and_fetch_json_give_the_same_overlay(self):
        nodes, links = self._mixed()
        a = _convert(_lab_yaml("mix", nodes, links), "--mode", "overlay")
        b = _convert(_lab_fetch_json("mix", nodes, links), "--mode", "overlay", as_json=True)
        self.assertEqual(a.rc, 0, a.stderr)
        self.assertEqual(b.rc, 0, b.stderr)
        self.assertEqual(a.script, b.script)


class LagTests(unittest.TestCase):
    def test_xr_bundle_ether_vrf_port_reaches_a_day0_ce(self):
        run = _overlay("be", [
            ("pe-be", "xrd", ["GigabitEthernet0/0/0/0", "GigabitEthernet0/0/0/1"], XR_PE_BUNDLE),
            ("ce-d0", "iosv", ["GigabitEthernet0/0", "GigabitEthernet0/1"], DAY0),
        ], [("pe-be", "GigabitEthernet0/0/0/1", "ce-d0", "GigabitEthernet0/1")])
        m = run.model
        self.assertIn("ce-d0", m["devices"])
        self.assertEqual(_linked(m, "pe-be", "ce-d0"),
                         ("pe-be", "GigabitEthernet 0/0/0/1", "ce-d0", "GigabitEthernet 0/1"))
        self.assertIn({"device": "pe-be", "physical_ports": ["GigabitEthernet 0/0/0/1"],
                       "portchannel_name": "Bundle-Ether 1"}, m["port_channels"])
        self.assertEqual(_ips(m)[("pe-be", "Bundle-Ether 1")], ["198.51.100.1/30"])
        self.assertEqual(_vrfs(m)[("pe-be", "Bundle-Ether 1")], "CUST")
        self.assertNotIn(("pe-be", "GigabitEthernet 0/0/0/1"), _ips(m))
        self.assertNotIn("has no CML link", run.report)

    def test_ios_port_channel_with_two_members(self):
        run = _overlay("po", [
            ("ios-pe-po", "iosv", ["GigabitEthernet0/1", "GigabitEthernet0/2"], IOS_PE_PO),
            ("sw-ce", "unmanaged_switch", ["port0", "port1", "port2"], ""),
            ("ce-po", "iosv", ["GigabitEthernet0/0"], CE.format(name="ce-po", ip="192.0.2.10")),
        ], [("ios-pe-po", "GigabitEthernet0/1", "sw-ce", "port0"),
            ("ios-pe-po", "GigabitEthernet0/2", "sw-ce", "port1"),
            ("sw-ce", "port2", "ce-po", "GigabitEthernet0/0")])
        m = run.model
        pcs = [pc for pc in m["port_channels"] if pc["device"] == "ios-pe-po"]
        self.assertEqual(pcs, [{"device": "ios-pe-po",
                                "physical_ports": ["GigabitEthernet 0/1", "GigabitEthernet 0/2"],
                                "portchannel_name": "Port-channel 1"}])
        self.assertEqual(_vrfs(m)[("ios-pe-po", "Port-channel 1")], "RED")
        self.assertIsNotNone(_linked(m, "sw-ce", "ce-po"))
        self.assertEqual(_ips(m)[("ce-po", "GigabitEthernet 0/0")], ["192.0.2.10/30"])
        self.assertEqual(_ips(m)[("ios-pe-po", "Port-channel 1")], ["192.0.2.9/30"])


class MixedUnderlayTests(unittest.TestCase):
    """An overlay lab keeps its other devices as underlay; a lab with no overlay is the underlay."""

    def _doc(self) -> Dict:
        l3 = [
            ("pe-be", "xrd", ["GigabitEthernet0/0/0/0", "GigabitEthernet0/0/0/1"], XR_PE_BUNDLE),
            ("p1", "xrd", ["GigabitEthernet0/0/0/0"], P_ROUTER),
            ("ce-d0", "iosv", ["GigabitEthernet0/0", "GigabitEthernet0/1"], DAY0),
            ("ext", "external_connector", ["port"], ""),
        ]
        l3_links = [("pe-be", "GigabitEthernet0/0/0/0", "p1", "GigabitEthernet0/0/0/0"),
                    ("pe-be", "GigabitEthernet0/0/0/1", "ce-d0", "GigabitEthernet0/1"),
                    ("ce-d0", "GigabitEthernet0/0", "ext", "port")]
        plain = [
            ("r1", "iosv", ["GigabitEthernet0/0"], CE.format(name="r1", ip="192.0.2.21")),
            ("r2", "iosv", ["GigabitEthernet0/0"], CE.format(name="r2", ip="192.0.2.22")),
        ]
        plain_links = [("r1", "GigabitEthernet0/0", "r2", "GigabitEthernet0/0")]
        return _labs_fetch_json(("l3lab", l3, l3_links), ("plainlab", plain, plain_links))

    def _run(self) -> _Run:
        run = _convert(self._doc(), "--mode", "both", as_json=True)
        self.assertEqual(run.rc, 0, run.stderr)
        return run

    def test_overlay_lab_draws_its_other_devices_as_underlay(self):
        run = self._run()
        m = run.model
        d = m["devices"]
        self.assertEqual(d["p1"]["area"], "l3lab")
        self.assertIsNotNone(_linked(m, "pe-be", "p1"))
        self.assertEqual(_ips(m)[("pe-be", "GigabitEthernet 0/0/0/0")], ["10.0.13.1/30"])
        self.assertEqual(_ips(m)[("p1", "GigabitEthernet 0/0/0/0")], ["10.0.13.2/30"])
        self.assertEqual(_ips(m)[("p1", "Loopback 0")], ["10.255.255.12/32"])
        # The External Connector keeps its underlay waypoint area and link.
        self.assertEqual(d["ext"]["area"], "l3lab-ext_wp_")
        self.assertIsNotNone(_linked(m, "ce-d0", "ext"))
        self.assertIn("p1 [l3lab]", run.report)

    def test_lab_without_overlay_is_the_underlay(self):
        run = self._run()
        m = run.model
        under = json.loads((run.out_dir / "ns_model.json").read_text(encoding="utf-8"))
        self.assertEqual(m["area_to_devices"]["plainlab"], under["area_to_devices"]["plainlab"])
        for name in ("r1", "r2"):
            self.assertEqual(m["devices"][name]["area"], "plainlab")
        self.assertIsNotNone(_linked(m, "r1", "r2"))
        self.assertEqual(_ips(m)[("r1", "GigabitEthernet 0/0")], ["192.0.2.21/30"])
        self.assertIn("Labs with no overlay, copied from the underlay: plainlab.", run.report)
        # Port info is the underlay's for underlay devices, Unknown for the overlay ones.
        self.assertIn("[['r1','r2'],'_ALL_',['1Gbps','Full','1000BASE-T']]", run.script.replace("'p1',", ""))
        self.assertIn("'pe-be'],'_ALL_',['Unknown','Unknown','Unknown']", run.script)
        # Labs sit side by side as in the underlay, the WAN waypoint row above.
        self.assertEqual(m["area_layout"][0], ["l3lab-wan_wp_"])
        flat = [a for row in m["area_layout"] for a in row]
        self.assertIn("plainlab", flat)
        for row in m["area_layout"]:
            for left, right in zip(row, row[1:]):
                self.assertFalse(left.endswith("_wp_") and right.endswith("_wp_"), row)

    def test_overlay_attributes_keep_their_kind(self):
        run = self._run()
        self.assertIn("['r1', \\\"['DEVICE',[235, 241, 222]]\\\"", run.script)
        self.assertIn("['pe-be', \\\"['DEVICE',[221, 204, 255]]\\\"", run.script)


class ParserTests(unittest.TestCase):
    def test_classic_ip_vrf_forwarding_keeps_vrf_and_ip(self):
        from cml_converter.src.config_parser import parse_running_config
        iface = parse_running_config(IOS_PE, "ios-pe1").interfaces["GigabitEthernet0/1"]
        self.assertEqual(iface.vrf, "RED")
        self.assertEqual(iface.all_cidrs(), ["198.51.100.9/30"])


class ErrorTests(unittest.TestCase):
    def test_no_overlay_exits_3_with_day0_hint(self):
        doc = _lab_yaml("plain", [
            ("r1", "iosv", ["GigabitEthernet0/0"], CE.format(name="r1", ip="192.0.2.1")),
            ("r2", "iosv", ["GigabitEthernet0/0"], "hostname inserthostname-here\n"),
        ], [("r1", "GigabitEthernet0/0", "r2", "GigabitEthernet0/0")])
        run = _convert(doc, "--mode", "overlay")
        self.assertEqual(run.rc, 3)
        self.assertIn("No overlay configuration (VXLAN EVPN / SD-WAN / L3VPN) was found", run.stderr)
        self.assertIn("r2", run.stderr)
        self.assertIn("Extract Configs", run.stderr)
        self.assertFalse((run.out_dir / "ns_commands_cml_overlay.txt").exists())
        self.assertFalse((run.out_dir / "ns_commands.txt").exists())

    def test_both_mode_warns_and_still_writes_underlay(self):
        doc = _lab_yaml("plain", [
            ("r1", "iosv", ["GigabitEthernet0/0"], CE.format(name="r1", ip="192.0.2.1")),
        ], [])
        run = _convert(doc, "--mode", "both")
        self.assertEqual(run.rc, 0)
        self.assertIn("[WARN] No overlay configuration", run.stderr)
        self.assertTrue((run.out_dir / "ns_commands.txt").exists())
        self.assertFalse((run.out_dir / "ns_commands_cml_overlay.txt").exists())

    def test_requested_type_missing_exits_3(self):
        run = _convert(_lab_yaml("sdwan", _sdwan_nodes(), SDWAN_LINKS),
                       "--mode", "overlay", "--overlay", "evpn")
        self.assertEqual(run.rc, 3)
        self.assertIn("Requested overlay type(s) not found", run.stderr)

    def test_overlay_filter_draws_only_the_requested_type(self):
        nodes = _evpn_nodes(border=True) + _sdwan_nodes()
        links = EVPN_LINKS + SDWAN_LINKS + [("leaf1", "Ethernet1/3", "dc-edge", "GigabitEthernet3")]
        m = _overlay("mix", nodes, links, "--overlay", "sdwan").model
        self.assertFalse(any(n.startswith("VRF-GW:") for n in m["devices"]))
        self.assertIn("dc-edge", m["devices"])

    def test_underlay_default_is_unchanged_by_overlay_flags(self):
        doc = _lab_yaml("sdwan", _sdwan_nodes(), SDWAN_LINKS)
        a = _convert(doc)
        b = _convert(doc, "--mode", "both")
        self.assertEqual(a.rc, 0)
        self.assertEqual(b.rc, 0)
        self.assertEqual((a.out_dir / "ns_commands.txt").read_text(encoding="utf-8"),
                         (b.out_dir / "ns_commands.txt").read_text(encoding="utf-8"))
        self.assertNotIn("Unknown','Unknown','Unknown",
                         (a.out_dir / "ns_commands.txt").read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
