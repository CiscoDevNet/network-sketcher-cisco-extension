# Copyright 2026 Cisco Systems, Inc. and its affiliates
# SPDX-License-Identifier: Apache-2.0

"""Build the *physical campus* NSModel from a Cisco Catalyst Center export.

Node source:
  * ``devices`` — each managed device's ``role`` (CORE | DISTRIBUTION | ACCESS |
    BORDER ROUTER | ...), ``family`` (Routers | Switches and Hubs | Wireless
    Controller | Unified AP), ``hostname``, ``platformId``, ``softwareType`` +
    ``softwareVersion``, ``managementIpAddress``, ``serialNumber``.

L1 links:
  * ``physicalTopology.links`` — OBSERVED cabling. Catalyst Center reports the
    *real* topology (like NDFC), so no CLOS inference is needed. ``source`` /
    ``target`` are topology node ids that EQUAL network-device ids for managed
    devices; ``startPortName`` / ``endPortName`` are the cabled ports. A
    topology node whose id is NOT a managed device (e.g. an unmanaged neighbour)
    is drawn as a light-blue OBSERVED external waypoint (it is a real topology
    node, just not one Catalyst Center manages — see ``_OBSERVED_WAYPOINT``).
  * INFERRED L3 links (``include_inferred_links``, default True) — Catalyst
    Center's physical topology only reports CDP/LLDP adjacencies between MANAGED
    devices, so devices reachable only across UNMANAGED switches appear
    isolated. An inference pass supplements the cabling from shared routed
    interface subnets: two devices on a subnet not already CDP-linked get a
    direct inferred link; 3+ devices on a subnet get a single light-gray
    shared-segment DEVICE (named by the CIDR) linked to each member. Loopbacks
    (/32), the OOB management subnet, and anycast SVIs (same host IP on 2+
    devices) are excluded. These links represent L3 reachability across
    unmanaged switching, not directly observed cables.

Layout (connectivity distance): rows are assigned by hop distance from the set
of FABRIC devices (``idx.fabric_devices`` network-device ids) over the underlay
link graph (CDP + inferred links + the shared-segment node), so the fabric is
the bottom tier and devices climb upward by how far they are from it. Directly
connected devices land in adjacent rows; an inferred shared-segment node sits
between its members. Switches are coloured green; the inferred shared-segment
node is light-gray (it is an inferred, not observed, node) but is drawn as a
normal DEVICE in the SAME ``campus`` area. All devices land in a single
``campus`` area (Catalyst Center returns one campus); genuinely unmanaged
external neighbours share a single ``external`` waypoint area so
device<->external links are valid (RULE 3).
"""
from __future__ import annotations

import ipaddress
import re
from collections import Counter, deque
from typing import Any, Dict, List, Optional, Set, Tuple

from . import catc_stencil_mapper as sm
from . import catc_topology as topo
from .catc_stencil_mapper import StencilMapping, normalise_role
from .ns_model import (
    NSDevice, NSIPAssignment, NSL1Link, NSL2Segment, NSModel, NSVirtualPort,
    build_area_layout, normalise_port_name,
)

# Tier rows for the physical campus.
_ROW_BORDER = 0     # router / border / border-router (L3 hand-off)
_ROW_CORE = 0
_ROW_DISTRIBUTION = 1
_ROW_WLC = 1
_ROW_ACCESS = 2
_ROW_AP = 3

_AREA_CAMPUS = "campus"
_AREA_EXTERNAL = "external"

# Light-gray colour for an INFERRED node (the shared L3 segment). The segment is
# drawn as a normal DEVICE (NOT a WayPoint) in the campus area, but light-gray so
# it reads as "inferred, not observed". It uses a Switch stencil (not Cloud) so
# the attribute builder classifies it as DEVICE rather than WayPoint.
_SEG_COLOR = (200, 200, 200)

# NS's native WayPoint colour (light blue), reused here for an OBSERVED WayPoint:
# an "external neighbour" node IS a real topology node reported by Catalyst
# Center's physical-topology API (it has a real label/IP/node id) — it is just
# not a managed device, so its role/model is unknown. That makes it observed,
# not inferred, unlike the gray _SEG_COLOR node above (which the converter
# invents from a shared subnet with no corresponding topology node at all).
_OBSERVED_WAYPOINT = (220, 230, 242)


