# Copyright 2026 Cisco Systems, Inc. and its affiliates
# SPDX-License-Identifier: Apache-2.0

"""Build the *abstract SD-Access logical overlay* NSModel from a Cisco Catalyst
Center export — "Plan B": an ACI-tenant-style logical view.

The ACI-tenant analog in SD-Access is the VIRTUAL NETWORK (VN / VRF), NOT the
physical fabric switches. So this overlay does NOT draw the real fabric
switches green; instead it draws an ABSTRACT logical view where each VN is the
purple logical entity (the SD-Access analog of an ACI tenant) and the whole
fabric is collapsed into a single underlay cloud. Per fabric site (one NS area,
named after the site-hierarchy leaf, e.g. "CML-Lab"):

    SDA Fabric cloud   → ONE collapsed "SDA Fabric (<area>)" cloud representing
                         the entire fabric underlay (border / control-plane /
                         edge switches). Drawn as a gray fabric/cloud stencil
                         (NOT purple); its member fabric devices + SD-Access
                         roles are listed in Attribute-D. Anchored at the bottom.
    VN gateway         → PURPLE logical VN device "VN:<area>-<VN>" per Layer3
                         Virtual Network bound to the site (the ACI-tenant
                         analog). Linked to the Fabric cloud. The system VNs
                         INFRA_VN / DEFAULT_VN are skipped unless an anycast
                         gateway references them.
    Anycast segment    → the SD-Access anycast gateway is a SINGLE object
                         (VLAN(L2) + SVI(L3) on the fabric), so it is represented
                         DIRECTLY by the VN device's SVI — NOT by a standalone
                         segment device. Each anycast gateway places on its VN
                         device an SVI ('Vlan <id>') + L2 segment + VRF rename,
                         the REAL anycast gateway CIDR (an IP on the SVI, taken
                         from a fabric edge's 'Vlan<id>' interface), and folds
                         ipPoolName + vlanName + trafficType + subnet into the
                         VN's Attribute-D. No separate "<area>-Vlan <id>" device
                         is drawn anymore.
    Border hand-off    → ONE PURPLE external "Border:<area> L3 hand-off" cloud
                         linked to every VN device (the VN exits via the border),
                         drawn only when a BORDER fabric device exists.
    Client / host      → optional PURPLE endpoint "EP:<vn>_<n>_<seq>" attached DIRECTLY
                         to its VN device (on the VN's anycast SVI port, or a
                         dedicated access port), with its IP (respecting
                         max_endpoints_per_vn).

The ACI-tenant analogy is deliberate: the VN is the logical tenant-analog (the
purple entity), and the fabric switches are collapsed into a single underlay
cloud — they are infrastructure, not the policy entity. SD-Access has no
per-pair contracts (reachability is open within a VN, segmented by SGTs / group
policy), so no flows are emitted unless the operator opts in (see
:mod:`flow_list_builder`).
"""
from __future__ import annotations

import ipaddress
import re
from collections import defaultdict
from typing import Any, Dict, List, Optional, Set, Tuple

from . import catc_stencil_mapper as sm
from . import catc_topology as topo
from .catc_stencil_mapper import StencilMapping, normalise_role
from .ns_model import (
    NSDevice, NSIPAssignment, NSL1Link, NSL2Segment, NSModel, NSVirtualPort,
    normalise_port_name,
)

# Every overlay device is a *logical* construct (VN gateway / anycast segment /
# border hand-off / client), so they are coloured light purple — the SD-Access
# analog of an ACI tenant. The single collapsed fabric underlay cloud is the one
# exception: it stays a gray fabric cloud (it is infrastructure, not policy).
OVERLAY_COLOR = (221, 204, 255)  # light purple / lavender

# Tier rows inside a fabric-site area (Plan B abstract overlay).
_ROW_BORDER = 0      # border / external L3 hand-off cloud(s)
_ROW_VN = 1          # purple logical VN devices (the tenant analog; carry SVIs)
_ROW_FABRIC = 2      # the single collapsed fabric underlay cloud (anchor)
_ROW_CLIENT = 3      # clients beneath their VN device
_ROW_UNDERLAY = 4    # real non-fabric underlay devices attached to the fabric

# System VNs that are skipped unless an anycast gateway references them.
_SYSTEM_VNS = {"INFRA_VN", "DEFAULT_VN"}


def _dummy(n: int) -> str:
    """Pseudo port name for a logical link (no real interface corresponds)."""
    return f"Dummy {n}"


def _host_cidr(ip: str) -> str:
    ip = (ip or "").strip()
    if "/" in ip:
        return ip
    return f"{ip}/128" if ":" in ip else f"{ip}/32"


def sanitize(name: str) -> str:
    """Keep NS-safe characters (no quotes / brackets). Collapse whitespace.

    ':' is allowed so device names can carry a type prefix (``VN:...``,
    ``Border:...``, ``EP:...``) — mirrors aci_converter's convention."""
    if name is None:
        return ""
    keep = [ch for ch in str(name).strip() if ch.isalnum() or ch in " -_.:+/"]
    return re.sub(r"\s+", " ", "".join(keep)).strip()


def _site_leaf_name(idx, fabric_site: Dict[str, Any]) -> str:
    """Resolve a fabric site to a readable area name (site-hierarchy leaf)."""
    site_id = str(fabric_site.get("siteId") or "")
    site = idx.site_by_id().get(site_id, {})
    hierarchy = (site.get("siteNameHierarchy") or site.get("name")
                 or fabric_site.get("siteNameHierarchy") or "")
    if hierarchy:
        leaf = str(hierarchy).split("/")[-1].strip()
        if leaf:
            return leaf
    return site.get("name") or site_id or "fabric"


def _sda_roles(fd: Dict[str, Any]) -> List[str]:
    return [str(r).upper() for r in (fd.get("deviceRoles") or [])]


def _anycast_cidr_for_vlan(idx, fabric_ndids: Set[str], vid: int) -> Optional[str]:
    """Find the REAL anycast gateway CIDR for VLAN ``vid``.

    The anycast gateway IP is identical on every fabric edge, so scan the
    fabric devices' interfaces for one named ``Vlan<vid>`` carrying
    ipv4Address + ipv4Mask and return '<ip>/<prefixlen>' (e.g. 10.100.10.1/24).
    Returns None if no such SVI is found (e.g. interfaces were not fetched).
    """
    from .catc_physical_mapper import iface_cidr

    target = normalise_port_name(f"Vlan{vid}")
    for ndid in fabric_ndids:
        for itf in idx.interfaces.get(str(ndid), []):
            port = normalise_port_name(itf.get("portName") or itf.get("name") or "")
            if port != target:
                continue
            cidr = iface_cidr(itf.get("ipv4Address"), itf.get("ipv4Mask"))
            if cidr:
                return cidr
    return None


