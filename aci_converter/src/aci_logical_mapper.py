# Copyright 2026 Cisco Systems, Inc. and its affiliates
# SPDX-License-Identifier: Apache-2.0

"""Build the *logical overlay* NSModel from an APIC config export.

Maps the ACI policy hierarchy onto Network Sketcher constructs:

    Tenant (fvTenant)          → NS area (one per tenant)
    VRF (fvCtx)                → a synthesised per-VRF gateway device + l3_instance
    Bridge Domain (fvBD)       → an L2 VLAN segment
    BD subnet (fvSubnet)       → SVI + IP on the VRF gateway (distributed anycast GW)
    EPG (fvAEPg)               → an NS device, attached to its BD's VLAN
    App Profile (fvAp)         → row grouping of its EPGs
    L3Out (l3extOut)           → a gray cloud (external routed network)

Contracts (vzBrCP) are NOT topology — they become the ``[Flow_List]`` sheet,
built separately by :mod:`flow_list_builder` using the EPG-DN→name map this
module returns.

The VRF gateway is a single logical node standing in for ACI's distributed
anycast gateway (which actually lives on every deploying leaf). That is an
honest abstraction for a logical diagram; the physical mode shows the real
leafs.
"""
from __future__ import annotations

import re
from collections import defaultdict
from typing import Any, Dict, List, Optional, Tuple

from . import aci_stencil_mapper as sm
from . import aci_topology as topo
from .aci_stencil_mapper import StencilMapping
from .ns_model import (
    NSDevice, NSIPAssignment, NSL1Link, NSL2Segment, NSModel,
    NSVirtualPort,
)

_TENANT_RE = re.compile(r"/tn-([^/]+)")
_VLAN_ENCAP_RE = re.compile(r"vlan-(\d+)", re.IGNORECASE)

# Tier rows inside a tenant area.
_ROW_L3OUT = 0
_ROW_GATEWAY = 1
_ROW_EPG = 3
_ROW_ENDPOINT = 4

# Pseudo port name for the overlay's *logical* links. ACI has no physical
# interface for EPG<->BD/EPG<->contract relationships, so 'Dummy N' makes clear
# these are synthetic (not real fabric ports). 'Dummy' is not a Cisco interface
# type, so it is never mistaken for a physical port; NS accepts it as a label.
def _dummy(n: int) -> str:
    return f"Dummy {n}"


def _host_cidr(ip: str) -> str:
    """A host endpoint address as a host CIDR (/32 IPv4, /128 IPv6).

    fvCEp/fvIp report bare host IPs with no prefix; /32 (or /128) marks them as
    host addresses without implying a (possibly wrong) subnet mask."""
    return f"{ip}/128" if ":" in ip else f"{ip}/32"

# Every device in the overlay is a *logical* construct (EPG / VRF gateway /
# L3Out), not real gear — so they are all coloured light purple to distinguish
# the overlay view from the underlay's role-coloured physical fabric.
OVERLAY_COLOR = (221, 204, 255)  # light purple / lavender

# Server vs client classification for an EPG's endpoints. Contract direction is
# the primary signal (an EPG that PROVIDES a contract offers a service = server;
# one that only CONSUMES = client); EPG-name keywords are the fallback. This
# drives the sna-style device naming: servers stay individual (SRV_...), clients
# in the same EPG/segment collapse into one PC_...{n} device.
_SERVER_KW = ("srv", "server", "web", "db", "sql", "app", "http", "dns",
              "mail", "api", "svc", "nfs", "ftp", "ldap", "vm")
_CLIENT_KW = ("client", "pc", "user", "desktop", "vdi", "workstation",
              "wkstn", "laptop")


def _epg_role(provides: list, consumes: list, name: str) -> str:
    """'server' or 'client' for an EPG (contract direction first, name fallback)."""
    if provides:
        return "server"
    if consumes:
        return "client"
    n = (name or "").lower()
    if any(k in n for k in _CLIENT_KW):
        return "client"
    if any(k in n for k in _SERVER_KW):
        return "server"
    return "server"  # unknown: keep individual so no endpoint detail is lost


