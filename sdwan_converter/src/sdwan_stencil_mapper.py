# Copyright 2026 Cisco Systems, Inc. and its affiliates
# SPDX-License-Identifier: Apache-2.0

"""Map a Cisco Catalyst SD-WAN (vManage) device ``personality`` / ``device-model``
(or a synthesised underlay/overlay construct) -> NS Stencil Type + Model + OS
string.

Returns a ``StencilMapping`` per device that is serialised into
``rename attribute_bulk`` rows and into ``sdwan_inventory.csv`` for audit. The
dataclass and the NS stencil-type constants mirror
``aci_converter/src/aci_stencil_mapper.py`` / ``catc_converter`` so the shared
``ns_command_builder`` consumes them unchanged.

Confidence values:
- 1.00 : exact ``personality`` match from vManage's device inventory
  (``vmanage`` / ``vsmart`` / ``vbond`` / ``vedge``)
- 0.85 : personality inferred from the ``device-model`` string (a fetch/export
  that omitted the ``personality`` field)
- 0.60 : synthesised underlay/overlay construct (transport segment / Service
  VPN waypoint / per-Edge LAN dummy switch / control-plane L3+L2 placeholders)
- 0.40 : pure default -- flagged for human review
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


# vManage device ``personality`` -> (Stencil, Model display, OS display).
# The three control-plane roles are cloud/VM-hosted management appliances (not
# data-plane forwarding routers), so -- mirroring aci_converter's APIC ->
# Server and catc_converter's WLC/controller precedent -- they map to Server.
# Only the data-plane SD-WAN Edge (cEdge/vEdge) maps to Router.
ROLE_TABLE = {
    "vmanage": (NS_SERVER, "Cisco SD-WAN Manager (vManage)", "vManage"),
    "vsmart": (NS_SERVER, "Cisco SD-WAN Controller (vSmart)", "vSmart"),
    "vbond": (NS_SERVER, "Cisco SD-WAN Validator (vBond)", "vBond"),
    "vedge": (NS_ROUTER, "Cisco SD-WAN Edge", "SD-WAN"),
}


def normalise_personality(personality: str, device_model: str = "") -> str:
    """Canonicalise a vManage ``personality`` string, falling back to a
    ``device-model`` keyword match when ``personality`` is absent (a hand-built
    export or an older API response may omit it).

    vManage's own device-model naming is not fully standardised across
    releases/platforms (``vedge-cloud``, ``vedge-C8000V``, ``c8000v``,
    ``vedge-1000``, ...), so the ``device-model`` fallback is a best-effort
    keyword match, NOT authoritative -- prefer a real ``personality`` field
    whenever the source data has one.
    """
    p = (personality or "").strip().lower()
    if p in ROLE_TABLE:
        return p
    model = (device_model or "").strip().lower()
    if "vmanage" in model:
        return "vmanage"
    if "vsmart" in model:
        return "vsmart"
    if model == "vedge-cloud" or "vbond" in model:
        return "vbond"
    if model:
        return "vedge"
    return ""


@dataclass
class StencilMapping:
    label: str
    node_definition: str        # the source personality / construct token
    image_definition: str       # uuid / extra context (audit only)
    stencil_type: str
    model: str
    os: str
    confidence: float
    reason: str
    tags: List[str]


def map_device(
    name: str,
    personality: str,
    device_model: str = "",
    uuid: str = "",
    inferred: bool = False,
) -> StencilMapping:
    """Map a vManage-managed device (``personality`` / ``device-model``) to a
    stencil.

    ``device_model`` (e.g. ``vedge-C8000V``, ``vmanage``) is appended to the
    model description when it adds information beyond the role table's own
    display string.
    """
    role_key = personality
    if role_key in ROLE_TABLE:
        stencil, model, os_str = ROLE_TABLE[role_key]
        if device_model and device_model.lower() not in model.lower():
            model = f"{model} [{device_model}]"
        return StencilMapping(
            label=name,
            node_definition=role_key,
            image_definition=uuid,
            stencil_type=stencil,
            model=model,
            os=os_str,
            confidence=0.85 if inferred else 1.0,
            reason=(f"personality inferred from device-model '{device_model}' -> '{role_key}'"
                    if inferred else f"personality='{role_key}'"),
            tags=[role_key],
        )
    return StencilMapping(
        label=name,
        node_definition=personality or device_model or "",
        image_definition=uuid,
        stencil_type=NS_ROUTER,
        model=f"SD-WAN device ({personality or device_model or 'unspecified'})",
        os="SD-WAN",
        confidence=0.40,
        reason=f"unknown personality '{personality}' / device-model '{device_model}' -- REVIEW",
        tags=[personality or device_model or "unknown"],
    )


def map_logical(
    name: str,
    kind: str,
    model: str = "",
    os_str: str = "",
) -> StencilMapping:
    """Map a synthesised underlay/overlay construct to a stencil.

    ``kind`` is one of: ``transport-segment`` (underlay -- an INFERRED shared
    VPN0 transport subnet), ``service-vpn-waypoint`` (overlay -- the shared
    cloud every Edge's Service VPN attaches to), ``lan-dummy`` (both modes --
    the synthetic per-Edge LAN aggregation switch every one of that Edge's
    Service VPN PHYSICAL interfaces plugs into), or the two underlay
    control-plane placeholders: ``control-l3-dummy`` (the unknown routed hop
    between the transport networks and the controllers' own segment, hence a
    Router) and ``control-l2-dummy`` (the segment the controllers share,
    hence a Switch).
    """
    table = {
        "transport-segment": (NS_CLOUD, model or "Inferred VPN0 transport segment", os_str or ""),
        "service-vpn-waypoint": (NS_CLOUD, model or "SD-WAN Service VPN waypoint", os_str or ""),
        "lan-dummy": (NS_SWITCH, model or "Synthetic Service VPN LAN switch", os_str or ""),
        "control-l3-dummy": (NS_ROUTER,
                             model or "Synthetic control-plane routed hop", os_str or ""),
        "control-l2-dummy": (NS_SWITCH,
                             model or "Synthetic control-plane LAN segment", os_str or ""),
    }
    stencil, mdl, os_v = table.get(kind, (NS_CLOUD, model or kind, os_str))
    return StencilMapping(
        label=name,
        node_definition=kind,
        image_definition="",
        stencil_type=stencil,
        model=mdl,
        os=os_v,
        confidence=0.60,
        reason=f"synthesised construct '{kind}'",
        tags=[kind],
    )


def to_csv_rows(mappings: List[StencilMapping]) -> List[List[str]]:
    rows = [["name", "personality/kind", "uuid/context", "stencil_type",
             "model", "os", "confidence", "reason", "tags"]]
    for m in mappings:
        rows.append([
            m.label, m.node_definition, m.image_definition, m.stencil_type,
            m.model, m.os, f"{m.confidence:.2f}", m.reason, ",".join(m.tags)
        ])
    return rows