def _role_label(roles: List[str]) -> str:
    """Compact SD-Access role label, e.g. 'BORDER/CP', 'EDGE'."""
    bits: List[str] = []
    if any("BORDER" in r for r in roles):
        bits.append("BORDER")
    if any("CONTROL_PLANE" in r for r in roles):
        bits.append("CP")
    if any("EDGE" in r for r in roles):
        bits.append("EDGE")
    return "/".join(bits) if bits else (roles[0] if roles else "")


def build_logical_model(
    idx,
    cfg: Optional[Dict[str, Any]] = None,
    layout: str = "tier",
) -> Tuple[NSModel, Dict[Tuple[str, str], str], Dict[str, Any]]:
    """Return (NSModel, vn_name_by_key, info) for the Plan B abstract overlay.

    ``vn_name_by_key`` maps an ``(area, virtualNetworkName)`` tuple to that VN's
    purple logical device name, so :mod:`flow_list_builder` can resolve flow
    endpoints if it ever emits any (today it does not).
    """
    cfg = cfg or {}
    naming = str(cfg.get("device_naming", "hostname") or "hostname")
    strip_suffix = str(cfg.get("strip_domain_suffix", "") or "")
    include_endpoints = bool(cfg.get("include_endpoints", True))
    max_ep = int(cfg.get("max_endpoints_per_vn", 20) or 20)

    model = NSModel()
    mappings: List[StencilMapping] = []
    vn_name_by_key: Dict[Tuple[str, str], str] = {}
    seen_names: set = set()
    counts = {
        # Keys consumed by convert.py _run_overlay (kept present):
        "fabric_site": 0, "vn": 0, "anycast_gw": 0, "segment": 0,
        "external": 0, "host": 0,
        "control_plane": 0, "border": 0, "edge": 0, "edge_svi": 0, "fusion": 0,
        # Plan B additions:
        "fabric_collapsed": 0, "vn_device": 0, "segment_device": 0,
        "border_cloud": 0,
        # The anycast gateway is now the VN device's SVI (no standalone segment
        # device); 'segment'/'segment_device' stay present but are always 0.
        "anycast_ip": 0,
        # Real non-fabric underlay devices attached to the fabric:
        "underlay_device": 0, "underlay_segment": 0, "underlay_link": 0,
        "underlay_ip": 0,
        # RULE 12 FlexConnect APs + their per-SSID dummy wireless clients:
        "underlay_ap": 0, "dummy_wireless_client": 0,
        # Real observed clients: "host" = total underlying clients folded in;
        # "host_device" = the number of MERGED PC boxes actually drawn (one
        # per broadcast domain, i.e. per VN group).
        "host_device": 0,
    }
    caveats: List[str] = []

    def _unique(name: str) -> str:
        name = sanitize(name) or "device"
        base, i = name, 2
        while name in seen_names:
            name = f"{base}_{i}"
            i += 1
        seen_names.add(name)
        return name

    # ----- fabric sites → areas; map fabricId -> area name ---------------
    area_by_fabric_id: Dict[str, str] = {}
    for fs in idx.fabric_sites:
        fid = str(fs.get("id") or "")
        if not fid:
            continue
        area_by_fabric_id[fid] = _site_leaf_name(idx, fs)
        counts["fabric_site"] += 1

    all_areas = sorted(set(area_by_fabric_id.values()))
    fallback_area = all_areas[0] if all_areas else "fabric"

    # ----- 1) collapsed fabric devices per area --------------------------
    # Gather each fabric device's resolved hostname + SD-Access role(s) per area;
    # detect whether the site has a BORDER node.
    fabric_members: Dict[str, List[str]] = defaultdict(list)   # area -> ["C9Kv-3=BORDER/CP", ...]
    has_border: Dict[str, bool] = defaultdict(bool)
    fabric_ndid_area: Dict[str, str] = {}   # fabric network-device id -> area
    for fd in idx.fabric_devices:
        fid = str(fd.get("fabricId") or "")
        ndid = str(fd.get("networkDeviceId") or "")
        if not ndid:
            continue
        area = area_by_fabric_id.get(fid, fallback_area)
        fabric_ndid_area[ndid] = area
        dev = idx.device_by_id.get(ndid)
        roles = _sda_roles(fd)
        host = (topo.device_display_name(dev, naming, strip_suffix)
                if dev else ndid)
        label = _role_label(roles)
        fabric_members[area].append(f"{host}={label}" if label else host)
        for r in roles:
            if "CONTROL_PLANE" in r:
                counts["control_plane"] += 1
            if "BORDER" in r:
                counts["border"] += 1
                has_border[area] = True
            if "EDGE" in r:
                counts["edge"] += 1

    # Areas that actually have fabric devices (skip empty sites for layout).
    active_areas = sorted(set(fabric_members) | set(area_by_fabric_id.values()))

    # The single collapsed fabric underlay cloud per area (anchor, bottom row).
    fabric_cloud_by_area: Dict[str, str] = {}
    for area in active_areas:
        members = fabric_members.get(area, [])
        name = _unique(f"SDA Fabric ({area})")
        st = sm.map_logical(
            name, "external",
            model="SD-Access fabric underlay (collapsed)")
        mappings.append(st)
        attr = "fabric: " + ", ".join(members) if members else "fabric underlay"
        model.devices[name] = NSDevice(
            name=name, area=area, row=_ROW_FABRIC, stencil=st, is_endpoint=False,
            routing_attribute=attr,   # NOT purple -> gray fabric/cloud stencil
        )
        fabric_cloud_by_area[area] = name
        counts["fabric_collapsed"] += 1

    # ----- 2) which VNs to draw ------------------------------------------
    # A VN is drawn if any anycast gateway references it (the VNs that matter);
    # system VNs (INFRA_VN / DEFAULT_VN) with no anycast gateway are skipped.
    gw_by_area_vn: Dict[str, List[Dict[str, Any]]] = defaultdict(list)  # area -> [anycastGateway]
    vns_with_gw: Dict[str, Set[str]] = defaultdict(set)                 # area -> {VN names}
    for gw in idx.anycast_gateways:
        fid = str(gw.get("fabricId") or "")
        area = area_by_fabric_id.get(fid, fallback_area)
        vn = (gw.get("virtualNetworkName") or "").strip()
        gw_by_area_vn[area].append(gw)
        if vn:
            vns_with_gw[area].add(vn)

    # Bind each Layer3 VN to the site(s) it lists in fabricIds; fall back to
    # every active area when a VN carries no fabricIds (system VNs are global).
    vn_bindings: List[Tuple[str, str]] = []   # (area, VN name)
    seen_binding: Set[Tuple[str, str]] = set()
    for v in idx.l3_vns:
        vn = (v.get("virtualNetworkName") or "").strip()
        if not vn:
            continue
        fabric_ids = [str(f) for f in (v.get("fabricIds") or [])]
        areas = ([area_by_fabric_id.get(f, fallback_area) for f in fabric_ids]
                 if fabric_ids else list(active_areas))
        for area in areas:
            # Skip the system VNs unless an anycast gateway references them.
            if vn in _SYSTEM_VNS and vn not in vns_with_gw.get(area, set()):
                continue
            key = (area, vn)
            if key not in seen_binding:
                seen_binding.add(key)
                vn_bindings.append(key)

    # Also include any VN that an anycast gateway references but that is not in
    # the layer3VirtualNetworks list (defensive — keeps segments parented).
    for area, vns in vns_with_gw.items():
        for vn in vns:
            key = (area, vn)
            if key not in seen_binding:
                seen_binding.add(key)
                vn_bindings.append(key)

    # ----- 3) purple VN devices, linked to the fabric cloud --------------
    vn_dev_by_key: Dict[Tuple[str, str], str] = {}
    vn_devs_by_area: Dict[str, List[str]] = defaultdict(list)
    fab_port: Dict[str, int] = defaultdict(int)   # fabric cloud -> downlink port
    for (area, vn) in sorted(vn_bindings):
        name = _unique(f"VN:{area}-{vn}")
        st = sm.map_logical(name, "gateway",
                            model=f"SD-Access Virtual Network (VRF {vn})")
        mappings.append(st)
        model.devices[name] = NSDevice(
            name=name, area=area, row=_ROW_VN, stencil=st, is_endpoint=False,
            default_color=OVERLAY_COLOR,
            routing_attribute=f"VRF {vn}",
        )
        vn_dev_by_key[(area, vn)] = name
        vn_name_by_key[(area, vn)] = name
        vn_devs_by_area[area].append(name)
        counts["vn_device"] += 1
        # Link the VN to its collapsed fabric underlay cloud.
        cloud = fabric_cloud_by_area.get(area)
        if cloud:
            fab_port[cloud] += 1
            model.l1_links.append(
                NSL1Link(name, _dummy(0), cloud, _dummy(fab_port[cloud])))
    counts["vn"] = len({vn for (_a, vn) in vn_dev_by_key})

    # ----- 4) anycast gateway = the VN device's SVI (no separate segment) --
    # In SD-Access the anycast gateway is a SINGLE object — VLAN(L2) + SVI(L3)
    # on the fabric — so it is represented directly by the VN device's SVI, NOT
    # by a standalone segment device. Each anycast gateway therefore adds, on
    # its VN device: an SVI virtual port 'Vlan <id>', the L2 segment for that
    # VLAN, the VRF rename, the REAL anycast gateway CIDR (NSIPAssignment), and
    # folds its pool / vlanName / trafficType + subnet into the VN's Attribute-D.
    fabric_ndids: Set[str] = set(fabric_ndid_area)
    vn_port: Dict[str, int] = defaultdict(int)    # VN device -> downlink port
    vn_extra_attr: Dict[str, List[str]] = defaultdict(list)  # VN device -> attr bits
    vlans_on_vn: Dict[Tuple[str, str], List[int]] = defaultdict(list)  # (area,vn)->[vid]
    for area, gws in gw_by_area_vn.items():
        for gw in gws:
            counts["anycast_gw"] += 1
            vn = (gw.get("virtualNetworkName") or "").strip()
            vid = gw.get("vlanId")
            try:
                vid = int(vid) if vid is not None and str(vid).strip() else None
            except (ValueError, TypeError):
                vid = None
            if vid is None:
                continue
            pool = (gw.get("ipPoolName") or "").strip()
            vlan_name = (gw.get("vlanName") or "").strip()
            traffic = (gw.get("trafficType") or "").strip()

            vn_dev = vn_dev_by_key.get((area, vn))
            if vn_dev is None:
                # No VN device (e.g. orphan gateway) — skip; cannot parent it.
                continue

            # Put the SVI + L2 segment + VRF rename on the VN device so the NS
            # L2/L3 layers show the VLAN and VRF.
            svi = f"Vlan {vid}"
            model.virtual_ports.append(NSVirtualPort(device=vn_dev, port=svi, vlan_id=vid))
            model.l2_segments_svi.append(NSL2Segment(device=vn_dev, port=svi, vlans=[f"Vlan{vid}"]))
            if vn:
                model.vrf_renames.append((vn_dev, svi, vn))
            counts["edge_svi"] += 1
            vlans_on_vn[(area, vn)].append(vid)

            # The REAL anycast gateway CIDR (identical on every edge) goes on the
            # SVI as an IP assignment — more accurate than the pool name.
            cidr = _anycast_cidr_for_vlan(idx, fabric_ndids, vid)
            if cidr:
                model.ip_assignments.append(
                    NSIPAssignment(device=vn_dev, port=svi, cidrs=[cidr]))
                counts["anycast_ip"] += 1

            # Fold the (removed) segment's label into the VN device's Attribute-D
            # so no information is lost (pool / vlanName / trafficType + subnet).
            bits = (([f"pool {pool}"] if pool else [])
                    + ([f"vlanName {vlan_name}"] if vlan_name else [])
                    + ([f"traffic {traffic}"] if traffic else [])
                    + [f"VLAN {vid}"]
                    + ([f"subnet {cidr}"] if cidr else []))
            vn_extra_attr[vn_dev].append("[" + " / ".join(bits) + "]")

    # Fold each VN device's anycast-segment label(s) into its Attribute-D so the
    # standalone segment device can be removed with no loss of information.
    for vn_dev, extra in vn_extra_attr.items():
        dev = model.devices.get(vn_dev)
        if dev is None:
            continue
        base = dev.routing_attribute
        dev.routing_attribute = (base + " | " if base else "") + " ; ".join(extra)

    # ----- 5) ONE purple border / L3 hand-off cloud per area -------------
    border_cloud_by_area: Dict[str, str] = {}
    for area in active_areas:
        if not has_border.get(area):
            continue
        if not vn_devs_by_area.get(area):
            continue
        name = _unique(f"Border:{area} L3 hand-off")
        st = sm.map_logical(name, "external",
                            model="SD-Access border / L3 hand-off")
        mappings.append(st)
        model.devices[name] = NSDevice(
            name=name, area=area, row=_ROW_BORDER, stencil=st, is_endpoint=False,
            default_color=OVERLAY_COLOR,
            routing_attribute="border / external L3 hand-off",
        )
        border_cloud_by_area[area] = name
        counts["border_cloud"] += 1
        counts["external"] += 1
        # The VN exits via the border hand-off: link to each VN device.
        bport = 0
        for vn_dev in vn_devs_by_area[area]:
            bport += 1
            model.l1_links.append(
                NSL1Link(name, _dummy(bport), vn_dev, _dummy(_vn_uplink_port(vn_port, vn_dev))))

    # ----- 5b) REAL non-fabric underlay devices attached to the fabric ---
    # Add the real devices that are NOT fabric devices but attach to the fabric
    # via the underlay (physicalTopology links OR the SAME subnet-inference rules
    # the underlay mapper uses), so the overlay also shows fabric<->non-fabric
    # underlay connectivity. A non-fabric device that links to a FABRIC node is
    # connected to the collapsed "SDA Fabric (<area>)" cloud; non-fabric<->non-
    # fabric and non-fabric<->segment links are drawn directly. These real links
    # use REAL port names and stay visually distinct from the purple logical
    # links (real stencils/colours; the inferred segment node light-gray).
    ap_ids: Set[str] = set()
    for dev in idx.devices:
        fam_key = normalise_role(topo.device_family(dev))
        if sm.FAMILY_TO_ROLE.get(fam_key) == "ap":
            did = dev.get("id")
            if did:
                ap_ids.add(str(did))

    underlay_rows_by_area: Dict[str, List[List[str]]] = {}
    underlay_id_to_name: Dict[str, str] = {}   # ndid -> created NS name
    ap_svi_by_name: Dict[str, str] = {}        # created AP NS name -> its Mgmt SVI port
    _add_underlay_attachments(
        idx, model, mappings, counts, _unique,
        fabric_ndid_area, fabric_cloud_by_area, fallback_area,
        naming, strip_suffix, underlay_rows_by_area, fab_port,
        underlay_id_to_name, ap_ids=ap_ids, ap_svi_by_name=ap_svi_by_name)

    # CHANGE #1 (overlay): the REAL non-fabric underlay devices get IPs on their
    # real link ports (and loopbacks), exactly like the underlay diagram — e.g.
    # C8000v-1 GigabitEthernet 2/3, C8000v-2 GigabitEthernet 2.11, C9800 Vlan 11.
    from . import catc_physical_mapper as pm
    pm.add_interface_ips(
        idx, model, underlay_id_to_name, counts,
        only_devices=set(underlay_id_to_name.values()))
    counts["underlay_ip"] = counts.get("ip_assignments", 0)

    # ----- 5c) per-SSID dummy wireless clients (meraki_converter convention) -
    # Mirrors meraki_converter's per-SSID dummy-client pattern EXACTLY: each
    # configured SSID becomes a synthetic wireless interface on the AP, linked
    # DOWN to a gray dummy PC standing in for that SSID's (unobserved) wireless
    # clients. Unconditional (like meraki) — independent of include_endpoints,
    # which only gates REAL observed clients.
    _add_wireless_dummy_clients(
        idx, model, mappings, counts, _unique,
        underlay_id_to_name, ap_ids, ap_svi_by_name, underlay_rows_by_area)

    # ----- 6) clients → ONE merged PC per broadcast domain (VN) -----------
    # The standalone segment device is gone (the anycast gateway is the VN's
    # SVI), so clients attach DIRECTLY to their VN device — on a dedicated
    # 'Dummy N' access port. ALL clients sharing the SAME broadcast domain
    # (every client under a given VN is placed on that VN's first/only
    # resolvable VLAN, so "same VN" == "same broadcast domain" here) are
    # represented by ONE combined PC device — mirroring the dummy wireless
    # client's "one box stands for many hosts on that segment" convention —
    # instead of one near-duplicate host box per client. This also avoids the
    # multi-child fan-out artifact NS's column placement otherwise produces
    # when several rows attach to progressively-numbered ports on one parent.
    clients_under: Dict[str, Dict[str, List[str]]] = defaultdict(lambda: defaultdict(list))
    if include_endpoints and idx.clients:
        groups: Dict[Tuple[str, str], List[Dict[str, Any]]] = defaultdict(list)
        # A simple round-robin VN assignment when a client gives no VN hint.
        for client in idx.clients:
            hierarchy = (client.get("siteHierarchy") or "").strip()
            area = None
            if hierarchy:
                leaf = hierarchy.split("/")[-1].strip()
                if leaf in all_areas:
                    area = leaf
            if area is None:
                area = fallback_area

            vn = (client.get("vnid") or client.get("virtualNetwork")
                  or client.get("vn") or "").strip()
            # Pick the VN: explicit hint, else the first VN in the area.
            vn_dev = None
            if vn and (area, vn) in vn_dev_by_key:
                vn_dev = vn_dev_by_key[(area, vn)]
            else:
                area_vns = vn_devs_by_area.get(area)
                if area_vns:
                    vn_dev = area_vns[0]
                    # recover the VN name for this device
                    for (a, vname), dn in vn_dev_by_key.items():
                        if dn == vn_dev:
                            vn = vname
                            break
            if vn_dev is None:
                continue
            groups[(area, vn)].append(client)

        ep_seq: Dict[str, int] = defaultdict(int)
        for (area, vn), members in sorted(groups.items()):
            vn_dev = vn_dev_by_key.get((area, vn))
            if vn_dev is None:
                continue
            capped = members[:max_ep]

            ep_seq[vn] += 1
            host = _unique(f"EP:{vn}_{len(members)}_{ep_seq[vn]}")
            st_h = sm.map_logical(
                host, "endpoint",
                model=f"{len(members)} client(s) (same broadcast domain)")
            mappings.append(st_h)
            hattr: List[str] = [f"{len(members)} client(s)"]
            ips: List[str] = []
            for c in capped:
                bits: List[str] = []
                if c.get("macAddress"):
                    bits.append(str(c["macAddress"]))
                if c.get("ipv4Address"):
                    bits.append(str(c["ipv4Address"]))
                if c.get("type"):
                    bits.append(str(c["type"]))
                if bits:
                    hattr.append("/".join(bits))
                if c.get("ipv4Address"):
                    ips.append(_host_cidr(str(c["ipv4Address"])))
            if len(members) > len(capped):
                hattr.append(f"... +{len(members) - len(capped)} more")
            model.devices[host] = NSDevice(
                name=host, area=area, row=_ROW_CLIENT, stencil=st_h,
                is_endpoint=True, default_color=OVERLAY_COLOR,
                routing_attribute=" | ".join(hattr),
            )
            # Attach the merged host directly to its VN device on a dedicated
            # 'Dummy N' access port (the VN's SVI/anycast port itself is a
            # single L3 gateway interface and cannot double as an L1 link
            # endpoint). Tag that access port with the VN's first VLAN so the
            # L2 layer still shows the broadcast domain's membership.
            vlans = vlans_on_vn.get((area, vn))
            access_port = _dummy(_vn_uplink_port(vn_port, vn_dev))
            model.l1_links.append(NSL1Link(host, _dummy(0), vn_dev, access_port))
            if vlans:
                model.l2_segments_phys.append(
                    NSL2Segment(device=vn_dev, port=access_port, vlans=[f"Vlan{vlans[0]}"]))
            if ips:
                # Every member's real IP lands on the SAME merged access port
                # (NSIPAssignment.cidrs accepts a list) — one box, many hosts.
                model.ip_assignments.append(
                    NSIPAssignment(device=host, port=_dummy(0), cidrs=ips))
            clients_under[area][vn_dev].append(host)
            counts["host"] += len(members)
            counts["host_device"] = counts.get("host_device", 0) + 1

    # ----- 7) placement: grid per fabric site ----------------------------
    ordered = [a for a in active_areas
               if fabric_cloud_by_area.get(a) or vn_devs_by_area.get(a)]
    model.areas = [ordered] if ordered else []
    model.area_to_devices = {
        area: _overlay_grid(
            border_cloud_by_area.get(area),
            vn_devs_by_area.get(area, []),
            area,
            fabric_cloud_by_area.get(area),
            clients_under.get(area, {}),
            underlay_rows_by_area.get(area, []),
        )
        for area in ordered
    }

    # ----- caveats --------------------------------------------------------
    if counts["fabric_site"] == 0:
        caveats.append(
            "No SD-Access fabric sites found in the export (fabricSites was "
            "empty). The overlay may be empty. Configure SD-Access fabric sites "
            "in Catalyst Center (or select a deployment that has them) and re-fetch."
        )
    caveats.append(
        "MODEL — this is the 'Plan B' ABSTRACT logical overlay: the ACI-tenant "
        "analog in SD-Access is the VIRTUAL NETWORK (VN / VRF), NOT the physical "
        "switches. Each VN is drawn as ONE purple logical device (the tenant "
        "analog); the entire fabric (border / control-plane / edge switches) is "
        "collapsed into a SINGLE gray underlay cloud 'SDA Fabric (<area>)'. The "
        "real fabric switches are deliberately NOT drawn as separate nodes."
    )
    caveats.append(
        "OBSERVED — each SD-Access fabric site maps to ONE NS area (named after "
        "the site-hierarchy leaf). The collapsed fabric cloud's Attribute-D lists "
        "its member fabric devices and their SD-Access roles (e.g. "
        "'C9Kv-3=BORDER/CP, C9Kv-1=EDGE, C9Kv-2=EDGE')."
    )
    caveats.append(
        "MODEL — only VNs that MATTER are drawn: a VN appears when an anycast "
        "gateway references it (or it is an explicitly user-bound VN). The system "
        "VNs INFRA_VN / DEFAULT_VN are skipped unless they carry an anycast "
        "gateway."
    )
    caveats.append(
        "MODEL — in SD-Access the anycast gateway is a SINGLE object (the "
        "VLAN(L2) + SVI(L3) on the fabric), so it is represented by the VN "
        "device's own SVI, NOT by a separate '<area>-Vlan <id>' segment device "
        "(those standalone segment devices are no longer drawn). Each anycast "
        "gateway places on its VN device an SVI ('Vlan <id>'), an L2 segment "
        "and a VRF rename (so the NS L2/L3 layers show the VLAN and VRF), plus "
        "the REAL anycast gateway CIDR as an IP on the SVI (taken from a fabric "
        "edge's 'Vlan<id>' interface — identical on every edge, e.g. "
        "10.100.10.1/24). The ipPoolName + vlanName + trafficType + subnet are "
        "folded into the VN device's Attribute-D so no information is lost."
    )
    if counts["border_cloud"]:
        caveats.append(
            "MODEL — a single purple 'Border:<area> L3 hand-off' cloud is drawn "
            "per site that has a BORDER fabric device and linked to every VN "
            "device (each VN exits the fabric via the border hand-off)."
        )
    caveats.append(
        "INFERRED — the purple logical L1 links in this overlay (VN<->fabric "
        "cloud, border<->VN, host<->VN) are SYNTHETIC; they represent VN "
        "membership and the border hand-off, NOT real cabling, and use the "
        "pseudo label 'Dummy N'. (The underlay mode draws the real cabling.) The "
        "REAL non-fabric underlay attachments, by contrast, use real port names."
    )
    if counts.get("anycast_ip") or counts.get("underlay_ip"):
        caveats.append(
            f"OBSERVED — anycast gateway SVIs carry their REAL gateway CIDR "
            f"({counts.get('anycast_ip', 0)} IP(s), e.g. 10.100.10.1/24, read "
            "from a fabric edge's 'Vlan<id>' interface), and the REAL non-fabric "
            "underlay devices carry their interface IPs on their real link ports "
            "and loopbacks (same rule as the underlay diagram). The OOB "
            "management IP is skipped (it is shown in Attribute-D)."
        )
    if counts["host"]:
        caveats.append(
            f"OBSERVED/MODEL — {counts['host']} client(s) from the clients API are "
            f"folded into {counts.get('host_device', 0)} merged PC device(s) — ONE "
            "per broadcast domain (same VN), mirroring the dummy wireless "
            "client's 'one box for many hosts' convention. Each member's real "
            "MAC/IP is listed in Attribute-D and all member IPs land on the "
            "SAME access port (Stencil Type PC). max_endpoints_per_vn now caps "
            "how many individual clients are LISTED in that merged box's "
            "attributes (not how many boxes are drawn — that is always one)."
        )
    else:
        caveats.append(
            "NOTE — no clients were present in the export (the clients API may "
            "have returned empty, or endpoints were not fetched). Re-fetch with "
            "endpoints to draw real hosts beneath each segment."
        )
    if counts.get("underlay_device") or counts.get("underlay_segment"):
        caveats.append(
            f"OBSERVED/INFERRED — {counts['underlay_device']} REAL non-fabric "
            f"device(s) and {counts['underlay_segment']} inferred shared-segment "
            "node(s) that attach to the fabric via the UNDERLAY are also drawn "
            "(with their real stencils/colours; the segment node light-gray), "
            "below the collapsed fabric cloud. A non-fabric device cabled to a "
            "FABRIC node links to the 'SDA Fabric (<area>)' cloud (the fabric is "
            "collapsed); non-fabric<->non-fabric and non-fabric<->segment links "
            "are drawn directly with their REAL/inferred underlay port names. "
            "These underlay links are distinct from the purple logical VN links "
            "(which use 'Dummy' ports). The non-fabric attachments use the SAME "
            "subnet-inference rules as the underlay diagram."
        )
    caveats.append(
        "NOTE — SD-Access provides open any-to-any reachability WITHIN a VN; "
        "inter-group segmentation is SGT / group-policy based, not per-pair "
        "contracts, so no flow rows are emitted unless emit_sgt_flows is set "
        "(SGT flow emission is not implemented yet)."
    )

    info = {"mappings": mappings, "counts": counts, "caveats": caveats}
    return model, vn_name_by_key, info


