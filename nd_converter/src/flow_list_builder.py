# Copyright 2026 Cisco Systems, Inc. and its affiliates
# SPDX-License-Identifier: Apache-2.0

"""Turn NDFC VRF/Network reachability into a Network Sketcher ``[Flow_List]`` sheet.

Unlike ACI (which is zero-trust: an EPG talks to another EPG only via an explicit
``vzBrCP`` contract), NX-OS VXLAN EVPN provides **open any-to-any L3 reachability
within a VRF** by default — there is no per-pair contract to enumerate. So this
builder does NOT invent flows from policy.

To still produce a useful traffic matrix, it can optionally emit the
*implied* intra-VRF reachability: one row per ordered pair of Networks that
share a VRF (``emit_intra_vrf_flows``, default off to avoid an O(n²) explosion
on large fabrics). The output CSV format matches ``aci_converter`` /
``cv_converter`` so it pastes straight into the master's ``[Flow_List]`` sheet.

Bandwidth is left blank: a config export carries no flow-rate data.
"""
from __future__ import annotations

import csv
from collections import defaultdict
from typing import Any, Dict, List, Optional, Tuple

from . import nd_topology as topo


def build_flow_rows(
    idx,
    net_name_by_key: Dict[Tuple[str, str], str],
    cfg: Optional[Dict[str, Any]] = None,
) -> List[List[str]]:
    """Build ``[Flow_List]`` data rows: [src, dst, proto, service_label].

    ``net_name_by_key`` maps ``(fabric, networkName)`` to its NS device name
    (shared with the logical mapper so source/destination names match the
    topology). Returns [] unless ``emit_intra_vrf_flows`` is enabled.
    """
    cfg = cfg or {}
    if not bool(cfg.get("emit_intra_vrf_flows", False)):
        return []
    fabric_include = {str(x) for x in (cfg.get("fabric_include") or [])}

    rows: List[List[str]] = []
    seen: set = set()

    fabrics = [f for f in idx.fabric_names() if not fabric_include or f in fabric_include]
    for fabric in fabrics:
        # Group network device names by VRF.
        by_vrf: Dict[str, List[str]] = defaultdict(list)
        for net in idx.networks.get(fabric, []):
            ninfo = topo.network_l2_info(net)
            vrf = ninfo["vrf"]
            dev = net_name_by_key.get((fabric, ninfo["name"]))
            if vrf and dev:
                by_vrf[vrf].append(dev)
        # Any-to-any within each VRF (both directions).
        for vrf, devs in by_vrf.items():
            for src in sorted(devs):
                for dst in sorted(devs):
                    if src == dst:
                        continue
                    pair = (src, dst, "", f"intra-VRF {vrf}(any)")
                    if pair in seen:
                        continue
                    seen.add(pair)
                    rows.append([src, dst, "", f"intra-VRF {vrf}(any)"])
    return rows


def write_flow_list_csv(path: str, rows: List[List[str]]) -> None:
    """Write the ``[Flow_List]`` sheet CSV (same header as aci_converter)."""
    with open(path, "w", encoding="utf-8", newline="") as fh:
        fh.write("[Flow_List]\n")
        w = csv.writer(fh)
        w.writerow(["No", "Source Device Name", "Destination Device Name",
                    "TCP/UDP/ICMP", "Service name(Port)", "Max. bandwidth(Mbps)",
                    "Manually routing path settings", "Automatic routing path settings"])
        for i, (src, dst, proto, service) in enumerate(rows, start=1):
            w.writerow([i, src, dst, proto, service, "", "", ""])
