# Copyright 2026 Cisco Systems, Inc. and its affiliates
# SPDX-License-Identifier: Apache-2.0

"""Build the *physical fabric* NSModel from a Nexus Dashboard / NDFC export.

Node source:
  * ``switchesByFabric`` — each switch's ``switchRoleEnum`` (Leaf | Spine |
    Border | BorderGateway[Spine] | ...), ``logicalName``, ``model``,
    ``release`` (NX-OS version), ``ipAddress``, ``serialNumber``, ``vpcDomain``.

L1 links:
  * ``control/links`` — OBSERVED cabling. NDFC reports the *real* topology, so
    (unlike ACI) no CLOS inference is needed. ``ethisl`` links are intra-fabric
    cables (leaf<->spine, vPC peer-link); ``lan_neighbor_link`` adjacencies
    often reach switches OUTSIDE the fabric inventory (ISN / edge / core) —
    these are real, observed switches (a real sysName), just not managed by
    this fabric, so they are drawn as light-blue OBSERVED external waypoints
    (see ``_OBSERVED_WAYPOINT``), not gray inferred ones.

Layout (tier): super-spine/router row above spine, spine above leaf/border.
Switches are coloured green; external waypoints light blue (observed). Each
fabric becomes its own NS area; external neighbours share a single
``external`` waypoint area so fabric<->external links are valid (RULE 3).
"""
from __future__ import annotations

import re
from collections import Counter, defaultdict
from typing import Any, Dict, List, Optional, Tuple

from . import nd_stencil_mapper as sm
from . import nd_topology as topo
from .nd_stencil_mapper import StencilMapping, normalise_role
from .ns_model import NSDevice, NSL1Link, NSModel, build_area_layout, normalise_port_name

# Tier rows for the physical fabric.
_ROW_ROUTER = 0
_ROW_SUPERSPINE = 1
_ROW_SPINE = 2
_ROW_LEAF = 3

_SUPERSPINE_ROLES = {"superspine", "bordersuperspine", "bordergatewaysuperspine"}

# NS's native WayPoint colour (light blue), used for an OBSERVED WayPoint: an
# "external neighbour" reached via lan_neighbor_link IS a real switch reported
# by NDFC (it has a real sysName) — it is just outside the fabric inventory, so
# its role/model is unknown. That makes it observed, not inferred.
_OBSERVED_WAYPOINT = (220, 230, 242)


def _row_for(role_key: str) -> int:
    if role_key in sm.ROUTER_ROLES:
        return _ROW_ROUTER
    if role_key in _SUPERSPINE_ROLES:
        return _ROW_SUPERSPINE
    if role_key in sm.SPINE_ROLES:
        return _ROW_SPINE
    return _ROW_LEAF  # leaf / border (leaf) / tor / access / aggregation


def _sanitize(name: str) -> str:
    if name is None:
        return ""
    keep = [ch for ch in str(name).strip() if ch.isalnum() or ch in " -_.+/"]
    return re.sub(r"\s+", " ", "".join(keep)).strip()


