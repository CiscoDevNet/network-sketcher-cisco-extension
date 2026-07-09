# Copyright 2026 Cisco Systems, Inc. and its affiliates
# SPDX-License-Identifier: Apache-2.0

"""config_converter-local port of ``3rd_party/netbox_converter/src/netbox_layout.py``
(DESIGN.md section 4.4, requirement D — "netbox_converter流レイアウト適用").

DESIGN.md 4.4 explicitly endorses porting this file "ほぼそのまま"
(near-verbatim): the hostname-keyword dictionaries and the
clusters/tiers/connected-components algorithm are copied here with only the
adaptations needed to plug into config_converter's own ``NSL1Link`` dataclass
(``ns_model.py``, which already has identical ``a_device``/``a_port``/
``b_device``/``b_port`` field names, so ``build_graph()`` below needs no
translation layer) and to fold in ``cfg['role_keyword_overrides']``
(config-driven keyword additions, DESIGN.md 4.4 item 3 / section 7).

Deliberately NOT ported from ``netbox_layout.py`` (unused by
``netbox_mapper.py``'s own integration, and not needed by DESIGN.md 4.4):
``calculate_tier_by_centrality`` (an alternative, centrality-only tiering
strategy that ``netbox_mapper.py`` itself never calls) and
``extract_device_number`` (only used by netbox_converter's own PPTX
coordinate logic, irrelevant to a NS ``device_location`` grid).

Per AGENTS.md ("Keep each tool self-contained (own README.md +
requirements.txt)") this is a local COPY, not a cross-tool import — the two
files are allowed to drift over time; see ``ns_model.py``'s module docstring
for the analogous, already-established precedent of documented divergence
between per-converter copies of a shared-origin file.
"""
from __future__ import annotations

import re
from collections import defaultdict
from typing import Dict, List, Optional, Set, Tuple

import networkx as nx

from .ns_model import NSL1Link


# --- name helpers (verbatim from netbox_layout.py) --------------------------

def extract_device_base_name(device_name: str) -> str:
    """Base name without a trailing number, for clustering detection."""
    return re.sub(r"[-_]?\d+$", "", device_name).lower()


def get_redundant_keywords() -> Set[str]:
    return {
        "mpls", "wan", "internet", "isp", "carrier", "provider",
        "transit", "peering", "upstream", "backbone", "metro",
        "edge", "border", "bdr", "dmz", "perimeter",
        "core", "spine", "dcn",
        "dist", "distribution", "agg", "aggregation", "distrib",
        "acc", "access", "leaf", "tor", "floor", "closet", "idf",
        "sw", "switch", "rtr", "router", "rt", "gw", "gateway",
        "fw", "firewall", "fwall", "lb", "loadbalancer", "balancer",
        "vpn", "wlc", "wireless", "ctl", "ctrl", "controller",
        "ap", "accesspoint", "srv", "server", "host",
        "sec", "security", "ids", "ips", "asa", "ngfw", "utm",
        "pri", "primary", "secondary", "std", "standby",
        "actv", "active", "prim", "back", "backup", "ha", "red", "redundant",
        "main", "spare", "hot", "cold", "warm",
        "top", "bot", "bottom", "left", "right", "rght",
        "east", "west", "north", "south", "nth", "sth",
        "a", "b", "c", "d", "side",
        "abr", "asbr", "pe", "ce", "p", "rr", "reflector", "bgp",
        "dc", "datacenter", "pod", "rack", "row", "zone",
        "bldg", "building", "campus", "site", "branch", "remote",
        "stack", "cluster", "vss", "vpc", "mlag", "lag",
        "mgmt", "management", "oob", "console", "admin",
        "prod", "production", "dev", "test", "staging", "lab",
    }


def get_wan_keywords() -> Set[str]:
    return {
        "mpls", "wan", "internet", "isp", "carrier", "provider",
        "transit", "peering", "upstream", "backbone", "metro",
        "l3vpn", "vpls", "evpn", "sdwan", "overlay",
    }


# --- clustering + tiering (verbatim logic, parameterised by graph) ----------

def detect_device_clusters(G: "nx.Graph") -> Dict[str, Set[str]]:
    """Detect redundant-pair clusters (same base name + shared role keyword)."""
    if G.number_of_nodes() == 0:
        return {}
    base_name_groups: Dict[str, Set[str]] = defaultdict(set)
    for node in G.nodes():
        base_name_groups[extract_device_base_name(node)].add(node)

    clusters: Dict[str, Set[str]] = {}
    cluster_id = 0
    redundant_keywords = get_redundant_keywords()
    wan_keywords = get_wan_keywords()

    for base_name, devices in base_name_groups.items():
        if len(devices) < 2:
            continue
        devices_list = list(devices)
        if any(wan_kw in base_name.lower() for wan_kw in wan_keywords):
            continue
        matching_keyword = None
        for keyword in redundant_keywords:
            if all(keyword in d.lower() for d in devices_list):
                matching_keyword = keyword
                break
        if not matching_keyword:
            continue
        degrees = [G.degree(d) for d in devices_list]
        avg_degree = sum(degrees) / len(degrees) if degrees else 0
        similar_devices = set()
        for device in devices_list:
            if avg_degree == 0 or abs(G.degree(device) - avg_degree) <= max(avg_degree * 0.5, 2):
                similar_devices.add(device)
        if len(similar_devices) >= 2:
            clusters[f"cluster_{cluster_id}"] = similar_devices
            cluster_id += 1
    return clusters


