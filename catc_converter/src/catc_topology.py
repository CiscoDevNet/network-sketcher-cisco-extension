# Copyright 2026 Cisco Systems, Inc. and its affiliates
# SPDX-License-Identifier: Apache-2.0

"""Shared helpers over Catalyst Center device / interface / topology objects.

Used by the underlay (device naming + role + cabling) and overlay (fabric-site /
VN / anycast-gateway parsing) mappers. All helpers degrade gracefully to empty /
default results when a field is missing, so a partial export still works.
"""
from __future__ import annotations

import ipaddress
import re
from collections import defaultdict
from typing import Any, Dict, List, Optional, Set, Tuple

from .ns_model import (
    NSIPAssignment, NSL2Segment, NSVirtualPort, normalise_port_name,
)


# ---------------------------------------------------------------------------
# Devices
# ---------------------------------------------------------------------------

def device_hostname(dev: Dict[str, Any]) -> str:
    """Best-available hostname for a Catalyst Center managed device."""
    return (dev.get("hostname") or dev.get("name") or dev.get("managementIpAddress")
            or "").strip()


def device_display_name(dev: Dict[str, Any], naming: str = "hostname",
                        strip_suffix: str = "") -> str:
    """Device display name per the ``device_naming`` policy.

    ``hostname`` -> hostname (falls back to mgmt IP); ``ip`` -> mgmt IP;
    ``hostname_ip`` -> '<hostname>-<ip>'. ``strip_suffix`` (e.g.
    ``.dcloud.cisco.com``) is removed from the hostname when present.
    """
    nm = device_hostname(dev)
    if strip_suffix and nm.endswith(strip_suffix):
        nm = nm[: -len(strip_suffix)]
    ip = (dev.get("managementIpAddress") or "").strip()
    if naming == "ip" and ip:
        return ip
    if naming == "hostname_ip" and nm and ip:
        return f"{nm}-{ip}"
    return nm or ip or "device"


def device_role(dev: Dict[str, Any]) -> str:
    """Catalyst Center campus role (e.g. 'CORE', 'ACCESS', 'BORDER ROUTER')."""
    return (dev.get("role") or "").strip()


def device_family(dev: Dict[str, Any]) -> str:
    """Catalyst Center device family (e.g. 'Switches and Hubs', 'Routers')."""
    return (dev.get("family") or "").strip()


def device_platform(dev: Dict[str, Any]) -> str:
    """Platform model hint (``platformId`` e.g. 'C9300-48U', or ``series``)."""
    return (dev.get("platformId") or dev.get("series") or "").strip()


def device_os_type(dev: Dict[str, Any]) -> str:
    return (dev.get("softwareType") or "").strip()


def device_os_version(dev: Dict[str, Any]) -> str:
    return (dev.get("softwareVersion") or "").strip()


def device_serial(dev: Dict[str, Any]) -> str:
    return (dev.get("serialNumber") or "").strip()


def device_mgmt_ip(dev: Dict[str, Any]) -> str:
    return (dev.get("managementIpAddress") or "").strip()


# ---------------------------------------------------------------------------
# Topology nodes / links
# ---------------------------------------------------------------------------

def resolve_node_to_device(idx, node_id: str) -> Optional[Dict[str, Any]]:
    """Resolve a physical-topology node id to a managed device.

    Catalyst Center topology node ids EQUAL network-device ids for managed
    devices, so a direct lookup in ``device_by_id`` suffices.
    """
    if not node_id:
        return None
    return idx.device_by_id.get(str(node_id))


def link_port_names(link: Dict[str, Any]) -> tuple:
    """Return (normalised startPortName, normalised endPortName) for a topo link."""
    a = normalise_port_name(link.get("startPortName") or "")
    b = normalise_port_name(link.get("endPortName") or "")
    return a, b


# ---------------------------------------------------------------------------
# Interfaces — speed/duplex/media → NS port_info
# ---------------------------------------------------------------------------

def _speed_to_ns(speed: Any) -> Optional[str]:
    """Catalyst Center interface ``speed`` (Kbps string) -> NS speed label."""
    try:
        kbps = int(float(speed))
    except (TypeError, ValueError):
        return None
    if kbps <= 0:
        return None
    mbps = kbps // 1000
    return {1000: "1Gbps", 10000: "10Gbps", 25000: "25Gbps", 40000: "40Gbps",
            100000: "100Gbps", 400000: "400Gbps", 100: "100Mbps", 10: "10Mbps"}.get(
        mbps, f"{mbps // 1000}Gbps" if mbps >= 1000 else f"{mbps}Mbps")


def _media_for(speed_label: Optional[str], media_type: str) -> str:
    if media_type:
        return media_type
    if speed_label in ("10Gbps", "25Gbps", "40Gbps", "100Gbps", "400Gbps"):
        return "10GBASE-SR"
    return "1000BASE-T"


