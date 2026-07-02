# Copyright 2026 Cisco Systems, Inc. and its affiliates
# SPDX-License-Identifier: Apache-2.0

"""Network Sketcher intermediate model + reusable helpers.

This module is the converter-agnostic half of the Network Sketcher pipeline,
shared verbatim with ``aci_converter`` / ``cml_converter`` so that every mapper
(physical / logical) emits the *same* ``NSModel`` and the single
``ns_command_builder`` can serialise any of them identically.

It contains ONLY the reusable pieces — no ND- or ACI-specific build logic:

  * the ``NS*`` dataclasses (the model contract ``ns_command_builder`` depends on),
  * ``normalise_port_name`` (Cisco interface-name → NS canonical form),
  * ``build_area_layout`` + its placement helpers (tier / coordinate layout),
  * ``_coalesce_directly_linked_areas`` (RULE 3 area-merge),
  * ``model_to_dict`` (JSON serialisation for the debug artifact).

Layout policy (RULE 0 — vertical tier hierarchy):
  row 0 = WAN / Internet / waypoint clouds
  row 1 = BGW / Border / Firewall
  row 2 = Spine
  row 3 = Leaf / Distribution / Aggregation
  row 4 = Access
  row 5 = Endpoint / Host / Server / PC / IoT
"""
from __future__ import annotations

import itertools
import math
import random
import re
import statistics
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

from .nd_stencil_mapper import StencilMapping


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
    routing_attribute: str = ""    # free-text summary (RULE 11.5 + Attribute-D)
    x: Optional[float] = None      # canvas X coordinate (None if absent)
    y: Optional[float] = None      # canvas Y coordinate (None if absent)
    default_color: Optional[Tuple[int, int, int]] = None  # overrides role colour in the Attribute 'Default' cell
    port_info: Optional[Tuple[str, str, str]] = None  # (speed, duplex, media) override from real interface detail


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
    subinterfaces: List[NSSubInterface] = field(default_factory=list)  # dot1q sub-ifs
    vrf_renames: List[Tuple[str, str, str]] = field(default_factory=list)  # (device, port, vrf)


# ---------------------------------------------------------------------------
# Port-name normalisation (raw → NS conventions, with spaces)
# ---------------------------------------------------------------------------

# Interface type tokens that NS accepts. The matcher tries each pattern in
# order; the first hit yields the canonical type token, and whatever follows
# the matched prefix becomes the "number" portion (joined with a single space).
#
# NS validates port names against this family of standard Cisco interface
# types and REJECTS anything else with "Invalid from_port". Both full names and
# common abbreviations (Gi, Te, Fa, Lo, Po, Se, Tu, Vl ...) must therefore be
# canonicalised. Single-letter abbreviations use a (?=\d) lookahead so they only
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


def normalise_port_name(raw: str) -> str:
    """Convert a raw interface name into the NS convention (a single space
    between the type token and the number portion).

    NS only accepts standard Cisco interface type tokens; anything else is
    rejected by the engine.

    Examples:
        Ethernet1/1            -> Ethernet 1/1
        eth1/49                -> Ethernet 1/49     (NX-OS fabric ports)
        Gi0/0                  -> GigabitEthernet 0/0
        Vlan100                -> Vlan 100
        Lo0                    -> Loopback 0
    """
    raw = (raw or "").strip()
    if not raw:
        return raw

    for pat, canonical in _IFACE_TYPE_PATTERNS:
        m = pat.match(raw)
        if m:
            remainder = raw[m.end():].lstrip()
            return f"{canonical} {remainder}" if remainder else canonical

    return raw  # unknown form: leave as-is (NS may still reject it)


# ---------------------------------------------------------------------------
# Area / hierarchy layout
# ---------------------------------------------------------------------------

# Raw area names (before the build_area_layout `*_wp_` promotion) that denote a
# WAN / Internet / cloud / external waypoint area. Inter-area links that touch
# one of these are legitimate "device-to-waypoint" connections (RULE 3); links
# between two NON-waypoint areas are not allowed by the engine.
_RAW_WAYPOINT_AREAS = {"wan-isn", "wan", "internet", "cloud", "external"}


