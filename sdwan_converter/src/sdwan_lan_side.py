# Copyright 2026 Cisco Systems, Inc. and its affiliates
# SPDX-License-Identifier: Apache-2.0

"""Shared Service VPN LAN-side modelling, used by BOTH SD-WAN mappers.

The Overlay (``sdwan_logical_mapper``) and the Underlay
(``sdwan_physical_mapper``) draw the SAME LAN side below each SD-WAN Edge:
every REAL Service VPN PHYSICAL interface links DOWN to one synthetic
per-Edge ``Dummy_<n>`` switch, every Service VPN Loopback becomes an
UNCONNECTED virtual port (NS RULE 15's Loopback exception), and each of those
ports carries its VPN's VRF (``Corporate_vpn10``). The two diagrams are read
side by side, so they must agree on the LAN switch NAMES, on which Edge owns
which switch, and on the VRF labels -- hence one implementation here rather
than one per mapper.

Planning is deliberately separated from drawing:

  * :func:`plan_service_vpn_lan_side` resolves the whole plan from the vManage
    export alone, in ONE deterministic pass over the Edges in sorted display-name
    order. Both mappers call it with the same configuration and therefore get
    the identical ``Dummy_<n>`` numbering by construction, not by coincidence.
  * :func:`apply_edge_lan_side` draws one Edge's plan into an ``NSModel``, with
    the tier row, colour and name-uniquifier supplied by the calling mapper.

The one LAN-side-adjacent difference between the two diagrams stays in the
callers: the Overlay additionally draws each Edge's VPN membership UP into the
Service VPN waypoint clouds (synthetic ``Vpn <id>`` ports), whereas the
Underlay draws the VPN 0 transport clouds instead, with VPN 0 itself as the
``Transport_vpn0`` L3 instance on the Edges' physical transport ports.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

from . import sdwan_stencil_mapper as sm
from . import sdwan_topology as topo
from .sdwan_stencil_mapper import StencilMapping
from .ns_model import (
    NSDevice, NSIPAssignment, NSL1Link, NSModel, NSVirtualPort,
    normalise_port_name,
)

DEFAULT_LAN_DUMMY_NAME_FORMAT = "Dummy_{n}"

# (vpn-id, NS port name, CIDR or None) for one Service VPN interface.
PortFact = Tuple[int, str, Optional[str]]


def _dummy_port(n: int) -> str:
    """Pseudo port on the synthetic LAN switch -- the repo-wide convention for
    a port with no real interface behind it."""
    return f"Dummy {n}"


@dataclass
class EdgeLanSide:
    """One Edge's Service VPN LAN side, resolved once for both mappers.

    ``service_vpn_ids`` lists every Service VPN id reported for this Edge,
    while ``port_vpn_ids`` holds only those that survived the "one port = one
    VPN" de-duplication below; they differ only for an export with duplicate
    interface records.
    """
    device: Dict[str, Any]
    display_name: str
    service_vpn_ids: List[int]
    port_vpn_ids: Set[int]
    lan_ports: List[PortFact]
    loopbacks: List[PortFact]
    vrf_of: Dict[int, str]
    lan_switch_name: Optional[str]


@dataclass
class LanSidePlan:
    """The whole fabric's Service VPN LAN side, shared by both mappers."""
    edges: List[EdgeLanSide] = field(default_factory=list)
    vpn_ids: Set[int] = field(default_factory=set)
    vpn_names: Dict[str, str] = field(default_factory=dict)
    excluded: Set[int] = field(default_factory=set)
    skipped_edges: int = 0


@dataclass
class LanSideCounts:
    """What :func:`apply_edge_lan_side` added, for the caller's own counters."""
    lan_dummy: int = 0
    lan_links: int = 0
    loopback_ports: int = 0
    ip_assignments: int = 0
    vrf_renames: int = 0


def excluded_vpn_ids(cfg: Dict[str, Any]) -> Set[int]:
    """Normalise the ``excluded_service_vpns`` configuration value to ints.

    The value may legitimately hold ints (``512``) or strings (``"512"``);
    anything unparseable is ignored. VPN 0 and the other reserved ids are NOT
    the caller's responsibility -- ``topo.service_vpn_interfaces`` always
    excludes :data:`sdwan_topology.RESERVED_VPN_IDS` unconditionally.
    """
    raw = (cfg or {}).get("excluded_service_vpns")
    if raw is None:
        return set()
    if not isinstance(raw, (list, tuple, set)):
        raw = [raw]
    out: Set[int] = set()
    for item in raw:
        try:
            out.add(int(str(item).strip()))
        except (TypeError, ValueError):
            continue
    return out


