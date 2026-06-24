# Copyright 2026 Cisco Systems, Inc. and its affiliates
# SPDX-License-Identifier: Apache-2.0

"""Turn ACI contracts into a Network Sketcher ``[Flow_List]`` sheet.

ACI is zero-trust: an EPG only talks to another EPG when one *provides* and the
other *consumes* a contract (``vzBrCP``). A contract's subjects (``vzSubj``)
reference filters (``vzFilter``) whose entries (``vzEntry``) carry the L4
protocol/port. That maps cleanly onto NS's ``[Flow_List]`` sheet — exactly how
``cv_converter`` emits ``gen_flow_list.csv`` — with one row per
(provider EPG → consumer EPG, protocol, service-port).

Bandwidth is left blank: a config export has no flow-rate data (unlike Cyber
Vision, which derives Mbps from byte counts).
"""
from __future__ import annotations

import csv
import os
import re
from collections import defaultdict
from typing import Any, Callable, Dict, List, Optional, Tuple

_TENANT_RE = re.compile(r"/tn-([^/]+)")

# Common named ports used by ACI default filters → numeric string.
_NAMED_PORTS = {
    "https": "443", "http": "80", "ftpData": "20", "ftp-data": "20",
    "smtp": "25", "dns": "53", "pop3": "110", "rtsp": "554",
    "ssh": "22", "telnet": "23", "ldap": "389", "snmp": "161",
}

_PROTO_MAP = {
    "tcp": "TCP", "udp": "UDP", "icmp": "ICMP", "icmpv6": "ICMP",
}


def _tenant_of(dn: str) -> str:
    m = _TENANT_RE.search(dn or "")
    return m.group(1) if m else ""


def _port_str(raw: str) -> str:
    raw = (raw or "").strip()
    if not raw or raw.lower() in ("unspecified", "0"):
        return ""
    return _NAMED_PORTS.get(raw, raw)


def _entry_proto_port(entry) -> Tuple[str, str]:
    """Return (proto, port_label) for a ``vzEntry`` MO.

    proto is one of TCP/UDP/ICMP/'' (empty for IP/any). port_label is a
    human service-port string, e.g. '443', '80-88', or '' (any).
    """
    prot = (entry.get("prot") or "").lower()
    proto = _PROTO_MAP.get(prot, "")
    if not proto:
        ether = (entry.get("etherType") or "").lower()
        if ether in ("arp", ""):
            proto = ""  # non-IP / unspecified
    frm = _port_str(entry.get("dFromPort"))
    to = _port_str(entry.get("dToPort"))
    if frm and to and frm != to:
        port = f"{frm}-{to}"
    else:
        port = frm or to
    return proto, port


def _contract_filters(idx, contract) -> List[Tuple[str, str, str]]:
    """Resolve a contract to a list of (proto, port_label, filter_name).

    Walks vzSubj → (vzRsSubjFiltAtt | vzInTerm/vzOutTerm → vzRsFiltAtt) →
    vzFilter → vzEntry. Filters are looked up by name within the contract's
    tenant first, then the 'common' tenant, then anywhere.
    """
    tenant = _tenant_of(contract.dn)
    results: List[Tuple[str, str, str]] = []
    seen: set = set()

    filt_refs: List[str] = []
    for subj in idx.children_of(contract, "vzSubj"):
        for ratt in idx.children_of(subj, "vzRsSubjFiltAtt"):
            if ratt.get("tnVzFilterName"):
                filt_refs.append(ratt.get("tnVzFilterName"))
        for term_cls in ("vzInTerm", "vzOutTerm"):
            for term in idx.children_of(subj, term_cls):
                for ratt in idx.children_of(term, "vzRsFiltAtt"):
                    if ratt.get("tnVzFilterName"):
                        filt_refs.append(ratt.get("tnVzFilterName"))

    for fname in filt_refs:
        filt = _find_filter(idx, fname, tenant)
        if filt is None:
            # Filter not in export (e.g. implicit default/permit-all): record a
            # single any/any flow so the relationship is still visible.
            key = ("", "", fname)
            if key not in seen:
                seen.add(key)
                results.append(key)
            continue
        entries = idx.children_of(filt, "vzEntry")
        if not entries:
            key = ("", "", fname)
            if key not in seen:
                seen.add(key)
                results.append(key)
            continue
        for entry in entries:
            proto, port = _entry_proto_port(entry)
            label = entry.get("name") or fname
            key = (proto, port, label)
            if key not in seen:
                seen.add(key)
                results.append(key)
    return results


