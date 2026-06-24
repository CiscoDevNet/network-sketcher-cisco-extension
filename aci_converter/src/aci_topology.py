# Copyright 2026 Cisco Systems, Inc. and its affiliates
# SPDX-License-Identifier: Apache-2.0

"""Shared helpers over operational / topology MOs (fetched by default; see
``fetch_from_apic.py --no-topology`` to skip them).

Used by the underlay (topSystem enrichment + vPC pairs), overlay (real
endpoints), and unified mappers (endpoint / EPG placement on real leaf ports).
All helpers degrade gracefully to empty results when the operational classes
were not fetched, so the config-only path still works.
"""
from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Dict, List, Tuple

_NODE_RE = re.compile(r"/node-(\d+)")
_PATHS_RE = re.compile(r"/paths-(\d+)/")
_PROTPATHS_RE = re.compile(r"/protpaths-(\d+)-(\d+)/")
_PATHEP_RE = re.compile(r"pathep-\[([^\]]+)\]")
_EXPGEP_RE = re.compile(r"expgep-([^/]+)")


def parse_path(tdn: str) -> Tuple[List[str], str, bool]:
    """Parse a path target DN into (node_ids, access_port_or_group, is_vpc).

    ``topology/pod-1/paths-101/pathep-[eth1/5]``        -> (['101'], 'eth1/5', False)
    ``topology/pod-1/protpaths-101-102/pathep-[GRP]``   -> (['101','102'], 'GRP', True)
    """
    tdn = tdn or ""
    node_ids: List[str] = []
    is_vpc = False
    m = _PROTPATHS_RE.search(tdn)
    if m:
        node_ids = [m.group(1), m.group(2)]
        is_vpc = True
    else:
        m = _PATHS_RE.search(tdn)
        if m:
            node_ids = [m.group(1)]
    mp = _PATHEP_RE.search(tdn)
    access = mp.group(1) if mp else ""
    return node_ids, access, is_vpc


def topsystem_by_node(idx) -> Dict[str, object]:
    """{node_id: topSystem Mo} for per-node version / mgmt IP / TEP / pod."""
    out: Dict[str, object] = {}
    for mo in idx.of("topSystem"):
        m = _NODE_RE.search(mo.dn or "")
        if m:
            out[m.group(1)] = mo
    return out


def vpc_pairs(idx) -> Dict[str, Tuple[str, List[str]]]:
    """{node_id: (vpc_group_name, [peer_node_ids])} from explicit protection groups."""
    groups: Dict[str, List[str]] = defaultdict(list)
    for mo in idx.of("fabricNodePEp"):
        m = _EXPGEP_RE.search(mo.dn or "")
        nid = mo.get("id")
        if m and nid:
            groups[m.group(1)].append(nid)
    out: Dict[str, Tuple[str, List[str]]] = {}
    for grp, ids in groups.items():
        for nid in ids:
            out[nid] = (grp, [x for x in ids if x != nid])
    return out


@dataclass
class Endpoint:
    mac: str
    ips: List[str] = field(default_factory=list)
    encap: str = ""
    node_ids: List[str] = field(default_factory=list)
    access: str = ""        # physical port or vPC policy-group name
    is_vpc: bool = False

    def best_ip(self) -> str:
        """Prefer an IPv4 address for display; fall back to IPv6 / ''."""
        v4 = [x for x in self.ips if ":" not in x]
        if v4:
            return v4[0]
        return self.ips[0] if self.ips else ""


def endpoints_by_epg(idx) -> Dict[str, List[Endpoint]]:
    """{epg_dn: [Endpoint, ...]} from fvCEp + fvIp + fvRsCEpToPathEp.

    These are operational; joined here by DN prefix because a class query
    returns them flat (not nested under the EPG)."""
    ips_by_cep: Dict[str, List[str]] = defaultdict(list)
    for mo in idx.of("fvIp"):
        dn = mo.dn or ""
        i = dn.find("/ip-[")
        if i > 0 and mo.get("addr"):
            ips_by_cep[dn[:i]].append(mo.get("addr"))

    path_by_cep: Dict[str, str] = {}
    for mo in idx.of("fvRsCEpToPathEp"):
        dn = mo.dn or ""
        i = dn.find("/rscEpToPathEp-")
        if i > 0:
            path_by_cep[dn[:i]] = mo.get("tDn")

    out: Dict[str, List[Endpoint]] = defaultdict(list)
    for mo in idx.of("fvCEp"):
        dn = mo.dn or ""
        i = dn.find("/cep-")
        if i < 0:
            continue
        epg_dn = dn[:i]
        ips = ips_by_cep.get(dn, [])
        node_ids, access, is_vpc = parse_path(path_by_cep.get(dn, ""))
        out[epg_dn].append(Endpoint(
            mac=mo.get("mac"), ips=ips, encap=mo.get("encap"),
            node_ids=node_ids, access=access, is_vpc=is_vpc,
        ))
    return out
