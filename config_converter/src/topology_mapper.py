# Copyright 2026 Cisco Systems, Inc. and its affiliates
# SPDX-License-Identifier: Apache-2.0

"""Build an NSModel from a collection of ParsedConfig objects.

See ``config_converter/DESIGN.md`` sections 4.3-4.7 (requirements C, D, E,
F, G) for the full algorithm design; section 4.8 (requirement H) is
implemented here too since WAN classification feeds directly into how an
unmatched interface is turned into a placeholder device (requirement E) or a
shared WAN/Internet cloud waypoint.

Pipeline this module is responsible for (DESIGN.md section 3, Phase 2-6):

    Phase 2  infer_l1_links_from_subnets()   requirement C
    Phase 3  score_wan_interfaces()          requirement H
    Phase 4  detect_closed_environments()    requirement G
    Phase 6  build_model()                   requirements D, E, F
             (delegates tier/area placement to a ported version of
             3rd_party/netbox_converter/src/netbox_layout.py — see the
             ``_TIER`` / tier-keyword TODOs below)

**Phase 4 status (DESIGN.md section 6 roadmap)**: ``build_model()`` is a
WORKING, verified pipeline that wires up requirements C through H in full:
  - Requirement C: k==1/k==2 subnet matching incl. the L2-switch-inference
    gate, k>=3's full DESIGN.md 4.3.6 priority chain (real-hub detection ->
    ``shared_subnet_strategy`` ('best_pair' Blossom/brute-force-derived
    single-best-pair, or 'synthetic_switch') -> the
    ``max_candidates_for_brute_force`` scalability safety valve -- see
    ``find_real_hub()`` / ``match_subnet_group()``), AND cross-site RFC1918
    subnet-reuse exclusion (``cfg['site_scoping']`` + ``site_hint`` derived
    from ``ParsedConfig.source_filename``, DESIGN.md 4.3.9 -- see
    ``_site_hint_from_source()``).
  - Requirement D: full netbox_layout-derived tier/area placement (ported to
    ``layout.py``) via ``compute_tiers_and_areas()``.
  - Requirement E: ``Dummy_<TC>_<n>`` peer/L2-switch synthesis via
    ``synthesize_inferred_peers()``.
  - Requirement F: the full-connectivity guarantee (``assume_fully_
    connected`` defaults True) via ``ensure_full_connectivity()``.
  - Requirement G: closed-environment detection (``detect_closed_
    environments()``) -- shutdown interfaces (certain) and bidirectional
    deny-all ACL pairs (high confidence, only when both ACLs resolve
    locally) drive a per-device "fully closed" verdict; every such device
    is routed to the single dedicated ``cfg['isolated_area_name']`` area
    and every link (real, L2-switch-star, inferred-peer, or full-
    connectivity) that would cross that area's boundary is dropped (DESIGN.
    md 4.7.1) via ``_drop_cross_isolation_links()``. Null0 black-hole routes
    and routing-protocol absence are DELIBERATELY NOT implemented as
    device-level signals (DESIGN.md 4.7 itself scopes Null0 to a single
    route, and forbids using routing-protocol absence alone) -- documented
    limitation, not a TODO.
  - Requirement H: fully config-driven WAN scoring (``score_wan_
    interface()`` / ``compute_wan_scores()``).
Stencil mapping for every real device is computed as a pre-pass, BEFORE
requirement C runs, so k>=3 tie-breaking can consult each candidate's
Stencil tier (DESIGN.md 4.3.4 rule 3).

**Phase 5 status**: pipeline step 8 (per-device VLAN/SVI/port-channel/
sub-interface/IP/VRF population, ``apply_parsed_configs()``, ported from
cml_converter) is now implemented and wired up as the final step of
``build_model()``. A Phase-5 fix discovered via live Network Sketcher MCP
verification is also wired in immediately before it:
``synthesize_portchannel_member_links()`` gives every LAG/port-channel
physical member interface its own L1 link (mirroring the peer already
established for the logical Port-channel/Bundle-Ether interface), because
the live engine's ``add portchannel_bulk`` requires every member port to
already exist as an L1 interface, and a channel-group member is never a
subnet-matching candidate on its own (see that function's docstring for
the full rationale).

What remains INTENTIONALLY UNIMPLEMENTED (see each function's own
docstring for specifics):
  - The EtherChannel/LAG member-pairing algorithm (DESIGN.md 4.3.6's
    "メンバーポートの対向ペアリングアルゴリズム") is NOT implemented; a
    Port-channel/Bundle-Ether candidate is still matched/unmatched as one
    plain interface for requirement C/E purposes, not expanded per
    physical member for TOPOLOGY INFERENCE (``synthesize_portchannel_
    member_links()`` above only makes existing member ports bindable for
    ``add portchannel_bulk`` by reusing the logical interface's already-
    resolved peer -- it does not attempt to independently discover or
    verify each member's own peer-side physical port).
  - The cross-site exclusion's "distinguishing evidence" check only
    consults description-field hints; NAT-domain comparison (DESIGN.md
    4.3.6 mitigation 3) is a documented, deliberately-unimplemented
    refinement (see ``match_subnet_group()``'s own docstring).

All 13 previously-open design questions in DESIGN.md section 8 are now
CONFIRMED decisions (see DESIGN.md section 9 for the consolidated summary).
The ones most relevant to this module: requirement F defaults to
"exhaustive" full connectivity; requirement G's closed devices are always
routed to one dedicated isolated area (never merely flagged in place);
requirement C gained a new "L2 switch inference gate"
(``needs_l2_switch_inference()`` below); k>=3 (and gated k==2) shared
subnets prefer a real config-identified hub, then fall back to
``shared_subnet_strategy``; ``networkx``'s Blossom algorithm
(``min_weight_matching``) is the default, preferred matching engine for both
this module's subnet matching and the ported netbox_layout tier/area logic;
and every inferred device uses the fixed ``Dummy_<2-letter-type-code>_<n>``
naming grammar (interfaces: ``Dummy <n>``) — see DESIGN.md section 4.5.1.
This naming grammar's ``Dummy <n>`` port names were live-verified against
the Network Sketcher engine in Phase 1e (DESIGN.md section 5 risk #12): they
are accepted as-is, no fallback naming scheme is needed.
"""
from __future__ import annotations

import csv
import ipaddress
import itertools
import re
from collections import defaultdict
from dataclasses import dataclass, field, replace as _dc_replace
from typing import Dict, List, Optional, Set, Tuple

try:
    import networkx as nx
except ImportError:  # pragma: no cover -- stdlib-only fallback (DESIGN.md 4.3.4)
    nx = None  # type: ignore[assignment]

from . import layout
from .config_parser import AclDefinition, IPv4Addr, ParsedConfig, ParsedInterface
from .ns_model import (
    NSDevice, NSIPAssignment, NSL1Link, NSL2Segment, NSModel,
    NSPortChannel, NSSubInterface, NSVirtualPort, build_area_layout,
    normalise_port_name,
)
from . import stencil_mapper as sten

# RULE 0 vertical tier per stencil type — copied from
# template_converter/src/platform_mapper.py.template's ``_TIER`` (also used,
# under the name ``_TIER``, by 3rd_party/netbox_converter/src/netbox_mapper.py
# as the ``stencil_tiers`` input to netbox_layout.calculate_integrated_tier).
_TIER = {
    sten.NS_CLOUD: 0,
    sten.NS_ROUTER: 1, sten.NS_FIREWALL: 1,
    sten.NS_WLC: 2,
    sten.NS_L3SWITCH: 3,
    sten.NS_SWITCH: 4, sten.NS_AP: 4,
    sten.NS_SERVER: 5, sten.NS_PC: 5, sten.NS_PHONE: 5,
}

# Purely-synthesised placeholder / WayPoint colour (this repo's "inferred,
# no real record behind it" convention — see template_converter/GUIDE.md
# "Observed vs. inferred WayPoints"). Every device requirements E/F/H invent
# MUST use this colour; never the observed-WayPoint light blue (220,230,242).
GRAY = (200, 200, 200)


# ---------------------------------------------------------------------------
# Data structures used only within this module (not part of the shared
# ns_model.py contract) — working state for requirement C's matching problem.
# ---------------------------------------------------------------------------

@dataclass
class SubnetCandidate:
    """One interface's IP that participates in a same-subnet candidate group
    (DESIGN.md section 4.3.1)."""
    device: str
    interface: str
    addr: Optional[IPv4Addr] = None
    other_subnet_degree: int = 0       # DESIGN.md 4.3.2 "degree(x)"
    site_hint: Optional[str] = None    # populated when site_scoping is enabled (DESIGN.md 4.3.6 NAT mitigation)
    network: str = ""                  # bookkeeping only: this candidate's SubnetGroup.network (for config_excluded_links.csv)
    iface_obj: Optional[ParsedInterface] = None  # direct reference (Phase 2: real-hub detection + description-hint tie-break, DESIGN.md 4.3.4/4.3.6)


@dataclass
class SubnetGroup:
    """All candidates sharing one network address + prefix length."""
    network: str                        # e.g. '192.0.2.0/30'
    prefix_len: int = 32                # cached from `network` for large_subnet_prefix_threshold comparisons (DESIGN.md 4.3.3)
    candidates: List[SubnetCandidate] = field(default_factory=list)
    has_virtual_ip: bool = False        # True if any HSRP/VRRP/GLBP virtual IP was seen in this subnet (DESIGN.md 4.3.3)


@dataclass
class MatchResult:
    """Outcome of resolving one SubnetGroup (DESIGN.md 4.3.3/4.3.6)."""
    linked_pairs: List[Tuple[SubnetCandidate, SubnetCandidate]] = field(default_factory=list)
    unmatched: List[SubnetCandidate] = field(default_factory=list)
    strategy_used: str = ""             # 'direct' | 'real_hub' | 'brute_force' | 'blossom' | 'synthetic_switch'
    ambiguous: bool = False             # True if a tie-break rule had to decide (DESIGN.md 4.3.4)
    l2_switch_inferred: bool = False    # True if this group synthesized a Dummy_L2_<n> (DESIGN.md 4.3.3/4.3.6)
    excluded_reason: Optional[str] = None  # non-None -> this group's link(s) were NOT emitted and were written to config_excluded_links.csv instead (DESIGN.md 4.3.9)
    excluded_confidence: float = 0.5    # populated alongside excluded_reason; feeds the CSV's 'confidence' column (DESIGN.md 4.3.9) -- no fixed formula is mandated, a low-to-medium default is acceptable


# ---------------------------------------------------------------------------
# Requirement C — same-subnet connectivity inference + degree-similarity
# matching (DESIGN.md section 4.3)
# ---------------------------------------------------------------------------

def _site_hint_from_source(source_filename: Optional[str]) -> Optional[str]:
    """Derive a DESIGN.md 4.3.6/4.3.9 ``site_hint`` from a ``ParsedConfig.
    source_filename`` label.

    ``convert.py``'s ``_load_config_directory()`` only ever prefixes a label
    with ``"<site>/<stem>"`` (POSIX-style, one level) when ``cfg
    ['site_scoping']`` is True AND the file lives one directory level below
    the input root -- when ``site_scoping`` is False (the default) or the
    file is at the top level, the label never contains a ``/`` and this
    always returns None. This means every call site can unconditionally
    populate ``site_hint`` from this helper without checking the config flag
    itself first: the flag's effect is already baked into whether a ``/``
    ever appears in ``source_filename`` at all, so behaviour is unchanged
    when ``site_scoping`` is disabled.
    """
    if not source_filename or "/" not in source_filename:
        return None
    return source_filename.split("/", 1)[0]


def build_subnet_groups(configs: Dict[str, ParsedConfig], cfg: Dict) -> List[SubnetGroup]:
    """Group every routed interface IP (across all devices) by network
    address + prefix length (DESIGN.md 4.3.1).

    Excludes HSRP/VRRP/GLBP virtual IPs from the candidate pool (their
    presence still sets ``SubnetGroup.has_virtual_ip``, DESIGN.md 4.3.3
    decision 3), shutdown interfaces, loopbacks, and /32 host addresses.

    Phase 4 (DESIGN.md 4.3.6/4.3.9): ``site_hint`` is now populated from
    ``ParsedConfig.source_filename`` via ``_site_hint_from_source()`` --
    ``match_subnet_group()`` uses it to detect and exclude cross-site
    RFC1918 subnet reuse when ``cfg['site_scoping']`` is enabled.
    """
    ignore_virtual = cfg.get("ignore_virtual_ips", True)
    groups: Dict[str, SubnetGroup] = {}

    for device, parsed in configs.items():
        site_hint = _site_hint_from_source(parsed.source_filename)
        for ifname, iface in parsed.interfaces.items():
            if iface.shutdown or iface.kind == "loopback":
                continue
            has_vip = bool(iface.virtual_ips)
            for addr in list(iface.ipv4) + list(iface.ipv4_secondary):
                if addr.prefix >= 32:
                    continue
                try:
                    network = ipaddress.IPv4Network(f"{addr.address}/{addr.prefix}", strict=False)
                except ValueError:
                    continue
                key = str(network)
                group = groups.get(key)
                if group is None:
                    group = SubnetGroup(network=key, prefix_len=addr.prefix)
                    groups[key] = group
                group.candidates.append(SubnetCandidate(
                    device=device, interface=ifname, addr=addr, network=key,
                    iface_obj=iface, site_hint=site_hint,
                ))
                if has_vip and ignore_virtual:
                    group.has_virtual_ip = True

    return list(groups.values())


def needs_l2_switch_inference(group: SubnetGroup, cfg: Dict) -> bool:
    """Requirement-C "L2 switch inference gate" (DESIGN.md section 4.3.3,
    confirmed design per section 8.1 decision 3) — decides whether ``group``
    should be represented as a shared LAN segment (a single L2 switch, real
    or inferred, hubbing every candidate) rather than a plain point-to-point
    link, even when it currently has only 2 real candidates.

    Returns True (i.e. "an L2 switch belongs here") iff ANY of:
      - len(group.candidates) >= 3, OR
      - group.has_virtual_ip is True (HSRP/VRRP/GLBP seen in this subnet), OR
      - group.prefix_len <= cfg['large_subnet_prefix_threshold'] (default 24
        -- i.e. /24, /23, /22, ... have far more free addresses than a
        typical /30-/29 point-to-point link and likely have a real switch
        behind them).
    Returns False only for a plain 2-candidate group with no virtual IP and a
    prefix length above the threshold (e.g. a /30 or /31 point-to-point
    link) -- that case stays a direct, non-L2 link with no inferred switch.
    Honours ``cfg['l2_inference_enabled']`` -- if False, always returns False
    (restores the pre-decision-3 direct-link-only behaviour).
    """
    if not cfg.get("l2_inference_enabled", True):
        return False
    if len(group.candidates) >= 3:
        return True
    if group.has_virtual_ip:
        return True
    threshold = cfg.get("large_subnet_prefix_threshold", 24)
    if group.prefix_len <= threshold:
        return True
    return False


def compute_other_subnet_degree(configs: Dict[str, ParsedConfig]) -> Dict[Tuple[str, str], int]:
    """Return {(device, interface): degree} where degree = the number of
    OTHER subnet groups that (device, interface)'s device participates in
    (DESIGN.md 4.3.2's cost-function input ``degree(x)``).

    Counts physical/SVI/subif/portchannel/mgmt/tunnel ports with a routed
    IPv4 address (shutdown and loopback interfaces excluded, mirroring
    build_subnet_groups()'s own candidacy rules) -- this answers "how many
    OTHER subnet-group memberships does this interface's device have",
    which is what DESIGN.md 4.3.4's best_pair tie-break needs (a device with
    many other subnet memberships is more likely to be a core/aggregation
    device than an access-layer leaf).
    """
    device_subnets: Dict[str, Set[str]] = defaultdict(set)
    iface_subnets: Dict[Tuple[str, str], Set[str]] = defaultdict(set)

    for device, parsed in configs.items():
        for ifname, iface in parsed.interfaces.items():
            if iface.shutdown or iface.kind == "loopback":
                continue
            for addr in list(iface.ipv4) + list(iface.ipv4_secondary):
                if addr.prefix >= 32:
                    continue
                try:
                    network = ipaddress.IPv4Network(f"{addr.address}/{addr.prefix}", strict=False)
                except ValueError:
                    continue
                key = str(network)
                device_subnets[device].add(key)
                iface_subnets[(device, ifname)].add(key)

    degree: Dict[Tuple[str, str], int] = {}
    for (device, ifname), own_subnets in iface_subnets.items():
        degree[(device, ifname)] = len(device_subnets[device] - own_subnets)
    return degree


def _brute_force_pairing(
    candidates: List[SubnetCandidate],
    degree: Dict[Tuple[str, str], int],
) -> Tuple[List[Tuple[SubnetCandidate, SubnetCandidate]], List[SubnetCandidate]]:
    """Exhaustive minimum-cost pairing for a small candidate list (stdlib-only
    fallback, cost(i, j) = |degree(i) - degree(j)|, DESIGN.md 4.3.4/4.3.6).

    Only ever called with <= ``max_candidates_for_brute_force`` (config
    default 8) candidates, so the search space (at most 7!! = 105 pairings
    for 8 items) stays small. One candidate is left over when the count is
    odd. This is the exact-optimum stdlib fallback DESIGN.md 4.3.6 already
    sanctions for when networkx/Blossom is unavailable -- it is reused here
    for Phase 1e's k>=3 default path too (see match_subnet_group()'s
    docstring note: Phase 2 adds the real-hub priority check and the
    networkx Blossom engine for larger groups).
    """
    def cost(a: SubnetCandidate, b: SubnetCandidate) -> int:
        return abs(
            degree.get((a.device, a.interface), 0) - degree.get((b.device, b.interface), 0)
        )

    def best(remaining: Tuple[SubnetCandidate, ...]):
        if len(remaining) <= 1:
            return 0, [], list(remaining)
        first, rest = remaining[0], remaining[1:]
        best_total = None
        best_pairs: List[Tuple[SubnetCandidate, SubnetCandidate]] = []
        best_leftover: List[SubnetCandidate] = []
        for i, partner in enumerate(rest):
            sub_remaining = rest[:i] + rest[i + 1:]
            sub_total, sub_pairs, sub_leftover = best(sub_remaining)
            total = cost(first, partner) + sub_total
            if best_total is None or total < best_total:
                best_total = total
                best_pairs = [(first, partner)] + sub_pairs
                best_leftover = sub_leftover
        return best_total, best_pairs, best_leftover

    _, pairs, leftover = best(tuple(candidates))
    return pairs, leftover


def _vlan_id_from_ifname(name: str) -> Optional[int]:
    """Extract the numeric VLAN ID from an SVI name ('Vlan10' -> 10, 'vlan 10'
    -> 10). Returns None for anything else (DESIGN.md 4.3.6 real-hub check)."""
    m = re.match(r"^\s*vlan\s*(\d+)\s*$", (name or ""), re.IGNORECASE)
    return int(m.group(1)) if m else None


def find_real_hub(group: SubnetGroup, configs: Dict[str, ParsedConfig]) -> Optional[SubnetCandidate]:
    """DESIGN.md 4.3.6 priority 1 ("real config priority") -- for a
    k>=3 shared-subnet group, decide whether one of the candidate devices is
    structurally the real L2 hub for this segment: that candidate's own
    interface is an SVI, AND the same device has at least one OTHER
    interface (physical/portchannel/subif) whose access VLAN or trunk
    membership includes that SVI's VLAN ID -- i.e. the device is not merely
    routing into this subnet, it is ALSO switching other ports into the same
    VLAN, which is exactly what a real hub switch looks like from its own
    config.

    Conservative by construction: if more than one candidate in the group
    independently looks like a real hub (which would itself be a config
    anomaly -- two different "real" hubs claiming the same subnet), this
    function refuses to guess and returns None so the caller falls through
    to ``shared_subnet_strategy`` (priority 2) instead of picking one
    arbitrarily.
    """
    hubs: List[SubnetCandidate] = []
    for cand in group.candidates:
        iface = cand.iface_obj
        if iface is None or iface.kind != "svi":
            continue
        vid = _vlan_id_from_ifname(cand.interface)
        if vid is None:
            continue
        parsed = configs.get(cand.device)
        if parsed is None:
            continue
        has_member_port = False
        for other_name, other_iface in parsed.interfaces.items():
            if other_name == cand.interface or other_iface.kind not in ("physical", "portchannel", "subif"):
                continue
            if other_iface.access_vlan == vid:
                has_member_port = True
                break
            if vid in (other_iface.trunk_allowed_vlans or []):
                has_member_port = True
                break
            if other_iface.trunk_native_vlan == vid:
                has_member_port = True
                break
        if has_member_port:
            hubs.append(cand)
    if len(hubs) == 1:
        return hubs[0]
    return None