def build_physical_model(
    idx,
    cfg: Optional[Dict[str, Any]] = None,
    layout: str = "tier",
) -> Tuple[NSModel, Dict[str, Any]]:
    """Return (NSModel, info) for the physical fabric across all fabrics in the export."""
    cfg = cfg or {}
    naming = str(cfg.get("switch_naming", "name") or "name")
    include_external = bool(cfg.get("include_external_neighbors", True))
    fabric_include = {str(x) for x in (cfg.get("fabric_include") or [])}

    model = NSModel()
    mappings: List[StencilMapping] = []
    seen_names: set = set()
    sysname_to_dev: Dict[str, str] = {}     # NDFC sys-name -> NS device name
    dev_role: Dict[str, str] = {}           # NS device name -> role_key
    counts = {"spine": 0, "leaf": 0, "border": 0, "bgw": 0, "router": 0,
              "switch": 0, "external": 0, "l1_links": 0, "vpc_switches": 0}
    caveats: List[str] = []

    # Optional richer sources (used when present, ignored when absent).
    vpc_map: Dict[str, Dict[str, str]] = {}
    for fab in idx.vpc_pairs:
        vpc_map.update(topo.vpc_pairs(idx.vpc_pairs[fab]))
    itf_by_serial: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for fab in idx.interfaces:
        for itf in idx.interfaces[fab]:
            s = itf.get("serialNumber") or itf.get("serialNo")
            if s:
                itf_by_serial[str(s)].append(itf)

    def _unique(name: str) -> str:
        name = _sanitize(name) or "switch"
        base, i = name, 2
        while name in seen_names:
            name = f"{base}_{i}"
            i += 1
        seen_names.add(name)
        return name

    fabrics = [f for f in idx.fabric_names() if not fabric_include or f in fabric_include]

    # ----- switches → devices --------------------------------------------
    for fabric in fabrics:
        for sw in idx.switches.get(fabric, []):
            raw_role = topo.switch_role(sw)
            role_key = normalise_role(raw_role)
            inferred = role_key not in sm.ROLE_TABLE
            if inferred:
                role_key = _infer_role_from_name(sw)
            disp = topo.switch_display_name(sw, naming)
            name = _unique(disp)
            st = sm.map_fabric_node(
                name=name, role=raw_role or role_key,
                serial=sw.get("serialNumber") or "",
                model_hint=sw.get("model") or "",
                os_hint=topo.switch_version(sw),
                inferred=inferred,
            )
            mappings.append(st)
            dev_role[name] = role_key

            for key in (sw.get("logicalName"), sw.get("sysName"),
                        sw.get("hostName"), sw.get("serialNumber"),
                        sw.get("ipAddress")):
                if key:
                    sysname_to_dev.setdefault(str(key), name)

            # Attribute-D: mgmt IP / serial / role / vPC.
            attr_bits: List[str] = []
            ip = sw.get("ipAddress") or sw.get("mgmtAddress")
            if ip:
                attr_bits.append(f"mgmt {ip}")
            if sw.get("serialNumber"):
                attr_bits.append(f"serial {sw['serialNumber']}")
            if raw_role:
                attr_bits.append(f"role {raw_role}")
            serial = str(sw.get("serialNumber") or "")
            vpc = sw.get("vpcDomain")
            vp = vpc_map.get(serial)
            if vp:  # authoritative vPC pairing from the /vpcpair source
                peer = vp.get("peerName") or vp.get("peer")
                dom = vp.get("domain")
                attr_bits.append(f"vPC peer {peer}" + (f" (domain {dom})" if dom else ""))
                counts["vpc_switches"] += 1
            elif vpc not in (None, 0, "0", ""):  # fallback: per-switch vpcDomain field
                attr_bits.append(f"vPC domain {vpc}")
                counts["vpc_switches"] += 1

            # Real per-port speed/duplex/media from interface detail, when fetched.
            pinfo = None
            specs = topo.interface_portinfo(itf_by_serial.get(serial, []))
            if specs:
                pinfo = Counter(specs.values()).most_common(1)[0][0]

            row = _row_for(role_key)
            model.devices[name] = NSDevice(
                name=name, area=fabric, row=row, stencil=st, is_endpoint=False,
                routing_attribute=" | ".join(attr_bits), port_info=pinfo,
            )
            counts["switch"] += 1
            if role_key in sm.ROUTER_ROLES:
                counts["router"] += 1
            elif role_key in {"bordergateway", "bordergatewayspine", "bordergatewaysuperspine"}:
                counts["bgw"] += 1
            elif role_key in {"border", "borderspine", "bordersuperspine"}:
                counts["border"] += 1
            elif role_key in sm.SPINE_ROLES or role_key in _SUPERSPINE_ROLES:
                counts["spine"] += 1
            else:
                counts["leaf"] += 1

    # ----- links → L1 links ----------------------------------------------
    ext_seen: set = set()

    def _external_device(sysname: str, model_name: str) -> Optional[str]:
        if not include_external:
            return None
        if sysname in sysname_to_dev:
            return sysname_to_dev[sysname]
        if sysname in ext_seen:
            return sysname_to_dev.get(sysname)
        ext_seen.add(sysname)
        name = _unique(sysname)
        st = sm.map_logical(name, "external",
                            model=f"External neighbour ({model_name})" if model_name else "External neighbour")
        mappings.append(st)
        model.devices[name] = NSDevice(
            name=name, area="external", row=_ROW_ROUTER, stencil=st, is_endpoint=False,
            default_color=_OBSERVED_WAYPOINT,
        )
        sysname_to_dev[sysname] = name
        dev_role[name] = "external"
        counts["external"] += 1
        return name

    # Gather candidate links and rank them for the one-cable-per-port contest:
    #   1. fabric ISLs first  (real intra-fabric cabling)
    #   2. then PRESENT neighbour links (is-present=True — an actual cable)
    #   3. then non-present neighbour links (is-present=False — planned / stale).
    # NS enforces one cable per physical port, but NDFC neighbour discovery can
    # report several neighbours on one local port (e.g. a spine uplink that has
    # both a real and a planned ISN adjacency). Without this ranking the stale
    # link could win the port and leave the real neighbour isolated.
    candidates: List[tuple] = []
    for fabric in fabrics:
        for link in idx.links.get(fabric, []):
            ep = topo.link_endpoints(link)
            if ep is None:
                continue
            (sa, ifa, _ra, ma), (sb, ifb, _rb, mb) = ep
            is_fabric = topo.is_fabric_link(link)
            present = link.get("is-present")
            present_rank = 0 if (present in (True, "true", None) or is_fabric) else 1
            candidates.append((0 if is_fabric else 1, present_rank, sa, ifa, ma, sb, ifb, mb))
    candidates.sort(key=lambda c: (c[0], c[1]))

    link_seen: set = set()
    used_ports: set = set()      # (device, port) — one cable per physical port
    dropped_ports = 0
    for _prio, _pres, sa, ifa, ma, sb, ifb, mb in candidates:
        da = sysname_to_dev.get(sa) or _external_device(sa, ma)
        db = sysname_to_dev.get(sb) or _external_device(sb, mb)
        if not da or not db or da == db:
            continue
        pa = normalise_port_name(ifa) or "Ethernet 1/1"
        pb = normalise_port_name(ifb) or "Ethernet 1/1"
        key = frozenset({(da, pa), (db, pb)})
        if key in link_seen:
            continue
        if (da, pa) in used_ports or (db, pb) in used_ports:
            dropped_ports += 1
            continue
        link_seen.add(key)
        used_ports.add((da, pa))
        used_ports.add((db, pb))
        model.l1_links.append(NSL1Link(da, pa, db, pb))
    counts["l1_links"] = len(model.l1_links)
    counts["dropped_links"] = dropped_ports

    # Prune external waypoints left with NO link after the port contest (e.g. a
    # stale/not-present neighbour whose only adjacency lost its port to a real
    # cable). A dangling, disconnected external node is misleading, so it is
    # removed rather than drawn floating. Real fabric switches are never pruned.
    linked = {lk.a_device for lk in model.l1_links} | {lk.b_device for lk in model.l1_links}
    pruned = [n for n, d in model.devices.items()
              if d.area == "external" and n not in linked]
    for n in pruned:
        del model.devices[n]
        counts["external"] -= 1
    counts["pruned_external"] = len(pruned)

    model.areas, model.area_to_devices = build_area_layout(
        model.devices, model.l1_links, layout=layout,
    )

    # ----- caveats --------------------------------------------------------
    caveats.append(
        "OBSERVED — switch roles, models, NX-OS versions, mgmt IPs and ALL L1 "
        "cabling are taken directly from NDFC (switchesByFabric + control/links). "
        "Unlike ACI, no CLOS topology is inferred — the links are the real fabric."
    )
    if counts["vpc_switches"]:
        caveats.append(
            f"OBSERVED — {counts['vpc_switches']} switch(es) carry a vPC pairing "
            "annotation (from the /vpcpair source or the switch vpcDomain field) "
            "in Attribute-D."
        )
    if itf_by_serial:
        caveats.append(
            "OBSERVED — physical-port Speed/Duplex/Media come from NDFC interface "
            "detail where available (else role-based defaults). Virtual platforms "
            "(N9Kv) report no speed, so the default is used."
        )
    if dropped_ports:
        caveats.append(
            f"INFERRED — {dropped_ports} link(s) were dropped because their local "
            "port was already cabled (NDFC neighbour discovery can report multiple "
            "adjacencies on one physical port; NS allows one cable per port). "
            "Kept in preference order: fabric ISL > present (is-present=True) cable "
            "> planned/not-present adjacency."
        )
    if counts.get("pruned_external"):
        caveats.append(
            f"INFERRED — {counts['pruned_external']} external neighbour(s) were "
            "removed from the diagram because they had no link left after the "
            "one-cable-per-port contest (typically a not-present/planned adjacency "
            "that lost its port to a real cable). They are omitted rather than "
            "drawn as floating, disconnected nodes."
        )
    if counts["external"]:
        caveats.append(
            f"OBSERVED (unmanaged) — {counts['external']} external neighbour(s) "
            "reached via 'lan_neighbor_link' adjacencies are NOT in the fabric "
            "inventory (ISN / edge / core); they are drawn as light-blue observed "
            "WayPoints with no role/model detail."
        )
    # Spine/leaf presence is by tier row (a Border-Gateway-Spine counts as a
    # spine here even though it is tallied separately under 'bgw').
    spine_tier = sum(1 for d in model.devices.values()
                     if d.row in (_ROW_SUPERSPINE, _ROW_SPINE))
    leaf_tier = sum(1 for d in model.devices.values() if d.row == _ROW_LEAF)
    if not spine_tier or not leaf_tier:
        caveats.append(
            f"Incomplete fabric: {spine_tier} spine-tier node(s), {leaf_tier} leaf-tier node(s)."
        )
    if not model.l1_links:
        caveats.append(
            "No L1 links found in the export (control/links was empty); the "
            "diagram shows isolated nodes."
        )

    info = {
        "mappings": mappings,
        "counts": counts,
        "fabrics": fabrics,
        "caveats": caveats,
    }
    return model, info


_NAME_ROLE_KW = [
    ("bordergatewayspine", ("bgw-spine", "bgwspine", "border-gateway-spine")),
    ("bordergateway", ("bgw", "border-gateway", "bordergw")),
    ("borderspine", ("border-spine",)),
    ("superspine", ("superspine", "super-spine", "ssp")),
    ("spine", ("spine", "-sp")),
    ("border", ("border", "-bl", "borderleaf")),
    ("leaf", ("leaf", "-lf")),
    ("edgerouter", ("edge", "edgerouter")),
    ("corerouter", ("core", "corerouter")),
]


def _infer_role_from_name(sw: Dict[str, Any]) -> str:
    """Best-effort role from the switch name when switchRoleEnum is unknown."""
    nm = (sw.get("logicalName") or sw.get("sysName") or sw.get("hostName") or "").lower()
    for role_key, kws in _NAME_ROLE_KW:
        if any(k in nm for k in kws):
            return role_key
    return "leaf"
