# Copyright 2026 Cisco Systems, Inc. and its affiliates
# SPDX-License-Identifier: Apache-2.0

"""Build the *physical fabric* NSModel from an APIC export.

Node source (in priority order):
  * ``fabricNode`` (operational, fetched via ``--with-topology``) — authoritative
    ``role`` (leaf | spine | controller), ``name``, ``model``, ``podId``; crucially
    it INCLUDES the APIC controllers, which ``fabricNodeIdentP`` does not.
  * ``fabricNodeIdentP`` (config) — fabric membership for switches only; its
    ``role`` is often ``unspecified`` (then inferred from name / nodeId).

L1 links:
  * ``lldpAdjEp`` (operational, ``--with-topology``) — OBSERVED spine/leaf/APIC
    cabling, used when present.
  * otherwise INFERRED from ACI's full-mesh CLOS (every leaf ↔ every spine; no
    leaf-leaf / spine-spine; APICs attach to leafs). The chosen source is
    recorded in the report.

``l3extRsNodeL3OutAtt`` flags border leafs (config). Layout (tier): spine row
above leafs, APICs below. Switches are coloured green and APICs red; only
inferred links are synthetic (links carry no colour).
"""
from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Tuple

from . import aci_stencil_mapper as sm
from . import aci_topology as topo
from .aci_stencil_mapper import StencilMapping
from .ns_model import NSDevice, NSL1Link, NSModel, build_area_layout, normalise_port_name

# Tier rows for the physical fabric.
_ROW_SPINE = 1
_ROW_LEAF = 2
_ROW_APIC = 3

_NODE_DN_RE = re.compile(r"/node-(\d+)")


def _infer_role(role: str, name: str, node_id: str) -> str:
    """Resolve a fabric node's role.

    ``fabricNodeIdentP.role`` is authoritative when set, but some releases /
    simulators leave it ``unspecified``. Fall back to the node name (which
    almost always contains 'spine' / 'leaf' / 'apic') and finally to the ACI
    nodeId convention (1xx = leaf, 2xx/3xx = spine, <100 = controller).
    """
    role = (role or "").lower()
    if role in ("spine", "leaf", "controller"):
        return role
    nm = (name or "").lower()
    if "spine" in nm:
        return "spine"
    if "leaf" in nm:
        return "leaf"
    if "apic" in nm or "controller" in nm:
        return "controller"
    try:
        nid = int(node_id)
    except (TypeError, ValueError):
        return "leaf"
    if nid < 100:
        return "controller"
    if 200 <= nid < 400:
        return "spine"
    return "leaf"


def _node_name(name: str, role: str, node_id: str, naming: str) -> str:
    """Device display name per the ``spine_leaf_naming`` policy."""
    nm = (name or "").strip()
    role_token = {"controller": "apic"}.get(role, role) or "node"
    fallback = f"{role_token}-{node_id}" if node_id else (nm or role_token)
    if naming == "nodeid":
        return fallback
    if naming == "name_nodeid" and nm and node_id:
        return f"{nm}-{node_id}"
    return nm or fallback


def _detect_border_leaf_ids(idx) -> set:
    """Node IDs referenced by any L3Out logical node profile (border leafs)."""
    ids: set = set()
    for mo in idx.of("l3extRsNodeL3OutAtt"):
        tdn = mo.get("tDn")
        m = _NODE_DN_RE.search(tdn)
        if m:
            ids.add(m.group(1))
    return ids


