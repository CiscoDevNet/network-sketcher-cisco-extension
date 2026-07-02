# Copyright 2026 Cisco Systems, Inc. and its affiliates
# SPDX-License-Identifier: Apache-2.0

"""Map an NDFC fabric-node role (or a logical VRF/Network/external role) → NS
Stencil Type + Model + OS string.

Returns a ``StencilMapping`` per device that is serialised into
``rename attribute_bulk`` rows and into ``nd_inventory.csv`` for audit. The
dataclass and the NS stencil-type constants mirror
``aci_converter/src/aci_stencil_mapper.py`` so the shared ``ns_command_builder``
consumes them unchanged.

Confidence values:
- 1.00 : exact role match from ``switchRoleEnum`` (leaf/spine/border/bgw/...)
- 0.85 : role inferred from node name keyword
- 0.60 : logical-construct synthesis (Network / VRF-gateway / external)
- 0.40 : pure default — flagged for human review
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import List


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


# NDFC ``switchRoleEnum`` (normalised to lower-case, spaces removed) →
# (Stencil, Model display, OS display). NX-OS fabric nodes are all L3 switches
# (Nexus 9000); routers are the L3 hand-off devices.
ROLE_TABLE = {
    "leaf": (NS_L3SWITCH, "NX-OS VXLAN EVPN Leaf (Nexus 9000)", "NX-OS"),
    "spine": (NS_L3SWITCH, "NX-OS VXLAN EVPN Spine (Nexus 9000)", "NX-OS"),
    "superspine": (NS_L3SWITCH, "NX-OS VXLAN EVPN Super-Spine (Nexus 9000)", "NX-OS"),
    "border": (NS_L3SWITCH, "NX-OS Border Leaf (Nexus 9000)", "NX-OS"),
    "borderspine": (NS_L3SWITCH, "NX-OS Border Spine (Nexus 9000)", "NX-OS"),
    "bordersuperspine": (NS_L3SWITCH, "NX-OS Border Super-Spine (Nexus 9000)", "NX-OS"),
    "bordergateway": (NS_L3SWITCH, "NX-OS Border Gateway / VXLAN Multi-Site (Nexus 9000)", "NX-OS"),
    "bordergatewayspine": (NS_L3SWITCH, "NX-OS Border Gateway Spine / VXLAN Multi-Site (Nexus 9000)", "NX-OS"),
    "bordergatewaysuperspine": (NS_L3SWITCH, "NX-OS Border Gateway Super-Spine (Nexus 9000)", "NX-OS"),
    "tor": (NS_SWITCH, "NX-OS ToR (Nexus 9000)", "NX-OS"),
    "access": (NS_SWITCH, "NX-OS Access Switch", "NX-OS"),
    "aggregation": (NS_L3SWITCH, "NX-OS Aggregation Switch", "NX-OS"),
    "edgerouter": (NS_ROUTER, "Edge Router", "IOS-XE/IOS-XR"),
    "corerouter": (NS_ROUTER, "Core Router", "IOS-XE/IOS-XR"),
}

# Roles that, although they live on a 'spine' tier physically, also act as a
# border / multi-site hand-off (drawn one tier higher in the underlay).
BORDER_ROLES = {
    "border", "borderspine", "bordersuperspine",
    "bordergateway", "bordergatewayspine", "bordergatewaysuperspine",
}
SPINE_ROLES = {"spine", "superspine", "borderspine", "bordersuperspine",
               "bordergatewayspine", "bordergatewaysuperspine"}
ROUTER_ROLES = {"edgerouter", "corerouter"}


def normalise_role(role: str) -> str:
    """Canonicalise an NDFC role string ('Border Gateway Spine' -> 'bordergatewayspine')."""
    return "".join((role or "").lower().split()).replace("-", "").replace("_", "")


@dataclass
class StencilMapping:
    label: str
    node_definition: str        # the source role / construct token
    image_definition: str       # serial / extra context (audit only)
    stencil_type: str
    model: str
    os: str
    confidence: float
    reason: str
    tags: List[str]


def map_fabric_node(
    name: str,
    role: str,
    serial: str = "",
    model_hint: str = "",
    os_hint: str = "",
    inferred: bool = False,
) -> StencilMapping:
    """Map a fabric node (NDFC ``switchRoleEnum``) to a stencil.

    ``model_hint`` (e.g. ``N9K-C9300v``) is appended to the model description and
    ``os_hint`` (e.g. ``10.6(1)``) is folded into the OS string when available.
    """
    role_key = normalise_role(role)
    if role_key in ROLE_TABLE:
        stencil, model, os_str = ROLE_TABLE[role_key]
        if model_hint:
            model = f"{model} [{model_hint}]"
        if os_hint:
            os_str = f"NX-OS {os_hint}" if "NX-OS" in os_str else f"{os_str} {os_hint}"
        return StencilMapping(
            label=name,
            node_definition=role_key,
            image_definition=serial,
            stencil_type=stencil,
            model=model,
            os=os_str,
            confidence=0.85 if inferred else 1.0,
            reason=(f"role inferred from name -> '{role_key}'" if inferred
                    else f"switchRoleEnum='{role}'"),
            tags=[role_key],
        )
    return StencilMapping(
        label=name,
        node_definition=role or "",
        image_definition=serial,
        stencil_type=NS_L3SWITCH,
        model=f"NX-OS Fabric Node ({role or 'unspecified'})"
              + (f" [{model_hint}]" if model_hint else ""),
        os=(f"NX-OS {os_hint}" if os_hint else "NX-OS"),
        confidence=0.40,
        reason=f"unknown switchRoleEnum '{role}' — REVIEW",
        tags=[role or "unknown"],
    )


def map_logical(
    name: str,
    kind: str,
    model: str = "",
    os_str: str = "",
) -> StencilMapping:
    """Map a synthesised logical-overlay construct to a stencil.

    ``kind`` is one of: ``network`` (an L2VNI network / EPG-equivalent),
    ``gateway`` (a per-VRF distributed anycast gateway), ``external`` (an
    external / L3 hand-off cloud), ``endpoint`` (a discovered host).
    """
    table = {
        "network":  (NS_PC, model or "VXLAN Network (L2VNI)", os_str or ""),
        "gateway":  (NS_L3SWITCH, model or "VXLAN Distributed Anycast Gateway (VRF)", os_str or "NX-OS"),
        "external": (NS_CLOUD, model or "External / L3 hand-off", os_str or ""),
        "endpoint": (NS_SERVER, model or "Endpoint host", os_str or ""),
    }
    stencil, mdl, os_v = table.get(kind, (NS_PC, model or kind, os_str))
    return StencilMapping(
        label=name,
        node_definition=kind,
        image_definition="",
        stencil_type=stencil,
        model=mdl,
        os=os_v,
        confidence=0.60,
        reason=f"logical construct '{kind}'",
        tags=[kind],
    )


def to_csv_rows(mappings: List[StencilMapping]) -> List[List[str]]:
    rows = [["name", "role/kind", "serial/context", "stencil_type",
             "model", "os", "confidence", "reason", "tags"]]
    for m in mappings:
        rows.append([
            m.label, m.node_definition, m.image_definition, m.stencil_type,
            m.model, m.os, f"{m.confidence:.2f}", m.reason, ",".join(m.tags)
        ])
    return rows
