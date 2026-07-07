# Copyright 2026 Cisco Systems, Inc. and its affiliates
# SPDX-License-Identifier: Apache-2.0

"""Map CML topology + parsed configs → Network Sketcher topology model.

Outputs a plain-dict model (`build_ns_model`) that the command builder
serialises into NS CLI commands. The model is also written to
``artifacts/ns_model.json`` for human review.

Layout policy (RULE 0 — vertical tier hierarchy, preserved as-is):
  row 0 = WAN / Internet / waypoint clouds
  row 1 = BGW / Border / Firewall
  row 2 = Spine
  row 3 = Leaf / Distribution / Aggregation
  row 4 = Access
  row 5 = Endpoint / Host / Server / PC / IoT

Crossing avoidance (RULE 0.5 — horizontal ordering within each tier):
  The NS engine draws L1 lines as straight segments between the *ports* on the
  device borders and offsets multiple links to distinct border points; it does
  NOT auto-route around devices. Empirically (verified against the live engine
  via the local MCP) this means:
    - A diagonal link between two devices on different tier rows almost never
      crosses a third device, because the port offsets clear the neighbouring
      cells. Inserting ``_AIR_`` corridors or column-aligning children under
      parents therefore does NOT measurably reduce real crossings.
    - The crossings that actually render are dominated by the *same-row* case:
      two linked devices in the SAME tier row with a third device sitting
      between them (a straight horizontal line drawn over that device).
  So the placement step keeps every device on its RULE 0 tier row and focuses
  on the one lever that matters: choosing the left-right ORDER inside each row
  to minimise the number of intra-row links that straddle another device
  (H1 link adjacency / H6 shortest L1). The order is found by exhaustive search
  for small rows and a deterministic hill-climb for large ones, with the
  barycentre of each device's cross-row neighbours used only as an aesthetic
  tie-break (H3 hub centring). Residual same-row crossings come from hubs with
  three or more same-row spokes and dense meshes, which are irreducible by
  ordering alone (RULE 0.5 H7).

Area policy (RULE 3):
  - Group nodes by shared CML "site" tag (e.g. site1, site2, wan-isn).
  - If a node has multiple site-like tags, prefer the most-specific one (`site*`).
  - WAN / inter-site fabric is its own waypoint area (`*_wp_`).
  - Nodes with no usable tag fall into the catch-all area `default`.
"""
from __future__ import annotations

import itertools
import math
import random
import re
import statistics
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple

from .stencil_mapper import (
    NS_AP, NS_CLOUD, NS_FIREWALL, NS_L3SWITCH, NS_PC, NS_ROUTER,
    NS_SERVER, NS_SWITCH, NS_WLC, StencilMapping,
)


# ---------------------------------------------------------------------------
# Data classes (intermediate NS model)
# ---------------------------------------------------------------------------

@dataclass
class NSDevice:
    name: str
    area: str
    row: int
    stencil: StencilMapping
    is_endpoint: bool
    routing_attribute: str = ""    # free-text BGP/OSPF/EVPN summary (RULE 11.5 + Attribute-D)
    x: Optional[float] = None      # CML canvas X coordinate (None if absent)
    y: Optional[float] = None      # CML canvas Y coordinate (None if absent)
    default_color: Optional[Tuple[int, int, int]] = None  # overrides role colour in the Attribute 'Default' cell


@dataclass
class NSL1Link:
    a_device: str
    a_port: str
    b_device: str
    b_port: str


@dataclass
class NSVirtualPort:
    device: str
    port: str                       # 'Vlan 100', 'Loopback 0', 'Port-channel 10'
    is_loopback: bool = False
    vlan_id: Optional[int] = None   # for SVIs


@dataclass
class NSIPAssignment:
    device: str
    port: str
    cidrs: List[str]


@dataclass
class NSL2Segment:
    device: str
    port: str                       # physical L1 port, or SVI for self-binding (RULE 15)
    vlans: List[str]                # ['Vlan100', 'Vlan200']


@dataclass
class NSPortChannel:
    device: str
    physical_ports: List[str]
    portchannel_name: str           # 'Port-channel 10'


@dataclass
class NSSubInterface:
    """A router/L3 sub-interface (e.g. router-on-a-stick dot1q).

    NS models these as a virtual port directly bound to the parent L1 interface
    via ``vport_l1if_direct_binding`` (and ``vport_l2_direct_binding`` for the
    dot1q VLAN), NOT via ``virtual_port_bulk``. The sub-interface must be
    created this way BEFORE any IP address can be assigned to it.
    """
    device: str
    parent_port: str                # 'GigabitEthernet 0/1'
    subif_port: str                 # 'GigabitEthernet 0/1.10'
    vlan_id: Optional[int] = None   # dot1q encapsulation VLAN, if any


@dataclass
class NSModel:
    areas: List[List[str]] = field(default_factory=list)            # area layout 2-D grid
    area_to_devices: Dict[str, List[List[str]]] = field(default_factory=dict)  # area -> 2-D device grid (rows)
    devices: Dict[str, NSDevice] = field(default_factory=dict)
    l1_links: List[NSL1Link] = field(default_factory=list)
    virtual_ports: List[NSVirtualPort] = field(default_factory=list)
    ip_assignments: List[NSIPAssignment] = field(default_factory=list)
    l2_segments_phys: List[NSL2Segment] = field(default_factory=list)  # L2 on physical ports
    l2_segments_svi: List[NSL2Segment] = field(default_factory=list)   # SVI self-binding (RULE 15)
    port_channels: List[NSPortChannel] = field(default_factory=list)
    subinterfaces: List[NSSubInterface] = field(default_factory=list)  # dot1q sub-ifs (RULE: vport_l1if_direct_binding)
    vrf_renames: List[Tuple[str, str, str]] = field(default_factory=list)  # (device, port, vrf)


# ---------------------------------------------------------------------------
# Port-name normalisation (CML → NS conventions, with spaces)
# ---------------------------------------------------------------------------

