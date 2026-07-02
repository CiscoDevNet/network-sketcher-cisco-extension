# Copyright 2026 Cisco Systems, Inc. and its affiliates
# SPDX-License-Identifier: Apache-2.0

"""Map a Cisco Meraki device (productType + model) -> NS Stencil Type + Model + OS.

Returns a ``StencilMapping`` per device that is serialised into
``rename attribute_bulk`` rows and into ``meraki_inventory.csv`` for audit. The
dataclass and the NS stencil-type constants mirror
``aci_converter/src/aci_stencil_mapper.py`` so the shared ``ns_command_builder``
(sibling-copied into this package) consumes them unchanged.

Meraki product families and their NS stencil:
  * MX (appliance)  -> Firewall   (security appliance / SD-WAN gateway)
  * MS (switch)     -> L3Switch for L3-capable families (MS2xx/3xx/4xx),
                       else Switch
  * MR (wireless)   -> AP
  * MV (camera)     -> Server     (NS has no Camera stencil; modelled as a host)
  * MG/MT/MD/...    -> PC          (other endpoints / sensors)
  * client          -> PC

Confidence values:
- 1.00 : exact productType match (appliance / switch / wireless / camera)
- 0.60 : productType unknown but model prefix recognised
- 0.40 : pure default — flagged for human review
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


# Meraki switch families that are L3-capable (run dynamic routing / SVIs).
_L3_SWITCH_PREFIXES = ("MS2", "MS3", "MS4", "MS5", "C9300")


@dataclass
class StencilMapping:
    label: str
    node_definition: str        # the source productType / construct token
    image_definition: str       # serial / extra context (audit only)
    stencil_type: str
    model: str
    os: str
    confidence: float
    reason: str
    tags: List[str]


def _switch_stencil(model: str) -> str:
    up = (model or "").upper().replace("-", "").replace(" ", "")
    for pfx in _L3_SWITCH_PREFIXES:
        if up.startswith(pfx.replace("-", "")):
            return NS_L3SWITCH
    return NS_SWITCH


# productType -> (stencil resolver, model-prefix label, os-prefix).
# A resolver is either a fixed NS_* constant or a callable(model) -> NS_*.
_PRODUCT_TABLE = {
    "appliance": (NS_FIREWALL, "Cisco Meraki", "MX"),
    "switch":    (_switch_stencil, "Cisco Meraki", "MS"),
    "wireless":  (NS_AP, "Cisco Meraki", "MR"),
    "camera":    (NS_SERVER, "Cisco Meraki", "MV"),
    "sensor":    (NS_PC, "Cisco Meraki", "MT"),
    "cellularGateway": (NS_ROUTER, "Cisco Meraki", "MG"),
}


def map_meraki_device(
    name: str,
    product_type: str,
    model: str = "",
    serial: str = "",
    os_version: str = "",
) -> StencilMapping:
    """Map a Meraki device to a stencil.

    ``os_version`` is the running software string from the Dashboard device
    ``details`` (e.g. ``"MX 18.107.13"``); when absent it falls back to the
    product-family token.
    """
    entry = _PRODUCT_TABLE.get(product_type)
    if entry is not None:
        resolver, vendor, fam = entry
        stencil = resolver(model) if callable(resolver) else resolver
        model_disp = f"{vendor} {model}".strip() if model else f"{vendor} {fam}"
        if product_type == "camera":
            model_disp = f"{model_disp} (Camera)"
        return StencilMapping(
            label=name,
            node_definition=product_type,
            image_definition=serial,
            stencil_type=stencil,
            model=model_disp,
            os=os_version or fam,
            confidence=1.0,
            reason=f"productType='{product_type}'",
            tags=[product_type, model] if model else [product_type],
        )

    # productType unknown: try to recognise by the model prefix.
    up = (model or "").upper()
    guess: Optional[Tuple[str, str]] = None
    if up.startswith("MX"):
        guess = (NS_FIREWALL, "MX")
    elif up.startswith("MS") or up.startswith("C93"):
        guess = (_switch_stencil(model), "MS")
    elif up.startswith("MR") or up.startswith("CW"):
        guess = (NS_AP, "MR")
    elif up.startswith("MV"):
        guess = (NS_SERVER, "MV")
    if guess is not None:
        stencil, fam = guess
        return StencilMapping(
            label=name, node_definition=product_type or "?", image_definition=serial,
            stencil_type=stencil, model=f"Cisco Meraki {model}".strip(),
            os=os_version or fam, confidence=0.60,
            reason=f"model-prefix guess from '{model}'", tags=[fam],
        )

    return StencilMapping(
        label=name, node_definition=product_type or "?", image_definition=serial,
        stencil_type=NS_PC, model=f"Cisco Meraki {model}".strip() or "Meraki device",
        os=os_version or "", confidence=0.40,
        reason=f"unknown productType '{product_type}' / model '{model}' — REVIEW",
        tags=[product_type or "unknown"],
    )


def map_internet_cloud(name: str = "Internet") -> StencilMapping:
    """A synthetic Internet / WAN cloud waypoint (MX uplink target)."""
    return StencilMapping(
        label=name, node_definition="internet", image_definition="",
        stencil_type=NS_CLOUD, model="Internet / WAN", os="",
        confidence=0.60, reason="synthetic WAN waypoint for MX uplink",
        tags=["internet", "waypoint"],
    )


def map_client(name: str, description: str = "") -> StencilMapping:
    """A network client endpoint (from GET .../clients)."""
    return StencilMapping(
        label=name, node_definition="client", image_definition="",
        stencil_type=NS_PC, model=description or "Network client", os="",
        confidence=0.60, reason="Meraki network client", tags=["client"],
    )


def to_csv_rows(mappings: List[StencilMapping]) -> List[List[str]]:
    rows = [["name", "productType/kind", "serial/context", "stencil_type",
             "model", "os", "confidence", "reason", "tags"]]
    for m in mappings:
        rows.append([
            m.label, m.node_definition, m.image_definition, m.stencil_type,
            m.model, m.os, f"{m.confidence:.2f}", m.reason, ",".join(m.tags)
        ])
    return rows