def _vn_uplink_port(vn_port: Dict[str, int], vn_dev: str) -> int:
    """Reserve and return the next free 'Dummy' port on a VN device for an
    uplink (border) connection, so it never collides with a segment downlink."""
    vn_port[vn_dev] += 1
    return vn_port[vn_dev]


def _map_real_device(idx, ndid: str, name: str):
    """Build a real-device StencilMapping for a managed device (overlay side).

    Mirrors the underlay mapper's role/family resolution so a non-fabric device
    drawn in the overlay gets the SAME stencil/model/OS it has in the underlay.
    """
    from . import catc_physical_mapper as pm  # local import (avoid import cycle)

    dev = idx.device_by_id.get(str(ndid)) or {}
    raw_role = topo.device_role(dev)
    family = topo.device_family(dev)
    role_key = normalise_role(raw_role)
    fam_key = normalise_role(family)
    known = role_key in sm.ROLE_TABLE or fam_key in sm.FAMILY_TO_ROLE
    inferred = not known
    if fam_key in sm.FAMILY_TO_ROLE:
        role_key = sm.FAMILY_TO_ROLE[fam_key]
    elif inferred:
        role_key = pm._infer_role_from_name(dev)
    st = sm.map_device(
        name=name, role=raw_role or role_key, family=family,
        serial=topo.device_serial(dev),
        model_hint=topo.device_platform(dev),
        os_type=topo.device_os_type(dev),
        os_version=topo.device_os_version(dev),
        inferred=inferred,
    )
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
    return st, " | ".join(attr_bits)