def _row_for(role_key: str) -> int:
    if role_key in sm.ROUTER_ROLES or role_key in sm.BORDER_ROLES:
        return _ROW_BORDER
    if role_key == "core":
        return _ROW_CORE
    if role_key == "distribution":
        return _ROW_DISTRIBUTION
    if role_key == "wlc":
        return _ROW_WLC
    if role_key == "ap":
        return _ROW_AP
    if role_key in sm.ACCESS_ROLES:
        return _ROW_ACCESS
    return _ROW_ACCESS


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
    """Return (NSModel, info) for the physical campus across all devices in the export."""
    cfg = cfg or {}
    naming = str(cfg.get("device_naming", "hostname") or "hostname")
    strip_suffix = str(cfg.get("strip_domain_suffix", "") or "")
    include_external = bool(cfg.get("include_external_neighbors", True))

    model = NSModel()
    mappings: List[StencilMapping] = []
    seen_names: set = set()
    id_to_dev: Dict[str, str] = {}       # network-device id -> NS device name
    dev_role: Dict[str, str] = {}        # NS device name -> role_key
    ap_ids: Set[str] = set()             # network-device ids of Unified APs (RULE 12)
    mgmt_ip_by_name: Dict[str, str] = {}  # NS device name -> its own managementIpAddress
    include_inferred = bool(cfg.get("include_inferred_links", True))
    counts = {"core": 0, "distribution": 0, "access": 0, "border": 0,
              "router": 0, "wlc": 0, "ap": 0, "device": 0, "external": 0, "l1_links": 0,
              "inferred_links": 0, "inferred_segments": 0,
              "ip_assignments": 0, "loopbacks": 0, "dummy_wireless_client": 0}
    caveats: List[str] = []

    def _unique(name: str) -> str:
        name = _sanitize(name) or "device"
        base, i = name, 2
        while name in seen_names:
            name = f"{base}_{i}"
            i += 1
        seen_names.add(name)
        return name

    # ----- devices → NSDevice --------------------------------------------
    for dev in idx.devices:
        raw_role = topo.device_role(dev)
        family = topo.device_family(dev)
        role_key = normalise_role(raw_role)
        fam_key = normalise_role(family)
        known = role_key in sm.ROLE_TABLE or fam_key in sm.FAMILY_TO_ROLE
        inferred = not known
        if fam_key in sm.FAMILY_TO_ROLE:
            # Wireless families (WLC / AP) win over a generic campus role.
            role_key = sm.FAMILY_TO_ROLE[fam_key]
        elif inferred:
            role_key = _infer_role_from_name(dev)

        disp = topo.device_display_name(dev, naming, strip_suffix)
        name = _unique(disp)
        st = sm.map_device(
            name=name, role=raw_role or role_key, family=family,
            serial=topo.device_serial(dev),
            model_hint=topo.device_platform(dev),
            os_type=topo.device_os_type(dev),
            os_version=topo.device_os_version(dev),
            inferred=inferred,
        )
        mappings.append(st)
        dev_role[name] = role_key
        did = dev.get("id")
        if did:
            id_to_dev[str(did)] = name

        # Attribute-D: mgmt IP / serial / role / family.
        attr_bits: List[str] = []
        ip = topo.device_mgmt_ip(dev)
        if ip:
            attr_bits.append(f"mgmt {ip}")
        if topo.device_serial(dev):
            attr_bits.append(f"serial {topo.device_serial(dev)}")
        if raw_role:
            attr_bits.append(f"role {raw_role}")
        if family:
            attr_bits.append(f"family {family}")

        # Real per-port speed/duplex/media from interface detail, when fetched.
        pinfo = None
        specs = topo.interface_portinfo(idx.interfaces.get(str(did), [])) if did else {}
        if specs:
            pinfo = Counter(specs.values()).most_common(1)[0][0]

        row = _row_for(role_key)
        model.devices[name] = NSDevice(
            name=name, area=_AREA_CAMPUS, row=row, stencil=st, is_endpoint=False,
            routing_attribute=" | ".join(attr_bits), port_info=pinfo,
        )
        counts["device"] += 1
        if ip:
            mgmt_ip_by_name[name] = ip
        if role_key in sm.ROUTER_ROLES:
            counts["router"] += 1
        elif role_key in sm.BORDER_ROLES:
            counts["border"] += 1
        elif role_key == "core":
            counts["core"] += 1
        elif role_key == "distribution":
            counts["distribution"] += 1
        elif role_key == "wlc":
            counts["wlc"] += 1
        elif role_key == "ap":
            counts["ap"] += 1
            if did:
                ap_ids.add(str(did))
        else:
            counts["access"] += 1

    # ----- links → L1 links ----------------------------------------------
    ext_seen: set = set()
    node_by_id: Dict[str, Dict[str, Any]] = {str(n.get("id")): n for n in idx.topo_nodes if n.get("id")}

    def _external_device(node_id: str) -> Optional[str]:
        if not include_external or not node_id:
            return None
        if node_id in id_to_dev:
            return id_to_dev[node_id]
        if node_id in ext_seen:
            return id_to_dev.get(node_id)
        ext_seen.add(node_id)
        node = node_by_id.get(node_id, {})
        label = node.get("label") or node.get("ip") or node_id
        name = _unique(label)
        st = sm.map_logical(name, "external",
                            model=f"External neighbour ({node.get('deviceType') or node.get('family') or '?'})")
        mappings.append(st)
        model.devices[name] = NSDevice(
            name=name, area=_AREA_EXTERNAL, row=_ROW_BORDER, stencil=st, is_endpoint=False,
            default_color=_OBSERVED_WAYPOINT,
        )
        id_to_dev[node_id] = name
        dev_role[name] = "external"
        counts["external"] += 1
        return name

    link_seen: set = set()
    used_ports: set = set()      # (device, port) — one cable per physical port
    linked_pairs: set = set()    # frozenset({devA, devB}) directly CDP-linked
    dropped_ports = 0
    for link in idx.topo_links:
        src = str(link.get("source") or "")
        tgt = str(link.get("target") or "")
        if not src or not tgt:
            continue
        da = id_to_dev.get(src) or _external_device(src)
        db = id_to_dev.get(tgt) or _external_device(tgt)
        if not da or not db or da == db:
            continue
        pa, pb = topo.link_port_names(link)
        pa = pa or "GigabitEthernet 0/0"
        pb = pb or "GigabitEthernet 0/0"
        key = frozenset({(da, pa), (db, pb)})
        if key in link_seen:
            continue
        if (da, pa) in used_ports or (db, pb) in used_ports:
            dropped_ports += 1
            continue
        link_seen.add(key)
        used_ports.add((da, pa))
        used_ports.add((db, pb))
        linked_pairs.add(frozenset({da, db}))
        model.l1_links.append(NSL1Link(da, pa, db, pb))
    counts["dropped_links"] = dropped_ports

    # ----- inferred L3 / subnet links -------------------------------------
    # Catalyst Center's physicalTopology only reports CDP/LLDP adjacencies
    # between managed devices, so devices reachable only across UNMANAGED
    # switches show up isolated. Supplement the observed cabling with links
    # inferred from shared interface IP subnets (does NOT alter CDP links).
    segment_names: Set[str] = set()
    ap_svi_by_name: Dict[str, str] = {}   # AP NS name -> its RULE 12 Management SVI port
    if include_inferred:
        ap_names = {id_to_dev[i] for i in ap_ids if i in id_to_dev}
        segment_names = _add_inferred_links(
            idx, model, mappings, counts, _unique,
            id_to_dev, linked_pairs, used_ports,
            ap_ids=ap_ids, ap_names=ap_names, mgmt_ip_by_name=mgmt_ip_by_name,
            ap_svi_by_name=ap_svi_by_name)

    counts["l1_links"] = len(model.l1_links)

    # ----- interface IP addresses (underlay) ------------------------------
    # Attach each device's routed-interface IPs so they render in the L3
    # diagram + device table. A link-port IP lands on its existing L1 port; a
    # loopback IP becomes a virtual Loopback port + IP; the OOB management IP
    # and any other non-link, non-loopback interface IP are skipped (those
    # ports are not in the topology).
    add_interface_ips(idx, model, id_to_dev, counts)

    # Prune external waypoints left with NO link after the port contest. A
    # dangling, disconnected external node is misleading, so it is removed
    # rather than drawn floating. Real campus devices are never pruned.
    linked = {lk.a_device for lk in model.l1_links} | {lk.b_device for lk in model.l1_links}
    pruned = [n for n, d in model.devices.items()
              if d.area == _AREA_EXTERNAL and n not in linked]
    for n in pruned:
        del model.devices[n]
        counts["external"] -= 1
    counts["pruned_external"] = len(pruned)

    # ----- connectivity-distance row layout -------------------------------
    # Override each campus device's tier row with its hop distance from the set
    # of FABRIC devices over the underlay link graph (CDP + inferred + segment),
    # so the fabric sits at the bottom and devices climb upward by distance, with
    # directly-connected devices in adjacent rows and the inferred segment node
    # between its members. External-waypoint devices keep their own row.
    fabric_names = _fabric_device_names(idx, id_to_dev)
    ap_names_for_rows = {id_to_dev[i] for i in ap_ids if i in id_to_dev}
    _assign_connectivity_rows(model, fabric_names, segment_names, ap_names_for_rows)

    # ----- per-SSID dummy wireless clients (mirrors the overlay / meraki_converter) -
    # Each configured SSID becomes a synthetic wireless interface on the AP,
    # linked DOWN to a gray dummy PC standing in for that SSID's (unobserved)
    # wireless clients. Placed one row below its AP (after RULE 12 row
    # assignment above), so it renders beneath the AP in the underlay too.
    _add_wireless_dummy_clients(idx, model, mappings, counts, _unique, ap_ids, id_to_dev, ap_svi_by_name)

    model.areas, model.area_to_devices = build_area_layout(
        model.devices, model.l1_links, layout=layout,
    )

    # ----- caveats --------------------------------------------------------
    caveats.append(
        "OBSERVED — device roles, platforms, IOS-XE versions, mgmt IPs and ALL "
        "L1 cabling are taken directly from Catalyst Center (network-device "
        "inventory + physical-topology). Like NDFC and unlike ACI, no topology "
        "is inferred — the links are the real campus cabling."
    )
    if idx.interfaces:
        caveats.append(
            "OBSERVED — physical-port Speed/Duplex/Media come from Catalyst Center "
            "interface detail where available (else role-based defaults). Requires "
            "--with-interfaces at fetch time (one call per device, heavy)."
        )
    if dropped_ports:
        caveats.append(
            f"INFERRED — {dropped_ports} link(s) were dropped because their local "
            "port was already cabled (NS allows one cable per physical port)."
        )
    if counts.get("pruned_external"):
        caveats.append(
            f"INFERRED — {counts['pruned_external']} external neighbour(s) were "
            "removed from the diagram because they had no link left after the "
            "one-cable-per-port contest (drawn nowhere rather than floating)."
        )
    if counts["external"]:
        caveats.append(
            f"OBSERVED (unmanaged) — {counts['external']} external neighbour(s) "
            "appear in the physical topology but are NOT Catalyst-Center-managed "
            "devices; they are drawn as light-blue observed WayPoints with no "
            "role/model detail."
        )
    if counts.get("inferred_links") or counts.get("inferred_segments"):
        caveats.append(
            f"INFERRED — {counts['inferred_links']} L3/subnet-inferred link(s) and "
            f"{counts['inferred_segments']} light-gray shared-segment device(s) were "
            "ADDED on top of the observed CDP/LLDP cabling. Catalyst Center's "
            "physicalTopology only reports adjacencies between MANAGED devices, so "
            "devices reachable only across UNMANAGED switches appear isolated. "
            "These extra links/segments are derived from devices sharing a routed "
            "IP subnet (interface ipv4Address+mask); they represent L3 reachability "
            "that traverses unmanaged switching, NOT a directly observed cable. "
            "Loopbacks (/32), the OOB management subnet, and anycast SVIs (the same "
            "host IP on 2+ devices) are excluded. Set include_inferred_links=false "
            "to draw only the observed CDP/LLDP cabling."
        )
    if counts.get("ap"):
        caveats.append(
            "MODEL (RULE 12, FlexConnect default) — Wireless APs are modeled as "
            "the one endpoint-class device with an SVI: the physical uplink port "
            "carries an L2 segment for the Management VLAN (no IP on the physical "
            "port), and a Management SVI ('Vlan <N>') carries the AP's REAL "
            "managementIpAddress. Catalyst Center does not return per-interface "
            "detail for APs (the interface fetch 404s), so an AP joins its shared "
            "L3 segment via its OWN managementIpAddress falling inside an "
            "already-established subnet (INFERRED), and the segment's VLAN number "
            "is read from another REAL member's 'Vlan <N>' port name when present "
            "(else defaults to VLAN 1)."
        )
    if counts.get("dummy_wireless_client"):
        caveats.append(
            f"MODEL — {counts['dummy_wireless_client']} dummy wireless client(s) "
            "were added (one per configured SSID per AP, gray PC stencil) to "
            "represent each SSID's unobserved wireless clients, placed one row "
            "below their AP — the SAME per-SSID dummy-client convention used by "
            "the overlay diagram and by meraki_converter."
        )
    if not model.l1_links:
        caveats.append(
            "No L1 links found in the export (physicalTopology.links was empty); "
            "the diagram shows isolated nodes."
        )

    info = {
        "mappings": mappings,
        "counts": counts,
        "caveats": caveats,
    }
    return model, info