def _find_filter(idx, name: str, tenant: str):
    candidates = [f for f in idx.of("vzFilter") if f.get("name") == name]
    if not candidates:
        return None
    for f in candidates:
        if _tenant_of(f.dn) == tenant:
            return f
    for f in candidates:
        if _tenant_of(f.dn) == "common":
            return f
    return candidates[0]


def build_flow_rows(
    idx,
    epg_name_by_dn: Dict[str, str],
    cfg: Optional[Dict[str, Any]] = None,
) -> List[List[str]]:
    """Build ``[Flow_List]`` data rows: [src, dst, proto, service_label].

    ``epg_name_by_dn`` maps an EPG DN to its NS device name (shared with the
    logical mapper so source/destination names match the topology).
    """
    cfg = cfg or {}
    bidir = bool(cfg.get("emit_bidirectional_flows", True))

    # contract name -> provider/consumer EPG DNs (matched within tenant scope).
    providers: Dict[Tuple[str, str], set] = defaultdict(set)
    consumers: Dict[Tuple[str, str], set] = defaultdict(set)
    for rs in idx.of("fvRsProv"):
        cname = rs.get("tnVzBrCPName")
        if cname and rs.parent_dn:
            providers[(_tenant_of(rs.parent_dn), cname)].add(rs.parent_dn)
    for rs in idx.of("fvRsCons"):
        cname = rs.get("tnVzBrCPName")
        if cname and rs.parent_dn:
            consumers[(_tenant_of(rs.parent_dn), cname)].add(rs.parent_dn)

    rows: List[List[str]] = []
    seen_rows: set = set()

    for contract in idx.of("vzBrCP"):
        cname = contract.get("name")
        ctenant = _tenant_of(contract.dn)
        if not cname:
            continue
        # Match providers/consumers in the same tenant, plus any that referenced
        # the contract by name (handles exported/global contracts loosely).
        prov_dns: set = set()
        cons_dns: set = set()
        for (t, n), dns in providers.items():
            if n == cname and (t == ctenant or ctenant == "common"):
                prov_dns |= dns
        for (t, n), dns in consumers.items():
            if n == cname and (t == ctenant or ctenant == "common"):
                cons_dns |= dns
        if not prov_dns or not cons_dns:
            continue

        flows = _contract_filters(idx, contract)
        if not flows:
            flows = [("", "", cname)]

        for p_dn in sorted(prov_dns):
            for c_dn in sorted(cons_dns):
                if p_dn == c_dn:
                    continue
                src = epg_name_by_dn.get(p_dn)
                dst = epg_name_by_dn.get(c_dn)
                if not src or not dst:
                    continue
                for proto, port, label in flows:
                    service = f"{label}({port})" if port else label
                    # Consumer initiates toward the provider's service, so the
                    # traffic flow is consumer → provider.
                    pair = (dst, src, proto, service)
                    if pair not in seen_rows:
                        seen_rows.add(pair)
                        rows.append([dst, src, proto, service])
                    if bidir:
                        rpair = (src, dst, proto, service)
                        if rpair not in seen_rows:
                            seen_rows.add(rpair)
                            rows.append([src, dst, proto, service])
    return rows


def write_flow_list_csv(path: str, rows: List[List[str]]) -> None:
    """Write the ``[Flow_List]`` sheet CSV (same header as cv_converter)."""
    with open(path, "w", encoding="utf-8", newline="") as fh:
        fh.write("[Flow_List]\n")
        w = csv.writer(fh)
        w.writerow(["No", "Source Device Name", "Destination Device Name",
                    "TCP/UDP/ICMP", "Service name(Port)", "Max. bandwidth(Mbps)",
                    "Manually routing path settings", "Automatic routing path settings"])
        for i, (src, dst, proto, service) in enumerate(rows, start=1):
            w.writerow([i, src, dst, proto, service, "", "", ""])
