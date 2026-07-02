# Copyright 2026 Cisco Systems, Inc. and its affiliates
# SPDX-License-Identifier: Apache-2.0

"""Map a Cisco Catalyst Center (DNA Center) device ``role`` / ``family`` (or a
logical SD-Access VN / anycast-gateway / external role) → NS Stencil Type +
Model + OS string.

Returns a ``StencilMapping`` per device that is serialised into
``rename attribute_bulk`` rows and into ``catc_inventory.csv`` for audit. The
dataclass and the NS stencil-type constants mirror
``aci_converter/src/aci_stencil_mapper.py`` / ``nd_converter`` so the shared
``ns_command_builder`` consumes them unchanged.

Confidence values:
- 1.00 : exact role/family match from Catalyst Center inventory
- 0.85 : role inferred from device name keyword
- 0.60 : logical-construct synthesis (VN / anycast-gateway / external)
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


# Catalyst Center ``role`` (normalised to lower-case, spaces/hyphens/underscores
# removed) → (Stencil, Model display, OS display). Campus fabric tiers map to
# Catalyst 9000 switches; the L3 hand-off / aggregation tiers are L3 switches.
ROLE_TABLE = {
    "core": (NS_L3SWITCH, "Catalyst 9000 (IOS-XE)", "IOS-XE"),
    "distribution": (NS_L3SWITCH, "Catalyst 9000 (IOS-XE)", "IOS-XE"),
    "border": (NS_L3SWITCH, "Catalyst 9000 (IOS-XE)", "IOS-XE"),
    "borderrouter": (NS_L3SWITCH, "Catalyst 9000 (IOS-XE)", "IOS-XE"),
    "access": (NS_SWITCH, "Catalyst 9000 Access (IOS-XE)", "IOS-XE"),
    "edge": (NS_SWITCH, "Catalyst 9000 Access (IOS-XE)", "IOS-XE"),
    "router": (NS_ROUTER, "IOS-XE Router", "IOS-XE"),
    "wlc": (NS_WLC, "Catalyst 9800 WLC", "IOS-XE"),
    "ap": (NS_AP, "Catalyst Access Point", "IOS-XE"),
}

# Catalyst Center ``family`` (normalised) → role-table key. Wireless families do
# not carry a campus ``role`` tier, so they are keyed on family instead.
FAMILY_TO_ROLE = {
    "wirelesscontroller": "wlc",
    "unifiedap": "ap",
}

# Roles drawn on the L3 hand-off / border tier of the underlay (one tier above
# the core/distribution fabric).
BORDER_ROLES = {"border", "borderrouter"}
ROUTER_ROLES = {"router"}
ACCESS_ROLES = {"access", "edge"}


def normalise_role(role: str) -> str:
    """Canonicalise a Catalyst Center role/family string
    ('BORDER ROUTER' -> 'borderrouter', 'Wireless Controller' -> 'wirelesscontroller')."""
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


def map_device(
    name: str,
    role: str,
    family: str = "",
    serial: str = "",
    model_hint: str = "",
    os_type: str = "",
    os_version: str = "",
    inferred: bool = False,
) -> StencilMapping:
    """Map a Catalyst Center managed device (role/family) to a stencil.

    ``model_hint`` (e.g. ``platformId`` ``C9300-48U``) is appended to the model
    description. ``os_type`` + ``os_version`` (e.g. ``IOS-XE`` + ``17.18.2``)
    are folded into the OS string when available.
    """
    role_key = normalise_role(role)
    fam_key = normalise_role(family)

    # Wireless gear is keyed on family (a WLC/AP often carries a generic campus
    # role like ACCESS, so the family wins for wireless families).
    family_keyed = fam_key in FAMILY_TO_ROLE
    if family_keyed:
        role_key = FAMILY_TO_ROLE[fam_key]

    os_str = (os_type or "IOS-XE").strip() or "IOS-XE"
    if os_version:
        os_str = f"{os_str} {os_version}".strip()

    if role_key in ROLE_TABLE:
        stencil, model, default_os = ROLE_TABLE[role_key]
        if model_hint:
            model = f"{model} [{model_hint}]"
        return StencilMapping(
            label=name,
            node_definition=role_key,
            image_definition=serial,
            stencil_type=stencil,
            model=model,
            os=os_str or default_os,
            confidence=0.85 if inferred else 1.0,
            reason=(f"role inferred from name -> '{role_key}'" if inferred
                    else f"role='{role}'" + (f" family='{family}'" if family_keyed else "")),
            tags=[role_key],
        )
    return StencilMapping(
        label=name,
        node_definition=role or family or "",
        image_definition=serial,
        stencil_type=NS_L3SWITCH,
        model=f"Catalyst device ({role or family or 'unspecified'})"
              + (f" [{model_hint}]" if model_hint else ""),
        os=os_str,
        confidence=0.40,
        reason=f"unknown role '{role}' / family '{family}' — REVIEW",
        tags=[role or family or "unknown"],
    )


def map_logical(
    name: str,
    kind: str,
    model: str = "",
    os_str: str = "",
) -> StencilMapping:
    """Map a synthesised logical-overlay construct to a stencil.

    ``kind`` is one of: ``network`` (an L2 anycast segment), ``gateway`` (a
    per-VN distributed anycast gateway), ``external`` (a border / L3 hand-off
    cloud), ``endpoint`` (a discovered client/host).
    """
    table = {
        "network":  (NS_PC, model or "SD-Access anycast segment", os_str or ""),
        "gateway":  (NS_L3SWITCH, model or "SD-Access VN anycast gateway", os_str or "IOS-XE"),
        "external": (NS_CLOUD, model or "Fabric border / L3 hand-off", os_str or ""),
        "endpoint": (NS_PC, model or "Endpoint host", os_str or ""),
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
