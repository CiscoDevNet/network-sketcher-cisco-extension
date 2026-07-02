# Copyright 2026 Cisco Systems, Inc. and its affiliates
# SPDX-License-Identifier: Apache-2.0

"""Build the *logical overlay* NSModel from a Nexus Dashboard / NDFC export.

Maps the NX-OS VXLAN EVPN overlay onto Network Sketcher constructs:

    Fabric                     → NS area (one per fabric)
    VRF (L3VNI)                → a synthesised per-VRF gateway device + l3_instance
    Network (L2VNI)            → an L2 VLAN segment + an NS device (the segment),
                                 with the network's anycast gateway as an SVI on
                                 the VRF gateway
    Network gateway subnet     → SVI + IP on the VRF gateway (distributed anycast GW)
    External / L3 hand-off     → (reserved) gray cloud linked to the VRF gateway

A Network in NDFC is the rough equivalent of an ACI Bridge-Domain *and* EPG
combined (one L2VNI = one VLAN + one subnet/SVI), so each Network yields BOTH a
shared VLAN segment and a device bound to it (mirroring the ACI overlay's
BD-VLAN + EPG-device split). The VRF gateway is a single logical node standing
in for VXLAN's distributed anycast gateway (which actually lives on every leaf
where the VRF is deployed).

Unlike ACI, NX-OS VXLAN EVPN has no per-pair *contracts* — L3 reachability
within a VRF is open by default — so contracts are not emitted as flows unless
the operator opts in (see :mod:`flow_list_builder`).
"""
from __future__ import annotations

import re
from collections import defaultdict
from typing import Any, Dict, List, Optional, Tuple

from . import nd_stencil_mapper as sm
from . import nd_topology as topo
from .nd_stencil_mapper import StencilMapping
from .ns_model import (
    NSDevice, NSIPAssignment, NSL1Link, NSL2Segment, NSModel, NSVirtualPort,
)

# Tier rows inside a fabric area.
_ROW_GATEWAY = 1
_ROW_NETWORK = 3
_ROW_ENDPOINT = 4

OVERLAY_COLOR = (221, 204, 255)  # light purple / lavender — every logical device


def _dummy(n: int) -> str:
    """Pseudo port name for the overlay's *logical* links (no real interface)."""
    return f"Dummy {n}"


def _host_cidr(ip: str) -> str:
    ip = (ip or "").strip()
    if "/" in ip:           # EPL sometimes reports ip/mask already
        return ip
    return f"{ip}/128" if ":" in ip else f"{ip}/32"


def sanitize(name: str) -> str:
    """Keep NS-safe characters (no quotes / brackets). Collapse whitespace.

    ':' is allowed so device names can carry a type prefix (``VRF-GW:...``,
    ``NET:...``) — the same convention as aci_converter, verified against the
    live NS engine to be accepted in device_location / l1_link_bulk /
    l2_segment_bulk."""
    if name is None:
        return ""
    keep = [ch for ch in str(name).strip() if ch.isalnum() or ch in " -_.:+/"]
    return re.sub(r"\s+", " ", "".join(keep)).strip()