def _add_underlay_attachments(
    idx, model, mappings, counts, unique_fn,
    fabric_ndid_area: Dict[str, str],
    fabric_cloud_by_area: Dict[str, str],
    fallback_area: str,
    naming: str, strip_suffix: str,
    underlay_rows_by_area: Dict[str, List[List[str]]],
    cloud_port: Dict[str, int],
    out_id_to_name: Optional[Dict[str, str]] = None,
    ap_ids: Optional[Set[str]] = None,
    ap_svi_by_name: Optional[Dict[str, str]] = None,
) -> None:
    """Draw the REAL non-fabric devices that attach to the fabric via the underlay.

    Builds the underlay graph (physicalTopology links + subnet inference via the
    shared :func:`catc_topology.infer_subnet_adjacencies` helper), finds every
    non-fabric device connected — directly or transitively — to a fabric device,
    and draws those devices (and any inferred shared-segment node between them)
    with their real stencils/colours below the collapsed fabric cloud. Links are
    re-pointed to the fabric cloud whenever they would touch a (collapsed) fabric
    device; non-fabric<->non-fabric and non-fabric<->segment links are direct.
    """
    from collections import deque
    from . import catc_physical_mapper as pm

    fabric_ndids: Set[str] = set(fabric_ndid_area)
    if not fabric_ndids:
        return

    # All managed device ids -> NS device name (the inference helper key space).
    id_to_name: Dict[str, str] = {}
    name_to_ndid: Dict[str, str] = {}
    for dev in idx.devices:
        did = str(dev.get("id") or "")
        if not did:
            continue
        nm = topo.device_display_name(dev, naming, strip_suffix)
        id_to_name[did] = nm
        name_to_ndid[nm] = did

    fabric_names = {id_to_name[d] for d in fabric_ndids if d in id_to_name}

    # ----- underlay graph over NS device names ---------------------------
    # 1) observed cabling (physicalTopology links between managed devices),
    #    carrying real port names.
    edges: Dict[frozenset, Tuple[str, str]] = {}     # {a,b} -> (port_a_for_a, port_b_for_b) by name
    port_of: Dict[Tuple[str, str], str] = {}         # (deva, devb) -> port on deva
    adj: Dict[str, Set[str]] = defaultdict(set)

    def _add_edge(a: str, pa: str, b: str, pb: str) -> None:
        if not a or not b or a == b:
            return
        key = frozenset({a, b})
        if key in edges:
            return
        edges[key] = (a, b)
        port_of[(a, b)] = pa
        port_of[(b, a)] = pb
        adj[a].add(b)
        adj[b].add(a)

    for link in idx.topo_links:
        s = str(link.get("source") or "")
        t = str(link.get("target") or "")
        a = id_to_name.get(s)
        b = id_to_name.get(t)
        if not a or not b:
            continue
        pa, pb = topo.link_port_names(link)
        _add_edge(a, pa or "GigabitEthernet 0/0", b, pb or "GigabitEthernet 0/0")

    linked_pairs = {frozenset(v) for v in edges.values()}

    # 2) subnet inference (same rules as the underlay): direct pairs + segments.
    direct_pairs, segments = topo.infer_subnet_adjacencies(
        idx, id_to_name, linked_pairs, mgmt_fallback_ids=ap_ids)
    for (da, pa, db, pb) in direct_pairs:
        _add_edge(da, pa, db, pb)

    # Segment nodes: connect to each member by name (real member port / dummy seg).
    seg_member_ports: Dict[str, Dict[str, str]] = {}   # seg name -> {member: real port}
    seg_name_for_cidr: Dict[str, str] = {}
    for cidr, per_dev in segments:
        seg = unique_fn(cidr)
        seg_name_for_cidr[cidr] = seg
        seg_member_ports[seg] = dict(per_dev)
        for member in per_dev:
            adj[member].add(seg)
            adj[seg].add(member)

    seg_nodes = set(seg_member_ports)

    # ----- which devices/segments attach to the fabric (transitively) ----
    reachable: Set[str] = set()
    q = deque(fabric_names)
    seen = set(fabric_names)
    while q:
        u = q.popleft()
        for v in adj[u]:
            if v not in seen:
                seen.add(v)
                reachable.add(v)
                q.append(v)
    # Non-fabric REAL devices + segments attached to the fabric.
    nonfabric_real = {n for n in reachable if n in name_to_ndid and n not in fabric_names}
    attached_segs = {n for n in reachable if n in seg_nodes}
    if not nonfabric_real and not attached_segs:
        return

    # All these devices share the (single) fabric area for this export. Resolve a
    # per-device area: the area of the fabric cloud they ultimately attach to.
    # With one fabric site this is simply that site's area.
    areas = set(fabric_ndid_area.values())
    area = sorted(areas)[0] if areas else fallback_area
    fabric_cloud = fabric_cloud_by_area.get(area)

    # ----- create the real non-fabric NSDevices --------------------------
    seg_ns_name: Dict[str, str] = {}     # internal seg name -> created NS name
    created_id_to_name: Dict[str, str] = {}  # ndid -> created NS name (real devs)
    for nm in sorted(nonfabric_real):
        ndid = name_to_ndid[nm]
        ns_name = unique_fn(nm)
        st, attr = _map_real_device(idx, ndid, ns_name)
        mappings.append(st)
        model.devices[ns_name] = NSDevice(
            name=ns_name, area=area, row=_ROW_UNDERLAY, stencil=st,
            is_endpoint=False, routing_attribute=attr,
        )
        # remember the rename so links/rows use the created (unique) NS name
        name_to_ndid[ns_name] = ndid
        seg_ns_name[nm] = ns_name  # reuse map for name resolution below
        created_id_to_name[ndid] = ns_name
        if out_id_to_name is not None:
            out_id_to_name[ndid] = ns_name
        counts["underlay_device"] += 1

    cidr_for_seg = {s: c for c, s in seg_name_for_cidr.items()}
    for seg in sorted(attached_segs):
        cidr = cidr_for_seg.get(seg, seg)
        # 'seg' was already uniquified by unique_fn when the segment was built.
        st = pm.segment_stencil(seg, cidr)
        mappings.append(st)
        model.devices[seg] = NSDevice(
            name=seg, area=area, row=_ROW_UNDERLAY, stencil=st,
            is_endpoint=False, default_color=pm._SEG_COLOR,
            routing_attribute=f"L3 shared segment {cidr} (inferred)",
        )
        seg_ns_name[seg] = seg
        counts["underlay_segment"] += 1

    def _resolved(n: str) -> str:
        """Map an internal graph name to its created NS device name."""
        return seg_ns_name.get(n, n)

    drawn = nonfabric_real | attached_segs

    # ----- draw the underlay links among the attached set ----------------
    used_ports: Set[Tuple[str, str]] = set()
    seg_port: Dict[str, int] = defaultdict(int)
    link_seen: Set[frozenset] = set()

    def _emit(a: str, pa: str, b: str, pb: str) -> None:
        key = frozenset({(a, pa), (b, pb)})
        if key in link_seen:
            return
        link_seen.add(key)
        model.l1_links.append(NSL1Link(a, pa, b, pb))
        counts["underlay_link"] += 1

    ap_ids = ap_ids or set()
    ap_svi_done: Set[str] = set()

    def _maybe_apply_ap_flexconnect(side_name: str, side_port: str, seg_side: str) -> None:
        """If ``side_name`` is a Unified AP attached to segment ``seg_side``,
        apply RULE 12 (Management SVI + physical-port L2 segment) exactly once."""
        ndid = name_to_ndid.get(side_name)
        ap_name = _resolved(side_name)
        if not ndid or ndid not in ap_ids or seg_side not in seg_nodes or ap_name in ap_svi_done:
            return
        ap_svi_done.add(ap_name)
        vlan_tag = topo.vlan_tag_for_segment(seg_member_ports.get(seg_side, {}))
        cidr = cidr_for_seg.get(seg_side, seg_side)
        try:
            prefixlen = ipaddress.ip_network(cidr).prefixlen
        except ValueError:
            prefixlen = 24
        mgmt_ip = topo.device_mgmt_ip(idx.device_by_id.get(ndid) or {})
        svi = topo.apply_ap_flexconnect(model, ap_name, side_port, vlan_tag, mgmt_ip,
                                        prefixlen, counts)
        counts["underlay_ap"] = counts.get("underlay_ap", 0) + 1
        if ap_svi_by_name is not None:
            ap_svi_by_name[ap_name] = svi

    for nm in sorted(drawn):
        for other in sorted(adj[nm]):
            if nm >= other and other in drawn:
                continue  # draw each non-fabric pair once (skip the mirror)
            # Case 1: neighbour is a FABRIC device -> link to the fabric cloud.
            if other in fabric_names:
                if not fabric_cloud:
                    continue
                # only emit once per (device -> cloud), using a real port if any.
                pa = port_of.get((nm, other), "GigabitEthernet 0/0")
                if nm in seg_nodes:
                    continue  # a segment never cables straight to fabric here
                if (nm, pa) in used_ports:
                    continue
                used_ports.add((nm, pa))
                cloud_port[fabric_cloud] += 1
                _emit(_resolved(nm), pa, fabric_cloud, _dummy(cloud_port[fabric_cloud]))
                continue
            # Case 2: neighbour is another attached non-fabric/segment node.
            if other not in drawn:
                continue
            # Resolve real ports; a segment side uses a 'Dummy N' pseudo-port.
            if nm in seg_nodes:
                seg_port[nm] += 1
                pa = _dummy(seg_port[nm])
            else:
                pa = port_of.get((nm, other)) or seg_member_ports.get(other, {}).get(nm) \
                     or "GigabitEthernet 0/0"
            if other in seg_nodes:
                seg_port[other] += 1
                pb = _dummy(seg_port[other])
            else:
                pb = port_of.get((other, nm)) or seg_member_ports.get(nm, {}).get(other) \
                     or "GigabitEthernet 0/0"
            _emit(_resolved(nm), pa, _resolved(other), pb)
            # RULE 12 (FlexConnect default): apply once the AP's real uplink
            # port + attached segment are both known.
            _maybe_apply_ap_flexconnect(nm, pa, other)
            _maybe_apply_ap_flexconnect(other, pb, nm)

    # ----- layout rows: closest-to-fabric just below the fabric anchor ---
    # Distance over the attached underlay subgraph (fabric = 0), with segment
    # nodes adding a tier between their members (same idea as the underlay).
    ap_internal_names = {nm for nm in nonfabric_real
                         if name_to_ndid.get(nm) in (ap_ids or set())}
    rows = _underlay_overlay_rows(
        drawn, fabric_names, adj, seg_nodes, _resolved, ap_internal_names)
    underlay_rows_by_area.setdefault(area, []).extend(rows)


