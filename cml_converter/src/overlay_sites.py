# Copyright 2026 Cisco Systems, Inc. and its affiliates
# SPDX-License-Identifier: Apache-2.0
"""Decide the overlay area of every CML node.

An SD-WAN Edge belongs to ``<lab>-site<site-id>``. Every other node joins the
site of the Edge it is nearest to over CML L1 links on the LAN side: the
search never leaves an Edge through one of its WAN transport ports (the
``tunnel-interface`` ports), so the WAN underlay does not pull sites
together. A node at the same distance from two sites goes to the smaller
site-id. A node that no Edge reaches, and every node of a lab without SD-WAN,
uses the lab area ``<lab>``.
"""

from __future__ import annotations

from collections import defaultdict, deque
from typing import Dict, List, Optional, Set, Tuple

from .overlay_parser import OverlayFacts
from .topology_mapper import NSL1Link


def wan_area(lab: str) -> str:
    return f"{lab}-wan_wp_"


def site_area(lab: str, site_id: Optional[int], edge: str) -> str:
    return f"{lab}-site{site_id}" if site_id is not None else f"{lab}-{edge}"


def assign_overlay_areas(
    labels: List[str],
    lab_of: Dict[str, str],
    l1_links: List[NSL1Link],
    facts: Dict[str, OverlayFacts],
) -> Dict[str, str]:
    """``{label: area}`` for every label in ``labels``."""
    transport: Set[Tuple[str, str]] = set()
    for label, f in facts.items():
        if f.sdwan is not None:
            transport.update((label, p) for p in f.sdwan.transport_ports)

    adjacency: Dict[str, Set[str]] = defaultdict(set)
    for lk in l1_links:
        if (lk.a_device, lk.a_port) in transport or (lk.b_device, lk.b_port) in transport:
            continue
        if lk.a_device == lk.b_device:
            continue
        adjacency[lk.a_device].add(lk.b_device)
        adjacency[lk.b_device].add(lk.a_device)

    edges = sorted(
        (f.sdwan.site_id if f.sdwan.site_id is not None else 1 << 30, label)
        for label, f in facts.items() if f.sdwan is not None and label in lab_of
    )
    area: Dict[str, str] = {}
    dist: Dict[str, Tuple[int, int]] = {}
    queue: deque = deque()
    for site_id, label in edges:
        sid = None if site_id == 1 << 30 else site_id
        area[label] = site_area(lab_of[label], sid, label)
        dist[label] = (0, site_id)
        queue.append(label)
    # Breadth-first from every Edge at once; on a tie the smaller site-id wins
    # because the Edges were queued in site-id order.
    while queue:
        node = queue.popleft()
        d, sid = dist[node]
        for peer in sorted(adjacency[node]):
            if peer in dist or lab_of.get(peer) != lab_of.get(node):
                continue
            dist[peer] = (d + 1, sid)
            area[peer] = area[node]
            queue.append(peer)

    for label in labels:
        area.setdefault(label, lab_of.get(label, "lab"))
    return area