def _coerce_coord(value: Any) -> Optional[float]:
    """Return a canvas coordinate as float, or None when missing / non-numeric."""
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
    """Merge non-waypoint areas joined by a direct device-to-device L1 link,
    in place, to satisfy RULE 3.

    NS forbids a direct L1 link between two devices in different *non-waypoint*
    areas. A genuine inter-area link is modelled through a ``*_wp_`` waypoint;
    any plain device-to-device cable that straddles two non-waypoint areas is an
    over-eager split, so the RULE-3-correct fix is to put them in one area.
    Returns the number of devices reassigned.
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
      - Areas are placed left-to-right in a single outer row.
      - Within an area, device placement depends on ``layout``:
          * ``coordinate`` / ``auto`` — mirror canvas (x, y) positions via
            ``_place_by_coordinates``; both fall back to the tier layout for any
            area whose devices lack coordinates.
          * ``tier`` — keep each device on its RULE 0 tier row, ordering the
            left-right sequence inside each row for L1 crossing avoidance.
      - Waypoint areas become ``*_wp_`` so NS treats them as clouds.
    """
    by_area: Dict[str, List[NSDevice]] = {}
    for d in devices.values():
        by_area.setdefault(d.area, []).append(d)

    ordered_areas: List[str] = sorted(by_area.keys(), key=_area_sort_key)
    rendered_areas: List[str] = []
    name_map: Dict[str, str] = {}
    for a in ordered_areas:
        rendered = f"{a}_wp_" if a in _RAW_WAYPOINT_AREAS else a
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
        # Network Sketcher rule: a WayPoint area placed beside the main area(s)
        # must stack its devices VERTICALLY (one device per row, a single
        # column), not in a horizontal row. This deliberately overrides the
        # topology-derived placement for waypoint areas.
        if orig_area in _RAW_WAYPOINT_AREAS:
            area_to_grid[rendered] = [[d.name] for d in sorted(devs, key=lambda x: x.name)]
            continue
        if layout in ("auto", "coordinate"):
            grid = _place_by_coordinates(devs)
        if grid is None:
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
    """Number of intra-row links whose endpoints have >=1 device between them."""
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
    """Order one tier row to minimise same-row over-device crossings."""
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
    """Order devices within each tier row to minimise L1 lines drawn over a
    device (RULE 0.5). Rows are never reordered (RULE 0)."""
    rows: List[List[str]] = [list(r) for r in tier_rows if r]
    if not rows:
        return []

    names = {n for r in rows for n in r}
    adj: Dict[str, List[str]] = {
        n: [x for x in adjacency.get(n, ()) if x in names] for n in names
    }
    row_of: Dict[str, int] = {n: ri for ri, r in enumerate(rows) for n in r}

    edges: Set[Tuple[str, str]] = set()
    for n in names:
        for x in adj[n]:
            edges.add(tuple(sorted((n, x))))  # type: ignore[arg-type]
    intra: Dict[int, Set[Tuple[str, str]]] = defaultdict(set)
    for a, b in edges:
        if row_of[a] == row_of[b]:
            intra[row_of[a]].add((a, b))

    col: Dict[str, int] = {n: i for r in rows for i, n in enumerate(r)}

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
_COORD_CELL_DENSITY = 3.0


def _place_by_coordinates(devs: List[NSDevice]) -> Optional[List[List[str]]]:
    """Lay out one area's devices on a discrete grid mirroring their canvas
    coordinates. Returns None when any device lacks coordinates so the caller
    can fall back to the tier-based layout."""
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

    if xspan <= 0 and yspan <= 0:
        aspect = 1.0
    elif yspan <= 0:
        aspect = float(n)
    elif xspan <= 0:
        aspect = 1.0 / float(n)
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

    for d in sorted(devs, key=lambda d: (float(d.y), float(d.x), d.name)):  # type: ignore[arg-type]
        r = _bin(float(d.y), ymin, yspan, rows)   # type: ignore[arg-type]
        c = _bin(float(d.x), xmin, xspan, cols)   # type: ignore[arg-type]
        if grid[r][c] is None:
            grid[r][c] = d.name
            continue
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
            for rr in range(len(grid)):
                grid[rr].append(None)
            grid[r][-1] = d.name

    used_rows = [row for row in grid if any(cell is not None for cell in row)]
    if not used_rows:
        return []
    width = max(len(row) for row in used_rows)
    padded = [row + [None] * (width - len(row)) for row in used_rows]
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
    # WAN / external clouds go first (left), then site/tenant areas, 'default' last.
    if area in _RAW_WAYPOINT_AREAS:
        return (0, area)
    m = re.match(r"site(\d+)", area)
    if m:
        return (1, f"{int(m.group(1)):03d}")
    if area == "default":
        return (9, area)
    return (5, area)


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