def _iface_natural_sort_key(name: str) -> Tuple:
    """Split an interface name into (text, int) chunks so
    'GigabitEthernet0/2' sorts before 'GigabitEthernet0/10' (plain string
    sort would put '0/10' before '0/2'). Used only for deterministic,
    reproducible ordering -- never as a correctness signal."""
    return tuple(
        int(chunk) if chunk.isdigit() else chunk.lower()
        for chunk in re.findall(r"\d+|\D+", name or "")
    )


def _hub_spoke_ports(hub: SubnetCandidate, vid: Optional[int], configs: Dict[str, ParsedConfig]) -> List[str]:
    """Every port ON THE HUB DEVICE that a k>=3 real-hub group's spokes can
    each be individually wired to, in deterministic order (DESIGN.md 4.3.6
    priority 1 fix -- see the module docstring's 'real_hub port fan-out'
    note): the hub's own SVI interface FIRST (mirrors 'best_pair'/'direct',
    which also link two devices' SVI ports straight to each other), followed
    by every OTHER physical/portchannel/subif interface on the hub whose
    access VLAN or trunk membership includes ``vid`` (natural-sorted for
    reproducibility) -- i.e. exactly the member-port evidence
    ``find_real_hub()`` already requires to exist at least once. The live NS
    engine allows only ONE L1 link per port, so every generated real_hub link
    to this hub MUST reference a distinct entry from this list; the caller
    is responsible for leaving any extra spoke (beyond ``len()`` of this
    list) unmatched rather than reusing a port."""
    ports = [hub.interface]
    if vid is None:
        return ports
    parsed = configs.get(hub.device)
    if parsed is None:
        return ports
    member_ports: List[str] = []
    for other_name, other_iface in parsed.interfaces.items():
        if other_name == hub.interface or other_iface.kind not in ("physical", "portchannel", "subif"):
            continue
        if (
            other_iface.access_vlan == vid
            or vid in (other_iface.trunk_allowed_vlans or [])
            or other_iface.trunk_native_vlan == vid
        ):
            member_ports.append(other_name)
    ports.extend(sorted(member_ports, key=_iface_natural_sort_key))
    return ports


def _description_mentions_peer(a: SubnetCandidate, b: SubnetCandidate) -> bool:
    """DESIGN.md 4.3.4 tie-break rule 2: does either side's ``description``
    field name the OTHER side's hostname or interface? Treated as a stronger
    signal than the degree-similarity cost (folded into the primary sort key
    in ``_pair_sort_key`` below, ahead of cost)."""
    def _one_way(cand: SubnetCandidate, other: SubnetCandidate) -> bool:
        iface = cand.iface_obj
        desc = ((iface.description if iface else None) or "").lower()
        if not desc:
            return False
        return other.device.lower() in desc or other.interface.lower() in desc
    return _one_way(a, b) or _one_way(b, a)


def _pair_cost(a: SubnetCandidate, b: SubnetCandidate, degree: Dict[Tuple[str, str], int]) -> int:
    """DESIGN.md 4.3.2 cost(i, j) = |degree(i) - degree(j)|."""
    return abs(
        degree.get((a.device, a.interface), 0) - degree.get((b.device, b.interface), 0)
    )


def degree_for_group(group: SubnetGroup) -> Dict[Tuple[str, str], int]:
    """Rebuild the {(device, interface): degree} lookup ``_pair_cost()``
    needs from each candidate's own cached ``other_subnet_degree`` (set by
    ``infer_l1_links_from_subnets()`` before ``match_subnet_group()`` runs) --
    avoids threading the whole-corpus degree dict through every helper
    function's signature."""
    return {(c.device, c.interface): c.other_subnet_degree for c in group.candidates}


def _stencil_tier_distance(
    a: SubnetCandidate, b: SubnetCandidate, stencil_tiers: Dict[str, int],
) -> int:
    """DESIGN.md 4.3.4 tie-break rule 3: prefer pairs whose devices sit at a
    similar RULE-0 tier (e.g. L3Switch-L3Switch over L3Switch-PC). Unknown
    tiers (device not yet stencil-mapped) get a neutral mid-range distance so
    they neither win nor lose this tie-break outright."""
    ta, tb = stencil_tiers.get(a.device), stencil_tiers.get(b.device)
    if ta is None or tb is None:
        return 3
    return abs(ta - tb)


def _pair_sort_key(
    a: SubnetCandidate, b: SubnetCandidate,
    degree: Dict[Tuple[str, str], int], stencil_tiers: Dict[str, int],
) -> Tuple[int, int, int, Tuple[str, str]]:
    """Combined DESIGN.md 4.3.4 tie-break waterfall as a single sortable key:
      1. description-field hint (0 = hinted, wins -- "stronger than the
         degree heuristic" per DESIGN.md 4.3.4 rule 2)
      2. degree-similarity cost (rule ~1's underlying metric, DESIGN.md 4.3.2)
      3. Stencil-tier distance (rule 3)
      4. hostname dictionary order (rule 1's determinism guarantee, and the
         final tie-break of last resort -- always a total order since two
         DIFFERENT candidates never share the exact same device+interface).
    The lowest key wins. Reused for both the direct O(k^2) enumeration
    (``_select_best_pair_direct``) and for picking the single best edge out
    of a Blossom/brute-force full matching (see ``match_subnet_group``).
    """
    desc_hint = 0 if _description_mentions_peer(a, b) else 1
    cost = _pair_cost(a, b, degree)
    tier_dist = _stencil_tier_distance(a, b, stencil_tiers)
    names = tuple(sorted((f"{a.device}:{a.interface}", f"{b.device}:{b.interface}")))
    return (desc_hint, cost, tier_dist, names)


def _select_best_pair_direct(
    candidates: List[SubnetCandidate],
    degree: Dict[Tuple[str, str], int],
    stencil_tiers: Dict[str, int],
) -> Tuple[Tuple[SubnetCandidate, SubnetCandidate], bool]:
    """Exhaustively enumerate every C(k,2) pair and return the single lowest
    ``_pair_sort_key`` pair, i.e. the stdlib-only ("brute_force"-adjacent, but
    O(k^2) not O(k!) since we only need ONE edge, not a full partition)
    fallback used when networkx's Blossom matching is unavailable or the
    group is small. ``ambiguous`` is True iff 2+ pairs tie on the
    (desc_hint, cost, tier_dist) prefix of the key (i.e. only the final,
    always-unique hostname tie-break separated them)."""
    pairs = list(itertools.combinations(candidates, 2))
    keyed = [(pair, _pair_sort_key(pair[0], pair[1], degree, stencil_tiers)) for pair in pairs]
    keyed.sort(key=lambda item: item[1])
    best_pair, best_key = keyed[0]
    tie_count = sum(1 for _, k in keyed if k[:3] == best_key[:3])
    return best_pair, tie_count > 1


def _blossom_full_matching(
    candidates: List[SubnetCandidate],
    degree: Dict[Tuple[str, str], int],
) -> Optional[List[Tuple[SubnetCandidate, SubnetCandidate]]]:
    """DESIGN.md 4.3.4/4.4 decision 9: compute a maximum-cardinality,
    minimum-total-weight matching over the FULL candidate graph using
    ``networkx.algorithms.matching.min_weight_matching`` (the Blossom
    algorithm), weighted by the DESIGN.md 4.3.2 degree-similarity cost.

    Returns None if ``networkx`` is unavailable (caller falls back to
    ``_select_best_pair_direct`` / ``_brute_force_pairing``). This full
    matching (which, for an even candidate count, pairs up EVERY candidate,
    not just two) is deliberately used only as an intermediate computation:
    DESIGN.md 4.3.6 ultimately wants just the SINGLE best pair out of a
    k>=3 group (the rest stay unmatched for requirement E), so
    ``match_subnet_group`` picks the lowest-``_pair_sort_key`` edge out of
    whatever this function returns rather than committing every edge in the
    matching. This reconciles 4.3.4's general Blossom-matching mandate with
    4.3.6's more conservative "only ever commit to one pair per ambiguous
    group" policy.
    """
    if nx is None:
        return None
    graph = nx.Graph()
    for i in range(len(candidates)):
        graph.add_node(i)
    for i, j in itertools.combinations(range(len(candidates)), 2):
        a, b = candidates[i], candidates[j]
        graph.add_edge(i, j, weight=_pair_cost(a, b, degree))
    if graph.number_of_edges() == 0:
        return []
    matching = nx.algorithms.matching.min_weight_matching(graph)
    return [(candidates[i], candidates[j]) for i, j in matching]


def match_subnet_group(
    group: SubnetGroup,
    cfg: Dict,
    configs: Optional[Dict[str, ParsedConfig]] = None,
    stencil_tiers: Optional[Dict[str, int]] = None,
) -> MatchResult:
    """Resolve which candidate(s) in ``group`` are actually linked, and how
    (DESIGN.md 4.3.3, 4.3.4, 4.3.6).

    Implemented (Phase 2, per DESIGN.md section 6):
      - len(group.candidates) == 1  -> everything is "unmatched" (requirement
        E takes over, see synthesize_inferred_peers()). The Port-channel
        "peer entirely absent" per-member expansion special case (DESIGN.md
        4.3.6 addition / 4.5 item 4) remains a later-phase TODO -- a lone
        Port-channel/Bundle-Ether candidate is still treated as one plain
        unmatched candidate.
      - len(group.candidates) == 2 AND needs_l2_switch_inference() is False
        -> direct point-to-point pair (`strategy_used = 'direct'`), after a
        same-device self-loop safety check (DESIGN.md 4.3.4).
      - len(group.candidates) == 2 AND needs_l2_switch_inference() is True
        (forced by a virtual IP or a large subnet, DESIGN.md 4.3.6) -> a
        Dummy_L2_<n> switch is ALWAYS synthesized (no real-hub / best_pair
        branching applies -- there is no pairing ambiguity with only 2
        candidates).
      - len(group.candidates) >= 3 -> DESIGN.md 4.3.6's full priority chain:
          1. ``find_real_hub()`` -- if a real device in the group is
             structurally identifiable as the segment's actual switch (its
             own SVI + at least one other access/trunk port carrying the
             same VLAN), star-connect every other candidate DIRECTLY to that
             real device (`strategy_used = 'real_hub'`, no Dummy_L2_<n> is
             synthesized: this is a REAL device, not an inferred one).
             Each spoke is wired to its OWN distinct port on the hub via
             ``_hub_spoke_ports()`` (the hub's SVI port for the first spoke,
             then any other real member port carrying the same VLAN) --
             the live NS engine rejects an ``add l1_link_bulk`` batch that
             reuses one port for 2+ links, so a spoke beyond the number of
             distinct hub ports actually present in the config falls
             through to ``result.unmatched`` (requirement E) instead of
             being force-linked onto an already-used port.
          2. Else, ``cfg['shared_subnet_strategy']``:
             - ``'synthetic_switch'``: one gray Dummy_L2_<n> hub, every
               candidate star-connected to it (`l2_switch_inferred = True`).
             - ``'best_pair'`` (default): compute the group's optimal
               matching (Blossom via networkx when available and
               ``cfg['matching_algorithm'] != 'brute_force'`, else the
               stdlib-exact ``_brute_force_pairing()`` fallback -- capped by
               ``cfg['max_candidates_for_brute_force']``, above which this
               group automatically falls back to ``synthetic_switch`` as the
               scalability safety valve, DESIGN.md 4.3.9), then commit ONLY
               the single lowest-cost edge from that matching as a direct
               real link (DESIGN.md 4.3.6's literal "最良の1ペアのみ") and
               leave every other candidate unmatched for requirement E.
               ``ambiguous = True`` iff 2+ candidate pairs tied on
               (description-hint, cost, stencil-tier-distance) -- i.e. only
               the final always-unique hostname tie-break separated them.
      - Cross-site RFC1918 duplicate handling (``excluded_reason`` /
        config_excluded_links.csv, DESIGN.md 4.3.6/4.3.9 decision 5):
        implemented (Phase 4) -- see the ``site_scoping`` check immediately
        below. When enabled and 2+ candidates in this group carry 2+
        DISTINCT ``site_hint`` values (derived from ``ParsedConfig.
        source_filename``'s subdirectory, DESIGN.md 4.3.6 mitigation 1) with
        no distinguishing evidence (a ``description`` field on either side
        naming the other, DESIGN.md 4.3.6 mitigation-adjacent signal --
        NAT-domain differences are documented as a further, unimplemented
        refinement, see this function's Phase 4 note below), the ENTIRE
        group is excluded (every candidate -> ``result.unmatched``,
        ``excluded_reason`` set) rather than guessing which pair (if any) is
        real -- this is the conservative, sna_converter-inspired policy
        DESIGN.md 4.3.9 mandates. The other ``excluded_reason`` this
        function can set is the same-device self-loop safety net for a
        malformed 2-candidate group.

    Phase 4 known simplification (documented, not a TODO blocker): the
    cross-site check's "distinguishing evidence" only consults
    ``_description_mentions_peer()`` (DESIGN.md 4.3.6 mitigation 1's
    explicit example). Mitigation 3 (comparing ``ip nat inside`` domains
    across sites) is NOT implemented -- config-only NAT-domain identity is
    itself not reliably determinable (DESIGN.md 4.3.6's own caveat: "この
    対策は補助的なものに留まる"), so it is deliberately left as a
    documented limitation rather than an unreliable heuristic.
    """
    result = MatchResult()
    n = len(group.candidates)
    if n == 0:
        return result

    if n >= 2 and cfg.get("site_scoping", False):
        site_hints = {c.site_hint for c in group.candidates}
        if len(site_hints) >= 2:
            cross_site_pairs = [
                (a, b) for a, b in itertools.combinations(group.candidates, 2)
                if a.site_hint != b.site_hint
            ]
            if not any(_description_mentions_peer(a, b) for a, b in cross_site_pairs):
                result.unmatched = list(group.candidates)
                result.excluded_reason = (
                    "cross-site RFC1918 subnet reuse; no distinguishing evidence "
                    "(site_scoping/description/NAT) available"
                )
                result.excluded_confidence = 0.35
                return result

    if n == 1:
        result.unmatched = list(group.candidates)
        return result

    if not needs_l2_switch_inference(group, cfg):
        # Only reachable for n == 2 (needs_l2_switch_inference() is always
        # True for n >= 3), so this is unambiguously a direct point-to-point
        # pair once the self-loop safety net clears.
        a, b = group.candidates[0], group.candidates[1]
        if a.device == b.device:
            result.unmatched = list(group.candidates)
            result.excluded_reason = "self_loop"
            result.excluded_confidence = 0.20
            return result
        result.linked_pairs = [(a, b)]
        result.strategy_used = "direct"
        return result

    if n == 2:
        # Forced L2 segment (virtual IP / large subnet) with only 2
        # candidates: no pairing ambiguity exists, always synthesize the
        # hub (DESIGN.md 4.3.6).
        result.strategy_used = "synthetic_switch"
        result.l2_switch_inferred = True
        return result

    # n >= 3: DESIGN.md 4.3.6 priority chain.
    configs = configs or {}
    stencil_tiers = stencil_tiers or {}

    hub = find_real_hub(group, configs)
    if hub is not None:
        # Live-engine constraint (discovered via Network Sketcher MCP
        # verification, DESIGN.md section 6 Phase 5 row): `add l1_link_bulk`
        # rejects a port that is already used by another L1 link, so the
        # hub's SVI interface cannot be reused verbatim for every spoke.
        # Fan each spoke out to its OWN distinct port on the hub (SVI first,
        # then any other real member port carrying the same VLAN,
        # DESIGN.md 4.3.6) -- a spoke beyond the number of distinct hub
        # ports available is left unmatched (requirement E) rather than
        # reusing a port, exactly like 'best_pair' leaves every candidate
        # but its single best match unmatched.
        others = sorted((c for c in group.candidates if c is not hub), key=lambda c: c.device.lower())
        vid = _vlan_id_from_ifname(hub.interface)
        hub_ports = _hub_spoke_ports(hub, vid, configs)
        linked_pairs: List[Tuple[SubnetCandidate, SubnetCandidate]] = []
        leftover: List[SubnetCandidate] = []
        for i, other in enumerate(others):
            if i < len(hub_ports):
                hub_side = hub if hub_ports[i] == hub.interface else _dc_replace(hub, interface=hub_ports[i])
                linked_pairs.append((hub_side, other))
            else:
                leftover.append(other)
        result.linked_pairs = linked_pairs
        result.unmatched = leftover
        result.strategy_used = "real_hub"
        result.l2_switch_inferred = False
        return result

    strategy = cfg.get("shared_subnet_strategy", "best_pair")
    if strategy == "synthetic_switch":
        result.strategy_used = "synthetic_switch"
        result.l2_switch_inferred = True
        return result

    # strategy == "best_pair" (default).
    algo = cfg.get("matching_algorithm", "blossom")
    max_bf = cfg.get("max_candidates_for_brute_force", 8)
    full_matching: Optional[List[Tuple[SubnetCandidate, SubnetCandidate]]] = None
    used_algo = "blossom"

    if algo == "blossom":
        full_matching = _blossom_full_matching(group.candidates, degree_for_group(group))
        if full_matching is None:
            algo = "brute_force"  # networkx unavailable -- fall through below

    if algo == "brute_force" and full_matching is None:
        used_algo = "brute_force"
        if n > max_bf:
            # DESIGN.md 4.3.9 scalability safety valve: O(k!) brute-force is
            # infeasible above this cap (and networkx/Blossom was either
            # explicitly declined or unavailable), so fall back to the
            # always-safe shared-segment rendering instead of guessing.
            result.strategy_used = "synthetic_switch"
            result.l2_switch_inferred = True
            result.ambiguous = True
            return result
        full_matching, _leftover = _brute_force_pairing(group.candidates, degree_for_group(group))

    if not full_matching:
        # Degenerate case (e.g. n >= 3 but every edge weight computation
        # failed) -- fall back to the shared-segment rendering rather than
        # silently dropping the group.
        result.strategy_used = "synthetic_switch"
        result.l2_switch_inferred = True
        result.ambiguous = True
        return result

    degree = degree_for_group(group)
    keyed = sorted(full_matching, key=lambda pair: _pair_sort_key(pair[0], pair[1], degree, stencil_tiers))
    best_pair = keyed[0]
    best_key = _pair_sort_key(best_pair[0], best_pair[1], degree, stencil_tiers)
    tie_count = sum(
        1 for pair in keyed
        if _pair_sort_key(pair[0], pair[1], degree, stencil_tiers)[:3] == best_key[:3]
    )
    result.linked_pairs = [best_pair]
    result.unmatched = [c for c in group.candidates if c is not best_pair[0] and c is not best_pair[1]]
    result.strategy_used = used_algo
    result.ambiguous = tie_count > 1
    return result


def write_excluded_links_csv(excluded: List[MatchResult], output_path) -> None:
    """Write ``config_excluded_links.csv`` (DESIGN.md section 4.3.9, decision
    5) -- modelled on ``sna_converter``'s ``out_of_scope_ips.csv`` pattern of
    recording rejected/excluded candidates with a machine-readable reason
    instead of silently dropping them.

    Columns, in this exact order (DESIGN.md 4.3.9 authoritative schema):
      subnet, device, interface, ip, site_hint, duplicate_with, reason,
      confidence
    ``duplicate_with`` lists every OTHER excluded candidate in the same
    group as ``device:interface(site_hint)`` entries joined with ``;``. One
    row per excluded SubnetCandidate (not per SubnetGroup), so every
    device/interface left out of the diagram is individually auditable.
    Always writes the header row, even when ``excluded`` is empty, so the
    file's mere presence/absence is never itself a signal a user has to
    infer (matches convert.py's own comment on this point).
    """
    header = ["subnet", "device", "interface", "ip", "site_hint", "duplicate_with", "reason", "confidence"]
    rows: List[List[str]] = [header]

    for mr in excluded:
        for cand in mr.unmatched:
            others = [
                f"{other.device}:{other.interface}" + (f"({other.site_hint})" if other.site_hint else "")
                for other in mr.unmatched
                if other is not cand
            ]
            rows.append([
                cand.network,
                cand.device,
                cand.interface,
                cand.addr.cidr if cand.addr else "",
                cand.site_hint or "",
                ";".join(others),
                mr.excluded_reason or "",
                f"{mr.excluded_confidence:.2f}",
            ])

    with open(output_path, "w", newline="", encoding="utf-8") as fh:
        csv.writer(fh).writerows(rows)


