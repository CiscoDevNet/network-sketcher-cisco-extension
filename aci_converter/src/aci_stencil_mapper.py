# Copyright 2026 Cisco Systems, Inc. and its affiliates
# SPDX-License-Identifier: Apache-2.0

"""Map an ACI fabric-node role (or a logical EPG/BD/L3Out role) → NS Stencil
Type + Model + OS string.

Returns a ``StencilMapping`` per device that is serialised into
``rename attribute_bulk`` rows and into ``aci_inventory.csv`` for audit. The
dataclass and the NS stencil-type constants mirror
``cml_converter/src/stencil_mapper.py`` so the shared ``ns_command_builder``
consumes them unchanged.

Confidence values:
- 1.00 : exact role match from ``fabricNodeIdentP.role`` (spine/leaf/controller)
- 0.85 : keyword match on the node name (e.g. ``*-bl*`` border leaf)
- 0.60 : logical-construct synthesis (EPG / BD-gateway / L3Out)
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


# ACI fabricNode role → (Stencil, Model display, OS display).
ROLE_TABLE = {
    "spine": (NS_L3SWITCH, "ACI Spine (Nexus 9000)", "ACI"),
    "leaf": (NS_L3SWITCH, "ACI Leaf (Nexus 9000)", "ACI"),
    "border-leaf": (NS_L3SWITCH, "ACI Border Leaf (Nexus 9000)", "ACI"),
    "controller": (NS_SERVER, "Cisco APIC (Application Policy Infrastructure Controller)", "APIC"),
}


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
    is_border_leaf: bool = False,
    controller_stencil: str = NS_SERVER,
    model_hint: str = "",
) -> StencilMapping:
    """Map a fabric node (``fabricNode`` / ``fabricNodeIdentP``) to a stencil.

    ``model_hint`` (e.g. the operational ``fabricNode.model`` like
    ``N9K-C9336C-FX2``) is appended to the model description when available.
    """
    role_key = "border-leaf" if (role == "leaf" and is_border_leaf) else role
    if role_key in ROLE_TABLE:
        stencil, model, os_str = ROLE_TABLE[role_key]
        if model_hint:
            model = f"{model} [{model_hint}]"
        if role_key == "controller" and controller_stencil != NS_SERVER:
            stencil = controller_stencil
        return StencilMapping(
            label=name,
            node_definition=role_key,
            image_definition=serial,
            stencil_type=stencil,
            model=model,
            os=os_str,
            confidence=1.0,
            reason=f"role='{role}'" + (" (border leaf)" if is_border_leaf else ""),
            tags=[role_key],
        )
    return StencilMapping(
        label=name,
        node_definition=role or "",
        image_definition=serial,
        stencil_type=NS_L3SWITCH,
        model=f"ACI Fabric Node ({role or 'unspecified'})",
        os="ACI",
        confidence=0.40,
        reason=f"unknown fabricNode role '{role}' — REVIEW",
        tags=[role or "unknown"],
    )


def map_logical(
    name: str,
    kind: str,
    model: str = "",
    os_str: str = "",
) -> StencilMapping:
    """Map a synthesised logical-overlay construct to a stencil.

    ``kind`` is one of: ``epg`` (endpoint group), ``gateway`` (per-VRF BD
    gateway), ``l3out`` (external routed connection).
    """
    table = {
        "epg":     (NS_PC, model or "Endpoint Group (EPG)", os_str or ""),
        "gateway": (NS_L3SWITCH, model or "ACI Distributed Anycast Gateway", os_str or "ACI"),
        "l3out":   (NS_CLOUD, model or "L3Out (External Routed Network)", os_str or ""),
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
