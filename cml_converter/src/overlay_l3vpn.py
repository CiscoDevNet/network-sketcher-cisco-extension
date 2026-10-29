# Copyright 2026 Cisco Systems, Inc. and its affiliates
# SPDX-License-Identifier: Apache-2.0
"""L3VPN part of the integrated overlay (MPLS LDP / SR-MPLS / SRv6).

Each VRF of a lab is one purple waypoint cloud ``VRF:<lab>-<name>`` in
``<lab>-wan_wp_`` whose Attribute names the RD / route targets. Every PE sits
at the top of its area and links UP to each of its VRF clouds from a
synthetic ``Vrf <n>`` port carrying that VRF (no IP). Its real VRF ports face
DOWN to the CEs and are left to the stitch step; VRF loopbacks are drawn as
unlinked virtual ports with their IP and VRF.
"""

from __future__ import annotations

from typing import Dict, List, Set, Tuple

from .overlay_canvas import (
    CLOUD_SVI_PORT, COLOR_OVERLAY, ROW_EDGE, Attachment, OverlayCanvas, logical_stencil,
)
from .overlay_parser import OverlayFacts
from .overlay_sites import wan_area
from .stencil_mapper import NS_CLOUD, StencilMapping


def build_l3vpn(
    canvas: OverlayCanvas,
    facts: Dict[str, OverlayFacts],
    areas: Dict[str, str],
    lab_of: Dict[str, str],
    stencils: Dict[str, StencilMapping],
    taken: Set[Tuple[str, str]],
) -> List[Attachment]:
    """``taken`` holds ports another overlay already attached (EVPN border PE)."""
    pes = sorted(label for label, f in facts.items() if f.l3vpn is not None)
    attachments: List[Attachment] = []

    clouds: Dict[Tuple[str, str], str] = {}
    cloud_pes: Dict[str, List[str]] = {}
    for pe in pes:
        lab = lab_of.get(pe, "lab")
        for vrf in facts[pe].l3vpn.vrfs:
            key = (lab, vrf)
            if key not in clouds:
                name = canvas.unique_name(f"VRF:{lab}-{vrf}")
                canvas.add_device(
                    name, wan_area(lab), lab, 0,
                    logical_stencil(name, "l3vpn-vrf", NS_CLOUD, "L3VPN VRF (MP-BGP)"),
                    color=COLOR_OVERLAY,
                )
                canvas.virtual_port(name, CLOUD_SVI_PORT)
                canvas.l2_svi(name, CLOUD_SVI_PORT, f"VRF {vrf}")
                clouds[key] = name
                cloud_pes[name] = []
            cloud_pes[clouds[key]].append(pe)

    # Cloud Attribute: RD / RT as seen on the PEs.
    for (lab, vrf), name in clouds.items():
        rds: List[str] = []
        imp: List[str] = []
        exp: List[str] = []
        for pe in cloud_pes[name]:
            info = facts[pe].l3vpn.vrfs.get(vrf, {})
            for src, dst in ((info.get("rd", []), rds), (info.get("import", []), imp),
                             (info.get("export", []), exp)):
                dst.extend(x for x in src if x not in dst)
        canvas.model.devices[name].routing_attribute = " | ".join(x for x in (
            f"L3VPN VRF {vrf}",
            "RD " + ",".join(rds) if rds else "",
            "RT import " + ",".join(imp) if imp else "",
            "RT export " + ",".join(exp) if exp else "",
            "PE " + ",".join(cloud_pes[name]),
        ) if x)

    for pe in pes:
        f = facts[pe]
        lab = lab_of.get(pe, "lab")
        vrfs = sorted(f.l3vpn.vrfs)
        attr = " | ".join(x for x in (
            "L3VPN PE",
            ",".join(f.l3vpn.families),
            ",".join(f.l3vpn.transport),
            "VRF " + ",".join(vrfs),
        ) if x)
        canvas.add_device(pe, areas[pe], lab, ROW_EDGE, stencils[pe],
                          color=COLOR_OVERLAY, attribute=attr)
        canvas.counts["l3vpn_pes"] += 1
        for index, vrf in enumerate(vrfs, start=1):
            cloud = clouds[(lab, vrf)]
            cport = canvas.dummy(cloud)
            port = f"Vrf {index}"
            if canvas.link(pe, port, cloud, cport):
                canvas.l2(cloud, cport, f"VRF {vrf}")
                canvas.vrf(pe, port, vrf)
        for port, ctx in sorted(f.ports.items()):
            if not ctx.vrf or (pe, port) in taken:
                continue
            if ctx.kind == "loopback":
                canvas.virtual_port(pe, port, is_loopback=True)
                canvas.ip(pe, port, ctx.cidrs)
                canvas.vrf(pe, port, ctx.vrf)
                continue
            if ctx.kind in ("svi", "tunnel", "mgmt"):
                continue
            attachments.append(Attachment(
                device=pe, port=port, overlay="l3vpn", context=f"VRF {ctx.vrf}",
                vrf=ctx.vrf, cidrs=ctx.cidrs, draw_device=pe, draw_port=port,
                members=f.members.get(port, []),
            ))
    return attachments