def infer_l1_links_from_subnets(
    configs: Dict[str, ParsedConfig],
    cfg: Dict,
    stencil_tiers: Optional[Dict[str, int]] = None,
) -> Tuple[List[NSL1Link], List[SubnetCandidate], List[str], List[Tuple[str, List[SubnetCandidate]]], List[MatchResult], int, Dict[str, int]]:
    """Top-level requirement-C entry point.

    Returns (l1_links, unmatched_candidates, report_notes, l2_switch_specs,
    excluded, ambiguous_count, strategy_counts). ``strategy_counts`` maps
    ``MatchResult.strategy_used`` -> occurrence count (e.g. {'direct': 5,
    'real_hub': 1, 'blossom': 2, 'synthetic_switch': 1}) for
    config_report.md. ``unmatched_candidates`` feeds requirement E
    (synthesize_inferred_peers(), below); ``l2_switch_specs`` is a list of
    (subnet_network, candidates) pairs that still need an actual
    ``Dummy_L2_<n>`` NSDevice materialized -- this is deliberately left to
    the caller (build_model()) rather than done here, since only build_model
    owns the per-type-code naming counter shared with requirement E's own
    inferred devices (DESIGN.md 4.5.1); ``excluded`` is the list of
    MatchResults whose ``excluded_reason`` is set, for
    write_excluded_links_csv().

    ``stencil_tiers`` (Phase 2, DESIGN.md 4.3.4 tie-break rule 3): a
    {device: RULE-0 tier int} lookup, computed by build_model() from a
    real-device-only pre-pass of ``stencil_mapper.map_device()`` BEFORE this
    function runs, so k>=3 groups' best-pair tie-break can consult each
    candidate device's Stencil tier. Optional (defaults to an empty dict,
    which simply makes rule 3 a no-op) so this function stays independently
    testable without a full build_model() pipeline.

    NOTE: this is a superset of the 3-tuple return shape originally sketched
    in this function's TODO -- ``l2_switch_specs`` and ``excluded`` were
    added because both requirement E's build_model() orchestration and
    convert.py's config_excluded_links.csv output need them and neither can
    be reconstructed from ``l1_links``/``unmatched_candidates`` alone.
    """
    groups = build_subnet_groups(configs, cfg)
    degree = compute_other_subnet_degree(configs)
    stencil_tiers = stencil_tiers or {}

    l1_links: List[NSL1Link] = []
    unmatched: List[SubnetCandidate] = []
    l2_switch_specs: List[Tuple[str, List[SubnetCandidate]]] = []
    excluded: List[MatchResult] = []
    notes: List[str] = []
    ambiguous_count = 0
    strategy_counts: Dict[str, int] = defaultdict(int)

    for group in groups:
        for cand in group.candidates:
            cand.other_subnet_degree = degree.get((cand.device, cand.interface), 0)

        result = match_subnet_group(group, cfg, configs=configs, stencil_tiers=stencil_tiers)

        if result.excluded_reason:
            excluded.append(result)
            continue
        if result.strategy_used:
            strategy_counts[result.strategy_used] += 1
        if result.ambiguous:
            ambiguous_count += 1
        if result.l2_switch_inferred:
            l2_switch_specs.append((group.network, list(group.candidates)))
            continue

        for a, b in result.linked_pairs:
            l1_links.append(NSL1Link(
                a_device=a.device, a_port=normalise_port_name(a.interface),
                b_device=b.device, b_port=normalise_port_name(b.interface),
            ))
        unmatched.extend(result.unmatched)

    if strategy_counts.get("real_hub"):
        notes.append(
            f"{strategy_counts['real_hub']} shared-subnet group(s) with 3+ real candidates "
            "were resolved to a REAL device hub (DESIGN.md 4.3.6 priority 1 -- the hub's own "
            "SVI + at least one other access/trunk port on the same VLAN)."
        )
    if strategy_counts.get("blossom") or strategy_counts.get("brute_force"):
        n_bp = strategy_counts.get("blossom", 0) + strategy_counts.get("brute_force", 0)
        notes.append(
            f"{n_bp} shared-subnet group(s) with 3+ real candidates were resolved via "
            "'best_pair' degree-similarity matching (DESIGN.md 4.3.4/4.3.6): only the single "
            "lowest-cost pair was linked directly; every other candidate in those groups was "
            "left unmatched for requirement E's inferred-peer synthesis."
        )
    if ambiguous_count:
        notes.append(
            f"{ambiguous_count} shared-subnet group match(es) required a tie-break rule "
            "(DESIGN.md 4.3.4: description hint > degree-similarity cost > Stencil-tier "
            "distance > hostname order) to resolve a cost tie -- review config_report.md."
        )

    return l1_links, unmatched, notes, l2_switch_specs, excluded, ambiguous_count, dict(strategy_counts)


# ---------------------------------------------------------------------------
# Requirement H — WAN interface scoring (DESIGN.md section 4.8)
# ---------------------------------------------------------------------------

def score_wan_interface(iface: ParsedInterface, cfg: Dict) -> float:
    """Return a 0.0-1.0 WAN confidence score for one interface.

    TODO (DESIGN.md 4.8, confirmed design per section 8.3 decision 8): this
    is now a FULLY CONFIG-DRIVEN point-based sum, not a hardcoded formula.
    For each signal key in ``cfg['wan_signal_weights']`` that fires for
    ``iface``, add its configured weight; clip the total to [0.0, 1.0]. Do
    NOT hardcode any weight value in this function -- every weight must be
    read from ``cfg['wan_signal_weights']`` so a user can add/remove/re-weight
    signals purely via config_converter_to_ns_config.json. The default
    signal set (see the JSON's tentative starting weights) is:
      description_keyword_match       (cfg['wan_keywords'] substring match)
      interface_type_wan_like         (Serial/Dialer/Cellular/ATM/BRI/Tunnel)
      ip_nat_outside
      public_ip_on_interface          (non-RFC1918 IP on this interface)
      ip_address_dhcp_or_negotiated
      crypto_map_or_external_ipsec
      external_bgp_peer_detected
      bandwidth_limit_configured      (NEW signal 9, decision 8:
                                        iface.bandwidth_limit_configured)
      no_matching_peer_in_corpus      (additive-only booster, applied by the
                                        caller once requirement C's matching
                                        result is known -- NOT computed here
                                        in isolation)
    Always honour cfg['wan_interface_overrides'][device] first (manual
    override -> score 1.0 or 0.0 unconditionally, bypassing the formula
    entirely).

    Phase 1e implementation note: ``wan_interface_overrides`` and
    ``external_bgp_peer_detected`` both need config-wide context this
    function's ``(iface, cfg)`` signature does not have access to (the
    override is keyed by device name; the BGP signal needs
    ``ParsedConfig.bgp_peers``) -- both are applied by the
    ``compute_wan_scores()`` orchestration wrapper below instead, exactly as
    this docstring's "applied by the caller" note already anticipates for
    ``no_matching_peer_in_corpus``.
    """
    weights = cfg.get("wan_signal_weights", {}) or {}
    score = 0.0

    desc = (iface.description or "").lower()
    if desc and any(kw.lower() in desc for kw in cfg.get("wan_keywords", []) or []):
        score += weights.get("description_keyword_match", 0.0)

    if iface.kind == "tunnel" or re.match(r"^(serial|dialer|cellular|atm|bri)", iface.name, re.IGNORECASE):
        score += weights.get("interface_type_wan_like", 0.0)

    if iface.nat_side == "outside" or (iface.nameif or "").lower() in ("outside", "wan", "external"):
        score += weights.get("ip_nat_outside", 0.0)

    if iface.ipv4:
        try:
            if not ipaddress.IPv4Address(iface.ipv4[0].address).is_private:
                score += weights.get("public_ip_on_interface", 0.0)
        except ValueError:
            pass

    if iface.ipv4_dhcp:
        score += weights.get("ip_address_dhcp_or_negotiated", 0.0)

    if iface.crypto_map:
        score += weights.get("crypto_map_or_external_ipsec", 0.0)

    if iface.bandwidth_limit_configured:
        score += weights.get("bandwidth_limit_configured", 0.0)

    return max(0.0, min(1.0, score))


def _external_bgp_ifaces(parsed: ParsedConfig) -> Set[str]:
    """Best-effort: which interface(s) share a subnet with an external BGP
    neighbour's IP (DESIGN.md 4.8 signal 7 -- ``external_bgp_peer_detected``,
    a config-wide fact score_wan_interface() cannot see on its own)."""
    hits: Set[str] = set()
    if not parsed.bgp_peers:
        return hits
    for ifname, iface in parsed.interfaces.items():
        for addr in iface.ipv4:
            try:
                network = ipaddress.IPv4Network(f"{addr.address}/{addr.prefix}", strict=False)
            except ValueError:
                continue
            for peer in parsed.bgp_peers:
                try:
                    if ipaddress.IPv4Address(peer.neighbor_ip) in network:
                        hits.add(ifname)
                except ValueError:
                    continue
    return hits


def compute_wan_scores(configs: Dict[str, ParsedConfig], cfg: Dict) -> Dict[Tuple[str, str], float]:
    """Orchestration wrapper around score_wan_interface() (DESIGN.md 4.8):
    applies the per-interface point-based formula, layers on the one
    config-wide signal score_wan_interface() cannot compute by itself
    (``external_bgp_peer_detected``, via ``_external_bgp_ifaces()``), and
    finally applies ``cfg['wan_interface_overrides']`` (always wins,
    unconditional 1.0). The ``no_matching_peer_in_corpus`` booster is
    intentionally NOT applied here -- see this module's docstring and
    build_model(), which applies it only to candidates
    infer_l1_links_from_subnets() already determined are unmatched.
    """
    weights = cfg.get("wan_signal_weights", {}) or {}
    overrides = cfg.get("wan_interface_overrides", {}) or {}
    scores: Dict[Tuple[str, str], float] = {}

    for device, parsed in configs.items():
        bgp_ifaces = _external_bgp_ifaces(parsed)
        forced = set(overrides.get(device, []) or [])
        for ifname, iface in parsed.interfaces.items():
            if ifname in forced:
                scores[(device, ifname)] = 1.0
                continue
            score = score_wan_interface(iface, cfg)
            if ifname in bgp_ifaces:
                score += weights.get("external_bgp_peer_detected", 0.0)
            scores[(device, ifname)] = max(0.0, min(1.0, score))

    return scores


# ---------------------------------------------------------------------------
# Requirement G — closed-environment detection (DESIGN.md section 4.7)
# ---------------------------------------------------------------------------

def is_interface_administratively_closed(iface: ParsedInterface) -> bool:
    """DESIGN.md 4.7 "certain" signal — True iff ``iface.shutdown``. Treated
    as an unconditional no-link (the interface is physically link-down),
    not merely a low-confidence hint."""
    return bool(iface.shutdown)


def is_interface_acl_isolated(iface: ParsedInterface, acls: Dict[str, AclDefinition]) -> bool:
    """DESIGN.md 4.7 "medium-to-high confidence" signal — True only when
    BOTH ``iface.acl_in`` and ``iface.acl_out`` resolve to an ``AclDefinition``
    present in ``acls`` (i.e. defined in the SAME device's own config, not
    merely referenced by name) AND both resolve ACLs' ``is_bidirectional_
    deny_all()`` is True. An ACL name that cannot be resolved in ``acls``
    (referenced but never defined in the config we parsed) returns False —
    conservative by construction, per DESIGN.md 4.7's "判定不能（confidence
    低）として扱う" guidance for that exact case. A single-direction ACL
    (only ``acl_in`` OR only ``acl_out``) also returns False (DESIGN.md 4.7
    table row 4 — "片方向のみの制限...クローズドとは判定しない")."""
    if not iface.acl_in or not iface.acl_out:
        return False
    acl_in = acls.get(iface.acl_in)
    acl_out = acls.get(iface.acl_out)
    if acl_in is None or acl_out is None:
        return False
    return acl_in.is_bidirectional_deny_all() and acl_out.is_bidirectional_deny_all()


# Interface kinds excluded from requirement G's "every interface must be
# closed" sweep (DESIGN.md 4.7 table row 2's "ループバック/管理IF除く") --
# a loopback/mgmt interface being up (or having no ACL at all) says nothing
# about whether the device's DATA-PLANE connectivity is closed.
_CLOSED_CHECK_EXCLUDED_KINDS = ("loopback", "mgmt")


def detect_closed_environments(
    configs: Dict[str, ParsedConfig],
) -> Tuple[Dict[str, List[str]], Set[str]]:
    """DESIGN.md section 4.7's full conservative decision tree, implemented
    (Phase 4):

      1. Per-interface: closed iff ``is_interface_administratively_closed()``
         (certain) OR ``is_interface_acl_isolated()`` (medium-high, only
         when both ACLs resolve locally). Loopback/mgmt interfaces are
         excluded from candidacy entirely (see ``_CLOSED_CHECK_EXCLUDED_
         KINDS``) -- they never count as "closed" NOR as a reason to keep a
         device out of the fully-closed verdict.
      2. Per-device: "fully closed" (DESIGN.md 4.7.1) iff the device has AT
         LEAST ONE non-loopback/mgmt interface AND EVERY SUCH interface is
         individually closed per step 1 (DESIGN.md 4.7's "誤判定を避けるた
         めの具体的な判定基準" item 3 -- "単一シグナルのみでのデバイス全体
         クローズド判定は行わない", i.e. full coverage is always required,
         never a single interface in isolation). A device with ZERO
         qualifying interfaces (e.g. only a loopback) is never marked fully
         closed -- there is no data-plane evidence either way.
      3. Null0 black-hole routes and "no dynamic routing protocol on this
         interface" are DELIBERATELY NOT implemented as device-level closed
         signals: DESIGN.md 4.7 itself scopes Null0 to "そのルートのみクロ
         ーズド" (that specific destination only, never the whole device),
         and explicitly says the routing-protocol-absence signal is "弱
         （補助シグナルのみ）...単独では判定材料にせず". Since this module
         has no static-route model to parse (``config_parser.py`` does not
         extract ``ip route`` statements) and DESIGN.md forbids using either
         signal alone anyway, both are recorded here as a known, documented
         scope limitation rather than implemented speculatively — a future
         phase adding static-route parsing could layer this in without
         changing this function's return contract.
      4. Zone-based firewall policies (ZBFW/``zone-pair``/``class-map type
         inspect``) are out of scope per DESIGN.md 4.7 item 4 (Phase 1
         explicitly excludes them; the unified ASA(FTD/FDM) ACL path above already
         covers this OS family's own device-level isolation signal).

    Returns ``(closed_ifaces_by_device, fully_closed_devices)``:
      - ``closed_ifaces_by_device``: {device: [closed_interface_name, ...]}
        for EVERY device with at least one closed interface (not only fully
        closed devices) -- feeds ``MapperReport.closed_interfaces`` (a
        per-interface count, independent of the device-level verdict).
      - ``fully_closed_devices``: the set of devices to route into the
        single dedicated isolated area (``cfg['isolated_area_name']``) and
        to structurally exclude from every other pipeline step's link
        generation (requirements C/E/F, DESIGN.md 4.7.1's "いかなる他エリ
        アとの間のL1リンクも生成しない") -- ``build_model()`` is
        responsible for that exclusion; this function only detects.
    """
    closed_ifaces_by_device: Dict[str, List[str]] = {}
    fully_closed_devices: Set[str] = set()

    for device, parsed in configs.items():
        qualifying = [
            name for name, iface in parsed.interfaces.items()
            if iface.kind not in _CLOSED_CHECK_EXCLUDED_KINDS
        ]
        if not qualifying:
            continue

        closed_names: List[str] = []
        all_closed = True
        for name in qualifying:
            iface = parsed.interfaces[name]
            if is_interface_administratively_closed(iface):
                closed_names.append(name)
            elif is_interface_acl_isolated(iface, parsed.acls):
                closed_names.append(name)
            else:
                all_closed = False

        if closed_names:
            closed_ifaces_by_device[device] = closed_names
        if all_closed:
            fully_closed_devices.add(device)

    return closed_ifaces_by_device, fully_closed_devices


def describe_closed_device_reason(
    device: str, closed_ifaces: List[str], parsed: ParsedConfig,
) -> str:
    """Build the DESIGN.md 4.7.1 "判定根拠" audit string for one fully-closed
    device (used by ``config_report.md``): lists each closed interface and
    whether it was closed via ``shutdown`` or a bidirectional deny-all ACL
    pair, so a reviewer can verify the verdict without re-deriving it from
    raw config text."""
    parts = []
    for name in closed_ifaces:
        iface = parsed.interfaces.get(name)
        if iface is None:
            continue
        if is_interface_administratively_closed(iface):
            parts.append(f"{name}=shutdown")
        else:
            parts.append(f"{name}=ACL bidirectional deny-all ({iface.acl_in}/{iface.acl_out})")
    return f"{device}: all {len(closed_ifaces)} data-plane interface(s) closed ({', '.join(parts)})"


def _drop_cross_isolation_links(
    links: List[NSL1Link], closed_devices: Set[str],
) -> List[NSL1Link]:
    """DESIGN.md 4.7.1: "隔離エリア内のデバイスへは、いかなる他エリアとの
    間のL1リンクも生成しない（実リンク・推測リンクとも不可）". Applied as a
    single, generic post-filter over EVERY link source (direct/real_hub/
    best_pair pairs from requirement C, ``Dummy_L2_<n>`` star links, and
    later requirement E/F links) rather than re-implemented per producer:
    drops any link where exactly one endpoint is a fully-closed device.
    Links where BOTH endpoints are closed (an isolated sub-network entirely
    among closed devices) are kept unchanged, per DESIGN.md 4.7.1's "隔離
    エリア内部のデバイス同士のリンクは通常どおり生成してよい"."""
    if not closed_devices:
        return links
    return [
        lk for lk in links
        if (lk.a_device in closed_devices) == (lk.b_device in closed_devices)
    ]


# ---------------------------------------------------------------------------
# Requirements D, E, F — layout (netbox_layout port), inferred-peer synthesis,
# and full-connectivity guarantee (DESIGN.md sections 4.4, 4.5, 4.6)
# ---------------------------------------------------------------------------