def interface_portinfo(interfaces: List[Dict[str, Any]]) -> Dict[str, tuple]:
    """{normalised portName: (speed, duplex, media)} for physical ports.

    Catalyst Center interface ``speed`` is in Kbps; ``duplex`` is 'FullDuplex'
    /'HalfDuplex'/'AutoNegotiate'; ``mediaType`` may be empty on virtual ports.
    Falls back to a 1Gbps default when the speed is missing/unparseable.
    """
    out: Dict[str, tuple] = {}
    for itf in interfaces or []:
        name = itf.get("portName") or itf.get("name")
        if not name:
            continue
        port = normalise_port_name(name)
        speed = _speed_to_ns(itf.get("speed"))
        raw_dup = (itf.get("duplex") or "").lower()
        if "full" in raw_dup:
            duplex = "Full"
        elif "half" in raw_dup:
            duplex = "Half"
        else:
            duplex = "Full"
        media = _media_for(speed, (itf.get("mediaType") or "").strip())
        out[port] = (speed or "1Gbps", duplex, media)
    return out


# ---------------------------------------------------------------------------
# Shared L3 / subnet inference (used by BOTH the underlay and overlay mappers)
# ---------------------------------------------------------------------------

def iface_network(ip: str, mask: str):
    """(ip, mask) -> ipaddress.IPv4Network (host bits masked), or None.

    ``mask`` may be a dotted netmask ('255.255.192.0') or a prefix length
    ('18'). Returns None on any parse failure or for a /32 (loopback) address.
    """
    ip = (ip or "").strip()
    mask = (mask or "").strip()
    if not ip or not mask:
        return None
    try:
        if "." in mask:
            iface = ipaddress.ip_interface(f"{ip}/{mask}")
        else:
            iface = ipaddress.ip_interface(f"{ip}/{int(mask)}")
    except (ValueError, TypeError):
        return None
    if iface.version != 4:
        return None
    if iface.network.prefixlen >= 32:
        return None
    return iface.network


def infer_subnet_adjacencies(
    idx,
    id_to_dev: Dict[str, str],
    linked_pairs: Optional[set] = None,
    mgmt_fallback_ids: Optional[Set[str]] = None,
) -> Tuple[List[Tuple[str, str, str, str]], List[Tuple[str, Dict[str, str]]]]:
    """Derive L3/subnet adjacencies between MANAGED devices from shared subnets.

    Catalyst Center's physicalTopology only reports CDP/LLDP adjacencies between
    managed devices, so devices reachable only across UNMANAGED switching appear
    isolated. This helper supplements the observed cabling by grouping routed
    interfaces (ipv4Address + ipv4Mask) by network and reporting, per subnet:

      * a subnet with exactly 2 DISTINCT managed devices  -> ONE direct pair,
      * a subnet with 3+ DISTINCT managed devices         -> ONE shared segment.

    Excluded (same rules the underlay has always used):
      * /32 loopbacks,
      * the OOB management subnet (a subnet whose member interfaces are ALL the
        devices' own managementIpAddress),
      * anycast SVIs (the SAME host IP present on 2+ distinct devices).

    ``id_to_dev`` maps network-device id -> NS device name (the set of devices to
    consider). ``linked_pairs`` (optional) is a set of ``frozenset({devA, devB})``
    already directly CDP-linked; pairs in it are NOT reported as direct links
    (they would duplicate a real cable). It does NOT suppress shared segments.

    ``mgmt_fallback_ids`` (optional network-device ids, e.g. Unified APs) — a
    device with NO real interface data at all (Catalyst Center's per-interface
    fetch can 404 for some device types, e.g. lightweight APs) is otherwise
    invisible to this inference. For such a device, if its OWN
    ``managementIpAddress`` falls inside an ALREADY-ESTABLISHED subnet (derived
    from OTHER devices' real interfaces), it is added to that subnet as a
    synthetic ``GigabitEthernet 0`` member — a well-founded inference (its own
    authoritative mgmt IP genuinely places it on that L2/L3 domain), not a wild
    guess. A device with real interface data is never affected by this fallback.

    Returns ``(direct_pairs, segments)`` where:
      * ``direct_pairs`` = [(devA, portA, devB, portB), ...] (sorted device pair),
      * ``segments``     = [(cidr, {device: port, ...}), ...] (3+ members each),
    both deterministically ordered by network address / prefix.
    """
    linked_pairs = linked_pairs or set()

    # Each managed device's own management IP (to spot OOB mgmt interfaces).
    mgmt_ip_by_dev: Dict[str, Any] = {}
    for did, devname in id_to_dev.items():
        dev = idx.device_by_id.get(str(did)) or {}
        ip = device_mgmt_ip(dev)
        if ip:
            try:
                mgmt_ip_by_dev[devname] = ipaddress.ip_address(ip.strip())
            except (ValueError, TypeError):
                pass

    # network -> list of (device_name, ns_port, host_ip_obj, is_mgmt_iface)
    by_net: Dict[Any, List[Tuple[str, str, Any, bool]]] = defaultdict(list)
    for did, devname in id_to_dev.items():
        for itf in idx.interfaces.get(str(did), []):
            net = iface_network(itf.get("ipv4Address"), itf.get("ipv4Mask"))
            if net is None:
                continue
            try:
                host = ipaddress.ip_address(str(itf.get("ipv4Address")).strip())
            except (ValueError, TypeError):
                continue
            port = normalise_port_name(itf.get("portName") or itf.get("name") or "")
            if not port:
                continue
            is_mgmt = mgmt_ip_by_dev.get(devname) == host
            by_net[net].append((devname, port, host, is_mgmt))

    # ----- mgmt-IP fallback for devices with NO real interface data ------
    # Snapshot the ESTABLISHED networks (from real interface data only) before
    # adding any synthetic member, so a fallback device can only join a subnet
    # already proven to exist from other devices' real data.
    established_nets = list(by_net.keys())
    for did in (mgmt_fallback_ids or ()):
        devname = id_to_dev.get(str(did))
        if not devname:
            continue
        if idx.interfaces.get(str(did)):
            continue  # has real interface data — not a fallback candidate
        ip = device_mgmt_ip(idx.device_by_id.get(str(did)) or {})
        if not ip:
            continue
        try:
            host = ipaddress.ip_address(ip.strip())
        except (ValueError, TypeError):
            continue
        for net in established_nets:
            if host in net:
                by_net[net].append((devname, "GigabitEthernet 0", host, True))
                break

    direct_pairs: List[Tuple[str, str, str, str]] = []
    segments: List[Tuple[str, Dict[str, str]]] = []
    for net in sorted(by_net, key=lambda n: (int(n.network_address), n.prefixlen)):
        members = by_net[net]

        # Exclude the OOB management subnet (all members are mgmt interfaces).
        if len(members) >= 2 and all(is_mgmt for *_x, is_mgmt in members):
            continue

        # Exclude anycast: the SAME host IP present on 2+ distinct devices.
        host_to_devs: Dict[Any, set] = defaultdict(set)
        for dn, _p, host, _m in members:
            host_to_devs[host].add(dn)
        if any(len(devs) >= 2 for devs in host_to_devs.values()):
            continue

        # One representative (device, port) per distinct device on this subnet.
        per_dev: Dict[str, str] = {}
        for dn, port, _host, _m in members:
            per_dev.setdefault(dn, port)
        devs = sorted(per_dev)
        if len(devs) < 2:
            continue

        cidr = str(net)
        if len(devs) == 2:
            da, db = devs
            if frozenset({da, db}) in linked_pairs:
                continue   # already a CDP cable — don't duplicate
            direct_pairs.append((da, per_dev[da], db, per_dev[db]))
        else:
            segments.append((cidr, dict(per_dev)))
    return direct_pairs, segments