# Interface type tokens that NS accepts. The matcher tries each pattern in
# order; the first hit yields the canonical type token, and whatever follows
# the matched prefix becomes the "number" portion (joined with a single space).
#
# NS validates port names against this family of standard Cisco interface
# types and REJECTS anything else with "Invalid from_port" (empirically
# confirmed against the live engine). Both full names and common abbreviations
# (Gi, Te, Fa, Lo, Po, Se, Tu, Vl ...) must therefore be canonicalised. Single-
# letter abbreviations and abbreviations use a (?=\d) lookahead so they only
# match when an interface number actually follows.
_IFACE_TYPE_PATTERNS = [
    (re.compile(r"^TwentyFiveGigE", re.IGNORECASE), "TwentyFiveGigE"),
    (re.compile(r"^Twe(?=\d)", re.IGNORECASE), "TwentyFiveGigE"),
    (re.compile(r"^FortyGigabitEthernet", re.IGNORECASE), "FortyGigabitEthernet"),
    (re.compile(r"^FortyGigE", re.IGNORECASE), "FortyGigabitEthernet"),
    (re.compile(r"^Fo(?=\d)", re.IGNORECASE), "FortyGigabitEthernet"),
    (re.compile(r"^HundredGigE", re.IGNORECASE), "HundredGigE"),
    (re.compile(r"^Hu(?=\d)", re.IGNORECASE), "HundredGigE"),
    (re.compile(r"^TenGigabitEthernet", re.IGNORECASE), "TenGigabitEthernet"),
    (re.compile(r"^TenGigE", re.IGNORECASE), "TenGigabitEthernet"),
    (re.compile(r"^Te(?=\d)", re.IGNORECASE), "TenGigabitEthernet"),
    (re.compile(r"^GigabitEthernet", re.IGNORECASE), "GigabitEthernet"),
    (re.compile(r"^GigE", re.IGNORECASE), "GigabitEthernet"),
    (re.compile(r"^Gig(?=\d)", re.IGNORECASE), "GigabitEthernet"),
    (re.compile(r"^Gi(?=\d)", re.IGNORECASE), "GigabitEthernet"),
    (re.compile(r"^FastEthernet", re.IGNORECASE), "FastEthernet"),
    (re.compile(r"^Fas(?=\d)", re.IGNORECASE), "FastEthernet"),
    (re.compile(r"^Fa(?=\d)", re.IGNORECASE), "FastEthernet"),
    (re.compile(r"^Ethernet", re.IGNORECASE), "Ethernet"),
    (re.compile(r"^Eth(?=\d)", re.IGNORECASE), "Ethernet"),
    (re.compile(r"^Et(?=\d)", re.IGNORECASE), "Ethernet"),
    (re.compile(r"^Management", re.IGNORECASE), "Management"),
    (re.compile(r"^Mgmt", re.IGNORECASE), "mgmt"),
    (re.compile(r"^mgmt", re.IGNORECASE), "mgmt"),
    (re.compile(r"^Loopback", re.IGNORECASE), "Loopback"),
    (re.compile(r"^Loop(?=\d)", re.IGNORECASE), "Loopback"),
    (re.compile(r"^Lo(?=\d)", re.IGNORECASE), "Loopback"),
    (re.compile(r"^Vlan", re.IGNORECASE), "Vlan"),
    (re.compile(r"^Vl(?=\d)", re.IGNORECASE), "Vlan"),
    (re.compile(r"^Port-?channel", re.IGNORECASE), "Port-channel"),
    (re.compile(r"^Po(?=\d)", re.IGNORECASE), "Port-channel"),
    (re.compile(r"^Serial", re.IGNORECASE), "Serial"),
    (re.compile(r"^Ser(?=\d)", re.IGNORECASE), "Serial"),
    (re.compile(r"^Se(?=\d)", re.IGNORECASE), "Serial"),
    (re.compile(r"^Tunnel", re.IGNORECASE), "Tunnel"),
    (re.compile(r"^Tun(?=\d)", re.IGNORECASE), "Tunnel"),
    (re.compile(r"^Tu(?=\d)", re.IGNORECASE), "Tunnel"),
    (re.compile(r"^nve", re.IGNORECASE), "nve"),
    # Single-letter abbreviations (lowest priority): 'e0/0', 'g0/0'.
    (re.compile(r"^E(?=\d)", re.IGNORECASE), "Ethernet"),
    (re.compile(r"^G(?=\d)", re.IGNORECASE), "GigabitEthernet"),
]

# Vendor pseudo-ports (vWLC etc.) that have no Cisco interface type at all and
# must be remapped to a valid NS port name.
_PSEUDO_PORT_MAP = {
    "service-port": "Ethernet 0",
    "data-port": "Ethernet 1",
}

# Linux NIC name forms (eth0, ens3, enp0s2, eno1, em1, enxMAC). These must be
# matched explicitly so they are NOT confused with the Cisco "Ethernet" token.
_LINUX_NIC_RE = re.compile(r"^(?:eth\d|ens\d|enp\d|eno\d|em\d|enx[0-9a-f])", re.IGNORECASE)


def normalise_port_name(raw: str) -> str:
    """Convert a raw interface name into the NS convention (a single space
    between the type token and the number portion).

    NS only accepts standard Cisco interface type tokens; anything else is
    rejected by the engine. This function therefore maps abbreviations, Linux
    NIC names, and vendor pseudo-ports onto valid NS tokens.

    Examples:
        Ethernet1/1            -> Ethernet 1/1
        Gi0/0 / gig1/0         -> GigabitEthernet 0/0 / GigabitEthernet 1/0
        Te1/0/1                -> TenGigabitEthernet 1/0/1
        Vlan100                -> Vlan 100
        loopback0 / Lo0        -> Loopback 0
        port-channel10 / Po10  -> Port-channel 10
        GigabitEthernet0/2.20  -> GigabitEthernet 0/2.20  (sub-interface)
        mgmt0                  -> mgmt 0
        Management0/0          -> Management 0/0
        eth0 / ens3 / enp0s2   -> Ethernet 0 / Ethernet 3 / Ethernet 2  (Linux)
        port0 / port           -> Ethernet 0  (unmanaged-switch / external connector)
        service-port           -> Ethernet 0  (vWLC)
    """
    raw = (raw or "").strip()
    if not raw:
        return raw

    low = raw.lower()

    # Vendor pseudo-ports with no type token.
    if low in _PSEUDO_PORT_MAP:
        return _PSEUDO_PORT_MAP[low]

    # Unmanaged-switch / external-connector generic ports: 'port', 'port0' ...
    m = re.match(r"^port[\s_-]*(\d+)$", low)
    if m:
        return f"Ethernet {int(m.group(1))}"
    if low == "port":
        return "Ethernet 0"

    # Linux NIC names: derive the index from the last run of digits.
    if _LINUX_NIC_RE.match(low):
        nums = re.findall(r"\d+", raw)
        return f"Ethernet {int(nums[-1]) if nums else 0}"

    for pat, canonical in _IFACE_TYPE_PATTERNS:
        m = pat.match(raw)
        if m:
            remainder = raw[m.end():].lstrip()
            return f"{canonical} {remainder}" if remainder else canonical

    return raw  # unknown form: leave as-is (NS may still reject it)