def segment_stencil(name: str, cidr: str) -> StencilMapping:
    """StencilMapping for an inferred L3 shared-segment node.

    Deliberately a ``Switch`` stencil (NOT ``Cloud``): the attribute builder
    classifies a Cloud stencil as a 'WayPoint', but the segment must render as a
    DEVICE in the campus area. It is still flagged inferred (light-gray colour is
    applied on the NSDevice via ``default_color``) and confidence 0.60.
    """
    return StencilMapping(
        label=name,
        node_definition="l3-segment",
        image_definition="",
        stencil_type=sm.NS_SWITCH,
        model=f"L3 shared segment {cidr} (inferred)",
        os="",
        confidence=0.60,
        reason=f"inferred shared L3 segment {cidr}",
        tags=["inferred", "l3-segment"],
    )


def _add_inferred_links(idx, model, mappings, counts, unique_fn,
                        id_to_dev, linked_pairs, used_ports,
                        ap_ids: Optional[Set[str]] = None,
                        ap_names: Optional[Set[str]] = None,
                        mgmt_ip_by_name: Optional[Dict[str, str]] = None,
                        ap_svi_by_name: Optional[Dict[str, str]] = None) -> Set[str]:
    """Supplement the observed CDP/LLDP cabling with L3/subnet-inferred links.

    Uses the shared :func:`catc_topology.infer_subnet_adjacencies` helper (so the
    overlay mapper derives the SAME non-fabric attachments). For each subnet:
      * exactly 2 devices -> ONE inferred direct NSL1Link,
      * 3+ devices        -> ONE light-gray shared-segment DEVICE (named by the
                             CIDR, in the campus area) linked to EACH member.

    ``ap_ids`` lets a Unified AP with NO real interface data join a segment via
    its own ``managementIpAddress`` (see ``infer_subnet_adjacencies``'s
    ``mgmt_fallback_ids``). Once linked, an AP member (``ap_names``) additionally
    gets RULE 12 FlexConnect modeling (Management SVI carrying its real IP,
    physical port carries an L2 segment instead) via
    :func:`catc_topology.apply_ap_flexconnect`.

    Returns the set of inferred shared-segment NS device names created.
    """
    segment_names: Set[str] = set()
    ap_names = ap_names or set()
    mgmt_ip_by_name = mgmt_ip_by_name or {}
    direct_pairs, segments = topo.infer_subnet_adjacencies(
        idx, id_to_dev, linked_pairs, mgmt_fallback_ids=ap_ids)

    for (da, pa, db, pb) in direct_pairs:
        if (da, pa) in used_ports or (db, pb) in used_ports:
            continue
        used_ports.add((da, pa))
        used_ports.add((db, pb))
        linked_pairs.add(frozenset({da, db}))
        model.l1_links.append(NSL1Link(da, pa, db, pb))
        counts["inferred_links"] += 1

    for cidr, per_dev in segments:
        # 3+ devices: one light-gray shared-segment DEVICE named by the CIDR,
        # placed in the SAME campus area as the real devices (NOT a waypoint).
        seg = unique_fn(cidr)
        st = segment_stencil(seg, cidr)
        mappings.append(st)
        model.devices[seg] = NSDevice(
            name=seg, area=_AREA_CAMPUS, row=_ROW_BORDER, stencil=st,
            is_endpoint=False, default_color=_SEG_COLOR,
            routing_attribute=f"L3 shared segment {cidr} (inferred)",
        )
        segment_names.add(seg)
        counts["inferred_segments"] += 1
        vlan_tag = topo.vlan_tag_for_segment(per_dev)
        try:
            prefixlen = ipaddress.ip_network(cidr).prefixlen
        except ValueError:
            prefixlen = 24
        for i, dn in enumerate(sorted(per_dev), start=1):
            mport = per_dev[dn]
            if (dn, mport) in used_ports:
                continue
            used_ports.add((dn, mport))
            model.l1_links.append(NSL1Link(dn, mport, seg, _dummy(i)))
            counts["inferred_links"] += 1
            if dn in ap_names:
                # RULE 12 (FlexConnect default): the AP's physical uplink carries
                # an L2 segment (no IP); its own Management SVI carries the IP.
                svi = topo.apply_ap_flexconnect(
                    model, dn, mport, vlan_tag, mgmt_ip_by_name.get(dn),
                    prefixlen, counts)
                if ap_svi_by_name is not None:
                    ap_svi_by_name[dn] = svi
    return segment_names


