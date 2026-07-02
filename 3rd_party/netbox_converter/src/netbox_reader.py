# Copyright 2026 Cisco Systems, Inc. and its affiliates
# SPDX-License-Identifier: Apache-2.0

"""netbox_reader.py — load a NetBox export JSON and index it for the mapper.

Thin data-access layer over the combined JSON written by
``fetch_from_netbox.py`` (or a hand-saved API dump). It performs NO topology
logic — it only parses, normalises identifiers, and builds the lookup indexes
(`interfaces_by_device`, `ips_by_interface`, ...) that ``netbox_mapper`` walks.

Only core DCIM/IPAM objects are read; custom fields and plugin data are ignored
so the reader works against any NetBox instance.
"""
from __future__ import annotations

import json
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple


def _disp(obj: Any) -> Optional[str]:
    """Return a nested related-object's name/display, or None."""
    if isinstance(obj, dict):
        return obj.get("name") or obj.get("display") or obj.get("slug")
    return None


def _slug(obj: Any) -> Optional[str]:
    if isinstance(obj, dict):
        return obj.get("slug") or obj.get("value")
    return None


@dataclass
class NetboxData:
    meta: Dict[str, Any]
    sites: List[dict]
    locations: List[dict]
    device_roles: List[dict]
    platforms: List[dict]
    manufacturers: List[dict]
    devices: List[dict]
    interfaces: List[dict]
    cables: List[dict]
    ip_addresses: List[dict]
    vlans: List[dict]
    vrfs: List[dict]
    prefixes: List[dict]

    # Indexes (populated in __post_init__).
    device_by_id: Dict[int, dict] = field(default_factory=dict)
    site_by_id: Dict[int, dict] = field(default_factory=dict)
    interface_by_id: Dict[int, dict] = field(default_factory=dict)
    vlan_by_id: Dict[int, dict] = field(default_factory=dict)
    interfaces_by_device: Dict[int, List[dict]] = field(default_factory=lambda: defaultdict(list))
    ips_by_interface: Dict[int, List[dict]] = field(default_factory=lambda: defaultdict(list))

    def __post_init__(self) -> None:
        self.device_by_id = {d["id"]: d for d in self.devices}
        self.site_by_id = {s["id"]: s for s in self.sites}
        self.interface_by_id = {i["id"]: i for i in self.interfaces}
        self.vlan_by_id = {v["id"]: v for v in self.vlans}
        self.interfaces_by_device = defaultdict(list)
        for i in self.interfaces:
            dev = i.get("device") or {}
            if dev.get("id") is not None:
                self.interfaces_by_device[dev["id"]].append(i)
        self.ips_by_interface = defaultdict(list)
        for ip in self.ip_addresses:
            if ip.get("assigned_object_type") == "dcim.interface" and ip.get("assigned_object_id"):
                self.ips_by_interface[ip["assigned_object_id"]].append(ip)

    # -- device helpers -----------------------------------------------------
    def device_name(self, dev: dict) -> str:
        """Stable display name for a device (NetBox allows unnamed devices)."""
        return dev.get("name") or f"device-{dev.get('id')}"

    def device_site_slug(self, dev: dict) -> Optional[str]:
        return _slug(dev.get("site"))

    def device_site_name(self, dev: dict) -> Optional[str]:
        return _disp(dev.get("site"))

    def device_role(self, dev: dict) -> Tuple[str, str]:
        r = dev.get("role") or {}
        return (r.get("slug") or "", r.get("name") or "")

    def device_model(self, dev: dict) -> str:
        dt = dev.get("device_type") or {}
        return dt.get("model") or ""

    def device_manufacturer(self, dev: dict) -> str:
        dt = dev.get("device_type") or {}
        return _disp(dt.get("manufacturer")) or ""

    def device_platform(self, dev: dict) -> Tuple[str, str]:
        p = dev.get("platform") or {}
        return (p.get("slug") or "", p.get("name") or "")

    def device_status(self, dev: dict) -> str:
        return (dev.get("status") or {}).get("value") or ""

    # -- interface helpers --------------------------------------------------
    def iface_ips(self, iface: dict) -> List[str]:
        """CIDR strings assigned to this interface (e.g. '10.0.1.71/21')."""
        return [ip["address"] for ip in self.ips_by_interface.get(iface["id"], [])
                if ip.get("address")]

    def iface_vrf(self, iface: dict) -> Optional[str]:
        """VRF name for an interface: interface.vrf, else its first IP's vrf."""
        v = _disp(iface.get("vrf"))
        if v:
            return v
        for ip in self.ips_by_interface.get(iface["id"], []):
            vv = _disp(ip.get("vrf"))
            if vv:
                return vv
        return None


def load_export(path: str) -> NetboxData:
    with open(path, "r", encoding="utf-8") as fh:
        raw = json.load(fh)
    return NetboxData(
        meta=raw.get("_meta", {}),
        sites=raw.get("sites", []) or [],
        locations=raw.get("locations", []) or [],
        device_roles=raw.get("device_roles", []) or [],
        platforms=raw.get("platforms", []) or [],
        manufacturers=raw.get("manufacturers", []) or [],
        devices=raw.get("devices", []) or [],
        interfaces=raw.get("interfaces", []) or [],
        cables=raw.get("cables", []) or [],
        ip_addresses=raw.get("ip_addresses", []) or [],
        vlans=raw.get("vlans", []) or [],
        vrfs=raw.get("vrfs", []) or [],
        prefixes=raw.get("prefixes", []) or [],
    )
