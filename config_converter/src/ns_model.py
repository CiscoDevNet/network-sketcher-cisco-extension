# Copyright 2026 Cisco Systems, Inc. and its affiliates
# SPDX-License-Identifier: Apache-2.0

"""Network Sketcher intermediate model + reusable helpers.

BASED ON (but, as of DESIGN.md section 4.7.1 / section 8.1 decisions 2+6, NO
LONGER byte-identical to) ``template_converter/src/ns_model.py`` per
``template_converter/GUIDE.md`` ("What's copy-verbatim vs. what's
platform-specific"). This module is otherwise the converter-agnostic half of
the Network Sketcher pipeline, shared across every converter in this repo
(aci_converter / catc_converter / nd_converter / 3rd_party/netbox_converter /
template_converter) so that any mapper emits the *same* ``NSModel`` and the
single ``ns_command_builder`` can serialise any of them identically. The
``stencil_mapper`` import below is repointed from ``platform_stencil_mapper``
to this converter's own ``stencil_mapper`` module, same as every other
converter's copy.

**Intentional, DOCUMENTED deviation from the "copy verbatim" contract**
(DESIGN.md 4.7.1): ``_area_sort_key()`` and ``build_area_layout()`` gained an
``isolated_area_name`` parameter so requirement G's dedicated closed-device
area (``cfg['isolated_area_name']``, default ``"isolated"``) always sorts to
the rightmost position in the BOTTOM (non-waypoint) area row — strictly after
the ``default`` bucket. As of the R-WP-TOP change (DESIGN.md 4.7.1, section 5)
``build_area_layout()`` also promotes WAN/Internet/cloud/external WayPoint
(``*_wp_``) areas to their OWN row ABOVE the bottom row (with their devices
spread horizontally so multiple clouds render side-by-side), instead of the
old single row where a waypoint sat as a column to the LEFT of ``default``.
This is a config_converter-LOCAL change only:

  * The other five copies of this file (aci_converter / catc_converter /
    nd_converter / 3rd_party/netbox_converter / template_converter) are NOT
    touched by this change and keep their original, unmodified
    ``_area_sort_key()`` — none of those converters has a requirement-G-style
    "always-last dedicated area" concept, so there is nothing for them to
    gain from this addition today.
  * The new parameter defaults to ``"isolated"`` (matching
    ``config_converter_to_ns_config.json``'s own default) so every existing
    call site that does not pass it keeps its original sort behaviour for
    every area name other than the literal string ``"isolated"``.
  * If a future converter needs the same "always-last area" concept, port
    this same parameter (and this docstring note) into its own copy rather
    than trying to re-establish byte-identity across all six files — the
    six copies are allowed to diverge in this one, specific, documented way.

**Second intentional, DOCUMENTED deviation** (DESIGN.md 4.2.1 item 16):
``_IFACE_TYPE_PATTERNS`` gained a dedicated ``^MgmtEth`` entry (IOS-XR's
``MgmtEth<slot>/RP<n>/CPU<n>/<port>`` management interface naming), and
``normalise_port_name()`` gained the ``_numericise_path_segments()`` helper
that it applies ONLY to that canonical type, to eliminate a live-engine
``Could not convert '<segment>' to integer`` port-name sort-key warning that
IOS-XR's alpha+digit ``RP0``/``CPU0`` path segments would otherwise trigger
(confirmed via MCP live-engine verification, config_converter has no other
OS family with this naming shape). Same rule as above applies: this is a
config_converter-LOCAL change only, the other five copies of this file keep
their original, unmodified ``_IFACE_TYPE_PATTERNS``/``normalise_port_name()``
since none of them target IOS-XR configs today.

Do NOT add any OTHER config_converter-specific logic here — anything specific
to parsing running-configs, inferring L1 links from IP subnets, WAN
detection, or closed-environment DETECTION (as opposed to this file's
narrow layout-ordering concern) belongs in ``config_parser.py`` /
``topology_mapper.py`` (see ``config_converter/DESIGN.md`` sections B/C/D/E/F/
G/H for the full design rationale). This file otherwise contains ONLY the
reusable pieces:

  * the ``NS*`` dataclasses (the model contract ``ns_command_builder`` depends on),
  * ``normalise_port_name`` (Cisco interface-name -> NS canonical form),
  * ``build_area_layout`` + its placement helpers (tier / coordinate layout),
  * ``_coalesce_directly_linked_areas`` (RULE 3 area-merge),
  * ``model_to_dict`` (JSON serialisation for the debug artifact).

Layout policy (RULE 0 -- vertical tier hierarchy):
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

from .stencil_mapper import StencilMapping


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
# Port-name normalisation (raw -> NS conventions, with spaces)
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
    # IOS-XR management interface (config_converter extension, DESIGN.md
    # 4.2.1 Phase 1c / item 16) -- "MgmtEth<slot>/RP<n>/CPU<n>/<port>" (e.g.
    # "MgmtEth0/RP0/CPU0/0") is its OWN distinct interface-type token, NOT a
    # bare "Mgmt"/"mgmt" prefix. This MUST be checked BEFORE the generic
    # "^Mgmt" pattern below (same precedent as "Bundle-Ether" vs. bare "Bu"):
    # without this dedicated pattern, the generic "^Mgmt" match strips only
    # the 4-char "Mgmt" prefix and leaves the remaining "Eth0/RP0/CPU0/0" text
    # glued onto the number portion (producing the malformed "mgmt
    # Eth0/RP0/CPU0/0"). Matching the whole "MgmtEth" token fixes the FIRST
    # sort-key segment, but the engine's port-name sort key actually validates
    # EVERY "/"-separated segment as an integer -- "RP0"/"CPU0" further down
    # the path still fail the same way, so `normalise_port_name()` below ALSO
    # runs `_numericise_path_segments()` on the remainder for this specific
    # canonical type to eliminate the warning completely (see that helper's
    # docstring for the full rationale).
    (re.compile(r"^MgmtEth", re.IGNORECASE), "MgmtEth"),
    (re.compile(r"^Mgmt", re.IGNORECASE), "mgmt"),
    (re.compile(r"^mgmt", re.IGNORECASE), "mgmt"),
    (re.compile(r"^Loopback", re.IGNORECASE), "Loopback"),
    (re.compile(r"^Loop(?=\d)", re.IGNORECASE), "Loopback"),
    (re.compile(r"^Lo(?=\d)", re.IGNORECASE), "Loopback"),
    (re.compile(r"^Vlan", re.IGNORECASE), "Vlan"),
    (re.compile(r"^Vl(?=\d)", re.IGNORECASE), "Vlan"),
    (re.compile(r"^Port-?channel", re.IGNORECASE), "Port-channel"),
    (re.compile(r"^Po(?=\d)", re.IGNORECASE), "Port-channel"),
    # IOS-XR LAG interface (config_converter extension, DESIGN.md 4.2.1 Phase
    # 1c) -- confirmed via MCP live-engine verification (Phase 1e) that NS
    # rejects the un-normalised "Bundle-Ether1" form ("Invalid to_port"/
    # "Invalid from_port") but accepts "Bundle-Ether 1".
    (re.compile(r"^Bundle-?Ether", re.IGNORECASE), "Bundle-Ether"),
    (re.compile(r"^BE(?=\d)", re.IGNORECASE), "Bundle-Ether"),
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


def _numericise_path_segments(remainder: str) -> str:
    """Strip non-digit characters out of each ``/``-separated path segment.

    Live-engine verification (Phase 1e/5, DESIGN.md 4.2.1 item 16) showed the
    Network Sketcher engine's port-name sort key does NOT stop at the first
    "type + number" token: it walks EVERY ``/``-separated segment of the
    remainder and calls ``int()`` on each one, emitting a non-fatal
    ``Could not convert '<segment>' to integer`` warning (device_table_html
    export) for every segment that fails. Simply moving the interface-type
    boundary earlier (e.g. matching the whole "MgmtEth" token, see
    ``_IFACE_TYPE_PATTERNS`` above) only fixes the FIRST segment -- IOS-XR's
    ``MgmtEth<slot>/RP<n>/CPU<n>/<port>`` naming has two more alpha+digit
    segments ("RP0", "CPU0") deeper in the path that still fail the same way.
    Rather than truncating that identifying information outright (which could
    make two management interfaces on a dual-RP chassis collide, e.g. RP0 vs
    RP1), this helper keeps only the DIGITS of each segment (dropping the
    "RP"/"CPU" letters) so every segment becomes a plain, sort-key-friendly
    integer while still preserving the distinguishing index numbers positionally
    (``RP0`` -> ``0``, ``RP1`` -> ``1``, etc.). A segment with no digits at all
    (not expected in practice) falls back to ``"0"`` rather than an empty
    string, so the path shape (segment count) is always preserved.
    """
    segments = remainder.split("/")
    numeric_segments = [re.sub(r"\D", "", seg) or "0" for seg in segments]
    return "/".join(numeric_segments)


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
        MgmtEth0/RP0/CPU0/0    -> MgmtEth 0/0/0/0    (IOS-XR, see
                                   _numericise_path_segments docstring for why
                                   "RP0"/"CPU0" are reduced to their digits)
    """
    raw = (raw or "").strip()
    if not raw:
        return raw

    for pat, canonical in _IFACE_TYPE_PATTERNS:
        m = pat.match(raw)
        if m:
            remainder = raw[m.end():].lstrip()
            if canonical == "MgmtEth" and remainder:
                remainder = _numericise_path_segments(remainder)
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
    isolated_area_name: str = "isolated",
) -> Tuple[List[List[str]], Dict[str, List[List[str]]]]:
    """Return (area_layout, area_to_device_grid).

    Strategy:
      - Non-waypoint areas are placed left-to-right in the BOTTOM outer row.
      - Waypoint (``*_wp_``) areas are placed on a SEPARATE row ABOVE the
        bottom row (DESIGN.md 4.7.1, section 5 risk R-WP-TOP). When no waypoint
        area exists the layout collapses to a single bottom row, byte-identical
        to the pre-change behaviour. Rows may be ragged (unequal length); the
        NS engine tolerates that in ``add area_location`` and rejects the
        ``_AIR_`` spacer there, so short rows are left short, never padded.
      - Within a NON-waypoint area, device placement depends on ``layout``:
          * ``coordinate`` / ``auto`` — mirror canvas (x, y) positions via
            ``_place_by_coordinates``; both fall back to the tier layout for any
            area whose devices lack coordinates.
          * ``tier`` — keep each device on its RULE 0 tier row, ordering the
            left-right sequence inside each row for L1 crossing avoidance.
      - Waypoint areas become ``*_wp_`` so NS treats them as clouds, and their
        devices are spread HORIZONTALLY (a single row) so multiple waypoint
        clouds render side-by-side across the top.
      - ``isolated_area_name`` (DESIGN.md 4.7.1, requirement G — pass
        ``cfg['isolated_area_name']`` here) is always sorted to the rightmost
        position of the BOTTOM row, after every other area including
        ``default`` — see the module docstring's "intentional deviation" note
        and ``_area_sort_key()`` below. It is rendered as a normal
        (non-WayPoint) tiered grid area like any other, only its horizontal
        position differs.
    """
    by_area: Dict[str, List[NSDevice]] = {}
    for d in devices.values():
        by_area.setdefault(d.area, []).append(d)

    ordered_areas: List[str] = sorted(
        by_area.keys(), key=lambda a: _area_sort_key(a, isolated_area_name)
    )
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
        # Network Sketcher rule (DESIGN.md 4.7.1, section 5 risk R-WP-TOP): a
        # WayPoint area is now placed on its OWN row ABOVE the main area(s)
        # (see area_layout assembly below), not as a column to the LEFT of
        # `default`. The waypoint devices inside that single WayPoint area are
        # laid out HORIZONTALLY -- one row, multiple columns, sorted by name
        # for determinism -- so multiple WAN/Internet/cloud clouds render
        # side-by-side across the top. This was verified against the live NS
        # engine via the MCP server: `add device_location "['wan_wp_',
        # [['CL1','CL2']]]"` places CL1/CL2 side-by-side inside the one
        # waypoint area (the engine drops the auto-created placeholder
        # waypoint). This deliberately overrides the topology-derived
        # placement for waypoint areas.
        if orig_area in _RAW_WAYPOINT_AREAS:
            area_to_grid[rendered] = [[d.name for d in sorted(devs, key=lambda x: x.name)]]
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

    # Area layout assembly (DESIGN.md 4.7.1, section 5 risk R-WP-TOP): WayPoint
    # (`*_wp_`) areas are emitted on their OWN row ABOVE the non-waypoint areas,
    # instead of the old single row where a waypoint sat as a column to the LEFT
    # of `default`. The non-waypoint (bottom) row keeps its existing left-to-
    # right ordering (site* -> named -> default -> isolated, via
    # `_area_sort_key`). Ragged rows are intentional and safe: the live NS
    # engine (verified via the MCP server) accepts an `add area_location` grid
    # whose rows differ in length and REJECTS the `_AIR_` spacer here (that
    # placeholder is only valid inside `add device_location`), so shorter rows
    # must simply be left short -- never padded. When there is NO waypoint area
    # the layout collapses back to the original single bottom row (no empty top
    # row is emitted). config_converter only ever produces a single waypoint
    # area ("wan" -> "wan_wp_"), so the top row never places two waypoint-only
    # areas horizontally adjacent (an engine constraint); multiple waypoint
    # *devices* live inside that one area and are spread horizontally by the
    # per-area grid above.
    wp_row = [r for r in rendered_areas if r.endswith("_wp_")]
    main_row = [r for r in rendered_areas if not r.endswith("_wp_")]
    if wp_row:
        area_layout = [wp_row, main_row]
    else:
        area_layout = [main_row]
    return area_layout, area_to_grid