# ---------------------------------------------------------------------------
# CML link / interface plumbing
# ---------------------------------------------------------------------------

def _index_cml_interfaces(nodes: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Index every CML interface so that links can be resolved across formats.

    CML lab YAMLs come in (at least) two flavours that differ in how interface
    identity is encoded:

    1. **API dump** (e.g. produced by the CML REST API extractor): interface
       ``id`` values are globally-unique UUIDs, and links reference them via
       ``interface_a`` / ``interface_b``.

    2. **UI export / Download Lab / cml-community** files: interface ``id``
       values are *node-local* (every node starts again at ``i0``, ``i1`` …),
       and links reference them via ``i1`` / ``i2`` together with the owning
       node refs ``n1`` / ``n2``.

    To handle both, we return a dict with two sub-indexes:

    - ``by_node_iface``: ``{(node_id, iface_id): info}`` — always correct,
      used as the primary resolver for both formats.
    - ``by_iface``: ``{iface_id: info}`` — only populated for interface ids
      that are globally unique; used as a fallback when a link omits node refs.
    """
    by_node_iface: Dict[tuple, Dict[str, Any]] = {}
    global_counts: Dict[str, int] = {}
    by_iface: Dict[str, Dict[str, Any]] = {}

    for n in nodes:
        node_id = n.get("id")
        node_label = n.get("label", "")
        for iface in n.get("interfaces", []) or []:
            iid = iface.get("id")
            if iid is None:
                continue
            info = {
                "node_id": node_id,
                "node_label": node_label,
                "iface_id": iid,
                "iface_label": iface.get("label", "") or iface.get("slot", ""),
                "iface_slot": iface.get("slot"),
                "iface_type": iface.get("type", ""),
            }
            by_node_iface[(node_id, iid)] = info
            global_counts[iid] = global_counts.get(iid, 0) + 1
            by_iface[iid] = info

    # Drop colliding ids from the global index so it stays unambiguous.
    for iid, cnt in global_counts.items():
        if cnt > 1:
            by_iface.pop(iid, None)

    return {"by_node_iface": by_node_iface, "by_iface": by_iface}


# ---------------------------------------------------------------------------
# Area / hierarchy assignment
# ---------------------------------------------------------------------------

# Raw area names (before the build_area_layout `*_wp_` promotion) that denote a
# WAN / Internet / cloud waypoint area. Inter-area links that touch one of these
# are legitimate "device-to-waypoint" connections (RULE 3); links between two
# NON-waypoint areas are not allowed by the engine and signal an over-eager area
# split of directly-cabled devices (see _coalesce_directly_linked_areas).
_RAW_WAYPOINT_AREAS = {"wan-isn", "wan", "internet", "cloud"}

SITE_TAG_RE = re.compile(r"^(site\d+|wan-?isn|wan|core|dc\d+|pod\d+|hq|branch\d+|campus)$", re.IGNORECASE)
ENDPOINT_NDEF = {"alpine", "ubuntu", "centos", "tiny-linux", "server", "desktop",
                  "win-desktop", "win-server", "wireless-client"}
WAYPOINT_NDEF = {"external_connector"}

# NS's native WayPoint colour (light blue), used for an OBSERVED WayPoint: an
# 'external_connector' is a real node the user placed in the CML lab (it has
# its own label, like any other node) — it is just a bridge to the host
# network rather than a simulated device, so it is observed, not inferred.
_OBSERVED_WAYPOINT = (220, 230, 242)


def _pick_area(node: Dict[str, Any]) -> str:
    """Choose an area name from a CML node's tags / label."""
    tags = [str(t).lower() for t in (node.get("tags") or [])]
    for t in tags:
        if SITE_TAG_RE.match(t):
            return t.lower()
    # Heuristics on label: e.g. "s1-spine1" => site1, "s2-leaf1" => site2.
    label = (node.get("label", "") or "").lower()
    m = re.match(r"^s(\d+)-", label)
    if m:
        return f"site{m.group(1)}"
    if any(k in label for k in ["wan", "isn", "internet", "cloud"]):
        return "wan-isn"
    if label.startswith("h") and re.match(r"^h\d", label):
        # alpine host labelling pattern h11, h21 => site1, site2
        if len(label) >= 2 and label[1].isdigit():
            return f"site{label[1]}"
    return "default"


def _pick_row(node: Dict[str, Any], stencil: StencilMapping) -> int:
    tags = [str(t).lower() for t in (node.get("tags") or [])]
    nd = (node.get("node_definition") or "").lower()
    label = (node.get("label", "") or "").lower()

    if stencil.stencil_type == NS_CLOUD or nd in WAYPOINT_NDEF or "wan" in label or "isn" in label:
        return 0
    if any(t in tags for t in ["bgw", "border", "edge"]) or "bgw" in label or "border" in label:
        return 1
    if stencil.stencil_type == NS_FIREWALL:
        return 1
    if "spine" in tags or "spine" in label:
        return 2
    if "leaf" in tags or "leaf" in label:
        return 3
    if stencil.stencil_type == NS_L3SWITCH:
        return 3
    if any(k in label for k in ["core", "dist", "agg"]):
        return 3
    if stencil.stencil_type in {NS_SWITCH, NS_WLC}:
        return 4
    if stencil.stencil_type == NS_AP:
        return 4
    if nd in ENDPOINT_NDEF or stencil.stencil_type in {NS_SERVER, NS_PC}:
        return 5
    return 3  # safe default for unknown infra (Router)


def assign_areas_and_rows(
    nodes: List[Dict[str, Any]],
    stencils: Dict[str, StencilMapping],
) -> Dict[str, NSDevice]:
    devices: Dict[str, NSDevice] = {}
    for n in nodes:
        label = str(n.get("label", ""))
        if not label:
            continue
        st = stencils[label]
        area = _pick_area(n)
        row = _pick_row(n, st)
        is_endpoint = st.stencil_type in {NS_SERVER, NS_PC} or (n.get("node_definition") or "") in ENDPOINT_NDEF
        is_waypoint_ndef = (n.get("node_definition") or "") in WAYPOINT_NDEF
        devices[label] = NSDevice(
            name=label,
            area=area,
            row=row,
            stencil=st,
            is_endpoint=is_endpoint,
            x=_coerce_coord(n.get("x")),
            y=_coerce_coord(n.get("y")),
            default_color=_OBSERVED_WAYPOINT if is_waypoint_ndef else None,
        )
    return devices


def _coerce_coord(value: Any) -> Optional[float]:
    """Return a CML coordinate as float, or None when missing / non-numeric."""
    if isinstance(value, bool):  # guard: bool is an int subclass
        return None
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _coalesce_directly_linked_areas(
    devices: Dict[str, NSDevice],
    l1_links: List[NSL1Link],
) -> int:
    """Merge non-waypoint areas that are joined by a direct device-to-device
    L1 link, in place, to satisfy RULE 3.

    NS forbids a direct L1 link between two devices that live in different
    *non-waypoint* areas ("Device-to-Device (different areas) | No | Must use
    Waypoint"). A genuine inter-site link in CML is modelled through a WAN /
    cloud / external-connector node, which we already route into a ``*_wp_``
    waypoint area — those links are valid and left untouched. The only way two
    plain devices end up cabled across non-waypoint areas is an over-eager area
    split (e.g. a host labelled ``h1`` guessed into ``site1`` while its access
    switch stayed in ``default``). Since the cable proves they share a physical
    segment, the RULE-3-correct fix is to put them in the same area.

    Implementation: union-find over every direct device-to-device link whose
    endpoints are both in non-waypoint areas; each connected component that
    spans more than one non-waypoint area is reassigned to a single canonical
    area (the most populated one, tie-broken by ``_area_sort_key``). Devices in
    waypoint areas are never moved. Returns the number of devices reassigned.
    """
    parent: Dict[str, str] = {n: n for n in devices}

    def find(x: str) -> str:
        root = x
        while parent[root] != root:
            root = parent[root]
        while parent[x] != root:
            parent[x], x = root, parent[x]
        return root

    def union(a: str, b: str) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[ra] = rb

    def is_wp(name: str) -> bool:
        return devices[name].area in _RAW_WAYPOINT_AREAS

    for lk in l1_links:
        a, b = lk.a_device, lk.b_device
        if a in devices and b in devices and a != b and not is_wp(a) and not is_wp(b):
            union(a, b)

    components: Dict[str, List[str]] = defaultdict(list)
    for n in devices:
        components[find(n)].append(n)

    reassigned = 0
    for members in components.values():
        non_wp_areas = [devices[n].area for n in members if not is_wp(n)]
        if len(set(non_wp_areas)) <= 1:
            continue
        counts: Dict[str, int] = defaultdict(int)
        for a in non_wp_areas:
            counts[a] += 1
        canonical = sorted(
            set(non_wp_areas), key=lambda a: (-counts[a], _area_sort_key(a)),
        )[0]
        for n in members:
            if not is_wp(n) and devices[n].area != canonical:
                devices[n].area = canonical
                reassigned += 1
    return reassigned


def build_area_layout(
    devices: Dict[str, NSDevice],
    l1_links: Optional[List[NSL1Link]] = None,
    layout: str = "auto",
) -> Tuple[List[List[str]], Dict[str, List[List[str]]]]:
    """Return (area_layout, area_to_device_grid).

    Strategy:
      - Areas are placed left-to-right in row 0 (one outer row is enough for POC).
      - Within an area, device placement depends on ``layout``:
          * ``coordinate`` / ``auto`` — mirror the CML canvas (x, y) positions
            via ``_place_by_coordinates``; ``auto`` (and ``coordinate``) fall
            back to the tier layout for any area whose devices lack coordinates.
          * ``tier`` — keep each device on its RULE 0 tier row, ordering the
            left-right sequence inside each row for L1 crossing avoidance
            (RULE 0.5) via ``_place_columns``. When ``l1_links`` is omitted the
            rows fall back to a stable name sort.
      - Waypoint areas (`wan-isn`) become `*_wp_` placed between site areas if
        present.
    """
    by_area: Dict[str, List[NSDevice]] = {}
    for d in devices.values():
        by_area.setdefault(d.area, []).append(d)

    ordered_areas: List[str] = sorted(by_area.keys(), key=_area_sort_key)
    # Promote wan-isn-style areas to waypoint naming so NS treats them as clouds.
    rendered_areas: List[str] = []
    name_map: Dict[str, str] = {}
    for a in ordered_areas:
        if a in _RAW_WAYPOINT_AREAS:
            rendered = f"{a}_wp_"
        else:
            rendered = a
        rendered_areas.append(rendered)
        name_map[a] = rendered

    # Re-apply area names back to devices.
    for d in devices.values():
        d.area = name_map.get(d.area, d.area)

    # Build an undirected device adjacency from the L1 links so the placement
    # step can pull connected devices into adjacent grid columns (RULE 0.5).
    adjacency: Dict[str, Set[str]] = defaultdict(set)
    for lk in (l1_links or []):
        if lk.a_device and lk.b_device and lk.a_device != lk.b_device:
            adjacency[lk.a_device].add(lk.b_device)
            adjacency[lk.b_device].add(lk.a_device)

    area_to_grid: Dict[str, List[List[str]]] = {}
    for orig_area, devs in by_area.items():
        rendered = name_map[orig_area]
        grid: Optional[List[List[str]]] = None
        # Coordinate-preserving placement (inherit the CML canvas layout).
        # `auto` and `coordinate` both try coordinates first; a missing-coord
        # area returns None and drops through to the tier layout below.
        if layout in ("auto", "coordinate"):
            grid = _place_by_coordinates(devs)
        if grid is None:
            # Tier layout: group by RULE 0 tier row (rows never reordered),
            # then order columns for L1 crossing avoidance (RULE 0.5).
            row_buckets: Dict[int, List[str]] = {}
            for d in devs:
                row_buckets.setdefault(d.row, []).append(d.name)
            tier_rows: List[List[str]] = [
                sorted(row_buckets[row_idx]) for row_idx in sorted(row_buckets.keys())
            ]
            grid = _place_columns(tier_rows, adjacency)
        area_to_grid[rendered] = grid

    area_layout = [rendered_areas]
    return area_layout, area_to_grid


# Cap on row width for the exhaustive ordering search; above this we fall back
# to a deterministic hill-climb (permutations would be too many).
_ROW_PERM_LIMIT = 8
_ROW_HILLCLIMB_ITERS = 4000


def _row_between_cost(order: Sequence[str], intra_edges: Set[Tuple[str, str]]) -> int:
    """Number of intra-row links whose endpoints have ≥1 device between them
    in this left-to-right ``order`` (i.e. the line would render over a device).
    """
    pos = {n: i for i, n in enumerate(order)}
    cost = 0
    for a, b in intra_edges:
        if abs(pos[a] - pos[b]) > 1:
            cost += 1
    return cost


def _order_row(
    devices: List[str],
    intra_edges: Set[Tuple[str, str]],
    barycentre: Dict[str, float],
) -> List[str]:
    """Order one tier row to minimise same-row over-device crossings.

    Ties (orderings with equal crossing cost) are broken by keeping each device
    close to the barycentre column of its cross-row neighbours (H3 hub centring
    / tidy diagonals), then by name for determinism.
    """
    if len(devices) <= 1:
        return list(devices)

    base = sorted(devices, key=lambda n: (barycentre.get(n, 0.0), n))
    if not intra_edges:
        return base

    def tie_break(order: Sequence[str]) -> float:
        return sum(abs(i - barycentre.get(n, float(i))) for i, n in enumerate(order))

    if len(devices) <= _ROW_PERM_LIMIT:
        best_order = base
        best_key = (_row_between_cost(base, intra_edges), tie_break(base))
        for perm in itertools.permutations(base):
            key = (_row_between_cost(perm, intra_edges), tie_break(perm))
            if key < best_key:
                best_key = key
                best_order = list(perm)
        return list(best_order)

    # Large row: deterministic hill-climb from the barycentre order.
    cur = list(base)
    cur_cost = _row_between_cost(cur, intra_edges)
    rng = random.Random(1234)
    for _ in range(_ROW_HILLCLIMB_ITERS):
        if cur_cost == 0:
            break
        i, j = rng.sample(range(len(cur)), 2)
        cur[i], cur[j] = cur[j], cur[i]
        new_cost = _row_between_cost(cur, intra_edges)
        if new_cost <= cur_cost:
            cur_cost = new_cost
        else:
            cur[i], cur[j] = cur[j], cur[i]
    return cur


def _place_columns(
    tier_rows: List[List[str]],
    adjacency: Dict[str, Set[str]],
) -> List[List[str]]:
    """Order devices within each tier row to minimise L1 lines rendered over a
    device (RULE 0.5).

    The vertical tier of every device (its row index) is fixed by RULE 0; this
    routine only decides the horizontal ORDER inside each row. The dominant —
    and, against the live NS engine, essentially the only — class of real
    over-device crossings is the *same-row* case: two linked devices sharing a
    tier row with a third device between them. Diagonal (cross-row) links are
    cleared by NS's per-port border offsets, so neither ``_AIR_`` corridors nor
    column alignment help them; ordering each row is what moves the needle.

    Columns are left contiguous (NS centres/normalises the grid on its own); no
    ``_AIR_`` spacers are emitted, keeping the diagram compact.
    """
    rows: List[List[str]] = [list(r) for r in tier_rows if r]
    if not rows:
        return []

    names = {n for r in rows for n in r}
    adj: Dict[str, List[str]] = {
        n: [x for x in adjacency.get(n, ()) if x in names] for n in names
    }
    row_of: Dict[str, int] = {n: ri for ri, r in enumerate(rows) for n in r}

    # Undirected edges restricted to this area, split into intra-row buckets.
    edges: Set[Tuple[str, str]] = set()
    for n in names:
        for x in adj[n]:
            edges.add(tuple(sorted((n, x))))  # type: ignore[arg-type]
    intra: Dict[int, Set[Tuple[str, str]]] = defaultdict(set)
    for a, b in edges:
        if row_of[a] == row_of[b]:
            intra[row_of[a]].add((a, b))

    # Column index per device, seeded from the initial name-sorted order.
    col: Dict[str, int] = {n: i for r in rows for i, n in enumerate(r)}

    # A few sweeps so the barycentre tie-break can react to neighbour columns
    # settling in adjacent rows.
    for _sweep in range(4):
        for ri, row in enumerate(rows):
            barycentre = {
                n: (statistics.median([col[x] for x in adj[n] if row_of[x] != ri])
                    if any(row_of[x] != ri for x in adj[n]) else float(col[n]))
                for n in row
            }
            ordered = _order_row(row, intra.get(ri, set()), barycentre)
            rows[ri] = ordered
            for i, n in enumerate(ordered):
                col[n] = i

    return [list(r) for r in rows]


# Density factor: target grid cell count = _COORD_CELL_DENSITY * device count.
# >1 leaves slack so equal-width coordinate bins seldom collide; the more slack,
# the closer each device lands to its true coordinate cell (less sideways probe
# displacement). 3.0 keeps both axes faithful — on the 300-node reference lab,
# correlation(row, y) ≈ 0.999 and correlation(col, x) ≈ 0.94 — without an
# excessively sparse canvas.
_COORD_CELL_DENSITY = 3.0


def _place_by_coordinates(devs: List[NSDevice]) -> Optional[List[List[str]]]:
    """Lay out one area's devices on a discrete grid mirroring their CML
    canvas coordinates (inherit-CML-layout strategy).

    CML gives every node a continuous (x, y) position. Network Sketcher places
    devices on a row/column grid where ``_AIR_`` marks a blank cell, so we
    quantise the coordinates into an R x C grid:

      - x -> column (left → right); y -> row (top → bottom, matching CML's
        screen-style Y-down axis).
      - Grid size derives from the coordinate aspect ratio with slack
        (``_COORD_CELL_DENSITY``) so equal-width bins seldom collide.
      - Collisions are resolved by probing outward to the nearest free column
        in the same row, widening the grid as a last resort, so no device is
        ever dropped.
      - Fully-empty rows (blank y-bands) are dropped and trailing blank columns
        trimmed to keep the diagram compact while preserving relative order.

    Returns None when any device lacks coordinates so the caller can fall back
    to the tier-based layout.
    """
    if not devs:
        return []
    if any(d.x is None or d.y is None for d in devs):
        return None

    xs = [float(d.x) for d in devs]  # type: ignore[arg-type]
    ys = [float(d.y) for d in devs]  # type: ignore[arg-type]
    xmin, xmax = min(xs), max(xs)
    ymin, ymax = min(ys), max(ys)
    xspan = xmax - xmin
    yspan = ymax - ymin
    n = len(devs)

    # Grid dimensions from the coordinate aspect ratio (wider canvas => more
    # columns), with slack so equal-width bins rarely collide.
    if xspan <= 0 and yspan <= 0:
        aspect = 1.0
    elif yspan <= 0:
        aspect = float(n)          # all on one horizontal line => single row
    elif xspan <= 0:
        aspect = 1.0 / float(n)    # all on one vertical line => single column
    else:
        aspect = xspan / yspan
    cells = _COORD_CELL_DENSITY * n
    cols = max(1, round(math.sqrt(cells * aspect)))
    rows = max(1, round(math.sqrt(cells / aspect)))

    def _bin(v: float, lo: float, span: float, k: int) -> int:
        if span <= 0:
            return 0
        return min(k - 1, max(0, int((v - lo) / span * k)))

    grid: List[List[Optional[str]]] = [[None] * cols for _ in range(rows)]

    # Deterministic placement order: top→bottom, then left→right, then name.
    for d in sorted(devs, key=lambda d: (float(d.y), float(d.x), d.name)):  # type: ignore[arg-type]
        r = _bin(float(d.y), ymin, yspan, rows)   # type: ignore[arg-type]
        c = _bin(float(d.x), xmin, xspan, cols)   # type: ignore[arg-type]
        if grid[r][c] is None:
            grid[r][c] = d.name
            continue
        # Probe outward in the same row for the nearest free column.
        placed = False
        width = len(grid[r])
        for off in range(1, width + 1):
            for cc in (c + off, c - off):
                if 0 <= cc < width and grid[r][cc] is None:
                    grid[r][cc] = d.name
                    placed = True
                    break
            if placed:
                break
        if not placed:
            # Row full: widen every row by one column and use the new cell.
            for rr in range(len(grid)):
                grid[rr].append(None)
            grid[r][-1] = d.name

    # Drop fully-empty rows (collapse blank y-bands) and pad to a rectangle.
    used_rows = [row for row in grid if any(cell is not None for cell in row)]
    if not used_rows:
        return []
    width = max(len(row) for row in used_rows)
    padded = [row + [None] * (width - len(row)) for row in used_rows]
    # Trim trailing all-blank columns.
    last_used = -1
    for ci in range(width):
        if any(row[ci] is not None for row in padded):
            last_used = ci
    width = last_used + 1
    return [
        [cell if cell is not None else "_AIR_" for cell in row[:width]]
        for row in padded
    ]


def _area_sort_key(area: str) -> Tuple[int, str]:
    # WAN clouds go first (left), then site1, site2, ... then 'default' last.
    if area in {"wan-isn", "wan", "internet", "cloud"}:
        return (0, area)
    m = re.match(r"site(\d+)", area)
    if m:
        return (1, f"{int(m.group(1)):03d}")
    if area == "default":
        return (9, area)
    return (5, area)


# ---------------------------------------------------------------------------
# L1 link extraction (from CML topology)
# ---------------------------------------------------------------------------

def _link_endpoints(link: Dict[str, Any]) -> tuple:
    """Extract (node_a, iface_a, node_b, iface_b) refs from any link schema.

    Supports both the API-dump schema (``node_a`` / ``interface_a`` …) and the
    UI-export schema (``n1`` / ``i1`` …). Missing fields come back as ``None``.
    """
    node_a = link.get("node_a", link.get("n1"))
    node_b = link.get("node_b", link.get("n2"))
    iface_a = link.get("interface_a", link.get("i1"))
    iface_b = link.get("interface_b", link.get("i2"))
    return node_a, iface_a, node_b, iface_b


def _resolve_iface(
    iface_index: Dict[str, Any],
    node_ref: Any,
    iface_ref: Any,
) -> Optional[Dict[str, Any]]:
    """Resolve an interface using the composite key first, then a global
    fallback when the link omitted its node reference."""
    if iface_ref is None:
        return None
    by_node_iface = iface_index.get("by_node_iface", {})
    by_iface = iface_index.get("by_iface", {})
    if node_ref is not None:
        info = by_node_iface.get((node_ref, iface_ref))
        if info is not None:
            return info
    # Fallback: globally-unique interface id (API dumps may not repeat node ref).
    return by_iface.get(iface_ref)


def build_l1_links(
    links: List[Dict[str, Any]],
    iface_index: Dict[str, Any],
) -> List[NSL1Link]:
    out: List[NSL1Link] = []
    for link in links:
        node_a, iface_a_ref, node_b, iface_b_ref = _link_endpoints(link)
        a_iface = _resolve_iface(iface_index, node_a, iface_a_ref)
        b_iface = _resolve_iface(iface_index, node_b, iface_b_ref)
        if not a_iface or not b_iface:
            continue
        a_port = normalise_port_name(a_iface["iface_label"] or "")
        b_port = normalise_port_name(b_iface["iface_label"] or "")
        if not a_port or not b_port:
            continue
        out.append(NSL1Link(
            a_device=a_iface["node_label"],
            a_port=a_port,
            b_device=b_iface["node_label"],
            b_port=b_port,
        ))
    return out


# ---------------------------------------------------------------------------
# Apply parsed configs (VLANs / SVIs / Loopbacks / L3 / port-channels / VRFs)
# ---------------------------------------------------------------------------

def apply_parsed_configs(
    model: NSModel,
    parsed_configs: Dict[str, Any],   # label -> ParsedConfig
    cml_node_labels: Set[str],
) -> Dict[str, Dict[str, int]]:
    """Walk every parsed config and fill model.l2_segments / virtual_ports etc.

    Returns a per-device stat dict for the parse_report.md.
    """
    stats: Dict[str, Dict[str, int]] = {}
    for label, cfg in parsed_configs.items():
        if label not in model.devices:
            continue
        is_endpoint = model.devices[label].is_endpoint
        st = {"vlans": 0, "svi": 0, "loopback": 0,
              "l3_phys": 0, "l2_trunk": 0, "l2_access": 0, "portchannel": 0, "vrf": 0}

        # Per-VLAN are recorded only for routing-summary; NS doesn't have a
        # VLAN-table concept independent of an SVI binding. So we just count.
        st["vlans"] = len(cfg.vlans)

        # Track port-channel members for the portchannel_bulk call.
        po_members: Dict[int, List[str]] = {}

        for iname, iface in cfg.interfaces.items():
            ns_port = normalise_port_name(iname)

            if iface.kind == "svi":
                if not is_endpoint and iface.ipv4:
                    vid = _extract_vlan_id(iname)
                    model.virtual_ports.append(NSVirtualPort(
                        device=label, port=ns_port, vlan_id=vid,
                    ))
                    model.ip_assignments.append(NSIPAssignment(
                        device=label, port=ns_port,
                        cidrs=[a.cidr for a in iface.ipv4 + iface.ipv4_secondary],
                    ))
                    if vid is not None:
                        model.l2_segments_svi.append(NSL2Segment(
                            device=label, port=ns_port,
                            vlans=[f"Vlan{vid}"],
                        ))
                    if iface.vrf:
                        model.vrf_renames.append((label, ns_port, iface.vrf))
                    st["svi"] += 1

            elif iface.kind == "loopback":
                if iface.ipv4:
                    model.virtual_ports.append(NSVirtualPort(
                        device=label, port=ns_port, is_loopback=True,
                    ))
                    model.ip_assignments.append(NSIPAssignment(
                        device=label, port=ns_port,
                        cidrs=[a.cidr for a in iface.ipv4 + iface.ipv4_secondary],
                    ))
                    if iface.vrf:
                        model.vrf_renames.append((label, ns_port, iface.vrf))
                    st["loopback"] += 1

            elif iface.kind == "portchannel":
                # The port-channel virtual interface itself.
                if iface.ipv4:
                    model.ip_assignments.append(NSIPAssignment(
                        device=label, port=ns_port,
                        cidrs=[a.cidr for a in iface.ipv4 + iface.ipv4_secondary],
                    ))
                    st["l3_phys"] += 1
                if not is_endpoint and (iface.trunk_allowed_vlans or iface.trunk_native_vlan is not None):
                    vlans = _trunk_vlans(iface)
                    if vlans:
                        model.l2_segments_phys.append(NSL2Segment(
                            device=label, port=ns_port, vlans=vlans,
                        ))
                        st["l2_trunk"] += 1
                if iface.access_vlan and not is_endpoint:
                    model.l2_segments_phys.append(NSL2Segment(
                        device=label, port=ns_port,
                        vlans=[f"Vlan{iface.access_vlan}"],
                    ))
                    st["l2_access"] += 1
                if iface.vrf:
                    model.vrf_renames.append((label, ns_port, iface.vrf))

            elif iface.kind in {"physical", "subif"}:
                if iface.channel_group is not None:
                    po_members.setdefault(iface.channel_group, []).append(ns_port)
                    st["portchannel"] += 1
                    continue
                if iface.kind == "subif":
                    # Sub-interfaces must be created as a virtual port bound to
                    # the parent L1 interface BEFORE an IP can be assigned (NS
                    # rejects IPs on undeclared sub-ifs). Record the binding;
                    # the command builder emits vport_l1if_direct_binding (+
                    # vport_l2_direct_binding for the dot1q VLAN).
                    parent_port = normalise_port_name(iname.split(".", 1)[0])
                    model.subinterfaces.append(NSSubInterface(
                        device=label, parent_port=parent_port,
                        subif_port=ns_port, vlan_id=iface.access_vlan,
                    ))
                    if iface.ipv4:
                        model.ip_assignments.append(NSIPAssignment(
                            device=label, port=ns_port,
                            cidrs=[a.cidr for a in iface.ipv4 + iface.ipv4_secondary],
                        ))
                        if iface.vrf:
                            model.vrf_renames.append((label, ns_port, iface.vrf))
                        st["l3_phys"] += 1
                    continue
                if iface.is_routed() and iface.ipv4:
                    model.ip_assignments.append(NSIPAssignment(
                        device=label, port=ns_port,
                        cidrs=[a.cidr for a in iface.ipv4 + iface.ipv4_secondary],
                    ))
                    if iface.vrf:
                        model.vrf_renames.append((label, ns_port, iface.vrf))
                    st["l3_phys"] += 1
                else:
                    if is_endpoint:
                        # Endpoint side: do nothing here; IP (if any) is direct (RULE 11.5).
                        if iface.ipv4:
                            model.ip_assignments.append(NSIPAssignment(
                                device=label, port=ns_port,
                                cidrs=[a.cidr for a in iface.ipv4],
                            ))
                            st["l3_phys"] += 1
                    else:
                        # Switch-mode physical port: l2_segment.
                        if iface.mode == "trunk" and (iface.trunk_allowed_vlans or iface.trunk_native_vlan is not None):
                            vlans = _trunk_vlans(iface)
                            if vlans:
                                model.l2_segments_phys.append(NSL2Segment(
                                    device=label, port=ns_port, vlans=vlans,
                                ))
                                st["l2_trunk"] += 1
                        elif iface.access_vlan:
                            model.l2_segments_phys.append(NSL2Segment(
                                device=label, port=ns_port,
                                vlans=[f"Vlan{iface.access_vlan}"],
                            ))
                            st["l2_access"] += 1

            elif iface.kind == "mgmt":
                # Mgmt interfaces are usually in 'management' VRF — record as
                # L3 physical with VRF tag (NS represents it as L3 on the port).
                if iface.ipv4:
                    model.ip_assignments.append(NSIPAssignment(
                        device=label, port=ns_port,
                        cidrs=[a.cidr for a in iface.ipv4 + iface.ipv4_secondary],
                    ))
                    if iface.vrf:
                        model.vrf_renames.append((label, ns_port, iface.vrf))
                    st["l3_phys"] += 1

            # Tunnel / nve / others: routing-summary text only.

        # Emit port-channels we collected.
        for po_id, members in po_members.items():
            pc_name = f"Port-channel {po_id}"
            model.port_channels.append(NSPortChannel(
                device=label,
                physical_ports=sorted(set(members)),
                portchannel_name=pc_name,
            ))

        # Routing summary -> stored on device for assess.py.
        if cfg.routing_summary_lines:
            model.devices[label].routing_attribute = "\n".join(cfg.routing_summary_lines[:30])

        stats[label] = st
    return stats


def _extract_vlan_id(iface_name: str) -> Optional[int]:
    m = re.search(r"(\d+)$", iface_name)
    return int(m.group(1)) if m else None


def _trunk_vlans(iface: Any) -> List[str]:
    """VLAN list for a trunk port's L2 segment.

    The native VLAN is carried (untagged) on the trunk too, so include it even
    when it is not part of ``switchport trunk allowed vlan`` — otherwise its
    membership would be silently dropped.
    """
    vids = list(iface.trunk_allowed_vlans)
    if iface.trunk_native_vlan is not None and iface.trunk_native_vlan not in vids:
        vids.append(iface.trunk_native_vlan)
    return [f"Vlan{v}" for v in vids]


# ---------------------------------------------------------------------------
# Top-level builder
# ---------------------------------------------------------------------------

def build_ns_model(
    nodes: List[Dict[str, Any]],
    links: List[Dict[str, Any]],
    stencils: Dict[str, StencilMapping],
    parsed_configs: Dict[str, Any],
    layout: str = "auto",
) -> Tuple[NSModel, Dict[str, Dict[str, int]]]:
    model = NSModel()
    model.devices = assign_areas_and_rows(nodes, stencils)

    # Resolve L1 links first so the layout step can use device adjacency to
    # place connected devices in adjacent columns (RULE 0.5 crossing avoidance).
    iface_index = _index_cml_interfaces(nodes)
    model.l1_links = build_l1_links(links, iface_index)

    # RULE 3: a direct device-to-device link cannot cross non-waypoint areas.
    # Merge any areas that an over-eager split left straddled by such a link.
    _coalesce_directly_linked_areas(model.devices, model.l1_links)

    model.areas, model.area_to_devices = build_area_layout(
        model.devices, model.l1_links, layout=layout,
    )

    parse_stats = apply_parsed_configs(
        model, parsed_configs,
        cml_node_labels={str(n.get("label", "")) for n in nodes},
    )
    return model, parse_stats


# ---------------------------------------------------------------------------
# JSON serialisation helper
# ---------------------------------------------------------------------------

def model_to_dict(model: NSModel) -> Dict[str, Any]:
    return {
        "area_layout": model.areas,
        "area_to_devices": model.area_to_devices,
        "devices": {
            name: {
                "area": d.area, "row": d.row, "is_endpoint": d.is_endpoint,
                "stencil": d.stencil.stencil_type,
                "model": d.stencil.model,
                "os": d.stencil.os,
                "confidence": d.stencil.confidence,
                "routing_attribute_len": len(d.routing_attribute),
            }
            for name, d in sorted(model.devices.items())
        },
        "l1_links": [
            {"a_device": x.a_device, "a_port": x.a_port,
             "b_device": x.b_device, "b_port": x.b_port}
            for x in model.l1_links
        ],
        "virtual_ports": [
            {"device": v.device, "port": v.port,
             "is_loopback": v.is_loopback, "vlan_id": v.vlan_id}
            for v in model.virtual_ports
        ],
        "ip_assignments": [
            {"device": ip.device, "port": ip.port, "cidrs": ip.cidrs}
            for ip in model.ip_assignments
        ],
        "l2_segments_phys": [
            {"device": s.device, "port": s.port, "vlans": s.vlans}
            for s in model.l2_segments_phys
        ],
        "l2_segments_svi": [
            {"device": s.device, "port": s.port, "vlans": s.vlans}
            for s in model.l2_segments_svi
        ],
        "port_channels": [
            {"device": pc.device, "physical_ports": pc.physical_ports,
             "portchannel_name": pc.portchannel_name}
            for pc in model.port_channels
        ],
        "subinterfaces": [
            {"device": si.device, "parent_port": si.parent_port,
             "subif_port": si.subif_port, "vlan_id": si.vlan_id}
            for si in model.subinterfaces
        ],
        "vrf_renames": [
            {"device": d, "port": p, "vrf": v}
            for (d, p, v) in model.vrf_renames
        ],
    }