# ---------------------------------------------------------------------------
# RULE 12 — Wireless AP (FlexConnect) modeling, shared by the underlay and
# overlay mappers so an AP gets IDENTICAL treatment in both diagrams.
# ---------------------------------------------------------------------------

_VLAN_PORT_RE = re.compile(r"^vlan\s*(\d+)$", re.IGNORECASE)


def vlan_tag_for_segment(member_ports: Dict[str, str]) -> Optional[str]:
    """Best-effort VLAN number for a shared segment, from a REAL member port
    literally named ``Vlan <N>`` (e.g. a WLC's ``Vlan 11`` management SVI).
    Returns None if no member port matches (the AP then falls back to VLAN 1).
    """
    for port in member_ports.values():
        m = _VLAN_PORT_RE.match(port.strip())
        if m:
            return m.group(1)
    return None


def apply_ap_flexconnect(
    model,
    ap_name: str,
    uplink_port: str,
    vlan_tag: Optional[str],
    mgmt_ip: Optional[str],
    prefixlen: int,
    counts: Optional[Dict[str, int]] = None,
) -> str:
    """Model a Wireless AP per RULE 12 (FlexConnect mode, the mandatory default).

    APs are the ONLY endpoint-class device that uses an SVI: the AP's physical
    uplink port carries an L2 segment for the Management VLAN (NO IP on the
    physical port), and a Management SVI ('Vlan <N>') carries the AP's REAL
    management IP. Returns the SVI port name ('Vlan <N>') so callers can attach
    per-SSID client-VLAN trunking / dummy wireless clients to the same AP.
    """
    vnum = vlan_tag or "1"
    svi = f"Vlan {vnum}"
    model.l2_segments_phys.append(NSL2Segment(ap_name, uplink_port, [f"Vlan{vnum}"]))
    model.virtual_ports.append(NSVirtualPort(ap_name, svi))
    model.l2_segments_svi.append(NSL2Segment(ap_name, svi, [f"Vlan{vnum}"]))
    if mgmt_ip:
        model.ip_assignments.append(NSIPAssignment(ap_name, svi, [f"{mgmt_ip}/{prefixlen}"]))
        if counts is not None:
            counts["ip_assignments"] = counts.get("ip_assignments", 0) + 1
    return svi