def _add_wireless_dummy_clients(
    idx, model, mappings, counts, unique_fn,
    underlay_id_to_name: Dict[str, str],
    ap_ids: Set[str],
    ap_svi_by_name: Dict[str, str],
    underlay_rows_by_area: Dict[str, List[List[str]]],
) -> None:
    """Dummy wireless client per SSID per AP — the SAME convention
    ``meraki_converter`` uses: each configured SSID becomes a synthetic
    wireless interface on the AP ('<SSID name> 1'), connected DOWN to a gray
    dummy PC standing in for that SSID's (unobserved) wireless clients. The
    AP's wireless interface carries the resolved VLAN's L2 segment; the dummy
    PC side carries NONE (RULE 11.5 — endpoints have no SVI / L2 segment).
    Unconditional (independent of include_endpoints, exactly like meraki).
    """
    if not idx.ssids:
        return

    # SSID name -> its wireless profile's interfaceName ('management' or a
    # named dynamic interface). Catalyst Center ties a non-fabric SSID to an
    # interface, not to a VLAN number directly; 'management' means the SAME
    # VLAN as the AP's own management SVI (the only case we can resolve
    # precisely without a dedicated dynamic-interface-to-VLAN fetch).
    iface_by_ssid: Dict[str, str] = {}
    for prof in idx.wireless_profiles:
        for sd in ((prof.get("profileDetails") or {}).get("ssidDetails") or []):
            nm = (sd.get("name") or "").strip()
            if nm:
                iface_by_ssid[nm] = (sd.get("interfaceName") or "").strip()

    ap_names = sorted({nm for ndid, nm in underlay_id_to_name.items() if ndid in ap_ids})
    if not ap_names:
        return

    non_mgmt_ssids: Set[str] = set()
    dummy_seq = 0
    rows_by_area: Dict[str, List[str]] = defaultdict(list)
    for ap_name in ap_names:
        dev = model.devices.get(ap_name)
        if dev is None:
            continue
        area = dev.area
        svi = ap_svi_by_name.get(ap_name) or "Vlan 1"
        m = re.search(r"\d+", svi)
        ap_vlan = m.group(0) if m else "1"
        for i, ssid_obj in enumerate(idx.ssids, start=1):
            ssid_name = (ssid_obj.get("ssid") or "").strip()
            if not ssid_name:
                continue
            iface_name = iface_by_ssid.get(ssid_name, "").strip().lower()
            if iface_name and iface_name != "management":
                non_mgmt_ssids.add(ssid_name)
            # 'management' (or unresolved) -> the AP's own management VLAN —
            # the one client-VLAN mapping resolvable from the API without a
            # dedicated dynamic-interface-to-VLAN fetch.
            vnum = ap_vlan

            dummy_seq += 1
            pc_name = unique_fn(f"PC_{area}_0_{dummy_seq}")
            st_pc = sm.map_logical(
                pc_name, "endpoint", model=f"Dummy wireless client (SSID {ssid_name})")
            mappings.append(st_pc)
            model.devices[pc_name] = NSDevice(
                name=pc_name, area=area, row=_ROW_UNDERLAY, stencil=st_pc,
                is_endpoint=True, default_color=(200, 200, 200),
                routing_attribute=f"Dummy wireless client (SSID {ssid_name} on {ap_name})",
            )
            ap_port = f"{ssid_name} {i}"
            pc_port = "Wlan 0"
            model.l1_links.append(NSL1Link(ap_name, ap_port, pc_name, pc_port))
            model.l2_segments_phys.append(NSL2Segment(ap_name, ap_port, [f"Vlan{vnum}"]))
            rows_by_area[area].append(pc_name)
            counts["dummy_wireless_client"] += 1

    for area, names in rows_by_area.items():
        underlay_rows_by_area.setdefault(area, []).append(sorted(names))