# Bucket-name aliases accepted in cfg['role_keyword_overrides'] (DESIGN.md 4.4
# item 3 / section 7's "sample": {"core": ["core", "backbone"]}). Any override
# key not in this set is ignored (documented in compute_tiers_and_areas()).
_ROLE_BUCKET_NAMES = (
    "wan", "edge_router", "core", "distribution", "aggregation_sw",
    "security", "access", "endpoint",
)


def calculate_tier_by_device_role(
    G: "nx.Graph",
    node: str,
    role_keyword_overrides: Optional[Dict[str, List[str]]] = None,
) -> int:
    """Tier from device-role keywords in the name + connectivity (verbatim
    from netbox_layout.py, plus ``role_keyword_overrides`` keyword merging --
    DESIGN.md 4.4 item 3)."""
    degree = G.degree(node)
    name_lower = node.lower()
    overrides = role_keyword_overrides or {}

    def _merged(bucket: str, base: Set[str]) -> Set[str]:
        extra = overrides.get(bucket)
        return base | {str(k).lower() for k in extra} if extra else base

    wan_keywords = _merged("wan", {
        "mpls", "wan", "inet", "isp", "carr", "prov",
        "trans", "peer", "upstr", "bckbn",
        "extnet", "pubcld", "cldgw", "egress", "brdwan",
    })
    edge_router_base_keywords = _merged("edge_router", {
        "rtr", "router", "rt",
        "brdrtr", "bdrrtr", "perimr", "branch", "siter",
        "cer", "vpnedg", "inetedg", "dmzrtr", "ingres",
        "egres", "mplsed",
    })
    core_keywords = _merged("core", {
        "core", "spine", "fabric", "bckbn", "centr", "dccore",
        "mainc", "hspeed", "routco", "superc", "netwco",
        "enterc", "distco", "nxos", "iosxe",
    })
    distribution_keywords = _merged("distribution", {
        "dist", "agg", "distrib",
        "agglay", "campdi", "bldist", "flrdst", "accagg",
        "l3dist", "l3d", "routdi", "ivlr", "policy",
        "catalyst", "nexus",
    })
    aggregation_sw_base_keywords = _merged("aggregation_sw", {
        "sw", "switch",
        "aggsw", "stkmst", "stkmem", "clstagg", "idfagg",
        "mdfagg", "uplink", "intsw", "bldsw", "flrsw",
        "dataagg", "netagg", "mlagg",
    })
    security_keywords = _merged("security", {
        "fw", "firewall", "lb", "vpn", "gw", "gateway",
        "secapp", "utm", "ids", "ips", "proxy",
        "wlc", "asa", "webgw", "emailgw",
    })
    access_keywords = _merged("access", {
        "edge", "acc", "access",
        "accsw", "client", "user", "wkstat", "poe",
        "desksw", "flracc", "clstac", "portac", "endpt",
        "datapt", "cubicl",
    })
    endpoint_keywords = _merged("endpoint", {
        "host", "server", "pc", "wkstat", "print",
        "ipphon", "camera", "iot", "ap", "client",
        "term", "vm", "vmhost", "sensor", "hmi",
    })

    if any(k in name_lower for k in wan_keywords):
        return 0
    if any(k in name_lower for k in edge_router_base_keywords):
        neighbors = list(G.neighbors(node))
        has_external = any(
            any(wan_kw in str(G[node][n].get("links", [{}])[0].get("connection", "")).lower()
                for wan_kw in wan_keywords)
            for n in neighbors if G.has_edge(node, n)
        )
        return 1 if has_external else 3
    if any(k in name_lower for k in core_keywords):
        return 2
    if any(k in name_lower for k in distribution_keywords):
        return 4
    if any(k in name_lower for k in aggregation_sw_base_keywords):
        if any(k in name_lower for k in access_keywords):
            return 6
        if degree <= 3:
            return 6
        return 5
    if any(k in name_lower for k in security_keywords):
        return 2
    if degree == 1 or any(k in name_lower for k in endpoint_keywords):
        return 7
    if degree >= 8:
        return 3
    if degree >= 4:
        return 4
    return 6