def build_physical_model(
    idx,
    cfg: Optional[Dict[str, Any]] = None,
    layout: str = "tier",
) -> Tuple[NSModel, Dict[str, Any]]:
    """Return (NSModel, info) for the physical fabric.

    ``info`` carries the stencil mappings, object counts, and inference caveats
    for the converter's report.
    """
    cfg = cfg or {}
    naming = str(cfg.get("spine_leaf_naming", "name") or "name")
    controller_stencil = str(cfg.get("treat_controller_as", sm.NS_SERVER) or sm.NS_SERVER)
    apic_uplinks = max(1, int(cfg.get("apic_uplinks_per_leaf", 2) or 2))
    manual_border = {str(x) for x in (cfg.get("border_leaf_node_ids") or [])}

    border_ids = _detect_border_leaf_ids(idx) | manual_border
    ts_map = topo.topsystem_by_node(idx)   # A: per-node version / mgmt / TEP / pod
    vpc_map = topo.vpc_pairs(idx)          # C: vPC leaf pairs

    model = NSModel()
    mappings: List[StencilMapping] = []

    spines: List[str] = []
    leafs: List[str] = []
    apics: List[str] = []
    border_leaf_names: List[str] = []
    seen_names: set = set()
    nodeid_to_name: Dict[str, str] = {}     # '101' -> device name (for lldp local end)
    origname_to_name: Dict[str, str] = {}   # raw fabric name -> device name (for lldp remote end)

    def _unique(name: str) -> str:
        base = name
        i = 2
        while name in seen_names:
            name = f"{base}_{i}"
            i += 1
        seen_names.add(name)
        return name

    # Prefer operational fabricNode (authoritative role + APIC controllers +
    # model); fall back to config fabricNodeIdentP (switches only).
    use_oper = bool(idx.of("fabricNode"))
    node_source = "fabricNode" if use_oper else "fabricNodeIdentP"
    raw_nodes: List[Dict[str, Any]] = []
    if use_oper:
        for mo in idx.of("fabricNode"):
            raw_nodes.append({
                "node_id": mo.get("id"),
                "role": mo.get("role"),
                "name": mo.get("name"),
                "serial": mo.get("serial"),
                "model": mo.get("model"),
            })
    else:
        for mo in idx.of("fabricNodeIdentP"):
            raw_nodes.append({
                "node_id": mo.get("nodeId") or mo.get("id"),
                "role": mo.get("role"),
                "name": mo.get("name"),
                "serial": mo.get("serial"),
                "model": "",
            })

    for n in raw_nodes:
        node_id = n["node_id"]
        raw_name = n["name"]
        role = _infer_role(n["role"], raw_name, node_id)
        is_border = role == "leaf" and node_id in border_ids

        name = _unique(_node_name(raw_name, role, node_id, naming))
        st = sm.map_fabric_node(
            name=name, role=role, serial=n["serial"],
            is_border_leaf=is_border, controller_stencil=controller_stencil,
            model_hint=n.get("model") or "",
        )
        mappings.append(st)
        if node_id:
            nodeid_to_name[str(node_id)] = name
        if raw_name:
            origname_to_name[str(raw_name)] = name

        # A: enrich from topSystem (real version + mgmt/TEP/pod).
        attr_bits: List[str] = []
        ts = ts_map.get(str(node_id))
        if ts is not None:
            ver = ts.get("version")
            if ver:
                st.os = f"ACI {ver}"
            mgmt, tep, pod = ts.get("oobMgmtAddr"), ts.get("address"), ts.get("podId")
            if mgmt and mgmt != "0.0.0.0":
                attr_bits.append(f"mgmt {mgmt}")
            if tep:
                attr_bits.append(f"TEP {tep}")
            if pod:
                attr_bits.append(f"pod {pod}")
        # C: vPC pair annotation.
        vp = vpc_map.get(str(node_id))
        if vp:
            grp, peers = vp
            attr_bits.append(f"vPC {grp} with " + ",".join(f"node-{p}" for p in peers))

        if role == "spine":
            row, bucket = _ROW_SPINE, spines
        elif role == "controller":
            row, bucket = _ROW_APIC, apics
        else:  # leaf (incl. border leaf) / unspecified switch
            row, bucket = _ROW_LEAF, leafs
            if is_border:
                border_leaf_names.append(name)
        bucket.append(name)

        is_endpoint = st.stencil_type in {sm.NS_SERVER, sm.NS_PC}
        model.devices[name] = NSDevice(
            name=name, area="fabric", row=row, stencil=st, is_endpoint=is_endpoint,
            routing_attribute=" | ".join(attr_bits),
        )

    # Links: observed LLDP adjacencies when available, else inferred CLOS.
    lldp_links = _links_from_lldp(idx, nodeid_to_name, origname_to_name)
    if lldp_links:
        model.l1_links = lldp_links
        link_source = "lldpAdjEp"
    else:
        model.l1_links = _build_clos_links(spines, leafs, apics, apic_uplinks)
        link_source = "inferred-CLOS"

    model.areas, model.area_to_devices = build_area_layout(
        model.devices, model.l1_links, layout=layout,
    )

    caveats: List[str] = []
    if link_source == "lldpAdjEp":
        caveats.append(
            f"L1 links are OBSERVED from LLDP adjacencies (lldpAdjEp): "
            f"{len(model.l1_links)} link(s)."
        )
    else:
        caveats.append(
            "Spine↔leaf and APIC↔leaf cabling is INFERRED from ACI's full-mesh CLOS "
            "architecture (no lldpAdjEp available); port numbers are synthetic but "
            "deterministic. Re-fetch with --with-topology for observed cabling."
        )
    if node_source == "fabricNodeIdentP":
        caveats.append(
            "Nodes come from config fabricNodeIdentP (switches only): APIC controllers "
            "are absent and roles may be inferred from name/nodeId. Re-fetch with "
            "--with-topology to use operational fabricNode (authoritative roles + APICs)."
        )
    if not spines or not leafs:
        caveats.append(
            f"Incomplete fabric: {len(spines)} spine(s), {len(leafs)} leaf(s)."
        )

    info = {
        "mappings": mappings,
        "counts": {
            "spine": len(spines), "leaf": len(leafs),
            "border_leaf": len(border_leaf_names), "controller": len(apics),
            "l1_links": len(model.l1_links),
        },
        "node_source": node_source,
        "link_source": link_source,
        "border_leaf_names": border_leaf_names,
        "nodeid_to_name": nodeid_to_name,
        "caveats": caveats,
    }
    return model, info