def compute_tiers_and_areas(
    l1_links: List[NSL1Link],
    stencil_tiers: Dict[str, int],
    closed_devices: Set[str],
    cfg: Dict,
    waypoint_devices: Optional[Set[str]] = None,
) -> Tuple[List[Set[str]], Dict[str, int]]:
    """Implemented (Phase 3, DESIGN.md section 4.4, confirmed design per
    section 8.3 decision 9): delegate to ``layout.compute_network_groups_and_
    tiers()`` — a near-verbatim, config_converter-local port of
    ``3rd_party/netbox_converter/src/netbox_layout.py``'s function of the
    same name (connected-component area detection + hostname-keyword/degree
    tier assignment + ``detect_device_clusters()`` redundant-pair handling),
    using ``networkx`` (REQUIRED dependency for this function, not optional
    -- see requirements.txt and DESIGN.md section 4.4's decision 9; contrast
    with requirement C's ``match_subnet_group()``, where the Blossom
    algorithm has a stdlib brute-force fallback for when ``networkx`` is
    merely unavailable rather than architecturally required).

    TIER/AREA DECOUPLING (DESIGN.md 4.4.1, feature: waypoint-excluded area
    grouping). Returns ``(area_components, tiers)`` computed on TWO different
    graphs:
      - ``tiers``: the FULL graph (still INCLUDING cloud/waypoint edges), so
        edge-router WAN-adjacency tiering is byte-for-byte unchanged.
      - ``area_components``: the connected components of the NON-WAYPOINT
        subgraph (``layout.compute_area_components()``), i.e. every link that
        touches a ``waypoint_devices`` node (the shared gray ``Dummy_CL_1``
        cloud, ``stencil_type == NS_CLOUD``) is dropped before components are
        computed. This stops the cloud from bridging otherwise-separate
        real-device groups into a single area, so each contiguously-wired set
        of NON-waypoint devices becomes its own side-by-side area (the
        ``device -> Dummy_CL_1`` logical links are KEPT in the model; they are
        merely ignored as a component BRIDGE here). ``waypoint_devices``
        defaults to the empty set (pre-feature single-graph behaviour) when a
        caller does not pass it.

    ``closed_devices`` (from ``detect_closed_environments()``, Phase 4) is
    EXCLUDED from the connected-component graph passed to
    ``layout.compute_network_groups_and_tiers()`` -- any link touching a
    closed device is dropped before the graph is built, per DESIGN.md 4.6
    item 1 / 4.7.1's "最初から探索グラフの対象外とする" (excluded from the
    search graph from the start, not merely post-filtered). The caller
    (``build_model()``) is responsible for placing ``closed_devices`` into
    the single dedicated area named ``cfg['isolated_area_name']`` and for
    never emitting an inter-area L1 link that touches it — this function
    itself only returns components/tiers for the NON-closed subgraph.

    ``cfg['role_keyword_overrides']`` (DESIGN.md 4.4 item 3 / section 7) is
    forwarded to ``layout.calculate_tier_by_device_role()`` so
    organisation-specific hostname keywords augment (never replace) the
    ported dictionaries. Keyword dictionaries are English/technical-term
    only by confirmed design -- no localisation support is required
    (DESIGN.md section 8.4 decision 13).

    NOTE (deferred, out of scope for this function): cml_converter's
    ``LABEL_KEYWORD_RULES`` and this module's own ``HOSTNAME_KEYWORD_RULES``
    (``stencil_mapper.py``) are a SEPARATE keyword source used for Stencil
    TYPE classification (Router vs. Switch vs. Firewall, etc.), already
    folded into ``stencil_tiers`` before this function ever runs (see
    ``build_model()`` step 0) -- merging them a SECOND time into the
    layout-tier keyword dictionaries here would double-count the same
    signal, so this function intentionally does not touch them directly.
    """
    if not l1_links:
        return [], {}
    filtered_links = [
        lk for lk in l1_links
        if lk.a_device not in closed_devices and lk.b_device not in closed_devices
    ]
    role_overrides = cfg.get("role_keyword_overrides") or {}

    # TIER/AREA DECOUPLING (DESIGN.md 4.4.1, feature: waypoint-excluded area
    # grouping). Tiers are computed on the FULL graph -- one that still
    # INCLUDES cloud/waypoint edges -- exactly as before this feature, so the
    # edge-router WAN-adjacency check in ``calculate_tier_by_device_role()``
    # (tier 1 "has an external neighbour" vs. tier 3) never regresses when the
    # shared ``Dummy_CL_1`` waypoint is later excluded from AREA grouping.
    _, tiers = layout.compute_network_groups_and_tiers(
        filtered_links, stencil_tiers, role_overrides,
    )

    # AREAS are the connected components of the NON-WAYPOINT subgraph: every
    # link touching a waypoint device (``waypoint_devices``, i.e. the shared
    # gray ``Dummy_CL_1`` cloud, ``stencil_type == NS_CLOUD``) is dropped
    # before components are computed, so the cloud does NOT bridge
    # otherwise-separate real-device groups into one area. Each resulting
    # component becomes its own side-by-side area. NOTE: the cloud-bridge
    # ``device -> Dummy_CL_1`` LOGICAL links themselves are KEPT in
    # ``model.l1_links`` (they legitimise the inter-area device->waypoint
    # connection under NS RULE 3) -- we merely stop treating the waypoint as a
    # component BRIDGE for area assignment.
    components = layout.compute_area_components(
        filtered_links, waypoint_devices or set(),
    )
    return components, tiers


def synthesize_inferred_peers(
    unmatched: List[SubnetCandidate],
    wan_scores: Dict[Tuple[str, str], float],
    cfg: Dict,
) -> Tuple[List[NSDevice], List[NSL1Link]]:
    """Requirement E (+ requirement H's WAN-cloud special case).

    TODO (DESIGN.md section 4.5/4.5.1, confirmed naming per section 8.4
    decision 11): for each unmatched candidate,
      - if wan_scores[(device, interface)] >= cfg['wan_confidence_threshold']:
          link to the ONE shared WAN/Internet cloud device
          ``Dummy_CL_1`` (gray, area='wan' so ns_model.py's
          _RAW_WAYPOINT_AREAS promotion applies) instead of a per-interface
          placeholder. This is the SAME device requirement F's
          ensure_full_connectivity() uses for isolated-component
          force-connection when no site-specific cloud is warranted -- do
          not create a second, differently-named cloud device.
      - else: synthesize a device named ``Dummy_<TC>_<n>`` where ``<TC>`` is
        the 2-letter type code for the inferred stencil (see the type-code
        table in DESIGN.md 4.5.1 -- e.g. RT=Router, L2=Switch, L3=L3Switch,
        FW=Firewall, SV=Server, CL=Cloud, PC=PC) and ``<n>`` is a 1-based
        counter PER TYPE CODE (Dummy_RT_1, Dummy_RT_2, Dummy_L2_1, ...).
        Every inferred interface on such a device is named ``Dummy <n>``
        with its own independent 0-based counter (Dummy 0, Dummy 1, ...) --
        see DESIGN.md 4.5.1 for the full naming grammar and the
        ``normalise_port_name()`` validation risk noted there. Stencil
        chosen per the priority list in DESIGN.md 4.5 (NS_CLOUD >
        context-based Server/PC > NS_ROUTER default), default_color=GRAY,
        confidence <= 0.60 via stencil_mapper.map_inferred_peer().
      - honour cfg['aggregate_access_peers']: collapse multiple unmatched
        access-layer peers of the SAME real device into a single placeholder
        (netbox_converter's dummy_stub_N precedent) -- this remains the ONLY
        naming-granularity control; the deprecated 'placeholder_naming'
        config key ('per_interface' | 'per_subnet') no longer exists.
      - Port-channel/LAG special case (DESIGN.md 4.3.6 addition / 4.5 item 4,
        user requirement): when an unmatched candidate's own interface is a
        Port-channel logical interface (i.e. its peer Port-channel/device is
        entirely absent from the input corpus), do NOT collapse it to a
        single representative link. Instead synthesize ONE
        ``Dummy_<TC>_<n>`` device whose interface count equals
        ``len(local_members)`` (the local NSPortChannel.physical_ports
        count), with interfaces named ``Dummy 0``, ``Dummy 1``, ... using
        the same 0-based grammar as any other inferred device, and connect
        every local physical member port to one of these interfaces 1:1
        (ordered by the same numeric-tuple normalisation used by the
        real-peer member-pairing algorithm in DESIGN.md 4.3.6). This keeps
        "every physical member port is wired to something" true whether the
        peer Port-channel is real (member pairing) or unknown (this case).
        ``aggregate_access_peers`` must never reduce this to fewer than
        ``len(local_members)`` links -- it only aggregates across DIFFERENT
        unmatched interfaces of the same device, not across the members of
        one Port-channel.
    Every synthesized device/link must be traceable in config_report.md /
    config_inventory.csv (reason string, confidence) -- for the Port-channel
    case above, the reason string should record the member count (e.g.
    "port-channel member count=<N>, all members individually connected to
    one inferred peer", DESIGN.md 4.3.6).

    Phase 1e implementation scope (DESIGN.md section 6): implements the
    wan_cloud vs. generic-peer branch and the full ``Dummy_<TC>_<n>`` /
    ``Dummy <n>`` naming grammar (with independent per-type-code and
    per-device port counters) plus ``aggregate_access_peers``. NOT yet
    implemented, deferred to a later phase:
      - the context-based Server/PC branches of DESIGN.md 4.5's priority
        list (items 2/3) -- every non-WAN unmatched candidate defaults to a
        generic ``NS_ROUTER``/``Dummy_RT_<n>`` peer (item 4) for now;
      - the Port-channel/LAG per-member-expansion special case above -- a
        lone unmatched Port-channel/Bundle-Ether candidate (e.g. the
        bundled ``iosxr_edge01.txt`` Bundle-Ether1 sample) is currently
        given one plain placeholder device/link like any other interface,
        NOT one placeholder interface per local physical member.
    """
    devices: List[NSDevice] = []
    links: List[NSL1Link] = []
    type_counters: Dict[str, int] = {}
    port_counters: Dict[str, int] = {}
    aggregate = cfg.get("aggregate_access_peers", True)
    threshold = cfg.get("wan_confidence_threshold", 0.5)
    aggregated_device_for: Dict[str, str] = {}
    wan_cloud_created = False

    def _next_port(dev_name: str) -> str:
        idx = port_counters.get(dev_name, 0)
        port_counters[dev_name] = idx + 1
        return f"Dummy {idx}"

    def _new_peer_device() -> NSDevice:
        code = sten.DUMMY_TYPE_CODES[sten.NS_ROUTER]
        type_counters[code] = type_counters.get(code, 0) + 1
        name = f"Dummy_{code}_{type_counters[code]}"
        mapping = sten.map_inferred_peer(
            name, "peer",
            reason=(
                "No matching subnet peer found in the input corpus "
                "(requirement E, DESIGN.md 4.5)"
            ),
        )
        return NSDevice(
            name=name, area="default", row=_TIER[sten.NS_ROUTER],
            stencil=mapping, is_endpoint=True, default_color=GRAY,
        )

    for cand in unmatched:
        score = wan_scores.get((cand.device, cand.interface), 0.0)

        if score >= threshold:
            if not wan_cloud_created:
                mapping = sten.map_inferred_peer(
                    "Dummy_CL_1", "wan_cloud",
                    reason="Shared WAN/Internet cloud waypoint (requirements E+H, DESIGN.md 4.5/4.8)",
                )
                devices.append(NSDevice(
                    name="Dummy_CL_1", area="wan", row=_TIER[sten.NS_CLOUD],
                    stencil=mapping, is_endpoint=False, default_color=GRAY,
                ))
                wan_cloud_created = True
            links.append(NSL1Link(
                a_device=cand.device, a_port=normalise_port_name(cand.interface),
                b_device="Dummy_CL_1", b_port=_next_port("Dummy_CL_1"),
            ))
            continue

        if aggregate and cand.device in aggregated_device_for:
            peer_name = aggregated_device_for[cand.device]
        else:
            dev = _new_peer_device()
            devices.append(dev)
            peer_name = dev.name
            if aggregate:
                aggregated_device_for[cand.device] = peer_name

        links.append(NSL1Link(
            a_device=cand.device, a_port=normalise_port_name(cand.interface),
            b_device=peer_name, b_port=_next_port(peer_name),
        ))

    return devices, links


def _materialize_l2_switch(
    network: str,
    candidates: List[SubnetCandidate],
    type_counters: Dict[str, int],
) -> Tuple[NSDevice, List[NSL1Link]]:
    """Turn one ``infer_l1_links_from_subnets()`` l2_switch_spec entry into
    an actual ``Dummy_L2_<n>`` NSDevice + one star-topology NSL1Link per real
    candidate (DESIGN.md 4.3.6). ``type_counters`` is owned and shared by
    ``build_model()`` across every such call so the per-type-code numbering
    (``DESIGN.md 4.5.1``) stays globally consistent."""
    code = sten.DUMMY_TYPE_CODES[sten.NS_SWITCH]
    type_counters[code] = type_counters.get(code, 0) + 1
    name = f"Dummy_{code}_{type_counters[code]}"
    mapping = sten.map_inferred_peer(
        name, "l2_switch",
        reason=(
            f"Shared-subnet hub inferred for {network} "
            f"({len(candidates)} real candidate(s), DESIGN.md 4.3.6)"
        ),
    )
    device = NSDevice(
        name=name, area="default", row=_TIER[sten.NS_SWITCH],
        stencil=mapping, is_endpoint=False, default_color=GRAY,
    )
    links = [
        NSL1Link(
            a_device=cand.device, a_port=normalise_port_name(cand.interface),
            b_device=name, b_port=f"Dummy {i}",
        )
        for i, cand in enumerate(candidates)
    ]
    return device, links


def ensure_full_connectivity(
    model: NSModel,
    stencil_tiers: Dict[str, int],
    closed_devices: Set[str],
    cfg: Dict,
) -> Tuple[List[NSDevice], List[NSL1Link]]:
    """Implemented (Phase 3, DESIGN.md section 4.6, confirmed design per
    section 8.1 decision 1) — sna_converter-INSPIRED, not a direct port
    (sna_converter has no device inventory to preserve; this function does).

    ``cfg['assume_fully_connected']`` DEFAULTS to True ("exhaustive" policy,
    decision 1). If False (opt-in conservative override): return ``([], [])``
    and leave isolated connected components as independent areas/components
    (``build_model()`` still assigns each its own area via
    ``compute_tiers_and_areas()`` — they are simply never stitched together).

    Otherwise (default): every device already in ``closed_devices`` (from
    requirement G, Phase 4) is treated as OUT OF SCOPE entirely (structural
    G > F integration, decisions 2/6) -- excluded from the eligible-device
    set below BEFORE any component analysis. Unlike ``compute_tiers_and_
    areas()`` (requirement D), this function does NOT exclude ``NS_CLOUD``
    devices from the graph: the shared WAN cloud is a real bridge between
    otherwise-separate real-device clusters (e.g. two edge routers that both
    happen to have a WAN-classified interface, per requirement H, ARE
    already mutually reachable through that shared ``Dummy_CL_1`` node, even
    though requirement C's subnet matching alone never linked them). Folding
    the cloud into the SAME graph avoids the bug of re-connecting an
    already-cloud-reachable component a second, redundant time. For the
    remaining eligible devices + links:
      1. Build components via ``layout.compute_network_groups_and_tiers()``
         (same helper requirement D uses, but INCLUDING cloud edges here).
         Devices with literally zero eligible links (e.g. no L3-addressed
         interface at all, so requirement C never even produced an unmatched
         candidate for requirement E to synthesize a peer from) are NOT
         returned by that helper (they have no edge to be part of a
         component) -- each such device is added back here as its own
         size-1 pseudo-component so ``cfg['min_component_size_for_inference']``
         can filter it out explicitly (DESIGN.md 4.6 item 5), rather than
         silently vanishing.
      2. Any component that already CONTAINS an ``NS_CLOUD`` device (directly
         or transitively, since components are connectivity-transitive) is
         considered already reachable and is skipped entirely -- it needs no
         new link. Every remaining ("isolated") component smaller than
         ``cfg['min_component_size_for_inference']`` (default 2) is ALSO
         left alone (avoids inventing a connection for a device with no
         addressing information at all).
      3. If no isolated component qualifies, OR exactly one qualifies AND no
         cloud-containing component exists anywhere (a single, lone blob
         with nothing else to connect it to -- connecting it to a brand-new,
         otherwise-unused cloud device would add no information), there is
         nothing to stitch together -- return ``([], [])``.
      4. Otherwise, for each qualifying isolated component pick a
         representative device: lowest tier first (closer to the top of the
         hierarchy, per DESIGN.md 4.6 item 3's "最もtierが小さい"), tie-broken
         by highest eligible-link degree, then by name for determinism.
         Star-connect every representative to the ONE shared gray cloud
         device ``Dummy_CL_1`` (the SAME device ``synthesize_inferred_
         peers()`` uses for WAN-classified interfaces, per DESIGN.md 4.8's
         "判定結果の反映" -- created here if it does not already exist). Both
         link endpoints use the ``Dummy <n>`` synthetic-port grammar
         (DESIGN.md 4.5.1; confirmed accepted by the NS engine regardless of
         which side of the link it is on, Phase 1e risk #12), since this
         link is not backed by any real interface on EITHER side.
      5. Every representative device's ``routing_attribute`` gains an
         "INFERRED: assumed connectivity, not observed" note (DESIGN.md 4.6
         risk note) so the visualisation never implies this link was
         actually observed in a config.
    """
    if not cfg.get("assume_fully_connected", True):
        return [], []

    eligible_names = [name for name in model.devices if name not in closed_devices]
    eligible_set = set(eligible_names)
    eligible_links = [
        lk for lk in model.l1_links
        if lk.a_device in eligible_set and lk.b_device in eligible_set
    ]
    role_overrides = cfg.get("role_keyword_overrides") or {}
    components, tiers = layout.compute_network_groups_and_tiers(
        eligible_links, stencil_tiers, role_overrides,
    )
    linked_names = {n for comp in components for n in comp}
    for name in eligible_names:
        if name not in linked_names:
            components.append({name})

    cloud_names = {
        n for n in eligible_names if model.devices[n].stencil.stencil_type == sten.NS_CLOUD
    }
    hub_components = [comp for comp in components if comp & cloud_names]
    isolated_components = [comp for comp in components if not (comp & cloud_names)]

    min_size = cfg.get("min_component_size_for_inference", 2)
    qualifying = [comp for comp in isolated_components if len(comp) >= min_size]
    if not qualifying:
        return [], []
    if not hub_components and len(qualifying) < 2:
        return [], []

    degree: Dict[str, int] = defaultdict(int)
    for lk in eligible_links:
        degree[lk.a_device] += 1
        degree[lk.b_device] += 1

    def _representative(comp: Set[str]) -> str:
        return sorted(
            comp,
            key=lambda n: (tiers.get(n, stencil_tiers.get(n, 6)), -degree.get(n, 0), n),
        )[0]

    new_devices: List[NSDevice] = []
    new_links: List[NSL1Link] = []
    cloud_name = "Dummy_CL_1"
    if cloud_name not in model.devices:
        mapping = sten.map_inferred_peer(
            cloud_name, "wan_cloud",
            reason="Shared WAN/Internet cloud waypoint (requirements E+H, DESIGN.md 4.5/4.8)",
        )
        cloud_device = NSDevice(
            name=cloud_name, area="wan", row=_TIER[sten.NS_CLOUD],
            stencil=mapping, is_endpoint=False, default_color=GRAY,
        )
        model.devices[cloud_name] = cloud_device
        new_devices.append(cloud_device)

    cloud_port_idx = sum(
        1 for lk in model.l1_links
        if lk.a_device == cloud_name or lk.b_device == cloud_name
    )
    # Bugfix (found via live sample5 verification, DESIGN.md section 5 risk
    # #23): a representative device chosen below can itself be a
    # requirement-E synthesized peer (e.g. ``Dummy_RT_1``/``Dummy_RT_2``,
    # created earlier by ``synthesize_inferred_peers()`` for an unmatched
    # subnet candidate) that ALREADY owns one or more ``Dummy <n>`` ports in
    # ``model.l1_links`` (e.g. 'Dummy 0', 'Dummy 1', 'Dummy 2' from its
    # existing links to real devices). The counter used to mint this
    # function's OWN new 'Dummy <n>' port on that same representative device
    # used to always start at 0 regardless of those pre-existing links,
    # so it would re-mint an already-used port name on that device -- the
    # live NS engine's ``add l1_link_bulk`` rejects this with "Port used"
    # (two different links both claiming e.g. 'Dummy_RT_1':'Dummy 0'). Seed
    # the per-device counter from the highest 'Dummy <n>' index already
    # present on that device across ``model.l1_links``, exactly like the
    # analogous, already-correct seeding in
    # ``synthesize_portchannel_member_links()`` below, so newly-minted ports
    # are guaranteed to be free.
    rep_port_counters: Dict[str, int] = {}
    for lk in model.l1_links:
        for dev, port in ((lk.a_device, lk.a_port), (lk.b_device, lk.b_port)):
            m = re.match(r"^Dummy (\d+)$", port)
            if m:
                rep_port_counters[dev] = max(
                    rep_port_counters.get(dev, -1), int(m.group(1)) + 1
                )

    def _next_rep_port(dev_name: str) -> str:
        idx = rep_port_counters.get(dev_name, 0)
        rep_port_counters[dev_name] = idx + 1
        return f"Dummy {idx}"

    note = (
        "INFERRED: assumed connectivity to WAN/Core cloud (requirement F, "
        "DESIGN.md 4.6) -- not observed in any config; added only to "
        "satisfy the full-connectivity guarantee (assume_fully_connected=true)."
    )
    for comp in sorted(qualifying, key=lambda c: (-len(c), min(c))):
        rep = _representative(comp)
        new_links.append(NSL1Link(
            a_device=rep, a_port=_next_rep_port(rep),
            b_device=cloud_name, b_port=f"Dummy {cloud_port_idx}",
        ))
        cloud_port_idx += 1
        dev = model.devices[rep]
        dev.routing_attribute = (
            f"{dev.routing_attribute} | {note}" if dev.routing_attribute else note
        )

    return new_devices, new_links