def normalise_vpn_names(raw: Any) -> Dict[str, str]:
    """Coerce an export's ``vpn_names`` blob to ``{str(id): str(name)}``."""
    if not isinstance(raw, dict):
        return {}
    out: Dict[str, str] = {}
    for vpn_id, name in raw.items():
        if vpn_id is None or name is None:
            continue
        key, label = str(vpn_id).strip(), str(name).strip()
        if key and label:
            out[key] = label
    return out


def format_lan_switch_name(fmt: str, n: int) -> str:
    """Name the n-th synthetic per-Edge LAN switch, tolerating a bad format."""
    try:
        return fmt.format(n=n) or DEFAULT_LAN_DUMMY_NAME_FORMAT.format(n=n)
    except (IndexError, KeyError, ValueError):
        return DEFAULT_LAN_DUMMY_NAME_FORMAT.format(n=n)


def plan_service_vpn_lan_side(idx, cfg: Optional[Dict[str, Any]] = None) -> LanSidePlan:
    """Resolve every SD-WAN Edge's Service VPN LAN side from a vManage export.

    Edges with no Service VPN interface at all are left out of the plan (and
    counted in ``skipped_edges``); the remaining ones are returned in sorted
    display-name order, each with its physical LAN ports, its Loopbacks, its
    per-VPN VRF names and the name of the ``Dummy_<n>`` LAN switch its
    physical ports plug into. The switch counter advances only for an Edge
    that actually HAS a physical port, so an Edge whose only Service VPN
    interfaces are Loopbacks gets no switch and consumes no number.

    Both mappers call this with the same configuration, so the numbering, the
    Edge-to-switch assignment and the VRF labels are identical in the two
    diagrams by construction.
    """
    cfg = cfg or {}
    naming = str(cfg.get("device_naming", "hostname") or "hostname")
    vrf_fmt = str(cfg.get("vrf_name_format") or topo.DEFAULT_VRF_NAME_FORMAT)
    lan_switch_fmt = str(
        cfg.get("lan_dummy_name_format") or DEFAULT_LAN_DUMMY_NAME_FORMAT)
    plan = LanSidePlan(
        vpn_names=normalise_vpn_names(getattr(idx, "vpn_names", None)),
        excluded=excluded_vpn_ids(cfg),
    )

    records: List[Tuple[Dict[str, Any], List[Tuple[int, Dict[str, Any]]]]] = []
    edge_total = 0
    for dev in idx.devices:
        if topo.device_personality(dev) != "vedge":
            continue
        edge_total += 1
        sysip = str(dev.get("system-ip") or "")
        svc = topo.service_vpn_interfaces(idx.interfaces.get(sysip, []), plan.excluded)
        if not svc:
            continue
        records.append((dev, svc))
        plan.vpn_ids.update(vpn_id for vpn_id, _ in svc)
    plan.skipped_edges = edge_total - len(records)

    lan_switch_seq = 0
    for dev, svc in sorted(records, key=lambda r: topo.device_display_name(r[0], naming)):
        claimed_ports: Set[str] = set()
        lan_ports: List[PortFact] = []
        loopbacks: List[PortFact] = []
        port_vpn_ids: Set[int] = set()
        for vpn_id, itf in sorted(
            svc, key=lambda r: (r[0], normalise_port_name(r[1].get("ifname") or ""))
        ):
            ifname = itf.get("ifname") or itf.get("interface") or ""
            port = normalise_port_name(ifname)
            if not port or port in claimed_ports:
                continue   # a duplicate export record; one port = one VPN
            claimed_ports.add(port)
            port_vpn_ids.add(vpn_id)

            ip = (itf.get("ip-address") or "").strip()
            cidr = (topo.cidr_from_mask(ip, itf.get("ipv4-subnet-mask") or "")
                    or topo.cidr_from_mask(ip, "255.255.255.255"))
            bucket = loopbacks if topo.is_loopback_ifname(ifname) else lan_ports
            bucket.append((vpn_id, port, cidr))

        lan_switch_name: Optional[str] = None
        if lan_ports:
            lan_switch_seq += 1
            lan_switch_name = format_lan_switch_name(lan_switch_fmt, lan_switch_seq)

        plan.edges.append(EdgeLanSide(
            device=dev,
            display_name=topo.device_display_name(dev, naming),
            service_vpn_ids=sorted({vpn_id for vpn_id, _ in svc}),
            port_vpn_ids=port_vpn_ids,
            lan_ports=lan_ports,
            loopbacks=loopbacks,
            vrf_of={
                vpn_id: topo.vrf_label_for_vpn(vpn_id, plan.vpn_names, vrf_fmt)
                for vpn_id in port_vpn_ids
            },
            lan_switch_name=lan_switch_name,
        ))
    return plan


