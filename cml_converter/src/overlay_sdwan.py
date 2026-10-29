# Copyright 2026 Cisco Systems, Inc. and its affiliates
# SPDX-License-Identifier: Apache-2.0
"""SD-WAN part of the integrated overlay (same drawing as sdwan_converter).

Each Service VPN of a lab is one purple waypoint cloud ``VPN:<lab>-<id>`` in
``<lab>-wan_wp_``. Every cEdge sits at the top of its site area and links UP
to each VPN cloud from a synthetic ``Vpn <id>`` port that carries the VRF
and no IP. Its real Service VPN ports face DOWN to the LAN side and are left
to the stitch step; its Service VPN loopbacks are drawn as unlinked virtual
ports with their IP and VRF.
"""

from __future__ import annotations

from typing import Dict, List

from .overlay_canvas import (
    CLOUD_SVI_PORT, COLOR_OVERLAY, ROW_EDGE, Attachment, OverlayCanvas, logical_stencil,
)
from .overlay_parser import OverlayFacts
from .overlay_sites import wan_area
from .stencil_mapper import NS_CLOUD, StencilMapping


def _vpn_port(vpn_id: int) -> str:
    return f"Vpn {vpn_id}"


def build_sdwan(
    canvas: OverlayCanvas,
    facts: Dict[str, OverlayFacts],
    areas: Dict[str, str],
    lab_of: Dict[str, str],
    stencils: Dict[str, StencilMapping],
) -> List[Attachment]:
    attachments: List[Attachment] = []
    edges = [label for label, f in facts.items() if f.sdwan is not None]
    edges.sort(key=lambda l: (facts[l].sdwan.site_id if facts[l].sdwan.site_id is not None else 1 << 30, l))

    clouds: Dict[tuple, str] = {}
    for label in edges:
        sd = facts[label].sdwan
        lab = lab_of.get(label, "lab")
        vpn_ids = sorted(set(sd.vpns) | {
            int(p.vrf) for p in facts[label].ports.values()
            if p.vrf and p.vrf.isdigit() and int(p.vrf) not in (0, 512)
        })
        for vpn_id in vpn_ids:
            key = (lab, vpn_id)
            if key in clouds:
                continue
            name = canvas.unique_name(f"VPN:{lab}-{vpn_id}")
            canvas.add_device(
                name, wan_area(lab), lab, 0,
                logical_stencil(name, "sdwan-vpn", NS_CLOUD, "SD-WAN Service VPN (OMP)"),
                color=COLOR_OVERLAY,
            )
            canvas.virtual_port(name, CLOUD_SVI_PORT)
            canvas.l2_svi(name, CLOUD_SVI_PORT, f"VPN {vpn_id}")
            clouds[key] = name
        # Clouds in VPN-id order regardless of which Edge introduced them.
        canvas.rows[wan_area(lab)][0].sort(key=_vpn_sort_key)

    for label in edges:
        f = facts[label]
        sd = f.sdwan
        lab = lab_of.get(label, "lab")
        vpn_ids = sorted(k[1] for k in clouds if k[0] == lab and (
            k[1] in sd.vpns or any(p.vrf == str(k[1]) for p in f.ports.values())))
        colours = sorted({c for c in sd.transport_ports.values() if c})
        attr = " | ".join(x for x in (
            "SD-WAN",
            f"system-ip {sd.system_ip}" if sd.system_ip else "",
            f"site-id {sd.site_id}" if sd.site_id is not None else "",
            "VPN " + ",".join(str(v) for v in vpn_ids) if vpn_ids else "",
            "TLOC " + ",".join(colours) if colours else "",
        ) if x)
        canvas.add_device(label, areas[label], lab, ROW_EDGE, stencils[label],
                          color=COLOR_OVERLAY, attribute=attr)
        canvas.counts["sdwan_edges"] += 1
        for vpn_id in vpn_ids:
            cloud = clouds[(lab, vpn_id)]
            cport = canvas.dummy(cloud)
            if canvas.link(label, _vpn_port(vpn_id), cloud, cport):
                canvas.l2(cloud, cport, f"VPN {vpn_id}")
                canvas.vrf(label, _vpn_port(vpn_id), sd.vpns.get(vpn_id, str(vpn_id)))
        for port, ctx in sorted(f.ports.items()):
            if not ctx.vrf or port in sd.transport_ports:
                continue
            if ctx.kind == "loopback":
                canvas.virtual_port(label, port, is_loopback=True)
                canvas.ip(label, port, ctx.cidrs)
                canvas.vrf(label, port, ctx.vrf)
                continue
            if ctx.kind in ("svi", "tunnel", "mgmt"):
                continue
            attachments.append(Attachment(
                device=label, port=port, overlay="sdwan", context=f"VPN {ctx.vrf}",
                vrf=ctx.vrf, cidrs=ctx.cidrs, draw_device=label, draw_port=port,
                lan_dummy=True, members=f.members.get(port, []),
            ))
    return attachments


def _vpn_sort_key(name: str) -> tuple:
    tail = name.rsplit("-", 1)[-1]
    return (0, int(tail), name) if tail.isdigit() else (1, 0, name)
