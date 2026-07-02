# Copyright 2026 Cisco Systems, Inc. and its affiliates
# SPDX-License-Identifier: Apache-2.0

"""netbox_layout.py — placement logic ported from the offline Network Sketcher.

This is a faithful port of the device-placement algorithm in the offline edition
(``network-sketcher_offline/ns_option_convert_to_master.py``,
``ns_option_convert_to_master_csv``): devices are grouped by **connected
component** ("network group"), and within each group a **tier** (0 = WAN/top …
7 = endpoints) is derived from graph centrality + device-role name keywords, with
redundant pairs forced onto the same tier.

The keyword sets, centrality weights, percentile thresholds and the
role→tier decision tree are copied verbatim from the offline tool so the two
produce the same tiering. Only the *output* differs: the offline tool draws
continuous (x, y) coordinates into a PPTX, whereas here the tier becomes the NS
``add device_location`` grid row and the shared crossing-minimising column
ordering (``ns_model._place_columns``) handles the horizontal order.

Uses ``networkx`` (as the offline tool does) for the centrality metrics and
connected-component detection.
"""
from __future__ import annotations

import re
from collections import defaultdict
from typing import Dict, List, Set, Tuple

import networkx as nx


# --- name helpers (verbatim from the offline tool) -------------------------

def extract_device_base_name(device_name: str) -> str:
    """Base name without a trailing number, for clustering detection."""
    return re.sub(r"[-_]?\d+$", "", device_name).lower()


def extract_device_number(device_name: str) -> int:
    numbers = re.findall(r"\d+", device_name)
    return int(numbers[-1]) if numbers else 0


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


def calculate_tier_by_centrality(G: "nx.Graph") -> Dict[str, int]:
    """Tier from centrality metrics (WAN keywords forced to tier 0)."""
    if G.number_of_nodes() == 0:
        return {}
    degree_centrality = nx.degree_centrality(G)
    betweenness_centrality = nx.betweenness_centrality(G)
    closeness_centrality = nx.closeness_centrality(G)
    try:
        eigenvector_centrality = nx.eigenvector_centrality(G, max_iter=1000)
    except Exception:
        eigenvector_centrality = {node: 0 for node in G.nodes()}

    node_scores: Dict[str, float] = {}
    wan_keywords = get_wan_keywords()
    for node in G.nodes():
        if any(wan_kw in node.lower() for wan_kw in wan_keywords):
            node_scores[node] = 1.0
            continue
        node_scores[node] = (
            degree_centrality[node] * 0.3
            + betweenness_centrality[node] * 0.4
            + closeness_centrality[node] * 0.2
            + eigenvector_centrality[node] * 0.1
        )

    sorted_nodes = sorted(node_scores.items(), key=lambda x: x[1], reverse=True)
    node_tiers: Dict[str, int] = {}
    num_nodes = len(sorted_nodes)
    for i, (node, _score) in enumerate(sorted_nodes):
        if any(wan_kw in node.lower() for wan_kw in wan_keywords):
            node_tiers[node] = 0
            continue
        percentile = i / num_nodes if num_nodes > 0 else 0
        if percentile < 0.05:
            tier = 1
        elif percentile < 0.15:
            tier = 2
        elif percentile < 0.30:
            tier = 3
        elif percentile < 0.50:
            tier = 4
        elif percentile < 0.70:
            tier = 5
        elif percentile < 0.85:
            tier = 6
        else:
            tier = 7
        node_tiers[node] = tier
    return node_tiers