def _underlay_overlay_rows(
    drawn: Set[str],
    fabric_names: Set[str],
    adj: Dict[str, Set[str]],
    seg_nodes: Set[str],
    resolve,
    ap_names: Optional[Set[str]] = None,
) -> List[List[str]]:
    """Order the attached non-fabric nodes into rows by underlay distance from
    the fabric (closest first, i.e. just below the fabric cloud), with an
    inferred segment node placed between its members (same banding the underlay
    diagram uses). ``ap_names`` (internal/pre-unique names) are then pushed into
    a band STRICTLY deeper than every segment they attach to (RULE 12 — an AP's
    CAPWAP path to the WLC logically transits the shared segment, even though
    it started as a peer of the segment's other members, e.g. the WLC).
    Returns a list of rows (each a list of NS device names)."""
    import heapq

    nodes = set(drawn)
    INF = (float("inf"), float("inf"))
    best: Dict[str, Tuple[float, float]] = {n: INF for n in nodes}
    pq: List[Tuple[float, float, str]] = []
    # Seed from fabric: each attached node adjacent to fabric starts at distance 1.
    for n in sorted(nodes):
        if any(f in fabric_names for f in adj[n]):
            seg_step = 1 if n in seg_nodes else 0
            best[n] = (seg_step, 1)
            heapq.heappush(pq, (best[n][0], best[n][1], n))
    while pq:
        sc, hp, u = heapq.heappop(pq)
        if (sc, hp) > best[u]:
            continue
        for v in adj[u]:
            if v not in nodes:
                continue
            add_seg = 1 if v in seg_nodes else 0
            cand = (sc + add_seg, hp + 1)
            if cand < best[v]:
                best[v] = cand
                heapq.heappush(pq, (cand[0], cand[1], v))

    band: Dict[int, List[str]] = defaultdict(list)
    tier_of: Dict[str, int] = {}
    for n in nodes:
        sc, hp = best[n]
        if sc == float("inf"):
            tier = 999
        elif n in seg_nodes:
            tier = int(sc) * 2          # segment sits between its members
        else:
            tier = int(sc) * 2 - 1      # real device band
        tier_of[n] = tier
        band[tier].append(resolve(n))

    # RULE 12 override (see docstring): move each AP out of its generic band
    # into one strictly deeper than every segment it attaches to AND every
    # OTHER real node sharing that segment (e.g. a WLC, which the generic
    # formula puts in the SAME band as the AP — both are equally-distant peers
    # on the same flat shared subnet). Comparing against the segment's OTHER
    # members too (not just the segment itself) guarantees the AP lands in a
    # genuinely NEW band regardless of which numeric direction the banding
    # happens to grow in (so this is correct for either Y-axis orientation,
    # not dependent on a numeric coincidence for one specific topology).
    for ap in (ap_names or ()):
        if ap not in nodes:
            continue
        peer_tiers: List[int] = []
        for seg in adj[ap]:
            if seg not in seg_nodes or seg not in tier_of:
                continue
            peer_tiers.append(tier_of[seg])
            for peer in adj[seg]:
                if peer != ap and peer in tier_of and peer not in (ap_names or ()):
                    peer_tiers.append(tier_of[peer])
        if not peer_tiers:
            continue
        target = max(peer_tiers) + 1
        if target <= tier_of[ap]:
            continue
        name = resolve(ap)
        if name in band.get(tier_of[ap], []):
            band[tier_of[ap]].remove(name)
        band[target].append(name)
        tier_of[ap] = target

    return [sorted(band[t]) for t in sorted(band) if band[t]]