@dataclass
class _OrphanPortChannel:
    """One device's orphaned (no L1 link yet), IP-less logical Port-channel/
    Bundle-Ether interface, as collected by ``synthesize_portchannel_member_
    links()``'s first pass -- see DESIGN.md section 5's same-batch vPC
    peer-link pairing risk entry."""
    device: str
    group_id: int
    logical_iname: str
    member_inames: List[str]
    vpc_peer_link: bool
    vpc_domain: Optional[int]
    description: Optional[str]


def _mentions_hostname(description: Optional[str], hostname: str) -> bool:
    """DESIGN.md 4.3.4 tie-break rule 2, reused here: does ``description``
    name ``hostname``? Case-insensitive substring match, same convention as
    ``_description_mentions_peer()`` above."""
    if not description or not hostname:
        return False
    return hostname.lower() in description.lower()


def _resolve_vpc_peer_link_bucket(
    candidates: List[_OrphanPortChannel],
) -> Tuple[List[Tuple[_OrphanPortChannel, _OrphanPortChannel]], List[_OrphanPortChannel]]:
    """Peel off confident 1:1 pairs from a bucket of 2+ same-vpc_domain (or
    both-unscoped) ``vpc peer-link`` candidates.

    - Exactly 2 candidates: always paired -- sharing a vpc_domain id (or both
      lacking one) is already the strongest available same-batch signal, and
      a vPC domain fundamentally only ever has two peer switches.
    - 3+ candidates (only possible when several devices' vpc_domain either
      collides or is unparsed): DO NOT guess by shape alone -- only pair off
      candidates whose ``description`` MUTUALLY names the other's hostname
      (DESIGN.md 4.3.4 rule 2, the same description-hint signal already
      trusted elsewhere in this tool). Anything left unpaired after that is
      returned as ``leftover`` for the caller to fall back to Dummy_L2
      synthesis for, exactly as it would without this pairing step at all.
    """
    if len(candidates) == 2:
        return [(candidates[0], candidates[1])], []

    remaining = list(candidates)
    pairs: List[Tuple[_OrphanPortChannel, _OrphanPortChannel]] = []
    changed = True
    while changed and len(remaining) >= 2:
        changed = False
        for a, b in itertools.combinations(remaining, 2):
            if _mentions_hostname(a.description, b.device) and _mentions_hostname(b.description, a.device):
                pairs.append((a, b))
                remaining.remove(a)
                remaining.remove(b)
                changed = True
                break
    return pairs, remaining


def _member_port_suffix(iname: str) -> str:
    """Trailing digit/slash identity of a member interface name (e.g.
    'Ethernet1/1' -> '1/1'), used to pair two real devices' EtherChannel
    members by their (very commonly identical, e.g. symmetric vPC peer
    switches) physical port numbering before falling back to positional
    zip-pairing. Never used as a correctness signal on its own -- only to
    order an already-confirmed real-to-real Port-channel pairing's member
    links deterministically."""
    m = re.search(r"([\d/]+)$", iname)
    return m.group(1) if m else iname.lower()


def _link_vpc_peer_link_pair(
    a: _OrphanPortChannel,
    b: _OrphanPortChannel,
    link_by_endpoint: Dict[Tuple[str, str], Tuple[str, str]],
    next_port,
    new_links: List[NSL1Link],
) -> None:
    """Wire two REAL devices' orphaned 'vpc peer-link' Port-channels'
    physical members directly to each other, instead of each getting its
    own synthetic Dummy_L2 peer (DESIGN.md section 5 same-batch vPC
    peer-link pairing risk entry). Members are paired by matching trailing
    port-number identity first (the common case -- symmetric peer switches
    reuse the same physical port numbers for the peer-link, as seen live in
    Input_data_sample5/9), a natural-sorted zip for any leftovers with
    non-matching names, and -- only for a genuine member-COUNT mismatch
    between the two sides -- a freshly-minted 'Dummy N' port on the
    shorter side's peer (the same placeholder-port convention already used
    for the common subnet-matched case below, DESIGN.md section 5 risk #17).

    DESIGN.md section 5 risk #26 (live Network Sketcher MCP verification):
    this function used to ALSO link the two logical Port-channel names
    (``a.logical_iname``/``b.logical_iname``) directly to each other, on
    top of the per-member links below -- e.g. both ``dc-nexus01
    <-> dc-nexus02`` ``Port-channel 10 <-> Port-channel 10`` AND
    ``Ethernet 1/1 <-> Ethernet 1/1`` / ``Ethernet 1/2 <-> Ethernet 1/2``.
    That direct logical link is now INTENTIONALLY never created: it
    double-registered the exact same port name as both a first-class,
    independently-linked L1 interface AND the virtual/bundled port ``add
    portchannel_bulk`` synthesizes OVER these very member links, which is a
    phantom third "cable" between two devices that in reality have only
    the two member links -- see ``synthesize_portchannel_member_links()``'s
    docstring for the full rationale and live-engine evidence (confirmed:
    ``add portchannel_bulk`` alone is sufficient to make the logical name
    usable by ``add ip_address_bulk``/``add l2_segment_bulk``, exactly like
    ``add virtual_port_bulk`` is for SVI/Loopback -- no separate direct
    link is needed OR wanted).
    """
    a_members = sorted(a.member_inames, key=_iface_natural_sort_key)
    b_members = sorted(b.member_inames, key=_iface_natural_sort_key)
    b_by_suffix: Dict[str, str] = {}
    for name in b_members:
        b_by_suffix.setdefault(_member_port_suffix(name), name)

    used_b: Set[str] = set()
    pairs: List[Tuple[str, str]] = []
    remaining_a: List[str] = []
    for a_name in a_members:
        b_name = b_by_suffix.get(_member_port_suffix(a_name))
        if b_name is not None and b_name not in used_b:
            pairs.append((a_name, b_name))
            used_b.add(b_name)
        else:
            remaining_a.append(a_name)
    remaining_b = [name for name in b_members if name not in used_b]
    for a_name, b_name in zip(remaining_a, remaining_b):
        pairs.append((a_name, b_name))
    matched_count = len(remaining_b) if len(remaining_a) >= len(remaining_b) else len(remaining_a)
    leftover_a = remaining_a[matched_count:]
    leftover_b = remaining_b[matched_count:]

    for a_name, b_name in pairs:
        ns_a = normalise_port_name(a_name)
        ns_b = normalise_port_name(b_name)
        if (a.device, ns_a) in link_by_endpoint:
            continue
        new_links.append(NSL1Link(a_device=a.device, a_port=ns_a, b_device=b.device, b_port=ns_b))
        link_by_endpoint[(a.device, ns_a)] = (b.device, ns_b)
        link_by_endpoint[(b.device, ns_b)] = (a.device, ns_a)
    for a_name in leftover_a:
        ns_a = normalise_port_name(a_name)
        if (a.device, ns_a) in link_by_endpoint:
            continue
        new_port = next_port(b.device)
        new_links.append(NSL1Link(a_device=a.device, a_port=ns_a, b_device=b.device, b_port=new_port))
        link_by_endpoint[(a.device, ns_a)] = (b.device, new_port)
    for b_name in leftover_b:
        ns_b = normalise_port_name(b_name)
        if (b.device, ns_b) in link_by_endpoint:
            continue
        new_port = next_port(a.device)
        new_links.append(NSL1Link(a_device=b.device, a_port=ns_b, b_device=a.device, b_port=new_port))
        link_by_endpoint[(b.device, ns_b)] = (a.device, new_port)


def synthesize_portchannel_member_links(
    model: "NSModel", configs: Dict[str, ParsedConfig], type_counters: Dict[str, int],
) -> Tuple[List[NSDevice], List[NSL1Link], List[str], Set[Tuple[str, str]]]:
    """Give every physical LAG/port-channel member interface its OWN L1
    link, mirroring the peer already established for the logical Port-
    channel/Bundle-Ether interface it belongs to -- and, since DESIGN.md
    section 5 risk #17's follow-up investigation (the ``add portchannel_
    bulk`` "Port not found" bug), SYNTHESIZE that peer too when the logical
    interface has none, instead of silently skipping the whole channel-group.

    **DESIGN.md section 5 risk #26 (live Network Sketcher MCP verification,
    supersedes this function's own former behaviour described below)**: the
    logical Port-channel/Bundle-Ether interface itself NEVER gets its own
    direct L1 link from this function (or, per the 4th return value below,
    keeps one that a PRIOR pipeline step already gave it) -- only its
    physical members do. Live MCP verification (``show l1_interface``/
    ``show l1_link`` against a rebuilt Input_data_sample9 master) confirmed
    that giving the logical name its own direct link, on top of the member
    links, double-registers that exact same name as both (a) a first-class,
    independently-linked L1 interface, and (b) the virtual/bundled port
    ``add portchannel_bulk`` synthesizes OVER those very members -- i.e. a
    phantom third "cable" between two devices (e.g. ``dc-nexus01``/
    ``dc-nexus02``) that in reality only have the two real member links.
    This is very likely the true root cause of DESIGN.md section 5 risk
    #25's "3 parallel L1 links sometimes render as 0" flakiness (see that
    entry, now cross-referenced to this one), since it inflates a
    genuinely-2-parallel-link device pair to 3.

    A follow-up live experiment (synthetic 2-device/2-member master, member-
    to-member ``add l1_link_bulk`` + ``add portchannel_bulk`` only, NO direct
    link on the logical name) confirmed this is not just harmless but
    correct: ``add portchannel_bulk`` alone makes the logical name a fully
    valid ``add ip_address_bulk``/``add l2_segment_bulk`` target (``show
    l3_interface``/``show l3_broadcast_domain`` both populate correctly),
    exactly like ``add virtual_port_bulk`` pre-declares SVI/Loopback for the
    SAME two commands (DESIGN.md section 5 risk #17's own note already
    predicted this: "Port-channel/Bundle-Ether logical interfaces get their
    IP via plain add ip_address_bulk with no add virtual_port_bulk
    pre-declaration at all"). NS's own AI-context CLI reference (RULE 9,
    "PORT-CHANNEL VLAN SYMMETRY") independently documents this exact
    "member-only l1_link_bulk + portchannel_bulk + l2_segment_bulk on the
    logical name" sequence as the canonical, mistake-free pattern -- and
    explicitly calls out "adding VLANs to physical interfaces instead of
    Port-channel" as a mistake, never "giving the Port-channel its own L1
    link". See ``apply_parsed_configs()``'s ``po_claimed_by_l1`` check
    (updated alongside this fix to recognise "will be portchannel_bulk-
    registered" as an independent, sufficient claim path, not just "is
    directly L1-linked").

    Consequently, this function's 4th return value, ``redundant_direct_
    links``, reports every ``(device, logical_port)`` pair whose direct L1
    link -- established by an EARLIER pipeline step (requirement C's direct
    subnet match, or requirement E's inferred-peer match) BEFORE this
    function ever runs -- is now redundant given the channel-group has real
    physical members that will themselves be individually linked below;
    ``build_model()`` drops that now-redundant entry from ``model.l1_links``
    using this set. This covers the "common case" described below (a routed
    Port-channel/Bundle-Ether matched directly by IP), in addition to the
    same-batch vPC-pairing and Dummy_L2-fallback paths below, which simply
    never create the direct link in the first place post-fix -- i.e. this
    fix is NOT limited to the vPC-pairing code path, it covers every way a
    Port-channel's logical name could end up with its own direct link.

    Phase 5 fix, discovered via live Network Sketcher MCP verification
    (this function's docstring doubles as the DESIGN.md 4.2/4.6 audit
    trail for that discovery): ``add portchannel_bulk`` requires every
    member port to already exist as an L1 interface ("Port not found"
    otherwise), yet a channel-group member is NEVER a subnet-matching
    candidate on its own -- ``ParsedInterface.is_routed()`` returns False
    for a switchport member with no IP of its own (only the logical
    Port-channel/Bundle-Ether interface carries the IP and therefore
    participates in requirement C/E/F). Without this step, no L1 link
    would EVER touch a port-channel's physical members, and
    ``apply_parsed_configs()``'s ``add portchannel_bulk`` emission (which
    only checks ``iface.channel_group``, not link existence) would
    unconditionally fail at the live engine.

    For every device, for every numeric channel-group id that has >=1
    physical member AND a resolvable logical interface (``kind ==
    'portchannel'`` whose trailing numeric suffix equals the channel-group
    id):
      - If that logical interface already has exactly one L1 link in
        ``model.l1_links`` (the common case -- a routed Port-channel/
        Bundle-Ether that requirement C matched directly, or requirement E
        gave an inferred peer to): every member without its own link is
        connected to the SAME peer device, using a fresh 'Dummy N' port
        name on that peer (seeded from the highest 'Dummy N' index already
        used against that peer, so it never collides with a real port or
        another synthesized link). This preserves the topological intent
        -- "these physical links are the ones composing the aggregate seen
        by the peer" -- without requiring per-member ground truth we do
        not have. The logical interface's OWN pre-existing direct link is
        reported back via ``redundant_direct_links`` (risk #26 above) since
        the members now fully subsume it.
      - **Risk #17 fix (DESIGN.md section 5, resolved)**: if the logical
        interface has NO L1 link at all, this is not "unexpected" as
        originally assumed -- live-engine verification against
        Input_data_sample5/6/9 confirmed the concrete, reproducible cause:
        a Port-channel used purely as an L2 trunk/access aggregate
        (``switchport``, no IPv4 address at all -- e.g. an NX-OS ``vpc
        peer-link`` or a plain IOS access/distribution trunk bundle) is
        NEVER a requirement-C subnet-matching candidate in the first place
        (``build_subnet_groups()`` only ever considers routed, IP-bearing
        interfaces), so it never reaches requirement C OR requirement E's
        ``synthesize_inferred_peers()`` (which only ever sees candidates
        requirement C already produced from routed interfaces). Every
        physical member of such a Port-channel would therefore stay
        permanently unregistered in the live engine's port table, and
        ``add portchannel_bulk`` would fail with "Port not found:
        <device>:<physical_port_name>" for every one of them (confirmed
        live: e.g. ``Port not found: dist-sw01:Ethernet 1/1`` for
        Input_data_sample5's NX-OS vPC peer-link).

        This Port-channel is genuinely NOT eligible for real IP-based peer
        discovery (pure-L2 trunk/adjacency detection without IP is
        explicitly out of scope for this converter's whole design, DESIGN.
        md section 4.3's general L2-trunk risk) -- inventing a real
        peer-discovery algorithm for it is not something this tool has the
        evidence to do safely. So, exactly mirroring what requirement E
        already does for every OTHER never-matched interface, this
        function now synthesizes ONE inferred ``Dummy_L2_<n>`` peer
        (NS_SWITCH -- the same "shared L2 segment hub" stencil convention
        ``needs_l2_switch_inference()``/``_materialize_l2_switch()``
        already use for an L2-only shared subnet) sized to exactly this
        Port-channel's own member count (risk #26: NOT plus one port for
        the logical interface itself anymore -- its own VLAN/trunk config,
        if any, is still applied via ``add l2_segment_bulk`` directly onto
        the logical name with no dedicated L1 link needed, see
        ``apply_parsed_configs()``'s ``po_claimed_by_l1`` check), and wires
        every member 1:1 onto it via the SAME per-peer 'Dummy N'
        port-minting logic used in the common case above. This keeps the
        already-established EtherChannel/LAG policy ("every physical
        member port ends up L1-linked, hence registered, hence
        portchannel_bulk-able") true end-to-end, even for a Port-channel
        that could never participate in subnet matching at all -- while
        remaining an honest, clearly-labelled INFERRED device/link (``
        map_inferred_peer``'s reason string says exactly why), never a
        silent guess at the real physical peer. ``type_counters`` is the
        SAME dict ``build_model()`` uses for ``_materialize_l2_switch()``,
        so ``Dummy_L2_<n>`` numbering stays globally unique across both
        sources.

    **Same-batch vPC peer-link pairing (DESIGN.md section 5, NEW risk entry
    added alongside this change)**: before falling back to Dummy_L2
    synthesis above, this function now makes ONE additional pass over every
    device's orphaned logical Port-channels looking for an explicit,
    high-confidence NX-OS ``vpc peer-link`` signal (``ParsedInterface.
    vpc_peer_link``, set by ``config_parser._consume_iface_line()``) instead
    of ever guessing from Port-channel "shape" (member count) alone --
    live-config investigation (Input_data_sample5's dist-sw01/dist-sw02 and
    Input_data_sample9's dc-nexus01/dc-nexus02) confirmed this exact keyword
    (plus, in sample9, a description literally naming the peer) is already
    present and extractable in real vPC-pair configs, and is categorically
    more reliable than inferring a peer-link from two devices merely having
    similarly-sized orphaned Port-channels (Input_data_sample6 demonstrates
    why: 4 plain-IOS access/distribution switches there each carry a
    same-shaped 2-member trunk Port-channel with NO vpc/description signal
    at all, and there is no safe way to tell which pairs of those 6
    candidates -- if any -- should actually be linked to each other; this
    tool deliberately does NOT attempt shape-only matching for exactly this
    reason, and sample6's Port-channels are therefore still Dummy_L2
    terminated after this change, unchanged from before it).

    Same-vpc_domain-id candidates (or, absent that id on either side, the
    shared "no domain id known" bucket) are grouped; a bucket of exactly 2
    is paired unconditionally (a vPC domain fundamentally has only two peer
    switches); a bucket of 3+ (only possible when several devices' domain
    ids collide or are unparsed) is resolved ONLY via mutual description
    hostname mentions (see ``_resolve_vpc_peer_link_bucket()``), with any
    remainder left for the existing Dummy_L2 fallback below -- exactly the
    same "don't guess for k>=3 without evidence" posture already documented
    for requirement C's k>=3 ambiguous-subnet case (DESIGN.md 4.3.6). A
    bucket of exactly 1 (the device's peer genuinely is not part of this
    conversion batch, e.g. Input_data_sample9's downstream Port-channel100
    vPC-to-server case, which uses a bare ``vpc 100`` -- not ``vpc
    peer-link`` -- and so never even enters this pairing step) correctly
    keeps going through to Dummy_L2 synthesis, unchanged.

    Returns (new_devices, new_links, skip_notes, redundant_direct_links) so
    ``build_model()`` can register any synthesized device, surface any
    genuine skip (e.g. no resolvable logical interface at all, so there is
    no ``portchannel_name`` to report against) in ``config_report.md``, and
    drop any now-redundant pre-existing direct link (risk #26 above) from
    ``model.l1_links`` before extending it with ``new_links``.
    """
    link_by_endpoint: Dict[Tuple[str, str], Tuple[str, str]] = {}
    for link in model.l1_links:
        link_by_endpoint[(link.a_device, link.a_port)] = (link.b_device, link.b_port)
        link_by_endpoint[(link.b_device, link.b_port)] = (link.a_device, link.a_port)

    port_counters: Dict[str, int] = {}
    for link in model.l1_links:
        for dev, port in ((link.a_device, link.a_port), (link.b_device, link.b_port)):
            m = re.match(r"^Dummy (\d+)$", port)
            if m:
                port_counters[dev] = max(port_counters.get(dev, -1), int(m.group(1)) + 1)

    def _next_port(dev_name: str) -> str:
        idx = port_counters.get(dev_name, 0)
        port_counters[dev_name] = idx + 1
        return f"Dummy {idx}"

    new_devices: List[NSDevice] = []
    new_links: List[NSL1Link] = []
    skip_notes: List[str] = []

    # Pass 1: compute every device's (logical Port-channel <-> channel-group
    # members) mapping ONCE, shared between the new vPC peer-link pairing
    # pass below and the existing per-device Dummy_L2 fallback pass.
    per_device_groups: Dict[str, Tuple[Dict[int, str], Dict[int, List[str]]]] = {}
    for device, parsed in configs.items():
        logical_iname_by_id: Dict[int, str] = {}
        members_by_id: Dict[int, List[str]] = {}
        for iname, iface in parsed.interfaces.items():
            if iface.kind == "portchannel":
                m = re.search(r"(\d+)$", iname)
                if m:
                    logical_iname_by_id[int(m.group(1))] = iname
            elif iface.kind in ("physical", "subif") and iface.channel_group is not None:
                members_by_id.setdefault(iface.channel_group, []).append(iname)
        per_device_groups[device] = (logical_iname_by_id, members_by_id)

    # Pass 2 (DESIGN.md section 5, same-batch vPC peer-link pairing): collect
    # every ORPHANED (no existing L1 link) logical Port-channel across the
    # whole batch, then try to pair up the ones carrying an explicit ``vpc
    # peer-link`` signal directly to each other -- see this function's
    # docstring and ``_resolve_vpc_peer_link_bucket()``/``_link_vpc_peer_link_
    # pair()`` above for the full rationale and algorithm.
    orphan_groups: List[_OrphanPortChannel] = []
    for device, parsed in configs.items():
        logical_iname_by_id, members_by_id = per_device_groups[device]
        for group_id, member_inames in members_by_id.items():
            logical_iname = logical_iname_by_id.get(group_id)
            if logical_iname is None:
                continue  # reported as a skip_note in pass 3 below
            ns_logical_port = normalise_port_name(logical_iname)
            if (device, ns_logical_port) in link_by_endpoint:
                continue  # already has a real peer (requirement C/E) -- not orphaned
            logical_iface = parsed.interfaces[logical_iname]
            orphan_groups.append(_OrphanPortChannel(
                device=device, group_id=group_id, logical_iname=logical_iname,
                member_inames=member_inames, vpc_peer_link=logical_iface.vpc_peer_link,
                vpc_domain=parsed.vpc_domain, description=logical_iface.description,
            ))

    paired_group_keys: Set[Tuple[str, int]] = set()
    vpc_buckets: Dict[object, List[_OrphanPortChannel]] = {}
    for group in orphan_groups:
        if not group.vpc_peer_link:
            continue
        bucket_key = group.vpc_domain if group.vpc_domain is not None else "_no_vpc_domain_"
        vpc_buckets.setdefault(bucket_key, []).append(group)

    for bucket_key, candidates in vpc_buckets.items():
        if len(candidates) < 2:
            continue  # this device's real peer is genuinely not in this batch -- Dummy_L2 fallback below, unchanged
        pairs, leftover = _resolve_vpc_peer_link_bucket(candidates)
        for a, b in pairs:
            if a.device == b.device:
                continue  # cannot happen (each device contributes <=1 group per group_id) but guard anyway
            _link_vpc_peer_link_pair(a, b, link_by_endpoint, _next_port, new_links)
            paired_group_keys.add((a.device, a.group_id))
            paired_group_keys.add((b.device, b.group_id))
        if leftover:
            skip_notes.append(
                "Same-batch vPC peer-link pairing left "
                f"{', '.join(f'{g.device} {normalise_port_name(g.logical_iname)}' for g in leftover)} "
                f"unresolved: {len(candidates)} devices in this batch all carry an "
                "orphaned 'vpc peer-link' Port-channel with the same (or no) "
                "vpc-domain id and no mutual description hint to disambiguate "
                "which pairs belong together -- falling back to Dummy_L2 "
                "synthesis for safety rather than guessing (DESIGN.md section 5)."
            )

    # Pass 3 (existing risk #17 behaviour, extended by risk #26): every
    # remaining channel-group -- i.e. every one NOT already resolved via
    # vPC peer-link pairing above -- gets every physical member wired to a
    # peer, so they remain valid L1 endpoints for ``add portchannel_bulk``.
    # Risk #26 (see docstring above): the logical Port-channel name itself
    # NEVER gets its own direct link from this pass anymore -- if one
    # already exists (the "common case" below, from requirement C/E running
    # BEFORE this function), it is now redundant and reported back via
    # ``redundant_direct_links`` for ``build_model()`` to drop from
    # ``model.l1_links``; if none exists (the Dummy_L2 fallback case), none
    # is created. Either way, ``add portchannel_bulk`` alone makes the
    # logical name a fully valid target for ``add ip_address_bulk``/``add
    # l2_segment_bulk`` (see ``apply_parsed_configs()``'s ``po_claimed_by_l1``
    # check) -- no direct link is needed for that, and having one on top of
    # the member links only creates a phantom extra parallel L1 link.
    redundant_direct_links: Set[Tuple[str, str]] = set()
    for device, parsed in configs.items():
        logical_iname_by_id, members_by_id = per_device_groups[device]

        for group_id, member_inames in members_by_id.items():
            logical_iname = logical_iname_by_id.get(group_id)
            if logical_iname is None:
                skip_notes.append(
                    f"Port-channel member link synthesis skipped for {device} "
                    f"channel-group {group_id}: no logical Port-channel/"
                    f"Bundle-Ether interface found (DESIGN.md 4.2)."
                )
                continue
            if (device, group_id) in paired_group_keys:
                continue  # already linked directly to its real vPC peer above
            ns_logical_port = normalise_port_name(logical_iname)
            peer = link_by_endpoint.get((device, ns_logical_port))
            if peer is None:
                # Risk #17 fix (see docstring above): synthesize a
                # dedicated inferred L2 peer instead of skipping, so every
                # member becomes a genuine L1 endpoint.
                code = sten.DUMMY_TYPE_CODES[sten.NS_SWITCH]
                type_counters[code] = type_counters.get(code, 0) + 1
                peer_device = f"Dummy_{code}_{type_counters[code]}"
                mapping = sten.map_inferred_peer(
                    peer_device, "l2_switch",
                    reason=(
                        f"Inferred peer for {device} {ns_logical_port} "
                        f"(channel-group {group_id}): this Port-channel "
                        f"carries no IPv4 address (pure L2 trunk/access "
                        f"aggregate), so it is never a requirement-C "
                        f"subnet-matching candidate and no real peer could "
                        f"be identified (DESIGN.md section 5 risk #17)."
                    ),
                )
                # This runs AFTER step 5's area/tier assignment (see the
                # caller's step 7.5 comment), so ``model.devices[device]``
                # already has its FINAL area -- place this synthesized peer
                # in that SAME area (rather than a stray "default") so it
                # renders next to the device it belongs to instead of in an
                # unrelated segment.
                owner = model.devices.get(device)
                peer_area = owner.area if owner is not None else "default"
                new_devices.append(NSDevice(
                    name=peer_device, area=peer_area, row=_TIER[sten.NS_SWITCH],
                    stencil=mapping, is_endpoint=False, default_color=GRAY,
                ))
                # Risk #26: NO direct link for ns_logical_port is minted
                # here anymore -- only member links below. peer_device is
                # already known; that is all this branch needs to produce.
                peer = (peer_device, None)
            else:
                # "Common case": a direct link on the logical name already
                # exists from an earlier pipeline step (requirement C's
                # direct subnet match, or requirement E's inferred-peer
                # match) -- risk #26: now redundant, since every member is
                # about to get its OWN link to the same peer device below.
                redundant_direct_links.add((device, ns_logical_port))
            peer_device, _peer_port = peer
            for member_iname in member_inames:
                ns_member_port = normalise_port_name(member_iname)
                if (device, ns_member_port) in link_by_endpoint:
                    continue
                new_link = NSL1Link(
                    a_device=device, a_port=ns_member_port,
                    b_device=peer_device, b_port=_next_port(peer_device),
                )
                new_links.append(new_link)
                link_by_endpoint[(device, ns_member_port)] = (peer_device, new_link.b_port)

    return new_devices, new_links, skip_notes, redundant_direct_links