def calculate_tier_by_device_role(G: "nx.Graph", node: str) -> int:
    """Tier from device-role keywords in the name + connectivity (verbatim)."""
    degree = G.degree(node)
    name_lower = node.lower()

    wan_keywords = {"mpls", "wan", "inet", "isp", "carr", "prov",
                    "trans", "peer", "upstr", "bckbn",
                    "extnet", "pubcld", "cldgw", "egress", "brdwan"}
    edge_router_base_keywords = {"rtr", "router", "rt",
                                 "brdrtr", "bdrrtr", "perimr", "branch", "siter",
                                 "cer", "vpnedg", "inetedg", "dmzrtr", "ingres",
                                 "egres", "mplsed"}
    core_keywords = {"core", "spine", "fabric", "bckbn", "centr", "dccore",
                     "mainc", "hspeed", "routco", "superc", "netwco",
                     "enterc", "distco", "nxos", "iosxe"}
    distribution_keywords = {"dist", "agg", "distrib",
                             "agglay", "campdi", "bldist", "flrdst", "accagg",
                             "l3dist", "l3d", "routdi", "ivlr", "policy",
                             "catalyst", "nexus"}
    aggregation_sw_base_keywords = {"sw", "switch",
                                    "aggsw", "stkmst", "stkmem", "clstagg", "idfagg",
                                    "mdfagg", "uplink", "intsw", "bldsw", "flrsw",
                                    "dataagg", "netagg", "mlagg"}
    security_keywords = {"fw", "firewall", "lb", "vpn", "gw", "gateway",
                         "secapp", "utm", "ids", "ips", "proxy",
                         "wlc", "asa", "webgw", "emailgw"}
    access_keywords = {"edge", "acc", "access",
                       "accsw", "client", "user", "wkstat", "poe",
                       "desksw", "flracc", "clstac", "portac", "endpt",
                       "datapt", "cubicl"}
    endpoint_keywords = {"host", "server", "pc", "wkstat", "print",
                         "ipphon", "camera", "iot", "ap", "client",
                         "term", "vm", "vmhost", "sensor", "hmi"}

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


def calculate_integrated_tier(G: "nx.Graph", clusters: Dict[str, Set[str]],
                              stencil_tiers: Dict[str, int] = None) -> Dict[str, int]:
    """Tier per node = min(name-keyword role tier, NetBox-role stencil tier).

    The offline tool has only the device NAME to infer a role, so it blends a
    name-keyword tier with graph centrality. Here we ALSO have NetBox's real
    device role (via the stencil), which is far more reliable — a device whose
    name lacks a role keyword (e.g. a router named '...test_updated') is no
    longer mis-tiered by degree, and a switch whose degree is inflated by
    synthetic host stubs is no longer pulled up. Taking the min keeps the more
    specific of the two signals (e.g. name 'core' -> tier 2 beats stencil
    L3Switch -> 3, so core still sits above distribution). Redundant pairs are
    pulled to a common tier (cluster constraint), as in the offline tool.
    """
    if G.number_of_nodes() == 0:
        return {}
    stencil_tiers = stencil_tiers or {}
    node_tiers: Dict[str, int] = {}
    for node in G.nodes():
        role_tier = calculate_tier_by_device_role(G, node)
        st = stencil_tiers.get(node)
        node_tiers[node] = min(role_tier, st) if st is not None else role_tier
    for _cid, cluster_devices in clusters.items():
        cluster_tiers = [node_tiers[n] for n in cluster_devices if n in node_tiers]
        if cluster_tiers:
            ct = min(cluster_tiers)
            for n in cluster_devices:
                node_tiers[n] = ct
    return node_tiers


# --- top-level: graph → (network groups, tiers) ----------------------------

def build_graph(links) -> "nx.Graph":
    """Build the undirected device graph from NSL1Link objects, tagging each
    edge with a 'connection' string (peer names/ports) so the edge-router
    external-WAN check in calculate_tier_by_device_role works as offline."""
    G = nx.Graph()
    for lk in links:
        if lk.a_device == lk.b_device:
            continue
        conn = f"{lk.a_device} {lk.a_port} {lk.b_device} {lk.b_port}"
        G.add_edge(lk.a_device, lk.b_device, links=[{"connection": conn}])
    return G


def compute_network_groups_and_tiers(
        links, stencil_tiers: Dict[str, int] = None) -> Tuple[List[Set[str]], Dict[str, int]]:
    """Return (connected components sorted large→small, {device: tier}).

    For each connected component, detect redundant-pair clusters (offline logic),
    then compute the integrated tier blending the name-keyword tier with the
    NetBox-role stencil tier (``stencil_tiers``). Isolated devices (no link) are
    NOT in the graph, so — as in the offline tool — they are not placed.
    """
    G = build_graph(links)
    if G.number_of_nodes() == 0:
        return [], {}
    components = sorted(nx.connected_components(G),
                        key=lambda c: (-len(c), min(c)))
    tiers: Dict[str, int] = {}
    for comp in components:
        sub = G.subgraph(comp).copy()
        clusters = detect_device_clusters(sub)
        tiers.update(calculate_integrated_tier(sub, clusters, stencil_tiers))
    return components, tiers
