# Copyright 2026 Cisco Systems, Inc. and its affiliates
# SPDX-License-Identifier: Apache-2.0

"""Map a NetBox device (role + device-type model + platform) → NS Stencil Type
+ Model + OS string.

Returns a ``StencilMapping`` per device that the shared ``ns_command_builder``
serialises into ``rename attribute_bulk`` rows (and ``netbox_inventory.csv`` for
audit). The dataclass and the NS stencil-type constants mirror
``nd_converter/src/nd_stencil_mapper.py`` so the shared builder consumes them
unchanged.

NetBox is deliberately vendor-neutral and its **device roles are user-defined**
(any slug is possible), so mapping is by keyword heuristic on the role
slug/name (with a device-type/platform hint), NOT a fixed enum. Confidence:
- 0.90 : matched a specific role keyword (router / firewall / core-switch / ...)
- 0.50 : matched only a broad 'switch' / endpoint keyword
- 0.40 : no keyword matched — defaulted, flagged for review
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional, Tuple


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


@dataclass
class StencilMapping:
    label: str
    node_definition: str        # the source role slug / construct token
    image_definition: str       # device-type model / extra context (audit only)
    stencil_type: str
    model: str
    os: str
    confidence: float
    reason: str
    tags: List[str]


# Endpoint stencils (RULE 11.5: endpoints get their IP on the L1 port, no SVI).
_ENDPOINT_STENCILS = {NS_SERVER, NS_PC, NS_PHONE}

# Ordered keyword rules over the (normalised) role slug/name. First hit wins, so
# more specific keywords must come before broader ones (e.g. 'core-switch'
# before 'switch'). (keywords, stencil, confidence).
_ROLE_RULES: List[Tuple[Tuple[str, ...], str, float]] = [
    (("firewall", "fw", "ngfw", "asa", "ftd", "palo", "fortigate"), NS_FIREWALL, 0.90),
    (("wlc", "wireless-controller", "wlan-controller"), NS_WLC, 0.90),
    (("access-point", "accesspoint", "wifi", "wap"), NS_AP, 0.90),
    (("router", "rtr", "wan", "edge-router", "gateway-router"), NS_ROUTER, 0.90),
    # L3-capable switches
    (("core-switch", "core", "distribution", "dist-switch", "aggregation",
      "agg", "l3-switch", "l3switch", "spine", "leaf", "multilayer"), NS_L3SWITCH, 0.90),
    # L2 / generic switches
    (("access-switch", "tor-switch", "tor", "switch", "sw"), NS_SWITCH, 0.85),
    # Endpoints
    (("application-server", "app-server", "server", "host", "vm",
      "compute", "storage", "nas", "hypervisor", "esxi"), NS_SERVER, 0.90),
    (("workstation", "desktop", "laptop", "client", "pc"), NS_PC, 0.90),
    (("phone", "voip", "handset", "ip-phone"), NS_PHONE, 0.90),
]


def _norm(text: str) -> str:
    return (text or "").strip().lower()


def _os_from_platform(platform_slug: str, platform_name: str) -> str:
    """Human OS string from a NetBox platform (slug or display name)."""
    name = platform_name or platform_slug or ""
    s = _norm(platform_slug or platform_name)
    if not s:
        return ""
    if "ios-xe" in s or "iosxe" in s:
        return "Cisco IOS-XE"
    if "ios-xr" in s or "iosxr" in s:
        return "Cisco IOS-XR"
    if "nx-os" in s or "nxos" in s or "nexus" in s:
        return "Cisco NX-OS"
    if "cisco-ios" in s or s == "ios":
        return "Cisco IOS"
    if "asa" in s:
        return "Cisco ASA"
    return name  # ubuntu / junos / arista-eos / etc. — pass through as-is


def map_device(
    name: str,
    role_slug: str,
    role_name: str = "",
    model: str = "",
    platform_slug: str = "",
    platform_name: str = "",
    manufacturer: str = "",
) -> StencilMapping:
    """Map a NetBox device to a stencil by role keyword + device-type/platform hints."""
    hay = f"{_norm(role_slug)} {_norm(role_name)}"
    stencil: Optional[str] = None
    confidence = 0.40
    matched = ""
    for keywords, st, conf in _ROLE_RULES:
        for kw in keywords:
            if kw in hay:
                stencil, confidence, matched = st, conf, kw
                break
        if stencil:
            break

    if stencil is None:
        # No role keyword matched — default to a plain Switch (most NetBox gear
        # is a switch) but flag it for human review.
        stencil = NS_SWITCH
        reason = f"no role keyword matched (role='{role_slug or role_name}') — REVIEW"
    else:
        reason = f"role '{role_slug or role_name}' matched '{matched}'"

    os_str = _os_from_platform(platform_slug, platform_name)
    model_disp = model or (role_name or role_slug or "device")
    if manufacturer and manufacturer.lower() not in model_disp.lower():
        model_disp = f"{manufacturer} {model_disp}"

    return StencilMapping(
        label=name,
        node_definition=role_slug or role_name or "",
        image_definition=model or "",
        stencil_type=stencil,
        model=model_disp,
        os=os_str,
        confidence=confidence,
        reason=reason,
        tags=[t for t in (role_slug, matched) if t],
    )


def is_endpoint_stencil(stencil_type: str) -> bool:
    return stencil_type in _ENDPOINT_STENCILS


def map_waypoint(name: str, kind: str = "external") -> StencilMapping:
    """Map a non-device far-end (circuit / provider network / WAN) to a cloud."""
    return StencilMapping(
        label=name,
        node_definition=kind,
        image_definition="",
        stencil_type=NS_CLOUD,
        model="External / WAN (NetBox circuit)",
        os="",
        confidence=0.60,
        reason=f"non-device endpoint '{kind}'",
        tags=[kind],
    )


def map_stub(name: str) -> StencilMapping:
    """Map a synthetic host-stub peer (④) — the far end invented for an uncabled
    port that carries a VLAN / IP, so the port exists in NS."""
    return StencilMapping(
        label=name,
        node_definition="stub",
        image_definition="",
        stencil_type=NS_PC,
        model="Dummy stub (synthetic peer for an uncabled VLAN/IP port)",
        os="",
        confidence=0.30,
        reason="synthesized so an uncabled VLAN/IP port can exist in NS",
        tags=["stub"],
    )


def to_csv_rows(mappings: List[StencilMapping]) -> List[List[str]]:
    rows = [["name", "role/kind", "model", "stencil_type",
             "model_disp", "os", "confidence", "reason", "tags"]]
    for m in mappings:
        rows.append([
            m.label, m.node_definition, m.image_definition, m.stencil_type,
            m.model, m.os, f"{m.confidence:.2f}", m.reason, ",".join(m.tags)
        ])
    return rows