def _dummy(n: int) -> str:
    """Pseudo port on the shared-segment node side (no real cabled port)."""
    return f"Dummy {n}"


def iface_cidr(ip: str, mask: str) -> Optional[str]:
    """Build a '<ip>/<prefixlen>' CIDR from an IPv4 address + dotted/prefix mask.

    Accepts a dotted netmask ('255.255.255.252') or a prefix length ('30').
    Returns None on any parse failure.
    """
    ip = (ip or "").strip()
    mask = (mask or "").strip()
    if not ip or not mask:
        return None
    try:
        if "." in mask:
            prefix = ipaddress.IPv4Network(f"0.0.0.0/{mask}").prefixlen
        else:
            prefix = int(mask)
        ipaddress.IPv4Address(ip)  # validate the address itself
    except (ValueError, TypeError):
        return None
    if not 0 <= prefix <= 32:
        return None
    return f"{ip}/{prefix}"


def link_ports_by_device(model: NSModel) -> Dict[str, Set[str]]:
    """{device name -> set of its real L1-link port names} from the model links."""
    out: Dict[str, Set[str]] = {}
    for lk in model.l1_links:
        out.setdefault(lk.a_device, set()).add(lk.a_port)
        out.setdefault(lk.b_device, set()).add(lk.b_port)
    return out