# Local interface portion of an lldpAdjEp DN, e.g. ".../if-[eth1/49]/adj-1".
_LLDP_LOCAL_PORT_RE = re.compile(r"if-\[([^\]]+)\]")
# A bracketed pathep port, e.g. "topology/pod-1/paths-201/pathep-[eth5/1]".
_LLDP_BRACKET_RE = re.compile(r"\[([^\]]+)\]")
# A bare interface token anywhere in the value, e.g. "apic1-eth2" -> "eth2".
_LLDP_ETH_RE = re.compile(r"(eth[\d/]+)", re.IGNORECASE)


def _clean_lldp_port(raw: str) -> str:
    """Extract a clean NS port from an LLDP remote-port value.

    LLDP reports the neighbour port in several shapes: a full pathep DN
    (``topology/pod-1/paths-201/pathep-[eth5/1]``), a vendor string
    (``apic1-eth2``), or already a plain port. Pull out the ``ethX/Y`` token so
    the result normalises to a valid NS port (and so both ends of a link agree,
    enabling de-duplication)."""
    raw = (raw or "").strip()
    if not raw:
        return ""
    m = _LLDP_BRACKET_RE.search(raw)
    if m:
        return normalise_port_name(m.group(1))
    m = _LLDP_ETH_RE.search(raw)
    if m:
        return normalise_port_name(m.group(1))
    return normalise_port_name(raw)


def _links_from_lldp(
    idx,
    nodeid_to_name: Dict[str, str],
    origname_to_name: Dict[str, str],
) -> List[NSL1Link]:
    """Build L1 links from observed ``lldpAdjEp`` adjacencies.

    Each adjacency gives the local node (from its DN) + local port, and the
    remote neighbour (``sysName`` + ``portDesc``/``portIdV``). Only adjacencies
    whose remote ``sysName`` resolves to a known fabric node are kept (so host /
    FEX / out-of-fabric neighbours are skipped). Links are de-duplicated across
    the two ends that each report them.
    """
    out: List[NSL1Link] = []
    seen: set = set()
    for mo in idx.of("lldpAdjEp"):
        dn = mo.dn or ""
        m_node = _NODE_DN_RE.search(dn)
        m_port = _LLDP_LOCAL_PORT_RE.search(dn)
        if not m_node or not m_port:
            continue
        local_name = nodeid_to_name.get(m_node.group(1))
        if not local_name:
            continue
        remote_sys = (mo.get("sysName") or "").strip()
        remote_name = origname_to_name.get(remote_sys)
        if not remote_name or remote_name == local_name:
            continue  # neighbour is not a known fabric node (host/FEX/etc.)
        local_port = normalise_port_name(m_port.group(1))
        remote_port = _clean_lldp_port(mo.get("portDesc") or mo.get("portIdV") or "")
        if not remote_port:
            remote_port = "Ethernet 1/1"
        key = frozenset({(local_name, local_port), (remote_name, remote_port)})
        if key in seen:
            continue
        seen.add(key)
        out.append(NSL1Link(local_name, local_port, remote_name, remote_port))
    return out


def _build_clos_links(
    spines: List[str],
    leafs: List[str],
    apics: List[str],
    apic_uplinks: int,
) -> List[NSL1Link]:
    """Synthesize the inferred full-mesh CLOS + APIC uplinks.

    Port numbering convention (synthetic, deterministic, NS-valid):
      * leaf fabric uplinks to spines: Ethernet 1/49, 1/50, ... (one per spine)
      * spine downlinks to leafs:      Ethernet 1/1, 1/2, ...   (one per leaf)
      * APIC NICs:                     Ethernet 1/1, 1/2, ...
      * leaf APIC-facing downlinks:    Ethernet 1/1, 1/2, ...   (host range)
    """
    links: List[NSL1Link] = []

    # Full mesh: every leaf ↔ every spine.
    for li, leaf in enumerate(leafs):
        for si, spine in enumerate(spines):
            leaf_port = normalise_port_name(f"Ethernet 1/{49 + si}")
            spine_port = normalise_port_name(f"Ethernet 1/{li + 1}")
            links.append(NSL1Link(leaf, leaf_port, spine, spine_port))

    if not leafs:
        return links

    # APICs dual-home to consecutive leafs; track each leaf's host-port counter.
    leaf_host_port: Dict[str, int] = {leaf: 0 for leaf in leafs}
    for ai, apic in enumerate(apics):
        for k in range(apic_uplinks):
            leaf = leafs[(ai * apic_uplinks + k) % len(leafs)]
            leaf_host_port[leaf] += 1
            apic_port = normalise_port_name(f"Ethernet 1/{k + 1}")
            leaf_port = normalise_port_name(f"Ethernet 1/{leaf_host_port[leaf]}")
            links.append(NSL1Link(apic, apic_port, leaf, leaf_port))

    return links