# Cap on row width for the exhaustive ordering search; above this we fall back
# to a deterministic hill-climb (permutations would be too many).
_ROW_PERM_LIMIT = 8
_ROW_HILLCLIMB_ITERS = 4000

# Phase A (DESIGN.md 4.7.1 / section 5): cap on the number of up/down barycentre
# sweeps in ``_place_columns`` before we stop even if the row orders have not
# reached a fixed point. In practice convergence (an already-seen state) is hit
# in far fewer passes; this only bounds pathological oscillation.
_MAX_PLACE_SWEEPS = 20


def _row_between_cost(order: Sequence[str], intra_edges: Set[Tuple[str, str]]) -> int:
    """Number of intra-row links whose endpoints have >=1 device between them."""
    pos = {n: i for i, n in enumerate(order)}
    cost = 0
    for a, b in intra_edges:
        if abs(pos[a] - pos[b]) > 1:
            cost += 1
    return cost


def _row_wire_length(
    order: Sequence[str], inter_cols: Dict[str, List[float]]
) -> float:
    """Total Manhattan column distance of this row's CROSS-ROW edges.

    Phase A wire-length term (DESIGN.md 4.7.1 / section 5, "wire-length
    tiebreak"): ``inter_cols[n]`` holds the current column indices of ``n``'s
    neighbours that live in OTHER rows. For a candidate ``order`` this returns
    ``Σ |col(n) - col(neighbour)|`` over every such cross-row edge, i.e. the
    horizontal length of the lines that will be drawn between this row and the
    rows above/below it. Minimising it pulls connected devices into adjacent
    columns. Same-row edges are handled separately by ``_row_between_cost``.
    """
    pos = {n: i for i, n in enumerate(order)}
    total = 0.0
    for n in order:
        p = pos[n]
        for c in inter_cols.get(n, ()):  # neighbour columns in other rows
            total += abs(p - c)
    return total