def apply_edge_lan_side(
    model: NSModel,
    mappings: List[StencilMapping],
    edge_name: str,
    area: str,
    lan: EdgeLanSide,
    unique_name: Callable[[str], str],
    row: int,
    color: Tuple[int, int, int],
    port_info: Optional[Tuple[str, str, str]] = None,
) -> LanSideCounts:
    """Draw one planned Edge's Service VPN LAN side into ``model``.

    Creates that Edge's ``Dummy_<n>`` LAN switch (skipped when the Edge has no
    physical Service VPN port -- there would be nothing to plug into it), L1-links
    every physical Service VPN port DOWN to it, assigns each of those ports its
    reported IPv4 address, writes the VPN's VRF onto BOTH ends of every LAN link,
    and adds each Service VPN Loopback as an unconnected virtual port with its
    own IP and VRF. No L2 segment is placed on any of these ports: Network
    Sketcher treats an L2-segmented port as an L2 switchport and drops it from
    the L3 interface table, which would make its IP and VRF unassignable.

    ``unique_name`` is the calling mapper's own device-name uniquifier, so the
    switch cannot collide with a device that mapper has already named.
    """
    out = LanSideCounts()

    if lan.lan_ports and lan.lan_switch_name:
        dummy_name = unique_name(lan.lan_switch_name)
        dummy_vrfs = sorted({lan.vrf_of[vpn_id] for vpn_id, _p, _c in lan.lan_ports})
        dst = sm.map_logical(
            dummy_name, "lan-dummy",
            model=f"Synthetic Service VPN LAN switch for {edge_name}",
        )
        mappings.append(dst)
        model.devices[dummy_name] = NSDevice(
            name=dummy_name, area=area, row=row, stencil=dst,
            is_endpoint=False, default_color=color, port_info=port_info,
            routing_attribute=(
                f"synthetic LAN aggregation for {edge_name} | "
                f"VRFs: {','.join(dummy_vrfs)}"
            ),
        )
        out.lan_dummy += 1

        for i, (vpn_id, port, cidr) in enumerate(lan.lan_ports, start=1):
            dport = _dummy_port(i)
            model.l1_links.append(NSL1Link(edge_name, port, dummy_name, dport))
            out.lan_links += 1
            if cidr:
                model.ip_assignments.append(
                    NSIPAssignment(device=edge_name, port=port, cidrs=[cidr]))
                out.ip_assignments += 1
            # Both ends share the VRF, so the synthetic switch is split by VRF
            # too rather than reading as one flat LAN. The Dummy side
            # deliberately gets no IP (it is not a real endpoint).
            model.vrf_renames.append((edge_name, port, lan.vrf_of[vpn_id]))
            model.vrf_renames.append((dummy_name, dport, lan.vrf_of[vpn_id]))
            out.vrf_renames += 2

    for vpn_id, port, cidr in lan.loopbacks:
        model.virtual_ports.append(
            NSVirtualPort(device=edge_name, port=port, is_loopback=True))
        out.loopback_ports += 1
        if cidr:
            model.ip_assignments.append(
                NSIPAssignment(device=edge_name, port=port, cidrs=[cidr]))
            out.ip_assignments += 1
        model.vrf_renames.append((edge_name, port, lan.vrf_of[vpn_id]))
        out.vrf_renames += 1

    return out
