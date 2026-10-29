# Copyright 2026 Cisco Systems, Inc. and its affiliates
# SPDX-License-Identifier: Apache-2.0
"""VXLAN EVPN part of the integrated overlay (same drawing as nd_converter).

The leaves themselves are not drawn. Per area and VRF there is one purple
``VRF-GW:<area>-<vrf>`` (the distributed anycast gateway, row 1) carrying an
SVI ``Vlan N`` with the gateway IP for every L2VNI of that VRF. Each L2VNI is
one ``NET:<area>-<name>`` (row 2) linked to its gateway on ``Dummy 0`` /
``Dummy k`` with the VLAN as L2 segment on both ends. A host on a leaf
access port in that VLAN links to the network (row 3). A border leaf's routed
VRF port is handed to the stitch step as an attachment of the VRF-GW.
"""

from __future__ import annotations

from collections import defaultdict
from typing import Dict, List, Optional, Set, Tuple

from .overlay_canvas import (
    COLOR_OVERLAY, ROW_GATEWAY, ROW_HOST, ROW_NETWORK, Attachment, OverlayCanvas,
    logical_stencil,
)
from .overlay_parser import OverlayFacts
from .stencil_mapper import NS_L3SWITCH, NS_PC, StencilMapping
from .topology_mapper import NSL1Link

_OS_NAME = {"nxos": "NX-OS", "iosxe": "IOS-XE", "ios": "IOS", "iosxr": "IOS-XR"}


def _segment(vlan: int) -> str:
    return f"Vlan{vlan}"


def _safe(text: str) -> str:
    return "".join(ch if ch.isalnum() or ch in "-_.+" else "_" for ch in text)