def _tenant_of(dn: str) -> str:
    m = _TENANT_RE.search(dn or "")
    return m.group(1) if m else ""


def sanitize(name: str) -> str:
    """Keep NS-safe characters (no quotes / brackets). Collapse whitespace."""
    if name is None:
        return ""
    keep = [ch for ch in str(name).strip() if ch.isalnum() or ch in " -_.+/"]
    return re.sub(r"\s+", " ", "".join(keep)).strip()


def build_logical_model(
    idx,
    cfg: Optional[Dict[str, Any]] = None,
    layout: str = "tier",
) -> Tuple[NSModel, Dict[str, str], Dict[str, Any]]:
    """Return (NSModel, epg_name_by_dn, info).

    ``epg_name_by_dn`` is consumed by :mod:`flow_list_builder` so flow rows use
    the same device names as the topology.
    """
    cfg = cfg or {}
    include = {str(x) for x in (cfg.get("tenant_include") or [])}
    exclude = {str(x) for x in (cfg.get("tenant_exclude") or ["mgmt", "infra", "common"])}
    vlan_base = int(cfg.get("vlan_base", 101) or 101)
    max_ep = int(cfg.get("max_endpoints_per_epg", 50) or 50)
    eps_by_epg = topo.endpoints_by_epg(idx)  # B: real endpoints per EPG

    model = NSModel()
    mappings: List[StencilMapping] = []
    epg_name_by_dn: Dict[str, str] = {}
    seen_names: set = set()

    def _unique(name: str) -> str:
        name = sanitize(name) or "node"
        base, i = name, 2
        while name in seen_names:
            name = f"{base}_{i}"
            i += 1
        seen_names.add(name)
        return name

    # ----- choose tenants -------------------------------------------------
    tenants = []
    for t in idx.of("fvTenant"):
        tname = t.get("name")
        if not tname:
            continue
        if include and tname not in include:
            continue
        if not include and tname in exclude:
            continue
        tenants.append(t)

    # ----- pre-index BDs & VRFs per tenant + assign BD VLANs --------------
    bd_by_key: Dict[Tuple[str, str], Any] = {}      # (tenant, bdname) -> fvBD mo
    bd_vlan: Dict[str, int] = {}                    # bd dn -> vlan id
    bd_vrf: Dict[str, str] = {}                     # bd dn -> vrf name
    vlan_counter = vlan_base

    for bd in idx.of("fvBD"):
        tenant = _tenant_of(bd.dn)
        bd_by_key[(tenant, bd.get("name"))] = bd
        # VRF the BD belongs to.
        vrf = ""
        for rsctx in idx.children_of(bd, "fvRsCtx"):
            vrf = rsctx.get("tnFvCtxName") or vrf
        bd_vrf[bd.dn] = vrf or "default"

    # Encap VLAN hints from static path bindings (fvRsPathAtt under EPGs).
    bd_encap_hint: Dict[str, int] = {}
    for rspath in idx.of("fvRsPathAtt"):
        m = _VLAN_ENCAP_RE.search(rspath.get("encap"))
        if not m:
            continue
        epg = idx.get(rspath.parent_dn) if rspath.parent_dn else None
        if epg is None:
            continue
        for rsbd in idx.children_of(epg, "fvRsBd"):
            bdname = rsbd.get("tnFvBDName")
            bd = bd_by_key.get((_tenant_of(epg.dn), bdname))
            if bd is not None:
                bd_encap_hint.setdefault(bd.dn, int(m.group(1)))

    # Each BD must map to a DISTINCT VLAN id so distinct BDs render as distinct
    # broadcast domains (one shared L2 segment per BD). Encap hints win; any
    # synthetic id that would collide with a hint (or a prior BD) is skipped.
    used_vids = set(bd_encap_hint.values())
    for bd in idx.of("fvBD"):
        if bd.dn in bd_encap_hint:
            bd_vlan[bd.dn] = bd_encap_hint[bd.dn]
        else:
            while vlan_counter in used_vids:
                vlan_counter += 1
            bd_vlan[bd.dn] = vlan_counter
            used_vids.add(vlan_counter)
            vlan_counter += 1

    # ----- build per-tenant areas ----------------------------------------
    gateways: Dict[Tuple[str, str], str] = {}       # (tenant, vrf) -> gw device name
    # Per-tenant placement structures so each EPG's endpoints can be stacked in
    # the SAME column directly beneath the EPG (short endpoint links).
    t_gateways: Dict[str, List[str]] = defaultdict(list)
    t_l3outs: Dict[str, List[str]] = defaultdict(list)
    t_epg_cols: Dict[str, List[Tuple[str, List[str]]]] = defaultdict(list)  # tenant -> [(epg_dev, [endpoint_devs])]
    counts = {"tenant": 0, "vrf": 0, "bd": 0, "epg": 0, "l3out": 0, "subnet": 0,
              "endpoint": 0, "srv": 0, "pc_segment": 0}
    caveats: List[str] = []

    def _gateway(tenant: str, vrf: str) -> str:
        key = (tenant, vrf)
        if key in gateways:
            return gateways[key]
        gw_name = _unique(f"{tenant}-{vrf}-GW")
        st = sm.map_logical(gw_name, "gateway")
        mappings.append(st)
        model.devices[gw_name] = NSDevice(
            name=gw_name, area=tenant, row=_ROW_GATEWAY, stencil=st, is_endpoint=False,
            default_color=OVERLAY_COLOR,
        )
        gateways[key] = gw_name
        t_gateways[tenant].append(gw_name)
        counts["vrf"] += 1
        return gw_name

    gw_port: Dict[str, int] = {}  # gateway name -> downlink port counter

    for t in tenants:
        tenant = t.get("name")
        counts["tenant"] += 1

        # BD subnets → SVI + IP on their VRF gateway.
        for (btenant, _bname), bd in bd_by_key.items():
            if btenant != tenant:
                continue
            counts["bd"] += 1
            vrf = bd_vrf.get(bd.dn, "default")
            subnets = [s.get("ip") for s in idx.children_of(bd, "fvSubnet") if s.get("ip")]
            if not subnets:
                continue
            gw = _gateway(tenant, vrf)
            vid = bd_vlan[bd.dn]
            svi = f"Vlan {vid}"
            model.virtual_ports.append(NSVirtualPort(device=gw, port=svi, vlan_id=vid))
            model.ip_assignments.append(NSIPAssignment(device=gw, port=svi, cidrs=subnets))
            model.l2_segments_svi.append(NSL2Segment(device=gw, port=svi, vlans=[f"Vlan{vid}"]))
            if vrf and vrf != "default":
                model.vrf_renames.append((gw, svi, vrf))
            counts["subnet"] += len(subnets)

        # EPGs → devices attached to their BD VLAN on the VRF gateway.
        for ap in idx.of("fvAp"):
            if _tenant_of(ap.dn) != tenant:
                continue
            ap_name = ap.get("name") or ""
            for epg in idx.children_of(ap, "fvAEPg"):
                counts["epg"] += 1
                epg_name = epg.get("name") or "epg"
                epg_label = f"{ap_name}/{epg_name}"

                # Resolve the BD first: the device is named '<BD>-<AP>-<EPG>' and
                # is a MEMBER of the BD's shared L2 segment (= broadcast domain).
                # The Application Profile is included because an EPG's identity is
                # tenant/AP/EPG — EPG names repeat across APs (and even within one
                # BD), so '<BD>-<EPG>' alone would collide and get _2/_3 suffixes.
                bd = None
                bdname = ""
                for rsbd in idx.children_of(epg, "fvRsBd"):
                    bdname = rsbd.get("tnFvBDName") or bdname
                    bd = bd_by_key.get((tenant, bdname))
                name_parts = [p for p in (bdname, ap_name, epg_name) if p]
                dev = _unique(sanitize("-".join(name_parts)) or epg_label)
                epg_name_by_dn[epg.dn] = dev

                provides = sorted({r.get("tnVzBrCPName") for r in idx.children_of(epg, "fvRsProv") if r.get("tnVzBrCPName")})
                consumes = sorted({r.get("tnVzBrCPName") for r in idx.children_of(epg, "fvRsCons") if r.get("tnVzBrCPName")})
                role = _epg_role(provides, consumes, epg_name)
                st = sm.map_logical(dev, "epg", model=f"EPG {epg_label}")
                mappings.append(st)
                epg_attr = f"role={role} | BD {bdname or '(none)'}"
                if provides:
                    epg_attr += " | provides " + ",".join(provides)
                if consumes:
                    epg_attr += " | consumes " + ",".join(consumes)
                model.devices[dev] = NSDevice(
                    name=dev, area=tenant, row=_ROW_EPG, stencil=st, is_endpoint=True,
                    default_color=OVERLAY_COLOR, routing_attribute=epg_attr,
                )

                # Bind to the BD's shared VLAN (broadcast domain) + uplink to GW.
                vrf = bd_vrf.get(bd.dn, "default") if bd is not None else "default"
                vid = bd_vlan.get(bd.dn) if bd is not None else None
                if vid is None:
                    vid = vlan_counter
                    vlan_counter += 1
                gw = _gateway(tenant, vrf)
                gw_port[gw] = gw_port.get(gw, 0) + 1
                gport = _dummy(gw_port[gw])
                eport = _dummy(0)  # EPG's gateway-facing logical port
                model.l1_links.append(NSL1Link(dev, eport, gw, gport))
                model.l2_segments_phys.append(NSL2Segment(device=dev, port=eport, vlans=[f"Vlan{vid}"]))
                model.l2_segments_phys.append(NSL2Segment(device=gw, port=gport, vlans=[f"Vlan{vid}"]))

                # B: attach the EPG's real endpoints (fvCEp), named sna-style.
                # Server EPG -> one SRV_<AP>-<epg>_<seq> per endpoint;
                # client EPG -> all endpoints collapse into one PC_<AP>-<epg>_<n>.
                # The segment label uses AP-EPG (unique per tenant) to avoid the
                # same cross-AP name collisions as the EPG device.
                eps = eps_by_epg.get(epg.dn, [])
                seg_label = sanitize(f"{ap_name}-{epg_name}") or "seg"
                ep_devs: List[str] = []  # endpoint device names to stack under this EPG
                if role == "client" and eps:
                    n = len(eps)
                    pc_dev = _unique(f"PC_{seg_label}_{n}")
                    st_pc = sm.map_logical(pc_dev, "epg", model=f"Client PC segment x{n} ({epg_label})")
                    mappings.append(st_pc)
                    ips = [ip for e in eps for ip in e.ips]
                    attr = f"{n} client endpoint(s) in segment {epg_label}"
                    if ips:
                        attr += " | IP " + ",".join(ips[:12]) + ("..." if len(ips) > 12 else "")
                    model.devices[pc_dev] = NSDevice(
                        name=pc_dev, area=tenant, row=_ROW_ENDPOINT, stencil=st_pc,
                        is_endpoint=True, default_color=OVERLAY_COLOR, routing_attribute=attr,
                    )
                    epg_dport = _dummy(1)
                    model.l1_links.append(NSL1Link(pc_dev, _dummy(0), dev, epg_dport))
                    # Host side = L3 port with the clients' IPs; EPG (switch) side
                    # carries the BD VLAN (broadcast domain).
                    model.l2_segments_phys.append(NSL2Segment(device=dev, port=epg_dport, vlans=[f"Vlan{vid}"]))
                    pc_cidrs = [_host_cidr(ip) for ip in ips]
                    if pc_cidrs:
                        model.ip_assignments.append(NSIPAssignment(device=pc_dev, port=_dummy(0), cidrs=pc_cidrs))
                    ep_devs.append(pc_dev)
                    counts["pc_segment"] += 1
                    counts["endpoint"] += n
                else:
                    ep_port = 1  # EPG 'Dummy 0' is the gateway uplink
                    seq = 1
                    for ep in eps[:max_ep]:
                        srv_dev = _unique(f"SRV_{seg_label}_{seq}")
                        seq += 1
                        st_srv = sm.map_logical(srv_dev, "epg", model=f"Server endpoint {ep.mac}")
                        mappings.append(st_srv)
                        loc = []
                        if ep.access:
                            loc.append(("vPC " if ep.is_vpc else "") + ep.access)
                        if ep.node_ids:
                            loc.append("node-" + ",".join(ep.node_ids))
                        attr = f"MAC {ep.mac}"
                        if ep.ips:
                            attr += " | IP " + ",".join(ep.ips)
                        if ep.encap:
                            attr += f" | {ep.encap}"
                        if loc:
                            attr += " | " + " ".join(loc)
                        model.devices[srv_dev] = NSDevice(
                            name=srv_dev, area=tenant, row=_ROW_ENDPOINT, stencil=st_srv,
                            is_endpoint=True, default_color=OVERLAY_COLOR, routing_attribute=attr,
                        )
                        epg_dport = _dummy(ep_port)
                        model.l1_links.append(NSL1Link(srv_dev, _dummy(0), dev, epg_dport))
                        # Server side = L3 port with its real IP(s); EPG (switch)
                        # side carries the BD VLAN (broadcast domain).
                        model.l2_segments_phys.append(NSL2Segment(device=dev, port=epg_dport, vlans=[f"Vlan{vid}"]))
                        srv_cidrs = [_host_cidr(ip) for ip in ep.ips]
                        if srv_cidrs:
                            model.ip_assignments.append(NSIPAssignment(device=srv_dev, port=_dummy(0), cidrs=srv_cidrs))
                        ep_port += 1
                        ep_devs.append(srv_dev)
                        counts["srv"] += 1
                        counts["endpoint"] += 1
                    if len(eps) > max_ep:
                        caveats.append(f"EPG {epg_label}: {len(eps)} server endpoints; showing first {max_ep}.")

                # Record this EPG and its endpoints as one placement column.
                t_epg_cols[tenant].append((dev, ep_devs))

        # L3Outs → gray external clouds linked to their VRF gateway.
        for l3 in idx.of("l3extOut"):
            if _tenant_of(l3.dn) != tenant:
                continue
            counts["l3out"] += 1
            vrf = ""
            for rsectx in idx.children_of(l3, "l3extRsEctx"):
                vrf = rsectx.get("tnFvCtxName") or vrf
            vrf = vrf or "default"
            dev = _unique(f"L3Out-{l3.get('name')}")
            st = sm.map_logical(dev, "l3out", model=f"L3Out {l3.get('name')}")
            mappings.append(st)
            model.devices[dev] = NSDevice(
                name=dev, area=tenant, row=_ROW_L3OUT, stencil=st, is_endpoint=False,
                default_color=OVERLAY_COLOR,
            )
            t_l3outs[tenant].append(dev)
            gw = _gateway(tenant, vrf)
            gw_port[gw] = gw_port.get(gw, 0) + 1
            model.l1_links.append(
                NSL1Link(dev, _dummy(0), gw, _dummy(gw_port[gw]))
            )

    if not tenants:
        caveats.append(
            "No tenants selected (after include/exclude filtering). "
            "Check tenant_include / tenant_exclude in the config."
        )
    # Inferred / synthetic elements in the overlay (ACI has no such physical
    # objects; they are drawn only to make the policy model navigable in NS).
    caveats.append(
        "MODEL — broadcast domain = Bridge Domain: all EPGs that share a BD are "
        "bound to ONE shared L2 segment (the BD's VLAN), matching ACI's default "
        "flooding (broadcast reaches all EPGs in the BD). EPG devices are named "
        "'<BD>-<EPG>' and are members of that shared segment. NOTE: this assumes "
        "default flooding; with 'Flood in Encapsulation' enabled, each EPG encap "
        "is its own flood scope. Per-EPG access-encap VLANs are not modelled as "
        "separate broadcast domains."
    )
    caveats.append(
        "INFERRED — VRF gateway devices ('<tenant>-<vrf>-GW') are synthesised: "
        "one per VRF, standing in for ACI's distributed anycast gateway (which "
        "actually lives on every deploying leaf)."
    )
    caveats.append(
        "INFERRED — all L1 links in the overlay (EPG<->gateway, endpoint<->EPG, "
        "L3Out<->gateway) are synthetic; they represent BD/EPG membership and "
        "contract relationships, NOT real cabling (ACI has no such physical "
        "links). Their interface names use the pseudo label 'Dummy N' precisely "
        "because no real ACI interface corresponds to a logical link."
    )
    caveats.append(
        "MODEL — endpoint (server/PC) ports are L3 host ports carrying the real "
        "fvCEp/fvIp address(es) as host CIDRs (/32, /128). The BD VLAN (broadcast "
        "domain) is bound on the EPG (switch) side of each endpoint link, so the "
        "host sits in the BD broadcast domain, gatewayed by the anycast-GW SVI."
    )
    caveats.append(
        "INFERRED — VLAN ids for any BD with no encap from a static path binding "
        f"(fvRsPathAtt) are synthesised sequentially from vlan_base ({vlan_base})."
    )
    caveats.append(
        "OBSERVED — EPG, BD, subnet, VRF, contract and endpoint (fvCEp) data are "
        "taken directly from the APIC; only the topology placement above is synthetic."
    )
    caveats.append(
        "INFERRED — endpoint roles: an EPG that PROVIDES a contract is treated as a "
        "server (endpoints kept individual, named SRV_<tenant>-<epg>_<seq>); an EPG "
        "that only CONSUMES is a client (its endpoints collapse into one "
        "PC_<tenant>-<epg>_<n> segment device, sna-style); otherwise classified by "
        "EPG-name keyword. ACI does not label endpoints client/server natively."
    )

    # Custom overlay placement: each EPG and its endpoints share one column so
    # endpoint links are short vertical drops directly beneath the EPG.
    ordered_tenants = sorted(
        {tn for tn in list(t_epg_cols) + list(t_gateways) + list(t_l3outs)}
    )
    model.areas = [ordered_tenants] if ordered_tenants else []
    model.area_to_devices = {
        tn: _overlay_grid(t_l3outs.get(tn, []), t_gateways.get(tn, []),
                          t_epg_cols.get(tn, []))
        for tn in ordered_tenants
    }

    info = {"mappings": mappings, "counts": counts, "caveats": caveats}
    return model, epg_name_by_dn, info


def _overlay_grid(
    l3outs: List[str],
    gateways: List[str],
    epg_cols: List[Tuple[str, List[str]]],
) -> List[List[str]]:
    """Lay a tenant out so each EPG's endpoints stack directly under it.

    Rows (top→bottom): L3Outs, VRF gateways, the EPG row, then one row per
    endpoint depth. Column k holds EPG k and all of its endpoints, so every
    endpoint→EPG link is a short vertical segment. Gateways / L3Outs occupy the
    leftmost columns of their own rows (their links to EPGs are diagonal, which
    NS renders cleanly via port offsets)."""
    ncols = max(len(epg_cols), len(gateways), len(l3outs), 1)

    def _row(items: List[str]) -> List[str]:
        return list(items) + ["_AIR_"] * (ncols - len(items))

    grid: List[List[str]] = []
    if l3outs:
        grid.append(_row(l3outs))
    if gateways:
        grid.append(_row(gateways))
    if epg_cols:
        grid.append(_row([epg for epg, _ in epg_cols]))
        depth = max((len(eps) for _, eps in epg_cols), default=0)
        for i in range(depth):
            grid.append(_row([
                eps[i] if i < len(eps) else "_AIR_" for _, eps in epg_cols
            ]))
    return grid