def add_interface_ips(
    idx,
    model: NSModel,
    id_to_dev: Dict[str, str],
    counts: Optional[Dict[str, int]] = None,
    only_devices: Optional[Set[str]] = None,
) -> None:
    """Attach routed-interface IPs to the devices already in ``model``.

    For every managed device (network-device id -> NS name in ``id_to_dev``)
    and every interface with ``ipv4Address`` + ``ipv4Mask``:

      * if the interface's normalised ``portName`` is ALREADY a real L1-link
        port in the model -> add an ``NSIPAssignment`` on that port;
      * else if it is a Loopback -> add an ``NSVirtualPort`` (is_loopback) plus
        an ``NSIPAssignment`` for the loopback (RLOCs etc.);
      * else if the IP equals the device's managementIpAddress -> skip (OOB
        mgmt, already shown in Attribute-D; its port is not in the topology);
      * else skip (a non-link, non-loopback interface whose port is absent).

    ``only_devices`` (optional NS names) restricts the work to those devices —
    used by the overlay to add IPs to just the non-fabric underlay devices.
    """
    counts = counts if counts is not None else {}
    link_ports = link_ports_by_device(model)
    for did, devname in id_to_dev.items():
        if only_devices is not None and devname not in only_devices:
            continue
        if devname not in model.devices:
            continue
        dev = idx.device_by_id.get(str(did)) or {}
        mgmt = topo.device_mgmt_ip(dev)
        dev_link_ports = link_ports.get(devname, set())
        for itf in idx.interfaces.get(str(did), []):
            raw_ip = itf.get("ipv4Address")
            cidr = iface_cidr(raw_ip, itf.get("ipv4Mask"))
            if not cidr:
                continue
            raw_port = itf.get("portName") or itf.get("name") or ""
            port = normalise_port_name(raw_port)
            if not port:
                continue
            if port in dev_link_ports:
                model.ip_assignments.append(
                    NSIPAssignment(device=devname, port=port, cidrs=[cidr]))
                counts["ip_assignments"] = counts.get("ip_assignments", 0) + 1
            elif raw_port.strip().lower().startswith("loopback"):
                model.virtual_ports.append(
                    NSVirtualPort(device=devname, port=port, is_loopback=True))
                model.ip_assignments.append(
                    NSIPAssignment(device=devname, port=port, cidrs=[cidr]))
                counts["ip_assignments"] = counts.get("ip_assignments", 0) + 1
                counts["loopbacks"] = counts.get("loopbacks", 0) + 1
            elif str(raw_ip).strip() == (mgmt or "").strip():
                continue  # OOB mgmt interface — not a topology port
            else:
                continue  # non-link, non-loopback interface — port absent


