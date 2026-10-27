# Copyright 2026 Cisco Systems, Inc. and its affiliates
# SPDX-License-Identifier: Apache-2.0

"""Turn an FMC export JSON document into typed records. No placement here."""
from __future__ import annotations

import ipaddress
import json
import pathlib
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class FmcInterface:
    kind: str
    name: str
    ifname: str
    zone: str
    enabled: bool
    management_only: bool
    ipv4: str
    vlan: Optional[int] = None
    members: List[str] = field(default_factory=list)
    ipv6: List[str] = field(default_factory=list)

    @property
    def cidrs(self) -> List[str]:
        """IPv4 first, then IPv6."""
        return ([self.ipv4] if self.ipv4 else []) + list(self.ipv6)


@dataclass
class StaticRoute:
    interface_name: str
    gateway: str
    destinations: List[str]
    metric: int = 1
    tunneled: bool = False
    is_default: bool = False


@dataclass
class FmcDevice:
    id: str
    name: str
    hostname: str
    model: str
    version: str
    interfaces: List[FmcInterface] = field(default_factory=list)
    static_routes: List[StaticRoute] = field(default_factory=list)


@dataclass
class HaPair:
    name: str
    primary_id: str
    secondary_id: str


@dataclass
class FmcData:
    fmc_name: str
    fmc_hostname: str
    fmc_version: str
    devices: List[FmcDevice]
    ha_pairs: List[HaPair]
    fmc_ipv4: str = ""
    fmc_gateway: str = ""


def _prefix_len(mask: Any) -> Optional[int]:
    """Accept a prefix length (``24`` or ``\"24\"``) or a dotted netmask."""
    if isinstance(mask, int) and not isinstance(mask, bool):
        return mask if 0 <= mask <= 32 else None
    text = str(mask).strip()
    if text.isdigit():
        length = int(text)
        return length if 0 <= length <= 32 else None
    try:
        parts = [int(part) for part in text.split(".")]
    except ValueError:
        return None
    if len(parts) != 4 or any(part < 0 or part > 255 for part in parts):
        return None
    return sum(bin(part).count("1") for part in parts)


def ipv4_cidr(raw: Any) -> str:
    """Return ``address/prefix`` from the FMC ipv4 object, or ''."""
    if not isinstance(raw, dict):
        return ""
    static = raw.get("static") or raw.get("ipAddress") or {}
    if not isinstance(static, dict):
        return ""
    address = static.get("address") or static.get("ipAddress") or ""
    mask = static.get("netmask") or static.get("netMask") or ""
    if not address:
        return ""
    if "/" in str(address):
        return str(address)
    length = _prefix_len(str(mask)) if mask else None
    return f"{address}/{length}" if length is not None else ""


def ipv6_cidrs(raw: Any) -> List[str]:
    """Static ``address/prefix`` entries from the FMC ipv6 object.

    ``linkLocalAddress`` and the router-advertisement ``prefixes`` are not
    interface addresses. An EUI-64 entry carries only the prefix, so the real
    address is unknown and the entry is skipped. Link-local entries in the
    list are skipped as well.
    """
    if not isinstance(raw, dict):
        return []
    entries = raw.get("addresses") or []
    if isinstance(entries, dict):
        entries = [entries]
    found: List[str] = []
    for entry in entries:
        if not isinstance(entry, dict) or _flag(entry.get("enforceEUI64")):
            continue
        address = str(entry.get("address") or "").strip()
        if not address:
            continue
        text = address
        if "/" not in address:
            try:
                length = int(str(entry.get("prefix") or "").strip())
            except ValueError:
                continue
            text = f"{address}/{length}"
        try:
            iface = ipaddress.IPv6Interface(text)
        except ValueError:
            continue
        if iface.ip.is_link_local:
            continue
        cidr = iface.with_prefixlen
        if cidr not in found:
            found.append(cidr)
    return found


def _enabled(value: Any) -> bool:
    """FMC usually sends a JSON boolean. A string 'false' must not become True."""
    if value is None:
        return True
    if isinstance(value, str):
        return value.strip().lower() not in {"false", "no", "0", "disabled"}
    return bool(value)


def _flag(value: Any) -> bool:
    if isinstance(value, str):
        return value.strip().lower() in {"true", "yes", "1"}
    return bool(value)


def _names(value: Any) -> List[str]:
    if not isinstance(value, list):
        return []
    names: List[str] = []
    for item in value:
        if isinstance(item, dict) and item.get("name"):
            names.append(str(item["name"]))
        elif isinstance(item, str) and item:
            names.append(item)
    return names