# ---------------------------------------------------------------------------
# Pipeline step 8 — per-device L2/L3 population (DESIGN.md section 4.2's
# "そのまま流用できる部分" table): VLANs/SVIs/port-channels/sub-interfaces/
# IP assignments/VRF renames. Near-verbatim port of cml_converter/src/
# topology_mapper.py's apply_parsed_configs() (Phase 5, DESIGN.md section 6)
# -- the only behavioural differences from that original are: (1) no
# ``cml_node_labels`` parameter (config_converter has no CML-specific node
# list to cross-reference), and (2) the routing-summary assignment is
# PREPENDED to, rather than overwriting, any pre-existing
# ``routing_attribute`` (e.g. requirement F's "INFERRED: assumed
# connectivity..." note set by ``ensure_full_connectivity()``, which must
# survive this later step, DESIGN.md 4.6).
# ---------------------------------------------------------------------------

def _trunk_vlans(
    iface: ParsedInterface,
    default_trunk_vlans: Optional[List[int]] = None,
) -> List[str]:
    """VLAN list for a trunk port's L2 segment.

    The native VLAN is carried (untagged) on the trunk too, so include it
    even when it is not part of ``switchport trunk allowed vlan`` --
    otherwise its membership would be silently dropped.

    **DESIGN.md section 5 risk #28 fix** -- Cisco default-trunk behaviour:
    an interface in ``switchport mode trunk`` with NO explicit
    ``switchport trunk allowed vlan`` (and no native-vlan override) allows
    *all VLANs in the device's VLAN database* by default -- this is the
    documented IOS/IOS-XE **and** NX-OS behaviour, confirmed via MATCHA
    (see risk #28 for the source citations). The original port from
    ``cml_converter/src/topology_mapper.py`` returned an EMPTY list in that
    case, so such trunks (e.g. an NX-OS vPC peer-link ``interface
    port-channel10`` that is ``switchport mode trunk`` only) never received
    an ``add l2_segment_bulk`` entry and silently vanished from the L2
    diagram even though their ``add portchannel_bulk`` (L1) entry was fine.

    When ``trunk_allowed_vlans`` and ``trunk_native_vlan`` are both empty we
    therefore fall back to ``default_trunk_vlans`` -- the device-defined
    VLAN set (``ParsedConfig.vlans`` keys) supplied by the caller -- rather
    than a literal 1-4094 range, so the emitted L2 segment stays meaningful
    and small. VLAN 1 is intentionally excluded from that fallback set by
    the caller (see ``apply_parsed_configs``) to avoid merging unrelated
    devices into one giant default-VLAN shared segment. An EXPLICIT allowed
    list is always honoured verbatim (that path never touches the
    fallback), so this change is backward-compatible for every trunk that
    already pruned its VLANs.
    """
    vids = list(iface.trunk_allowed_vlans)
    if iface.trunk_native_vlan is not None and iface.trunk_native_vlan not in vids:
        vids.append(iface.trunk_native_vlan)
    if not vids and default_trunk_vlans:
        vids = list(default_trunk_vlans)
    return [f"Vlan{v}" for v in vids]


def _l1_unclaimed_skip_note(label: str, ns_port: str, command: str) -> str:
    """Build a ``config_report.md`` note explaining why ``command`` was
    skipped for ``(label, ns_port)``.

    Live-engine constraint (confirmed via Network Sketcher MCP
    verification, DESIGN.md section 6 Phase 5 row): a physical /
    port-channel / sub-interface / management port only becomes known to
    the engine's internal port table when it is referenced as an
    endpoint of a successful ``add l1_link_bulk`` entry -- unlike SVI and
    Loopback ports, there is no separate ``add virtual_port_bulk``
    pre-declaration path for these kinds. A port that carries an IP/VLAN
    in the parsed config but never becomes an L1 link endpoint (e.g. a
    spare hub-side port with more physical capacity than the number of
    spokes actually present, or a LAN-side port on a device whose WAN
    uplink is the only interface selected for L1 linking) would silently
    fail or no-op if targeted by ``add ip_address_bulk`` / ``add
    l2_segment_bulk`` on the live engine. This is a command-generation-
    time decision only: the raw parsed data remains fully visible in
    config_inventory.csv / the routing-summary attribute text.
    """
    return (
        f"{label} {ns_port}: {command} SKIPPED (this port never became an "
        f"L1 link endpoint -- the live engine only recognises a physical/"
        f"port-channel/sub-interface/mgmt port once an `add l1_link_bulk` "
        f"entry references it; there is no virtual_port_bulk-style "
        f"pre-declaration path for these kinds, unlike SVI/Loopback). The "
        f"parsed IP/VLAN data remains visible in config_inventory.csv."
    )


