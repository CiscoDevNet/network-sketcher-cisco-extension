# Copyright 2026 Cisco Systems, Inc. and its affiliates
# SPDX-License-Identifier: Apache-2.0

"""Turn SD-Access reachability into a Network Sketcher ``[Flow_List]`` sheet.

Like NX-OS VXLAN EVPN (and unlike ACI's zero-trust EPG-to-EPG contracts),
Cisco SD-Access provides **open any-to-any reachability WITHIN a Virtual
Network (VN)** by default — segmentation between groups is enforced by Scalable
Group Tags (SGTs) and a group-based policy matrix, not by per-pair L3 contracts.
So this builder does NOT invent flows from topology.

To still produce a useful traffic matrix it can optionally emit SGT-based group
policy as flow rows (``emit_sgt_flows``). That source is not implemented yet
(Catalyst Center group-based-access-control policy is a separate API surface),
so the builder defaults to emitting **no** rows. The output CSV format matches
``aci_converter`` / ``nd_converter`` so it pastes straight into the master's
``[Flow_List]`` sheet.

Bandwidth is left blank: a topology export carries no flow-rate data.
"""
from __future__ import annotations

import csv
from typing import Any, Dict, List, Optional, Tuple


def build_flow_rows(
    idx,
    vn_name_by_key: Dict[Tuple[str, str], str],
    cfg: Optional[Dict[str, Any]] = None,
) -> List[List[str]]:
    """Build ``[Flow_List]`` data rows: [src, dst, proto, service_label].

    ``vn_name_by_key`` maps a ``(area, virtualNetworkName)`` key to its NS
    device name (shared with the logical mapper so source/destination names
    match the topology). Returns [] unless ``emit_sgt_flows`` is enabled.

    SD-Access is open-within-VN; per-group segmentation is SGT/policy based.
    SGT flow emission is left unimplemented for now, so this returns an empty
    list (no flows) by default.
    """
    cfg = cfg or {}
    if not bool(cfg.get("emit_sgt_flows", False)):
        return []
    # SGT / group-based-access-control policy emission is not implemented yet.
    # Catalyst Center exposes the policy matrix on a separate API surface that
    # this converter does not fetch; until that is wired in, emit no rows.
    return []


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
