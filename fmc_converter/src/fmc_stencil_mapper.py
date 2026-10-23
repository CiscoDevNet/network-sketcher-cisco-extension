# Copyright 2026 Cisco Systems, Inc. and its affiliates
# SPDX-License-Identifier: Apache-2.0

"""Map an FMC device to a Network Sketcher stencil."""
from __future__ import annotations

from dataclasses import dataclass
from typing import List


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
    node_definition: str
    image_definition: str
    stencil_type: str
    model: str
    os: str
    confidence: float
    reason: str
    tags: List[str]


def map_firewall(name: str, model: str, version: str) -> StencilMapping:
    return StencilMapping(
        label=name, node_definition="ftd", image_definition=version,
        stencil_type=NS_FIREWALL, model=model or "Secure Firewall Threat Defense",
        os=version or "FTD", confidence=1.0,
        reason="FMC device record", tags=[],
    )


def map_fmc(name: str, version: str) -> StencilMapping:
    return StencilMapping(
        label=name, node_definition="fmc", image_definition=version,
        stencil_type=NS_SERVER, model="Secure Firewall Management Center",
        os=version or "FMC", confidence=1.0,
        reason="FMC server version", tags=[],
    )


def map_switch(name: str, reason: str) -> StencilMapping:
    return StencilMapping(
        label=name, node_definition="dummy-switch", image_definition="",
        stencil_type=NS_SWITCH, model="Synthesised switch",
        os="", confidence=0.6, reason=reason, tags=["synthesised"],
    )


def map_waypoint(name: str) -> StencilMapping:
    return StencilMapping(
        label=name, node_definition="dummy-wan", image_definition="",
        stencil_type=NS_CLOUD, model="Synthesised WAN waypoint",
        os="", confidence=0.6, reason="management path between FMC and firewalls",
        tags=["synthesised"],
    )


def to_csv_rows(devices: dict) -> List[List[str]]:
    rows = [["device", "area", "stencil", "model", "os", "confidence", "reason"]]
    for name, device in sorted(devices.items()):
        stencil = device.stencil
        rows.append([
            name, device.area, stencil.stencil_type, stencil.model, stencil.os,
            f"{stencil.confidence:.2f}", stencil.reason,
        ])
    return rows