def apply_parsed_configs(
    model: NSModel,
    configs: Dict[str, ParsedConfig],
) -> Tuple[Dict[str, Dict[str, int]], List[str]]:
    """Requirement/pipeline step 8 (DESIGN.md section 4.2, section 6 Phase
    5) -- walk every REAL device's ``ParsedConfig`` and populate
    ``model.virtual_ports`` / ``model.ip_assignments`` / ``model.
    l2_segments_phys`` / ``model.l2_segments_svi`` / ``model.port_channels``
    / ``model.subinterfaces`` / ``model.vrf_renames``, plus each device's
    ``routing_attribute`` summary text.

    Ported near-verbatim from ``cml_converter/src/topology_mapper.py``'s
    ``apply_parsed_configs()`` (see this module's own section-header comment
    for the two intentional deltas). Only devices already present in
    ``model.devices`` are processed (inferred ``Dummy_<TC>_<n>`` devices
    have no ``ParsedConfig`` and are skipped automatically via the dict
    lookup below) -- runs on EVERY real device unconditionally, including
    ones routed to the requirement-G isolated area: DESIGN.md 4.7.1 only
    forbids an L1 link crossing that area's boundary, it does not forbid
    populating a closed device's own observed VLAN/IP/VRF attributes.

    **Phase 5 fix, discovered via live Network Sketcher MCP verification**:
    when requirement C matches two devices' SVIs directly (an L3-routed
    VLAN uplink with no separate physical-trunk evidence -- e.g. an access
    switch and its distribution switch both exchange the same subnet on
    "Vlan 10"), ``model.l1_links`` already uses that SVI's port name
    (``normalise_port_name('Vlan10') == 'Vlan 10'``) as the L1 link
    endpoint (built in an EARLIER pipeline step, before this function
    runs). The live engine's ``add virtual_port_bulk`` can NEVER declare a
    virtual port under a name that already exists as a plain L1 interface
    (confirmed empirically: "Vlan 10 conflicts with L1 interface on
    <device>") -- and since ``add l1_link_bulk`` always runs before ``add
    virtual_port_bulk`` in the mandatory NS command phase order (DESIGN.md
    4.2), there is no ordering fix available once C has already claimed
    the SVI as an L1 endpoint. This function therefore SKIPS the
    ``model.virtual_ports`` / ``model.ip_assignments`` / ``model.
    vrf_renames`` entries for any SVI whose ``(device, port)`` pair is
    already an L1 link endpoint -- the VLAN membership itself
    (``model.l2_segments_svi``) is NOT skipped, since ``add
    l2_segment_bulk`` works on any port regardless of virtual-port status
    (also confirmed empirically). The skipped IP is NOT lost data: it is
    still recorded in ``config_inventory.csv``/``config_report.md`` from
    the raw parse, just not reflected in the NS model's L3 table for this
    specific dual-role port. Loopback and Port-channel/Bundle-Ether IPs are
    NEVER affected by this (Loopbacks are never matching-eligible against
    a peer's SVI in practice, and Port-channel/Bundle-Ether logical
    interfaces get their IP via plain ``add ip_address_bulk`` with no
    ``add virtual_port_bulk`` pre-declaration at all -- see the
    ``portchannel`` branch below).

    Returns (per-device stat dict [VLAN/SVI/loopback/L3-physical/L2-trunk/
    L2-access/port-channel/VRF counts], skip_notes) for config_report.md.
    """
    l1_endpoints: Set[Tuple[str, str]] = set()
    devices_with_l1_link: Set[str] = set()
    for link in model.l1_links:
        l1_endpoints.add((link.a_device, link.a_port))
        l1_endpoints.add((link.b_device, link.b_port))
        devices_with_l1_link.add(link.a_device)
        devices_with_l1_link.add(link.b_device)

    stats: Dict[str, Dict[str, int]] = {}
    skip_notes: List[str] = []
    for label, cfg_parsed in configs.items():
        dev = model.devices.get(label)
        if dev is None:
            continue
        is_endpoint = dev.is_endpoint
        st = {"vlans": 0, "svi": 0, "loopback": 0,
              "l3_phys": 0, "l2_trunk": 0, "l2_access": 0, "portchannel": 0, "vrf": 0}
        st["vlans"] = len(cfg_parsed.vlans)

        if label not in devices_with_l1_link:
            # Live-engine constraint (discovered via Network Sketcher MCP
            # verification, DESIGN.md section 6 Phase 5 row): `add
            # device_location` performs its device<->L2/L3 sync LAZILY, and
            # the only command in this pipeline that forces it is a
            # successful `add l1_link_bulk` touching that device. A device
            # with ZERO L1 links -- structurally always true for a
            # requirement-G fully-closed device routed to the isolated area
            # (DESIGN.md 4.7.1: "No L1 link (real or inferred) crosses this
            # area's boundary"), and possible for any other device left
            # fully unconnected by an unusual/incomplete input corpus -- is
            # therefore never synced, so `add virtual_port_bulk` / `add
            # ip_address_bulk` / `add l2_segment_bulk` calls against it
            # would all fail with "not found" on the live engine even
            # though every port name is textually valid. Skip emitting
            # those NS commands entirely for such a device (this is a
            # command-generation-time decision, not a data-loss one: the
            # raw parsed IPs/VLANs/loopbacks remain fully visible in
            # config_inventory.csv / the routing-summary attribute text).
            skip_notes.append(
                f"{label}: virtual_port_bulk/ip_address_bulk/l2_segment_bulk "
                f"SKIPPED for this ENTIRE device (0 L1 links -- the live "
                f"engine only performs its deferred device/L2/L3 sync when "
                f"at least one `add l1_link_bulk` entry touches the "
                f"device, per DESIGN.md 4.7.1's 'no link crosses the "
                f"isolated area boundary' rule for fully-closed devices). "
                f"The device's parsed interfaces/IPs/VLANs remain fully "
                f"visible in config_inventory.csv and this report."
            )
            stats[label] = st
            continue

        po_members: Dict[int, List[str]] = {}

        # DESIGN.md section 5 risk #26: precompute which channel-group ids
        # have >=1 physical member BEFORE the main loop below (the logical
        # ``kind == "portchannel"`` interface's config-file line often comes
        # BEFORE its members' ``channel-group`` lines, so ``po_members``
        # itself -- built incrementally by the loop below -- cannot be
        # relied on yet at that point). A channel-group with >=1 member is
        # guaranteed (by ``synthesize_portchannel_member_links()``, which
        # already ran in step 7.5) to have every member individually
        # L1-linked and to receive an ``add portchannel_bulk`` entry below,
        # which alone is sufficient to make the logical Port-channel name a
        # valid ``add ip_address_bulk``/``add l2_segment_bulk`` target on
        # the live engine (see that function's docstring for the live-MCP
        # evidence) -- so the logical name no longer needs to ALSO be its
        # own direct L1 link endpoint for ``po_claimed_by_l1`` below.
        channel_groups_with_members: Set[int] = {
            iface.channel_group
            for iface in cfg_parsed.interfaces.values()
            if iface.kind in ("physical", "subif") and iface.channel_group is not None
        }

        # DESIGN.md section 5 risk #28: the Cisco default-trunk VLAN set for
        # this device -- every VLAN explicitly declared in its VLAN database
        # (``vlan <id>`` / ``vlan <id> name ...``), used by ``_trunk_vlans``
        # as the fallback membership for a ``switchport mode trunk`` port
        # that has NO explicit ``switchport trunk allowed vlan`` (Cisco
        # default = "all VLANs in the database"; confirmed via MATCHA).
        # VLAN 1 is deliberately EXCLUDED here: it is the universal default
        # VLAN present on essentially every device, so folding it into every
        # default-trunk fallback would collapse otherwise-unrelated devices
        # into a single enormous VLAN1 shared segment in the L2 diagram.
        # Explicitly-allowed VLAN 1 (``switchport trunk allowed vlan 1,...``)
        # is unaffected -- that path keeps its VLANs verbatim and never uses
        # this fallback. Both ends of a point-to-point trunk/Port-channel
        # (e.g. a vPC peer-link) each substitute their own device VLAN DB;
        # since peers normally share the same VLAN set this preserves RULE 9
        # (PORT-CHANNEL VLAN SYMMETRY) naturally, without any cross-device
        # union that could introduce phantom VLANs.
        default_trunk_vlans: List[int] = sorted(
            vid for vid in cfg_parsed.vlans if vid != 1
        )

        for iname, iface in cfg_parsed.interfaces.items():
            ns_port = normalise_port_name(iname)

            if iface.kind == "svi":
                if not is_endpoint and iface.ipv4:
                    vid = _vlan_id_from_ifname(iname)
                    svi_claimed_by_l1 = (label, ns_port) in l1_endpoints
                    if svi_claimed_by_l1:
                        skip_notes.append(
                            f"{label} {ns_port}: virtual_port_bulk/ip_address_bulk/"
                            f"l3_instance SKIPPED (SVI already claimed as an L1 link "
                            f"endpoint by requirement C's subnet matching -- the live "
                            f"engine cannot declare a virtual port under a name that "
                            f"already exists as an L1 interface). VLAN membership "
                            f"(l2_segment_bulk) is still applied; the IP itself "
                            f"remains available in config_inventory.csv."
                        )
                    else:
                        model.virtual_ports.append(NSVirtualPort(
                            device=label, port=ns_port, vlan_id=vid,
                        ))
                        model.ip_assignments.append(NSIPAssignment(
                            device=label, port=ns_port,
                            cidrs=[a.cidr for a in list(iface.ipv4) + list(iface.ipv4_secondary)],
                        ))
                        if iface.vrf:
                            model.vrf_renames.append((label, ns_port, iface.vrf))
                    if vid is not None:
                        model.l2_segments_svi.append(NSL2Segment(
                            device=label, port=ns_port,
                            vlans=[f"Vlan{vid}"],
                        ))
                    st["svi"] += 1

            elif iface.kind == "loopback":
                if iface.ipv4:
                    model.virtual_ports.append(NSVirtualPort(
                        device=label, port=ns_port, is_loopback=True,
                    ))
                    model.ip_assignments.append(NSIPAssignment(
                        device=label, port=ns_port,
                        cidrs=[a.cidr for a in list(iface.ipv4) + list(iface.ipv4_secondary)],
                    ))
                    if iface.vrf:
                        model.vrf_renames.append((label, ns_port, iface.vrf))
                    st["loopback"] += 1

            elif iface.kind == "portchannel":
                # DESIGN.md section 5 risk #26 fix: a Port-channel is
                # "claimed" (safe to target with ip_address_bulk/
                # l2_segment_bulk) either the OLD way (it is itself a
                # direct L1 link endpoint -- still possible if it has no
                # channel-group members at all, e.g. an incomplete/unusual
                # input corpus) OR -- newly recognised -- because its
                # channel-group has >=1 physical member and will therefore
                # receive its own ``add portchannel_bulk`` entry below,
                # which alone pre-declares the logical name on the live
                # engine (see synthesize_portchannel_member_links()'s
                # docstring for the live-MCP evidence backing this). The
                # two conditions are not mutually exclusive; either one
                # alone is sufficient.
                po_id_match = re.search(r"(\d+)$", iname)
                po_bulk_registered = (
                    po_id_match is not None
                    and int(po_id_match.group(1)) in channel_groups_with_members
                )
                po_claimed_by_l1 = (label, ns_port) in l1_endpoints or po_bulk_registered
                if iface.ipv4:
                    if po_claimed_by_l1:
                        model.ip_assignments.append(NSIPAssignment(
                            device=label, port=ns_port,
                            cidrs=[a.cidr for a in list(iface.ipv4) + list(iface.ipv4_secondary)],
                        ))
                        st["l3_phys"] += 1
                    else:
                        skip_notes.append(_l1_unclaimed_skip_note(label, ns_port, "ip_address_bulk"))
                # DESIGN.md risk #28: treat the Port-channel as a trunk
                # either when it carries explicit VLAN evidence OR when it is
                # simply ``switchport mode trunk`` with no allowed-vlan list
                # (Cisco default = all device VLANs); ``_trunk_vlans`` then
                # supplies the device VLAN-DB fallback. This is what makes a
                # bare ``interface port-channelN / switchport mode trunk``
                # (e.g. an NX-OS vPC peer-link) reach the L2 diagram.
                po_is_trunk = (
                    iface.mode == "trunk"
                    or bool(iface.trunk_allowed_vlans)
                    or iface.trunk_native_vlan is not None
                )
                if not is_endpoint and po_is_trunk:
                    vlans = _trunk_vlans(iface, default_trunk_vlans)
                    if vlans:
                        if po_claimed_by_l1:
                            model.l2_segments_phys.append(NSL2Segment(
                                device=label, port=ns_port, vlans=vlans,
                            ))
                            st["l2_trunk"] += 1
                        else:
                            skip_notes.append(_l1_unclaimed_skip_note(label, ns_port, "l2_segment_bulk"))
                if iface.access_vlan and not is_endpoint:
                    if po_claimed_by_l1:
                        model.l2_segments_phys.append(NSL2Segment(
                            device=label, port=ns_port,
                            vlans=[f"Vlan{iface.access_vlan}"],
                        ))
                        st["l2_access"] += 1
                    else:
                        skip_notes.append(_l1_unclaimed_skip_note(label, ns_port, "l2_segment_bulk"))
                if iface.vrf:
                    model.vrf_renames.append((label, ns_port, iface.vrf))

            elif iface.kind in ("physical", "subif"):
                if iface.channel_group is not None:
                    po_members.setdefault(iface.channel_group, []).append(ns_port)
                    st["portchannel"] += 1
                    continue
                phys_claimed_by_l1 = (label, ns_port) in l1_endpoints
                if iface.kind == "subif":
                    parent_port = normalise_port_name(iname.split(".", 1)[0])
                    # Bugfix (found via live sample2 "R1" verification,
                    # DESIGN.md section 5 risk #22): a sub-interface that
                    # carries its own routable IP is ALSO a requirement-C
                    # subnet-matching candidate in its own right (see
                    # build_subnet_groups()). When no better peer exists for
                    # that VLAN's subnet in the input corpus (or a real peer
                    # IS found directly against this exact sub-interface),
                    # requirement C/E wires the L1 link straight onto THIS
                    # sub-interface's OWN port name (e.g.
                    # 'GigabitEthernet 0/1.100' <-> an inferred Dummy_RT_<n>
                    # peer) rather than onto its parent -- `phys_claimed_by_l1`
                    # (computed above from `model.l1_links`, the definitive
                    # "already consumed as an L1 endpoint" record) is True in
                    # that case. `add vport_l1if_direct_binding` would then
                    # try to declare that SAME port name a second time, as a
                    # virtual port layered on the parent physical interface
                    # -- the live NS engine rejects this with a naming
                    # conflict ("the L1 interface name is the same as the
                    # vport it is trying to bind"), because a sub-interface
                    # that is already its own direct L1 link endpoint is not
                    # also a logical vport overlay on the parent; the two
                    # representations are mutually exclusive for the same
                    # (device, sub-interface) pair. So `model.subinterfaces`
                    # (which drives `cmd_add_vport_l1if_direct_binding()` /
                    # `cmd_add_vport_l2_direct_binding()`) MUST NOT receive an
                    # entry for this sub-interface when `phys_claimed_by_l1`
                    # is True. This is the inverse of the common case (parent
                    # physical port is the real/inferred L1 link, e.g. a
                    # router-on-a-stick trunk with NO IP on the parent -- see
                    # the ``else`` branch below), which is left completely
                    # unchanged and still emits the vport binding as before.
                    if phys_claimed_by_l1:
                        skip_notes.append(
                            f"{label} {ns_port}: vport_l1if_direct_binding/"
                            f"vport_l2_direct_binding SKIPPED (this sub-interface "
                            f"is itself an L1 link endpoint -- requirement C's "
                            f"subnet matching linked '{ns_port}' directly to a "
                            f"real or inferred peer, rather than treating "
                            f"'{parent_port}' as the physical link with this "
                            f"sub-interface layered on top as a virtual port. "
                            f"Binding it a second time onto the parent would "
                            f"conflict with its own L1 port registration on the "
                            f"live engine). Its IP address is still applied via "
                            f"ip_address_bulk below, since it is already a valid "
                            f"L1 port in its own right."
                        )
                    else:
                        model.subinterfaces.append(NSSubInterface(
                            device=label, parent_port=parent_port,
                            subif_port=ns_port, vlan_id=iface.access_vlan,
                        ))
                    if iface.ipv4:
                        if phys_claimed_by_l1:
                            model.ip_assignments.append(NSIPAssignment(
                                device=label, port=ns_port,
                                cidrs=[a.cidr for a in list(iface.ipv4) + list(iface.ipv4_secondary)],
                            ))
                            if iface.vrf:
                                model.vrf_renames.append((label, ns_port, iface.vrf))
                            st["l3_phys"] += 1
                        else:
                            skip_notes.append(_l1_unclaimed_skip_note(label, ns_port, "ip_address_bulk"))
                    continue
                if iface.is_routed() and iface.ipv4:
                    if phys_claimed_by_l1:
                        model.ip_assignments.append(NSIPAssignment(
                            device=label, port=ns_port,
                            cidrs=[a.cidr for a in list(iface.ipv4) + list(iface.ipv4_secondary)],
                        ))
                        if iface.vrf:
                            model.vrf_renames.append((label, ns_port, iface.vrf))
                        st["l3_phys"] += 1
                    else:
                        skip_notes.append(_l1_unclaimed_skip_note(label, ns_port, "ip_address_bulk"))
                else:
                    if is_endpoint:
                        if iface.ipv4:
                            if phys_claimed_by_l1:
                                model.ip_assignments.append(NSIPAssignment(
                                    device=label, port=ns_port,
                                    cidrs=[a.cidr for a in iface.ipv4],
                                ))
                                st["l3_phys"] += 1
                            else:
                                skip_notes.append(_l1_unclaimed_skip_note(label, ns_port, "ip_address_bulk"))
                    elif iface.mode == "trunk":
                        # DESIGN.md risk #28: a bare ``switchport mode trunk``
                        # physical port with no explicit allowed-vlan list
                        # defaults to all device VLANs; ``_trunk_vlans``
                        # supplies that fallback (guarded by ``if vlans`` so a
                        # device with an empty VLAN DB still emits nothing).
                        if phys_claimed_by_l1:
                            vlans = _trunk_vlans(iface, default_trunk_vlans)
                            if vlans:
                                model.l2_segments_phys.append(NSL2Segment(
                                    device=label, port=ns_port, vlans=vlans,
                                ))
                                st["l2_trunk"] += 1
                        else:
                            skip_notes.append(_l1_unclaimed_skip_note(label, ns_port, "l2_segment_bulk"))
                    elif iface.access_vlan:
                        if phys_claimed_by_l1:
                            model.l2_segments_phys.append(NSL2Segment(
                                device=label, port=ns_port,
                                vlans=[f"Vlan{iface.access_vlan}"],
                            ))
                            st["l2_access"] += 1
                        else:
                            skip_notes.append(_l1_unclaimed_skip_note(label, ns_port, "l2_segment_bulk"))

            elif iface.kind == "mgmt":
                if iface.ipv4:
                    if (label, ns_port) in l1_endpoints:
                        model.ip_assignments.append(NSIPAssignment(
                            device=label, port=ns_port,
                            cidrs=[a.cidr for a in list(iface.ipv4) + list(iface.ipv4_secondary)],
                        ))
                        if iface.vrf:
                            model.vrf_renames.append((label, ns_port, iface.vrf))
                        st["l3_phys"] += 1
                    else:
                        skip_notes.append(_l1_unclaimed_skip_note(label, ns_port, "ip_address_bulk"))

            # Tunnel / nve / others: routing-summary text only (no dedicated
            # NS command for these in this phase's scope).

        for po_id, members in po_members.items():
            model.port_channels.append(NSPortChannel(
                device=label,
                physical_ports=sorted(set(members)),
                portchannel_name=f"Port-channel {po_id}",
            ))

        if cfg_parsed.routing_summary_lines:
            summary = "\n".join(cfg_parsed.routing_summary_lines[:30])
            existing = dev.routing_attribute
            dev.routing_attribute = f"{summary}\n{existing}" if existing else summary

        stats[label] = st
    return stats, skip_notes


def synthesize_dummy_portchannel_mirrors(model: "NSModel") -> List[str]:
    """Mirror a REAL device's Port-channel onto its inferred ``Dummy_`` peer
    when every physical member of that Port-channel lands on the SAME Dummy
    device (DESIGN.md section 5 risk #29).

    Background: ``synthesize_portchannel_member_links()`` (step 7.5) already
    gives every physical channel-group member its own L1 link, and -- for a
    pure-L2 trunk/access Port-channel that never matched a real peer -- wires
    all those members onto ONE freshly-minted ``Dummy_L2_<n>`` peer's ``Dummy
    <k>`` ports. But it only creates the *member* links; the Dummy side is left
    as a bag of independent access ports, NOT a Port-channel. The live NS L2
    diagram therefore shows the real switch's uplink correctly bundled while
    the Dummy end is a set of separate links -- asymmetric with the real
    device's ``add portchannel_bulk`` entry.

    This function closes that gap: after ``apply_parsed_configs()`` has
    populated ``model.port_channels`` (real devices) and ``model.
    l2_segments_phys`` (their trunk/access VLANs), for every real Port-channel
    whose members all terminate on the same Dummy peer it appends:
      1. a mirror ``NSPortChannel`` on that Dummy, bundling exactly the peer
         ``Dummy <k>`` ports the members connect to (name 'Port-channel 1',
         or the next free number if that Dummy already carries one), and
      2. the SAME VLAN list the real Port-channel carries (RULE 9,
         PORT-CHANNEL VLAN SYMMETRY) as an ``NSL2Segment`` on the mirror --
         but only if the real side actually has one (a routed/L3 Port-channel
         with no ``add l2_segment_bulk`` entry gets no L2 mirror either).

    The members are already valid L1 endpoints (step 7.5), so the mirror
    ``add portchannel_bulk`` never hits "Port not found". Only the "all members
    on a single Dummy" case is mirrored; a Port-channel whose members split
    across several Dummies (or land on a real peer) is left untouched and a
    skip note is recorded. Returns skip_notes for ``config_report.md``.
    """
    skip_notes: List[str] = []

    # (device, port) -> (peer_device, peer_port), both directions.
    peer_by_endpoint: Dict[Tuple[str, str], Tuple[str, str]] = {}
    for link in model.l1_links:
        peer_by_endpoint[(link.a_device, link.a_port)] = (link.b_device, link.b_port)
        peer_by_endpoint[(link.b_device, link.b_port)] = (link.a_device, link.a_port)

    # (device, logical Port-channel port) -> its VLAN list (real side).
    vlans_by_po: Dict[Tuple[str, str], List[str]] = {}
    for seg in model.l2_segments_phys:
        vlans_by_po.setdefault((seg.device, seg.port), []).extend(seg.vlans)

    def _is_dummy(name: str) -> bool:
        return name.startswith("Dummy_")

    # Per-Dummy bookkeeping so a Dummy that already carries a Port-channel
    # (existing or just-mirrored) gets a fresh, non-colliding number.
    existing_po_names: Dict[str, Set[str]] = {}
    for pc in model.port_channels:
        existing_po_names.setdefault(pc.device, set()).add(pc.portchannel_name)

    def _next_po_name(dummy: str) -> str:
        used = existing_po_names.setdefault(dummy, set())
        idx = 1
        while f"Port-channel {idx}" in used:
            idx += 1
        name = f"Port-channel {idx}"
        used.add(name)
        return name

    for pc in list(model.port_channels):
        if _is_dummy(pc.device):
            continue  # never mirror a mirror
        peer_devices: Set[str] = set()
        peer_ports: List[str] = []
        missing = False
        for member_port in pc.physical_ports:
            peer = peer_by_endpoint.get((pc.device, member_port))
            if peer is None:
                missing = True
                break
            peer_devices.add(peer[0])
            peer_ports.append(peer[1])
        if missing or not peer_ports:
            continue
        if len(peer_devices) != 1:
            skip_notes.append(
                f"Dummy Port-channel mirror skipped for {pc.device} "
                f"{pc.portchannel_name}: its members terminate on "
                f"{len(peer_devices)} distinct peers "
                f"({', '.join(sorted(peer_devices))}), not a single Dummy peer "
                f"(DESIGN.md section 5 risk #29)."
            )
            continue
        dummy = next(iter(peer_devices))
        if not _is_dummy(dummy):
            continue  # real peer (e.g. a paired vPC peer-link) -- nothing to mirror
        mirror_name = _next_po_name(dummy)
        model.port_channels.append(NSPortChannel(
            device=dummy,
            physical_ports=sorted(set(peer_ports)),
            portchannel_name=mirror_name,
        ))
        # RULE 9 (PORT-CHANNEL VLAN SYMMETRY): copy the real end's VLANs onto
        # the mirror. A routed/L3 Port-channel with no l2_segment entry gets
        # no L2 mirror either (keeps both ends symmetric in every case).
        real_vlans = vlans_by_po.get((pc.device, pc.portchannel_name))
        if real_vlans:
            model.l2_segments_phys.append(NSL2Segment(
                device=dummy, port=mirror_name,
                vlans=list(dict.fromkeys(real_vlans)),  # dedup, preserve real-side order
            ))

    return skip_notes


@dataclass
class MapperReport:
    """Counts + human-readable notes for config_report.md.
    TODO: extend with whatever counters make the DESIGN.md 4.3-4.8 decisions
    auditable (ambiguous_matches, inferred_peers, wan_interfaces,
    closed_interfaces, inferred_connectivity_links, ...)."""
    devices: int = 0
    l1_links: int = 0
    inferred_peers: int = 0
    inferred_l2_switches: int = 0       # Dummy_L2_<n> devices synthesized by needs_l2_switch_inference() (DESIGN.md 4.3.3/4.3.6)
    ambiguous_matches: int = 0
    real_hub_matches: int = 0           # k>=3 groups resolved to a REAL device hub (Phase 2, DESIGN.md 4.3.6 priority 1)
    best_pair_matches: int = 0          # k>=3 groups resolved via best_pair degree-similarity matching (Phase 2, DESIGN.md 4.3.6 priority 2)
    closed_interfaces: int = 0
    closed_devices_isolated: int = 0    # devices placed in cfg['isolated_area_name'] (DESIGN.md 4.7.1)
    wan_interfaces: int = 0
    excluded_links: int = 0             # == len(excluded_candidates); rows written to config_excluded_links.csv (DESIGN.md 4.3.9)
    inferred_connectivity_links: int = 0  # links added by ensure_full_connectivity() (DESIGN.md 4.6)
    network_groups: int = 0             # connected components found by compute_tiers_and_areas() (requirement D, DESIGN.md 4.4)
    notes: List[str] = field(default_factory=list)
    # The actual excluded MatchResult objects (DESIGN.md 4.3.9), so convert.py's
    # main() can call write_excluded_links_csv(report.excluded_candidates, ...)
    # without build_model() needing a wider return signature. build_model()
    # MUST populate this list (from infer_l1_links_from_subnets()'s internal
    # excluded-group bookkeeping) whenever it increments excluded_links above.
    excluded_candidates: List["MatchResult"] = field(default_factory=list)
    # DESIGN.md 4.7.1: one human-readable audit line per fully-closed device
    # (device name + which interfaces were closed and why), for
    # config_report.md's isolated-area section.
    closed_device_details: List[str] = field(default_factory=list)