def _fabric_device_names(idx, id_to_dev: Dict[str, str]) -> Set[str]:
    """NS device names of the SD-Access fabric devices (idx.fabric_devices)."""
    out: Set[str] = set()
    for fd in getattr(idx, "fabric_devices", []) or []:
        ndid = str(fd.get("networkDeviceId") or "")
        name = id_to_dev.get(ndid)
        if name:
            out.add(name)
    return out


def _assign_connectivity_rows(
    model: NSModel,
    fabric_names: Set[str],
    segment_names: Set[str],
    ap_names: Optional[Set[str]] = None,
) -> None:
    """Assign each campus device a tier row by connectivity distance from the
    set of fabric devices, so the fabric is the TOP tier and devices descend
    downward by hop distance, with an inferred segment node between its
    members. This matches the overlay diagram's Y-axis direction (its
    collapsed fabric cloud anchors the TOP of the non-fabric underlay chain,
    with distance increasing downward) — the two diagrams share one convention.

    Generic (no hardcoded names). Algorithm:
      * Build the undirected underlay graph from ``model.l1_links`` restricted to
        campus devices (CDP + inferred links + the shared-segment node).
      * Run a lexicographic Dijkstra from the fabric set keyed on
        ``(segments_crossed, hops)`` — the primary cost counts how many inferred
        shared-segment nodes lie on the shortest path; ties break on hop count.
      * A fabric device gets tier 0; a non-fabric real device gets
        ``segments_crossed * 2 + 1`` (so a chain of real devices that reaches the
        fabric WITHOUT crossing a segment collapses to a single tier just below
        the fabric); an inferred segment node gets ``segments_crossed * 2`` (it
        sits between the members above it and the members it pushes below it).
      * The tier IS the row directly (fabric tier 0 -> row 0, the TOP; the
        most-distant devices get the highest row number, at the BOTTOM).
      * RULE 12 override: a Wireless AP (``ap_names``) is electrically a PEER
        of a shared segment's other members (e.g. a WLC also on that segment),
        but its CAPWAP path to the WLC logically TRANSITS the segment, so it is
        forced into a row STRICTLY BELOW (a higher row number than) every
        segment it attaches to — matching RULE 12's "AP below its connected
        access switch" placement, even though the segment stands in for a
        flat shared LAN rather than a literal switch.
    External-waypoint devices keep their existing row.
    """
    import heapq

    campus = {n for n, d in model.devices.items() if d.area == _AREA_CAMPUS}
    if not campus:
        return
    fabric = {n for n in fabric_names if n in campus}
    if not fabric:
        # No fabric anchor (e.g. no SD-Access). Leave role-based rows untouched.
        return

    adj: Dict[str, Set[str]] = {n: set() for n in campus}
    for lk in model.l1_links:
        a, b = lk.a_device, lk.b_device
        if a in campus and b in campus and a != b:
            adj[a].add(b)
            adj[b].add(a)

    INF = (float("inf"), float("inf"))
    best: Dict[str, Tuple[float, float]] = {n: INF for n in campus}
    pq: List[Tuple[float, float, str]] = []
    for f in sorted(fabric):
        best[f] = (0, 0)
        heapq.heappush(pq, (0, 0, f))
    while pq:
        sc, hp, u = heapq.heappop(pq)
        if (sc, hp) > best[u]:
            continue
        for v in adj[u]:
            add_seg = 1 if v in segment_names else 0
            cand = (sc + add_seg, hp + 1)
            if cand < best[v]:
                best[v] = cand
                heapq.heappush(pq, (cand[0], cand[1], v))

    # Raw tier per campus device (fabric at 0, growing upward).
    tier: Dict[str, int] = {}
    for n in campus:
        sc = best[n][0]
        if sc == float("inf"):
            # Disconnected from the fabric — park it on the top band.
            sc = 0
            tier[n] = None  # type: ignore[assignment]
            continue
        if n in fabric:
            tier[n] = 0
        elif n in segment_names:
            tier[n] = int(sc) * 2
        else:
            tier[n] = int(sc) * 2 + 1

    placed = [t for t in tier.values() if t is not None]
    max_tier = max(placed) if placed else 0
    # Disconnected nodes: park them one band above everything else.
    for n, t in tier.items():
        if t is None:
            tier[n] = max_tier + 1
    max_tier = max(tier.values())

    # Fabric (tier 0) is the TOP row; distance from it grows DOWNWARD. This
    # matches the overlay diagram's convention, where the collapsed fabric
    # cloud anchors the TOP of the non-fabric underlay chain and increasing
    # underlay distance renders further down the page — the two diagrams now
    # share the same Y-axis direction (fabric-relative "closer" = higher up).
    for n in campus:
        model.devices[n].row = tier[n]

    # RULE 12 override: push each AP into a row STRICTLY GREATER than every
    # segment it attaches to AND every OTHER real device sharing that segment
    # (e.g. a WLC, which the generic formula puts in the SAME tier as the AP,
    # since both are equally-distant peers on the same flat shared subnet).
    # Comparing against the segment's OTHER members too (not just the segment
    # itself) guarantees the AP lands in a genuinely NEW row of its own,
    # regardless of which numeric direction "closer to fabric" happens to sort
    # (this override is therefore correct under either Y-axis orientation).
    for ap in (ap_names or ()):
        if ap not in model.devices:
            continue
        peer_rows: List[int] = []
        for seg in adj.get(ap, set()):
            if seg not in segment_names or seg not in model.devices:
                continue
            peer_rows.append(model.devices[seg].row)
            for peer in adj.get(seg, set()):
                if peer != ap and peer in model.devices and peer not in (ap_names or ()):
                    peer_rows.append(model.devices[peer].row)
        if not peer_rows:
            continue
        required = max(peer_rows) + 1
        if model.devices[ap].row != required:
            model.devices[ap].row = required