def build_evpn(
    canvas: OverlayCanvas,
    facts: Dict[str, OverlayFacts],
    areas: Dict[str, str],
    lab_of: Dict[str, str],
    stencils: Dict[str, StencilMapping],
    l1_links: List[NSL1Link],
) -> List[Attachment]:
    leaves = sorted(label for label, f in facts.items() if f.evpn is not None)
    by_area: Dict[str, List[str]] = defaultdict(list)
    for leaf in leaves:
        by_area[areas[leaf]].append(leaf)

    peers: Dict[Tuple[str, str], List[Tuple[str, str]]] = defaultdict(list)
    for lk in l1_links:
        peers[(lk.a_device, lk.a_port)].append((lk.b_device, lk.b_port))
        peers[(lk.b_device, lk.b_port)].append((lk.a_device, lk.a_port))

    attachments: List[Attachment] = []
    for area, members in by_area.items():
        lab = lab_of.get(members[0], "lab")
        os_name = _OS_NAME.get(facts[members[0]].os_family, "")

        # vlan -> network facts merged across the leaves of this area
        nets: Dict[int, Dict[str, object]] = {}
        vrf_l3vni: Dict[str, int] = {}
        vrf_leaves: Dict[str, Set[str]] = defaultdict(set)
        for leaf in members:
            ev = facts[leaf].evpn
            for vrf, vni in ev.l3vni.items():
                vrf_l3vni.setdefault(vrf, vni)
                vrf_leaves[vrf].add(leaf)
            for vlan, vni in ev.l2vni.items():
                net = nets.setdefault(vlan, {"vni": vni, "name": "", "vrf": None,
                                             "gw": [], "leaves": set()})
                net["leaves"].add(leaf)
                if ev.vlan_names.get(vlan) and not net["name"]:
                    net["name"] = ev.vlan_names[vlan]
                svi = facts[leaf].ports.get(f"Vlan {vlan}")
                if svi is not None:
                    if svi.vrf and not net["vrf"]:
                        net["vrf"] = svi.vrf
                    if svi.cidrs and (not net["gw"] or vlan in ev.anycast_vlans):
                        net["gw"] = list(svi.cidrs)
            for port, ctx in facts[leaf].ports.items():
                if ctx.vrf and ctx.kind in ("physical", "subif", "portchannel") and ctx.cidrs:
                    vrf_leaves[ctx.vrf].add(leaf)

        gateways: Dict[str, str] = {}

        def gateway(vrf: str) -> str:
            if vrf in gateways:
                return gateways[vrf]
            name = canvas.unique_name(f"VRF-GW:{area}-{vrf}")
            attr = " | ".join(x for x in (
                "VXLAN EVPN",
                f"VRF {vrf}",
                f"L3VNI {vrf_l3vni[vrf]}" if vrf in vrf_l3vni else "",
                "VTEP " + ",".join(sorted(vrf_leaves.get(vrf, ()))),
            ) if x)
            canvas.add_device(
                name, area, lab, ROW_GATEWAY,
                logical_stencil(name, "gateway", NS_L3SWITCH,
                                "VXLAN Distributed Anycast Gateway (VRF)", os_name),
                color=COLOR_OVERLAY, attribute=attr,
            )
            gateways[vrf] = name
            canvas.counts["evpn_gateways"] += 1
            return name

        for vrf in sorted(set(vrf_l3vni) | {str(n["vrf"]) for n in nets.values() if n["vrf"]}):
            gateway(vrf)

        networks: Dict[int, str] = {}
        for vlan in sorted(nets):
            net = nets[vlan]
            label = str(net["name"]) or f"VNI{net['vni']}"
            name = canvas.unique_name(f"NET:{area}-{_safe(label)}")
            vrf: Optional[str] = net["vrf"]  # type: ignore[assignment]
            attr = " | ".join(x for x in (
                f"L2VNI {net['vni']}",
                f"VLAN {vlan}",
                f"VRF {vrf}" if vrf else "L2 only",
                "GW " + ",".join(net["gw"]) if net["gw"] else "",
            ) if x)
            canvas.add_device(
                name, area, lab, ROW_NETWORK,
                logical_stencil(name, "network", NS_PC, "VXLAN Network (L2VNI)"),
                color=COLOR_OVERLAY, attribute=attr,
            )
            networks[vlan] = name
            canvas.counts["evpn_networks"] += 1
            if vrf:
                gw = gateway(vrf)
                svi = f"Vlan {vlan}"
                canvas.virtual_port(gw, svi, vlan_id=vlan)
                canvas.l2_svi(gw, svi, _segment(vlan))
                canvas.ip(gw, svi, net["gw"])  # type: ignore[arg-type]
                canvas.vrf(gw, svi, vrf)
                gport = canvas.dummy(gw)
                if canvas.link(name, "Dummy 0", gw, gport):
                    canvas.l2(name, "Dummy 0", _segment(vlan))
                    canvas.l2(gw, gport, _segment(vlan))

        # Hosts on leaf access ports in an L2VNI VLAN.
        for leaf in members:
            for port, ctx in sorted(facts[leaf].ports.items()):
                if ctx.access_vlan not in networks:
                    continue
                for host, hport in peers.get((leaf, port), []):
                    if host in facts and facts[host].types():
                        continue
                    st = stencils.get(host)
                    if st is None:
                        continue
                    net = networks[ctx.access_vlan]
                    host_area = canvas.area_of(host) or areas.get(host, area)
                    if host_area != area:
                        canvas.notes.append(
                            f"Host {host} {hport} on {leaf} {port} (VLAN {ctx.access_vlan}) "
                            f"is in area {host_area}, not {area}, and is not linked.")
                        continue
                    canvas.add_device(host, area, lab, ROW_HOST, st, is_endpoint=True)
                    nport = canvas.dummy(net)
                    if canvas.link(host, hport, net, nport):
                        canvas.l2(net, nport, _segment(ctx.access_vlan))
                        host_ctx = facts[host].ports.get(hport) if host in facts else None
                        if host_ctx is not None:
                            canvas.ip(host, hport, host_ctx.cidrs)
                        canvas.counts["evpn_hosts"] += 1

        # Border leaf routed VRF ports become attachments of the VRF-GW.
        for leaf in members:
            for port, ctx in sorted(facts[leaf].ports.items()):
                if not ctx.vrf or ctx.kind not in ("physical", "subif", "portchannel") or not ctx.cidrs:
                    continue
                attachments.append(Attachment(
                    device=leaf, port=port, overlay="evpn", context=f"VRF {ctx.vrf}",
                    vrf=ctx.vrf, cidrs=ctx.cidrs, draw_device=gateway(ctx.vrf),
                ))
    return attachments