def _order_row(
    devices: List[str],
    intra_edges: Set[Tuple[str, str]],
    barycentre: Dict[str, float],
    inter_cols: Optional[Dict[str, List[float]]] = None,
) -> List[str]:
    """Order one tier row to minimise same-row over-device crossings, then
    (Phase A) cross-row wire length.

    Objective key (lexicographic, so each term only breaks ties of the one
    before it -- existing good small cases therefore never regress on the
    PRIMARY crossing count):
      1. ``_row_between_cost``  -- same-row over-device crossings (PRIMARY,
         unchanged from the pre-Phase-A behaviour);
      2. ``_row_wire_length``   -- Σ|Δcol| of cross-row edges (Phase A
         secondary objective, pulls connected devices into nearer columns);
      3. barycentre offset      -- the original tie-break, kept as a further
         tie-break so behaviour is unchanged whenever 1+2 already tie;
      4. the device-name sequence -- final deterministic tie-break.
    """
    if len(devices) <= 1:
        return list(devices)

    inter_cols = inter_cols or {}
    base = sorted(devices, key=lambda n: (barycentre.get(n, 0.0), n))
    if not intra_edges and not any(inter_cols.get(n) for n in devices):
        return base

    def bary_offset(order: Sequence[str]) -> float:
        return sum(abs(i - barycentre.get(n, float(i))) for i, n in enumerate(order))

    def full_key(order: Sequence[str]) -> Tuple[int, float, float, Tuple[str, ...]]:
        return (
            _row_between_cost(order, intra_edges),
            _row_wire_length(order, inter_cols),
            bary_offset(order),
            tuple(order),
        )

    if len(devices) <= _ROW_PERM_LIMIT:
        best_order = base
        best_key = full_key(base)
        for perm in itertools.permutations(base):
            key = full_key(perm)
            if key < best_key:
                best_key = key
                best_order = list(perm)
        return list(best_order)

    # Wide row: deterministic hill-climb on (crossings, wire-length). The
    # seeded Random(1234) keeps the swap sequence -- and therefore the output
    # -- identical for identical input (determinism requirement).
    cur = list(base)
    cur_cost = (_row_between_cost(cur, intra_edges), _row_wire_length(cur, inter_cols))
    rng = random.Random(1234)
    for _ in range(_ROW_HILLCLIMB_ITERS):
        if cur_cost[0] == 0 and cur_cost[1] == 0.0:
            break
        i, j = rng.sample(range(len(cur)), 2)
        cur[i], cur[j] = cur[j], cur[i]
        new_cost = (_row_between_cost(cur, intra_edges), _row_wire_length(cur, inter_cols))
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
    device (RULE 0.5) and, as a Phase A secondary objective, the total
    cross-row wire length. Rows are NEVER reordered vertically (RULE 0 tier
    hierarchy is a hard constraint) -- only the column order within each row
    changes."""
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

    # Phase A (DESIGN.md 4.7.1 / section 5): a BIDIRECTIONAL, converging sweep
    # replaces the old fixed 4-pass one-directional loop. Each pass reorders
    # every row against its neighbours' CURRENT columns; alternating the pass
    # direction (top->bottom, then bottom->top) lets column decisions
    # propagate both ways so cross-row Manhattan distance actually settles.
    # Fully deterministic: we stop as soon as a whole-grid state repeats (a
    # fixed point or a cycle -- ``seen`` catches both), or after
    # ``_MAX_PLACE_SWEEPS`` passes, whichever comes first.
    seen: Set[Tuple[Tuple[str, ...], ...]] = set()
    for sweep in range(_MAX_PLACE_SWEEPS):
        top_down = sweep % 2 == 0
        row_indices = range(len(rows)) if top_down else range(len(rows) - 1, -1, -1)
        for ri in row_indices:
            row = rows[ri]
            barycentre = {
                n: (statistics.median([col[x] for x in adj[n] if row_of[x] != ri])
                    if any(row_of[x] != ri for x in adj[n]) else float(col[n]))
                for n in row
            }
            # Per-device neighbour columns in OTHER rows -> the Phase A
            # wire-length term (endpoints in DIFFERENT rows only).
            inter_cols = {
                n: [float(col[x]) for x in adj[n] if row_of[x] != ri]
                for n in row
            }
            ordered = _order_row(row, intra.get(ri, set()), barycentre, inter_cols)
            rows[ri] = ordered
            for i, n in enumerate(ordered):
                col[n] = i
        state = tuple(tuple(r) for r in rows)
        if state in seen:
            break
        seen.add(state)

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


def _area_sort_key(area: str, isolated_area_name: str = "isolated") -> Tuple[int, str]:
    # WAN / external clouds bucket first (0), then site/tenant areas, 'default',
    # then the dedicated requirement-G isolated area LAST of all (rightmost) —
    # see the module docstring's "intentional deviation from copy-verbatim"
    # note (DESIGN.md 4.7.1, section 8.1 decisions 2+6). Bucket 10 sorts after
    # bucket 9 ('default'), so the isolated area is always the last column
    # regardless of how many other named areas exist. NOTE: bucket 0 now only
    # controls the ordering of waypoint areas *within the dedicated top row*
    # (build_area_layout splits the `*_wp_` areas out into their own row above
    # the main areas, DESIGN.md 4.7.1 R-WP-TOP); it no longer left-anchors them
    # in the same row as `default`. The remaining buckets order the bottom row.
    if area in _RAW_WAYPOINT_AREAS:
        return (0, area)
    m = re.match(r"site(\d+)", area)
    if m:
        return (1, f"{int(m.group(1)):03d}")
    if area == "default":
        return (9, area)
    if isolated_area_name and area == isolated_area_name:
        return (10, area)
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