def _overlay_grid(
    border_cloud: Optional[str],
    vn_devs: List[str],
    area: str,
    fabric_cloud: Optional[str],
    clients_under: Dict[str, List[str]],
    underlay_rows: Optional[List[List[str]]] = None,
) -> List[List[str]]:
    """Lay one fabric site out (top→bottom), purple (logical) devices grouped in
    ONE block at the TOP, then the gray fabric anchor, then the REAL non-fabric
    underlay chain (green real devices + gray inferred nodes) at the BOTTOM:

      row 0 : border / external cloud(s)                      -- purple
      row 1 : purple VN devices (each VN carries its own anycast SVI — there is
              no longer a separate segment device row)         -- purple
      row 2+: real wired clients, stacked under their VN device's column
              (purple — an ACI-tenant-style logical endpoint of the VN, so it
              belongs in the purple block, ABOVE the fabric anchor)
      then  : the single collapsed fabric underlay cloud (anchor)  -- gray
      then  : the REAL non-fabric underlay devices attached to the fabric,
              laid out by their underlay distance from the fabric (closest just
              below the fabric anchor), with the inferred segment between
              members, and any per-SSID dummy wireless clients at the very
              bottom (below their AP).

    Each VN occupies one column; the fabric and border clouds occupy the
    leftmost cell of their own row.
    """
    cols = list(vn_devs)
    ncols = max(len(cols), 1)

    def _row(items: List[str]) -> List[str]:
        return list(items) + ["_AIR_"] * (ncols - len(items))

    grid: List[List[str]] = []
    if border_cloud:
        grid.append(_row([border_cloud]))
    if cols:
        grid.append(_row(cols))
    # Real wired clients: purple, so grouped with Border/VN ABOVE the fabric
    # anchor (they are the VN's logical endpoints, not fabric infrastructure).
    depth = max((len(clients_under.get(c, [])) for c in cols), default=0)
    for i in range(depth):
        grid.append([
            (clients_under.get(c, [])[i]
             if i < len(clients_under.get(c, [])) else "_AIR_")
            for c in cols
        ])
    # Fabric underlay cloud anchors the boundary between the purple logical
    # block (above) and the real/gray underlay chain (below).
    if fabric_cloud:
        grid.append(_row([fabric_cloud]))
    # Real non-fabric underlay devices + dummy wireless clients, below the
    # fabric anchor (their own rows, closest-to-fabric first).
    for urow in (underlay_rows or []):
        if urow:
            grid.append(list(urow))
    return grid
