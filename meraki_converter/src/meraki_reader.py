# Copyright 2026 Cisco Systems, Inc. and its affiliates
# SPDX-License-Identifier: Apache-2.0

"""meraki_reader.py — load + normalise a Meraki export JSON.

Consumes the document written by ``fetch_from_meraki.py`` (or an equivalent
hand-saved snapshot) and exposes light, typed accessors so the topology mapper
does not have to know the raw Dashboard-API field names. No network access.
"""
from __future__ import annotations

import json
import pathlib
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class MerakiDevice:
    serial: str
    name: str
    model: str
    product_type: str
    network_id: str
    mac: str = ""
    os_version: str = ""
    lan_ip: Optional[str] = None
    wan1_ip: Optional[str] = None
    wan2_ip: Optional[str] = None
    raw: Dict[str, Any] = field(default_factory=dict)

    @property
    def display_name(self) -> str:
        """A stable, human/NS-friendly device name (never blank).

        Sandbox devices often have an empty ``name``; fall back to
        ``<model>_<last-4-of-serial>`` (e.g. ``MX100_YL5K``) so every node is
        uniquely identifiable in the diagram.
        """
        named = self.name.strip()
        if named:
            return named
        if self.serial:
            return f"{self.model}_{self.serial[-4:]}"
        return self.model or "unknown"


@dataclass
class MerakiNetwork:
    id: str
    name: str
    product_types: List[str] = field(default_factory=list)
    raw: Dict[str, Any] = field(default_factory=dict)


@dataclass
class MerakiExport:
    organization_id: str
    organization: Dict[str, Any]
    networks: List[MerakiNetwork]
    devices: List[MerakiDevice]
    switch_ports: Dict[str, List[Dict[str, Any]]]
    management_interfaces: Dict[str, Dict[str, Any]]
    network_details: Dict[str, Dict[str, Any]]
    lldp_cdp: Dict[str, Dict[str, Any]] = field(default_factory=dict)          # serial -> {ports: {...}}
    switch_port_statuses: Dict[str, List[Dict[str, Any]]] = field(default_factory=dict)  # serial -> [port status]
    switch_routing: Dict[str, List[Dict[str, Any]]] = field(default_factory=dict)       # serial -> [SVIs]
    switch_static_routes: Dict[str, List[Dict[str, Any]]] = field(default_factory=dict)  # serial -> [routes]
    uplink_statuses: List[Dict[str, Any]] = field(default_factory=list)        # org appliance uplinks

    def devices_in(self, network_id: str) -> List[MerakiDevice]:
        return [d for d in self.devices if d.network_id == network_id]

    def detail(self, network_id: str) -> Dict[str, Any]:
        return self.network_details.get(network_id, {})


def _running_sw(device_raw: Dict[str, Any]) -> str:
    """Pull the 'Running software version' string from a device's details list."""
    for d in device_raw.get("details") or []:
        if str(d.get("name", "")).lower().startswith("running software"):
            return str(d.get("value", "")).strip()
    return ""


def _index_statuses(statuses: List[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    return {s.get("serial"): s for s in statuses if s.get("serial")}


def load_export(path: str) -> MerakiExport:
    raw = json.loads(pathlib.Path(path).read_text(encoding="utf-8-sig"))

    networks = [
        MerakiNetwork(
            id=n.get("id", ""), name=n.get("name", "") or n.get("id", ""),
            product_types=list(n.get("productTypes") or []), raw=n,
        )
        for n in raw.get("networks", [])
    ]

    statuses = _index_statuses(raw.get("deviceStatuses", []))
    devices: List[MerakiDevice] = []
    for d in raw.get("devices", []):
        serial = d.get("serial", "")
        st = statuses.get(serial, {})
        devices.append(MerakiDevice(
            serial=serial,
            name=d.get("name", "") or "",
            model=d.get("model", "") or "",
            product_type=d.get("productType", "") or "",
            network_id=d.get("networkId", "") or "",
            mac=d.get("mac", "") or "",
            os_version=_running_sw(d),
            lan_ip=d.get("lanIp") or st.get("lanIp"),
            wan1_ip=d.get("wan1Ip") or st.get("wan1Ip"),
            wan2_ip=d.get("wan2Ip") or st.get("wan2Ip"),
            raw=d,
        ))

    return MerakiExport(
        organization_id=raw.get("organizationId", ""),
        organization=raw.get("organization") or {},
        networks=networks,
        devices=devices,
        switch_ports=raw.get("switchPorts") or {},
        management_interfaces=raw.get("managementInterfaces") or {},
        network_details=raw.get("networkDetails") or {},
        lldp_cdp=raw.get("lldpCdp") or {},
        switch_port_statuses=raw.get("switchPortStatuses") or {},
        switch_routing=raw.get("switchRouting") or {},
        switch_static_routes=raw.get("switchStaticRoutes") or {},
        uplink_statuses=raw.get("applianceUplinkStatuses") or [],
    )