def build_model(configs: Dict[str, ParsedConfig], cfg: Dict) -> Tuple[NSModel, List["sten.StencilMapping"], MapperReport]:
    """Top-level orchestration — the config_converter equivalent of every
    other converter's ``<platform>_mapper.build_model()``.

    Pipeline (DESIGN.md section 3, Phase 2-6 — all 13 section-8 decisions
    are final, see DESIGN.md section 9 for the consolidated summary), wired
    together in order:
      1. infer_l1_links_from_subnets()               (requirement C, incl.
         the L2 switch inference gate and config_excluded_links.csv export)
      2. score_wan_interface() for every interface     (requirement H,
         fully config-driven wan_signal_weights scoring)
      3. detect_closed_environments()                  (requirement G)
      4. stencil_mapper.map_device() per real device (IOS/IOS-XE/NX-OS/
         IOS-XR/ASA(FTD/FDM) all in scope, DESIGN.md 4.2.1)
      5. compute_tiers_and_areas() -> build_area_layout()  (requirement D,
         networkx/Blossom REQUIRED; isolated devices routed to the single
         cfg['isolated_area_name'] area here -- MUST pass
         isolated_area_name=cfg['isolated_area_name'] into
         ns_model.build_area_layout() so its config_converter-local
         ``_area_sort_key()`` extension sorts that area to the rightmost
         column, DESIGN.md 4.7.1)
      6. synthesize_inferred_peers()                   (requirement E,
         Dummy_<TC>_<n> naming)
      7. ensure_full_connectivity()                     (requirement F,
         defaults to "exhaustive" -- assume_fully_connected=True -- and
         runs after step 3's closed-environment results are available so it
         never reconsiders devices already routed to the isolated area)
      7.5. synthesize_portchannel_member_links()        (Phase 5 fix,
         DESIGN.md 4.2/4.6 -- see that function's own docstring; discovered
         via live Network Sketcher MCP verification that ``add
         portchannel_bulk`` requires every member port to already exist as
         an L1 interface)
      8. apply per-device L2/L3 population using the SAME approach as
         cml_converter's ``apply_parsed_configs()`` (VLANs, SVIs, port-channels,
         sub-interfaces, IP assignments, VRF renames) — this part is a
         near-verbatim port, see DESIGN.md section 4.2's "そのまま流用できる
         部分" table.
    Return (model, mappings, report).

    Phase 5 implementation scope (DESIGN.md section 6 roadmap): ALL 8
    pipeline steps above (plus the 7.5 fix) are wired up for real:
      - step 8 (Phase 5): ``model.virtual_ports`` / ``ip_assignments`` /
        ``l2_segments_*`` / ``port_channels`` / ``subinterfaces`` /
        ``vrf_renames`` are now populated -- ns_commands.txt emits Phase 1
        (areas/devices), Phase 2 (l1_link_bulk/port_info), Phase 3
        (portchannel_bulk/virtual_port_bulk/l2_segment_bulk), Phase 4
        (ip_address_bulk/l3_instance), and Phase 6 (attribute_bulk) NS
        commands -- the full L1/L2/L3 topology (including the isolated
        area and cross-site exclusions) is exercised/verified via the
        Network Sketcher MCP.

    Step 3 (G, Phase 4) detail: ``detect_closed_environments()`` runs right
    after step 0 (it only needs ``configs``, no dependency on requirement
    C's matching). Its ``fully_closed_devices`` result is threaded through
    EVERY later step so a closed device never gains an L1 link to anything
    outside the isolated area (DESIGN.md 4.7.1's "いかなる他エリアとの間の
    L1リンクも生成しない"):
      - requirement C's own output (``l1_links`` + the L2-switch star links)
        is passed through ``_drop_cross_isolation_links()`` right after both
        are assembled, dropping any link with exactly one closed endpoint
        (a link between two closed devices is kept -- it stays inside the
        isolated area).
      - ``unmatched`` candidates belonging to a closed device are filtered
        out BEFORE requirement E's ``synthesize_inferred_peers()`` runs, so
        a closed device's dangling interface never gains an inferred peer
        link either (that would also cross the isolation boundary).
      - requirement F's ``ensure_full_connectivity()`` already excludes
        ``closed_devices`` from its eligible-device set up front (see that
        function's own docstring), so it never re-connects an isolated
        device to the shared cloud.
      - requirement D's ``compute_tiers_and_areas()`` already excludes any
        link touching a closed device from its connected-component graph
        (see that function's own docstring), and step 4 below routes every
        closed device straight to ``cfg['isolated_area_name']``.
    """
    report = MapperReport()
    model = NSModel()
    mappings: List[sten.StencilMapping] = []

    # Step 0 (Phase 2 prerequisite): map every REAL device to its Stencil
    # BEFORE requirement C's subnet matching runs, so the k>=3 best_pair
    # tie-break (DESIGN.md 4.3.4 rule 3, Stencil-tier proximity) has real
    # tier data available. This mapping is reused, unchanged, for step 4
    # below -- it is computed only once.
    mappings_by_device: Dict[str, sten.StencilMapping] = {}
    for device, parsed in configs.items():
        has_svi = any(iface.kind == "svi" for iface in parsed.interfaces.values())
        has_routing_protocol = bool(parsed.local_asn) or bool(parsed.bgp_peers) or any(
            re.match(r"^\s*router\s+(ospf|ospfv3|eigrp|isis)\b", line, re.IGNORECASE)
            for line in parsed.routing_summary_lines
        )
        mappings_by_device[device] = sten.map_device(
            name=device, os_family=parsed.os_family,
            interface_count=len(parsed.interfaces),
            has_svi=has_svi, has_routing_protocol=has_routing_protocol,
        )
    stencil_tiers = {dev: _TIER.get(m.stencil_type, 6) for dev, m in mappings_by_device.items()}

    # Step 3 (requirement G, Phase 4): detect closed devices BEFORE
    # requirement C's matching output is finalized, so its l1_links can be
    # filtered against closed_devices before requirement E/F ever see them
    # (see docstring above). Only depends on configs. Honours
    # cfg['closed_environment_detection'] (default True) -- if explicitly
    # disabled, every device is treated as "not closed" (pre-Phase-4
    # behaviour), e.g. for a user who wants shutdown/ACL-deny-all
    # interfaces rendered normally rather than isolated.
    if cfg.get("closed_environment_detection", True):
        closed_ifaces_by_device, closed_devices = detect_closed_environments(configs)
    else:
        closed_ifaces_by_device, closed_devices = {}, set()
    report.closed_interfaces = sum(len(v) for v in closed_ifaces_by_device.values())
    report.closed_devices_isolated = len(closed_devices)
    report.closed_device_details = [
        describe_closed_device_reason(dev, closed_ifaces_by_device[dev], configs[dev])
        for dev in sorted(closed_devices)
    ]

    # Step 1 (requirement C).
    l1_links, unmatched, notes, l2_switch_specs, excluded, ambiguous_count, strategy_counts = (
        infer_l1_links_from_subnets(configs, cfg, stencil_tiers=stencil_tiers)
    )
    report.notes.extend(notes)
    report.excluded_candidates = excluded
    report.excluded_links = sum(len(mr.unmatched) for mr in excluded)
    report.ambiguous_matches = ambiguous_count
    report.real_hub_matches = strategy_counts.get("real_hub", 0)
    report.best_pair_matches = strategy_counts.get("blossom", 0) + strategy_counts.get("brute_force", 0)

    # Closed devices must never gain an inferred peer (requirement E) for a
    # dangling interface -- that would cross the isolation boundary just as
    # much as a real link would (DESIGN.md 4.7.1).
    unmatched = [c for c in unmatched if c.device not in closed_devices]

    type_counters: Dict[str, int] = {}
    for network, candidates in l2_switch_specs:
        device, star_links = _materialize_l2_switch(network, candidates, type_counters)
        model.devices[device.name] = device
        # Phase A fix (DESIGN.md 4.7.1 / section 5, "stencil_tiers pipeline
        # consistency"): register this inferred L2-switch dummy's Stencil tier
        # NOW, at creation time, BEFORE step 5's compute_tiers_and_areas()
        # runs. Without this entry, layout.calculate_integrated_tier() would
        # find no stencil tier for the dummy and fall back to the name/degree
        # role tier ALONE -- a degree-2 Dummy_L2_<n> resolves to role tier 6
        # (endpoint/bottom row) instead of blending in _TIER[NS_SWITCH]=4, so
        # the dummy would drop to the bottom row far from the switches it
        # bridges. setdefault keeps this consistent with how every other
        # inferred device (steps 6/7 below) seeds stencil_tiers.
        stencil_tiers.setdefault(
            device.name, _TIER.get(device.stencil.stencil_type, 6)
        )
        l1_links.extend(star_links)
        report.inferred_l2_switches += 1

    # DESIGN.md 4.7.1: drop any requirement-C link (direct/real_hub/
    # best_pair/L2-switch-star) that crosses the isolation boundary --
    # applied once, generically, over every link source this step produced.
    l1_links = _drop_cross_isolation_links(l1_links, closed_devices)

    # Step 2 (requirement H): WAN scoring, plus the 'no_matching_peer_in_corpus'
    # booster (DESIGN.md 4.8) now that requirement C's matching is known.
    wan_scores = compute_wan_scores(configs, cfg)
    booster = (cfg.get("wan_signal_weights", {}) or {}).get("no_matching_peer_in_corpus", 0.0)
    for cand in unmatched:
        key = (cand.device, cand.interface)
        wan_scores[key] = max(0.0, min(1.0, wan_scores.get(key, 0.0) + booster))
    wan_threshold = cfg.get("wan_confidence_threshold", 0.5)
    report.wan_interfaces = sum(1 for v in wan_scores.values() if v >= wan_threshold)

    # Step 4: materialize the step-0 stencil mappings into NSDevice entries
    # (IOS/IOS-XE/NX-OS/IOS-XR/ASA(FTD/FDM) all in scope, DESIGN.md 4.2.1). The
    # area/row set here is a PLACEHOLDER only -- step 5b below overwrites
    # both for every non-isolated, non-cloud device once the final
    # connected-component/tier data (after E + F) is known; it only matters
    # for devices that end up with literally no link at all (never touched
    # by step 5b's ``tiers``/``linked_names`` loop).
    for device, parsed in configs.items():
        mapping = mappings_by_device[device]
        mappings.append(mapping)
        area = cfg.get("isolated_area_name", "Closed") if device in closed_devices else "default"
        model.devices[device] = NSDevice(
            name=device, area=area, row=_TIER.get(mapping.stencil_type, 1),
            stencil=mapping,
            is_endpoint=mapping.stencil_type in (sten.NS_PC, sten.NS_SERVER, sten.NS_PHONE),
        )

    # Step 6 (requirement E): synthesize a placeholder for every candidate
    # requirement C left unmatched. Runs BEFORE step 5's layout so that
    # inferred peers participate in the SAME connected-component/tier graph
    # as real devices (DESIGN.md 4.4 item 5 -- a degree==1 inferred leaf
    # naturally lands on tier 7 via layout.calculate_tier_by_device_role()).
    inferred_devices, inferred_links = synthesize_inferred_peers(unmatched, wan_scores, cfg)
    for dev in inferred_devices:
        model.devices[dev.name] = dev
        if dev.name != "Dummy_CL_1":
            report.inferred_peers += 1
    l1_links.extend(inferred_links)
    model.l1_links = l1_links

    # Extend stencil_tiers (step-0 was real-devices-only) with every
    # inferred device's own Stencil tier, so step 5's
    # calculate_integrated_tier() has a baseline for them too.
    for dev in inferred_devices:
        stencil_tiers.setdefault(dev.name, _TIER.get(dev.stencil.stencil_type, 6))

    # Step 7 (requirement F): star-connect any remaining isolated component
    # (size >= cfg['min_component_size_for_inference']) to the shared WAN/
    # Core cloud, using the FIRST-pass component/tier data (pre-F) to pick
    # each component's representative device. Must run AFTER requirement
    # G's closed_devices is known (structural G > F integration, decisions
    # 2/6) -- closed_devices is threaded straight through to
    # ensure_full_connectivity(), which excludes it entirely up front.
    f_devices, f_links = ensure_full_connectivity(model, stencil_tiers, closed_devices, cfg)
    for dev in f_devices:
        stencil_tiers.setdefault(dev.name, _TIER.get(dev.stencil.stencil_type, 6))
    model.l1_links.extend(f_links)
    report.inferred_connectivity_links = len(f_links)

    # Step 5 (requirement D, final pass): now that E's inferred peers AND
    # F's connectivity-guarantee links are both in model.l1_links, compute
    # the DEFINITIVE connected components + tiers and apply them as each
    # device's area/row. Cloud waypoints (area='wan') and isolated/closed
    # devices (area=cfg['isolated_area_name']) are already correctly placed
    # and are skipped here -- neither participates in ordinary area
    # assignment (DESIGN.md 4.7.1 / 4.4 item 2).
    #
    # DESIGN.md 4.4.1 (feature: waypoint-excluded area grouping): AREAS are the
    # connected components of the NON-WAYPOINT subgraph, so a shared cloud
    # waypoint (``Dummy_CL_1``, ``stencil_type == NS_CLOUD``) never bridges
    # otherwise-separate real-device groups into one area. ``waypoint_devices``
    # is identified by NS_CLOUD stencil type (and, defensively, the
    # ``Dummy_CL`` name prefix). TIERS are still computed on the full,
    # cloud-inclusive graph inside ``compute_tiers_and_areas()`` so
    # edge-router WAN-adjacency tiering never regresses.
    # TIER/ROW ASSIGNMENT happens HERE (before step 7.5), exactly as before
    # this feature: tiers are computed on the pre-7.5 full (cloud-inclusive)
    # graph so no port-channel-member sibling link (step 7.5) can perturb the
    # tier scale -- preserving byte-for-byte row placement for every sample.
    # AREA GROUPING is DEFERRED to step 8.6 below (after step 7.5/8/8.5), so
    # it sees the FINAL link set: a device whose ONLY L1 links are the
    # port-channel member links synthesized in step 7.5 (e.g. sample3's
    # access/distribution switches) is NOT mistaken for a truly linkless
    # device (DESIGN.md 4.4.1).
    isolated_area_name = cfg.get("isolated_area_name", "Closed")
    waypoint_devices = {
        name for name, dev in model.devices.items()
        if dev.stencil.stencil_type == sten.NS_CLOUD or name.startswith("Dummy_CL")
    }
    _, tiers = compute_tiers_and_areas(
        model.l1_links, stencil_tiers, closed_devices, cfg, waypoint_devices
    )
    for name, dev in model.devices.items():
        if name in closed_devices or dev.stencil.stencil_type == sten.NS_CLOUD:
            continue
        dev.row = tiers.get(name, dev.row)

    # Step 7.5 (Phase 5 fix, DESIGN.md 4.2/4.6): give every LAG/port-channel
    # physical member its own L1 link (mirroring the logical interface's
    # peer) so the live engine's ``add portchannel_bulk`` has a real L1
    # interface to bind for every member -- see
    # synthesize_portchannel_member_links()'s docstring for the full
    # rationale (discovered via Network Sketcher MCP verification). Runs
    # AFTER step 5's area/tier assignment is final: these sibling links
    # never change connectivity between DEVICES (the logical interface
    # already connects the same device pair), so they must not perturb the
    # connected-component/tier computation above.
    #
    # DESIGN.md section 5 risk #26: the logical Port-channel/Bundle-Ether
    # name itself must NEVER also be a direct ``l1_link_bulk`` endpoint once
    # its members are individually linked (it double-registers the same
    # name as both a first-class L1 interface and the virtual/bundled port
    # ``add portchannel_bulk`` synthesizes over those members -- a phantom
    # extra parallel link between the same two devices). ``redundant_direct_
    # links`` reports any such pre-existing direct link (from requirement
    # C/E, which ran earlier in this same function) so it can be dropped
    # here, before ``model.l1_links`` is extended with the member links
    # that now fully represent that same connectivity.
    po_member_devices, po_member_links, po_member_skip_notes, po_redundant_direct_links = (
        synthesize_portchannel_member_links(model, configs, type_counters)
    )
    for dev in po_member_devices:
        model.devices[dev.name] = dev
        report.inferred_peers += 1
    if po_redundant_direct_links:
        model.l1_links = [
            link for link in model.l1_links
            if (link.a_device, link.a_port) not in po_redundant_direct_links
            and (link.b_device, link.b_port) not in po_redundant_direct_links
        ]
    model.l1_links.extend(po_member_links)
    report.notes.extend(po_member_skip_notes)

    # Step 8 (Phase 5, DESIGN.md 4.2): per-device L2/L3 population (VLANs/
    # SVIs/port-channels/sub-interfaces/IP assignments/VRF renames), ported
    # from cml_converter's apply_parsed_configs(). Runs LAST, after every
    # device's final area/row (step 5) is known, so it only needs
    # dev.is_endpoint (already stable since step 4) -- area/row placement
    # itself is untouched by this step.
    _, l2l3_skip_notes = apply_parsed_configs(model, configs)
    report.notes.extend(l2l3_skip_notes)

    # Step 8.5 (DESIGN.md section 5 risk #29): once real Port-channels and
    # their VLANs are populated, mirror any Port-channel whose members all
    # terminate on a single inferred Dummy_ peer onto that Dummy (same bundle
    # + same VLANs, RULE 9 PORT-CHANNEL VLAN SYMMETRY) so both ends of the
    # aggregate render symmetrically. Additive only -- never touches the real
    # devices' entries. Members are already valid L1 endpoints (step 7.5), so
    # the mirror `add portchannel_bulk` cannot hit "Port not found".
    po_mirror_skip_notes = synthesize_dummy_portchannel_mirrors(model)
    report.notes.extend(po_mirror_skip_notes)

    # Step 8.6 (DESIGN.md 4.4.1, feature: waypoint-excluded area grouping):
    # AREA assignment, run HERE (after step 7.5's port-channel member links,
    # step 8's L2/L3 population, and step 8.5's Dummy mirrors) so it operates
    # on the DEFINITIVE ``model.l1_links``. Two graphs are still kept apart:
    # tiers/rows were already assigned at step 5 (pre-7.5 graph, unchanged);
    # this step only sets ``dev.area``.
    #
    #   * Areas are the connected components of the NON-WAYPOINT subgraph
    #     (waypoint/cloud edges dropped as bridges) -- each contiguously-wired
    #     set of non-waypoint devices becomes its own side-by-side area.
    #   * A device wired ONLY to a waypoint (still "wired", just not to any
    #     other real device) becomes its own size-1 area.
    #   * A TRULY linkless device (no L1 link at all, e.g. sample2's S1/S2) and
    #     every requirement-G fully-closed device land together in the ONE
    #     shared "Closed" area (``isolated_area_name``, sorted rightmost).
    #   * Area NAMING scheme (deterministic; surfaced for review): a single
    #     non-waypoint component keeps the historical name ``default``;
    #     multiple components each get ``segment_NN`` ordered by (size DESC,
    #     min device name ASC).
    area_waypoints = {
        name for name, dev in model.devices.items()
        if dev.stencil.stencil_type == sten.NS_CLOUD or name.startswith("Dummy_CL")
    }
    area_filtered_links = [
        lk for lk in model.l1_links
        if lk.a_device not in closed_devices and lk.b_device not in closed_devices
    ]
    area_components = layout.compute_area_components(area_filtered_links, area_waypoints)
    linked_names = {n for comp in area_components for n in comp}
    has_any_link: Set[str] = set()
    for lk in model.l1_links:
        has_any_link.add(lk.a_device)
        has_any_link.add(lk.b_device)
    for name, dev in model.devices.items():
        if name in linked_names or name in closed_devices:
            continue
        if name in area_waypoints:
            continue
        if name in has_any_link:
            area_components.append({name})
    area_components.sort(key=lambda c: (-len(c), min(c)))

    report.network_groups = len(area_components)
    gwidth = max(2, len(str(max(len(area_components), 1))))
    node_group: Dict[str, str] = {}
    if len(area_components) <= 1:
        for comp in area_components:
            for n in comp:
                node_group[n] = "default"
    else:
        for idx, comp in enumerate(area_components, 1):
            gid = f"segment_{idx:0{gwidth}d}"
            for n in comp:
                node_group[n] = gid

    for name, dev in model.devices.items():
        if name in closed_devices or dev.stencil.stencil_type == sten.NS_CLOUD:
            continue
        if name in node_group:
            dev.area = node_group[name]
        else:
            dev.area = isolated_area_name

    areas, area_to_devices = build_area_layout(
        model.devices, model.l1_links, layout="auto",
        isolated_area_name=isolated_area_name,
    )
    model.areas = areas
    model.area_to_devices = area_to_devices

    report.devices = len(model.devices)
    report.l1_links = len(model.l1_links)
    if closed_devices:
        report.notes.append(
            f"{len(closed_devices)} device(s) were classified as fully closed "
            f"(DESIGN.md 4.7) and routed to the dedicated isolated area "
            f"'{isolated_area_name}' -- see config_report.md's isolated-device "
            "list for the interface-level rationale. No L1 link (real or "
            "inferred) crosses this area's boundary (DESIGN.md 4.7.1)."
        )
    if excluded and cfg.get("site_scoping", False):
        n_cross_site = sum(
            1 for mr in excluded
            if mr.excluded_reason and mr.excluded_reason.startswith("cross-site")
        )
        if n_cross_site:
            report.notes.append(
                f"{n_cross_site} shared-subnet group(s) were excluded due to "
                "cross-site RFC1918 subnet reuse with no distinguishing "
                "evidence (DESIGN.md 4.3.9) -- see config_excluded_links.csv."
            )
    report.notes.append(
        "Phase 5 scope (DESIGN.md section 6): requirements C-H (matching incl. "
        "cross-site exclusion, WAN scoring, closed-environment detection, "
        "stencil mapping, layout/tiering, inferred-peer synthesis, and the "
        "full-connectivity guarantee) and pipeline step 8 (per-device "
        "VLAN/SVI/port-channel/sub-interface/IP/VRF population) are all "
        "implemented."
    )
    return model, mappings, report
