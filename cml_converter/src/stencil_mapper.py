# Copyright 2026 Cisco Systems, Inc. and its affiliates
# SPDX-License-Identifier: Apache-2.0

"""Map CML node_definition + tags + label → NS Stencil Type + Model + OS string.

Returns a `StencilMapping` per device that we can later serialise into
`rename attribute_bulk` rows and into `stencil_mapping.csv` for audit.

Confidence values:
- 1.00 : exact lookup hit in NODE_DEF_TABLE (NS rule RULE 16 explicit case)
- 0.85 : keyword/tag-based heuristic match
- 0.60 : fuzzy match on label substrings (`*-leaf*`, `*-spine*`, ...)
- 0.40 : pure default (Router) — flagged for human review
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Iterable, List, Optional


# Allowed NS Stencil Type values (see RULE 16 of the AI context).
NS_ROUTER = "Router"
NS_L3SWITCH = "L3Switch"
NS_SWITCH = "Switch"
NS_FIREWALL = "Firewall"
NS_WLC = "WLC"
NS_AP = "AP"
NS_SERVER = "Server"
NS_CLOUD = "Cloud"
NS_PHONE = "Phone"
NS_PC = "PC"


# Maps CML node_definition values to a (Stencil, Model display, OS display) tuple.
NODE_DEF_TABLE: Dict[str, tuple] = {
    # ---- Routers (Cisco IOS-XE / IOS / IOS-XR / SD-WAN) -------------------
    "iosv": (NS_ROUTER, "IOSv (Cisco IOS Router)", "IOS"),
    "iosvl2": (NS_SWITCH, "IOSv L2 (Cisco IOS Switch)", "IOS"),
    "iosxrv": (NS_ROUTER, "IOS XRv (Cisco IOS XR Router)", "IOS-XR"),
    "iosxrv9000": (NS_ROUTER, "IOS XRv 9000 (Cisco IOS XR Router)", "IOS-XR"),
    "csr1000v": (NS_ROUTER, "CSR1000v (Cisco IOS-XE Router)", "IOS-XE"),
    "cat8000v": (NS_ROUTER, "Catalyst 8000V (SD-WAN Edge / IOS-XE Router)", "IOS-XE"),
    "cat-sdwan-edge": (NS_ROUTER, "Catalyst SD-WAN Edge", "SD-WAN"),
    "iol": (NS_ROUTER, "IOL (Cisco IOS-on-Linux)", "IOS"),
    "iol-xe": (NS_ROUTER, "IOL-XE (Cisco IOS-XE on Linux)", "IOS-XE"),
    "ioll2-xe": (NS_SWITCH, "IOL L2-XE (Cisco IOS-XE Switch on Linux)", "IOS-XE"),
    "xrd": (NS_ROUTER, "XRd (Cisco IOS XR Container)", "IOS-XR"),
    "asav": (NS_FIREWALL, "ASAv (Cisco Adaptive Security Appliance Virtual)", "ASA"),
    "ftdv": (NS_FIREWALL, "FTDv (Cisco Firepower Threat Defense Virtual)", "FTD"),
    "meraki-vmx": (NS_FIREWALL, "Meraki vMX (Cisco Meraki Virtual MX)", "Meraki"),
    "frr": (NS_ROUTER, "Free Range Routing (FRR)", "FRR/Linux"),
    # ---- L3 / data centre switches --------------------------------------
    "nxosv9000": (NS_L3SWITCH, "Nexus 9000v (Cisco NX-OS)", "NX-OS"),
    "nxosv": (NS_L3SWITCH, "Nexus 9000v (Cisco NX-OS)", "NX-OS"),
    "cat9000v-l2": (NS_SWITCH, "Catalyst 9000v L2 (Cisco IOS-XE Switch)", "IOS-XE"),
    "cat9000v-l3": (NS_L3SWITCH, "Catalyst 9000v L3 (Cisco IOS-XE Switch)", "IOS-XE"),
    # ---- Wireless / endpoints / specialty -------------------------------
    "cat9800": (NS_WLC, "Catalyst 9800 WLC", "IOS-XE"),
    "wireless-ap": (NS_AP, "Wireless Access Point", "IOS-XE"),
    "wireless-client": (NS_PC, "Wireless Client", "Client"),
    "unmanaged_switch": (NS_SWITCH, "Unmanaged Switch", "Linux"),
    "external_connector": (NS_CLOUD, "External Connector (Bridge to host)", ""),
    "alpine": (NS_SERVER, "Alpine Linux Host", "Linux"),
    "ubuntu": (NS_SERVER, "Ubuntu Linux Host", "Linux"),
    "centos": (NS_SERVER, "CentOS Linux Host", "Linux"),
    "tiny-linux": (NS_SERVER, "Tiny Linux Host", "Linux"),
    "server": (NS_SERVER, "Generic Server", "Linux"),
    "nginx": (NS_SERVER, "NGINX Web Server", "Linux"),
    "dnsmasq": (NS_SERVER, "dnsmasq (DNS/DHCP Server)", "Linux"),
    "desktop": (NS_PC, "Desktop PC", "Linux"),
    "win-desktop": (NS_PC, "Windows Desktop", "Windows"),
    "win-server": (NS_SERVER, "Windows Server", "Windows"),
}


# Keyword-based fallback rules; each item:
#   (keyword_substring, NS_STENCIL, model_hint, os_hint, confidence)
LABEL_KEYWORD_RULES = [
    ("spine", NS_L3SWITCH, "Spine Switch", "NX-OS", 0.85),
    ("leaf", NS_L3SWITCH, "Leaf Switch", "NX-OS", 0.85),
    ("bgw", NS_L3SWITCH, "Border Gateway Switch", "NX-OS", 0.85),
    ("border", NS_L3SWITCH, "Border Switch / Router", "NX-OS", 0.75),
    ("agg", NS_L3SWITCH, "Aggregation Switch", "IOS-XE", 0.75),
    ("dist", NS_L3SWITCH, "Distribution Switch", "IOS-XE", 0.75),
    ("core", NS_L3SWITCH, "Core Switch", "IOS-XE", 0.80),
    ("access", NS_SWITCH, "Access Switch", "IOS-XE", 0.80),
    ("acc", NS_SWITCH, "Access Switch", "IOS-XE", 0.65),
    ("isn", NS_ROUTER, "Inter-Site Network Router", "NX-OS / IOS-XE", 0.70),
    ("wan", NS_ROUTER, "WAN Edge Router", "IOS-XE", 0.70),
    ("edge", NS_ROUTER, "Edge Router", "IOS-XE", 0.70),
    ("mpls", NS_ROUTER, "MPLS PE Router", "IOS-XR", 0.65),
    ("fw", NS_FIREWALL, "Firewall", "ASA / FTD", 0.65),
    ("asa", NS_FIREWALL, "Cisco ASA", "ASA", 0.85),
    ("ftd", NS_FIREWALL, "Cisco FTD", "FTD", 0.85),
    ("wlc", NS_WLC, "Wireless LAN Controller", "IOS-XE", 0.85),
    ("ap-", NS_AP, "Wireless Access Point", "IOS-XE", 0.65),
    ("printer", NS_SERVER, "Network Printer", "Embedded", 0.60),
    ("server", NS_SERVER, "Server", "Linux", 0.70),
    ("vm", NS_SERVER, "Virtual Machine", "Linux", 0.55),
]


@dataclass
class StencilMapping:
    label: str
    node_definition: str
    image_definition: str
    stencil_type: str
    model: str
    os: str
    confidence: float
    reason: str
    tags: List[str]


def map_one(
    label: str,
    node_definition: str,
    image_definition: str = "",
    tags: Optional[Iterable[str]] = None,
) -> StencilMapping:
    tags = list(tags or [])
    nd = (node_definition or "").lower()

    # Direct lookup wins.
    if nd in NODE_DEF_TABLE:
        stencil, model, os_str = NODE_DEF_TABLE[nd]
        return StencilMapping(
            label=label,
            node_definition=node_definition,
            image_definition=image_definition,
            stencil_type=stencil,
            model=model,
            os=os_str,
            confidence=1.0,
            reason=f"direct table hit on node_definition='{node_definition}'",
            tags=tags,
        )

    # Heuristics on tags / label.
    lab_lower = (label or "").lower()
    tag_lower = " ".join((t or "").lower() for t in tags)

    for kw, stencil, model, os_str, conf in LABEL_KEYWORD_RULES:
        if kw in lab_lower or kw in tag_lower:
            return StencilMapping(
                label=label,
                node_definition=node_definition,
                image_definition=image_definition,
                stencil_type=stencil,
                model=model + f" (inferred from '{kw}')",
                os=os_str,
                confidence=conf,
                reason=f"keyword match '{kw}' in {'label' if kw in lab_lower else 'tag'}",
                tags=tags,
            )

    # Last-resort defaults.
    if "host" in nd or "linux" in nd or "windows" in nd:
        return StencilMapping(label, node_definition, image_definition,
                              NS_SERVER, f"Host ({node_definition})", "Linux", 0.45,
                              "host-like node_definition", tags)
    if "switch" in nd:
        return StencilMapping(label, node_definition, image_definition,
                              NS_SWITCH, f"Switch ({node_definition})", "", 0.45,
                              "fallback: contains 'switch'", tags)
    if "router" in nd or "ios" in nd or "xr" in nd:
        return StencilMapping(label, node_definition, image_definition,
                              NS_ROUTER, f"Router ({node_definition})", "", 0.50,
                              "fallback: ios/xr/router substring", tags)

    return StencilMapping(label, node_definition, image_definition,
                          NS_ROUTER, f"Unknown ({node_definition})", "", 0.30,
                          "no mapping rule matched — REVIEW", tags)


def map_all(nodes: List[Dict]) -> List[StencilMapping]:
    out: List[StencilMapping] = []
    for n in nodes:
        out.append(
            map_one(
                label=str(n.get("label", "")),
                node_definition=str(n.get("node_definition", "")),
                image_definition=str(n.get("image_definition", "") or ""),
                tags=n.get("tags") or [],
            )
        )
    return out


def to_csv_rows(mappings: List[StencilMapping]) -> List[List[str]]:
    rows = [["label", "node_definition", "image_definition", "stencil_type",
             "model", "os", "confidence", "reason", "tags"]]
    for m in mappings:
        rows.append([
            m.label, m.node_definition, m.image_definition, m.stencil_type,
            m.model, m.os, f"{m.confidence:.2f}", m.reason, ",".join(m.tags)
        ])
    return rows