def parse_interface(raw: Dict[str, Any]) -> FmcInterface:
    raw_type = str(raw.get("type") or "")
    lowered = raw_type.lower()
    if "etherchannel" in lowered or "redundant" in lowered:
        kind = "etherchannel"
    elif "subinterface" in lowered or "subintf" in lowered:
        kind = "subinterface"
    elif "loopback" in lowered:
        kind = "loopback"
    else:
        kind = "physical"
    zone = raw.get("securityZone") or {}
    zone_name = zone.get("name") if isinstance(zone, dict) else ""
    vlan = raw.get("vlanId")
    if vlan is None:
        vlan = raw.get("vlan")
    try:
        vlan_id = int(vlan) if vlan is not None else None
    except (TypeError, ValueError):
        vlan_id = None
    members = _names(
        raw.get("selectedInterfaces")
        or raw.get("memberInterfaces")
        or raw.get("members")
    )
    return FmcInterface(
        kind=kind,
        name=str(raw.get("name") or ""),
        ifname=str(raw.get("ifname") or ""),
        zone=str(zone_name or ""),
        enabled=_enabled(raw.get("enabled", True)),
        management_only=_flag(raw.get("managementOnly") or raw.get("managementonly")),
        ipv4=ipv4_cidr(raw.get("ipv4")),
        vlan=vlan_id,
        members=members,
        ipv6=ipv6_cidrs(raw.get("ipv6")),
    )


_DEFAULT_DESTINATIONS = {
    "any-ipv4", "any", "0.0.0.0/0", "0.0.0.0", "any-ipv6", "::/0", "::",
}


def _route_destinations(raw: Dict[str, Any]) -> List[str]:
    nets = raw.get("selectedNetworks") or raw.get("networks") or []
    if isinstance(nets, dict):
        nets = [nets]
    found: List[str] = []
    for net in nets:
        if isinstance(net, dict):
            found.append(str(net.get("value") or net.get("name") or ""))
        elif isinstance(net, str):
            found.append(net)
    return [item for item in found if item]


def _route_gateway(raw: Dict[str, Any]) -> str:
    gateway = raw.get("gateway") or {}
    if isinstance(gateway, str):
        return gateway
    if not isinstance(gateway, dict):
        return ""
    literal = gateway.get("literal") or {}
    if isinstance(literal, dict) and literal.get("value"):
        return str(literal["value"])
    obj = gateway.get("object") or {}
    if isinstance(obj, dict) and obj.get("value"):
        return str(obj["value"])
    return ""


def parse_route(raw: Dict[str, Any]) -> StaticRoute:
    destinations = _route_destinations(raw)
    try:
        metric = int(raw.get("metricValue") or raw.get("metric") or 1)
    except (TypeError, ValueError):
        metric = 1
    is_default = any(item.strip().lower() in _DEFAULT_DESTINATIONS for item in destinations)
    interface = raw.get("interfaceName") or raw.get("interface") or ""
    if isinstance(interface, dict):
        interface = interface.get("name") or ""
    return StaticRoute(
        interface_name=str(interface or ""),
        gateway=_route_gateway(raw),
        destinations=destinations,
        metric=metric,
        tunneled=_flag(raw.get("isTunneled")),
        is_default=is_default,
    )


def _device(raw: Dict[str, Any]) -> FmcDevice:
    interfaces = [
        parse_interface(item)
        for item in (raw.get("interfaces") or [])
        if isinstance(item, dict)
    ]
    routes = [
        parse_route(item)
        for item in (raw.get("static_routes") or [])
        if isinstance(item, dict)
    ]
    return FmcDevice(
        id=str(raw.get("id") or ""),
        name=str(raw.get("name") or ""),
        hostname=str(raw.get("hostName") or raw.get("hostname") or ""),
        model=str(raw.get("model") or ""),
        version=str(raw.get("sw_version") or raw.get("swVersion") or ""),
        interfaces=interfaces,
        static_routes=routes,
    )


def _ref_id(value: Any) -> str:
    if isinstance(value, dict):
        return str(value.get("id") or "")
    if isinstance(value, str):
        return value
    return ""


def _pair(raw: Dict[str, Any]) -> Optional[HaPair]:
    primary_id = _ref_id(raw.get("primary"))
    secondary_id = _ref_id(raw.get("secondary"))
    if not primary_id or not secondary_id:
        return None
    return HaPair(
        name=str(raw.get("name") or ""),
        primary_id=primary_id,
        secondary_id=secondary_id,
    )


def parse_export(data: Dict[str, Any]) -> FmcData:
    fmc = data.get("fmc") or {}
    if not isinstance(fmc, dict):
        fmc = {}
    devices = [
        _device(item) for item in (data.get("devices") or []) if isinstance(item, dict)
    ]
    pairs = []
    for item in data.get("ha_pairs") or []:
        if isinstance(item, dict):
            pair = _pair(item)
            if pair is not None:
                pairs.append(pair)
    return FmcData(
        fmc_name=str(fmc.get("name") or "FMC"),
        fmc_hostname=str(fmc.get("hostname") or ""),
        fmc_version=str(fmc.get("version") or ""),
        devices=devices,
        ha_pairs=pairs,
        fmc_ipv4=str(fmc.get("ipv4") or ""),
        fmc_gateway=str(fmc.get("gateway") or ""),
    )


def load_export(path: pathlib.Path) -> FmcData:
    with path.open(encoding="utf-8-sig") as fh:
        data = json.load(fh)
    if not isinstance(data, dict):
        raise ValueError("FMC export root must be a JSON object")
    return parse_export(data)