def calculate_integrated_tier(
    G: "nx.Graph",
    clusters: Dict[str, Set[str]],
    stencil_tiers: Optional[Dict[str, int]] = None,
    role_keyword_overrides: Optional[Dict[str, List[str]]] = None,
) -> Dict[str, int]:
    """Tier per node = min(name-keyword role tier, Stencil-derived tier).

    Verbatim logic from netbox_layout.py: config_converter has only the
    device NAME (+ graph degree) to infer a role via keywords, so this blends
    that with ``stencil_tiers`` (this converter's own ``_TIER`` dict, keyed
    by Stencil type — Cloud=0 .. Server/PC/Phone=5, see topology_mapper.py).
    Taking the min() keeps the MORE SPECIFIC of the two signals (e.g. name
    'core' -> tier 2 beats stencil L3Switch -> 3, so 'core' still sits above
    'distribution'); this deliberately mixes two non-identical tier SCALES
    (0-7 name/degree-based vs. 0-5 Stencil-based) as a heuristic blend, which
    is the same pattern already shipped by
    ``3rd_party/netbox_converter/src/netbox_mapper.py`` (its own ``_TIER``
    dict is 0-5, fed into this same ``min()``). Redundant pairs are pulled to
    a common tier (cluster constraint), as in the offline tool.
    """
    if G.number_of_nodes() == 0:
        return {}
    stencil_tiers = stencil_tiers or {}
    node_tiers: Dict[str, int] = {}
    for node in G.nodes():
        role_tier = calculate_tier_by_device_role(G, node, role_keyword_overrides)
        st = stencil_tiers.get(node)
        node_tiers[node] = min(role_tier, st) if st is not None else role_tier
    for _cid, cluster_devices in clusters.items():
        cluster_tiers = [node_tiers[n] for n in cluster_devices if n in node_tiers]
        if cluster_tiers:
            ct = min(cluster_tiers)
            for n in cluster_devices:
                node_tiers[n] = ct
    return node_tiers


# --- top-level: graph -> (network groups, tiers) ----------------------------

def build_graph(links: List[NSL1Link]) -> "nx.Graph":
    """Build the undirected device graph from ``NSL1Link`` objects, tagging
    each edge with a 'connection' string (peer names/ports) so the
    edge-router external-WAN check in ``calculate_tier_by_device_role`` works
    the same as in netbox_layout.py."""
    G = nx.Graph()
    for lk in links:
        if lk.a_device == lk.b_device:
            continue
        conn = f"{lk.a_device} {lk.a_port} {lk.b_device} {lk.b_port}"
        G.add_edge(lk.a_device, lk.b_device, links=[{"connection": conn}])
    return G


def compute_network_groups_and_tiers(
    links: List[NSL1Link],
    stencil_tiers: Optional[Dict[str, int]] = None,
    role_keyword_overrides: Optional[Dict[str, List[str]]] = None,
) -> Tuple[List[Set[str]], Dict[str, int]]:
    """Return (connected components sorted large->small, {device: tier}).

    For each connected component, detect redundant-pair clusters (offline
    logic), then compute the integrated tier blending the name-keyword tier
    with the Stencil-role tier (``stencil_tiers``). Devices with NO link at
    all are NOT in the graph (as in the offline tool) and therefore absent
    from both the returned components and the tiers dict — the caller
    (``topology_mapper.compute_tiers_and_areas`` / ``ensure_full_connectivity``)
    is responsible for handling such zero-degree devices as their own
    size-1 pseudo-component (DESIGN.md 4.6 item 5,
    ``min_component_size_for_inference``).
    """
    G = build_graph(links)
    if G.number_of_nodes() == 0:
        return [], {}
    components = sorted(
        nx.connected_components(G), key=lambda c: (-len(c), min(c))
    )
    tiers: Dict[str, int] = {}
    for comp in components:
        sub = G.subgraph(comp).copy()
        clusters = detect_device_clusters(sub)
        tiers.update(
            calculate_integrated_tier(sub, clusters, stencil_tiers, role_keyword_overrides)
        )
    return components, tiers


def compute_area_components(
    links: List[NSL1Link],
    waypoint_devices: Optional[Set[str]] = None,
) -> List[Set[str]]:
    """Return the connected components of the NON-WAYPOINT subgraph, sorted
    large->small (then by ``min(name)`` for determinism), for AREA grouping.

    Contrast with ``compute_network_groups_and_tiers()`` above, which builds a
    single graph that INCLUDES waypoint/cloud edges (used for TIER assignment,
    where a device's WAN/cloud adjacency legitimately matters). This helper
    instead DROPS every link that touches a ``waypoint_devices`` node before
    computing components, so a shared cloud waypoint (e.g. ``Dummy_CL_1``,
    ``stencil_type == NS_CLOUD``) does NOT bridge otherwise-separate
    real-device groups into a single area (DESIGN.md 4.4.1 "waypoint-excluded
    connected-component area grouping"). This is what makes each set of
    contiguously-wired NON-waypoint devices become its own side-by-side area.

    Devices with NO non-waypoint link at all (linkless, or wired only to a
    waypoint) are absent from the returned components -- the caller
    (``topology_mapper.build_model``) is responsible for routing them (a
    truly linkless device to the shared "Closed" area; a waypoint-only device
    to its own singleton area).
    """
    wp = waypoint_devices or set()
    non_wp_links = [
        lk for lk in links
        if lk.a_device not in wp and lk.b_device not in wp
    ]
    G = build_graph(non_wp_links)
    if G.number_of_nodes() == 0:
        return []
    return sorted(
        nx.connected_components(G), key=lambda c: (-len(c), min(c))
    )