def build_logical_model(
    idx,
    cfg: Optional[Dict[str, Any]] = None,
    layout: str = "tier",
) -> Tuple[NSModel, Dict[str, str], Dict[str, Any]]:
    """Return (NSModel, network_name_by_key, info).

    ``network_name_by_key`` maps a ``(fabric, networkName)`` tuple to its NS
    device name, consumed by :mod:`flow_list_builder` so flow rows use the same
    device names as the topology.
    """
    cfg = cfg or {}
    fabric_include = {str(x) for x in (cfg.get("fabric_include") or [])}
    vlan_base = int(cfg.get("vlan_base", 101) or 101)
    include_endpoints = bool(cfg.get("include_endpoints", True))
    max_ep = int(cfg.get("max_endpoints_per_network", 50) or 50)

    model = NSModel()
    mappings: List[StencilMapping] = []
    net_name_by_key: Dict[Tuple[str, str], str] = {}
    seen_names: set = set()
    counts = {"fabric": 0, "vrf": 0, "network": 0, "subnet": 0, "endpoint": 0,
              "l2_only": 0, "external": 0, "host": 0}
    caveats: List[str] = []

    def _unique(name: str) -> str:
        name = sanitize(name) or "node"
        base, i = name, 2
        while name in seen_names:
            name = f"{base}_{i}"
            i += 1
        seen_names.add(name)
        return name

    fabrics = [f for f in idx.fabric_names() if not fabric_include or f in fabric_include]

    gateways: Dict[Tuple[str, str], str] = {}     # (fabric, vrf) -> gw device name
    gw_port: Dict[str, int] = {}
    t_gateways: Dict[str, List[str]] = defaultdict(list)
    t_net_cols: Dict[str, List[Tuple[str, List[str]]]] = defaultdict(list)
    vlan_counter = vlan_base
    used_vids: set = set()

    def _gateway(fabric: str, vrf: str) -> str:
        key = (fabric, vrf)
        if key in gateways:
            return gateways[key]
        gw_name = _unique(f"VRF-GW:{fabric}-{vrf}")
        st = sm.map_logical(gw_name, "gateway")
        mappings.append(st)
        model.devices[gw_name] = NSDevice(
            name=gw_name, area=fabric, row=_ROW_GATEWAY, stencil=st, is_endpoint=False,
            default_color=OVERLAY_COLOR,
        )
        gateways[key] = gw_name
        t_gateways[fabric].append(gw_name)
        counts["vrf"] += 1
        return gw_name

    for fabric in fabrics:
        nets = idx.networks.get(fabric, [])
        vrfs = idx.vrfs.get(fabric, [])
        if not nets and not vrfs:
            continue
        counts["fabric"] += 1
        # Real hosts from Endpoint Locator, grouped by their network (when present).
        eps_by_net = (topo.endpoints_by_network(idx.endpoints.get(fabric, []), nets)
                      if include_endpoints else {})

        # Pre-create gateways for every defined VRF (so empty VRFs still show).
        for vrf in vrfs:
            info = topo.vrf_l3_info(vrf)
            if info["name"]:
                gw = _gateway(fabric, info["name"])
                if info["vni"]:
                    model.devices[gw].routing_attribute = f"L3VNI {info['vni']}"

        # Networks → VLAN segment + SVI on VRF gateway + a network device.
        for net in nets:
            ninfo = topo.network_l2_info(net)
            nname = ninfo["name"]
            if not nname:
                continue
            counts["network"] += 1
            vrf = ninfo["vrf"]
            vid = ninfo["vlan_id"]
            if vid is None:
                while vlan_counter in used_vids:
                    vlan_counter += 1
                vid = vlan_counter
                used_vids.add(vid)
                vlan_counter += 1
            else:
                used_vids.add(vid)
            svi = f"Vlan {vid}"

            # The network device (the L2VNI segment / EPG-equivalent).
            dev = _unique(f"NET:{nname}")
            net_name_by_key[(fabric, nname)] = dev
            st = sm.map_logical(dev, "network", model=f"Network {ninfo['display']} (L2VNI {ninfo['vni']})")
            mappings.append(st)
            attr = f"L2VNI {ninfo['vni']}" if ninfo["vni"] else ""
            if vrf:
                attr += (" | " if attr else "") + f"VRF {vrf}"
            if ninfo["gw_v4"]:
                attr += (" | " if attr else "") + f"GW {ninfo['gw_v4']}"
            model.devices[dev] = NSDevice(
                name=dev, area=fabric, row=_ROW_NETWORK, stencil=st, is_endpoint=True,
                default_color=OVERLAY_COLOR, routing_attribute=attr,
            )

            if vrf:
                gw = _gateway(fabric, vrf)
                # Anycast gateway SVI on the VRF gateway.
                gw_subnets = [s for s in (ninfo["gw_v4"], ninfo["gw_v6"]) if s]
                if gw_subnets:
                    model.virtual_ports.append(NSVirtualPort(device=gw, port=svi, vlan_id=vid))
                    model.ip_assignments.append(NSIPAssignment(device=gw, port=svi, cidrs=gw_subnets))
                    model.l2_segments_svi.append(NSL2Segment(device=gw, port=svi, vlans=[f"Vlan{vid}"]))
                    model.vrf_renames.append((gw, svi, vrf))
                    counts["subnet"] += len(gw_subnets)
                # Network <-> gateway logical link, both sides carry the BD VLAN.
                gw_port[gw] = gw_port.get(gw, 0) + 1
                gport = _dummy(gw_port[gw])
                eport = _dummy(0)
                model.l1_links.append(NSL1Link(dev, eport, gw, gport))
                model.l2_segments_phys.append(NSL2Segment(device=dev, port=eport, vlans=[f"Vlan{vid}"]))
                model.l2_segments_phys.append(NSL2Segment(device=gw, port=gport, vlans=[f"Vlan{vid}"]))
            else:
                counts["l2_only"] += 1

            # Real endpoints (hosts) beneath the network, from Endpoint Locator.
            # The host side is an L3 port carrying its IP; the network (switch)
            # side carries the BD VLAN — same modelling as the ACI overlay.
            ep_devs: List[str] = []
            eps = eps_by_net.get(nname, [])
            for seq, ep in enumerate(eps[:max_ep], start=1):
                host = _unique(f"EP_{nname}_{seq}")
                st_h = sm.map_logical(host, "endpoint",
                                      model=f"Endpoint {ep.mac or ep.ip or seq}")
                mappings.append(st_h)
                hattr_parts = []
                if ep.mac:
                    hattr_parts.append(f"MAC {ep.mac}")
                if ep.ip:
                    hattr_parts.append(f"IP {ep.ip}")
                if ep.switch:
                    hattr_parts.append(f"on {ep.switch}" + (f":{ep.port}" if ep.port else ""))
                model.devices[host] = NSDevice(
                    name=host, area=fabric, row=_ROW_ENDPOINT, stencil=st_h,
                    is_endpoint=True, default_color=OVERLAY_COLOR,
                    routing_attribute=" | ".join(hattr_parts),
                )
                hport = _dummy(seq)  # network-side port for this host (Dummy 0 = gateway uplink)
                model.l1_links.append(NSL1Link(host, _dummy(0), dev, hport))
                model.l2_segments_phys.append(NSL2Segment(device=dev, port=hport, vlans=[f"Vlan{vid}"]))
                if ep.ip:
                    model.ip_assignments.append(
                        NSIPAssignment(device=host, port=_dummy(0), cidrs=[_host_cidr(ep.ip)]))
                ep_devs.append(host)
                counts["host"] += 1
            if len(eps) > max_ep:
                caveats.append(f"Network {nname}: {len(eps)} endpoints; showing first {max_ep}.")

            t_net_cols[fabric].append((dev, ep_devs))

    if counts["fabric"] == 0:
        caveats.append(
            "No VRFs or Networks found in the export. The selected fabric(s) have "
            "no VXLAN EVPN overlay configured (top-down vrfs / networks were empty), "
            "so the overlay diagram is empty. Configure tenants/VRFs/Networks in "
            "NDFC (or select a fabric that has them) and re-fetch."
        )

    caveats.append(
        "MODEL — each NDFC Network (L2VNI) maps to ONE shared L2 segment "
        "(Vlan <id>) plus an NS device bound to it; its anycast gateway subnet "
        "becomes an SVI on the VRF gateway. This mirrors ACI's BD-VLAN + EPG split."
    )
    caveats.append(
        "INFERRED — VRF gateway devices ('VRF-GW:<fabric>-<vrf>') are synthesised: "
        "one per VRF, standing in for VXLAN's distributed anycast gateway (which "
        "actually lives on every leaf where the VRF is deployed)."
    )
    caveats.append(
        "INFERRED — all L1 links in the overlay (network<->gateway) are synthetic; "
        "they represent VRF/VLAN membership, NOT real cabling. Their interface "
        "names use the pseudo label 'Dummy N'."
    )
    caveats.append(
        "INFERRED — VLAN ids for any Network with no vlanId in its template config "
        f"are synthesised sequentially from vlan_base ({vlan_base})."
    )
    caveats.append(
        "NOTE — NX-OS VXLAN EVPN provides open any-to-any L3 reachability within a "
        "VRF (no ACI-style contracts), so no per-pair flow policy is drawn unless "
        "emit_intra_vrf_flows is enabled in the config."
    )
    if counts["host"]:
        caveats.append(
            f"OBSERVED — {counts['host']} real endpoint host(s) from Endpoint "
            "Locator are drawn beneath their Network (host side = L3 port with the "
            "real IP, network side = BD VLAN). Requires EPL enabled on the fabric."
        )
    else:
        caveats.append(
            "NOTE — no Endpoint Locator hosts were present in the export. Enable EPL "
            "on the fabric (and re-fetch with endpoints) to draw real hosts under "
            "each Network; otherwise only the Network segments are shown."
        )

    # Placement: gateways row on top, network devices below (one column each).
    ordered = sorted({tn for tn in list(t_net_cols) + list(t_gateways)})
    model.areas = [ordered] if ordered else []
    model.area_to_devices = {
        tn: _overlay_grid(t_gateways.get(tn, []), t_net_cols.get(tn, []))
        for tn in ordered
    }

    info = {"mappings": mappings, "counts": counts, "caveats": caveats}
    return model, net_name_by_key, info


def _overlay_grid(
    gateways: List[str],
    net_cols: List[Tuple[str, List[str]]],
) -> List[List[str]]:
    """Lay a fabric out: VRF gateways on top, networks below, endpoints stacked."""
    ncols = max(len(net_cols), len(gateways), 1)

    def _row(items: List[str]) -> List[str]:
        return list(items) + ["_AIR_"] * (ncols - len(items))

    grid: List[List[str]] = []
    if gateways:
        grid.append(_row(gateways))
    if net_cols:
        grid.append(_row([n for n, _ in net_cols]))
        depth = max((len(eps) for _, eps in net_cols), default=0)
        for i in range(depth):
            grid.append(_row([
                eps[i] if i < len(eps) else "_AIR_" for _, eps in net_cols
            ]))
    return grid