def _add_wireless_dummy_clients(
    idx, model, mappings, counts, unique_fn,
    ap_ids: Set[str],
    id_to_dev: Dict[str, str],
    ap_svi_by_name: Dict[str, str],
) -> None:
    """Dummy wireless client per SSID per AP — mirrors the overlay mapper's /
    meraki_converter's convention: each configured SSID becomes a synthetic
    wireless interface on the AP ('<SSID name> N'), connected DOWN to a gray
    dummy PC standing in for that SSID's (unobserved) wireless clients. The
    AP's wireless interface carries the resolved VLAN's L2 segment; the dummy
    PC side carries none (RULE 11.5). Placed ONE row below its AP (must run
    AFTER RULE 12 row assignment) so it renders beneath the AP, exactly like
    the overlay diagram places its per-SSID dummy clients below the AP.
    """
    if not idx.ssids:
        return
    ap_names = sorted({id_to_dev[i] for i in ap_ids if i in id_to_dev})
    if not ap_names:
        return

    dummy_seq = 0
    for ap_name in ap_names:
        ap_dev = model.devices.get(ap_name)
        if ap_dev is None:
            continue
        svi = ap_svi_by_name.get(ap_name) or "Vlan 1"
        m = re.search(r"\d+", svi)
        ap_vlan = m.group(0) if m else "1"
        pc_row = ap_dev.row + 1
        for i, ssid_obj in enumerate(idx.ssids, start=1):
            ssid_name = (ssid_obj.get("ssid") or "").strip()
            if not ssid_name:
                continue
            dummy_seq += 1
            pc_name = unique_fn(f"PC_{_AREA_CAMPUS}_0_{dummy_seq}")
            st_pc = sm.map_logical(
                pc_name, "endpoint", model=f"Dummy wireless client (SSID {ssid_name})")
            mappings.append(st_pc)
            model.devices[pc_name] = NSDevice(
                name=pc_name, area=_AREA_CAMPUS, row=pc_row, stencil=st_pc,
                is_endpoint=True, default_color=(200, 200, 200),
                routing_attribute=f"Dummy wireless client (SSID {ssid_name} on {ap_name})",
            )
            ap_port = f"{ssid_name} {i}"
            pc_port = "Wlan 0"
            model.l1_links.append(NSL1Link(ap_name, ap_port, pc_name, pc_port))
            model.l2_segments_phys.append(NSL2Segment(ap_name, ap_port, [f"Vlan{ap_vlan}"]))
            counts["dummy_wireless_client"] = counts.get("dummy_wireless_client", 0) + 1


_NAME_ROLE_KW = [
    ("borderrouter", ("border-router", "borderrouter")),
    ("border", ("border", "-bn", "bordernode")),
    ("core", ("core", "-cr")),
    ("distribution", ("dist", "distribution", "-da", "-dn")),
    ("access", ("access", "edge", "-ac", "-en")),
    ("router", ("router", "-rtr", "wan", "-isr", "-asr")),
    ("wlc", ("wlc", "9800", "controller")),
    ("ap", ("-ap", "accesspoint", "ap-")),
]


def _infer_role_from_name(dev: Dict[str, Any]) -> str:
    """Best-effort role from the hostname when role/family are unknown."""
    nm = (dev.get("hostname") or dev.get("name") or "").lower()
    for role_key, kws in _NAME_ROLE_KW:
        if any(k in nm for k in kws):
            return role_key
    return "access"
